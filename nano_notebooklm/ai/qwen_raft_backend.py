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
import os
import re
import time
import urllib.parse
from typing import AsyncIterator

import httpx

from nano_notebooklm import config
from nano_notebooklm.ai.base import LLMBackend
from nano_notebooklm.types import LLMResponse

# 2026-05-13: Qwen-7B-RAFT was fine-tuned to emit the three-stage RAFT
# output format ("Analyze key points: ... Quote evidence: ##begin_quote##
# ...##end_quote## Final conclusion: ..."). That structure leaks model
# chain-of-thought + RAFT-specific markers into the user-facing answer
# and doesn't match the codex path's plain-prose convention, so we strip
# the preamble here and reformat the quote(s) as markdown blockquotes
# (which the frontend already renders). The final-conclusion body
# becomes the main answer.
#
# Step 2 (TODO): fuzzy-match each ##begin_quote## body against the graph
# rag chunks the qa_skill passed in as context and append a
# `[Source: <file>, <loc>]` tag so the existing citation-chip pipeline
# can link quotes back to the PDF. This module only owns presentation;
# the cross-ref enrichment lives in qa_skill where the source list is
# in scope.
# Optional line-leading punctuation the RAFT model sometimes prepends:
# markdown list marker (`- `, `* `, `+ `, `• `), CJK bullet (`·`), an
# enumeration prefix (`1.`, `1)`, `(1)`), or just plain whitespace.
# Used at the start of both marker regexes so a "- Final conclusion:"
# line is recognised the same as a bare "Final conclusion:".
_LINE_LEAD = r"(?:[-*+•·]\s*|\(?\d+[.\)]\s*)?\s*"

_RAFT_FINAL_RE = re.compile(
    # The RAFT model paraphrases its "final answer" marker across runs:
    # "Final conclusion:" / "Conclusion:" / "Final answer:" / "Answer:" /
    # "结论:". Anchored to line start (or string start) with an optional
    # list-marker prefix so neither "- Final conclusion:" nor "  Final
    # conclusion:" slip through.
    r"(?:^|\n)" + _LINE_LEAD +
    r"(?:Final\s+conclusion|Conclusion|Final\s+answer|Answer|"
    r"(?:给出)?最终结论|给出结论|结论)"
    r"\s*[:：]\s*",
    flags=re.IGNORECASE,
)
_RAFT_QUOTE_RE = re.compile(
    r"##\s*begin_quote\s*##\s*(.*?)\s*##\s*end_quote\s*##",
    flags=re.DOTALL | re.IGNORECASE,
)
_RAFT_ANALYZE_RE = re.compile(
    # Paraphrased section markers — `Analyze key points` / `Key points to
    # analyze` / `Quote evidence` / `Evidence from (the) document` /
    # `Step-by-step` / `Reasoning` / `关键点分析`. Same list-prefix
    # tolerance as the final-marker regex.
    #
    # 2026-05-13 (review-swarm fix-now): removed standalone "Evidence" /
    # "分析" / "证据" alternatives — these single tokens appear as
    # legitimate subheadings in academic Chinese / English prose and
    # were triggering false-positive strips on non-RAFT responses.
    # Multi-word forms ("Evidence from the document" / "关键点分析")
    # are still kept because they're RAFT-specific.
    r"(?:^|\n)" + _LINE_LEAD + r"(?:"
    r"Analyze\s+key\s+points|"
    r"Key\s+points?\s+(?:to\s+analyze|analysis)|"
    r"Quote\s+evidence|"
    r"Evidence\s+from\s+(?:the\s+)?(?:text|document(?:s)?|passage)|"
    r"Step-?by-?step\s+reasoning|"
    r"关键点分析|"
    r"关键点\s+分析|"
    # 2026-05-13 hotfix: RAFT 实际输出的中文 trio 之前漏匹配。
    r"先?分析问题要点|"
    r"引用原文关键内容|"
    r"引用原文"
    r")\s*[:：]\s*",
    flags=re.IGNORECASE,
)


def _truncate_degenerate_loop(text: str) -> tuple[str, bool]:
    """Detect token-level repetition loops and truncate at the first repeat.

    2026-05-13: Qwen2.5-7B-RAFT under fragmented OCR math context (e.g.
    ch4(2).pdf Self-Attention slides with bare subscripts α₃,₁ q₁ ρ_j …)
    can degenerate into a "h i j t t t" token loop hundreds of times. The
    frequency_penalty / presence_penalty fields in the request payload
    reduce the rate but don't eliminate it entirely. As a safety net,
    scan the response for any 3-character (post-normalization) substring
    that recurs ≥ 5 times in a row, and truncate at the first repeat.

    Returns ``(cleaned_text, was_truncated)``. Caller can use the flag to
    trigger codex fallback when the response is salvageable but not
    confidently the model's best output.
    """
    if not text or len(text) < 50:
        return text, False
    # Collapse whitespace + lowercase for matching but PRESERVE original
    # text positions for slicing.
    # Strategy: walk the text in 3-char windows; track consecutive equal
    # windows. If we hit a run of ≥ 5 same windows, cut at the start of
    # the 2nd repeat.
    norm = " ".join(text.split())
    if len(norm) < 50:
        return text, False
    window = 3
    threshold = 5
    i = 0
    while i + window * threshold < len(norm):
        candidate = norm[i:i + window]
        # Skip empty / pure-space windows.
        if not candidate.strip():
            i += 1
            continue
        # Count consecutive occurrences starting at i.
        runs = 1
        j = i + window
        while j + window <= len(norm) and norm[j:j + window] == candidate:
            runs += 1
            j += window
        if runs >= threshold:
            # Truncate at i + window (after the FIRST occurrence so the
            # original output keeps the "real" tail before the loop).
            return text[: max(0, _find_original_offset(text, i + window))], True
        i += 1
    return text, False


