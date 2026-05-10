"""OpenAI-compatible backend (supports codex proxy and standard OpenAI)."""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import openai

from nano_notebooklm import config
from nano_notebooklm.ai.base import LLMBackend
from nano_notebooklm.types import LLMResponse

_executor = ThreadPoolExecutor(max_workers=4)

# fix-all v4 #B5: same cap discipline as agent_loop._CANCEL_WATCHER_LIMIT.
# Stream endpoints (notes / report) reach this code path; without a cap
# every concurrent stream span an unbounded watcher thread.
_CANCEL_WATCHER_LIMIT = threading.BoundedSemaphore(
    value=int(os.getenv("OPENAI_CANCEL_WATCHER_LIMIT", "64")),
)

# Per-request HTTP timeout for the underlying httpx client. Without this the
# sync client blocks indefinitely on a stalled provider; combined with the
# `asyncio.wait_for` wrapper in qa_skill (translate path), an unset timeout
# means the executor thread holds the connection even after wait_for cancels
# the awaiting coroutine. Tunable via env so the long generations (notes /
# report) can override.
_DEFAULT_HTTP_TIMEOUT = float(os.getenv("OPENAI_HTTP_TIMEOUT_SECONDS", "120"))


class OpenAIBackend(LLMBackend):
    name = "openai"

    def __init__(self, api_key: str = "", base_url: str = "", model: str = ""):
        self.api_key = api_key or config.OPENAI_API_KEY
        self.base_url = (base_url or config.OPENAI_BASE_URL).rstrip("/")
        self.model = model or config.OPENAI_MODEL
        self._is_codex = "codex" in self.base_url.lower()
        # Use sync client for codex compatibility. Configure httpx timeout so a
        # stalled upstream actually aborts — `asyncio.wait_for` alone can't
        # cancel a sync call running in a thread executor.
        self.client = openai.OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=httpx.Timeout(_DEFAULT_HTTP_TIMEOUT, connect=10.0),
        )

    async def complete(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        start = time.time()
        loop = asyncio.get_event_loop()
        if self._is_codex:
            return await loop.run_in_executor(
                _executor, self._complete_codex_sync, prompt, system, temperature, max_tokens, start
            )
        return await loop.run_in_executor(
            _executor, self._complete_chat_sync, prompt, system, temperature, max_tokens, start
        )

    def _complete_codex_sync(
        self, prompt: str, system: str, temperature: float, max_tokens: int, start: float,
    ) -> LLMResponse:
        """Call codex proxy using responses API (sync, streaming)."""
        input_msgs = []
        if system:
            input_msgs.append({"role": "system", "content": system})
        input_msgs.append({"role": "user", "content": prompt})

        # fix-all v3 #M7: pass `max_output_tokens` so the per-skill caps
        # (notes 8K, report 8K, qa 4K) actually clamp the codex side.
        # Some proxy variants don't accept the field — fall back if so.
        try:
            stream = self.client.responses.create(
                model=self.model,
                input=input_msgs,
                temperature=temperature,
                max_output_tokens=max_tokens,
                stream=True,
            )
        except TypeError:
            stream = self.client.responses.create(
                model=self.model,
                input=input_msgs,
                temperature=temperature,
                stream=True,
            )

        content = ""
        for event in stream:
            if event.type == "response.output_text.delta":
                content += event.delta

        return LLMResponse(
            content=content,
            model=self.model,
            input_tokens=0,
            output_tokens=0,
            latency_ms=self._elapsed_ms(start),
        )

    def _complete_chat_sync(
        self, prompt: str, system: str, temperature: float, max_tokens: int, start: float,
    ) -> LLMResponse:
        """Use standard OpenAI chat completions API (sync)."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        resp = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        choice = resp.choices[0]
        usage = resp.usage
        return LLMResponse(
            content=choice.message.content or "",
            model=resp.model or self.model,
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            latency_ms=self._elapsed_ms(start),
        )

    async def complete_stream(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ):
        """Real streaming — yield each provider delta as it arrives.

        review-swarm fix-all v3 #H7: bound the bridge queue so a slow /
        disconnected consumer back-pressures the producer instead of
        buffering unbounded provider tokens; spin off a cancel watcher
        thread that closes the upstream stream as soon as the consumer
        leaves so the executor thread doesn't keep the LLM call running
        (and billable) after the client is gone.
        """
        loop = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        cancel_event = threading.Event()
        active_stream: dict = {"obj": None}
        put_timeout = float(os.getenv("OPENAI_STREAM_PUT_TIMEOUT_S", "5.0"))

        def _thread_put(item):
            try:
                fut = asyncio.run_coroutine_threadsafe(queue.put(item), loop)
                fut.result(timeout=put_timeout)
            except Exception:
                cancel_event.set()

        def _cancel_watcher():
            cancel_event.wait(timeout=180.0)
            obj = active_stream.get("obj")
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass

        if _CANCEL_WATCHER_LIMIT.acquire(blocking=False):
            def _wrapped_watcher():
                try:
                    _cancel_watcher()
                finally:
                    _CANCEL_WATCHER_LIMIT.release()
            threading.Thread(target=_wrapped_watcher, daemon=True,
                             name="nlm-stream-cancel-watcher").start()

        def _producer():
            try:
                if self._is_codex:
                    input_msgs = []
                    if system:
                        input_msgs.append({"role": "system", "content": system})
                    input_msgs.append({"role": "user", "content": prompt})
                    try:
                        stream = self.client.responses.create(
                            model=self.model,
                            input=input_msgs,
                            temperature=temperature,
                            max_output_tokens=max_tokens,
                            stream=True,
                        )
                    except TypeError:
                        stream = self.client.responses.create(
                            model=self.model,
                            input=input_msgs,
                            temperature=temperature,
                            stream=True,
                        )
                    active_stream["obj"] = stream
                    for event in stream:
                        if cancel_event.is_set():
                            break
                        if event.type == "response.output_text.delta":
                            _thread_put(event.delta)
                else:
                    messages = []
                    if system:
                        messages.append({"role": "system", "content": system})
                    messages.append({"role": "user", "content": prompt})
                    stream = self.client.chat.completions.create(
                        model=self.model,
                        messages=messages,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        stream=True,
                    )
                    active_stream["obj"] = stream
                    for event in stream:
                        if cancel_event.is_set():
                            break
                        if not event.choices:
                            continue
                        delta = event.choices[0].delta.content
                        if delta:
                            _thread_put(delta)
            except Exception as exc:  # surface to consumer
                if not cancel_event.is_set():
                    _thread_put(exc)
            finally:
                cancel_event.set()
                obj = active_stream.get("obj")
                if obj is not None:
                    try:
                        obj.close()
                    except Exception:
                        pass
                _thread_put(None)

        producer_fut = loop.run_in_executor(_executor, _producer)
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            cancel_event.set()
            # Ensure producer completes / errors are surfaced
            try:
                await producer_fut
            except Exception:
                pass

    async def complete_structured(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> dict:
        json_system = (system + "\n\n" if system else "") + (
            "You MUST respond with valid JSON only. No markdown, no explanation."
        )
        resp = await self.complete(prompt, system=json_system, temperature=temperature, max_tokens=max_tokens)
        return _parse_json(resp.content)


def _parse_json(text: str) -> dict:
    """Parse JSON from LLM response, handling markdown code blocks."""
    text = text.strip()
    if "```" in text:
        m = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
        if m:
            text = m.group(1).strip()
    return json.loads(text)
