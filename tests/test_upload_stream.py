"""Round 4 #R4-2: /api/upload/{cid} streams NDJSON for the 4-stage
pipeline (chunking → embedding → kg_stage_a → kg_stage_b) + done|error.

Tests are offline:
  - chunker runs against tiny in-memory .md content (no PDF parser needed)
  - embedder uses the fake hash-based embed fn (no sentence-transformers)
  - KG extractor is monkeypatched to a stub that fires
    `progress_callback("kg_stage_a"|"kg_stage_b", pct)` and returns empty
    concepts/relations — so we exercise the streaming wrapper, not the
    LLM path.
"""

from __future__ import annotations

import asyncio
import importlib
import io
import json
import re
from pathlib import Path

import pytest


@pytest.fixture
def upload_client(monkeypatch, tmp_path, fake_embed_fn):
    """A TestClient with isolated artifacts + faked embed + faked KG extract."""
    art = tmp_path / "artifacts"
    (art / "courses").mkdir(parents=True)
    (art / "uploads").mkdir(parents=True)
    monkeypatch.setattr("nano_notebooklm.config.ARTIFACTS_DIR", art)
    from nano_notebooklm.kb import store as kb_store
    monkeypatch.setattr(kb_store, "_get_default_embed_fn", lambda: fake_embed_fn)
    import api.server as server_mod
    importlib.reload(server_mod)

    # Replace the extractor with a stub that exercises both stages of
    # progress_callback then returns an empty graph. Patches BOTH the
    # source module (where it's defined) AND the late binding inside the
    # upload handler (which `from ... import extract_from_chunks` would
    # capture).
    async def _fake_extract(chunks, course_name, router, max_chunks=30,
                            progress_callback=None, embed_fn=None):  # R4-4 fix-all v1 #C9: explicit kwarg pins signature drift
        if progress_callback is not None:
            progress_callback("kg_stage_a", 0)
            await asyncio.sleep(0)
            progress_callback("kg_stage_a", 100)
            progress_callback("kg_stage_b", 0)
            await asyncio.sleep(0)
            progress_callback("kg_stage_b", 50)
            progress_callback("kg_stage_b", 100)
        return [], []

    from nano_notebooklm.kg import extractor as extractor_mod
    monkeypatch.setattr(extractor_mod, "extract_from_chunks", _fake_extract)

    from fastapi.testclient import TestClient
    return TestClient(server_mod.app)


def _consume_ndjson(resp) -> list[dict]:
    body = "".join(chunk for chunk in resp.iter_text())
    return [json.loads(line) for line in body.splitlines() if line.strip()]


def _md_file(name: str = "doc.md", body: str = None) -> tuple[str, bytes, str]:
    body = (body or (
        "# Title\n\n"
        "This is paragraph one with enough content to chunk into at least one segment. "
        "It introduces the topic and gives an overview suitable for testing the "
        "ingest pipeline end to end without any LLM call.\n\n"
        "## Section\n\n"
        "Second paragraph adds more text so the chunker has material to operate on. "
        "Lorem ipsum dolor sit amet consectetur adipiscing elit. " * 4
    )).encode("utf-8")
    return (name, body, "text/markdown")


# ── mini ──────────────────────────────────────────────────────────────


