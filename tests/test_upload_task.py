"""R4-2 background-task upload: `POST /api/upload/{cid}` schedules a
background pipeline and returns ``{task_id, course_id}``; `GET /api/upload/
status/{task_id}` polls the live TaskState; per-course pipeline lock keeps
concurrent uploads to the same course serialised.

Tests are offline:
  - chunker runs against tiny in-memory .md content (no PDF parser needed)
  - embedder uses the fake hash-based embed fn (no sentence-transformers)
  - KG extractor is monkeypatched to a stub that fires
    `progress_callback("kg_stage_a"|"kg_stage_b", pct)` and returns empty
    concepts/relations — so we exercise the pipeline wrapper, not the
    LLM path.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import re
import time
import uuid
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
    # progress_callback then returns an empty graph. Patches the source
    # module (where it's defined) so the late-bound import inside the
    # upload pipeline picks up the stub too.
    async def _fake_extract(chunks, course_name, router, max_chunks=30,
                            progress_callback=None, embed_fn=None):
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

    # IMPORTANT: TestClient MUST be used as a context manager so its
    # ASGI lifespan starts and the event loop runs *between* requests.
    # Without `with TestClient(...)`, `asyncio.create_task` schedules
    # the pipeline on a loop that only iterates during an active
    # request — the task hangs at the first await and `status` stays
    # "running" forever.
    from fastapi.testclient import TestClient
    with TestClient(server_mod.app) as client:
        yield client


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


def _poll_until(client, task_id: str, *, predicate, timeout=15.0, sleep=0.05):
    """Poll /api/upload/status/{task_id} until `predicate(state)` is True
    or the timeout elapses. Returns the last seen state (raises on
    timeout)."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        r = client.get(f"/api/upload/status/{task_id}")
        if r.status_code == 404:
            time.sleep(sleep)
            continue
        assert r.status_code == 200, f"status poll failed: {r.status_code} {r.text}"
        last = r.json()
        if predicate(last):
            return last
        time.sleep(sleep)
    raise AssertionError(f"polling timed out; last state={last}")


# ── happy path ────────────────────────────────────────────────────────


def test_upload_returns_task_id(upload_client):
    """POST returns 200 + `{task_id, course_id}` immediately; the TaskState
    registry has the new entry."""
    files = [("files", _md_file())]
    r = upload_client.post("/api/upload/TaskIdCourse", files=files)
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body["task_id"], str)
    assert isinstance(body["course_id"], str)
    assert body["course_id"] == "TaskIdCourse"
    assert re.fullmatch(r"[a-f0-9]{32}", body["task_id"]), body["task_id"]

    import api.server as server_mod
    assert body["task_id"] in server_mod.app.state.upload_tasks


def test_upload_status_endpoint_returns_snapshot(upload_client):
    """Poll /api/upload/status until the pipeline reaches `done`; assert
    all 4 stages reached 100%, status=='done', and the result block is
    populated."""
    files = [("files", _md_file())]
    r = upload_client.post("/api/upload/SnapshotCourse", files=files)
    assert r.status_code == 200
    task_id = r.json()["task_id"]

    state = _poll_until(
        upload_client, task_id,
        predicate=lambda s: s["status"] in ("done", "error"),
    )
    assert state["status"] == "done", state
    for stage in ("chunking", "embedding", "kg_stage_a", "kg_stage_b"):
        assert state["stages"][stage]["progress"] == 100, state["stages"]
    assert state["result"]["course_id"] == "SnapshotCourse"
    assert state["result"]["files"] == 1
    assert state["result"]["chunks"] >= 1
    assert state["error"] is None


# ── validation ────────────────────────────────────────────────────────


def test_upload_rejects_unsupported_suffix(upload_client):
    """A .exe upload is rejected pre-task with 400 (no task_id issued)."""
    files = [("files", ("malware.exe", b"MZ\x90\x00", "application/octet-stream"))]
    r = upload_client.post("/api/upload/RejectExt", files=files)
    assert r.status_code == 400
    assert "Unsupported file type" in r.text


def test_upload_rejects_dotdot_course_id(upload_client):
    """Course id traversal still 400 (path validator runs before scheduling).

    Use ``foo..bar`` so URL routing accepts the path but the per-handler
    validator catches the embedded `..`.
    """
    files = [("files", _md_file())]
    r = upload_client.post("/api/upload/foo..bar", files=files)
    assert r.status_code == 400


# ── status endpoint corner cases ──────────────────────────────────────


