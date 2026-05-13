"""Round 4 #R4-5 — Qwen-RAFT remote backend contract tests.

All tests are offline: ``httpx.AsyncClient`` is monkeypatched with a stub
that records call args and returns canned OpenAI chat-completion envelopes.

History (R4-5 fix-all v3, 2026-05-12): rewritten to cover the OpenAI-
compatible ``/v1/chat/completions`` protocol after the backend switched
off the Gradio ``/api/predict`` route. The R4-5 part 2 integration tests
at the bottom of the file (after the SSE block) are protocol-agnostic
because they stub ``router.backends`` rather than the HTTP layer; they
survived the protocol change unchanged.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from nano_notebooklm.ai.qwen_raft_backend import (
    QwenBackendError,
    QwenRaftBackend,
)


# ── Stub httpx ─────────────────────────────────────────────────────


class _StubResponse:
    def __init__(self, status_code: int, body: dict | str | None = None):
        self.status_code = status_code
        self._body = body

    def json(self):
        if isinstance(self._body, str):
            return json.loads(self._body)
        return self._body or {}


class _StubStreamingResponse:
    """Mimics httpx's streaming Response inside ``client.stream(...)``.

    ``lines`` is the sequence of SSE lines yielded by ``aiter_lines()``;
    callers pass full ``data: {...}`` strings (no trailing newlines).
    """

    def __init__(self, status_code: int, lines: list[str] | None = None,
                 raise_on_iter: Exception | None = None):
        self.status_code = status_code
        self._lines = lines or []
        self._raise_on_iter = raise_on_iter

    async def aiter_lines(self):
        if self._raise_on_iter is not None:
            raise self._raise_on_iter
        for line in self._lines:
            yield line


class _StreamContextManager:
    """The object returned by ``client.stream(...)`` — an async context
    manager whose ``__aenter__`` yields the streaming response. ``stream``
    itself may also surface a transport error here (mirroring httpx's
    behavior where connection failures raise on enter, not on construct)."""

    def __init__(self, response: _StubStreamingResponse | None = None,
                 raise_on_enter: Exception | None = None):
        self._response = response
        self._raise = raise_on_enter

    async def __aenter__(self):
        if self._raise is not None:
            raise self._raise
        return self._response

    async def __aexit__(self, *exc):
        return False


class _StubAsyncClient:
    """Records POST/GET/stream calls and returns canned responses.

    ``responses`` is consumed in FIFO order for post/get. If you only pass
    one response it's reused for every call.

    ``stream_response`` is a single (response, raise) pair used by
    ``client.stream(...)``.
    """

    def __init__(
        self,
        *,
        responses: list[_StubResponse] | _StubResponse | None = None,
        raise_on_call: Exception | None = None,
        stream_response: _StubStreamingResponse | None = None,
        stream_raises: Exception | None = None,
    ):
        if isinstance(responses, _StubResponse):
            responses = [responses]
        self._responses = list(responses or [])
        self._raise = raise_on_call
        self._stream_response = stream_response
        self._stream_raises = stream_raises
        self.calls: list[dict[str, Any]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url: str, *, json=None, headers=None, **kw):
        self.calls.append({"method": "POST", "url": url, "json": json, "headers": headers, **kw})
        if self._raise is not None:
            raise self._raise
        return self._next_response()

    async def get(self, url: str, *, headers=None, **kw):
        self.calls.append({"method": "GET", "url": url, "headers": headers, **kw})
        if self._raise is not None:
            raise self._raise
        return self._next_response()

    def stream(self, method: str, url: str, *, json=None, headers=None, **kw):
        self.calls.append({
            "method": f"STREAM_{method}", "url": url, "json": json,
            "headers": headers, **kw,
        })
        return _StreamContextManager(
            response=self._stream_response, raise_on_enter=self._stream_raises,
        )

    async def aclose(self):
        # No-op; cached-client design calls this on backend.aclose().
        return None

    def _next_response(self):
        if not self._responses:
            return _StubResponse(200, _chat_envelope("fallback"))
        if len(self._responses) == 1:
            return self._responses[0]
        return self._responses.pop(0)


def _install_stub(monkeypatch, stub: _StubAsyncClient):
    """Replace ``httpx.AsyncClient(...)`` so it returns our stub regardless
    of constructor args."""
    import httpx
    def _factory(*a, **kw):
        return stub
    monkeypatch.setattr(httpx, "AsyncClient", _factory)


def _chat_envelope(content: str, *, model: str = "Qwen2.5-7B-RAFT",
                   prompt_tokens: int = 0, completion_tokens: int = 0) -> dict:
    """Build a minimal OpenAI chat-completion JSON envelope."""
    return {
        "id": "chatcmpl-stub",
        "object": "chat.completion",
        "created": 1234567890,
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def _sse_chunk(delta: str) -> str:
    """One SSE ``data:`` line carrying a chat-completion chunk."""
    payload = {
        "id": "chatcmpl-stub",
        "object": "chat.completion.chunk",
        "created": 1234567890,
        "model": "Qwen2.5-7B-RAFT",
        "choices": [{
            "index": 0,
            "delta": {"content": delta},
            "finish_reason": None,
        }],
    }
    return f"data: {json.dumps(payload)}"


# ── configured / not_configured gate ───────────────────────────────


def test_qwen_backend_not_configured_when_url_empty():
    b = QwenRaftBackend(url="", token="", model_name="qwen2.5-7b-raft")
    assert b.configured is False


def test_qwen_backend_configured_when_url_set():
    b = QwenRaftBackend(url="http://example.com:8001", token="")
    assert b.configured is True


def test_qwen_backend_strips_trailing_v1_suffix():
    """Operators may follow OpenAI convention and include /v1 in the URL.
    The backend prepends /v1 itself, so we strip it to keep the health
    probe pointing at /health (which is unversioned)."""
    b = QwenRaftBackend(url="http://example.com:8001/v1", token="")
    assert b.url == "http://example.com:8001"


def test_qwen_backend_strips_trailing_slash():
    b = QwenRaftBackend(url="http://example.com:8001/", token="")
    assert b.url == "http://example.com:8001"


@pytest.mark.asyncio
async def test_complete_raises_not_configured_when_url_empty():
    b = QwenRaftBackend(url="", token="")
    with pytest.raises(QwenBackendError) as ei:
        await b.complete("hi")
    assert ei.value.code == "not_configured"


# ── happy path ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_complete_posts_to_v1_chat_completions(monkeypatch):
    stub = _StubAsyncClient(
        responses=_StubResponse(200, _chat_envelope("hello, world",
                                                    prompt_tokens=12,
                                                    completion_tokens=4)),
    )
    _install_stub(monkeypatch, stub)

    b = QwenRaftBackend(url="http://example.com:8001", token="t1")
    resp = await b.complete("hi", system="you are helpful")

    assert resp.content == "hello, world"
    assert resp.model == "Qwen2.5-7B-RAFT"
    assert resp.input_tokens == 12
    assert resp.output_tokens == 4
    assert resp.latency_ms >= 0

    # One POST to /v1/chat/completions.
    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "http://example.com:8001/v1/chat/completions"

    # Body is the OpenAI envelope — model + messages list, no Gradio fields.
    body = call["json"]
    assert isinstance(body, dict)
    assert "fn_index" not in body
    assert "data" not in body
    # Operator's local .env can override QWEN_RAFT_MODEL_NAME (CLAUDE.md
    # documents dotenv override=True), so compare against the live
    # config value rather than the hardcoded default string.
    from nano_notebooklm import config as _cfg
    assert body["model"] == _cfg.QWEN_RAFT_MODEL_NAME
    assert body["stream"] is False

    msgs = body["messages"]
    assert msgs == [
        {"role": "system", "content": "you are helpful"},
        {"role": "user", "content": "hi"},
    ]

    # Authorization header threaded through.
    assert call["headers"].get("Authorization") == "Bearer t1"
    assert call["headers"].get("Content-Type") == "application/json"


@pytest.mark.asyncio
async def test_complete_omits_system_when_empty(monkeypatch):
    """No system prompt → messages list contains only the user turn."""
    stub = _StubAsyncClient(responses=_StubResponse(200, _chat_envelope("ok")))
    _install_stub(monkeypatch, stub)
    b = QwenRaftBackend(url="http://example.com:8001")
    await b.complete("just user")
    msgs = stub.calls[0]["json"]["messages"]
    assert msgs == [{"role": "user", "content": "just user"}]


@pytest.mark.asyncio
async def test_complete_passes_temperature_and_max_tokens(monkeypatch):
    """Unlike the previous Gradio backend, temperature + max_tokens
    actually round-trip to the upstream sampler."""
    stub = _StubAsyncClient(responses=_StubResponse(200, _chat_envelope("ok")))
    _install_stub(monkeypatch, stub)
    b = QwenRaftBackend(url="http://example.com:8001")
    await b.complete("hi", temperature=0.42, max_tokens=999)
    body = stub.calls[0]["json"]
    assert body["temperature"] == 0.42
    assert body["max_tokens"] == 999


@pytest.mark.asyncio
async def test_complete_strips_v1_suffix_so_chat_url_resolves(monkeypatch):
    """URL with trailing /v1 should not produce //v1/v1/chat/completions."""
    stub = _StubAsyncClient(responses=_StubResponse(200, _chat_envelope("ok")))
    _install_stub(monkeypatch, stub)
    b = QwenRaftBackend(url="http://example.com:8001/v1")
    await b.complete("hi")
    assert stub.calls[0]["url"] == "http://example.com:8001/v1/chat/completions"


