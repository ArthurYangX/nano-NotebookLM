"""Tests for the incremental per-file note cache.

User requirement (2026-05-11): a course with 10 PDFs already generated should
not re-run the LLM for those 10 when an 11th PDF is added. Only the new file
gets a fresh LLM call; cached files load from per_file_cache.json. The
merge/review pass always runs to fold the new file into existing cross-refs.

Coverage:
  - chunk_hash determinism + sensitivity
  - load/save round-trip via JSON
  - write_cache_entry atomic update of one entry
  - prune_stale_cache drops entries for removed files
  - plan_for_course populates cached_content on hit
  - plan_for_course leaves cached_content=None on hash mismatch
  - plan_for_course force_refresh=True bypasses cache
  - Endpoint: cache hit → file_cached event, no LLM call to that plan
  - Endpoint: force=true → ignores cache, all plans go to LLM
  - Endpoint: fresh worker writes cache entry after success
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest
from fastapi.testclient import TestClient

from nano_notebooklm.types import Chunk, FileType


# ── Helpers ──────────────────────────────────────────────────────────


def _mk_chunk(idx: int, source_file: str, text: str = "body") -> Chunk:
    return Chunk(
        chunk_id=f"c{idx}",
        doc_id=source_file.replace(".", "_"),
        course_id="testcourse",
        text=text,
        file_type=FileType.PDF,
        source_file=source_file,
        location=f"Page {idx}/10",
        page=idx,
    )


@pytest.fixture
def isolated_course(tmp_path, monkeypatch):
    """Point ARTIFACTS_DIR at a clean tmp dir for cache isolation."""
    art = tmp_path / "artifacts"
    (art / "courses" / "testcourse" / "notes").mkdir(parents=True)
    monkeypatch.setenv("ARTIFACTS_DIR", str(art))
    from nano_notebooklm import config
    monkeypatch.setattr(config, "ARTIFACTS_DIR", art)
    return art


# ── Hash determinism + sensitivity ──────────────────────────────────


def test_chunk_hash_stable_across_calls():
    from nano_notebooklm.skills import notes_full_course as nfc
    chunks = [_mk_chunk(1, "a.pdf"), _mk_chunk(2, "a.pdf")]
    h1 = nfc.chunk_hash(chunks)
    h2 = nfc.chunk_hash(chunks)
    assert h1 == h2
    assert len(h1) == 64  # SHA256 hex


def test_chunk_hash_changes_on_text_edit():
    from nano_notebooklm.skills import notes_full_course as nfc
    a = [_mk_chunk(1, "a.pdf", text="original")]
    b = [_mk_chunk(1, "a.pdf", text="edited")]
    assert nfc.chunk_hash(a) != nfc.chunk_hash(b)


def test_chunk_hash_changes_on_chunk_id_renumber():
    """Re-ingest may issue new chunk_ids even for the same text."""
    from nano_notebooklm.skills import notes_full_course as nfc
    a = [_mk_chunk(1, "a.pdf", text="x")]
    b = [_mk_chunk(99, "a.pdf", text="x")]
    assert nfc.chunk_hash(a) != nfc.chunk_hash(b)


def test_chunk_hash_independent_of_filename():
    """Hash is over chunk content, not the file path. Same chunks moved
    to a different source_file still hash the same."""
    from nano_notebooklm.skills import notes_full_course as nfc
    a = [_mk_chunk(1, "a.pdf", text="x")]
    b = [_mk_chunk(1, "b.pdf", text="x")]
    assert nfc.chunk_hash(a) == nfc.chunk_hash(b)


# ── Load/save round-trip ─────────────────────────────────────────────


def test_load_cache_missing_returns_empty(isolated_course):
    from nano_notebooklm.skills import notes_full_course as nfc
    assert nfc.load_cache("testcourse") == {}


def test_load_cache_corrupt_returns_empty(isolated_course):
    from nano_notebooklm.skills import notes_full_course as nfc
    p = isolated_course / "courses" / "testcourse" / "notes" / "per_file_cache.json"
    p.write_text("this is not json {{{")
    assert nfc.load_cache("testcourse") == {}


def test_save_cache_round_trip(isolated_course):
    from nano_notebooklm.skills import notes_full_course as nfc
    cache = {
        "a.pdf": {"chunk_hash": "abc", "content": "\\section{A}", "generated_at": "t", "model": "m"},
    }
    nfc.save_cache("testcourse", cache)
    assert nfc.load_cache("testcourse") == cache


def test_save_cache_atomic_temp_rename(isolated_course, monkeypatch):
    """Verify save_cache writes to a .tmp then rename, not directly. The
    temp file should not survive a successful save."""
    from nano_notebooklm.skills import notes_full_course as nfc
    nfc.save_cache("testcourse", {"a.pdf": {"chunk_hash": "x", "content": "y",
                                              "generated_at": "t", "model": "m"}})
    notes_dir = isolated_course / "courses" / "testcourse" / "notes"
    assert (notes_dir / "per_file_cache.json").exists()
    # No leftover .tmp
    assert not (notes_dir / "per_file_cache.json.tmp").exists()


def test_write_cache_entry_updates_one_key(isolated_course):
    from nano_notebooklm.skills import notes_full_course as nfc
    nfc.save_cache("testcourse", {
        "a.pdf": {"chunk_hash": "h1", "content": "A", "generated_at": "t", "model": "m"},
        "b.pdf": {"chunk_hash": "h2", "content": "B", "generated_at": "t", "model": "m"},
    })
    asyncio.run(nfc.write_cache_entry(
        "testcourse", "a.pdf",
        chunk_hash_value="h1-new", content="A2",
    ))
    cache = nfc.load_cache("testcourse")
    assert cache["a.pdf"]["chunk_hash"] == "h1-new"
    assert cache["a.pdf"]["content"] == "A2"
    # b.pdf preserved unchanged
    assert cache["b.pdf"]["chunk_hash"] == "h2"
    assert cache["b.pdf"]["content"] == "B"


def test_prune_stale_cache_drops_missing(isolated_course):
    from nano_notebooklm.skills import notes_full_course as nfc
    nfc.save_cache("testcourse", {
        "kept.pdf": {"chunk_hash": "h", "content": "K", "generated_at": "t", "model": ""},
        "deleted.pdf": {"chunk_hash": "h", "content": "D", "generated_at": "t", "model": ""},
    })
    removed = nfc.prune_stale_cache("testcourse", {"kept.pdf"})
    assert removed == 1
    cache = nfc.load_cache("testcourse")
    assert "kept.pdf" in cache
    assert "deleted.pdf" not in cache


def test_prune_stale_cache_noop_when_all_active(isolated_course):
    from nano_notebooklm.skills import notes_full_course as nfc
    nfc.save_cache("testcourse", {"a.pdf": {"chunk_hash": "h", "content": "A",
                                             "generated_at": "t", "model": ""}})
    assert nfc.prune_stale_cache("testcourse", {"a.pdf", "b.pdf"}) == 0


def test_cache_path_refuses_escape_attempt(isolated_course):
    """course_id with .. should NOT resolve outside the artifacts root —
    load returns {} (silent reject) and save is a no-op."""
    from nano_notebooklm.skills import notes_full_course as nfc
    # Both should fail gracefully (return {} / no-op) rather than throw
    # arbitrary paths into the filesystem.
    assert nfc.load_cache("../escape") == {}
    nfc.save_cache("../escape", {"x.pdf": {"chunk_hash": "h", "content": "X",
                                            "generated_at": "t", "model": ""}})
    # No file leaked outside artifacts
    leaked = list((isolated_course.parent).glob("**/per_file_cache.json"))
    assert all(str(p).startswith(str(isolated_course)) for p in leaked)


# ── plan_for_course cache behavior ───────────────────────────────────


class _FakeKB:
    def __init__(self, chunks):
        self._chunks = chunks

    def get_chunks(self, course_id):
        return self._chunks


def test_plan_for_course_cache_hit_fills_cached_content(isolated_course):
    """A pre-populated cache with matching chunk_hash short-circuits
    the LLM — plan.cached_content is non-None."""
    from nano_notebooklm.skills import notes_full_course as nfc

    chunks = [_mk_chunk(1, "a.pdf"), _mk_chunk(2, "a.pdf")]
    kb = _FakeKB(chunks)
    capped_hash = nfc.chunk_hash(chunks)
    nfc.save_cache("testcourse", {
        "a.pdf": {
            "chunk_hash": capped_hash,
            "content": "\\section{a.pdf}\nCACHED BODY",
            "generated_at": "2026-05-11T00:00:00+00:00",
            "model": "gpt-5.4",
            "prompt_version": nfc._NOTE_PROMPT_VERSION,
        },
    })
    plans = nfc.plan_for_course(kb, "testcourse")
    assert len(plans) == 1
    assert plans[0].cached_content == "\\section{a.pdf}\nCACHED BODY"
    assert plans[0].cache_key == capped_hash


def test_plan_for_course_hash_mismatch_misses(isolated_course):
    """Cache entry with a wrong hash is ignored — cached_content stays None,
    so the endpoint will run a fresh LLM call."""
    from nano_notebooklm.skills import notes_full_course as nfc

    chunks = [_mk_chunk(1, "a.pdf")]
    kb = _FakeKB(chunks)
    nfc.save_cache("testcourse", {
        "a.pdf": {
            "chunk_hash": "stale-hash",
            "content": "STALE BODY",
            "generated_at": "2026-05-10T00:00:00+00:00",
            "model": "",
        },
    })
    plans = nfc.plan_for_course(kb, "testcourse")
    assert len(plans) == 1
    assert plans[0].cached_content is None


def test_plan_for_course_force_refresh_ignores_cache(isolated_course):
    from nano_notebooklm.skills import notes_full_course as nfc

    chunks = [_mk_chunk(1, "a.pdf")]
    kb = _FakeKB(chunks)
    capped_hash = nfc.chunk_hash(chunks)
    nfc.save_cache("testcourse", {
        "a.pdf": {
            "chunk_hash": capped_hash,
            "content": "CACHED BODY",
            "generated_at": "2026-05-11T00:00:00+00:00",
            "model": "",
            "prompt_version": nfc._NOTE_PROMPT_VERSION,
        },
    })
    plans = nfc.plan_for_course(kb, "testcourse", force_refresh=True)
    assert len(plans) == 1
    assert plans[0].cached_content is None  # forced bypass


def test_plan_for_course_partial_hit_new_file(isolated_course):
    """The headline user requirement: 10 files cached, 11th file added —
    10 plans return with cached_content, 1 plan needs a fresh LLM call."""
    from nano_notebooklm.skills import notes_full_course as nfc

    chunks = []
    for i in range(1, 11):
        chunks.append(_mk_chunk(i, f"file_{i:02d}.pdf"))
    # 11th file added later — no cache entry for it
    chunks.append(_mk_chunk(99, "file_11.pdf"))
    kb = _FakeKB(chunks)

    # Pre-populate cache for the first 10
    cache = {}
    for i in range(1, 11):
        key = nfc.chunk_hash([c for c in chunks if c.source_file == f"file_{i:02d}.pdf"])
        cache[f"file_{i:02d}.pdf"] = {
            "chunk_hash": key,
            "content": f"\\section{{file_{i:02d}.pdf}}\nbody {i}",
            "generated_at": "2026-05-11T00:00:00+00:00",
            "model": "",
            "prompt_version": nfc._NOTE_PROMPT_VERSION,
        }
    nfc.save_cache("testcourse", cache)

    plans = nfc.plan_for_course(kb, "testcourse")
    assert len(plans) == 11
    cached_ones = [p for p in plans if p.cached_content is not None]
    fresh_ones = [p for p in plans if p.cached_content is None]
    assert len(cached_ones) == 10
    assert len(fresh_ones) == 1
    assert fresh_ones[0].source_file == "file_11.pdf"


# ── Endpoint behavior ───────────────────────────────────────────────


@pytest.fixture
def endpoint_client(monkeypatch, tmp_path, sample_chunks, fake_embed_fn):
    art = tmp_path / "artifacts"
    (art / "courses" / "testcourse").mkdir(parents=True)
    (art / "courses" / "testcourse" / "notes").mkdir(parents=True)
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
    return TestClient(server_mod.app), server_mod, art


def _read_events(response):
    return [json.loads(line) for line in response.iter_lines() if line]


def test_endpoint_emits_file_cached_for_hit(endpoint_client, monkeypatch):
    """Pre-populate cache with hashes matching the seeded chunks; endpoint
    emits file_cached events for each, NO file_start/file_done, NO LLM
    call. The review pass still runs."""
    client, server_mod, art = endpoint_client
    from nano_notebooklm.skills import notes_full_course as nfc

    # Build cache for every (file, capped chunks) combination
    chunks = server_mod.kb.get_chunks("testcourse")
    groups = nfc._group_chunks_by_file(chunks)
    cache = {}
    for source_file, file_chunks in groups.items():
        capped = file_chunks[:nfc.MAX_CHUNKS_PER_FILE]
        cache[source_file] = {
            "chunk_hash": nfc.chunk_hash(capped),
            "content": f"\\section{{{source_file}}}\nCACHED",
            "generated_at": "2026-05-11T00:00:00+00:00",
            "model": "",
            "prompt_version": nfc._NOTE_PROMPT_VERSION,
        }
    nfc.save_cache("testcourse", cache)

    # Track LLM calls — should be ZERO per-file calls; only the review pass.
    per_file_called = []

    async def fake_complete(*args, **kwargs):  # pragma: no cover — unreached
        per_file_called.append(args)
        raise AssertionError("per-file LLM should be skipped on cache hit")

    async def fake_complete_stream(*args, **kwargs):
        # Review pass — yield a small reviewed body
        for delta in ("\\section", "{Reviewed}\n", "polished"):
            yield delta

    monkeypatch.setattr(server_mod.router, "complete",
                        AsyncMock(side_effect=fake_complete))
    monkeypatch.setattr(server_mod.router, "complete_stream", fake_complete_stream)

    resp = client.post("/api/notes/full-course/stream", json={"course_id": "testcourse"})
    assert resp.status_code == 200
    events = _read_events(resp)

    plan = next(e for e in events if e["type"] == "plan")
    assert plan["cached_count"] == plan["total"]
    assert plan["fresh_count"] == 0
    cached_events = [e for e in events if e["type"] == "file_cached"]
    assert len(cached_events) == plan["total"]
    assert all("content" in e and e["content"] for e in cached_events)
    # No fresh worker events
    assert not any(e["type"] == "file_start" for e in events)
    assert not any(e["type"] == "file_done" for e in events)
    # Review pass ran and we got a done
    assert any(e["type"] == "reviewing" for e in events)
    assert events[-1]["type"] == "done"
    assert per_file_called == []  # zero per-file LLM calls


def test_endpoint_force_true_bypasses_cache(endpoint_client, monkeypatch):
    """force=true ignores the populated cache and runs every per-file LLM."""
    client, server_mod, art = endpoint_client
    from nano_notebooklm.skills import notes_full_course as nfc
    from nano_notebooklm.types import LLMResponse as RouterResponse

    # Pre-populate with matching hashes — cache WOULD hit if not for force.
    chunks = server_mod.kb.get_chunks("testcourse")
    groups = nfc._group_chunks_by_file(chunks)
    cache = {}
    for source_file, file_chunks in groups.items():
        capped = file_chunks[:nfc.MAX_CHUNKS_PER_FILE]
        cache[source_file] = {
            "chunk_hash": nfc.chunk_hash(capped),
            "content": "STALE",
            "generated_at": "2026-05-11T00:00:00+00:00",
            "model": "",
        }
    nfc.save_cache("testcourse", cache)

    call_count = {"n": 0}

    async def fake_complete(prompt, *args, **kwargs):
        call_count["n"] += 1
        return RouterResponse(content="\\section{fresh}\nFRESH BODY", model="test")

    async def fake_complete_stream(*args, **kwargs):
        for delta in ("\\section{Reviewed}\n",):
            yield delta

    monkeypatch.setattr(server_mod.router, "complete",
                        AsyncMock(side_effect=fake_complete))
    monkeypatch.setattr(server_mod.router, "complete_stream", fake_complete_stream)

    resp = client.post("/api/notes/full-course/stream",
                       json={"course_id": "testcourse", "force": True})
    assert resp.status_code == 200
    events = _read_events(resp)

    plan = next(e for e in events if e["type"] == "plan")
    assert plan["force"] is True
    assert plan["cached_count"] == 0
    assert plan["fresh_count"] == plan["total"]
    cached_events = [e for e in events if e["type"] == "file_cached"]
    assert cached_events == []
    file_done_events = [e for e in events if e["type"] == "file_done"]
    assert len(file_done_events) == plan["total"]
    assert call_count["n"] == plan["total"]


def test_endpoint_fresh_worker_writes_cache(endpoint_client, monkeypatch):
    """After a successful per-file LLM call, the result is persisted to
    per_file_cache.json with the plan's cache_key."""
    client, server_mod, art = endpoint_client
    from nano_notebooklm.skills import notes_full_course as nfc
    from nano_notebooklm.types import LLMResponse as RouterResponse

    async def fake_complete(prompt, *args, **kwargs):
        # Return a deterministic body so we can assert it landed in cache.
        return RouterResponse(content="\\section{Generated}\nGEN BODY", model="test-model")

    async def fake_complete_stream(*args, **kwargs):
        for delta in ("\\section{Reviewed}\n",):
            yield delta

    monkeypatch.setattr(server_mod.router, "complete",
                        AsyncMock(side_effect=fake_complete))
    monkeypatch.setattr(server_mod.router, "complete_stream", fake_complete_stream)

    resp = client.post("/api/notes/full-course/stream", json={"course_id": "testcourse"})
    assert resp.status_code == 200
    events = _read_events(resp)
    assert events[-1]["type"] == "done"

    # Cache should now have one entry per source_file
    cache = nfc.load_cache("testcourse")
    assert len(cache) > 0
    for source_file, entry in cache.items():
        assert entry["content"].startswith("\\section{Generated}")
        assert entry["chunk_hash"]
        assert entry["generated_at"]


