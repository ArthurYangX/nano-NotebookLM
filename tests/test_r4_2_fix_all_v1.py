"""R4-2 fix-all v1 regression tests.

Each test pins one fix; a future refactor that reverts the fix lights up
CI immediately.

  A1  setdefault-based lock + eviction (TOCTOU race + DoS dict growth)
  A3  current_stage tracking → error.stage attribution
  A4  final 100% event preserved under backpressure (drain-then-retry)
  A5  drain remaining queue events BEFORE re-raising extract_task
  A6  retry button re-invokes upload via retryRef (frontend grep)
  A7  error path still refreshes courses (frontend grep)
  A8  uploadFiles error envelope carries detail (frontend grep)
  A9  UPLOAD_STAGES constant single source of truth
  A10 progress_callback exception caught inside extractor
  A11 upload.done log + done event duration_ms field
"""

from __future__ import annotations

import asyncio
import importlib
import json
import re
from pathlib import Path

import pytest


# ── A1: setdefault + eviction ─────────────────────────────────────────


def test_upload_lock_for_setdefault_returns_same_lock():
    import api.server as server_mod
    importlib.reload(server_mod)
    server_mod._UPLOAD_LOCKS.clear()
    l1 = server_mod._upload_lock_for("CourseA")
    l2 = server_mod._upload_lock_for("CourseA")
    assert l1 is l2, "concurrent first-time requests must share a lock"


def test_upload_lock_for_source_uses_setdefault():
    """Grep pin: prior implementation `if lock is None: ... = Lock()` had a
    TOCTOU window. Confirm the fix uses setdefault inside the actual
    function body (the docstring may still reference the old anti-pattern
    for historical context — only the executable code matters)."""
    src = Path("api/server.py").read_text(encoding="utf-8")
    m = re.search(r"def _upload_lock_for\([\s\S]+?\n\n", src)
    assert m, "_upload_lock_for not found"
    body = m.group(0)
    # Strip the docstring before grepping the executable body.
    body_no_doc = re.sub(r'"""[\s\S]+?"""', "", body)
    assert "setdefault" in body_no_doc
    # And the prior buggy `if lock is None: ... = ...Lock()` is gone
    # from the executable path.
    assert "if lock is None" not in body_no_doc


def test_maybe_evict_upload_lock_drops_quiescent_above_cap():
    import api.server as server_mod
    importlib.reload(server_mod)
    server_mod._UPLOAD_LOCKS.clear()
    # Fill past the cap with idle locks.
    for i in range(server_mod._UPLOAD_LOCKS_MAX + 5):
        server_mod._upload_lock_for(f"c{i}")
    assert len(server_mod._UPLOAD_LOCKS) == server_mod._UPLOAD_LOCKS_MAX + 5
    server_mod._maybe_evict_upload_lock("c0")
    assert "c0" not in server_mod._UPLOAD_LOCKS


def test_maybe_evict_upload_lock_skips_when_under_cap():
    import api.server as server_mod
    importlib.reload(server_mod)
    server_mod._UPLOAD_LOCKS.clear()
    server_mod._upload_lock_for("c_only")
    server_mod._maybe_evict_upload_lock("c_only")
    # Below the cap → keep it (next upload to same course reuses the lock).
    assert "c_only" in server_mod._UPLOAD_LOCKS


# ── A3 + A11: current_stage attribution + duration_ms ────────────────


@pytest.fixture
def upload_client(monkeypatch, tmp_path, fake_embed_fn):
    art = tmp_path / "artifacts"
    (art / "courses").mkdir(parents=True)
    (art / "uploads").mkdir(parents=True)
    monkeypatch.setattr("nano_notebooklm.config.ARTIFACTS_DIR", art)
    from nano_notebooklm.kb import store as kb_store
    monkeypatch.setattr(kb_store, "_get_default_embed_fn", lambda: fake_embed_fn)
    import api.server as server_mod
    importlib.reload(server_mod)
    # TestClient as context manager so ASGI lifespan starts and the
    # event loop keeps running between requests — required for
    # `asyncio.create_task` to actually progress in the upload pipeline.
    from fastapi.testclient import TestClient
    with TestClient(server_mod.app) as client:
        yield client


def _md_file(name: str = "doc.md") -> tuple[str, bytes, str]:
    # Body deliberately large so the chunker (MIN_CHUNK_TOKENS=50,
    # CHUNK_SIZE_TOKENS=512) produces ≥1 chunk. With less content the
    # upload pipeline takes the empty-corpus shortcut and skips the
    # extractor entirely — which would mask the error-path assertions.
    body = (
        "# Title\n\n"
        "This is paragraph one with enough content to chunk into at least one segment. "
        "It introduces the topic and gives an overview suitable for testing the "
        "ingest pipeline end to end without any LLM call. Backpropagation computes "
        "gradients of loss with respect to weights via the chain rule. "
        "Convolutional neural networks use filters to extract spatial features.\n\n"
        "## Section\n\n"
        "Second paragraph adds more text so the chunker has material to operate on. "
        + ("Lorem ipsum dolor sit amet consectetur adipiscing elit. " * 30)
    ).encode("utf-8")
    return (name, body, "text/markdown")


