"""Regression tests for review-swarm v3 round 1 → fix-all v4 hardening.

Each test pins exactly one fix from `STATUS.md` v4 so a future refactor
that reverts the fix lights up CI immediately.
"""

from __future__ import annotations

import importlib
import io
import json
import re
import textwrap
import zipfile
from pathlib import Path

import pytest

from nano_notebooklm.types import Chunk, FileType, LLMResponse


# ── #A1 + #A4: explain-node + search_kb + generate_note 锁 lock_course_id ──


@pytest.mark.asyncio
async def test_search_kb_lock_course_id_rejects_mismatched_course(monkeypatch, tmp_path):
    """A locked search_kb must refuse a query that targets a different course."""
    from nano_notebooklm.orchestrator.tools.search_kb import build_search_kb

    class _StubKB:
        def search(self, query, top_k, course_id):
            raise AssertionError("kb.search must not be called when locked")

    class _StubOrch:
        def list_courses(self):
            return ["A", "B"]

    tool = build_search_kb(_StubKB(), _StubOrch(), lock_course_id="A")
    out = await tool.handler({"query": "x", "course_id": "B"})
    assert out == {"error": "cross_course_denied", "active_course": "A", "requested_course": "B"}


@pytest.mark.asyncio
async def test_search_kb_lock_course_id_forces_active_when_omitted(monkeypatch):
    """When the LLM omits course_id but the request is locked, force the lock value."""
    from nano_notebooklm.orchestrator.tools.search_kb import build_search_kb

    captured = {}

    class _StubKB:
        def search(self, query, top_k, course_id):
            captured["course_id"] = course_id
            return []

    class _StubOrch:
        def list_courses(self):
            return ["A"]

    tool = build_search_kb(_StubKB(), _StubOrch(), lock_course_id="A")
    out = await tool.handler({"query": "x"})
    assert out == []
    assert captured["course_id"] == "A"


@pytest.mark.asyncio
async def test_generate_note_lock_course_id_rejects_mismatched_course():
    from nano_notebooklm.orchestrator.tools.generate_note import build_generate_note

    class _Orch:
        def list_courses(self):
            return ["A", "B"]

        async def run_skill(self, name, params):
            raise AssertionError("run_skill must not be reached")

    tool = build_generate_note(_Orch(), lock_course_id="A")
    out = await tool.handler({"course_id": "B"})
    assert out == {"error": "cross_course_denied", "active_course": "A", "requested_course": "B"}


def test_explain_node_registry_passes_course_id_to_read_chunk_lock(monkeypatch):
    """Smoke-check #A1: _build_explain_node_registry must build a read_chunk
    whose closure refuses cross-course access."""
    import api.server as server_mod

    reg = server_mod._build_explain_node_registry(course_id="LockedCourse")
    read_chunk_tool = reg.get("read_chunk")
    assert read_chunk_tool is not None
    # Source-level pin: the handler closure must reference lock_course_id.
    src = read_chunk_tool.handler.__code__.co_freevars
    assert "lock_course_id" in src, (
        f"read_chunk handler missing lock_course_id closure (got {src})"
    )


# ── #A2: PUT /api/memory size + recursion guard ───────────────────────────


@pytest.fixture
def memory_client(monkeypatch, tmp_path, fake_embed_fn):
    art = tmp_path / "artifacts"
    (art / "courses").mkdir(parents=True)
    monkeypatch.setenv("ARTIFACTS_DIR", str(art))
    from nano_notebooklm import config
    monkeypatch.setattr(config, "ARTIFACTS_DIR", art)
    from nano_notebooklm.kb import store as kb_store
    monkeypatch.setattr(kb_store, "_get_default_embed_fn", lambda: fake_embed_fn)
    import api.server as server_mod
    importlib.reload(server_mod)
    from fastapi.testclient import TestClient
    return TestClient(server_mod.app)