def test_endpoint_prunes_stale_cache_on_request(endpoint_client, monkeypatch):
    """A cache entry for a source_file no longer in the course is dropped
    before generation."""
    client, server_mod, art = endpoint_client
    from nano_notebooklm.skills import notes_full_course as nfc
    from nano_notebooklm.types import LLMResponse as RouterResponse

    nfc.save_cache("testcourse", {
        "ghost-file.pdf": {
            "chunk_hash": "g",
            "content": "GHOST",
            "generated_at": "2026-05-11T00:00:00+00:00",
            "model": "",
        },
    })

    async def fake_complete(*args, **kwargs):
        return RouterResponse(content="\\section{X}\nbody", model="m")

    async def fake_complete_stream(*args, **kwargs):
        for delta in ("done",):
            yield delta

    monkeypatch.setattr(server_mod.router, "complete",
                        AsyncMock(side_effect=fake_complete))
    monkeypatch.setattr(server_mod.router, "complete_stream", fake_complete_stream)

    resp = client.post("/api/notes/full-course/stream", json={"course_id": "testcourse"})
    assert resp.status_code == 200
    _read_events(resp)

    cache = nfc.load_cache("testcourse")
    assert "ghost-file.pdf" not in cache


# ── Cache hardening v1 (2026-05-11) ──────────────────────────────────


