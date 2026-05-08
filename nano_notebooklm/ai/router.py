"""Model router: task-based routing, fallback, cost tracking."""

from __future__ import annotations

import asyncio
import logging
import time

from nano_notebooklm import config
from nano_notebooklm.ai.base import LLMBackend
from nano_notebooklm.ai.claude_backend import ClaudeBackend
from nano_notebooklm.ai.openai_backend import OpenAIBackend
from nano_notebooklm.types import LLMResponse, TokenUsage

logger = logging.getLogger(__name__)


class ModelRouter:
    """Routes tasks to appropriate LLM backends with fallback and cost tracking."""

    def __init__(self):
        self.backends: dict[str, LLMBackend] = {}
        self.usage = TokenUsage()
        self._init_backends()

    def _init_backends(self):
        if config.ANTHROPIC_API_KEY:
            self.backends["claude"] = ClaudeBackend()
        if config.OPENAI_API_KEY:
            self.backends["openai"] = OpenAIBackend()
        if not self.backends:
            logger.warning("No AI backends configured. Set API keys in .env")

    def get_backend(self, task_type: str = "") -> LLMBackend:
        """Get the appropriate backend for a task type."""
        target = config.TASK_ROUTES.get(task_type, config.DEFAULT_BACKEND)

        if target == "alternate":
            # Return the non-default backend for cross-review
            for name, backend in self.backends.items():
                if name != config.DEFAULT_BACKEND:
                    return backend
            target = config.DEFAULT_BACKEND

        if target in self.backends:
            return self.backends[target]

        # Fallback to whatever is available
        if self.backends:
            return next(iter(self.backends.values()))

        raise RuntimeError("No AI backend available. Configure API keys in .env")

    async def complete(
        self,
        prompt: str,
        task_type: str = "",
        system: str = "",
        temperature: float = 0.7,
        max_tokens: int = 4096,
        max_retries: int = 3,
    ) -> LLMResponse:
        """Complete with automatic fallback and retry."""
        backend = self.get_backend(task_type)
        last_error = None

        for attempt in range(max_retries):
            try:
                resp = await backend.complete(
                    prompt, system=system, temperature=temperature, max_tokens=max_tokens
                )
                self._track_usage(resp)
                return resp
            except Exception as e:
                last_error = e
                logger.warning(f"[{backend.name}] attempt {attempt + 1} failed: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                    # Try fallback backend on last retry
                    if attempt == max_retries - 2:
                        fallback = self._get_fallback(backend.name)
                        if fallback:
                            backend = fallback
                            logger.info(f"Falling back to {backend.name}")

        raise RuntimeError(f"All retries exhausted: {last_error}")

    async def complete_stream(
        self,
        prompt: str,
        task_type: str = "",
        system: str = "",
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ):
        """Stream content deltas. Routing matches `complete()`. No retry —
        once tokens have shipped, retrying would duplicate output. Backends
        without genuine streaming fall back to single-chunk yield via the
        default `LLMBackend.complete_stream` implementation."""
        backend = self.get_backend(task_type)
        async for delta in backend.complete_stream(
            prompt, system=system, temperature=temperature, max_tokens=max_tokens,
        ):
            yield delta

    async def complete_structured(
        self,
        prompt: str,
        task_type: str = "",
        system: str = "",
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> dict:
        """Structured JSON completion with routing."""
        backend = self.get_backend(task_type)
        result = await backend.complete_structured(
            prompt, system=system, temperature=temperature, max_tokens=max_tokens
        )
        return result

    def _get_fallback(self, current_name: str) -> LLMBackend | None:
        for name, backend in self.backends.items():
            if name != current_name:
                return backend
        return None

    def _track_usage(self, resp: LLMResponse):
        self.usage.input_tokens += resp.input_tokens
        self.usage.output_tokens += resp.output_tokens

    def get_usage_summary(self) -> dict:
        return {
            "input_tokens": self.usage.input_tokens,
            "output_tokens": self.usage.output_tokens,
            "backends_available": list(self.backends.keys()),
        }