def test_memory_put_rejects_oversized_payload(memory_client):
    """A2: 200KB cap also applies to PUT (the v3 fix only put it on POST)."""
    big = "x" * 250_000
    r = memory_client.put("/api/memory", json={"learning_goals": big})
    assert r.status_code == 413
    body = r.json()
    assert "200KB" in body.get("detail", "")


def test_validate_memory_payload_catches_recursion_error(monkeypatch):
    """B4: directly exercise the RecursionError catch in
    `_validate_memory_payload` — going through the HTTP path is brittle
    because TestClient's own json.dumps trips on deep dicts before the
    request even leaves the client."""
    import api.server as server_mod

    def boom(*a, **kw):
        raise RecursionError("too deep")

    monkeypatch.setattr(server_mod.json, "dumps", boom)
    with pytest.raises(server_mod.HTTPException) as ei:
        server_mod._validate_memory_payload({"any": "value"})
    assert ei.value.status_code == 400
    assert "deeply" in str(ei.value.detail).lower()


def test_validate_memory_payload_rejects_oversize(monkeypatch):
    """A2: 200KB cap path — pin the byte count + 413 status."""
    import api.server as server_mod
    big = "x" * 250_000
    with pytest.raises(server_mod.HTTPException) as ei:
        server_mod._validate_memory_payload({"learning_goals": big})
    assert ei.value.status_code == 413


def test_memory_update_validator_recursion_caught(monkeypatch):
    """B4 sibling: MemoryUpdate.value validator catches RecursionError too."""
    import api.server as server_mod

    real_dumps = server_mod.json.dumps
    call_count = {"n": 0}

    def boom(*a, **kw):
        # Only the validator's first dumps call should trigger; subsequent
        # calls from elsewhere (logging, etc.) use real_dumps.
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RecursionError("too deep")
        return real_dumps(*a, **kw)

    monkeypatch.setattr(server_mod.json, "dumps", boom)
    from pydantic import ValidationError
    with pytest.raises(ValidationError) as ei:
        server_mod.MemoryUpdate(key="k", value={"a": "b"})
    assert "deeply" in str(ei.value).lower()


# ── #A3: stream errors no longer leak vendor message ─────────────────────


def test_real_stream_error_event_carries_stable_code(monkeypatch, tmp_path, fake_embed_fn):
    """When the upstream stream raises with a juicy error, the NDJSON `error`
    event must carry `stream_failed`, NOT the raw exception string."""
    art = tmp_path / "artifacts"
    (art / "courses" / "X").mkdir(parents=True)
    (art / "courses" / "X" / "chunks.json").write_text(
        json.dumps([Chunk(chunk_id="x1", doc_id="d", course_id="X",
                          text="hello",
                          file_type=FileType.MARKDOWN, source_file="a.md",
                          location="").model_dump()],
                   default=str)
    )
    monkeypatch.setattr("nano_notebooklm.config.ARTIFACTS_DIR", art)
    from nano_notebooklm.kb import store as kb_store
    monkeypatch.setattr(kb_store, "_get_default_embed_fn", lambda: fake_embed_fn)
    import api.server as server_mod
    importlib.reload(server_mod)
    server_mod.kb.build_index(None)

    async def boom(*a, **kw):
        raise RuntimeError("AuthenticationError https://codex.ysaikeji.cn/v1 sk-secretkey1234567890")
        yield  # pragma: no cover

    monkeypatch.setattr(server_mod.router, "complete_stream", boom)

    from fastapi.testclient import TestClient
    client = TestClient(server_mod.app)
    with client.stream("POST", "/api/notes/stream",
                       json={"course_id": "X"}) as resp:
        body = "".join(chunk for chunk in resp.iter_text())
    events = [json.loads(line) for line in body.splitlines() if line.strip()]
    err = next((e for e in events if e.get("type") == "error"), None)
    assert err is not None
    assert err["error"] == "stream_failed"
    assert "sk-" not in json.dumps(err)
    assert "ysaikeji" not in json.dumps(err)


