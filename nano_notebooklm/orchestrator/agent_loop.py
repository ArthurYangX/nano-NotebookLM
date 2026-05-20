"""Multi-turn tool-calling agent loop.

Yields NDJSON-friendly events as the model thinks → calls tools → answers.

Reference (design only):
- previous-agent/src/query.ts:307 — `while (true)` loop shape; stop when a
  turn finishes without tool_use blocks.
- previous-agent/src/services/tools/toolOrchestration.ts — read-only batching
  (handled by `agent_tools.run_tool_calls`).

LLM transport: `chat.completions.create(stream=True, tools=[...])`. Codex
proxy may or may not support the chat-completions tool surface; if it
doesn't, configure OPENAI_BASE_URL to a vanilla OpenAI-compatible endpoint
for the agent endpoint, and keep codex for non-tool generations.

Frontend rendering contract:
- `tool_result.result` and `error.partial` carry untrusted text (course
  material, LLM output). The frontend MUST render these as preformatted
  text (e.g. `<pre>` / `<code>`), never as HTML or markdown.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import AsyncIterator, Callable

from nano_notebooklm.ai.openai_backend import OpenAIBackend
from nano_notebooklm.orchestrator.agent_tools import (
    ToolCall,
    ToolRegistry,
    run_tool_calls,
)

logger = logging.getLogger(__name__)


def _read_int_env(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return max(lo, min(hi, int(raw)))
    except ValueError:
        return default


DEFAULT_MAX_TURNS = _read_int_env("AGENT_MAX_TURNS", 8, 1, 32)
DEFAULT_MAX_TOKENS = _read_int_env("AGENT_MAX_TOKENS", 2048, 256, 16384)
DEFAULT_TEMPERATURE = float(os.getenv("AGENT_TEMPERATURE", "0.3"))

# Aggregate cap on tool_result bytes appended to `messages` across the whole
# agent run. Hitting it terminates with done.budget_hit=True; without it a
# misbehaving model can balloon the prompt to MB-scale.
TOOL_RESULT_BUDGET_BYTES = _read_int_env(
    "AGENT_TOOL_RESULT_BUDGET_BYTES", 200 * 1024, 8 * 1024, 4 * 1024 * 1024
)

# Producer queue maxsize. Bounded so a slow / disconnected consumer back-
# pressures the upstream stream instead of buffering unbounded deltas.
QUEUE_MAXSIZE = _read_int_env("AGENT_QUEUE_MAXSIZE", 256, 16, 4096)

# Max time the producer waits for the queue to drain on each put. Hitting
# it means the consumer is gone — the producer sets cancel_event and bails.
PRODUCER_QUEUE_PUT_TIMEOUT_S = float(os.getenv("AGENT_QUEUE_PUT_TIMEOUT_S", "5.0"))

# Dedicated executor so a multi-turn agent (which holds 1 slot for the
# entire run) cannot starve notes/report/qa endpoints sharing the
# OpenAIBackend `_executor` pool. Two workers handles one or two concurrent
# agent users on a single-user dev box.
_agent_executor = ThreadPoolExecutor(
    max_workers=_read_int_env("AGENT_EXECUTOR_WORKERS", 2, 1, 16),
    thread_name_prefix="nlm-agent",
)

# fix-all v4 #B5: cap concurrent cancel-watcher threads. Each request
# previously span an unbounded daemon thread; with no auth + no rate
# limit, an attacker could open thousands of concurrent agent streams
# and exhaust the OS thread limit. When the cap is reached new requests
# fall back to producer-side cancel polling (slightly slower cancel,
# never a thread-leak).
_CANCEL_WATCHER_LIMIT = threading.BoundedSemaphore(
    value=_read_int_env("AGENT_CANCEL_WATCHER_LIMIT", 64, 1, 1024),
)


SYSTEM_PROMPT_BASE = """You are nano-NOTEBOOKLM's study assistant agent. You help students understand their course materials.

Tools available:
- `search_kb` — hybrid retrieval over indexed course chunks. Always your first move for content questions.
- `read_chunk` — expand context for a specific chunk_id from a search result.
- `list_courses` — list available courses when you're unsure of the right course_id.
- `generate_note` — write a structured note file (only when the user explicitly asks for a "note" / "笔记").

