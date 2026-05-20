"""Model router: task-based routing, fallback, cost tracking."""

from __future__ import annotations

import asyncio
import logging

from nano_notebooklm import config
from nano_notebooklm.ai.base import LLMBackend
from nano_notebooklm.ai.claude_backend import ClaudeBackend
from nano_notebooklm.ai.local_backend import LocalBackend
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
        if config.OPENAI_API_KEY:
            self.backends["openai"] = OpenAIBackend()
        if config.ANTHROPIC_API_KEY:
            self.backends["claude"] = ClaudeBackend()
        if config.LOCAL_LLM_BASE_URL and config.LOCAL_LLM_MODEL:
            self.backends["local"] = LocalBackend()
        if not self.backends:
            logger.warning(
                "No AI backends configured. Set at least one of "
                "OPENAI_API_KEY / ANTHROPIC_API_KEY / LOCAL_LLM_BASE_URL in .env"
            )

    def _resolve_backend(self, task_type: str, backend_override: str | None) -> LLMBackend:
        """Pick a backend for a single call.

        Explicit `backend_override` (e.g. ChatRequest.backend from the
        frontend chip) takes precedence over task-type routing. Unknown
        or unconfigured target → RuntimeError so the endpoint can
        translate to a 422.
        """
        if backend_override:
            if backend_override not in self.backends:
                raise RuntimeError(
                    f"backend {backend_override!r} not configured "
                    f"(available: {sorted(self.backends.keys())})"
                )
            return self.backends[backend_override]
        return self.get_backend(task_type)

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
        backend: str | None = None,
    ) -> LLMResponse:
        """Complete with automatic fallback and retry.

        When `backend` is explicitly pinned by the caller, cross-backend
        fallback is disabled — only same-backend retries happen. The
        caller is responsible for any upstream timeout/fallback chain.
        """
        backend_obj = self._resolve_backend(task_type, backend)
        last_error = None
        allow_fallback = backend is None

        for attempt in range(max_retries):
            try:
                resp = await backend_obj.complete(
                    prompt, system=system, temperature=temperature, max_tokens=max_tokens
                )
                self._track_usage(resp)
                return resp
            except Exception as e:
                last_error = e
                logger.warning(f"[{backend_obj.name}] attempt {attempt + 1} failed: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                    if allow_fallback and attempt == max_retries - 2:
                        fallback = self._get_fallback(backend_obj.name)
                        if fallback:
                            backend_obj = fallback
                            logger.info(f"Falling back to {backend_obj.name}")

        raise RuntimeError(f"All retries exhausted: {last_error}")

    async def complete_stream(
        self,
        prompt: str,
        task_type: str = "",
        system: str = "",
        temperature: float = 0.7,
        max_tokens: int = 4096,
        backend: str | None = None,
    ):
        """Stream content deltas. Routing matches `complete()`. No retry —
        once tokens have shipped, retrying would duplicate output. Backends
        without genuine streaming fall back to single-chunk yield via the
        default `LLMBackend.complete_stream` implementation.

        A trailing ``TruncationSignal`` may follow the last delta when
        the upstream stopped at max_output_tokens / finish_reason='length'.
        """
        backend_obj = self._resolve_backend(task_type, backend)
        async for item in backend_obj.complete_stream(
            prompt, system=system, temperature=temperature, max_tokens=max_tokens,
        ):
            yield item

    async def complete_structured(
        self,
        prompt: str,
        task_type: str = "",
        system: str = "",
        temperature: float = 0.3,
        max_tokens: int = 4096,
        backend: str | None = None,
    ) -> dict:
        backend_obj = self._resolve_backend(task_type, backend)
        result = await backend_obj.complete_structured(
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
