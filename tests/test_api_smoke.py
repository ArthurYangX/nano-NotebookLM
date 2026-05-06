"""Smoke tests for the FastAPI server.

We seed a tiny isolated artifacts directory and patch the KBStore so the server
runs without requiring real models or LLM keys. Goal: exercise routing,
validation, error handlers, and middleware (request id / latency headers).
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch, tmp_path, sample_chunks, fake_embed_fn):
    """Build a TestClient backed by an in-memory KB seeded from sample_chunks."""
    art = tmp_path / "artifacts"
    (art / "courses" / "testcourse").mkdir(parents=True)

    # Persist chunks so kb.get_chunks(course) works
    chunks_path = art / "courses" / "testcourse" / "chunks.json"
    chunks_path.write_text(
        json.dumps([c.model_dump() for c in sample_chunks], default=str)
    )
    (art / "courses" / "testcourse" / "course_meta.json").write_text(
        json.dumps({"course_id": "testcourse", "name": "Test Course", "documents": ["d1"]})
    )

    monkeypatch.setenv("ARTIFACTS_DIR", str(art))

    from nano_notebooklm import config
    monkeypatch.setattr(config, "ARTIFACTS_DIR", art)

    # Bypass importing api.server's module-level KB initialisation by injecting
    # a no-network embed function before constructing the server module.
    from nano_notebooklm.kb import store as kb_store
    monkeypatch.setattr(kb_store, "_get_default_embed_fn", lambda: fake_embed_fn)

    # Reload-friendly import
    import importlib
    import api.server as server_mod
    importlib.reload(server_mod)

    # Build hybrid index from seeded chunks
    server_mod.kb.build_index("testcourse")

    return TestClient(server_mod.app)


def test_health_returns_ok(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "version" in body


def test_request_id_and_latency_headers_present(client):
    r = client.get("/api/health")
    assert r.headers.get("x-request-id"), "middleware should add x-request-id"
    assert r.headers.get("x-response-time-ms"), "middleware should add timing header"
    # Timing should be a positive number
    assert float(r.headers["x-response-time-ms"]) >= 0


def test_status_lists_backends_and_chunks(client):
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert "backends" in body
    assert body["total_chunks"] >= 1
    assert body["embedding_mode"] in ("local", "api")


def test_courses_lists_seeded_course(client):
    r = client.get("/api/courses")
    assert r.status_code == 200
    courses = r.json()["courses"]
    ids = [c["id"] for c in courses]
    assert "testcourse" in ids
    tc = next(c for c in courses if c["id"] == "testcourse")
    assert tc["chunks"] >= 1


def test_sources_returns_per_file(client):
    r = client.get("/api/sources/testcourse")
    assert r.status_code == 200
    sources = r.json()["sources"]
    titles = {s["title"] for s in sources}
    assert "ml.pdf" in titles
    # Each source should have a chunk count > 0
    assert all(s["chunks"] > 0 for s in sources)


def test_search_returns_relevant_chunk(client):
    r = client.post("/api/search", json={"query": "backpropagation gradients", "top_k": 3})
    assert r.status_code == 200
    results = r.json()["results"]
    assert results, "expected at least one search hit"
    assert any("backprop" in res["text"].lower() for res in results)


def test_validation_rejects_empty_question(client):
    r = client.post("/api/chat", json={"question": ""})
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "validation_error"
    assert body["request_id"]


def test_validation_rejects_bad_format(client):
    r = client.post("/api/notes", json={"course_id": "testcourse", "format": "yaml"})
    assert r.status_code == 422


def test_validation_rejects_oversize_top_k(client):
    r = client.post("/api/search", json={"query": "x", "top_k": 9999})
    assert r.status_code == 422


def test_404_for_unknown_course_sources(client):
    r = client.get("/api/sources/does-not-exist")
    assert r.status_code == 200
    # Empty list is the documented contract
    assert r.json()["sources"] == []


def test_ingest_rejects_missing_directory(client):
    r = client.post("/api/ingest", json={"course_dir": "/nope/this/does/not/exist"})
    assert r.status_code == 404
    assert r.json()["request_id"]
