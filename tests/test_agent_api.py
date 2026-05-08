"""Smoke test for POST /api/agent/stream — NDJSON event stream."""

from __future__ import annotations

import importlib
import json

import pytest
from fastapi.testclient import TestClient


class _FakeBackend:
    """Minimal stand-in for OpenAIBackend so the 503 guard passes."""
    name = "openai"
    model = "test-model"
    client = None


@pytest.fixture
def agent_client(monkeypatch, tmp_path, sample_chunks, fake_embed_fn):
    art = tmp_path / "artifacts"
    (art / "courses" / "testcourse").mkdir(parents=True)
    (art / "courses" / "testcourse" / "chunks.json").write_text(
        json.dumps([c.model_dump() for c in sample_chunks], default=str)
    )
    monkeypatch.setenv("ARTIFACTS_DIR", str(art))

    from nano_notebooklm import config
    monkeypatch.setattr(config, "ARTIFACTS_DIR", art)
    from nano_notebooklm.kb import store as kb_store
    monkeypatch.setattr(kb_store, "_get_default_embed_fn", lambda: fake_embed_fn)

    import api.server as server_mod
    importlib.reload(server_mod)
    server_mod.kb.build_index("testcourse")
    # Use monkeypatch.setitem so the fake backend is auto-removed at test
    # teardown. Direct assignment leaks across tests because importlib.reload
    # caches the module — subsequent tests would see the _FakeBackend.
    monkeypatch.setitem(server_mod.router.backends, "openai", _FakeBackend())
    return TestClient(server_mod.app), server_mod


def _read_events(response):
    return [json.loads(line) for line in response.iter_lines() if line]


def test_agent_stream_503_when_no_backend(monkeypatch, tmp_path, sample_chunks, fake_embed_fn):
    """No openai backend → endpoint returns 503 with structured error."""
    art = tmp_path / "artifacts"
    (art / "courses" / "testcourse").mkdir(parents=True)
    (art / "courses" / "testcourse" / "chunks.json").write_text(
        json.dumps([c.model_dump() for c in sample_chunks], default=str)
    )
    monkeypatch.setenv("ARTIFACTS_DIR", str(art))
    from nano_notebooklm import config
    monkeypatch.setattr(config, "ARTIFACTS_DIR", art)
    from nano_notebooklm.kb import store as kb_store
    monkeypatch.setattr(kb_store, "_get_default_embed_fn", lambda: fake_embed_fn)

    import api.server as server_mod
    importlib.reload(server_mod)
    server_mod.kb.build_index("testcourse")
    # Replace `backends` with an empty dict via monkeypatch so the original
    # is restored at teardown — `.clear()` would mutate the shared dict and
    # leak the empty state to later tests.
    monkeypatch.setattr(server_mod.router, "backends", {})
    client = TestClient(server_mod.app)

    resp = client.post("/api/agent/stream", json={"question": "hi", "course_id": "testcourse"})
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]
    assert body["request_id"]


def test_agent_stream_happy_path(agent_client, monkeypatch):
    """Mock the LLM stream factory; verify text deltas, tool_call/tool_result,
    done event all arrive in order."""
    client, server_mod = agent_client

    def fake_factory(backend):
        async def fake_stream(*, system, messages, tools, temperature, max_tokens):
            # First call: emit a search_kb tool call.
            # We track call count via a closure on `state`.
            state["calls"] += 1
            if state["calls"] == 1:
                yield {"type": "assistant_message", "message": {
                    "role": "assistant", "content": None,
                    "tool_calls": [{
                        "id": "c1", "type": "function",
                        "function": {"name": "search_kb",
                                     "arguments": '{"query": "rrf", "course_id": "testcourse"}'},
                    }],
                }}
            else:
                yield {"type": "text_delta", "delta": "Found "}
                yield {"type": "text_delta", "delta": "rrf info."}
                yield {"type": "assistant_message",
                       "message": {"role": "assistant", "content": "Found rrf info."}}
        return fake_stream

    state = {"calls": 0}
    monkeypatch.setattr(server_mod, "_agent_llm_stream_factory", fake_factory)

    resp = client.post("/api/agent/stream",
                       json={"question": "explain rrf", "course_id": "testcourse"})
    assert resp.status_code == 200
    events = _read_events(resp)

    types = [e["type"] for e in events]
    assert "tool_call" in types
    assert "tool_result" in types
    assert events[-1]["type"] == "done"
    assert events[-1]["answer"] == "Found rrf info."
    # tool_call must carry the parsed arguments
    tc = next(e for e in events if e["type"] == "tool_call")
    assert tc["arguments"] == {"query": "rrf", "course_id": "testcourse"}


