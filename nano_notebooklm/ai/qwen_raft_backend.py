"""Round 4 #R4-5 — remote Qwen-RAFT backend (OpenAI-compatible).

Talks to the AutoDL ``scripts/serve_openai.py`` FastAPI service (Qwen2.5-7B-
Instruct fine-tuned via RAFT, served on ``:8001``). The service exposes a
strict subset of the OpenAI API::

    GET  {URL}/health                  → {ok, model, device, loaded}
    GET  {URL}/v1/models               → {object: "list", data: [...]}
    POST {URL}/v1/chat/completions     → OpenAI chat completion envelope
    POST {URL}/v1/completions          → legacy text completion

History note (R4-5 fix-all v3, 2026-05-12): an earlier revision of this
backend spoke the Gradio ``/api/predict`` protocol against ``scripts/app.py``
on :6006 with ``{"data": [...], "fn_index": 0}``. That contract was never
satisfiable because ``app.py``'s ``fn_index=0`` is a 4-input streaming
handler (question, model_choice, source, top_k), not a single-string
chatbot function. The serve_openai.py route was always the correct target;
this rewrite finally aligns the client. fix-all v3 follow-up review-swarm
(2026-05-12) further hardened: gradio.live legacy-URL warning, empty-
content fallback parity, split-phase httpx timeout, narrower stream
error attribution, asyncio.Lock on cached-client cold path.

``QWEN_RAFT_URL`` may be either ``http://host:8001`` (the recommended root
form) or ``http://host:8001/v1`` (OpenAI convention). The backend strips a
trailing ``/v1`` so health probe + chat completions both resolve.

Streaming: ``complete_stream`` parses SSE ``data: {chunk}\\n\\n`` lines and
yields ``delta.content`` until ``data: [DONE]``. Backed by httpx
``AsyncClient.stream`` so cancellation propagates cleanly. Because it is
an async generator, errors raised inside surface on the first
``__anext__()`` call, NOT at the bare ``complete_stream(...)`` call —
callers must enter the ``async for`` loop to observe them.

Tests must remain offline: HTTP calls go through ``httpx.AsyncClient`` which
can be ``monkeypatch.setattr``'d to a stub in ``tests/``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.parse
from typing import AsyncIterator

import httpx

from nano_notebooklm import config
from nano_notebooklm.ai.base import LLMBackend
from nano_notebooklm.types import LLMResponse

logger = logging.getLogger(__name__)


# ── Errors ─────────────────────────────────────────────────────────


class QwenBackendError(RuntimeError):
    """Surface a stable error code; the message stays for the server log
    but the API surface should NEVER echo upstream errors (which may
    include filesystem paths or model names — privacy + supply-chain
    discipline mirroring fix-all v4 #A3)."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code


# ── Backend ────────────────────────────────────────────────────────


class QwenRaftBackend(LLMBackend):
    """Remote Qwen-RAFT backend served by AutoDL ``serve_openai.py``."""

    name = "qwen_raft"

    def __init__(
        self,
        url: str | None = None,
        token: str | None = None,
        model_name: str | None = None,
        timeout: float | None = None,
    ):
        raw_url = (url if url is not None else config.QWEN_RAFT_URL).rstrip("/")
        # Accept both ``http://host:8001`` and ``http://host:8001/v1`` —
        # OpenAI clients conventionally include the ``/v1`` segment, but
        # we prepend ``/v1`` ourselves so health probe can hit the root.
        if raw_url.endswith("/v1"):
            raw_url = raw_url[:-3]
        self.url = raw_url
        self.token = token if token is not None else config.QWEN_RAFT_TOKEN
        self.model_name = model_name or config.QWEN_RAFT_MODEL_NAME
        self.timeout = timeout if timeout is not None else config.QWEN_RAFT_HTTP_TIMEOUT
        # fix-all v3 #L3 (review-swarm 2026-05-12): asyncio.Lock guards the
        # cached-client cold path. Created at __init__ — modern asyncio.Lock
        # binds to the loop only on first acquire, so it's safe to construct
        # eagerly even when the backend is used across loops in tests.
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()
        # fix-all v3 #H1 (review-swarm 2026-05-12): legacy `.gradio.live`
        # URLs pass `_validate_qwen_url` (scheme + metadata-host check) but
        # the chat path is now /v1/chat/completions on serve_openai.py's
        # :8001 — operators upgrading in-place would otherwise see every
        # chat silently fall back to codex with no log hint.
        self._warn_if_legacy_gradio_host()

    def _warn_if_legacy_gradio_host(self) -> None:
        if not self.url:
            return
        try:
            host = (urllib.parse.urlparse(self.url).hostname or "").lower()
        except (ValueError, TypeError):
            return
        if host.endswith(".gradio.live") or host == "gradio.live":
            logger.warning(
                "QWEN_RAFT_URL host %r looks like the legacy Gradio service; "
                "this backend now talks to serve_openai.py on a different "
                "port (defaults to :8001). Confirm you migrated, otherwise "
                "every chat will silently fall back to codex.",
                host,
            )

    async def _get_client(self) -> httpx.AsyncClient:
        """Lazy-create + cache the shared httpx.AsyncClient under a lock.

        Split-phase timeout (fix-all v3 #M2 / review-swarm 2026-05-12):
            connect=10s (fail-fast on WAN refusals),
            read=self.timeout (default 60s — accommodates first-token
                latency on a cold Qwen GPU),
            write=10s,
            pool=5s.

        The cheaper health probe overrides the read budget via
        ``client.get(..., timeout=...)``.
        """
        if self._client is not None:
            return self._client
        async with self._client_lock:
            # Double-check under the lock — a concurrent first call may
            # have already constructed the client while we awaited.
            if self._client is None:
                self._client = httpx.AsyncClient(
                    timeout=httpx.Timeout(
                        self.timeout,
                        connect=10.0,
                        write=10.0,
                        pool=5.0,
                    ),
                )
        return self._client

    async def aclose(self) -> None:
        """Close the cached httpx.AsyncClient. Not wired into any FastAPI
        lifespan today (the leak across process lifetime is acceptable);
        exposed as an opt-in hook for callers that want clean shutdown
        (e.g. test teardown)."""
        if self._client is not None:
            try:
                await self._client.aclose()
            finally:
                self._client = None

    # ── Capability gate ────────────────────────────────────────────

    @property
    def configured(self) -> bool:
        """The backend is *configurable* if ``QWEN_RAFT_URL`` is set.
        Reachability is a separate concern — see ``health_check``."""
        return bool(self.url)

    async def health_check(self) -> dict:
        """Cheap GET on ``/health`` (serve_openai.py exposes this).
        Used by /api/status to render the topbar chip enabled/disabled.
        Never raises — on failure returns ``{ok: False, reason: <code>}``.

        Distinguishes three states:
            - service down / unreachable           → reason="unreachable"
            - service up but model not yet loaded  → reason="model_not_loaded"
            - service up + model loaded            → ok=True

        Privacy (fix-all v3 #M5 / review-swarm 2026-05-12): the success
        envelope does NOT echo upstream ``body.model``. A misbehaving or
        compromised AutoDL host could otherwise smuggle a filesystem path
        or fingerprint string to ``/api/status`` consumers; we only ever
        surface the operator-configured ``self.model_name``.
        """
        if not self.configured:
            return {"ok": False, "reason": "not_configured"}
        try:
            client = await self._get_client()
            resp = await client.get(
                self.url + "/health",
                headers=self._headers(),
                timeout=min(self.timeout, 5.0),
            )
        except httpx.TimeoutException:
            return {"ok": False, "reason": "timeout"}
        except Exception:
            # No vendor message leak.
            return {"ok": False, "reason": "unreachable"}

        if not (200 <= resp.status_code < 400):
            return {"ok": False, "reason": "unreachable", "status": resp.status_code}

        # Parse the loaded flag — serve_openai.py reports loaded=False
        # during the ~20-30s post-startup window before the model finishes
        # initializing. Surfacing this distinctly lets the frontend show
        # a "warming up" affordance instead of "unreachable".
        try:
            body = resp.json()
        except (json.JSONDecodeError, ValueError):
            # Non-JSON body — service is something else (or wrong URL).
            return {"ok": False, "reason": "unreachable", "status": resp.status_code}

        # fix-all v3 #L4 (review-swarm 2026-05-12): default to False
        # (fail-closed) on missing ``loaded`` so a downstream schema gap
        # surfaces as model_not_loaded instead of falsely-green.
        loaded = bool(body.get("loaded", False)) if isinstance(body, dict) else False
        if not loaded:
            return {
                "ok": False,
                "reason": "model_not_loaded",
                "status": resp.status_code,
            }
        return {
            "ok": True,
            "status": resp.status_code,
            "model": self.model_name,
        }

    # ── Required LLMBackend interface ──────────────────────────────

    async def complete(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        """POST OpenAI chat-completions envelope to /v1/chat/completions.

        ``temperature`` / ``max_tokens`` round-trip to the upstream sampler
        (unlike the previous Gradio-based backend which couldn't pass them).
        """
        if not self.configured:
            raise QwenBackendError("not_configured")

        payload = {
            "model": self.model_name,
            "messages": self._build_messages(prompt, system),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }

        start = time.monotonic()
        try:
            client = await self._get_client()
            resp = await client.post(
                self.url + "/v1/chat/completions",
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

        content, in_tok, out_tok, model = _parse_chat_completion(body)
        latency_ms = (time.monotonic() - start) * 1000
        return LLMResponse(
            content=content,
            model=model or self.model_name,
            input_tokens=in_tok,
            output_tokens=out_tok,
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

    async def complete_stream(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> AsyncIterator[str]:
        """Real SSE streaming via ``/v1/chat/completions`` ``stream=True``.

        serve_openai.py emits standard OpenAI chunks::

            data: {"choices":[{"delta":{"content":"hel"},...}]}
            data: {"choices":[{"delta":{"content":"lo"},...}]}
            data: [DONE]

        Error attribution (fix-all v3 #M3+#L1 / review-swarm 2026-05-12):
            ``ConnectError`` / ``ConnectTimeout`` at stream-open      → ``transport_failed``
            ``TimeoutException`` at any phase                          → ``timeout``
            non-2xx HTTP status before first chunk                     → ``upstream_4xx`` / ``upstream_5xx``
            any other ``httpx.HTTPError`` (incl. mid-stream RemoteProtocolError) → ``stream_failed``

        Each error branch logs the exception class name (no PII per
        fix-all v2 #V5) so operators can bisect on AutoDL flakes.

        Async-generator semantics: because this function contains
        ``yield``, errors raised here surface on first ``__anext__()``,
        not at function-call time. Callers must enter the ``async for``
        loop to observe them.
        """
        if not self.configured:
            raise QwenBackendError("not_configured")

        payload = {
            "model": self.model_name,
            "messages": self._build_messages(prompt, system),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }

        client = await self._get_client()
        # fix-all v3 #L5 (review-swarm 2026-05-12): SSE Accept header.
        # serve_openai.py is permissive but strict reverse-proxies may
        # gate on Accept; this is correct per the SSE spec.
        headers = self._headers()
        headers["Accept"] = "text/event-stream"
        stream_ctx = client.stream(
            "POST",
            self.url + "/v1/chat/completions",
            json=payload,
            headers=headers,
        )

        try:
            async with stream_ctx as response:
                if response.status_code >= 500:
                    raise QwenBackendError(
                        "upstream_5xx", f"status={response.status_code}",
                    )
                if response.status_code >= 400:
                    raise QwenBackendError(
                        "upstream_4xx", f"status={response.status_code}",
                    )
                async for line in response.aiter_lines():
                    delta = _parse_sse_line(line)
                    if delta is None:
                        continue
                    if delta is _SSE_DONE:
                        return
                    if delta:
                        yield delta
        except QwenBackendError:
            raise
        except httpx.ConnectError as exc:
            logger.warning(
                "qwen_raft.complete_stream transport_failed: %s",
                type(exc).__name__,
            )
            raise QwenBackendError("transport_failed", str(exc)) from exc
        except httpx.TimeoutException as exc:
            logger.warning(
                "qwen_raft.complete_stream timeout: %s", type(exc).__name__,
            )
            raise QwenBackendError("timeout", str(exc)) from exc
        except httpx.HTTPError as exc:
            logger.warning(
                "qwen_raft.complete_stream stream_failed: %s",
                type(exc).__name__,
            )
            raise QwenBackendError("stream_failed", str(exc)) from exc

    # ── Internals ──────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    @staticmethod
    def _build_messages(prompt: str, system: str) -> list[dict[str, str]]:
        msgs: list[dict[str, str]] = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": prompt})
        return msgs


# ── Parsing helpers ────────────────────────────────────────────────


# Sentinel to distinguish "end of stream" from "no content this chunk".
_SSE_DONE = object()


def _parse_chat_completion(body: object) -> tuple[str, int, int, str]:
    """Pull ``content / input_tokens / output_tokens / model`` from an
    OpenAI chat-completion envelope.

    fix-all v3 #M1 (review-swarm 2026-05-12): an empty or whitespace-only
    ``message.content`` is treated as ``empty_response`` so the
    ``qwen→codex`` fallback chain in ``qa_skill._complete_with_backend_fallback``
    actually fires. Previously the caller saw ``LLMResponse(content="")``
    silently — a Qwen-7B safety-filter rejection or a ``max_tokens=1``
    truncation would surface as a blank assistant turn instead of the
    codex fallback.
    """
    if not isinstance(body, dict):
        raise QwenBackendError("empty_response")
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise QwenBackendError("empty_response")
    first = choices[0]
    if not isinstance(first, dict):
        raise QwenBackendError("empty_response")
    msg = first.get("message")
    if isinstance(msg, dict):
        content = str(msg.get("content") or "")
    else:
        # Legacy `/v1/completions` shape — choices[0].text
        content = str(first.get("text") or "")
    if not content.strip():
        raise QwenBackendError("empty_response")
    usage = body.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    in_tok = int(usage.get("prompt_tokens") or 0)
    out_tok = int(usage.get("completion_tokens") or 0)
    model = str(body.get("model") or "")
    return content, in_tok, out_tok, model


def _parse_sse_line(line: str):
    """Parse one SSE line from /v1/chat/completions stream=True.

    Returns:
        - ``None``    if the line is empty / a comment / a non-data field
        - ``_SSE_DONE`` for the terminal ``data: [DONE]``
        - the delta string (possibly empty) otherwise
    """
    if not line:
        return None
    if not line.startswith("data:"):
        # SSE allows "event:", "id:", "retry:" etc. — ignore.
        return None
    payload = line[5:].lstrip()
    if not payload:
        return None
    if payload == "[DONE]":
        return _SSE_DONE
    try:
        chunk = json.loads(payload)
    except json.JSONDecodeError:
        # Malformed chunk — skip rather than abort the whole stream.
        return None
    if not isinstance(chunk, dict):
        return None
    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    delta = first.get("delta")
    if isinstance(delta, dict):
        content = delta.get("content")
        return str(content) if content else ""
    # Final chunk may have no delta but a finish_reason — treat as empty.
    return ""
