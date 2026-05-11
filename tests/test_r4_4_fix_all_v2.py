"""R4-4 GraphRAG retriever — review-swarm fix-all v2 regression tests.

Covers the 10 medium-severity findings from the v1 review swarm:
  V1: _graphrag_score_floor clamps to [0, 1]; STATUS / .env.example /
      test_router_intent.py contract gaps.
  V2: B7 warm-up is fire-and-forget + API-mode skip + /api/status surfaces
      embed_warm_ok.
  V3: graphrag admission passes_score_gate uses min_hits=1 (decoupled from
      RAG_SCORE_GATE_MIN_HITS so single-strong-hit courses pass).
  V4: _resolve_node_embeddings batch failure falls back to per-node embed
      so a poison-text outlier doesn't wipe the whole cache.
  V5: graph_search log lines drop exc_info=True / absolute paths to keep
      user query text + filesystem layout out of log shippers.
  V6: graph_search _load_kg applies a minimal user-edit overlay so
      delete_node / delete_edge ops actually affect retrieval.

All tests offline — no LLM keys, no sentence-transformer downloads.
"""

from __future__ import annotations

import importlib
import json
import re
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent


# ── Shared helpers ───────────────────────────────────────────────────


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


def _make_node(nid: str, name: str, definition: str, chunk_id: str | None = None,
               concept_type: str = "definition", depth: int = 2, weight: float = 1.0,
               concept_embedding: list[float] | None = None) -> dict:
    source_chunks = []
    if chunk_id is not None:
        source_chunks = [{"chunk_id": chunk_id, "source_file": "x.md",
                          "location": "p1", "page": 1}]
    node = {"id": nid, "name": name, "definition": definition,
            "concept_type": concept_type, "course_ids": [],
            "chunk_ids": [], "depth": depth, "weight": weight,
            "source_chunks": source_chunks, "parent_topic": None,
            "learning_order": None}
    if concept_embedding is not None:
        node["concept_embedding"] = concept_embedding
    return node


def _make_chunk(cid: str, text: str) -> dict:
    return {"chunk_id": cid, "doc_id": "d1", "course_id": "cT",
            "text": text, "file_type": "pdf",
            "source_file": "x.md", "location": "p1", "page": 1}


def _seed(art: Path, course_id: str, nodes, edges, chunks):
    cd = art / "courses" / course_id
    cd.mkdir(parents=True, exist_ok=True)
    (cd / "knowledge_graph.json").write_text(
        json.dumps({"nodes": nodes, "edges": edges, "course_name": course_id})
    )
    (cd / "chunks.json").write_text(
        json.dumps(chunks, default=str)
    )


# ── V1 contract / doc / clamp ────────────────────────────────────────


def test_graphrag_score_floor_clamps_negative(monkeypatch):
    from nano_notebooklm.skills import qa_skill
    monkeypatch.setenv("GRAPHRAG_SCORE_GATE_TOP1", "-0.5")
    assert qa_skill._graphrag_score_floor() == 0.0


def test_graphrag_score_floor_clamps_above_one(monkeypatch):
    from nano_notebooklm.skills import qa_skill
    monkeypatch.setenv("GRAPHRAG_SCORE_GATE_TOP1", "2.5")
    assert qa_skill._graphrag_score_floor() == 1.0


def test_test_router_intent_path_literal_accepts_graphrag():
    """Pin the previously-missing 'graphrag' in the canonical Pydantic
    accept test in tests/test_router_intent.py (Reviewer 4 F2)."""
    src = (REPO_ROOT / "tests" / "test_router_intent.py").read_text(
        encoding="utf-8"
    )
    # Match the accept-list tuple after fix-all v2.
    m = re.search(
        r'for ok in \(("rag",\s*"general",\s*"translated",\s*"cross-course",\s*"graphrag")\):',
        src,
    )
    assert m, "test_router_intent.py's ChatResponse.path accept-list must include 'graphrag'"


def test_env_example_documents_new_envs():
    """All three R4-4 env vars added by fix-all v1+v2 must be discoverable
    in .env.example so operators don't have to grep source."""
    src = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "GRAPHRAG_ENABLED" in src
    assert "GRAPHRAG_SCORE_GATE_TOP1" in src
    assert "NANO_NLM_DISABLE_EMBED_WARMUP" in src


# ── V2 warmup / health / status ──────────────────────────────────────


def test_startup_warmup_uses_create_task_not_blocking_await(monkeypatch):
    """Source pin: the production warmup path must use asyncio.create_task
    (fire-and-forget) rather than `await asyncio.to_thread(...)` directly
    inside the startup hook, so FastAPI accepts liveness probes during
    the model load window."""
    src = (REPO_ROOT / "api" / "server.py").read_text(encoding="utf-8")
    hook = src[src.index("async def _warm_embed_fn"):
               src.index("async def _warm_embed_fn") + 2500]
    assert "_aio.create_task(_do_warmup())" in hook, \
        "warmup must fire-and-forget via create_task"
    assert "app.state.embed_warm_ok" in hook, \
        "warmup must update app.state.embed_warm_ok"


