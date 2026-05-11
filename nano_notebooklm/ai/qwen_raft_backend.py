"""Round 4 #R4-5 — remote Qwen-RAFT backend.

Talks to the AutoDL ``scripts/app.py`` Gradio service (Qwen2.5-7B-Instruct
fine-tuned via RAFT, served on ``:6006``). The Gradio "predict" endpoint
follows the standard request/response envelope::

    POST {QWEN_RAFT_URL}/api/predict
    { "data": [prompt_string], "fn_index": 0 }
        →
    { "data": [answer_string], "duration": float, "average_duration": float }

``fn_index`` defaults to 0 (the first Gradio function), which matches the
single-input/single-output chatbot interface ``scripts/app.py`` exposes.
Operators with a different Gradio function index can override via
``QWEN_RAFT_FN_INDEX``.

Status:
    - ``complete()`` — full implementation
    - ``complete_structured()`` — best-effort JSON parse of ``complete()``;
      Qwen-7B is not as reliable as GPT-5.4 on JSON shape, so callers
      that depend on structured output should NOT route to qwen_raft
      until R4-5 fix-all v2 adds a retry+repair loop.
    - ``complete_stream()`` — falls back to single-chunk yield via the
      ``LLMBackend.complete_stream`` default. Gradio 3.x ``/api/predict``
      isn't streaming; switching to the ``/queue/join`` WebSocket path is
      a follow-up.

Health:
    ``health_check()`` does a cheap GET on ``{URL}/`` (Gradio root serves
    the chatbot HTML; 200 OK means the service is up). Used by
    ``/api/status`` to gate the topbar chip.

Tests must remain offline: HTTP calls go through ``httpx.AsyncClient``
which can be ``monkeypatch.setattr``'d to a stub in ``tests/``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import AsyncIterator

import httpx

from nano_notebooklm import config
from nano_notebooklm.ai.base import LLMBackend
from nano_notebooklm.types import LLMResponse

logger = logging.getLogger(__name__)


# ── Errors ─────────────────────────────────────────────────────────


class QwenBackendError(RuntimeError):
    """Surface a stable error code; the message stays for the server log
    but the API surface should NEVER echo upstream Gradio errors (which
    may include filesystem paths or model names — privacy + supply-chain
    discipline mirroring fix-all v4 #A3)."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code


# ── Backend ────────────────────────────────────────────────────────


class QwenRaftBackend(LLMBackend):
    """Remote Qwen-RAFT backend served by AutoDL Gradio ``:6006/api/predict``."""

    name = "qwen_raft"

    def __init__(
        self,
        url: str | None = None,
        token: str | None = None,
        model_name: str | None = None,
        timeout: float | None = None,
        fn_index: int | None = None,
    ):
        self.url = (url if url is not None else config.QWEN_RAFT_URL).rstrip("/")
        self.token = token if token is not None else config.QWEN_RAFT_TOKEN
        self.model_name = model_name or config.QWEN_RAFT_MODEL_NAME
        self.timeout = timeout if timeout is not None else config.QWEN_RAFT_HTTP_TIMEOUT
        self.fn_index = (
            fn_index if fn_index is not None
            else int(os.getenv("QWEN_RAFT_FN_INDEX", "0"))
        )

    # ── Capability gate ────────────────────────────────────────────

    @property
    def configured(self) -> bool:
        """The backend is *configurable* if ``QWEN_RAFT_URL`` is set.
        Reachability is a separate concern — see ``health_check``."""
        return bool(self.url)

    async def health_check(self) -> dict:
        """Cheap HEAD/GET probe on the Gradio root. Used by /api/status
        to render the topbar chip enabled/disabled. Never raises — on
        failure returns ``{ok: False, reason: <stable-code>}``."""
        if not self.configured:
            return {"ok": False, "reason": "not_configured"}
        try:
            async with httpx.AsyncClient(timeout=min(self.timeout, 5.0)) as client:
                resp = await client.get(self.url + "/", headers=self._headers())
            return {"ok": 200 <= resp.status_code < 400, "status": resp.status_code}
        except httpx.TimeoutException:
            return {"ok": False, "reason": "timeout"}
        except Exception:
            # No vendor message leak.
            return {"ok": False, "reason": "unreachable"}

    # ── Required LLMBackend interface ──────────────────────────────

    async def complete(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        """Send ``prompt`` (+ optional system prefix) to Gradio /api/predict.

        Temperature / max_tokens are accepted for interface compatibility
        with OpenAI/Claude backends but the Gradio app.py is a single-
        argument chatbot — it doesn't accept per-call sampling params.
        Future: extend ``scripts/app.py`` to accept a JSON config dict so
        these knobs round-trip.
        """
        if not self.configured:
            raise QwenBackendError("not_configured")

        # System prompt is glued to the front. Qwen2.5-7B-Instruct
        # respects this prefix the same way the codex/claude backends do.
        combined = f"{system.strip()}\n\n{prompt}".strip() if system else prompt

        payload = {"data": [combined], "fn_index": self.fn_index}
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    self.url + "/api/predict",
                    json=payload,
                    headers=self._headers(),
                )
        except httpx.TimeoutException as exc:
            raise QwenBackendError("timeout", str(exc)) from exc
        except httpx.HTTPError as exc:
            raise QwenBackendError("transport_failed", str(exc)) from exc

        if resp.status_code >= 500:
            raise QwenBackendError("upstream_5xx", f"status={resp.status_code}")
        if resp.status_code >= 400:
            raise QwenBackendError("upstream_4xx", f"status={resp.status_code}")

        try:
            body = resp.json()
        except json.JSONDecodeError as exc:
            raise QwenBackendError("malformed_response", str(exc)) from exc

        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list) or not data:
            raise QwenBackendError("empty_response")

        # Gradio chatbots commonly return either a plain string OR a list
        # of message dicts ([[user, assistant], ...]). Accept both.
        first = data[0]
        if isinstance(first, list) and first:
            last_turn = first[-1]
            if isinstance(last_turn, (list, tuple)) and len(last_turn) >= 2:
                content = str(last_turn[1] or "")
            elif isinstance(last_turn, dict):
                content = str(last_turn.get("content") or last_turn.get("text") or "")
            else:
                content = str(last_turn)
        elif isinstance(first, dict):
            content = str(first.get("content") or first.get("text") or "")
        else:
            content = str(first or "")

        latency_ms = (time.monotonic() - start) * 1000
        # Token counts aren't surfaced by Gradio; report 0 so the
        # cost-tracking sums don't misattribute to qwen_raft.
        return LLMResponse(
            content=content,
            model=self.model_name,
            input_tokens=0,
            output_tokens=0,
            latency_ms=latency_ms,
        )

    async def complete_structured(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> dict:
        """Best-effort: call ``complete()`` and try to parse the result as
        JSON. Qwen-7B isn't as reliable as GPT-5.4 on structured output;
        callers needing strict JSON (KG extraction, quiz generation)
        should keep routing to OpenAI."""
        resp = await self.complete(
            prompt, system=system, temperature=temperature, max_tokens=max_tokens,
        )
        text = (resp.content or "").strip()
        # Strip code fences if Qwen wrapped the JSON.
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[: -3]
            text = text.strip()
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else {"result": parsed}
        except json.JSONDecodeError:
            logger.warning("qwen_raft.complete_structured: non-JSON output (truncated to 80c)")
            return {"error": "non_json_output", "raw": text[:2000]}

    # ``complete_stream`` inherits the LLMBackend default — single-chunk
    # yield of ``complete()`` output. Gradio 3.x predict isn't streaming.

    # ── Internals ──────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h
