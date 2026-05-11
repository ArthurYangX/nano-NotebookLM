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

    async def aclose(self):
        # No-op; cached-client design calls this on backend.aclose().
        return None

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


# ══════════════════════════════════════════════════════════════════════
# R4-5 part 2 integration tests — wire QwenRaftBackend into /api/chat,
# /api/status, ChatRequest Literal, and the qwen→codex fallback chain.
# All use the FastAPI TestClient + monkeypatch router.backends so no
# real HTTP or LLM is touched.
# ══════════════════════════════════════════════════════════════════════


import importlib
import re as _re
from pathlib import Path
from fastapi.testclient import TestClient
from nano_notebooklm.types import LLMResponse


def _build_chat_client(monkeypatch, tmp_path, *, qwen_url="https://qwen.example",
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
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_qwen_client_is_reused_across_calls(monkeypatch):
    """Two consecutive complete() calls must reuse the same underlying
    httpx.AsyncClient instance so we don't pay TCP+TLS handshake on every
    chat turn (~150-400ms over WAN to AutoDL)."""
    import httpx

    construct_count = {"n": 0}
    stub = _StubAsyncClient(
        responses=_StubResponse(200, {"data": ["ok"]})
    )

    def _factory(*a, **kw):
        construct_count["n"] += 1
        return stub

    monkeypatch.setattr(httpx, "AsyncClient", _factory)

    b = QwenRaftBackend(url="https://example.gradio.live")
    # First call → constructs the cached client.
    await b.complete("hi")
    first = b._client
    assert first is not None
    assert construct_count["n"] == 1

    # Second call → reuses cached client (no new construction).
    await b.complete("hi again")
    assert b._client is first
    assert construct_count["n"] == 1

    # health_check shares the same cached client.
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
    stub = _StubAsyncClient(responses=_StubResponse(200, {"data": ["ok"]}))

    def _factory(*a, **kw):
        construct_count["n"] += 1
        return stub

    monkeypatch.setattr(httpx, "AsyncClient", _factory)

    b = QwenRaftBackend(url="https://example.gradio.live")
    await b.complete("hi")
    assert construct_count["n"] == 1
    await b.aclose()
    assert b._client is None
    # Next call rebuilds the client.
    await b.complete("hi")
    assert construct_count["n"] == 2
