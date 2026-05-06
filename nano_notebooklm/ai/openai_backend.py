"""OpenAI-compatible backend (supports codex proxy and standard OpenAI)."""

from __future__ import annotations

import asyncio
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor

import openai

from nano_notebooklm import config
from nano_notebooklm.ai.base import LLMBackend
from nano_notebooklm.types import LLMResponse

_executor = ThreadPoolExecutor(max_workers=4)


class OpenAIBackend(LLMBackend):
    name = "openai"

    def __init__(self, api_key: str = "", base_url: str = "", model: str = ""):
        self.api_key = api_key or config.OPENAI_API_KEY
        self.base_url = (base_url or config.OPENAI_BASE_URL).rstrip("/")
        self.model = model or config.OPENAI_MODEL
        self._is_codex = "codex" in self.base_url.lower()
        # Use sync client for codex compatibility
        self.client = openai.OpenAI(api_key=self.api_key, base_url=self.base_url)

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
