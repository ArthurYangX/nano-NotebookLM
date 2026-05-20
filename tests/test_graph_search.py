"""R4-4 GraphRAG retriever — mini + corner tests.

All tests offline:
  - KG / chunks.json synthesised in `tmp_path` via `isolated_artifacts`
  - embedding via a hand-rolled keyword embedder (one-hot per keyword) so
    cosine ranking is fully deterministic and independent of `fake_embed_fn`
  - LLM via the same `stub` pattern as tests/test_user_lang.py
"""

from __future__ import annotations

import importlib
import inspect
import json
import re
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent


# ── Keyword embedder ─────────────────────────────────────────────────
# One-hot 4-d embedding keyed on whichever keyword appears first in the
# text. Independent of conftest's hash embedder so we can predict exactly
# which concept node a query lands on.
_KEYWORDS = ["zebra", "quokka", "aardvark", "penguin"]


def _keyword_embed(texts: list[str]) -> np.ndarray:
    out = np.zeros((len(texts), len(_KEYWORDS)), dtype=np.float32)
    for i, t in enumerate(texts):
        low = (t or "").lower()
        for j, kw in enumerate(_KEYWORDS):
            if kw in low:
                out[i, j] = 1.0
    norms = np.linalg.norm(out, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return out / norms


# ── KG / chunks fixtures ─────────────────────────────────────────────


def _make_node(nid: str, name: str, definition: str,
               concept_type: str = "definition", depth: int = 2,
               weight: float = 1.0, chunk_id: str | None = None,
               source_chunks: list[dict] | None = None,
               concept_embedding: list[float] | None = None) -> dict:
    if source_chunks is None:
        if chunk_id is None:
            source_chunks = []
        else:
            source_chunks = [{
                "chunk_id": chunk_id, "source_file": "x.md",
                "location": "p1", "page": 1,
            }]
    node = {
        "id": nid, "name": name, "definition": definition,
        "concept_type": concept_type, "course_ids": [],
        "chunk_ids": [], "depth": depth, "weight": weight,
        "source_chunks": source_chunks, "parent_topic": None,
        "learning_order": None,
    }
    if concept_embedding is not None:
        node["concept_embedding"] = concept_embedding
    return node


def _make_chunk_row(cid: str, text: str, course_id: str = "cT",
                    source_file: str = "x.md",
                    location: str = "p1", page: int = 1) -> dict:
    return {
        "chunk_id": cid, "doc_id": "d1", "course_id": course_id,
        "text": text, "file_type": "pdf",
        "source_file": source_file, "location": location, "page": page,
    }


def _seed_course(art: Path, course_id: str, nodes: list[dict],
                 edges: list[dict], chunks: list[dict]) -> None:
    cd = art / "courses" / course_id
    cd.mkdir(parents=True, exist_ok=True)
    (cd / "knowledge_graph.json").write_text(
        json.dumps({"nodes": nodes, "edges": edges,
                    "course_name": course_id}, ensure_ascii=False)
    )
    (cd / "chunks.json").write_text(
        json.dumps(chunks, ensure_ascii=False, default=str)
    )


# ── Mini 1: BFS pulls neighbour chunks, ignores isolated ─────────────


def test_graph_search_returns_chunks_from_neighbor_nodes(isolated_artifacts):
    """Seed n_a (zebra). 2-hop BFS along part-of → n_b, depends-on → n_c
    visits A/B/C; n_d (penguin, isolated) must NOT appear."""
    from nano_notebooklm.kb.graph_search import graph_search

    art = isolated_artifacts
    nodes = [
        _make_node("n_a", "Alpha", "zebra zebra zebra",
                   concept_type="topic", depth=1, weight=5.0, chunk_id="ca"),
        _make_node("n_b", "Beta", "quokka quokka",
                   chunk_id="cb", weight=3.0),
        _make_node("n_c", "Gamma", "aardvark aardvark",
                   chunk_id="cc", weight=2.0),
        _make_node("n_d", "Delta", "penguin penguin",
                   chunk_id="cd", weight=1.0),
    ]
    edges = [
        {"source": "n_a", "target": "n_b", "relation_type": "part-of"},
        {"source": "n_b", "target": "n_c", "relation_type": "depends-on"},
        # n_d intentionally not connected
    ]
    chunks = [
        _make_chunk_row("ca", "alpha text"),
        _make_chunk_row("cb", "beta text"),
        _make_chunk_row("cc", "gamma text"),
        _make_chunk_row("cd", "delta text"),
    ]
    _seed_course(art, "cT", nodes, edges, chunks)

    results = graph_search(
        "zebra concept", "cT", _keyword_embed,
        artifacts_dir=art, top_k_concepts=1, hop_limit=2, max_chunks=10,
    )

    cids = {r.chunk_id for r in results}
    assert "ca" in cids
    assert "cb" in cids
    assert "cc" in cids
    assert "cd" not in cids
    assert all(r.course_id == "cT" for r in results)


# ── Mini 2: /api/chat surfaces path="graphrag" when KG present ───────


@pytest.fixture
def graphrag_client(monkeypatch, tmp_path):
    """Build a /api/chat stack with a course that has a KG + chunks."""
    art = tmp_path / "artifacts"
    courses = art / "courses"
    courses.mkdir(parents=True)

    nodes = [
        _make_node("n_a", "Alpha", "zebra zebra zebra",
                   concept_type="topic", depth=1, weight=5.0, chunk_id="ca"),
        _make_node("n_b", "Beta", "zebra topic supporting",
                   chunk_id="cb", weight=3.0),
    ]
    edges = [{"source": "n_a", "target": "n_b", "relation_type": "part-of"}]
    chunks = [
        _make_chunk_row("ca", "Alpha is the first concept about zebras."),
        _make_chunk_row("cb", "Beta builds on Alpha and adds more zebra context."),
    ]
    _seed_course(art, "cT", nodes, edges, chunks)

    monkeypatch.setenv("ARTIFACTS_DIR", str(art))
    from nano_notebooklm import config
    monkeypatch.setattr(config, "ARTIFACTS_DIR", art)
    from nano_notebooklm.kb import store as kb_store
    monkeypatch.setattr(kb_store, "_get_default_embed_fn",
                        lambda: _keyword_embed)
    monkeypatch.setenv("RAG_SCORE_GATE_TOP1", "0.0")

    import api.server as server_mod
    importlib.reload(server_mod)
    server_mod.kb.build_index(None)

    from nano_notebooklm.orchestrator import router_intent as ri
    ri._LANG_CACHE.clear()

    from nano_notebooklm.types import LLMResponse

    async def stub(prompt, task_type="", system="", temperature=0.7,
                   max_tokens=4096, max_retries=3, **kwargs):
        return LLMResponse(content="graph-rooted answer", model="fake",
                           input_tokens=1, output_tokens=1, latency_ms=1.0)

    monkeypatch.setattr(server_mod.router, "complete", stub)

    return TestClient(server_mod.app), server_mod, art


def test_chat_uses_graphrag_path_when_kg_present(graphrag_client):
    client, _, _ = graphrag_client
    r = client.post("/api/chat", json={
        "question": "zebra ecosystem analysis",
        "course_id": "cT",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("path") == "graphrag", body
    # graph-search joins source_chunks from BFS; we expect ≥2 sources.
    assert len(body.get("sources", [])) >= 2


def test_chat_all_courses_mode_runs_graphrag_across_courses(monkeypatch, tmp_path):
    """2026-05-12 All Courses graphrag: when course_id is null (All
    Courses mode), `_maybe_graphrag_all_courses` iterates every course
    with a `knowledge_graph.json`, runs `_maybe_graphrag` in parallel,
    merges by chunk_id, and surfaces `path=graphrag`. Pre-fix, All
    Courses mode skipped graphrag entirely and short queries that
    relied on KG semantic matching fell through to general."""
    import importlib
    from fastapi.testclient import TestClient
    from nano_notebooklm.types import LLMResponse

    art = tmp_path / "artifacts"
    courses = art / "courses"
    courses.mkdir(parents=True)

    # Seed TWO courses, each with its own KG. Both have a chunk that
    # matches the query — All Courses graphrag should merge both.
    nodes_a = [
        _make_node("a_n", "AlphaTopic", "zebra zebra zebra",
                   concept_type="topic", depth=1, weight=5.0, chunk_id="ca1"),
    ]
    edges_a: list = []
    chunks_a = [_make_chunk_row("ca1", "Alpha course mentions zebra anatomy.",
                                course_id="cA", source_file="alpha.pdf")]
    _seed_course(art, "cA", nodes_a, edges_a, chunks_a)

    nodes_b = [
        _make_node("b_n", "BetaTopic", "zebra topic supporting",
                   concept_type="topic", depth=1, weight=5.0, chunk_id="cb1"),
    ]
    edges_b: list = []
    chunks_b = [_make_chunk_row("cb1", "Beta course also covers zebra behaviour.",
                                course_id="cB", source_file="beta.pdf")]
    _seed_course(art, "cB", nodes_b, edges_b, chunks_b)

    monkeypatch.setenv("ARTIFACTS_DIR", str(art))
    from nano_notebooklm import config
    monkeypatch.setattr(config, "ARTIFACTS_DIR", art)
    from nano_notebooklm.kb import store as kb_store
    monkeypatch.setattr(kb_store, "_get_default_embed_fn", lambda: _keyword_embed)
    monkeypatch.setenv("RAG_SCORE_GATE_TOP1", "0.0")
    monkeypatch.setenv("GRAPHRAG_SCORE_GATE_TOP1", "0.0")
    monkeypatch.setenv("NANO_NLM_DISABLE_EMBED_WARMUP", "1")

    import api.server as server_mod
    importlib.reload(server_mod)
    server_mod.kb.build_index(None)
    from nano_notebooklm.orchestrator import router_intent as ri
    ri._LANG_CACHE.clear()

    async def stub_complete(prompt, **kw):
        return LLMResponse(content="graph-rooted answer", model="fake",
                           input_tokens=1, output_tokens=1, latency_ms=1.0)

    monkeypatch.setattr(server_mod.router, "complete", stub_complete)

    client = TestClient(server_mod.app)
    r = client.post("/api/chat", json={"question": "zebra question"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("path") == "graphrag", (
        f"All Courses mode should route to graphrag when ≥1 KG matches: {body}"
    )
    # Merged result should pull chunks from BOTH courses (not just one);
    # the per-course source_file names are distinct so we can verify the
    # fan-out via that field.
    sources = body.get("sources", [])
    source_files = {s.get("source_file") for s in sources}
    assert {"alpha.pdf", "beta.pdf"}.issubset(source_files), (
        f"expected hits from both courses (alpha.pdf + beta.pdf), got source_files={source_files}"
    )


def test_chat_all_courses_skips_graphrag_when_no_kg_files(monkeypatch, tmp_path):
    """When NO course has a KG, `_maybe_graphrag_all_courses` returns
    `[]` and the chain falls through to plain RAG / general."""
    import importlib
    from fastapi.testclient import TestClient
    from nano_notebooklm.types import LLMResponse

    art = tmp_path / "artifacts"
    courses = art / "courses"
    courses.mkdir(parents=True)
    # Seed one course with chunks but NO knowledge_graph.json.
    (courses / "cNoKG").mkdir()
    chunks = [_make_chunk_row("cnokg_1", "Plain RAG content about zebras.",
                              course_id="cNoKG", source_file="x.pdf")]
    (courses / "cNoKG" / "chunks.json").write_text(
        json.dumps(chunks, ensure_ascii=False, default=str)
    )

    monkeypatch.setenv("ARTIFACTS_DIR", str(art))
    from nano_notebooklm import config
    monkeypatch.setattr(config, "ARTIFACTS_DIR", art)
    from nano_notebooklm.kb import store as kb_store
    monkeypatch.setattr(kb_store, "_get_default_embed_fn", lambda: _keyword_embed)
    monkeypatch.setenv("RAG_SCORE_GATE_TOP1", "0.0")
    monkeypatch.setenv("NANO_NLM_DISABLE_EMBED_WARMUP", "1")

    import api.server as server_mod
    importlib.reload(server_mod)
    server_mod.kb.build_index(None)
    from nano_notebooklm.orchestrator import router_intent as ri
    ri._LANG_CACHE.clear()

    async def stub_complete(prompt, **kw):
        return LLMResponse(content="rag answer", model="fake",
                           input_tokens=1, output_tokens=1, latency_ms=1.0)
    monkeypatch.setattr(server_mod.router, "complete", stub_complete)

    client = TestClient(server_mod.app)
    r = client.post("/api/chat", json={"question": "zebra question"})
    assert r.status_code == 200
    body = r.json()
    # No KG anywhere → graphrag fan-out returns [] → fall through to rag.
    assert body.get("path") != "graphrag", body


# ── Corner 1: KG file missing → graph_search returns [] ──────────────


def test_graph_search_falls_back_to_rag_when_kg_missing(isolated_artifacts):
    from nano_notebooklm.kb.graph_search import graph_search

    # Course directory exists but no knowledge_graph.json file.
    (isolated_artifacts / "courses" / "cT").mkdir(parents=True)
    results = graph_search("anything", "cT", _keyword_embed,
                           artifacts_dir=isolated_artifacts)
    assert results == []


# ── Corner 2: graph_search 0 hits → empty (qa_skill falls through) ───


def test_graph_search_zero_hits_falls_back_to_rag(isolated_artifacts):
    """KG with nodes that have NO source_chunks. graph_search ranks them by
    cosine but ends up with no chunks to materialise → returns []."""
    from nano_notebooklm.kb.graph_search import graph_search

    art = isolated_artifacts
    nodes = [
        _make_node("n_a", "Alpha", "zebra zebra",
                   source_chunks=[]),  # no source_chunks
    ]
    edges: list[dict] = []
    chunks: list[dict] = []
    _seed_course(art, "cT", nodes, edges, chunks)

    results = graph_search("zebra", "cT", _keyword_embed, artifacts_dir=art)
    assert results == []


# ── Corner 3: hop_limit=2 caps chunks at max_chunks ──────────────────


def test_graph_search_hop_limit_2_caps_chunks_at_30(isolated_artifacts):
    """A hub node with 50 source_chunks must be truncated to max_chunks=30."""
    from nano_notebooklm.kb.graph_search import graph_search

    art = isolated_artifacts
    big_source_chunks = [
        {"chunk_id": f"c{i:03d}", "source_file": "x.md",
         "location": f"p{i}", "page": i}
        for i in range(50)
    ]
    nodes = [
        _make_node("n_hub", "Alpha", "zebra zebra zebra",
                   weight=10.0, source_chunks=big_source_chunks),
    ]
    chunks = [_make_chunk_row(f"c{i:03d}", f"chunk {i}") for i in range(50)]
    _seed_course(art, "cT", nodes, [], chunks)

    results = graph_search("zebra", "cT", _keyword_embed,
                           artifacts_dir=art, max_chunks=30)
    assert len(results) == 30


# ── Corner 4: lazy embed when concept_embedding missing + dim mismatch ──


def test_concept_embedding_lazy_when_missing(isolated_artifacts):
    """Two scenarios both rely on lazy recompute:
      (a) node without concept_embedding field at all → recompute
      (b) node with cached embedding of wrong dim (e.g. 384 vs 4) → recompute
    Either way the result set must contain the matched node's chunk.
    """
    from nano_notebooklm.kb.graph_search import graph_search

    art = isolated_artifacts
    nodes = [
        # (a) no concept_embedding field
        _make_node("n_a", "Alpha", "zebra zebra", chunk_id="ca"),
        # (b) stale 384-d cached embedding
        _make_node("n_b", "Beta", "quokka quokka", chunk_id="cb",
                   concept_embedding=[0.01] * 384),
    ]
    chunks = [
        _make_chunk_row("ca", "alpha chunk text"),
        _make_chunk_row("cb", "beta chunk text"),
    ]
    _seed_course(art, "cT", nodes, [], chunks)

    # Query zebra → should hit n_a (lazy recompute) but not n_b.
    r_zebra = graph_search("zebra", "cT", _keyword_embed,
                           artifacts_dir=art, top_k_concepts=1)
    assert {r.chunk_id for r in r_zebra} >= {"ca"}

    # Query quokka → should hit n_b (lazy recompute despite stale 384-d cache).
    r_quokka = graph_search("quokka", "cT", _keyword_embed,
                            artifacts_dir=art, top_k_concepts=1)
    assert {r.chunk_id for r in r_quokka} >= {"cb"}


# ── Corner 5: ChatResponse.path Literal pinned to 5 values ───────────


def test_chat_response_path_literal_includes_graphrag():
    """Grep server.py to ensure ChatResponse.path Literal contains the 5
    expected values. R4-4 added "graphrag"; prior Round 2 had the other 4.
    """
    src = (REPO_ROOT / "api" / "server.py").read_text(encoding="utf-8")
    m = re.search(
        r"path:\s*Literal\[(.+?)\]\s*\|\s*None",
        src,
    )
    assert m, "ChatResponse.path Literal definition not found in api/server.py"
    body = m.group(1)
    for needed in ('"rag"', '"general"', '"translated"',
                   '"cross-course"', '"graphrag"'):
        assert needed in body, f"{needed!r} missing from ChatResponse.path Literal: {body}"


# ── Regression 1: extract_from_chunks accepts embed_fn + writes embedding ──


async def test_extract_from_chunks_writes_concept_embedding_when_embed_fn_passed():
    """Wire an `embed_fn` through extract_from_chunks and verify Concept
    nodes carry concept_embedding. Uses the same _FakeRouter pattern as
    tests/test_kg_extractor.py to keep the test offline."""
    from nano_notebooklm.kg.extractor import extract_from_chunks
    from nano_notebooklm.types import Chunk, FileType

    chunks = [
        Chunk(chunk_id=f"c{i}", doc_id="d1", course_id="testcourse",
              text=f"chunk {i} text", file_type=FileType.PDF,
              source_file="x.pdf", location=f"p{i}", page=i)
        for i in range(3)
    ]

    stage_a = {
        "course_overview": "Course about zebras.",
        "topics": [{"name": "Topic Alpha", "summary": "Topic about zebra. zebra",
                    "weight": 5}],
    }
    stage_b_each = {
        "concepts": [{
            "name": "Concept Beta", "definition": "Beta talks about zebra too.",
            "type": "definition", "parent_topic": "Topic Alpha",
        }],
        "relations": [],
    }

    class _FakeRouter:
        def __init__(self, responses):
            self.responses = list(responses)
        async def complete_structured(self, prompt, *, system="", task_type="", **kw):
            if not self.responses:
                raise RuntimeError("FakeRouter ran out")
            return self.responses.pop(0)

    router = _FakeRouter([stage_a, stage_b_each, stage_b_each, stage_b_each])

    concepts, _ = await extract_from_chunks(
        chunks, course_name="testcourse", router=router, max_chunks=3,
        embed_fn=_keyword_embed,
    )

    # Root has no source_chunks, embedding optional. Topics + leaves MUST
    # have concept_embedding set when embed_fn is provided.
    topics = [c for c in concepts if c.concept_type == "topic"]
    leaves = [c for c in concepts if c.depth >= 2]
    assert topics, "Stage A topics missing from output"
    assert leaves, "Stage B leaves missing from output"
    for c in topics + leaves:
        assert c.concept_embedding is not None, \
            f"concept_embedding not written on {c.concept_id}"
        assert isinstance(c.concept_embedding, list)
        assert len(c.concept_embedding) == 4  # _keyword_embed produces 4-d

    # Verify signature accepts embed_fn as kwarg (defends against accidental
    # removal in future refactors).
    sig = inspect.signature(extract_from_chunks)
    assert "embed_fn" in sig.parameters
    assert sig.parameters["embed_fn"].default is None


# ── Regression 2: R4-2 4-stage upload contract unchanged ─────────────


def test_upload_stages_contract_unchanged_after_r4_4():
    """R4-4 must not add a stage_c — embedding computation folds into Stage B
    so frontend processing.jsx's 4-bar rendering stays correct."""
    from nano_notebooklm.kg import extractor
    assert extractor.UPLOAD_STAGES == (
        "chunking", "embedding", "kg_stage_a", "kg_stage_b",
    )
    # UploadStage Literal must match the tuple (no kg_stage_c).
    src = (REPO_ROOT / "nano_notebooklm" / "kg" / "extractor.py").read_text(
        encoding="utf-8"
    )
    m = re.search(r"UploadStage\s*=\s*Literal\[(.+?)\]", src)
    assert m, "UploadStage Literal definition not found"
    body = m.group(1)
    assert '"kg_stage_c"' not in body, \
        "R4-4 must not add kg_stage_c to UploadStage Literal (breaks R4-2 contract)"


# ══════════════════════════════════════════════════════════════════════
# R4-4 fix-all v3 (review-swarm follow-up) — mtime-keyed cache on _load_kg.
# Each cache test seeds its OWN course_id so it doesn't share cache state
# with sibling tests (the module-level cache is process-scoped). Tests
# also reset the cache explicitly to keep coupling local.
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def _reset_kg_cache():
    """Clear the module-level _KG_LOAD_CACHE around each test that asserts
    cache semantics so prior test runs don't leak entries (the cache key
    includes artifacts_dir so collisions are unlikely in practice, but a
    flaky shared state is worse than the cleanup cost)."""
    from nano_notebooklm.kb import graph_search as gs_mod
    gs_mod._KG_LOAD_CACHE.clear()
    yield
    gs_mod._KG_LOAD_CACHE.clear()


def test_kg_load_cache_returns_same_data_on_unchanged_mtime(
    isolated_artifacts, monkeypatch, _reset_kg_cache,
):
    """Two consecutive _load_kg() calls with no mtime change → second call
    must not re-read the file from disk."""
    from nano_notebooklm.kb import graph_search as gs_mod

    art = isolated_artifacts
    nodes = [_make_node("n_a", "Alpha", "zebra zebra", chunk_id="ca")]
    _seed_course(art, "cT", nodes, [], [_make_chunk_row("ca", "alpha")])

    # Count Path.read_text calls scoped to knowledge_graph.json + edits.
    real_read_text = Path.read_text
    read_calls: list[str] = []

    def _counting_read_text(self, *a, **kw):
        if self.name in ("knowledge_graph.json", "mindmap_edits.json"):
            read_calls.append(self.name)
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", _counting_read_text)

    art_path = Path(art)
    first = gs_mod._load_kg("cT", art_path)
    assert first is not None
    first_count = len(read_calls)
    assert first_count >= 1  # at least the kg file was read on miss

    second = gs_mod._load_kg("cT", art_path)
    assert second is not None
    # Second call must reuse cache → no additional reads of either file.
    assert len(read_calls) == first_count, (
        f"expected no new read_text calls on cache hit, got "
        f"{len(read_calls) - first_count} extra: {read_calls[first_count:]}"
    )
    # Returned dict is the cached instance (identity check).
    assert second is first


def test_kg_load_cache_invalidates_on_mtime_bump(
    isolated_artifacts, monkeypatch, _reset_kg_cache,
):
    """After touch()-ing the kg file, the next _load_kg() must re-read."""
    import time as _time
    from nano_notebooklm.kb import graph_search as gs_mod

    art = isolated_artifacts
    nodes = [_make_node("n_a", "Alpha", "zebra zebra", chunk_id="ca")]
    _seed_course(art, "cT2", nodes, [], [_make_chunk_row("ca", "alpha")])

    real_read_text = Path.read_text
    read_calls: list[str] = []

    def _counting_read_text(self, *a, **kw):
        if self.name == "knowledge_graph.json":
            read_calls.append(self.name)
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", _counting_read_text)

    art_path = Path(art)
    gs_mod._load_kg("cT2", art_path)
    assert len(read_calls) == 1

    # Bump mtime on the kg file. Sleep enough that the new mtime is
    # distinguishable from the original on filesystems with second-
    # resolution mtimes (most macOS HFS+/APFS is sub-second but
    # belt-and-braces).
    kg_path = art_path / "courses" / "cT2" / "knowledge_graph.json"
    new_mtime = kg_path.stat().st_mtime + 5.0
    import os as _os
    _os.utime(kg_path, (new_mtime, new_mtime))

    gs_mod._load_kg("cT2", art_path)
    # Cache invalidated → second read happened.
    assert len(read_calls) == 2


def test_kg_load_cache_invalidates_on_edits_mtime_bump(
    isolated_artifacts, monkeypatch, _reset_kg_cache,
):
    """Adding/updating mindmap_edits.json must invalidate the cache so
    delete_node overlays from /api/mindmap/{id}/edit take effect on the
    next chat without restarting the server."""
    from nano_notebooklm.kb import graph_search as gs_mod

    art = isolated_artifacts
    nodes = [
        _make_node("n_a", "Alpha", "zebra zebra", chunk_id="ca"),
        _make_node("n_b", "Beta", "zebra and topic", chunk_id="cb"),
    ]
    _seed_course(art, "cT3", nodes, [], [
        _make_chunk_row("ca", "alpha"), _make_chunk_row("cb", "beta"),
    ])

    art_path = Path(art)
    # First load (no edits sidecar yet) — both nodes present.
    first = gs_mod._load_kg("cT3", art_path)
    assert {n["id"] for n in first["nodes"]} == {"n_a", "n_b"}

    # Write an edits sidecar that deletes n_b.
    edits_path = art_path / "courses" / "cT3" / "mindmap_edits.json"
    edits_path.write_text(json.dumps([
        {"op": "delete_node", "id": "n_b"},
    ]))

    second = gs_mod._load_kg("cT3", art_path)
    # Overlay must drop n_b on the second call (cache invalidated by
    # edits-file mtime change).
    assert {n["id"] for n in second["nodes"]} == {"n_a"}


def test_kg_load_returns_none_when_file_missing_does_not_cache_negative(
    isolated_artifacts, _reset_kg_cache,
):
    """Negative result (no KG file) must not produce a sticky cache entry
    — if the user uploads a course later and writes knowledge_graph.json,
    the very next call should pick it up."""
    from nano_notebooklm.kb import graph_search as gs_mod

    art = isolated_artifacts
    art_path = Path(art)
    # Course dir exists but no KG file.
    (art_path / "courses" / "cT4").mkdir(parents=True)
    assert gs_mod._load_kg("cT4", art_path) is None

    # Now write the KG file.
    nodes = [_make_node("n_a", "Alpha", "zebra", chunk_id="ca")]
    (art_path / "courses" / "cT4" / "knowledge_graph.json").write_text(
        json.dumps({"nodes": nodes, "edges": [], "course_name": "cT4"})
    )
    out = gs_mod._load_kg("cT4", art_path)
    assert out is not None
    assert {n["id"] for n in out["nodes"]} == {"n_a"}