def _poll_state(client, task_id, *, timeout=15.0, sleep=0.05):
    """Poll /api/upload/status/{task_id} until terminal (done|error)."""
    import time as _time
    deadline = _time.monotonic() + timeout
    last = None
    while _time.monotonic() < deadline:
        r = client.get(f"/api/upload/status/{task_id}")
        if r.status_code == 200:
            last = r.json()
            if last["status"] in ("done", "error"):
                return last
        _time.sleep(sleep)
    raise AssertionError(f"polling timed out; last={last}")


def test_error_event_carries_current_stage(monkeypatch, upload_client):
    """R5 background-task migration of A3: when the extractor raises in
    Stage B, the TaskState must record `error_stage='kg_stage_b'` (not
    None / not 'unknown'). The old NDJSON stream payload became a state
    snapshot field; same invariant, new shape."""
    from nano_notebooklm.kg import extractor as extractor_mod

    async def _boom(chunks, course_name, router, max_chunks=30,
                    progress_callback=None, embed_fn=None):
        if progress_callback is not None:
            progress_callback("kg_stage_a", 0)
            progress_callback("kg_stage_a", 100)
            progress_callback("kg_stage_b", 0)
            progress_callback("kg_stage_b", 50)
        raise RuntimeError("boom inside Stage B")

    monkeypatch.setattr(extractor_mod, "extract_from_chunks", _boom)

    files = [("files", _md_file())]
    r = upload_client.post("/api/upload/StageAttrCourse", files=files)
    assert r.status_code == 200, r.text
    task_id = r.json()["task_id"]
    state = _poll_state(upload_client, task_id)
    assert state["status"] == "error", state
    assert state["error_stage"] == "kg_stage_b", state


def test_done_event_carries_duration_ms(monkeypatch, upload_client):
    """R5 background-task migration of A11: terminal success snapshot must
    include `result.duration_ms`. Replaces the old `done event duration_ms`
    NDJSON contract with the equivalent state-field assertion."""
    from nano_notebooklm.kg import extractor as extractor_mod

    async def _fake(chunks, course_name, router, max_chunks=30,
                    progress_callback=None, embed_fn=None):
        if progress_callback is not None:
            progress_callback("kg_stage_a", 100)
            progress_callback("kg_stage_b", 100)
        return [], []

    monkeypatch.setattr(extractor_mod, "extract_from_chunks", _fake)

    files = [("files", _md_file())]
    r = upload_client.post("/api/upload/DurationCourse", files=files)
    assert r.status_code == 200, r.text
    task_id = r.json()["task_id"]
    state = _poll_state(upload_client, task_id)
    assert state["status"] == "done", state
    assert "duration_ms" in state["result"]
    assert isinstance(state["result"]["duration_ms"], int)
    assert state["result"]["duration_ms"] >= 0


# ── A5: drain remaining queue events BEFORE re-raising ───────────────


def test_drain_queue_before_reraise_source_order():
    """Source pin: the empty-queue drain must run BEFORE `await extract_task`
    so events queued in the same tick as the exception aren't lost.

    R5 background-task rename: the generator `async def _events()` was
    inlined into module-level `async def _run_upload_pipeline(...)`. The
    drain-before-await invariant is preserved; the slice anchor moves.
    """
    src = Path("api/server.py").read_text(encoding="utf-8")
    # fix-all v2: bumped window 8000 → 20000 chars. The upload pipeline
    # body grew (truthful stage refactor) and the legacy 8000-char slice
    # no longer covered the queue-drain block, producing a stale-test
    # "substring not found" against a working invariant.
    upload = src[
        src.index("async def _run_upload_pipeline"):
        src.index("async def _run_upload_pipeline") + 20000
    ]
    drain_pos = upload.index("while not kg_queue.empty()")
    await_pos = upload.index("concepts, relations = await extract_task")
    assert drain_pos < await_pos, "queue drain must precede `await extract_task`"


# ── A9: UPLOAD_STAGES constant ────────────────────────────────────────