# ── #A5: requestNodeDeepDive parser has a buffer cap ─────────────────────


def test_requestNodeDeepDive_parser_has_buffer_cap():
    """White-box grep: `MAX_LINE_BYTES` must be present in study-state.js."""
    src = Path("frontend/study-state.js").read_text(encoding="utf-8")
    # Locate the function definition and ensure MAX_LINE_BYTES is inside it.
    m = re.search(r"function requestNodeDeepDive[\s\S]+?\n  \}\s*\n", src)
    assert m, "requestNodeDeepDive function not found"
    body = m.group(0)
    assert "MAX_LINE_BYTES" in body
    assert "buf = \"\"" in body  # the drop-on-overflow path resets buffer


# ── #A6 + #A7: upload + ingest off-load via to_thread ────────────────────


def test_upload_handler_uses_asyncio_to_thread():
    """Grep pin: upload_files writes via asyncio.to_thread, not sync I/O."""
    src = Path("api/server.py").read_text(encoding="utf-8")
    upload_block = src[src.index("async def upload_files"):src.index("async def upload_files") + 4000]
    assert "asyncio.to_thread" in upload_block, "upload no longer offloads I/O"
    assert "kb.ingest_course" not in upload_block.split("await _asyncio.to_thread")[0], (
        "ingest_course should be inside an asyncio.to_thread offload"
    )


# ── #A8: cached mindmap GET no longer holds the edit lock ─────────────────


def test_cached_mindmap_get_serves_outside_edit_lock():
    src = Path("api/server.py").read_text(encoding="utf-8")
    # locate get_mindmap body
    m = re.search(r"async def get_mindmap.+?(?=\n@app|\nasync def )", src, re.DOTALL)
    assert m, "get_mindmap not found"
    body = m.group(0)
    # Find the `async with _edit_lock_for(course_id):` line index relative to
    # the first `if kg_path.exists():` — the first existence check must come
    # BEFORE the `async with` line for the cached fast path to bypass the lock.
    first_exists = body.index("if kg_path.exists():")
    lock_pos = body.index("async with _edit_lock_for(course_id):")
    assert first_exists < lock_pos, (
        "cached path must precede the per-course generation lock (#A8)"
    )


# ── #B11: scrub patterns ──────────────────────────────────────────────────


def test_scrub_redacts_aws_access_key():
    from nano_notebooklm.orchestrator.agent_tools import _scrub
    assert "AKIAIOSFODNN7EXAMPLE" not in _scrub("creds AKIAIOSFODNN7EXAMPLE")
    assert "[aws-access-key]" in _scrub("creds AKIAIOSFODNN7EXAMPLE")