# ── error paths ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_complete_raises_timeout_code_on_httpx_timeout(monkeypatch):
    import httpx
    stub = _StubAsyncClient(raise_on_call=httpx.TimeoutException("slow"))
    _install_stub(monkeypatch, stub)
    b = QwenRaftBackend(url="http://example.com:8001", timeout=0.1)
    with pytest.raises(QwenBackendError) as ei:
        await b.complete("hi")
    assert ei.value.code == "timeout"


@pytest.mark.asyncio
async def test_complete_raises_transport_failed_on_connection_error(monkeypatch):
    import httpx
    stub = _StubAsyncClient(raise_on_call=httpx.ConnectError("refused"))
    _install_stub(monkeypatch, stub)
    b = QwenRaftBackend(url="http://example.com:8001")
    with pytest.raises(QwenBackendError) as ei:
        await b.complete("hi")
    assert ei.value.code == "transport_failed"


@pytest.mark.asyncio
async def test_complete_raises_upstream_5xx_on_server_error(monkeypatch):
    stub = _StubAsyncClient(responses=_StubResponse(503, {"error": "model loading"}))
    _install_stub(monkeypatch, stub)
    b = QwenRaftBackend(url="http://example.com:8001")
    with pytest.raises(QwenBackendError) as ei:
        await b.complete("hi")
    assert ei.value.code == "upstream_5xx"


@pytest.mark.asyncio
async def test_complete_raises_upstream_4xx_on_client_error(monkeypatch):
    stub = _StubAsyncClient(responses=_StubResponse(422, {"error": "bad request"}))
    _install_stub(monkeypatch, stub)
    b = QwenRaftBackend(url="http://example.com:8001")
    with pytest.raises(QwenBackendError) as ei:
        await b.complete("hi")
    assert ei.value.code == "upstream_4xx"