Style:
- Answer in the language the user wrote in (Chinese stays Chinese, English stays English).
- Cite by source_file + location (e.g. `[lecture3.pdf p.12]`) when answering from search results.
- Keep answers tight and grounded. If you don't have evidence, say so plainly — never invent quotes, page numbers, or chunk_ids.
- Don't paraphrase the question back; jump straight to the answer.
"""


def compose_system_prompt(
    course_id: str | None,
    course_names: list[str] | None = None,
    user_lang: str | None = None,
) -> str:
    parts = [SYSTEM_PROMPT_BASE]
    if course_id:
        parts.append(
            f"## Active course\nThe user is focused on `{course_id}`. Default `course_id` to "
            f"this in `search_kb` unless the user names a different one."
        )
    if course_names:
        listed = ", ".join(f"`{c}`" for c in course_names)
        parts.append(f"## Available courses\n{listed}")
    # Round 3 #R3-2: explicit lang binding overrides the agent's "Answer in
    # the language the user wrote in" rule when the student has expressed a
    # preference. Imported lazily to avoid a circular import (agent_loop is
    # imported by api/server.py at module load).
    from nano_notebooklm.ai import prompt_templates as _prompts
    binding = _prompts.USER_LANG_BINDING(user_lang)
    if binding:
        parts.append(f"## User language preference\n{binding}")
    return "\n\n".join(parts)


# ── Public agent loop ─────────────────────────────────────────────────


LLMStream = Callable[..., AsyncIterator[dict]]


async def run_agent(
    user_question: str,
    *,
    registry: ToolRegistry,
    backend: OpenAIBackend | None = None,
    course_id: str | None = None,
    course_names: list[str] | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    llm_stream: LLMStream | None = None,
    user_lang: str | None = None,
) -> AsyncIterator[dict]:
    """Run a multi-turn agent loop, yielding NDJSON-shaped events.

    Event shapes yielded to the caller:
      {"type": "text", "delta": str}
      {"type": "tool_call", "name": str, "arguments": dict, "call_id": str}
      {"type": "tool_result", "name": str, "call_id": str, "result": str}
      {"type": "done", "answer": str, "turns": int, "max_turns_hit": bool, "budget_hit": bool}
      {"type": "error", "error": str, "partial": str}

    `llm_stream` defaults to a chat.completions bridge built on `backend`;
    tests inject a fake to script the conversation deterministically.
    """
    if llm_stream is None:
        if backend is None:
            raise ValueError("run_agent requires either `backend` or `llm_stream`")
        llm_stream = make_chat_completions_stream(backend)

    system_prompt = compose_system_prompt(course_id, course_names, user_lang=user_lang)
    messages: list[dict] = [{"role": "user", "content": user_question}]
    tools_schema = registry.openai_schemas()

    text_buf = ""
    tool_result_bytes = 0
    turn = 0
    while turn < max_turns:
        turn += 1
        text_this_turn = ""
        assistant_msg: dict | None = None
        stream_error: str | None = None

        try:
            async for evt in llm_stream(
                system=system_prompt,
                messages=messages,
                tools=tools_schema,
                temperature=DEFAULT_TEMPERATURE,
                max_tokens=DEFAULT_MAX_TOKENS,
            ):
                etype = evt.get("type")
                if etype == "text_delta":
                    delta = evt.get("delta", "")
                    if delta:
                        text_this_turn += delta
                        yield {"type": "text", "delta": delta}
                elif etype == "assistant_message":
                    assistant_msg = evt.get("message")
                elif etype == "error":
                    stream_error = evt.get("error", "stream_error")
                    break
        except Exception:
            logger.exception("agent loop failed at turn %d (llm stream)", turn)
            yield {"type": "error", "error": "agent_error",
                   "partial": text_buf + text_this_turn}
            return

        if stream_error is not None:
            yield {"type": "error", "error": stream_error,
                   "partial": text_buf + text_this_turn}
            return

        text_buf += text_this_turn

        if assistant_msg is None:
            yield {"type": "error", "error": "no_assistant_message",
                   "partial": text_buf}
            return

        messages.append(assistant_msg)

        tool_calls_raw = assistant_msg.get("tool_calls") or []
        tool_calls = [
            ToolCall(
                call_id=tc["id"],
                name=tc["function"]["name"],
                arguments_raw=tc["function"].get("arguments", "") or "",
            )
            for tc in tool_calls_raw
        ]

        if not tool_calls:
            yield {"type": "done", "answer": text_buf, "turns": turn,
                   "max_turns_hit": False, "budget_hit": False}
            return

        for call in tool_calls:
            yield {"type": "tool_call", "name": call.name,
                   "arguments": call.arguments, "call_id": call.call_id}

        try:
            results = await run_tool_calls(tool_calls, registry)
        except Exception:
            logger.exception("agent loop failed at turn %d (tool exec)", turn)
            yield {"type": "error", "error": "tool_execution_failed",
                   "partial": text_buf}
            return

        for call, result_str in results:
            tool_result_bytes += len(result_str.encode("utf-8"))
            messages.append({
                "role": "tool",
                "tool_call_id": call.call_id,
                "content": result_str,
            })
            yield {"type": "tool_result", "name": call.name,
                   "call_id": call.call_id, "result": result_str}

        if tool_result_bytes > TOOL_RESULT_BUDGET_BYTES:
            yield {"type": "done",
                   "answer": text_buf or "(stopped — tool_result budget exceeded)",
                   "turns": turn, "max_turns_hit": False, "budget_hit": True}
            return

    yield {"type": "done",
           "answer": text_buf or "(reached max turns without final answer)",
           "turns": turn, "max_turns_hit": True, "budget_hit": False}


# ── chat.completions streaming bridge ─────────────────────────────────


def make_chat_completions_stream(backend: OpenAIBackend) -> LLMStream:
    """Bridge `client.chat.completions.create(stream=True, tools=...)` (sync,
    blocking iterator) to an async event stream consumable by `run_agent`.

    Cancellation: when the consumer is cancelled (HTTP client disconnect,
    asyncio.CancelledError), the consumer's `finally` sets `cancel_event`;
    the producer thread polls it on each delta and exits early, then closes
    the upstream stream so OpenAI tokens stop flowing through the executor.
    Without this, a single cancelled agent run can pin one thread of the
    dedicated 2-worker pool until the model finishes naturally (multi-second).

    Yields:
      {"type": "text_delta", "delta": str}
      {"type": "assistant_message", "message": {role, content, tool_calls?}}
      {"type": "error", "error": str}    — stable code, not the raw exception
    """

    async def _stream(*, system: str, messages: list[dict], tools: list[dict],
                      temperature: float, max_tokens: int):
        loop = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        cancel_event = threading.Event()
        active_stream: dict = {"obj": None}
        full_messages = [{"role": "system", "content": system}, *messages]

        def thread_put_blocking(item):
            """Put with backpressure. If the consumer is gone (timeout / loop
            closed), set cancel_event so the producer aborts on its next tick."""
            try:
                fut = asyncio.run_coroutine_threadsafe(queue.put(item), loop)
                fut.result(timeout=PRODUCER_QUEUE_PUT_TIMEOUT_S)
            except Exception:
                cancel_event.set()

        # fix-all v3 #H6: a watcher thread closes the upstream stream as
        # soon as cancel_event fires. Without it, a client disconnect
        # during the `client.chat.completions.create(...)` connect / first-
        # token wait would pin one of the dedicated 2-worker agent slots
        # for the full HTTP timeout — only when the loop reached the
        # `for event in stream` body could it observe the cancel.
        # fix-all v4 #B5: gated by _CANCEL_WATCHER_LIMIT so an attacker
        # opening thousands of streams can't spawn unbounded threads.
        def cancel_watcher():
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
                    cancel_watcher()
                finally:
                    _CANCEL_WATCHER_LIMIT.release()
            threading.Thread(target=_wrapped_watcher, daemon=True,
                             name="nlm-agent-cancel-watcher").start()

        def producer():
            try:
                stream = backend.client.chat.completions.create(
                    model=backend.model,
                    messages=full_messages,
                    tools=tools,
                    tool_choice="auto",
                    temperature=temperature,
                    max_tokens=max_tokens,
                    stream=True,
                )
                active_stream["obj"] = stream
                content_buf = ""
                tc_buffer: dict[int, dict] = {}

                for event in stream:
                    if cancel_event.is_set():
                        break
                    if not event.choices:
                        continue
                    delta = event.choices[0].delta
                    if delta is None:
                        continue
                    if getattr(delta, "content", None):
                        content_buf += delta.content
                        thread_put_blocking({"type": "text_delta", "delta": delta.content})
                    if getattr(delta, "tool_calls", None):
                        for tc in delta.tool_calls:
                            idx = getattr(tc, "index", 0) or 0
                            buf = tc_buffer.setdefault(
                                idx, {"id": "", "name": "", "args": ""}
                            )
                            tc_id = getattr(tc, "id", None)
                            if tc_id:
                                buf["id"] = tc_id
                            fn = getattr(tc, "function", None)
                            if fn is not None:
                                if getattr(fn, "name", None):
                                    buf["name"] += fn.name
                                if getattr(fn, "arguments", None):
                                    buf["args"] += fn.arguments

                if not cancel_event.is_set():
                    msg: dict = {"role": "assistant", "content": content_buf or None}
                    if tc_buffer:
                        msg["tool_calls"] = [
                            {
                                "id": v["id"] or f"call_{idx}",
                                "type": "function",
                                "function": {
                                    "name": v["name"],
                                    "arguments": v["args"] or "{}",
                                },
                            }
                            for idx, v in sorted(tc_buffer.items())
                        ]
                    thread_put_blocking({"type": "assistant_message", "message": msg})
            except Exception:
                # Don't leak vendor exception strings to clients (they may
                # carry URL paths, model names, sometimes API-key shape
                # hints). Log the full trace, ship a stable code.
                logger.exception("agent producer failed")
                if not cancel_event.is_set():
                    thread_put_blocking({"type": "error", "error": "upstream_error"})
            finally:
                # fix-all v3 #H6: ensure the cancel watcher exits even on
                # natural completion (no client cancel) — otherwise it sits
                # idle for 180s holding a stack frame.
                cancel_event.set()
                stream_obj = active_stream.get("obj")
                if stream_obj is not None:
                    try:
                        stream_obj.close()
                    except Exception:
                        pass
                # fix-all v3 #H5: sentinel must use the same back-pressure
                # path as deltas. The previous `put_nowait` could
                # silently raise QueueFull if 256 buffered events filled
                # the bounded queue, leaving the consumer stuck on
                # `queue.get()` forever (no sentinel ever arrived).
                try:
                    thread_put_blocking(None)
                except Exception:
                    pass

        loop.run_in_executor(_agent_executor, producer)
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item
        finally:
            # Idempotent. Ensures the producer's `for event in stream:` exits
            # even if the consumer left for any reason (cancel, exception,
            # natural end before sentinel).
            cancel_event.set()

    return _stream