def test_upload_status_404_on_unknown_task_id(upload_client):
    """Well-formed but unknown task_id → 404."""
    r = upload_client.get(f"/api/upload/status/{uuid.uuid4().hex}")
    assert r.status_code == 404


def test_upload_status_400_on_malformed_task_id(upload_client):
    """Non-uuid task_id (with dashes / non-hex chars) → 400."""
    r = upload_client.get("/api/upload/status/not-a-uuid")
    assert r.status_code == 400


# ── failure path ──────────────────────────────────────────────────────


def test_upload_extractor_failure_records_error(monkeypatch, upload_client):
    """When the KG extractor raises mid-pipeline the TaskState transitions
    to status='error' with stable `error="upload_pipeline_failed"` and
    `error_stage` set to the failing stage; chunks already on disk are
    preserved (course is still listed)."""
    async def _boom(chunks, course_name, router, max_chunks=30,
                    progress_callback=None, embed_fn=None):
        if progress_callback is not None:
            progress_callback("kg_stage_a", 0)
        raise RuntimeError("AuthenticationError sk-secretKey1234567890 ysaikeji.cn")

    from nano_notebooklm.kg import extractor as extractor_mod
    monkeypatch.setattr(extractor_mod, "extract_from_chunks", _boom)

    files = [("files", _md_file())]
    r = upload_client.post("/api/upload/ExtractorBoom", files=files)
    assert r.status_code == 200
    task_id = r.json()["task_id"]

    state = _poll_until(
        upload_client, task_id,
        predicate=lambda s: s["status"] in ("done", "error"),
    )
    assert state["status"] == "error", state
    assert state["error"] == "upload_pipeline_failed"
    assert state["error_stage"] in ("kg_stage_a", "kg_stage_b")
    # PII scrub: vendor / key strings must not surface in the public state.
    blob = json.dumps(state)
    assert "sk-" not in blob
    assert "ysaikeji" not in blob

    r2 = upload_client.get("/api/courses?mode=all")
    assert r2.status_code == 200
    ids = {c["id"] for c in r2.json()["courses"]}
    assert "ExtractorBoom" in ids


# ── serialisation ─────────────────────────────────────────────────────