@pytest.mark.asyncio
async def test_complete_raises_empty_response_when_choices_missing(monkeypatch):
    stub = _StubAsyncClient(responses=_StubResponse(200, {"choices": []}))
    _install_stub(monkeypatch, stub)
    b = QwenRaftBackend(url="http://example.com:8001")
    with pytest.raises(QwenBackendError) as ei:
        await b.complete("hi")
    assert ei.value.code == "empty_response"


@pytest.mark.asyncio
async def test_complete_raises_empty_response_when_body_not_dict(monkeypatch):
    # Body that JSON-decodes to a non-dict (a bare list) → empty_response,
    # not malformed_response. The latter is reserved for actual JSON parse
    # failures upstream of the shape check.
    stub = _StubAsyncClient(responses=_StubResponse(200, "[1, 2, 3]"))
    _install_stub(monkeypatch, stub)
    b = QwenRaftBackend(url="http://example.com:8001")
    with pytest.raises(QwenBackendError) as ei:
        await b.complete("hi")
    assert ei.value.code == "empty_response"


@pytest.mark.asyncio
async def test_complete_raises_malformed_response_on_invalid_json(monkeypatch):
    stub = _StubAsyncClient(responses=_StubResponse(200, "not-valid-json"))
    _install_stub(monkeypatch, stub)
    b = QwenRaftBackend(url="http://example.com:8001")
    with pytest.raises(QwenBackendError) as ei:
        await b.complete("hi")
    assert ei.value.code == "malformed_response"


def test_qwen_backend_error_does_not_leak_url_in_code():
    """fix-all v4 #A3 discipline applied to qwen too: the stable code
    must be exposed but the error str() (which goes to client through
    the error event) must not echo the secret URL."""
    err = QwenBackendError("timeout", "http://secret.host:8001 failed")
    # `code` is the API-visible identifier; the human message lives in
    # str(err) and only goes to server log (caller in api/server.py
    # must NOT echo str(err) into the response body, see fix-all v4 #A3).
    assert err.code == "timeout"


# ── health check ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_returns_not_configured_when_url_empty():
    b = QwenRaftBackend(url="", token="")
    h = await b.health_check()
    assert h == {"ok": False, "reason": "not_configured"}


@pytest.mark.asyncio
async def test_health_check_returns_ok_when_model_loaded(monkeypatch):
    # M5 (review-swarm 2026-05-12): the success envelope must NOT echo
    # upstream body.model — a misbehaving AutoDL host could otherwise
    # smuggle a filesystem path or fingerprint to /api/status. Backend
    # always surfaces the operator-configured model_name instead.
    stub = _StubAsyncClient(
        responses=_StubResponse(200, {
            "ok": True, "model": "/etc/passwd",  # adversarial upstream string
            "device": "cuda", "loaded": True,
        }),
    )
    _install_stub(monkeypatch, stub)
    b = QwenRaftBackend(url="http://example.com:8001")
    h = await b.health_check()
    assert h["ok"] is True
    assert h["status"] == 200
    # Operator-configured value wins; upstream malicious string never surfaced.
    # .env override means we compare against live config, not the default string.
    from nano_notebooklm import config as _cfg
    assert h["model"] == _cfg.QWEN_RAFT_MODEL_NAME
    assert "/etc/passwd" not in str(h)
    # Probed /health, not /.
    assert stub.calls[0]["url"] == "http://example.com:8001/health"


@pytest.mark.asyncio
async def test_health_check_returns_model_not_loaded_during_warmup(monkeypatch):
    """serve_openai.py returns loaded=False during the ~20-30s startup
    window. Frontend should be able to show a distinct 'warming up' chip."""
    stub = _StubAsyncClient(
        responses=_StubResponse(200, {
            "ok": True, "model": "Qwen2.5-7B-RAFT",
            "device": "cuda", "loaded": False,
        }),
    )
    _install_stub(monkeypatch, stub)
    b = QwenRaftBackend(url="http://example.com:8001")
    h = await b.health_check()
    assert h["ok"] is False
    assert h["reason"] == "model_not_loaded"


@pytest.mark.asyncio
async def test_health_check_returns_unreachable_on_exception(monkeypatch):
    import httpx
    stub = _StubAsyncClient(raise_on_call=httpx.ConnectError("nope"))
    _install_stub(monkeypatch, stub)
    b = QwenRaftBackend(url="http://example.com:8001")
    h = await b.health_check()
    assert h["ok"] is False
    assert h["reason"] == "unreachable"


@pytest.mark.asyncio
async def test_health_check_returns_timeout_on_httpx_timeout(monkeypatch):
    import httpx
    stub = _StubAsyncClient(raise_on_call=httpx.TimeoutException("slow"))
    _install_stub(monkeypatch, stub)
    b = QwenRaftBackend(url="http://example.com:8001")
    h = await b.health_check()
    assert h["ok"] is False
    assert h["reason"] == "timeout"


@pytest.mark.asyncio
async def test_health_check_returns_unreachable_on_non_2xx(monkeypatch):
    stub = _StubAsyncClient(responses=_StubResponse(500))
    _install_stub(monkeypatch, stub)
    b = QwenRaftBackend(url="http://example.com:8001")
    h = await b.health_check()
    assert h["ok"] is False
    assert h["reason"] == "unreachable"


# ── complete_structured best-effort ────────────────────────────────