def test_status_endpoint_surfaces_embed_warm_ok():
    """grep /api/status response shape for embed_warm_ok key.

    R4-5 fix-all v1 #V8 (R4-5 review v1): replace the magic char-count
    slice with a sentinel-based slice — find the end of status_endpoint
    by locating the next `async def` declaration. This makes the grep
    robust to future status_endpoint growth (the original 1200/2400
    char windows already broke once when v2 / part 2 grew the body)."""
    src = (REPO_ROOT / "api" / "server.py").read_text(encoding="utf-8")
    start = src.index("async def status_endpoint")
    # Next `async def` (or `def`) declaration marks the end of the function.
    end = src.index("\nasync def ", start + 1)
    status_block = src[start:end]
    assert '"embed_warm_ok"' in status_block, \
        "/api/status must surface embed_warm_ok"


def test_warmup_skipped_when_embedding_mode_api():
    """Source pin: API-mode skip avoids a useless outbound HTTP POST on
    every restart (Reviewer 2 F1)."""
    src = (REPO_ROOT / "api" / "server.py").read_text(encoding="utf-8")
    hook = src[src.index("async def _warm_embed_fn"):
               src.index("async def _warm_embed_fn") + 2500]
    assert 'config.EMBEDDING_MODE' in hook
    assert 'lower() != "local"' in hook


# ── V3 graphrag admission min_hits=1 ─────────────────────────────────


def test_qa_skill_graphrag_admission_uses_min_hits_one():
    """Pin the explicit `min_hits=1` passed to passes_score_gate so a
    future env-default flip on RAG_SCORE_GATE_MIN_HITS can't silently
    raise graphrag's required-hits bar."""
    src = (REPO_ROOT / "nano_notebooklm" / "skills" / "qa_skill.py").read_text(
        encoding="utf-8"
    )
    m = re.search(
        r"router_intent\.passes_score_gate\(\s*graphrag_results,\s*"
        r"top1_threshold=_graphrag_score_floor\(\),\s*min_hits=1,?\s*\)",
        src,
    )
    assert m, "graphrag admission must call passes_score_gate(..., min_hits=1)"


# ── V4 batched embed partial fallback ────────────────────────────────


def test_resolve_node_embeddings_falls_back_per_node_on_batch_failure(isolated_artifacts):
    """When the batched embed_fn raises, the per-node fallback must
    populate the cache for the texts that DO embed cleanly, instead of
    returning an empty cache. A 'poison text' that fails individually
    only loses itself, not the whole batch."""
    from nano_notebooklm.kb.graph_search import graph_search

    art = isolated_artifacts
    nodes = [
        _make_node(f"n_{i}", f"Node{i}", "zebra zebra", chunk_id=f"c{i}")
        for i in range(3)
    ]
    chunks = [_make_chunk(f"c{i}", f"text {i}") for i in range(3)]
    _seed(art, "cT", nodes, [], chunks)

    poison_at = {"value": 0}

    def flaky_embed(texts):
        # Batch path: first batch call always fails. Subsequent single-
        # text calls (the per-node fallback) succeed.
        if len(texts) > 1:
            raise RuntimeError("synthetic batch failure")
        return _keyword_embed(texts)

    results = graph_search("zebra", "cT", flaky_embed,
                           artifacts_dir=art, top_k_concepts=3)
    cids = {r.chunk_id for r in results}
    # All three nodes should still be retrievable via per-node fallback.
    assert "c0" in cids and "c1" in cids and "c2" in cids


# ── V5 log PII scrub ─────────────────────────────────────────────────


def test_graph_search_query_embed_log_has_no_exc_info():
    """Source pin: the query-embed exception log must NOT use
    exc_info=True (Reviewer 2 F3: OpenAI tracebacks carry request body)."""
    src = (REPO_ROOT / "nano_notebooklm" / "kb" / "graph_search.py").read_text(
        encoding="utf-8"
    )
    m = re.search(
        r'graph_search: embed_fn failed on query[^\n]*',
        src,
    )
    assert m, "query-embed warning line not found"
    assert "exc_info=True" not in m.group(0), \
        "query-embed log must drop exc_info=True to avoid leaking query body"


def test_qa_skill_graphrag_failure_log_has_no_exc_info():
    src = (REPO_ROOT / "nano_notebooklm" / "skills" / "qa_skill.py").read_text(
        encoding="utf-8"
    )
    m = re.search(
        r'graph_search failed for course=[^\n]*',
        src,
    )
    assert m
    assert "exc_info=True" not in m.group(0), \
        "graph_search failure log must drop exc_info=True"


