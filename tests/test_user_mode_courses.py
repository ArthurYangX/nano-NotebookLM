"""Round 4 #R4-1: /api/courses?mode=user|all + frontend empty-state contract."""

from __future__ import annotations

import importlib
import json
import re
from pathlib import Path

import pytest


@pytest.fixture
def courses_client(monkeypatch, tmp_path, fake_embed_fn):
    """Pre-seed two preset courses + one user-uploaded course, build server."""
    art = tmp_path / "artifacts"
    courses = art / "courses"
    courses.mkdir(parents=True)

    from nano_notebooklm.types import Chunk, FileType

    def _seed(cid: str, doc_id: str = "d1"):
        d = courses / cid
        d.mkdir(parents=True, exist_ok=True)
        chunks = [
            Chunk(
                chunk_id=f"{cid}-c1",
                doc_id=doc_id,
                course_id=cid,
                text="hello world",
                file_type=FileType.MARKDOWN,
                source_file="x.md",
                location="",
            ).model_dump()
        ]
        (d / "chunks.json").write_text(json.dumps(chunks, default=str))

    # 2 preset + 1 user-uploaded (use one preset id from PRESET_COURSE_IDS to
    # exercise the filter; pick "CS231N" + "15-213").
    _seed("CS231N")
    _seed("15-213")
    _seed("MyUploadedCourse")

    monkeypatch.setattr("nano_notebooklm.config.ARTIFACTS_DIR", art)
    from nano_notebooklm.kb import store as kb_store
    monkeypatch.setattr(kb_store, "_get_default_embed_fn", lambda: fake_embed_fn)

    import api.server as server_mod
    importlib.reload(server_mod)
    server_mod.kb.build_index(None)

    from fastapi.testclient import TestClient
    return TestClient(server_mod.app)


# ── mini ──────────────────────────────────────────────────────────────


def test_courses_endpoint_user_mode_excludes_presets(courses_client):
    """Default (no mode arg) hides 8 hardcoded preset courses, leaves uploads."""
    r = courses_client.get("/api/courses")
    assert r.status_code == 200
    ids = {c["id"] for c in r.json()["courses"]}
    assert "CS231N" not in ids
    assert "15-213" not in ids
    assert "MyUploadedCourse" in ids


def test_courses_endpoint_user_mode_explicit(courses_client):
    """Explicit mode=user has the same effect as the default."""
    r = courses_client.get("/api/courses?mode=user")
    assert r.status_code == 200
    ids = {c["id"] for c in r.json()["courses"]}
    assert "CS231N" not in ids
    assert "MyUploadedCourse" in ids


def test_app_jsx_empty_state_grep():
    """Frontend renders an upload CTA when courses.length === 0."""
    src = Path("frontend/app.jsx").read_text(encoding="utf-8")
    # The empty-state branch must reference an empty courses list AND the
    # canonical CTA copy / classname so a careless refactor that removes
    # the branch trips this test.
    assert 'data-testid="empty-courses"' in src
    assert "courses.length === 0" in src
    assert "上传第一个文档" in src


def test_api_js_get_courses_passes_mode():
    """frontend/api.js getCourses(mode) must thread mode into the URL."""
    src = Path("frontend/api.js").read_text(encoding="utf-8")
    m = re.search(r"async getCourses\([^)]*\)\s*\{[\s\S]+?\n\s{2}\},", src)
    assert m, "getCourses function not found"
    body = m.group(0)
    assert "mode" in body
    assert "?mode=" in body


# ── corner ────────────────────────────────────────────────────────────


def test_courses_endpoint_mode_all_includes_presets(courses_client):
    """mode=all is the rollback hatch — preset courses must reappear."""
    r = courses_client.get("/api/courses?mode=all")
    assert r.status_code == 200
    ids = {c["id"] for c in r.json()["courses"]}
    assert "CS231N" in ids
    assert "15-213" in ids
    assert "MyUploadedCourse" in ids


def test_courses_endpoint_invalid_mode_returns_422(courses_client):
    """mode=garbage must 422 (Pydantic Literal rejection)."""
    r = courses_client.get("/api/courses?mode=garbage")
    assert r.status_code == 422


def test_preset_course_ids_constant_shape():
    """PRESET_COURSE_IDS must be a frozenset of the canonical 8 ids — defends
    against accidental mutation that would let preset courses leak through."""
    from nano_notebooklm import config
    assert isinstance(config.PRESET_COURSE_IDS, frozenset)
    assert config.PRESET_COURSE_IDS == frozenset({
        "15-213", "CS182", "CS231N", "CS285", "CSE 234",
        "机器人导论", "计算机组成原理", "模式识别",
    })


def test_app_jsx_show_preset_url_param_grep():
    """The ?show_preset=1 escape hatch must remain in app.jsx so users can
    roll back to seeing the 8 preset courses if R4-4 needs debugging."""
    src = Path("frontend/app.jsx").read_text(encoding="utf-8")
    assert "show_preset" in src
    assert 'URLSearchParams' in src or 'searchParams' in src