@pytest.mark.asyncio
async def test_complete_structured_returns_dict_on_clean_json(monkeypatch):
    stub = _StubAsyncClient(
        responses=_StubResponse(200, _chat_envelope('{"answer": "yes"}')),
    )
    _install_stub(monkeypatch, stub)
    b = QwenRaftBackend(url="http://example.com:8001")
    out = await b.complete_structured("ask")
    assert out == {"answer": "yes"}


@pytest.mark.asyncio
async def test_complete_structured_strips_code_fence(monkeypatch):
    fenced = "```json\n{\"answer\": \"with fence\"}\n```"
    stub = _StubAsyncClient(responses=_StubResponse(200, _chat_envelope(fenced)))
    _install_stub(monkeypatch, stub)
    b = QwenRaftBackend(url="http://example.com:8001")
    out = await b.complete_structured("ask")
    assert out == {"answer": "with fence"}


@pytest.mark.asyncio
async def test_complete_structured_returns_error_dict_on_non_json(monkeypatch):
    stub = _StubAsyncClient(
        responses=_StubResponse(200, _chat_envelope("plain prose not json")),
    )
    _install_stub(monkeypatch, stub)
    b = QwenRaftBackend(url="http://example.com:8001")
    out = await b.complete_structured("ask")
    assert out["error"] == "non_json_output"
    assert out["raw"].startswith("plain prose")


# ── complete_stream (real SSE) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_complete_stream_yields_per_chunk_deltas(monkeypatch):
    """Real streaming: each ``data: {...}`` line produces one yielded
    delta. The ``[DONE]`` terminator ends the stream."""
    lines = [
        _sse_chunk("Hel"),
        "",                          # SSE blank-line separator → ignored
        _sse_chunk("lo, "),
        _sse_chunk("world"),
        "data: [DONE]",
    ]
    stub = _StubAsyncClient(
        stream_response=_StubStreamingResponse(200, lines=lines),
    )
    _install_stub(monkeypatch, stub)

    b = QwenRaftBackend(url="http://example.com:8001")
    chunks = []
    async for delta in b.complete_stream("hi"):
        chunks.append(delta)
    assert chunks == ["Hel", "lo, ", "world"]

    # One streaming POST to /v1/chat/completions with stream=True.
    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert call["method"] == "STREAM_POST"
    assert call["url"] == "http://example.com:8001/v1/chat/completions"
    assert call["json"]["stream"] is True


@pytest.mark.asyncio
async def test_complete_stream_skips_empty_deltas(monkeypatch):
    """Final chunks often have ``delta: {}`` (no content, just finish_reason).
    The backend must not yield empty strings — that would pollute the
    NDJSON stream with zero-length chunks."""
    empty_final = json.dumps({
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    })
    lines = [
        _sse_chunk("real"),
        f"data: {empty_final}",
        "data: [DONE]",
    ]
    stub = _StubAsyncClient(
        stream_response=_StubStreamingResponse(200, lines=lines),
    )
    _install_stub(monkeypatch, stub)
    b = QwenRaftBackend(url="http://example.com:8001")
    chunks = [c async for c in b.complete_stream("hi")]
    assert chunks == ["real"]


@pytest.mark.asyncio
async def test_complete_stream_ignores_malformed_chunk(monkeypatch):
    """A single malformed ``data:`` line should not abort the whole
    stream — log + skip, keep yielding good chunks."""
    lines = [
        _sse_chunk("good"),
        "data: not-valid-json",
        _sse_chunk("more"),
        "data: [DONE]",
    ]
    stub = _StubAsyncClient(
        stream_response=_StubStreamingResponse(200, lines=lines),
    )
    _install_stub(monkeypatch, stub)
    b = QwenRaftBackend(url="http://example.com:8001")
    chunks = [c async for c in b.complete_stream("hi")]
    assert chunks == ["good", "more"]


@pytest.mark.asyncio
async def test_complete_stream_raises_upstream_5xx_before_first_chunk(monkeypatch):
    stub = _StubAsyncClient(
        stream_response=_StubStreamingResponse(503, lines=[]),
    )
    _install_stub(monkeypatch, stub)
    b = QwenRaftBackend(url="http://example.com:8001")
    with pytest.raises(QwenBackendError) as ei:
        async for _ in b.complete_stream("hi"):
            pass
    assert ei.value.code == "upstream_5xx"


@pytest.mark.asyncio
async def test_complete_stream_raises_not_configured_when_url_empty():
    b = QwenRaftBackend(url="")
    with pytest.raises(QwenBackendError) as ei:
        async for _ in b.complete_stream("hi"):
            pass
    assert ei.value.code == "not_configured"


@pytest.mark.asyncio
async def test_complete_stream_terminates_without_done_marker(monkeypatch):
    """If the upstream forgets to emit ``[DONE]`` (e.g. connection closes
    cleanly mid-stream), we should still terminate cleanly when the
    iterator exhausts — not hang forever."""
    lines = [_sse_chunk("partial"), _sse_chunk(" only")]
    stub = _StubAsyncClient(
        stream_response=_StubStreamingResponse(200, lines=lines),
    )
    _install_stub(monkeypatch, stub)
    b = QwenRaftBackend(url="http://example.com:8001")
    chunks = [c async for c in b.complete_stream("hi")]
    assert chunks == ["partial", " only"]


# ══════════════════════════════════════════════════════════════════════
# R4-5 fix-all v3 review-swarm follow-up (2026-05-12)
# Coverage for H1 / M1 / M3+L1 / M4 / M5 / L2 / L3 / L4 / L5 findings.
# ══════════════════════════════════════════════════════════════════════


# ── H1: legacy gradio.live URL warns at __init__ ────────────────────


