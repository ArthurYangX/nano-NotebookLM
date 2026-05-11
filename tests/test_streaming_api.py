"""Offline tests for streaming generation endpoints."""

from __future__ import annotations

import importlib
import json

import pytest
from fastapi.testclient import TestClient

from nano_notebooklm.types import SkillResult


@pytest.fixture
def streaming_client(monkeypatch, tmp_path, sample_chunks, fake_embed_fn):
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
    return TestClient(server_mod.app), server_mod


def _read_events(response):
    events = []
    for line in response.iter_lines():
        if line:
            events.append(json.loads(line))
    return events


def test_stream_generation_happy(streaming_client, monkeypatch):
    """Pseudo-stream path: quiz still goes through orchestrator.run_skill +
    chunk_text because its output is structured JSON."""
    client, server_mod = streaming_client

    async def fake_run_skill(name, params):
        return SkillResult(success=True, data={"content": "alpha beta", "quiz": [{"question": "q1"}]})

    monkeypatch.setattr(server_mod.orchestrator, "run_skill", fake_run_skill)

    response = client.post("/api/quiz/stream", json={"course_id": "testcourse"})
    assert response.status_code == 200
    events = _read_events(response)

    assert events[0]["type"] == "chunk"
    assert events[-1]["type"] == "done"


def test_stream_generation_timeout(streaming_client, monkeypatch):
    client, server_mod = streaming_client

    async def fake_run_skill(name, params):
        raise TimeoutError("stream interrupted")

    monkeypatch.setattr(server_mod.orchestrator, "run_skill", fake_run_skill)

    response = client.post("/api/quiz/stream", json={"course_id": "testcourse"})
    events = _read_events(response)

    assert response.status_code == 200
    assert events[-1]["type"] == "error"
    assert events[-1]["partial"] == ""
    assert events[-1]["retryable"] is True


# ── Round 2 #5: real streaming for notes / report ─────────────────────


def test_real_stream_notes_pipes_router_deltas(streaming_client, monkeypatch):
    """Round 2 #5 mini: /api/notes/stream pipes `router.complete_stream`
    deltas straight through to NDJSON `chunk` events instead of waiting for
    the full content. We monkeypatch the streaming router to emit a
    deterministic sequence of small deltas; the response should preserve
    each delta as its own NDJSON event.
    """
    client, server_mod = streaming_client

    async def fake_complete_stream(prompt, task_type="", system="",
                                   temperature=0.7, max_tokens=4096):
        # LaTeX-refactor: deltas are LaTeX fragments now, not markdown.
        # Envelope assertions are content-agnostic so this is cosmetic.
        for piece in (r"\section{Intro}" "\n", r"\textbf{alpha} ",
                      r"\emph{beta} ", r"$\gamma$."):
            yield piece

    monkeypatch.setattr(server_mod.router, "complete_stream", fake_complete_stream)

    response = client.post("/api/notes/stream",
                           json={"course_id": "testcourse"})
    assert response.status_code == 200
    events = _read_events(response)

    # 4 deltas + 1 done event
    chunk_events = [e for e in events if e["type"] == "chunk"]
    assert len(chunk_events) == 4, events
    assert [e["chunk"] for e in chunk_events] == [
        r"\section{Intro}" "\n", r"\textbf{alpha} ",
        r"\emph{beta} ", r"$\gamma$.",
    ]
    # `partial` field must accumulate
    assert chunk_events[1]["partial"] == r"\section{Intro}" "\n" r"\textbf{alpha} "
    # done event terminates the stream with the full content
    assert events[-1]["type"] == "done"
    assert "alpha" in events[-1]["content"] and "gamma" in events[-1]["content"]
    # LaTeX-refactor: content must be raw LaTeX (no format_response repair)
    assert r"\section{Intro}" in events[-1]["content"]


def test_real_stream_notes_interruption(streaming_client, monkeypatch):
    """Round 2 #5 corner: stream raises mid-flight → events show partial
    accumulated so far + retryable=true error event, not a crash."""
    client, server_mod = streaming_client

    async def fake_complete_stream(prompt, task_type="", system="",
                                   temperature=0.7, max_tokens=4096):
        yield "first chunk "
        yield "second chunk "
        raise RuntimeError("upstream cancelled")

    monkeypatch.setattr(server_mod.router, "complete_stream", fake_complete_stream)

    response = client.post("/api/notes/stream",
                           json={"course_id": "testcourse"})
    events = _read_events(response)
    assert events[-1]["type"] == "error"
    assert events[-1]["partial"] == "first chunk second chunk "
    assert events[-1]["retryable"] is True


def test_real_stream_falls_back_when_inputs_missing(streaming_client, monkeypatch):
    """Round 2 #5 corner: prepare_inputs returns None (e.g. course has no
    chunks) → error event with retryable hint, no crash."""
    client, server_mod = streaming_client

    # Force prepare_inputs → None on the note skill
    note_skill = server_mod.orchestrator.skills["note_generator"]
    monkeypatch.setattr(note_skill, "prepare_inputs", lambda params: None)

    async def fake_complete_stream(*a, **kw):
        raise AssertionError("complete_stream should not be called when inputs are missing")
        yield  # pragma: no cover  (make this an async generator)
    monkeypatch.setattr(server_mod.router, "complete_stream", fake_complete_stream)

    response = client.post("/api/notes/stream",
                           json={"course_id": "testcourse"})
    events = _read_events(response)
    assert events[-1]["type"] == "error"
    assert events[-1]["retryable"] is True
