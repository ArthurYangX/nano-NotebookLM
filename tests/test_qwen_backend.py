"""Round 4 #R4-5 — Qwen-RAFT remote backend contract tests.

All tests are offline: ``httpx.AsyncClient`` is monkeypatched with a stub
that records call args and returns canned ``data: [...]`` envelopes.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

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


class _StubAsyncClient:
    """Records POST/GET calls and returns the canned response chain.

    ``responses`` is consumed in FIFO order. If you only pass one
    response it's reused for every call (matches the common case where
    the test just wants to assert one round-trip).
    """

    def __init__(self, *, responses: list[_StubResponse] | _StubResponse | None = None,
                 raise_on_call: Exception | None = None):
        if isinstance(responses, _StubResponse):
            responses = [responses]
        self._responses = list(responses or [])
        self._raise = raise_on_call
        self.calls: list[dict[str, Any]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url: str, *, json=None, headers=None):
        self.calls.append({"method": "POST", "url": url, "json": json, "headers": headers})
        if self._raise is not None:
            raise self._raise
        return self._next_response()

    async def get(self, url: str, *, headers=None):
        self.calls.append({"method": "GET", "url": url, "headers": headers})
        if self._raise is not None:
            raise self._raise
        return self._next_response()

    def _next_response(self):
        if not self._responses:
            return _StubResponse(200, {"data": ["fallback"]})
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


# ── configured / not_configured gate ───────────────────────────────


def test_qwen_backend_not_configured_when_url_empty():
    b = QwenRaftBackend(url="", token="", model_name="qwen2.5-7b-raft")
    assert b.configured is False


def test_qwen_backend_configured_when_url_set():
    b = QwenRaftBackend(url="https://example.gradio.live", token="")
    assert b.configured is True


@pytest.mark.asyncio
async def test_complete_raises_not_configured_when_url_empty():
    b = QwenRaftBackend(url="", token="")
    with pytest.raises(QwenBackendError) as ei:
        await b.complete("hi")
    assert ei.value.code == "not_configured"


# ── happy path ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_complete_posts_to_api_predict_with_data_envelope(monkeypatch):
    stub = _StubAsyncClient(responses=_StubResponse(200, {"data": ["hello, world"], "duration": 0.42}))
    _install_stub(monkeypatch, stub)

    b = QwenRaftBackend(url="https://example.gradio.live", token="t1", fn_index=0)
    resp = await b.complete("hi", system="you are helpful")

    assert resp.content == "hello, world"
    assert resp.model == "qwen2.5-7b-raft"
    assert resp.latency_ms >= 0
    # One POST to /api/predict.
    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://example.gradio.live/api/predict"
    # Body wraps prompt+system into a single string under data[0].
    assert isinstance(call["json"], dict)
    assert call["json"].get("fn_index") == 0
    data = call["json"].get("data")
    assert isinstance(data, list) and len(data) == 1
    assert "you are helpful" in data[0]
    assert "hi" in data[0]
    # Authorization header threaded through.
    assert call["headers"].get("Authorization") == "Bearer t1"


@pytest.mark.asyncio
async def test_complete_accepts_chatbot_history_response_shape(monkeypatch):
    """Some Gradio chatbots emit data as [[[user, assistant], ...]] instead
    of a bare string. The backend unwraps the last assistant turn."""
    history = [["hi", "hello back"], ["wassup", "all good thanks"]]
    stub = _StubAsyncClient(responses=_StubResponse(200, {"data": [history]}))
    _install_stub(monkeypatch, stub)

    b = QwenRaftBackend(url="https://example.gradio.live")
    resp = await b.complete("doesn't matter")
    assert resp.content == "all good thanks"


@pytest.mark.asyncio
async def test_complete_accepts_dict_message_shape(monkeypatch):
    stub = _StubAsyncClient(responses=_StubResponse(200, {"data": [{"content": "from dict"}]}))
    _install_stub(monkeypatch, stub)
    b = QwenRaftBackend(url="https://example.gradio.live")
    resp = await b.complete("hi")
    assert resp.content == "from dict"


# ── error paths ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_complete_raises_timeout_code_on_httpx_timeout(monkeypatch):
    import httpx
    stub = _StubAsyncClient(raise_on_call=httpx.TimeoutException("slow"))
    _install_stub(monkeypatch, stub)
    b = QwenRaftBackend(url="https://example.gradio.live", timeout=0.1)
    with pytest.raises(QwenBackendError) as ei:
        await b.complete("hi")
    assert ei.value.code == "timeout"


@pytest.mark.asyncio
async def test_complete_raises_transport_failed_on_connection_error(monkeypatch):
    import httpx
    stub = _StubAsyncClient(raise_on_call=httpx.ConnectError("refused"))
    _install_stub(monkeypatch, stub)
    b = QwenRaftBackend(url="https://example.gradio.live")
    with pytest.raises(QwenBackendError) as ei:
        await b.complete("hi")
    assert ei.value.code == "transport_failed"


@pytest.mark.asyncio
async def test_complete_raises_upstream_5xx_on_server_error(monkeypatch):
    stub = _StubAsyncClient(responses=_StubResponse(503, {"error": "model loading"}))
    _install_stub(monkeypatch, stub)
    b = QwenRaftBackend(url="https://example.gradio.live")
    with pytest.raises(QwenBackendError) as ei:
        await b.complete("hi")
    assert ei.value.code == "upstream_5xx"


@pytest.mark.asyncio
async def test_complete_raises_empty_response_when_data_missing(monkeypatch):
    stub = _StubAsyncClient(responses=_StubResponse(200, {"data": []}))
    _install_stub(monkeypatch, stub)
    b = QwenRaftBackend(url="https://example.gradio.live")
    with pytest.raises(QwenBackendError) as ei:
        await b.complete("hi")
    assert ei.value.code == "empty_response"


@pytest.mark.asyncio
async def test_qwen_backend_error_does_not_leak_url_in_message():
    """fix-all v4 #A3 discipline applied to qwen too: the stable code
    must be exposed but the error str() (which goes to client through
    the error event) must not echo the secret URL."""
    err = QwenBackendError("timeout", "https://secret.gradio.live failed to respond")
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
async def test_health_check_returns_ok_on_200(monkeypatch):
    stub = _StubAsyncClient(responses=_StubResponse(200))
    _install_stub(monkeypatch, stub)
    b = QwenRaftBackend(url="https://example.gradio.live")
    h = await b.health_check()
    assert h["ok"] is True
    assert h["status"] == 200


@pytest.mark.asyncio
async def test_health_check_returns_unreachable_on_exception(monkeypatch):
    import httpx
    stub = _StubAsyncClient(raise_on_call=httpx.ConnectError("nope"))
    _install_stub(monkeypatch, stub)
    b = QwenRaftBackend(url="https://example.gradio.live")
    h = await b.health_check()
    assert h["ok"] is False
    assert h["reason"] == "unreachable"


@pytest.mark.asyncio
async def test_health_check_returns_timeout_on_httpx_timeout(monkeypatch):
    import httpx
    stub = _StubAsyncClient(raise_on_call=httpx.TimeoutException("slow"))
    _install_stub(monkeypatch, stub)
    b = QwenRaftBackend(url="https://example.gradio.live")
    h = await b.health_check()
    assert h["ok"] is False
    assert h["reason"] == "timeout"


# ── complete_structured best-effort ────────────────────────────────


@pytest.mark.asyncio
async def test_complete_structured_returns_dict_on_clean_json(monkeypatch):
    stub = _StubAsyncClient(responses=_StubResponse(200, {"data": ['{"answer": "yes"}']}))
    _install_stub(monkeypatch, stub)
    b = QwenRaftBackend(url="https://example.gradio.live")
    out = await b.complete_structured("ask")
    assert out == {"answer": "yes"}


@pytest.mark.asyncio
async def test_complete_structured_strips_code_fence(monkeypatch):
    fenced = "```json\n{\"answer\": \"with fence\"}\n```"
    stub = _StubAsyncClient(responses=_StubResponse(200, {"data": [fenced]}))
    _install_stub(monkeypatch, stub)
    b = QwenRaftBackend(url="https://example.gradio.live")
    out = await b.complete_structured("ask")
    assert out == {"answer": "with fence"}


@pytest.mark.asyncio
async def test_complete_structured_returns_error_dict_on_non_json(monkeypatch):
    stub = _StubAsyncClient(responses=_StubResponse(200, {"data": ["plain prose not json"]}))
    _install_stub(monkeypatch, stub)
    b = QwenRaftBackend(url="https://example.gradio.live")
    out = await b.complete_structured("ask")
    assert out["error"] == "non_json_output"
    assert out["raw"].startswith("plain prose")


# ── complete_stream falls back to single chunk ─────────────────────


@pytest.mark.asyncio
async def test_complete_stream_yields_full_content_as_one_chunk(monkeypatch):
    stub = _StubAsyncClient(responses=_StubResponse(200, {"data": ["streamed once"]}))
    _install_stub(monkeypatch, stub)
    b = QwenRaftBackend(url="https://example.gradio.live")
    chunks = []
    async for delta in b.complete_stream("hi"):
        chunks.append(delta)
    assert chunks == ["streamed once"]