def test_qwen_backend_warns_on_legacy_gradio_host(caplog):
    """Operators upgrading in-place with QWEN_RAFT_URL pointing at the
    old Gradio service must see a warning — otherwise every chat
    silently falls back to codex with no log hint."""
    import logging as _logging
    with caplog.at_level(_logging.WARNING, logger="nano_notebooklm.ai.qwen_raft_backend"):
        QwenRaftBackend(url="https://your-autodl.gradio.live")
    msgs = [r.message for r in caplog.records]
    assert any("gradio" in m.lower() for m in msgs), \
        f"expected legacy-gradio warning, got: {msgs}"


def test_qwen_backend_does_not_warn_on_serve_openai_host(caplog):
    """A normal serve_openai.py host must NOT trigger the warning."""
    import logging as _logging
    with caplog.at_level(_logging.WARNING, logger="nano_notebooklm.ai.qwen_raft_backend"):
        QwenRaftBackend(url="http://autodl-host.example:8001")
    msgs = [r.message for r in caplog.records]
    assert not any("gradio" in m.lower() for m in msgs), \
        f"unexpected gradio warning on non-gradio host: {msgs}"


# ── M1: empty / whitespace content triggers empty_response ───────────


@pytest.mark.asyncio
async def test_complete_raises_empty_response_on_empty_content(monkeypatch):
    """A blank `message.content` (Qwen safety filter rejection,
    max_tokens=1 truncation) must trigger empty_response so the
    qwen→codex fallback chain in qa_skill fires."""
    stub = _StubAsyncClient(responses=_StubResponse(200, _chat_envelope("")))
    _install_stub(monkeypatch, stub)
    b = QwenRaftBackend(url="http://example.com:8001")
    with pytest.raises(QwenBackendError) as ei:
        await b.complete("hi")
    assert ei.value.code == "empty_response"


@pytest.mark.asyncio
async def test_complete_raises_empty_response_on_whitespace_content(monkeypatch):
    stub = _StubAsyncClient(
        responses=_StubResponse(200, _chat_envelope("   \n\t  ")),
    )
    _install_stub(monkeypatch, stub)
    b = QwenRaftBackend(url="http://example.com:8001")
    with pytest.raises(QwenBackendError) as ei:
        await b.complete("hi")
    assert ei.value.code == "empty_response"


# ── M3 + L1: stream error attribution + logs ─────────────────────────


@pytest.mark.asyncio
async def test_complete_stream_raises_transport_failed_on_connect_error(
    monkeypatch, caplog,
):
    """ConnectError at stream-open should map to transport_failed (parity
    with complete()), not stream_failed."""
    import logging as _logging
    stub = _StubAsyncClient(stream_raises=httpx.ConnectError("refused"))
    _install_stub(monkeypatch, stub)
    b = QwenRaftBackend(url="http://example.com:8001")
    with caplog.at_level(_logging.WARNING, logger="nano_notebooklm.ai.qwen_raft_backend"):
        with pytest.raises(QwenBackendError) as ei:
            async for _ in b.complete_stream("hi"):
                pass
    assert ei.value.code == "transport_failed"
    # Log line carries the exception class name (not str(exc)) per PII rule.
    msgs = [r.message for r in caplog.records]
    assert any("transport_failed" in m for m in msgs), msgs
    # The bare token "refused" (str(exc)) must NOT have been formatted in.
    assert not any("refused" in m for m in msgs), msgs


@pytest.mark.asyncio
async def test_complete_stream_raises_timeout_on_mid_stream_timeout(
    monkeypatch, caplog,
):
    """A stall during aiter_lines (slow first-token on cold GPU) must
    map to timeout, not stream_failed."""
    import logging as _logging
    stub = _StubAsyncClient(
        stream_response=_StubStreamingResponse(
            200, raise_on_iter=httpx.ReadTimeout("stalled"),
        ),
    )
    _install_stub(monkeypatch, stub)
    b = QwenRaftBackend(url="http://example.com:8001")
    with caplog.at_level(_logging.WARNING, logger="nano_notebooklm.ai.qwen_raft_backend"):
        with pytest.raises(QwenBackendError) as ei:
            async for _ in b.complete_stream("hi"):
                pass
    assert ei.value.code == "timeout"
    msgs = [r.message for r in caplog.records]
    assert any("timeout" in m for m in msgs), msgs


@pytest.mark.asyncio
async def test_complete_stream_raises_stream_failed_on_remote_protocol_error(
    monkeypatch, caplog,
):
    """Mid-stream connection drop (server killed connection ungracefully)
    must map to stream_failed."""
    import logging as _logging
    stub = _StubAsyncClient(
        stream_response=_StubStreamingResponse(
            200, raise_on_iter=httpx.RemoteProtocolError("conn lost mid-stream"),
        ),
    )
    _install_stub(monkeypatch, stub)
    b = QwenRaftBackend(url="http://example.com:8001")
    with caplog.at_level(_logging.WARNING, logger="nano_notebooklm.ai.qwen_raft_backend"):
        with pytest.raises(QwenBackendError) as ei:
            async for _ in b.complete_stream("hi"):
                pass
    assert ei.value.code == "stream_failed"
    msgs = [r.message for r in caplog.records]
    assert any("stream_failed" in m for m in msgs), msgs
    # PII rule: str(exc) "conn lost mid-stream" must not be in the log.
    assert not any("conn lost" in m for m in msgs), msgs


# ── M4: health 200 with malformed JSON body ──────────────────────────


