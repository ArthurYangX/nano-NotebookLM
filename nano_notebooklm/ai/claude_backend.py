"""Anthropic Claude backend."""

from __future__ import annotations

import json
import re
import time

import anthropic

from nano_notebooklm import config
from nano_notebooklm.ai.base import LLMBackend
from nano_notebooklm.types import LLMResponse


class ClaudeBackend(LLMBackend):
    name = "claude"

    def __init__(self, api_key: str = "", model: str = "", http_timeout: float | None = None):
        self.api_key = api_key or config.ANTHROPIC_API_KEY
        self.model = model or config.CLAUDE_MODEL
        # `http_timeout` is honoured for parity with OpenAIBackend so the
        # `/api/providers/{id}/test` endpoint can drop the ceiling on a
        # misconfigured row. None → SDK default.
        if http_timeout is not None:
            self.client = anthropic.AsyncAnthropic(api_key=self.api_key, timeout=http_timeout)
        else:
            self.client = anthropic.AsyncAnthropic(api_key=self.api_key)

    async def complete(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        start = time.time()
        kwargs: dict = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system

        resp = await self.client.messages.create(**kwargs)
        return LLMResponse(
            content=resp.content[0].text,
            model=resp.model,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
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