def test_extractor_exposes_upload_stages_constant():
    from nano_notebooklm.kg import extractor as extractor_mod
    assert hasattr(extractor_mod, "UPLOAD_STAGES")
    # 2026-05-20: "extracting" split out of "chunking" so the slow
    # MinerU/PyMuPDF page extraction is visible as its own stage.
    assert extractor_mod.UPLOAD_STAGES == (
        "extracting", "chunking", "embedding", "kg_stage_a", "kg_stage_b",
    )
    assert extractor_mod.EXTRACTING == "extracting"
    assert extractor_mod.KG_STAGE_A == "kg_stage_a"
    assert extractor_mod.KG_STAGE_B == "kg_stage_b"


def test_extractor_uses_stage_constants_not_literals():
    """Inside extract_from_chunks the stage names must come from the
    constants — protects against a typo that splits the source-of-truth."""
    src = Path("nano_notebooklm/kg/extractor.py").read_text(encoding="utf-8")
    m = re.search(r"async def extract_from_chunks[\s\S]+?(?=\n\n# |\nasync def |\ndef )", src)
    assert m
    body = m.group(0)
    # The four callback sites should reference the named constants, not
    # the bare string literals.
    assert "KG_STAGE_A" in body
    assert "KG_STAGE_B" in body


# ── A10: progress_callback exception caught ──────────────────────────


async def test_progress_callback_exception_does_not_abort_extraction():
    from nano_notebooklm.kg.extractor import extract_from_chunks
    from nano_notebooklm.types import Chunk, FileType

    chunks = [
        Chunk(chunk_id=f"c{i}", doc_id="d", course_id="X",
              text="The convolution operator slides a learnable filter.",
              file_type=FileType.MARKDOWN, source_file="a.md", location="")
        for i in range(3)
    ]

    class _OkRouter:
        async def complete_structured(self, *a, **kw):
            # Return shapes consistent with both Stage A and Stage B.
            return {
                "course_overview": "X.",
                "topics": [{"name": "T1", "summary": "s", "weight": 1}],
                "concepts": [{"name": "c1", "definition": "d", "type": "definition"}],
                "relations": [],
            }

    def _evil(stage, pct):
        raise ValueError("callback exploded")

    # Should NOT raise — the extractor must suppress callback errors.
    concepts, _ = await extract_from_chunks(
        chunks, course_name="X", router=_OkRouter(), max_chunks=3,
        progress_callback=_evil,
    )
    # And extraction still produced output.
    assert isinstance(concepts, list)


# ── A6 + A7 + A8: frontend grep pins ─────────────────────────────────


def test_app_jsx_retry_button_reinvokes_upload():
    src = Path("frontend/app.jsx").read_text(encoding="utf-8")
    assert "retryRef" in src
    # Retry handler must reference both the ref and the captured payload.
    assert "retryRef.current(processing.retryPayload)" in src
    # The Processing modal's onRetry handler (specifically) must use retryRef
    # — anchor on the Processing component to avoid matching unrelated
    # onRetry props (e.g. notes retry).
    m = re.search(r"<Processing[\s\S]+?onRetry=\{[\s\S]+?\}\}", src)
    assert m, "Processing onRetry block not found"
    block = m.group(0)
    assert "retryRef.current" in block


def test_app_jsx_error_path_refreshes_courses():
    """fix-all v1 #A7: on KG extractor crash, chunks already landed —
    the UI must call getCourses(mode) so the partially-ingested course
    appears in the dropdown. 2026-05-16: upload is now background-task
    + poll, so the refresh lives inside the poll's done/error branch
    (the `_startUploadPolling` helper)."""
    src = Path("frontend/app.jsx").read_text(encoding="utf-8")
    m = re.search(r"function _startUploadPolling\([\s\S]+?\n  \}\n", src)
    assert m, "_startUploadPolling helper not found"
    body = m.group(0)
    # getCourses must run when the task terminates (done OR error).
    assert "API.getCourses" in body
    # setActiveCourse on the other hand IS conditional (only on success).
    assert "setActiveCourse(courseName)" in body
    # Done + error branches both clear interval and localStorage.
    assert "status === \"done\"" in body
    assert "status === \"error\"" in body


def test_api_js_upload_error_carries_detail():
    """fix-all v1 #A8: startUpload (formerly uploadFiles) must parse server's
    {detail, error} envelope into err.message so the UI shows the real reason."""
    src = Path("frontend/api.js").read_text(encoding="utf-8")
    m = re.search(r"async startUpload\([\s\S]+?\n  \},", src)
    assert m
    body = m.group(0)
    # The error branch must reach for body.detail (with body.error as
    # fallback), and surface a requestId from the response header so
    # subsequent debugging is anchored.
    assert "body.detail" in body
    assert "x-request-id" in body
    # And the prior `throw err` with bare `HTTP ${res.status}` is gone
    # as the SOLE error path — must be accompanied by detail extraction.
    assert "new Error(detail" in body