@pytest.mark.asyncio
async def test_health_check_returns_unreachable_on_malformed_json(monkeypatch):
    """If QWEN_RAFT_URL accidentally points at an HTML page (wrong port,
    proxy default page), /health returns 200 but the body isn't JSON.
    Must surface as unreachable so the chip greys out."""
    stub = _StubAsyncClient(
        responses=_StubResponse(200, "<html>nginx default page</html>"),
    )
    _install_stub(monkeypatch, stub)
    b = QwenRaftBackend(url="http://example.com:8001")
    h = await b.health_check()
    assert h["ok"] is False
    assert h["reason"] == "unreachable"
    assert h["status"] == 200


# ── M4: streaming Auth header round-trip ─────────────────────────────


@pytest.mark.asyncio
async def test_complete_stream_passes_authorization_header(monkeypatch):
    """If QWEN_RAFT_TOKEN is set, complete_stream() must include the
    bearer header just like complete() does — easy to miss in a future
    refactor that factors header construction into complete() only."""
    lines = [_sse_chunk("ok"), "data: [DONE]"]
    stub = _StubAsyncClient(
        stream_response=_StubStreamingResponse(200, lines=lines),
    )
    _install_stub(monkeypatch, stub)
    b = QwenRaftBackend(url="http://example.com:8001", token="bearer-stub")
    chunks = [c async for c in b.complete_stream("hi")]
    assert chunks == ["ok"]
    # Streaming call recorded with the auth header.
    call = stub.calls[0]
    assert call["method"] == "STREAM_POST"
    assert call["headers"].get("Authorization") == "Bearer bearer-stub"


# ── L4: health loaded missing → fail-closed model_not_loaded ─────────


@pytest.mark.asyncio
async def test_health_check_treats_missing_loaded_as_not_loaded(monkeypatch):
    """If a future serve_openai.py version drops the `loaded` field, we
    default to False (fail-closed) so the operator sees the schema gap
    via the model_not_loaded chip state instead of a falsely-green light."""
    stub = _StubAsyncClient(
        responses=_StubResponse(200, {"ok": True, "model": "qwen2.5-7b-raft"}),
    )
    _install_stub(monkeypatch, stub)
    b = QwenRaftBackend(url="http://example.com:8001")
    h = await b.health_check()
    assert h["ok"] is False
    assert h["reason"] == "model_not_loaded"


# ── L5: SSE Accept header on stream path ─────────────────────────────


@pytest.mark.asyncio
async def test_complete_stream_sends_sse_accept_header(monkeypatch):
    """Per the SSE spec, the client should declare Accept: text/event-stream
    so strict reverse-proxies don't downgrade to application/json."""
    lines = [_sse_chunk("ok"), "data: [DONE]"]
    stub = _StubAsyncClient(
        stream_response=_StubStreamingResponse(200, lines=lines),
    )
    _install_stub(monkeypatch, stub)
    b = QwenRaftBackend(url="http://example.com:8001")
    _ = [c async for c in b.complete_stream("hi")]
    call = stub.calls[0]
    assert call["headers"].get("Accept") == "text/event-stream"


@pytest.mark.asyncio
async def test_complete_non_stream_keeps_json_accept_header(monkeypatch):
    """Non-streaming path keeps Accept: application/json — only the SSE
    path overrides."""
    stub = _StubAsyncClient(responses=_StubResponse(200, _chat_envelope("ok")))
    _install_stub(monkeypatch, stub)
    b = QwenRaftBackend(url="http://example.com:8001")
    await b.complete("hi")
    call = stub.calls[0]
    assert call["headers"].get("Accept") == "application/json"


# ── L3: _get_client serializes concurrent cold-start callers ─────────


@pytest.mark.asyncio
async def test_get_client_lock_serializes_cold_start(monkeypatch):
    """Two coroutines hitting complete() simultaneously before any client
    exists must not each construct an AsyncClient — the loser would leak
    its 100-connection pool until process exit."""
    import httpx as _httpx
    construct_count = {"n": 0}
    stub = _StubAsyncClient(responses=_StubResponse(200, _chat_envelope("ok")))

    def _factory(*a, **kw):
        construct_count["n"] += 1
        return stub

    monkeypatch.setattr(_httpx, "AsyncClient", _factory)
    b = QwenRaftBackend(url="http://example.com:8001")
    # Fire two concurrent complete() calls; both observe self._client is None.
    await asyncio.gather(b.complete("a"), b.complete("b"))
    assert construct_count["n"] == 1, \
        f"expected 1 AsyncClient construction under lock, got {construct_count['n']}"


# ══════════════════════════════════════════════════════════════════════
# R4-5 part 2 integration tests — wire QwenRaftBackend into /api/chat,
# /api/status, ChatRequest Literal, and the qwen→codex fallback chain.
# All use the FastAPI TestClient + monkeypatch router.backends so no
# real HTTP or LLM is touched. Protocol-agnostic — these survived the
# Gradio → OpenAI switch unchanged.
# ══════════════════════════════════════════════════════════════════════


import importlib
import re as _re
from pathlib import Path
from fastapi.testclient import TestClient
from nano_notebooklm.types import LLMResponse


