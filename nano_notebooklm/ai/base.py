"""Abstract LLM backend interface."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import AsyncIterator

from nano_notebooklm.types import LLMResponse


class LLMBackend(ABC):
    """Base class for all LLM backends."""

    name: str

    @abstractmethod
    async def complete(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        """Generate a text completion."""
        ...

    @abstractmethod
    async def complete_structured(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> dict:
        """Generate a structured JSON response."""
        ...

    async def complete_stream(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> AsyncIterator[str]:
        """Yield content deltas as the model generates them.

        Default implementation falls back to non-streaming `complete()` and
        yields the full content as a single chunk. Backends that genuinely
        stream (codex responses API, OpenAI chat-completions stream=True,
        Anthropic streaming) override this for token-by-token UX.
        """
        resp = await self.complete(prompt, system=system,
                                   temperature=temperature, max_tokens=max_tokens)
        if resp.content:
            yield resp.content

    @staticmethod
    def _elapsed_ms(start: float) -> float:
        return (time.time() - start) * 1000