def test_scrub_redacts_github_pat():
    from nano_notebooklm.orchestrator.agent_tools import _scrub
    assert "ghp_" not in _scrub("token ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    assert "[github-token]" in _scrub("token ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")


def test_scrub_redacts_jwt():
    from nano_notebooklm.orchestrator.agent_tools import _scrub
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.dummy_signature_here"
    out = _scrub(f"auth {jwt}")
    assert "eyJ" not in out
    assert "[jwt]" in out


def test_scrub_redacts_private_key_block():
    from nano_notebooklm.orchestrator.agent_tools import _scrub
    block = (
        "intro -----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEAabcdef\n"
        "-----END RSA PRIVATE KEY----- tail"
    )
    out = _scrub(block)
    assert "BEGIN RSA PRIVATE KEY" not in out
    assert "[private-key]" in out


def test_scrub_redacts_authorization_header():
    from nano_notebooklm.orchestrator.agent_tools import _scrub
    out = _scrub("got header Authorization: Token=abc123secret")
    assert "abc123secret" not in out


# ── #H1 / #B11: `..` rejection on every body endpoint ────────────────────


@pytest.fixture
def secure_client(monkeypatch, tmp_path, fake_embed_fn):
    """Minimal client for parametrised endpoint reach tests."""
    art = tmp_path / "artifacts"
    (art / "courses" / "X").mkdir(parents=True)
    (art / "courses" / "X" / "chunks.json").write_text("[]")
    monkeypatch.setattr("nano_notebooklm.config.ARTIFACTS_DIR", art)
    from nano_notebooklm.kb import store as kb_store
    monkeypatch.setattr(kb_store, "_get_default_embed_fn", lambda: fake_embed_fn)
    import api.server as server_mod
    importlib.reload(server_mod)
    from fastapi.testclient import TestClient
    return TestClient(server_mod.app)


@pytest.mark.parametrize("path,body", [
    ("/api/notes",          {"course_id": ".."}),
    ("/api/quiz",           {"course_id": ".."}),
    ("/api/report",         {"course_id": ".."}),
    ("/api/agent/stream",   {"question": "hi", "course_id": ".."}),
    ("/api/exam-analysis",  {"course_id": ".."}),
    ("/api/ingest",         {"course_dir": "/tmp/whatever", "course_id": ".."}),
])
def test_dotdot_course_id_rejected_on_every_body_endpoint(secure_client, path, body):
    r = secure_client.post(path, json=body)
    assert r.status_code == 422, f"{path} should reject `..` got {r.status_code}"


# ── #B2: extra=forbid on the two new request models ─────────────────────


def test_mindmap_edit_request_rejects_extra_field(secure_client):
    r = secure_client.post(
        "/api/mindmap/CS231N/edit",
        json={"ops": [{"op": "delete_node", "id": "n1"}], "future_field": 1},
    )
    assert r.status_code == 422


def test_node_explain_request_rejects_extra_field(secure_client):
    r = secure_client.post(
        "/api/mindmap/CS231N/explain-node",
        json={"node_id": "n1", "rogue_field": True},
    )
    assert r.status_code == 422


# ── #H3 / #B1 coverage: _check_zip_safety three rejection paths ──────────


def _build_pptx_with_entries(n: int, body_size: int = 16) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for i in range(n):
            z.writestr(f"entry_{i}.bin", b"x" * body_size)
    return buf.getvalue()


def test_check_zip_safety_rejects_too_many_entries(tmp_path):
    import api.server as server_mod
    p = tmp_path / "bomb.pptx"
    p.write_bytes(_build_pptx_with_entries(server_mod.ZIP_MAX_ENTRIES + 1))
    with pytest.raises(Exception) as ei:
        server_mod._check_zip_safety(p, p.stat().st_size)
    assert getattr(ei.value, "status_code", None) == 413
    assert not p.exists()  # the rejected file is unlinked


def test_check_zip_safety_rejects_oversize_uncompressed(tmp_path, monkeypatch):
    import api.server as server_mod
    monkeypatch.setattr(server_mod, "ZIP_MAX_UNCOMPRESSED_BYTES", 1024)
    monkeypatch.setattr(server_mod, "ZIP_MAX_RATIO", 10_000)  # disable ratio check
    p = tmp_path / "big.pptx"
    p.write_bytes(_build_pptx_with_entries(20, body_size=200))  # 20 * 200 = 4000 > 1024
    with pytest.raises(Exception) as ei:
        server_mod._check_zip_safety(p, p.stat().st_size)
    assert getattr(ei.value, "status_code", None) == 413


def test_check_zip_safety_rejects_high_ratio(tmp_path, monkeypatch):
    import api.server as server_mod
    monkeypatch.setattr(server_mod, "ZIP_MAX_RATIO", 2)  # demand >2× to flip
    monkeypatch.setattr(server_mod, "ZIP_MAX_UNCOMPRESSED_BYTES", 10_000_000)
    # 10 entries of 1KB highly-compressible content → uncompressed 10KB,
    # compressed ~tens of bytes → ratio >> 2.
    p = tmp_path / "ratio.pptx"
    p.write_bytes(_build_pptx_with_entries(10, body_size=1024))
    with pytest.raises(Exception) as ei:
        server_mod._check_zip_safety(p, p.stat().st_size)
    assert getattr(ei.value, "status_code", None) == 413


def test_check_zip_safety_rejects_invalid_zip(tmp_path):
    import api.server as server_mod
    p = tmp_path / "garbage.pptx"
    p.write_bytes(b"not a zip at all")
    with pytest.raises(Exception) as ei:
        server_mod._check_zip_safety(p, p.stat().st_size)
    assert getattr(ei.value, "status_code", None) == 400


# ── #C5 + #M8 frontend NDJSON parser cap (white-box grep) ─────────────────


def test_api_js_stream_parser_has_try_catch_and_cap():
    src = Path("frontend/api.js").read_text(encoding="utf-8")
    m = re.search(r"async function _stream\([\s\S]+?\n\}\n", src)
    assert m, "_stream function not found"
    body = m.group(0)
    assert "MAX_LINE_BYTES" in body
    assert "try { event = JSON.parse(line); }" in body or "JSON.parse(line)" in body
    assert "catch" in body  # at least one catch arm


# ── #C3 + #C4 markdownToHtml escapes before regex (grep) ─────────────────


def test_markdownToHtml_escapes_before_regex():
    src = Path("frontend/app.jsx").read_text(encoding="utf-8")
    m = re.search(r"function markdownToHtml\([\s\S]+?\n\}\n", src)
    assert m, "markdownToHtml function not found"
    body = m.group(0)
    # The escapeHtmlSafe call must precede the markdown regex pipeline.
    escape_pos = body.index("escapeHtmlSafe(stash.text)")
    bold_pos = body.index('"<strong>$1</strong>"')
    assert escape_pos < bold_pos, "escapeHtmlSafe must run before markdown regex"
    # Citation chip's inner is escaped.
    assert "escapeHtmlSafe(inner)" in body


# ── #B5 cancel watcher pool cap (grep) ────────────────────────────────────


def test_agent_loop_cancel_watcher_uses_bounded_semaphore():
    src = Path("nano_notebooklm/orchestrator/agent_loop.py").read_text(encoding="utf-8")
    assert "_CANCEL_WATCHER_LIMIT" in src
    assert "BoundedSemaphore" in src
    assert "_CANCEL_WATCHER_LIMIT.acquire(blocking=False)" in src


def test_openai_backend_cancel_watcher_uses_bounded_semaphore():
    src = Path("nano_notebooklm/ai/openai_backend.py").read_text(encoding="utf-8")
    assert "_CANCEL_WATCHER_LIMIT" in src
    assert "BoundedSemaphore" in src


# ── #B3 ingest fallback validates cid ─────────────────────────────────────


def test_findTextRangeInRoot_injects_phantom_block_separator():
    """Highlights spanning a heading + paragraph used to fail to re-apply
    because the walker concatenated text-node contents without the block
    separator that `sel.toString()` produces. Pin the phantom-newline
    injection so a future refactor can't regress. The walker logic was
    extracted into `getBlockAwareDomText` so it can be shared with the
    selection-capture and prune paths — the pin moved with it."""
    src = Path("frontend/app.jsx").read_text(encoding="utf-8")
    m = re.search(r"function getBlockAwareDomText\([\s\S]+?\n\}\n", src)
    assert m, "getBlockAwareDomText not found"
    body = m.group(0)
    assert "h1,h2,h3" in body, "block-element selector list missing"
    assert 'combined += "\\n\\n"' in body, "phantom newline injection missing"
    # findTextRangeInRoot must still route through the helper, otherwise
    # the original highlight Range-resolution loses block-aware text.
    range_fn = re.search(r"function findTextRangeInRoot\([\s\S]+?\n\}\n", src)
    assert range_fn and "getBlockAwareDomText(root)" in range_fn.group(0), \
        "findTextRangeInRoot no longer routes through getBlockAwareDomText"


def test_nav_epoch_threaded_into_pdf_frame_and_text_body():
    """R5-2 + R5-2 fix-all v1: clicking the same citation twice in the
    Reader used to no-op because `setActivePage(N)` with N unchanged is a
    React state no-op → iframe.src and scrollIntoView never re-fire.

    The fix bumps `navEpoch` on every `dispatchNavToReader` and the value
    threads down to:
      - DocumentTextBody's useEffect deps so scrollIntoView re-fires
      - DocumentPdfFrame's useEffect so iframe re-navigates (hash-first,
        src fallback after fix-all v1 M3)

    Pin all three so a future refactor that drops the nonce can't silently
    regress the re-click guarantee.
    """
    app_src = Path("frontend/app.jsx").read_text(encoding="utf-8")
    reader_src = Path("frontend/reader.jsx").read_text(encoding="utf-8")
    # 1. dispatchNavToReader must bump the epoch.
    assert "setNavEpoch(e => e + 1)" in app_src, \
        "dispatchNavToReader no longer bumps navEpoch — repeat citation clicks will silently no-op"
    # 2. <Reader> must receive the prop.
    assert "navEpoch={navEpoch}" in app_src, \
        "<Reader> no longer receives navEpoch prop"
    # 3. DocumentTextBody must include navEpoch in its scroll-effect deps.
    text_body_match = re.search(
        r"function DocumentTextBody\([\s\S]+?\}\, \[activePage, doc\.doc_id, navEpoch\]",
        reader_src,
    )
    assert text_body_match, \
        "DocumentTextBody scrollIntoView useEffect deps no longer include navEpoch"
    # 4. DocumentPdfFrame must include navEpoch in its iframe-nav effect deps.
    pdf_frame_match = re.search(
        r"function DocumentPdfFrame\([\s\S]+?\}\, \[navEpoch, url\]",
        reader_src,
    )
    assert pdf_frame_match, \
        "DocumentPdfFrame iframe-nav useEffect deps no longer include navEpoch"


def test_correct_letter_lives_in_study_state_not_app_jsx():
    """fix-all v1 H5: correctLetter must live in study-state.js (not as a
    local duplicate in app.jsx) so RealQuizView's render-time letter
    extraction and study-state's Wrong-Only filter share one source of
    truth. Pre-fix only the render path had it; the filter compared
    user-picked 'B' to 'B. full text' → all-correct quizzes showed every
    answered question as wrong."""
    study_src = Path("frontend/study-state.js").read_text(encoding="utf-8")
    app_src = Path("frontend/app.jsx").read_text(encoding="utf-8")
    # Helper definition lives in study-state.js
    assert "function correctLetter(q)" in study_src, \
        "correctLetter helper missing from study-state.js"
    # study-state.js exports it
    assert "correctLetter," in study_src or "correctLetter\n" in study_src, \
        "correctLetter not in study-state.js exports"
    # app.jsx no longer defines its own copy — it aliases StudyState's.
    assert "const correctLetter = StudyState.correctLetter" in app_src, \
        "app.jsx no longer routes through StudyState.correctLetter"
    # And no local function definition of correctLetter survives.
    local_def = re.search(r"^function correctLetter\(", app_src, re.MULTILINE)
    assert not local_def, \
        "app.jsx still has a local correctLetter function — Wrong-Only filter will drift again"


def test_ingest_validates_fallback_cid(secure_client, tmp_path):
    """When course_id is omitted and course_dir basename violates the
    pattern, /api/ingest must 400 — not write into artifacts/courses/<bad>/."""
    bad = tmp_path / "<bad>"
    bad.mkdir()
    r = secure_client.post("/api/ingest", json={"course_dir": str(bad)})
    # Either 400 (ingest_dir whitelist denies before cid validation) or
    # 400 (cid validator rejects). Anything in [400, 403] is acceptable;
    # what matters is NOT 200 / NOT 500.
    assert r.status_code in (400, 403, 422), r.text
