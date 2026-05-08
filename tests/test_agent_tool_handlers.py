"""Unit tests for the four real tool handlers.

These exercise the *production* handlers (search_kb, read_chunk,
list_courses, generate_note) — not the synthetic _ok_tool fakes used in
test_agent_loop. Catches regressions in argument parsing, course_id
whitelisting, top_k clamping, chunk lookup, and skill-failure passthrough.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from nano_notebooklm.kb.store import KBStore
from nano_notebooklm.orchestrator.tools.generate_note import build_generate_note
from nano_notebooklm.orchestrator.tools.list_courses import build_list_courses
from nano_notebooklm.orchestrator.tools.read_chunk import (
    MAX_CHUNK_TEXT_BYTES,
    build_read_chunk,
)
from nano_notebooklm.orchestrator.tools.search_kb import build_search_kb
from nano_notebooklm.types import SkillResult


@pytest.fixture
def seeded_kb(tmp_path, sample_chunks, fake_embed_fn) -> KBStore:
    """Build a KBStore on disk with sample_chunks indexed under 'testcourse'."""
    art = tmp_path / "artifacts"
    (art / "courses" / "testcourse").mkdir(parents=True)
    (art / "courses" / "testcourse" / "chunks.json").write_text(
        json.dumps([c.model_dump() for c in sample_chunks], default=str)
    )
    kb = KBStore(artifacts_dir=art, embed_fn=fake_embed_fn)
    kb.build_index("testcourse")
    return kb


@pytest.fixture
def fake_orchestrator(seeded_kb):
    """An orchestrator stub with list_courses() and run_skill()."""
    class _Orch:
        def list_courses(self):
            return ["testcourse"]

        async def run_skill(self, name, params):
            self.last_call = (name, params)
            return SkillResult(success=True, output_path="/tmp/note.md",
                               data={"format": params.get("format"),
                                     "topic": params.get("topic"),
                                     "sources_used": 5})
    return _Orch()


# ── search_kb ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_kb_returns_hits(seeded_kb, fake_orchestrator):
    tool = build_search_kb(seeded_kb, fake_orchestrator)
    result = await tool.handler({"query": "backpropagation gradient",
                                 "course_id": "testcourse", "top_k": 3})
    assert isinstance(result, list)
    assert len(result) <= 3
    assert all("chunk_id" in r and "score" in r for r in result)


@pytest.mark.asyncio
async def test_search_kb_blank_query_returns_error(seeded_kb, fake_orchestrator):
    tool = build_search_kb(seeded_kb, fake_orchestrator)
    result = await tool.handler({"query": "   "})
    assert isinstance(result, dict) and "error" in result


@pytest.mark.asyncio
async def test_search_kb_top_k_clamped_to_max(seeded_kb, fake_orchestrator):
    """top_k=99 must be clamped to 10 (the new schema cap)."""
    tool = build_search_kb(seeded_kb, fake_orchestrator)
    result = await tool.handler({"query": "neural", "top_k": 99})
    # 6 sample chunks total → at most 6 returned
    assert len(result) <= 10


@pytest.mark.asyncio
async def test_search_kb_rejects_unknown_course_id(seeded_kb, fake_orchestrator):
    tool = build_search_kb(seeded_kb, fake_orchestrator)
    result = await tool.handler({"query": "anything", "course_id": "ghostcourse"})
    assert "error" in result and "ghostcourse" in result["error"]


@pytest.mark.asyncio
async def test_search_kb_rejects_path_traversal_course_id(seeded_kb, fake_orchestrator):
    tool = build_search_kb(seeded_kb, fake_orchestrator)
    for poison in ("../etc", "..", "foo/bar", "foo\\bar", "foo\x00bar"):
        result = await tool.handler({"query": "x", "course_id": poison})
        assert "error" in result, f"should reject {poison!r}"


@pytest.mark.asyncio
async def test_search_kb_top_k_garbage_falls_back_to_default(seeded_kb, fake_orchestrator):
    tool = build_search_kb(seeded_kb, fake_orchestrator)
    result = await tool.handler({"query": "neural", "top_k": "not-a-number"})
    assert isinstance(result, list)


# ── read_chunk ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_chunk_finds_existing(seeded_kb):
    tool = build_read_chunk(seeded_kb)
    result = await tool.handler({"chunk_id": "c1"})
    assert result["chunk_id"] == "c1"
    assert "Backpropagation" in result["text"]
    assert result["truncated"] is False


@pytest.mark.asyncio
async def test_read_chunk_missing_returns_not_found(seeded_kb):
    tool = build_read_chunk(seeded_kb)
    result = await tool.handler({"chunk_id": "does-not-exist"})
    assert result["error"] == "not_found"
    assert result["chunk_id"] == "does-not-exist"


@pytest.mark.asyncio
async def test_read_chunk_blank_id_returns_error(seeded_kb):
    tool = build_read_chunk(seeded_kb)
    result = await tool.handler({"chunk_id": "   "})
    assert "error" in result


@pytest.mark.asyncio
async def test_read_chunk_truncates_oversized_text(seeded_kb, sample_chunks):
    """Inject an oversized chunk and confirm the handler truncates with a
    `(truncated, N more)` marker rather than blasting the model with MBs."""
    big_chunk = sample_chunks[0].model_copy(update={
        "chunk_id": "big",
        "text": "x" * (MAX_CHUNK_TEXT_BYTES + 5000),
    })
    seeded_kb._all_chunks = list(seeded_kb._all_chunks) + [big_chunk]
    seeded_kb._chunk_index = None  # force lazy rebuild

    tool = build_read_chunk(seeded_kb)
    result = await tool.handler({"chunk_id": "big"})
    assert result["truncated"] is True
    assert len(result["text"]) <= MAX_CHUNK_TEXT_BYTES + 200  # marker has wiggle
    assert "truncated" in result["text"]


# ── list_courses ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_courses_returns_known_ids(seeded_kb, fake_orchestrator):
    tool = build_list_courses(fake_orchestrator, seeded_kb)
    result = await tool.handler({})
    assert isinstance(result, list)
    ids = [r["course_id"] for r in result]
    assert "testcourse" in ids
    assert all("chunks" in r for r in result)


# ── generate_note ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_note_happy_path(fake_orchestrator):
    tool = build_generate_note(fake_orchestrator)
    result = await tool.handler({"course_id": "testcourse", "topic": "rrf"})
    assert "error" not in result
    assert result["output_path"] == "/tmp/note.md"
    name, params = fake_orchestrator.last_call
    assert name == "note_generator"
    assert params["course_id"] == "testcourse"
    assert params["topic"] == "rrf"


@pytest.mark.asyncio
async def test_generate_note_rejects_unknown_course(fake_orchestrator):
    tool = build_generate_note(fake_orchestrator)
    result = await tool.handler({"course_id": "doesnotexist"})
    assert "error" in result and "doesnotexist" in result["error"]


@pytest.mark.asyncio
async def test_generate_note_rejects_path_traversal(fake_orchestrator):
    tool = build_generate_note(fake_orchestrator)
    for poison in ("../etc", "../../tmp", "foo/bar", "foo\\bar", ".."):
        result = await tool.handler({"course_id": poison})
        assert "error" in result, f"should reject {poison!r}"


@pytest.mark.asyncio
async def test_generate_note_blank_course_id_required():
    class _Orch:
        def list_courses(self):
            return []

    tool = build_generate_note(_Orch())
    result = await tool.handler({})
    assert result["error"] == "course_id is required"


@pytest.mark.asyncio
async def test_generate_note_skill_failure_passes_through():
    class _Orch:
        def list_courses(self):
            return ["testcourse"]
        async def run_skill(self, name, params):
            return SkillResult(success=False, error="kb empty")

    tool = build_generate_note(_Orch())
    result = await tool.handler({"course_id": "testcourse"})
    assert result["error"] == "kb empty"


# ── KBStore.find_chunk regression test ─────────────────────────────────


def test_kbstore_find_chunk_uses_lazy_dict(seeded_kb):
    """First call builds the dict; subsequent calls hit it directly."""
    assert seeded_kb._chunk_index is None
    chunk = seeded_kb.find_chunk("c1")
    assert chunk is not None and chunk.chunk_id == "c1"
    assert seeded_kb._chunk_index is not None
    # Second call doesn't rebuild
    cached_id = id(seeded_kb._chunk_index)
    seeded_kb.find_chunk("c2")
    assert id(seeded_kb._chunk_index) == cached_id


def test_kbstore_find_chunk_returns_none_for_missing(seeded_kb):
    assert seeded_kb.find_chunk("nope") is None


def test_kbstore_find_chunk_invalidated_on_rebuild(seeded_kb):
    seeded_kb.find_chunk("c1")  # populate
    assert seeded_kb._chunk_index is not None
    seeded_kb.build_index("testcourse")
    assert seeded_kb._chunk_index is None