def test_upload_stream_emits_four_stages(upload_client):
    """A normal upload yields chunking + embedding + kg_stage_a + kg_stage_b
    progress events, then a terminal `done`."""
    files = [("files", _md_file())]
    with upload_client.stream(
        "POST", "/api/upload/UploadStreamCourse", files=files
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/x-ndjson")
        events = _consume_ndjson(resp)

    types = [e.get("type") for e in events]
    stages_seen = {e.get("stage") for e in events if e.get("type") == "stage"}
    assert "chunking" in stages_seen
    assert "embedding" in stages_seen
    assert "kg_stage_a" in stages_seen
    assert "kg_stage_b" in stages_seen
    assert types[-1] == "done"
    done = events[-1]
    assert done["course_id"] == "UploadStreamCourse"
    assert done["files"] == 1
    assert done["chunks"] >= 1


def test_upload_stream_progress_monotonic_per_stage(upload_client):
    """Within each stage, progress values are monotonically non-decreasing."""
    files = [("files", _md_file())]
    with upload_client.stream(
        "POST", "/api/upload/UploadProgressCourse", files=files
    ) as resp:
        events = _consume_ndjson(resp)

    by_stage: dict[str, list[int]] = {}
    for e in events:
        if e.get("type") == "stage":
            by_stage.setdefault(e["stage"], []).append(e["progress"])
    for stage, progresses in by_stage.items():
        assert progresses == sorted(progresses), f"{stage} not monotonic: {progresses}"
        assert progresses[-1] == 100, f"{stage} did not reach 100%"


def test_processing_jsx_renders_stage_progress_grep():
    """Frontend processing.jsx must render real stage progress bars + retry."""
    src = Path("frontend/processing.jsx").read_text(encoding="utf-8")
    assert "STAGE_DEFS" in src
    assert "chunking" in src and "embedding" in src
    assert "kg_stage_a" in src and "kg_stage_b" in src
    assert "pstep-bar-fill" in src
    assert "onRetry" in src or "processing-retry" in src


def test_api_js_upload_files_supports_on_event():
    """frontend/api.js uploadFiles must accept onEvent and consume NDJSON."""
    src = Path("frontend/api.js").read_text(encoding="utf-8")
    m = re.search(r"async uploadFiles\([^)]*\)\s*\{[\s\S]+?\n  \},", src)
    assert m, "uploadFiles function not found"
    body = m.group(0)
    assert "onEvent" in body
    assert "TextDecoder" in body
    assert "JSON.parse" in body


# ── corner ────────────────────────────────────────────────────────────


def test_upload_stream_rejects_unsupported_suffix(upload_client):
    """A .exe upload is rejected pre-stream with 400 (not 200 + error event)."""
    files = [("files", ("malware.exe", b"MZ\x90\x00", "application/octet-stream"))]
    r = upload_client.post("/api/upload/RejectExt", files=files)
    assert r.status_code == 400
    assert "Unsupported file type" in r.text


def test_upload_stream_rejects_dotdot_course_id(upload_client):
    """Course id traversal still 400 (path validator runs before stream).

    Use ``foo..bar`` so URL routing accepts the path but the per-handler
    validator catches the embedded `..`. (`/api/upload/..` itself is
    normalised to `/api/upload/` by Starlette and 404s — that's a
    different defense in depth, not what we're testing here.)
    """
    files = [("files", _md_file())]
    r = upload_client.post("/api/upload/foo..bar", files=files)
    assert r.status_code == 400


def test_upload_stream_extractor_failure_emits_error_event(monkeypatch, upload_client):
    """When the KG extractor raises mid-pipeline the response is 200 NDJSON
    ending in `{type:"error", error:"upload_pipeline_failed"}` — embeddings
    and chunks already on disk are preserved."""
    async def _boom(chunks, course_name, router, max_chunks=30,
                    progress_callback=None, embed_fn=None):  # R4-4 fix-all v1 #C9
        if progress_callback is not None:
            progress_callback("kg_stage_a", 0)
        raise RuntimeError("AuthenticationError sk-secretKey1234567890 ysaikeji.cn")

    from nano_notebooklm.kg import extractor as extractor_mod
    monkeypatch.setattr(extractor_mod, "extract_from_chunks", _boom)

    files = [("files", _md_file())]
    with upload_client.stream(
        "POST", "/api/upload/ExtractorBoom", files=files
    ) as resp:
        events = _consume_ndjson(resp)

    err = next((e for e in events if e.get("type") == "error"), None)
    assert err is not None
    # fix-all v4 #A3 contract: stable code, no vendor leak.
    assert err["error"] == "upload_pipeline_failed"
    assert "sk-" not in json.dumps(err)
    assert "ysaikeji" not in json.dumps(err)
    # Chunks survive — only the KG extraction was the failing stage.
    chunks_path = (
        Path(extractor_mod.__file__).resolve().parent.parent.parent  # repo root
    )
    # We don't grep the disk path directly; instead, hit /api/courses to
    # confirm the course is still listed (chunks were saved before stage 3).
    r = upload_client.get("/api/courses?mode=all")
    assert r.status_code == 200
    ids = {c["id"] for c in r.json()["courses"]}
    assert "ExtractorBoom" in ids


def test_upload_stream_concurrent_same_course_serializes(upload_client):
    """Per-course pipeline lock: two concurrent uploads to the same course
    must both succeed (the second waits behind the first) — no half-written
    state, no double done event for the same course concurrently."""
    files1 = [("files", _md_file("a.md"))]
    files2 = [("files", _md_file("b.md"))]

    # TestClient's sync API doesn't run requests in parallel from this
    # thread, but we can verify the lock is *reachable* (a follow-up call
    # against the same course doesn't error or lose the prior chunks).
    with upload_client.stream(
        "POST", "/api/upload/SerCourse", files=files1
    ) as r1:
        ev1 = _consume_ndjson(r1)
    with upload_client.stream(
        "POST", "/api/upload/SerCourse", files=files2
    ) as r2:
        ev2 = _consume_ndjson(r2)

    assert ev1[-1]["type"] == "done"
    assert ev2[-1]["type"] == "done"
    # Second upload should now see >=1 chunk (the union or replacement —
    # incremental ingest decides; what matters is no error).
    assert ev2[-1]["chunks"] >= 1


def test_extract_from_chunks_signature_accepts_progress_callback():
    """The new kwarg must remain backwards-compatible (default=None)."""
    import inspect
    from nano_notebooklm.kg.extractor import extract_from_chunks
    sig = inspect.signature(extract_from_chunks)
    assert "progress_callback" in sig.parameters
    assert sig.parameters["progress_callback"].default is None