def test_agent_stream_validation_blank_question(agent_client):
    client, _ = agent_client
    resp = client.post("/api/agent/stream", json={"question": "   "})
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "validation_error"


def test_agent_stream_validation_max_turns_bounds(agent_client):
    """max_turns must be 1..32 per the AgentRequest model."""
    client, _ = agent_client
    for bad in (0, 33, -1, 100):
        resp = client.post("/api/agent/stream",
                           json={"question": "hi", "max_turns": bad})
        assert resp.status_code == 422, f"max_turns={bad} should 422, got {resp.status_code}"


def test_agent_stream_validation_oversized_course_id(agent_client):
    client, _ = agent_client
    resp = client.post("/api/agent/stream",
                       json={"question": "hi", "course_id": "x" * 500})
    assert resp.status_code == 422


def test_agent_stream_max_turns_hit_via_api(agent_client, monkeypatch):
    """Model loops calling the same tool forever; loop terminates at max_turns
    and the final NDJSON event carries done.max_turns_hit=True."""
    client, server_mod = agent_client

    def fake_factory(backend):
        async def fake_stream(*, system, messages, tools, temperature, max_tokens):
            # Always emit a tool call → loop never terminates naturally.
            yield {"type": "assistant_message", "message": {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": f"c{len(messages)}", "type": "function",
                    "function": {"name": "search_kb",
                                 "arguments": '{"query": "x"}'},
                }],
            }}
        return fake_stream

    monkeypatch.setattr(server_mod, "_agent_llm_stream_factory", fake_factory)

    resp = client.post("/api/agent/stream",
                       json={"question": "loop", "course_id": "testcourse",
                             "max_turns": 2})
    assert resp.status_code == 200
    events = _read_events(resp)
    done = events[-1]
    assert done["type"] == "done"
    assert done["max_turns_hit"] is True
    assert done["turns"] == 2


def test_agent_stream_error_event_delivered_inline(agent_client, monkeypatch):
    """A factory that emits an error event must surface it as NDJSON, not 500."""
    client, server_mod = agent_client

    def fake_factory(backend):
        async def fake_stream(*, system, messages, tools, temperature, max_tokens):
            yield {"type": "text_delta", "delta": "thinking..."}
            yield {"type": "error", "error": "upstream_error"}
        return fake_stream

    monkeypatch.setattr(server_mod, "_agent_llm_stream_factory", fake_factory)

    resp = client.post("/api/agent/stream", json={"question": "boom"})
    assert resp.status_code == 200  # streaming response, error is in-band
    events = _read_events(resp)
    assert events[-1]["type"] == "error"
    assert events[-1]["error"] == "upstream_error"
    assert events[-1]["partial"] == "thinking..."


def test_agent_stream_carries_request_id_header(agent_client, monkeypatch):
    """Streaming responses must still flow through the request-id middleware."""
    client, server_mod = agent_client

    def fake_factory(backend):
        async def fake_stream(*, system, messages, tools, temperature, max_tokens):
            yield {"type": "text_delta", "delta": "ok"}
            yield {"type": "assistant_message",
                   "message": {"role": "assistant", "content": "ok"}}
        return fake_stream

    monkeypatch.setattr(server_mod, "_agent_llm_stream_factory", fake_factory)

    resp = client.post("/api/agent/stream", json={"question": "hi"})
    assert resp.status_code == 200
    assert "x-request-id" in resp.headers
    assert "x-response-time-ms" in resp.headers