def test_plan_for_course_prompt_version_mismatch_misses(isolated_course):
    """An entry whose chunk_hash matches but whose prompt_version is stale
    (e.g. the team edited NOTE_FORMAT_LATEX after the entry was written)
    must be treated as a cache miss. The on-disk body was produced under
    an outdated rubric and needs to regenerate."""
    from nano_notebooklm.skills import notes_full_course as nfc

    chunks = [_mk_chunk(1, "a.pdf")]
    kb = _FakeKB(chunks)
    capped_hash = nfc.chunk_hash(chunks)
    nfc.save_cache("testcourse", {
        "a.pdf": {
            "chunk_hash": capped_hash,
            "content": "\\section{a.pdf}\nOLD BODY",
            "generated_at": "2026-05-10T00:00:00+00:00",
            "model": "gpt-5.4",
            # Deliberately a different version string from current
            "prompt_version": "deadbeef",
        },
    })
    plans = nfc.plan_for_course(kb, "testcourse")
    assert len(plans) == 1
    assert plans[0].cached_content is None


def test_plan_for_course_rejects_unsafe_cached_content(isolated_course, caplog):
    """Defense in depth: even if the on-disk cache file was tampered with
    to inject `\\input{/etc/passwd}`, the read-time sanitizer rejects it
    and plan_for_course treats the entry as a miss, logging at WARNING."""
    from nano_notebooklm.skills import notes_full_course as nfc

    chunks = [_mk_chunk(1, "a.pdf")]
    kb = _FakeKB(chunks)
    capped_hash = nfc.chunk_hash(chunks)
    nfc.save_cache("testcourse", {
        "a.pdf": {
            "chunk_hash": capped_hash,
            # Looks legitimate, but contains a forbidden command. The
            # sanitizer's pattern catches `\input` followed by a non-letter.
            "content": "\\section{a.pdf}\nbody \\input{/etc/passwd} more",
            "generated_at": "2026-05-11T00:00:00+00:00",
            "model": "gpt-5.4",
            "prompt_version": nfc._NOTE_PROMPT_VERSION,
        },
    })
    with caplog.at_level(logging.WARNING, logger="nano_notebooklm.skills.notes_full_course"):
        plans = nfc.plan_for_course(kb, "testcourse")
    assert len(plans) == 1
    assert plans[0].cached_content is None
    # The rejection produced a WARNING log mentioning the file
    assert any(
        "unsafe LaTeX" in rec.getMessage() and "a.pdf" in rec.getMessage()
        for rec in caplog.records
    ), [rec.getMessage() for rec in caplog.records]