def _build_chat_client(monkeypatch, tmp_path, *, qwen_url="http://qwen.example:8001",
                      fake_embed_fn=None):
    """Stand up /api/chat against an isolated artifacts dir with both
    backends configured. Returns (TestClient, server_mod). Callers can
    further monkeypatch server_mod.router.backends to inject canned
    LLMResponses or exceptions."""
    art = tmp_path / "artifacts"
    (art / "courses").mkdir(parents=True)

    monkeypatch.setenv("ARTIFACTS_DIR", str(art))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub-codex")  # so OpenAIBackend registers
    monkeypatch.setenv("QWEN_RAFT_URL", qwen_url)
    monkeypatch.setenv("NANO_NLM_DISABLE_EMBED_WARMUP", "1")
    monkeypatch.setenv("RAG_SCORE_GATE_TOP1", "0.0")

    from nano_notebooklm import config as cfg
    monkeypatch.setattr(cfg, "ARTIFACTS_DIR", art)
    monkeypatch.setattr(cfg, "OPENAI_API_KEY", "sk-stub-codex")
    monkeypatch.setattr(cfg, "QWEN_RAFT_URL", qwen_url)

    if fake_embed_fn is not None:
        from nano_notebooklm.kb import store as kb_store
        monkeypatch.setattr(kb_store, "_get_default_embed_fn", lambda: fake_embed_fn)

    import api.server as server_mod
    importlib.reload(server_mod)

    from nano_notebooklm.orchestrator import router_intent as ri
    ri._LANG_CACHE.clear()
    return TestClient(server_mod.app), server_mod


def _make_stub_backend(name, *, complete_fn=None, complete_resp=None,
                      health_fn=None, health_resp=None):
    """Build a minimal LLMBackend-shaped stub for router.backends injection."""
    class _Stub:
        def __init__(self):
            self.name = name
            self.complete_calls = []
            self.complete_structured_calls = []
            self.health_calls = []

        async def complete(self, prompt, system="", temperature=0.7, max_tokens=4096):
            self.complete_calls.append({"prompt": prompt, "system": system})
            if complete_fn is not None:
                return await complete_fn(prompt, system, temperature, max_tokens)
            return complete_resp or LLMResponse(
                content=f"{name}-answer", model=f"{name}-model",
                input_tokens=1, output_tokens=1, latency_ms=1.0,
            )

        async def complete_structured(self, prompt, system="", temperature=0.3, max_tokens=4096):
            self.complete_structured_calls.append({"prompt": prompt})
            return {}

        async def complete_stream(self, prompt, system="", temperature=0.7, max_tokens=4096):
            resp = await self.complete(prompt, system, temperature, max_tokens)
            yield resp.content

        async def health_check(self):
            self.health_calls.append(True)
            if health_fn is not None:
                return await health_fn()
            return health_resp or {"ok": True}

    return _Stub()


# ── Mini 1: chat routes to qwen when backend="qwen_raft" ─────────────


def test_chat_routes_to_qwen_when_backend_qwen_raft(monkeypatch, tmp_path):
    client, server_mod = _build_chat_client(monkeypatch, tmp_path)
    qwen_stub = _make_stub_backend("qwen_raft")
    openai_stub = _make_stub_backend("openai")
    server_mod.router.backends["qwen_raft"] = qwen_stub
    server_mod.router.backends["openai"] = openai_stub

    r = client.post("/api/chat", json={
        "question": "hello",
        "backend": "qwen_raft",
    })
    assert r.status_code == 200, r.text
    assert len(qwen_stub.complete_calls) == 1
    assert len(openai_stub.complete_calls) == 0


# ── Mini 2: status endpoint lists qwen when URL configured ───────────


