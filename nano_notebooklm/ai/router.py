"""Model router: task-based routing, fallback, cost tracking."""

from __future__ import annotations

import asyncio
import logging
import time

from nano_notebooklm import config
from nano_notebooklm.ai.base import LLMBackend
from nano_notebooklm.ai.claude_backend import ClaudeBackend
from nano_notebooklm.ai.openai_backend import OpenAIBackend
from nano_notebooklm.ai.qwen_raft_backend import QwenRaftBackend
from nano_notebooklm.types import LLMResponse, TokenUsage

logger = logging.getLogger(__name__)


# R4-5 part 2: map user-facing backend names (ChatRequest.backend Literal)
# to internal backends dict keys. "codex" is the GOAL.md user-visible name
# for the OpenAI-compatible proxy backend; internally it's keyed "openai"
# because that's the OpenAIBackend class registered in _init_backends.
_BACKEND_NAME_ALIASES: dict[str, str] = {
    "codex": "openai",
    "qwen_raft": "qwen_raft",
}


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
        # R4-5 part 2: register Qwen-RAFT only when QWEN_RAFT_URL is set —
        # the backend's `configured` property would otherwise be False and
        # every call would raise. Operators enable Qwen by setting
        # QWEN_RAFT_URL in .env; the chat endpoint then accepts
        # `backend="qwen_raft"` requests.
        if config.QWEN_RAFT_URL:
            self.backends["qwen_raft"] = QwenRaftBackend()
        if not self.backends:
            logger.warning("No AI backends configured. Set API keys in .env")

    def _resolve_backend(self, task_type: str, backend_override: str | None) -> LLMBackend:
        """R4-5 part 2: resolve a backend for a single call. When the
        caller passes an explicit `backend_override` (user-facing chip
        value from ChatRequest.backend), it takes precedence over the
        task-type routing. Aliases ("codex" → "openai") let the wire
        contract stay decoupled from the internal backend key.
        Unknown override or unconfigured target → RuntimeError so the
        endpoint can translate to a 422 / fall back gracefully.
        """
        if backend_override:
            internal = _BACKEND_NAME_ALIASES.get(backend_override, backend_override)
            if internal not in self.backends:
                raise RuntimeError(
                    f"backend {backend_override!r} not configured "
                    f"(internal key {internal!r} missing from router.backends)"
                )
            return self.backends[internal]
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

        R4-5 part 2: `backend` lets the caller override task-type routing
        for a single call (e.g. ChatRequest.backend="qwen_raft"). When
        the override is supplied, fallback to the task-type default
        backend is also disabled inside the retry loop — the caller is
        responsible for upstream timeout / fallback handling (see
        QASkill._answer_rag for the qwen→codex chain).
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
                    # Try fallback backend on last retry — but only if the
                    # caller didn't pin a backend explicitly (R4-5 part 2:
                    # qwen_raft override must not silently switch to codex
                    # inside the router; that's the caller's call).
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

        Truncation contract: yielded items are normally str content deltas,
        but a backend MAY emit a trailing ``TruncationSignal`` (see
        ``ai.base.TruncationSignal``) when the upstream stopped at
        max_output_tokens / finish_reason='length'. The signal passes
        through this router unchanged — opt-in callers ``isinstance``-guard
        for it to surface a "⚠️ truncated" affordance to the user.

        R4-5 part 2: optional `backend` override (same semantics as
        `complete()`).
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
        """Structured JSON completion with routing.

        R4-5 part 2: optional `backend` override (same semantics as
        `complete()`).
        """
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