def test_save_cache_writes_v1_envelope(isolated_course):
    """On-disk JSON is wrapped in {"version": 1, "entries": {...},
    "prompt_version": "..."}. Old direct {source_file: entry} shape is
    not produced by save_cache anymore (load_cache still accepts it for
    legacy reads — covered by test_load_cache_accepts_legacy_v0_dict)."""
    from nano_notebooklm.skills import notes_full_course as nfc

    nfc.save_cache("testcourse", {
        "a.pdf": {"chunk_hash": "h", "content": "x", "generated_at": "t", "model": ""},
    })
    p = isolated_course / "courses" / "testcourse" / "notes" / "per_file_cache.json"
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    assert raw.get("version") == 1
    assert raw.get("prompt_version") == nfc._NOTE_PROMPT_VERSION
    assert isinstance(raw.get("entries"), dict)
    assert "a.pdf" in raw["entries"]


def test_load_cache_accepts_legacy_v0_dict(isolated_course):
    """Pre-create a bare-dict JSON file on disk (the v0 shape that shipped
    before this hardening). load_cache should treat it as entries — no
    operator action required to migrate."""
    from nano_notebooklm.skills import notes_full_course as nfc

    p = isolated_course / "courses" / "testcourse" / "notes" / "per_file_cache.json"
    # v0 legacy: bare dict mapping source_file → entry, no version envelope
    legacy = {
        "old.pdf": {
            "chunk_hash": "h",
            "content": "OLD",
            "generated_at": "2026-05-10T00:00:00+00:00",
            "model": "",
        },
    }
    p.write_text(json.dumps(legacy), encoding="utf-8")
    loaded = nfc.load_cache("testcourse")
    assert loaded == legacy


