"""Abstract LLM backend interface."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod

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

    @staticmethod
    def _elapsed_ms(start: float) -> float:
        return (time.time() - start) * 1000