def test_status_endpoint_lists_qwen_when_url_configured(monkeypatch, tmp_path):
    client, server_mod = _build_chat_client(monkeypatch, tmp_path)
    server_mod.router.backends["qwen_raft"] = _make_stub_backend(
        "qwen_raft", health_resp={"ok": True, "status": 200},
    )

    r = client.get("/api/status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["qwen_raft_configured"] is True
    assert body["qwen_raft_available"] is True
    assert "qwen_raft" in body["backends"]


# ── Corner 1: URL unconfigured + backend=qwen_raft → 422 ─────────────


def test_chat_qwen_url_unconfigured_returns_422(monkeypatch, tmp_path):
    # Pass qwen_url="" so QwenRaftBackend isn't even registered.
    client, server_mod = _build_chat_client(monkeypatch, tmp_path, qwen_url="")
    r = client.post("/api/chat", json={
        "question": "hello",
        "backend": "qwen_raft",
    })
    assert r.status_code == 422, r.text
    body = r.json()
    # Standard error envelope: {error, request_id, detail}.
    assert "error" in body
    assert "request_id" in body
    assert "not configured" in str(body.get("detail", ""))


# ── Corner 2: qwen timeout falls back to codex + flag ────────────────


def test_chat_qwen_timeout_falls_back_to_codex_with_flag(monkeypatch, tmp_path):
    # Patch QWEN_BACKEND_TIMEOUT_SECONDS to a tiny value so the test
    # doesn't actually wait 30s.
    from nano_notebooklm.skills import qa_skill
    monkeypatch.setattr(qa_skill, "QWEN_BACKEND_TIMEOUT_SECONDS", 0.05)

    client, server_mod = _build_chat_client(monkeypatch, tmp_path)

    async def hang(*a, **kw):
        await asyncio.sleep(2.0)  # well past the 0.05s patched timeout
        raise RuntimeError("should never get here")

    qwen_stub = _make_stub_backend("qwen_raft", complete_fn=hang)
    openai_stub = _make_stub_backend(
        "openai",
        complete_resp=LLMResponse(content="codex-fallback-answer",
                                  model="codex", input_tokens=1,
                                  output_tokens=1, latency_ms=1.0),
    )
    server_mod.router.backends["qwen_raft"] = qwen_stub
    server_mod.router.backends["openai"] = openai_stub

    r = client.post("/api/chat", json={
        "question": "hello",
        "backend": "qwen_raft",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("backend_fallback") is True
    assert "codex-fallback-answer" in body.get("answer", "")
    # Both backends were exercised: qwen attempted, codex completed.
    assert len(qwen_stub.complete_calls) >= 1
    assert len(openai_stub.complete_calls) == 1


# ── Corner 3: status 200 when qwen unavailable ───────────────────────


def test_status_endpoint_returns_200_when_qwen_unavailable(monkeypatch, tmp_path):
    client, server_mod = _build_chat_client(monkeypatch, tmp_path)

    async def boom():
        raise ConnectionError("autodl unreachable")

    server_mod.router.backends["qwen_raft"] = _make_stub_backend(
        "qwen_raft", health_fn=boom,
    )

    r = client.get("/api/status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["qwen_raft_configured"] is True
    assert body["qwen_raft_available"] is False


# ── Corner 4: ChatRequest Literal rejects unknown backend ───────────


def test_chat_request_rejects_unknown_backend_value(monkeypatch, tmp_path):
    client, _ = _build_chat_client(monkeypatch, tmp_path)
    r = client.post("/api/chat", json={
        "question": "hello",
        "backend": "bogus-backend",
    })
    assert r.status_code == 422, r.text


# ── Corner 5: ChatResponse extra=forbid + backend_fallback grep ─────


def test_chat_response_schema_includes_backend_fallback():
    """Source-pin: ChatResponse must list backend_fallback so future
    skills can surface it without ResponseValidationError. extra='forbid'
    is enforced on the model — the field must be explicit."""
    src = Path("api/server.py").read_text(encoding="utf-8")
    m = _re.search(r"class ChatResponse\b[\s\S]+?model_config\s*=\s*\{[\s\S]+?\}", src)
    assert m, "ChatResponse class block not found"
    block = m.group(0)
    assert "backend_fallback" in block
    assert 'extra": "forbid"' in block or "extra=\"forbid\"" in block


# ── Corner 6: router._resolve_backend rejects unconfigured override ──


def test_router_resolve_backend_rejects_missing_backend():
    """Defensive: if a future code path bypasses the chat() 422 guard,
    router._resolve_backend should still RuntimeError rather than
    silently picking a different backend."""
    from nano_notebooklm.ai.router import ModelRouter
    r = ModelRouter()
    # Drop qwen if it happened to register from env.
    r.backends.pop("qwen_raft", None)
    with pytest.raises(RuntimeError, match="qwen_raft"):
        r._resolve_backend("qa_answer", "qwen_raft")
    # codex alias must resolve to "openai" backend key when present.
    if "openai" in r.backends:
        assert r._resolve_backend("qa_answer", "codex").name in ("openai", "codex")


# ── Corner 7: empty backend value (None) leaves task routing intact ──


def test_chat_with_no_backend_uses_default_routing(monkeypatch, tmp_path):
    """backend=None / omitted should preserve the existing task-type
    routing (codex stays the main path). Verifies our wiring doesn't
    accidentally force every request through the qwen branch."""
    client, server_mod = _build_chat_client(monkeypatch, tmp_path)
    qwen_stub = _make_stub_backend("qwen_raft")
    openai_stub = _make_stub_backend("openai")
    server_mod.router.backends["qwen_raft"] = qwen_stub
    server_mod.router.backends["openai"] = openai_stub

    r = client.post("/api/chat", json={"question": "hello"})
    assert r.status_code == 200, r.text
    # qwen must NOT be called when backend kwarg is absent.
    assert len(qwen_stub.complete_calls) == 0
    # backend_fallback should be absent (no qwen attempt → no fallback).
    assert r.json().get("backend_fallback") is None


# ══════════════════════════════════════════════════════════════════════
# R4-5 fix-all v2 (review-swarm follow-up) — cached httpx.AsyncClient.
# Body shapes updated for OpenAI chat-completion envelope (fix-all v3).
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_qwen_client_is_reused_across_calls(monkeypatch):
    """Two consecutive complete() calls must reuse the same underlying
    httpx.AsyncClient instance so we don't pay TCP+TLS handshake on every
    chat turn (~150-400ms over WAN to AutoDL)."""
    import httpx

    construct_count = {"n": 0}
    stub = _StubAsyncClient(responses=_StubResponse(200, _chat_envelope("ok")))

    def _factory(*a, **kw):
        construct_count["n"] += 1
        return stub

    monkeypatch.setattr(httpx, "AsyncClient", _factory)

    b = QwenRaftBackend(url="http://example.com:8001")
    # First call → constructs the cached client.
    await b.complete("hi")
    first = b._client
    assert first is not None
    assert construct_count["n"] == 1

    # Second call → reuses cached client (no new construction).
    await b.complete("hi again")
    assert b._client is first
    assert construct_count["n"] == 1

    # health_check shares the same cached client. (use a /health body)
    stub._responses = [_StubResponse(200, {"ok": True, "loaded": True})]
    await b.health_check()
    assert b._client is first
    assert construct_count["n"] == 1


@pytest.mark.asyncio
async def test_qwen_aclose_releases_cached_client(monkeypatch):
    """aclose() must clear the cached client so a subsequent call lazily
    rebuilds. Exercises the opt-in shutdown hook even though it's not wired
    into FastAPI lifespan."""
    import httpx

    construct_count = {"n": 0}
    stub = _StubAsyncClient(responses=_StubResponse(200, _chat_envelope("ok")))

    def _factory(*a, **kw):
        construct_count["n"] += 1
        return stub

    monkeypatch.setattr(httpx, "AsyncClient", _factory)

    b = QwenRaftBackend(url="http://example.com:8001")
    await b.complete("hi")
    assert construct_count["n"] == 1
    await b.aclose()
    assert b._client is None
    # Next call rebuilds the client.
    await b.complete("hi")
    assert construct_count["n"] == 2
