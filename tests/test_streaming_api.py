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
    client, server_mod = streaming_client

    async def fake_run_skill(name, params):
        return SkillResult(success=True, data={"content": "alpha beta", "quiz": [{"question": "q1"}]})

    monkeypatch.setattr(server_mod.orchestrator, "run_skill", fake_run_skill)

    response = client.post("/api/notes/stream", json={"course_id": "testcourse"})
    assert response.status_code == 200
    events = _read_events(response)

    assert events[0]["type"] == "chunk"
    assert events[-1]["type"] == "done"
    assert events[-1]["content"] == "alpha beta"


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