def test_upload_concurrent_same_course_serializes(monkeypatch, upload_client):
    """Two POSTs to the same course return two task_ids immediately. The
    second task stays `status="waiting"` until the first transitions to a
    terminal state, because `_upload_lock_for(course_id)` is acquired
    inside the background coroutine.
    """
    # Slow down chunking on the first task so the second task can be
    # observed in the `waiting` state. We monkeypatch kb.ingest_course
    # to sleep on the first call only.
    import api.server as server_mod

    call_count = {"n": 0}
    real_ingest = server_mod.kb.ingest_course

    def _slow(course_dir, course_id=None, engine="pymupdf", lang="ch"):
        call_count["n"] += 1
        if call_count["n"] == 1:
            time.sleep(0.6)
        return real_ingest(course_dir, course_id, engine, lang)

    monkeypatch.setattr(server_mod.kb, "ingest_course", _slow)

    files1 = [("files", _md_file("a.md"))]
    files2 = [("files", _md_file("b.md"))]
    r1 = upload_client.post("/api/upload/SerCourse", files=files1)
    r2 = upload_client.post("/api/upload/SerCourse", files=files2)
    assert r1.status_code == 200 and r2.status_code == 200
    tid1, tid2 = r1.json()["task_id"], r2.json()["task_id"]
    assert tid1 != tid2

    # Sample both tasks while the first is still running.
    time.sleep(0.1)
    observed_waiting = False
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline:
        s1 = upload_client.get(f"/api/upload/status/{tid1}").json()
        s2 = upload_client.get(f"/api/upload/status/{tid2}").json()
        if s1["status"] == "running" and s2["status"] == "waiting":
            observed_waiting = True
            break
        if s1["status"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert observed_waiting, "second task should have stayed waiting while first ran"

    # Both eventually finish.
    final1 = _poll_until(upload_client, tid1,
                         predicate=lambda s: s["status"] in ("done", "error"))
    final2 = _poll_until(upload_client, tid2,
                         predicate=lambda s: s["status"] in ("done", "error"))
    assert final1["status"] == "done", final1
    assert final2["status"] == "done", final2
    assert final2["result"]["chunks"] >= 1


# ── eviction ──────────────────────────────────────────────────────────


def test_upload_task_eviction_after_ttl(upload_client, monkeypatch):
    """Monkeypatch TTL to 0; a manually-injected ended task is dropped on
    the next eviction sweep."""
    import api.server as server_mod
    monkeypatch.setattr(server_mod, "_UPLOAD_TASKS_TTL_S", 0)

    tid = "0" * 32
    server_mod._UPLOAD_TASKS[tid] = {
        "task_id": tid,
        "course_id": "EvictCourse",
        "started_at": time.time() - 10,
        "ended_at": time.time() - 1,
        "status": "done",
        "stages": {},
        "result": {"chunks": 0},
        "error": None,
        "error_stage": None,
        "file_names": [],
    }
    assert tid in server_mod._UPLOAD_TASKS
    server_mod._maybe_evict_upload_tasks()
    assert tid not in server_mod._UPLOAD_TASKS


def test_upload_eviction_preserves_running_tasks(upload_client, monkeypatch):
    """A running task with no ended_at must survive eviction even when the
    dict is over the cap."""
    import api.server as server_mod
    monkeypatch.setattr(server_mod, "_UPLOAD_TASKS_MAX", 1)
    monkeypatch.setattr(server_mod, "_UPLOAD_TASKS_TTL_S", 3600)

    running_tid = "a" * 32
    ended_tid = "b" * 32
    server_mod._UPLOAD_TASKS.clear()
    server_mod._UPLOAD_TASKS[running_tid] = {
        "task_id": running_tid, "course_id": "RunCourse",
        "started_at": time.time(), "ended_at": None,
        "status": "running", "stages": {}, "result": None,
        "error": None, "error_stage": None, "file_names": [],
    }
    server_mod._UPLOAD_TASKS[ended_tid] = {
        "task_id": ended_tid, "course_id": "EndedCourse",
        "started_at": time.time() - 10, "ended_at": time.time() - 5,
        "status": "done", "stages": {}, "result": {},
        "error": None, "error_stage": None, "file_names": [],
    }
    server_mod._maybe_evict_upload_tasks()
    assert running_tid in server_mod._UPLOAD_TASKS, "running task evicted!"


def test_upload_task_eviction_cap_drops_oldest_ended_first(monkeypatch, upload_client):
    """review-swarm M4 (2026-05-16): cap-based eviction must drop the
    OLDEST `ended_at` first, never run tasks. Pass-1 TTL is hot-set high
    so cap-based eviction (Pass 2) is the only path. Pin both ordering
    and the running-task immunity.
    """
    import api.server as server_mod
    monkeypatch.setattr(server_mod, "_UPLOAD_TASKS_MAX", 2)
    monkeypatch.setattr(server_mod, "_UPLOAD_TASKS_TTL_S", 3600)
    server_mod._UPLOAD_TASKS.clear()

    now = time.time()
    # 3 ended tasks with monotonically increasing ended_at + 1 running.
    for i, ended_offset in enumerate([-30, -20, -10]):
        tid = chr(ord("a") + i) * 32
        server_mod._UPLOAD_TASKS[tid] = {
            "task_id": tid, "course_id": f"C{i}",
            "started_at": now - 60, "ended_at": now + ended_offset,
            "status": "done", "stages": {}, "result": {},
            "error": None, "error_stage": None, "file_names": [],
        }
    running_tid = "f" * 32
    server_mod._UPLOAD_TASKS[running_tid] = {
        "task_id": running_tid, "course_id": "Run",
        "started_at": now, "ended_at": None,
        "status": "running", "stages": {}, "result": None,
        "error": None, "error_stage": None, "file_names": [],
    }
    assert len(server_mod._UPLOAD_TASKS) == 4

    server_mod._maybe_evict_upload_tasks()
    # Cap=2, but running is immune → expect 2 entries: running + freshest ended.
    assert running_tid in server_mod._UPLOAD_TASKS, "running task must never be evicted"
    assert "c" * 32 in server_mod._UPLOAD_TASKS, "freshest ended (now-10) must survive"
    # Oldest two ended (now-30 and now-20) should both be dropped.
    assert "a" * 32 not in server_mod._UPLOAD_TASKS, "oldest ended (now-30) should be evicted"
    assert "b" * 32 not in server_mod._UPLOAD_TASKS, "second-oldest ended (now-20) should be evicted"


def test_upload_task_strong_ref_set_keeps_pipeline_alive(upload_client):
    """review-swarm M4 (2026-05-16): the `_UPLOAD_TASK_OBJECTS` strong-ref
    set is the contract that prevents Python from GC'ing the in-flight
    asyncio.Task. If a future refactor drops the `.add(...)` line, the
    pipeline would race with GC depending on local refs — flaky in prod,
    silent in tests. Pin the set by source and by membership during a
    real in-flight upload.
    """
    import api.server as server_mod
    # Source-level pin: the add+discard pattern must live in the POST
    # endpoint body (not just somewhere in the module).
    import inspect
    src = inspect.getsource(server_mod.upload_files)
    assert "_UPLOAD_TASK_OBJECTS.add(" in src, (
        "review-swarm M4: upload_files must add the bg task to "
        "_UPLOAD_TASK_OBJECTS to keep it alive across GC"
    )
    assert "add_done_callback(_UPLOAD_TASK_OBJECTS.discard)" in src, (
        "review-swarm M4: upload_files must register the discard "
        "callback so completed tasks don't leak in the set"
    )

    # Behavior-level pin: during a real in-flight upload the set must
    # be non-empty. Even with `gc.collect()` forcing a GC pass, the
    # task object survives because the set holds the strong ref.
    import gc
    files = [("files", _md_file())]
    r = upload_client.post("/api/upload/StrongRefCourse", files=files)
    assert r.status_code == 200
    # Force a GC pass — the strong-ref set must keep the task alive.
    gc.collect()
    # Now poll to completion; if the task got GC'd, it'd never progress
    # and _poll_until would AssertionError on timeout.
    state = _poll_until(
        upload_client, r.json()["task_id"],
        predicate=lambda s: s["status"] in ("done", "error"),
        timeout=10.0,
    )
    assert state["status"] == "done", state


def test_upload_status_response_excludes_saved_count(upload_client):
    """review-swarm M5 (2026-05-16): `saved_count` is an internal field
    of the TaskState dict (used by `_run_upload_pipeline` for the result
    `files` count). It must NOT leak out via /api/upload/status — the
    Pydantic response model forbids extras, and the endpoint allow-lists
    fields before returning.
    """
    files = [("files", _md_file())]
    r = upload_client.post("/api/upload/SavedCountCourse", files=files)
    task_id = r.json()["task_id"]
    s = upload_client.get(f"/api/upload/status/{task_id}").json()
    assert "saved_count" not in s, (
        "review-swarm M5: saved_count is an internal field; "
        "the response Pydantic model + allow-list filter must drop it"
    )
    # Sanity: response is still well-formed.
    assert s["task_id"] == task_id
    assert "stages" in s
    assert "file_names" in s


def test_upload_file_names_are_sanitized(monkeypatch, upload_client):
    """review-swarm H1 (2026-05-16): `state["file_names"]` must mirror
    the on-disk sanitized name, not the raw client-provided filename
    (which could carry RTL bidi / control chars / > 255 bytes and
    bypass `_safe_upload_name`).
    """
    # Raw filename with a control character (bell, 0x07) — the on-disk
    # name should NOT contain it; neither should state["file_names"].
    raw_name = "report\x07.md"
    body = (
        "# Test\n\n"
        "Body content for ingest. " * 5
    ).encode("utf-8")
    files = [("files", (raw_name, body, "text/markdown"))]
    r = upload_client.post("/api/upload/SanitizeCourse", files=files)
    assert r.status_code == 200, r.text
    task_id = r.json()["task_id"]
    s = upload_client.get(f"/api/upload/status/{task_id}").json()
    assert s["file_names"], "file_names should not be empty"
    for name in s["file_names"]:
        assert "\x07" not in name, (
            f"review-swarm H1: file_names must be sanitized; got {name!r}"
        )


# ── concurrent pollers ────────────────────────────────────────────────


def test_upload_two_pollers_same_task_id(upload_client):
    """Two back-to-back polls return consistent (identical or one-tick-apart)
    snapshots — no torn state, no AttributeError on the shared dict."""
    files = [("files", _md_file())]
    r = upload_client.post("/api/upload/PollersCourse", files=files)
    task_id = r.json()["task_id"]

    s1 = upload_client.get(f"/api/upload/status/{task_id}").json()
    s2 = upload_client.get(f"/api/upload/status/{task_id}").json()
    assert s1["task_id"] == s2["task_id"] == task_id
    assert s1["course_id"] == s2["course_id"]
    # Progress is monotonic within a single stage so the second poll's
    # `chunking` progress must be ≥ the first's.
    assert s2["stages"]["chunking"]["progress"] >= s1["stages"]["chunking"]["progress"]


# ── R5/MinerU engine + lang query parameters ──────────────────────


def test_upload_engine_pymupdf_default(upload_client):
    """Without `?engine=` the upload defaults to pymupdf — and writes a
    `.extract_engine` marker so re-uploads can detect engine switches."""
    files = [("files", _md_file())]
    r = upload_client.post("/api/upload/EngineDefault", files=files)
    assert r.status_code == 200
    task_id = r.json()["task_id"]
    _poll_until(upload_client, task_id,
                predicate=lambda s: s["status"] in ("done", "error"))

    from nano_notebooklm import config as _cfg
    marker = _cfg.ARTIFACTS_DIR / "courses" / "EngineDefault" / ".extract_engine"
    assert marker.exists()
    assert marker.read_text().strip() == "pymupdf"


def test_upload_engine_mineru_routes_through_extractor(monkeypatch, upload_client):
    """`?engine=mineru` flows through to kb.ingest_course(engine='mineru').

    We don't actually run mineru here (CI doesn't have its models) — we
    monkeypatch ingest_course to capture the engine kwarg.
    """
    captured = {}

    from nano_notebooklm.kb.store import KBStore
    real_ingest = KBStore.ingest_course

    def _spy(self, course_dir, course_id=None, engine="pymupdf", lang="ch"):
        captured["engine"] = engine
        captured["lang"] = lang
        # Fall back to real implementation so the rest of the pipeline runs.
        return real_ingest(self, course_dir, course_id, engine="pymupdf", lang=lang)

    monkeypatch.setattr(KBStore, "ingest_course", _spy)

    files = [("files", _md_file())]
    r = upload_client.post("/api/upload/EngineMineru?engine=mineru&lang=en", files=files)
    assert r.status_code == 200
    _poll_until(upload_client, r.json()["task_id"],
                predicate=lambda s: s["status"] in ("done", "error"))
    assert captured["engine"] == "mineru"
    assert captured["lang"] == "en"


def test_upload_engine_invalid_rejected(upload_client):
    """`?engine=foo` returns 422 (FastAPI Query pattern guard) — never silently degrades."""
    files = [("files", _md_file())]
    r = upload_client.post("/api/upload/EngineBad?engine=tesseract", files=files)
    assert r.status_code == 422


def test_upload_lang_invalid_rejected(upload_client):
    """`?lang=xx` returns 422 — only `ch` and `en` are accepted by mineru today."""
    files = [("files", _md_file())]
    r = upload_client.post("/api/upload/LangBad?lang=de", files=files)
    assert r.status_code == 422


def test_ingest_course_engine_switch_busts_cache(monkeypatch, tmp_path, fake_embed_fn):
    """If a course is ingested with pymupdf then re-ingested with mineru,
    the per-file hash cache must NOT short-circuit — the new engine must
    actually run on the unchanged files. (Switching engines is exactly the
    case where you want a re-extract even though file content is identical.)"""
    import importlib
    art = tmp_path / "artifacts"
    (art / "courses").mkdir(parents=True)
    monkeypatch.setattr("nano_notebooklm.config.ARTIFACTS_DIR", art)
    from nano_notebooklm.kb import store as kb_store
    importlib.reload(kb_store)
    monkeypatch.setattr(kb_store, "_get_default_embed_fn", lambda: fake_embed_fn)

    src = tmp_path / "src"
    src.mkdir()
    (src / "doc.md").write_text(
        "# Sample\n\nLorem ipsum dolor sit amet consectetur adipiscing elit. " * 8,
        encoding="utf-8",
    )

    kb = kb_store.KBStore()
    course_id = "EngineSwitchCourse"
    kb.ingest_course(str(src), course_id, engine="pymupdf")
    marker = art / "courses" / course_id / ".extract_engine"
    assert marker.read_text().strip() == "pymupdf"

    # Spy on extract_file to confirm the second call actually re-ran.
    from nano_notebooklm.ingest import extractors as ext_mod
    call_count = {"n": 0}
    real_extract_file = ext_mod.extract_file

    def _spy(*args, **kwargs):
        call_count["n"] += 1
        # Force engine='pymupdf' for the spied call so we don't need mineru
        # installed; what we're testing is that the cache was busted at all.
        kwargs["engine"] = "pymupdf"
        return real_extract_file(*args, **kwargs)

    monkeypatch.setattr(kb_store, "extract_file", _spy)
    kb2 = kb_store.KBStore()
    kb2.ingest_course(str(src), course_id, engine="mineru")
    assert call_count["n"] >= 1, "engine switch should re-run extract_file"
    assert marker.read_text().strip() == "mineru"


def test_extract_from_chunks_signature_accepts_progress_callback():
    """The kwarg must remain backwards-compatible (default=None)."""
    import inspect
    from nano_notebooklm.kg.extractor import extract_from_chunks
    sig = inspect.signature(extract_from_chunks)
    assert "progress_callback" in sig.parameters
    assert sig.parameters["progress_callback"].default is None