def test_graph_search_load_kg_log_uses_course_id_not_path():
    """Path disclosure scrub: failed KG-load log must show course_id, not
    the full absolute path (Reviewer 2 F4 hygiene)."""
    src = (REPO_ROOT / "nano_notebooklm" / "kb" / "graph_search.py").read_text(
        encoding="utf-8"
    )
    m = re.search(r"failed to load knowledge_graph\.json[^\n]*", src)
    assert m
    body = m.group(0)
    assert "course=%s" in body
    assert "%s" not in body.replace("course=%s", ""), \
        "no extra %s for path interpolation — only course_id"


# ── V6 _load_kg applies user-edit overlay ────────────────────────────


def test_load_kg_applies_delete_node_overlay(isolated_artifacts):
    """When the student deletes a node via /api/mindmap/{id}/edit, the
    mindmap_edits.json sidecar gets a delete_node op. graph_search must
    honour this overlay so the deleted node no longer seeds retrieval."""
    from nano_notebooklm.kb.graph_search import graph_search

    art = isolated_artifacts
    nodes = [
        _make_node("n_a", "Alpha", "zebra zebra", chunk_id="ca"),
        _make_node("n_b", "Beta", "zebra context", chunk_id="cb"),
    ]
    chunks = [_make_chunk("ca", "alpha text"), _make_chunk("cb", "beta text")]
    _seed(art, "cT", nodes, [], chunks)

    # Student deletes n_a via the mindmap edit endpoint — the sidecar:
    edits_path = art / "courses" / "cT" / "mindmap_edits.json"
    edits_path.write_text(json.dumps([
        {"op": "delete_node", "id": "n_a"},
    ]))

    results = graph_search("zebra", "cT", _keyword_embed,
                           artifacts_dir=art, top_k_concepts=2)
    cids = {r.chunk_id for r in results}
    # Deleted node's chunk must be gone; surviving node's chunk remains.
    assert "ca" not in cids
    assert "cb" in cids


def test_load_kg_applies_delete_edge_overlay(isolated_artifacts):
    """delete_edge op must strip the edge from BFS so a deleted relation
    no longer expands into the connected node's source_chunks."""
    from nano_notebooklm.kb.graph_search import graph_search

    art = isolated_artifacts
    nodes = [
        _make_node("n_a", "Alpha", "zebra zebra", chunk_id="ca"),
        _make_node("n_b", "Beta", "quokka quokka", chunk_id="cb"),
    ]
    edges = [{"source": "n_a", "target": "n_b", "relation_type": "part-of"}]
    chunks = [_make_chunk("ca", "alpha"), _make_chunk("cb", "beta")]
    _seed(art, "cT", nodes, edges, chunks)

    edits_path = art / "courses" / "cT" / "mindmap_edits.json"
    edits_path.write_text(json.dumps([
        {"op": "delete_edge", "source": "n_a", "target": "n_b"},
    ]))

    # Query zebra hits n_a; BFS would normally reach n_b via the edge,
    # but the deleted edge should prevent that expansion.
    results = graph_search("zebra", "cT", _keyword_embed,
                           artifacts_dir=art, top_k_concepts=1, hop_limit=2)
    cids = {r.chunk_id for r in results}
    assert "ca" in cids
    assert "cb" not in cids


def test_load_kg_no_overlay_when_edits_missing(isolated_artifacts):
    """No mindmap_edits.json → graph_search returns the raw KG (no
    surprising behaviour for users who never edited the mindmap)."""
    from nano_notebooklm.kb.graph_search import graph_search

    art = isolated_artifacts
    nodes = [_make_node("n_a", "Alpha", "zebra", chunk_id="ca")]
    chunks = [_make_chunk("ca", "alpha")]
    _seed(art, "cT", nodes, [], chunks)

    results = graph_search("zebra", "cT", _keyword_embed,
                           artifacts_dir=art)
    assert {r.chunk_id for r in results} == {"ca"}


def test_load_kg_ignores_malformed_edits_file(isolated_artifacts):
    """Corrupt mindmap_edits.json must not crash graph_search; fall back
    to no overlay."""
    from nano_notebooklm.kb.graph_search import graph_search

    art = isolated_artifacts
    nodes = [_make_node("n_a", "Alpha", "zebra", chunk_id="ca")]
    chunks = [_make_chunk("ca", "alpha")]
    _seed(art, "cT", nodes, [], chunks)

    edits_path = art / "courses" / "cT" / "mindmap_edits.json"
    edits_path.write_text("{not valid json")

    results = graph_search("zebra", "cT", _keyword_embed, artifacts_dir=art)
    assert {r.chunk_id for r in results} == {"ca"}