def test_concurrent_writes_to_same_course_preserve_all_entries(isolated_course):
    """N parallel write_cache_entry calls for N different source_files —
    the per-course asyncio.Lock guarantees no entry is lost. Without the
    lock the read→mutate→save sequence races and last-writer-wins drops
    siblings; with it, the final cache has all N entries.

    8 writers is enough to make the race surface reliably on Python's
    cooperative scheduler — each writer awaits load_cache (file I/O)
    which deterministically yields control to the others.
    """
    from nano_notebooklm.skills import notes_full_course as nfc

    n = 8

    async def run():
        await asyncio.gather(*[
            nfc.write_cache_entry(
                "testcourse",
                f"file_{i:02d}.pdf",
                chunk_hash_value=f"hash-{i}",
                content=f"\\section{{file_{i:02d}.pdf}}\nbody {i}",
                model="test",
            )
            for i in range(n)
        ])

    asyncio.run(run())
    cache = nfc.load_cache("testcourse")
    assert len(cache) == n, f"expected {n} entries, got {len(cache)}: {sorted(cache)}"
    for i in range(n):
        key = f"file_{i:02d}.pdf"
        assert key in cache
        assert cache[key]["chunk_hash"] == f"hash-{i}"
        assert f"body {i}" in cache[key]["content"]
        # Each entry stamped with the current prompt_version
        assert cache[key]["prompt_version"] == nfc._NOTE_PROMPT_VERSION
