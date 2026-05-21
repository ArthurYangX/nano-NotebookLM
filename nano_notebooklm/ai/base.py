"""Abstract LLM backend interface."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator, Union

from nano_notebooklm.types import LLMResponse


@dataclass(frozen=True)
class TruncationSignal:
    """End-of-stream marker yielded by ``complete_stream`` when the upstream
    LLM stopped because it hit max_output_tokens / finish_reason=='length'
    rather than completing naturally.

    Why a sentinel instead of an exception: a truncated stream still
    delivered useful partial content; callers should keep that content and
    surface a visible "⚠️ truncated" affordance, not abort. Tagging the
    end-of-stream with a typed sentinel lets the existing
    ``async for delta in router.complete_stream(...)`` shape keep working
    while letting opt-in callers `isinstance`-guard for it::

        truncated = False
        async for item in router.complete_stream(...):
            if isinstance(item, TruncationSignal):
                truncated = True
                continue
            partial += item

    ``reason`` carries the upstream-reported reason string verbatim
    ("length" for chat.completions, "max_output_tokens" for the codex
    responses API) so logs can attribute the truncation precisely.
    """
    reason: str = "length"


# Public yield type for `complete_stream`. Backends MAY yield a single
# trailing TruncationSignal; all other yielded items are content delta
# strings. Existing callers that only `+= delta` keep working because
# Python's string concat will raise TypeError on a sentinel instance —
# but the sentinel is only emitted at end-of-stream, AFTER the last
# delta, so a caller that breaks out of the loop on first non-str item
# (or simply guards `if isinstance(item, str)`) is safe.
StreamItem = Union[str, TruncationSignal]


class LLMBackend(ABC):
    """Base class for all LLM backends.

    ``name`` is the routing key — set to the class-family default
    ("openai" / "claude") at the class level, then overridden to the
    provider id ("openai-main", "claude-main", or a user-added id) by
    ``ModelRouter._build_backend`` at construction time so log lines
    and cross-review's "alternate" lookup track the provider id
    instead of the class family.

    ``kind`` is the providers-matrix shape ("openai_compat",
    "openai_compat_local", "anthropic"). Default is "" so any future
    code path that instantiates a backend without going through
    ``_build_backend`` (e.g. unit tests, direct construction in a CLI
    helper) doesn't ``AttributeError`` on ``getattr(b, "kind", "")``
    — but callers that need to filter by kind (``get_openai_compat``,
    the agent-stream endpoint) must still go through
    ``_build_backend`` so the value is populated correctly.
    """

    name: str = ""
    kind: str = ""

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
    ) -> AsyncIterator[StreamItem]:
        """Yield content deltas as the model generates them.

        Default implementation falls back to non-streaming `complete()` and
        yields the full content as a single chunk. Backends that genuinely
        stream (codex responses API, OpenAI chat-completions stream=True,
        Anthropic streaming) override this for token-by-token UX.

        Truncation contract: when the upstream LLM stopped because it hit
        max_output_tokens / finish_reason='length', backends MAY yield a
        single trailing ``TruncationSignal`` AFTER the final content delta.
        The default implementation never emits one (it has no provider
        metadata to inspect).
        """
        resp = await self.complete(prompt, system=system,
                                   temperature=temperature, max_tokens=max_tokens)
        if resp.content:
            yield resp.content

    @staticmethod
    def _elapsed_ms(start: float) -> float:
        return (time.time() - start) * 1000