def _find_original_offset(original: str, norm_offset: int) -> int:
    """Map a normalised-whitespace offset back to the original text offset.
    Walks `original` advancing one normalised char per non-collapsed char,
    treating any whitespace run as a single space. Returns the original
    index that corresponds to `norm_offset`.
    """
    norm_seen = 0
    i = 0
    in_space = False
    while i < len(original) and norm_seen < norm_offset:
        ch = original[i]
        if ch.isspace():
            if not in_space:
                norm_seen += 1
                in_space = True
        else:
            norm_seen += 1
            in_space = False
        i += 1
    return i


def _strip_raft_preamble(text: str) -> str:
    """Convert RAFT three-stage output to a clean plain-prose answer.

    Pipeline:
      1. Detect whether the response is in RAFT three-stage format by
         requiring at least one canonical structural marker
         (``##begin_quote##`` OR an Analyze/Evidence section header).
         If absent, return text unchanged — a normal qwen response that
         happens to contain a line like "Answer: X" or "结论：X" must
         NOT have content before that line silently sliced off.
      2. Extract quote bodies (``##begin_quote##...##end_quote##``) — we
         keep these as markdown blockquotes appended to the answer.
      3. Locate ``Final conclusion:`` (or paraphrase) and take everything
         after it as the primary body. If absent, fall back to the input
         minus the Analyze / Quote section markers (only safe because
         we've already confirmed RAFT format at step 1).
      4. Re-attach the extracted quote bodies as ``> ...`` blockquotes
         after the primary body.

    Empty / whitespace-only input passes through unchanged so this is
    safe to call unconditionally on every qwen response.

    Reviewed 2026-05-13 (review-swarm fix-now CRITICAL #2): plain-prose
    fallback removed in favour of an explicit RAFT-format precondition,
    after reviewer flagged false-positive strips of natural Chinese
    prose containing "分析：" / "证据：" / "结论：" subheadings.
    """
    if not text or not text.strip():
        return text
    has_quote_markers = bool(_RAFT_QUOTE_RE.search(text))
    has_analyze_markers = bool(_RAFT_ANALYZE_RE.search(text))
    if not (has_quote_markers or has_analyze_markers):
        # Not RAFT format — return as-is. The `Final conclusion:` /
        # `Conclusion:` slice would otherwise truncate plain prose.
        return text
    quotes = [q.strip() for q in _RAFT_QUOTE_RE.findall(text) if q.strip()]
    # Strip the inline quote spans from text so they don't leak into
    # the conclusion body when Final conclusion isn't present.
    body_src = _RAFT_QUOTE_RE.sub("", text)
    m = _RAFT_FINAL_RE.search(body_src)
    if m:
        body = body_src[m.end():].strip()
    else:
        # Fallback path is now safe because we've gated on the
        # has_analyze_markers signal above — we know the input had at
        # least one of the section headers we're about to strip.
        body = _RAFT_ANALYZE_RE.sub("", body_src).strip()
    if quotes:
        # Render quotes as markdown blockquotes after the main body.
        # Each line of a multi-line quote needs its own `> ` prefix.
        quote_md = "\n\n".join(
            "\n".join("> " + line for line in q.splitlines())
            for q in quotes
        )
        if body:
            return f"{body}\n\n{quote_md}"
        return quote_md
    return body

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

        # 2026-05-13: Qwen2.5-7B-RAFT empirically degenerates into token
        # loops when the source context contains fragmented OCR'd math
        # symbols (the ch4(2).pdf Self-Attention slides emit hundreds of
        # bare subscripts: α₃,₁, q₁, k₁, ρ_j, etc). Pre-fix output was
        # "Attention(Q,K,V) = QKT" followed by 200+ repeats of "h i j t
        # t t". Adding frequency_penalty kills the immediate-repeat
        # attractor in the sampler; presence_penalty broadens vocabulary
        # so it can't get stuck on the same token cluster. Values are
        # mild — too high makes the model refuse to use math symbols at
        # all. Threaded via env so we can tune in prod without redeploy.
        freq_pen = float(os.getenv("QWEN_FREQUENCY_PENALTY", "0.5"))
        pres_pen = float(os.getenv("QWEN_PRESENCE_PENALTY", "0.3"))
        payload = {
            "model": self.model_name,
            "messages": self._build_messages(prompt, system),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
            "frequency_penalty": freq_pen,
            "presence_penalty": pres_pen,
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
        # 2026-05-13: RAFT-format preamble strip. See _strip_raft_preamble
        # docstring above. Idempotent on plain-prose output (the codex-
        # path style), so safe to apply unconditionally.
        content = _strip_raft_preamble(content)
        # 2026-05-13: degenerate-loop safety net (see
        # _truncate_degenerate_loop). When triggered we keep the prefix
        # before the loop — usually a salvageable partial answer —
        # rather than dropping the entire response.
        content, was_degenerate = _truncate_degenerate_loop(content)
        if was_degenerate:
            logger.warning("qwen response truncated at degenerate loop")
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
