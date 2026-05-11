"""R4-4 GraphRAG retriever — review-swarm fix-all v1 regression tests.

Covers the 5 critical/medium findings that came out of the post-R4-4
review swarm (a214858):

- #A1: concept_embedding now round-trips through KnowledgeGraph.add_concepts
       and kg.save → reload (previously dropped, forcing every chat into
       the slow lazy-embed path).
- #A2: extract_from_chunks's batch embedding pass runs via asyncio.to_thread
       so the event loop is free during the Stage B → done window.
- #A3: graphrag admission gate now uses router_intent.passes_score_gate
       with a graphrag-specific cosine floor (default 0.15), preventing
       low-relevance queries from pre-empting the RAG path.
- #B4: lazy embedding for cache-miss nodes batches into a single embed_fn
       call instead of per-node sequential calls.
- #B5: editing a node's name or definition via /api/mindmap/{id}/edit
       drops the cached concept_embedding so graph_search lazy-recomputes
       against the new text.
- #B6: GRAPHRAG_ENABLED=0 kill switch globally disables the graphrag path
       without removing knowledge_graph.json files.

All tests offline — no LLM keys, no sentence-transformer downloads.
"""

from __future__ import annotations

import importlib
import inspect
import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent


# ── shared helpers ───────────────────────────────────────────────────


_KEYWORDS = ["zebra", "quokka", "aardvark", "penguin"]


def _keyword_embed(texts: list[str]) -> np.ndarray:
    """One-hot keyword embedder; identical to tests/test_graph_search.py
    so KG fixtures stay interchangeable across both test files."""
    out = np.zeros((len(texts), len(_KEYWORDS)), dtype=np.float32)
    for i, t in enumerate(texts):
        low = (t or "").lower()
        for j, kw in enumerate(_KEYWORDS):
            if kw in low:
                out[i, j] = 1.0
    norms = np.linalg.norm(out, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return out / norms


def _make_node(nid: str, name: str, definition: str,
               concept_type: str = "definition", depth: int = 2,
               weight: float = 1.0, chunk_id: str | None = None,
               concept_embedding: list[float] | None = None) -> dict:
    source_chunks = []
    if chunk_id is not None:
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
                    source_file: str = "x.md") -> dict:
    return {
        "chunk_id": cid, "doc_id": "d1", "course_id": course_id,
        "text": text, "file_type": "pdf",
        "source_file": source_file, "location": "p1", "page": 1,
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


# ── #A1: round-trip concept_embedding through KG save/load ───────────


def test_add_concepts_persists_concept_embedding(isolated_artifacts):
    """Critical regression: KnowledgeGraph.add_concepts used to strip
    concept_embedding via its explicit kwarg whitelist, so kg.save's
    on-disk knowledge_graph.json carried no embedding and every chat
    paid the lazy-embed tax."""
    from nano_notebooklm.kg.graph import KnowledgeGraph
    from nano_notebooklm.types import Concept

    c = Concept(
        concept_id="concept_test_alpha",
        name="Alpha",
        definition="alpha definition text",
        concept_type="topic",
        depth=1,
        weight=5.0,
        source_chunks=[],
        parent_topic=None,
        concept_embedding=[0.1, 0.2, 0.3, 0.4],
    )

    kg = KnowledgeGraph()
    kg.add_concepts([c])
    out = isolated_artifacts / "kg.json"
    kg.save(out)

    data = json.loads(out.read_text())
    nodes = data["nodes"]
    assert len(nodes) == 1
    assert nodes[0].get("concept_embedding") == [0.1, 0.2, 0.3, 0.4], \
        f"concept_embedding lost during add_concepts → save round trip; got {nodes[0]!r}"


def test_add_concepts_merge_path_keeps_first_concept_embedding():
    """Merge branch: same concept_id added twice. First add carries the
    embedding; second add has None. The cached embedding must survive
    (first-seen discipline, same as parent_topic / learning_order)."""
    from nano_notebooklm.kg.graph import KnowledgeGraph
    from nano_notebooklm.types import Concept

    c1 = Concept(concept_id="x", name="X", definition="dx",
                 source_chunks=[],
                 concept_embedding=[1.0, 0.0, 0.0, 0.0])
    c2 = Concept(concept_id="x", name="X", definition="dx",
                 source_chunks=[],
                 concept_embedding=None)

    kg = KnowledgeGraph()
    kg.add_concepts([c1])
    kg.add_concepts([c2])
    assert kg.graph.nodes["x"]["concept_embedding"] == [1.0, 0.0, 0.0, 0.0]


def test_add_concepts_merge_path_fills_concept_embedding_when_existing_missing():
    """Merge branch: existing node has no embedding; new concept brings one
    → fill rather than leave it None."""
    from nano_notebooklm.kg.graph import KnowledgeGraph
    from nano_notebooklm.types import Concept

    c1 = Concept(concept_id="y", name="Y", definition="dy",
                 source_chunks=[], concept_embedding=None)
    c2 = Concept(concept_id="y", name="Y", definition="dy",
                 source_chunks=[],
                 concept_embedding=[0.0, 1.0, 0.0, 0.0])

    kg = KnowledgeGraph()
    kg.add_concepts([c1])
    kg.add_concepts([c2])
    assert kg.graph.nodes["y"]["concept_embedding"] == [0.0, 1.0, 0.0, 0.0]


# ── #A2: extract_from_chunks awaits embed_fn via asyncio.to_thread ──


def test_extract_from_chunks_offloads_embed_fn_to_thread():
    """Source pin: embed_fn call inside extract_from_chunks must be
    awaited via asyncio.to_thread, not invoked synchronously on the
    event loop. Grep guards against accidental revert."""
    src = (REPO_ROOT / "nano_notebooklm" / "kg" / "extractor.py").read_text(
        encoding="utf-8"
    )
    # The line is inside the embed_fn branch in extract_from_chunks; pin
    # the exact `await asyncio.to_thread(embed_fn, texts)` shape so a
    # future refactor can't downgrade it to `embs = embed_fn(texts)`.
    assert "await asyncio.to_thread(embed_fn, texts)" in src, \
        "embed_fn must be awaited via asyncio.to_thread to keep the event loop responsive"


# ── #A3: graphrag admission uses passes_score_gate ──────────────────


def test_qa_skill_graphrag_uses_passes_score_gate():
    """Source pin: the graphrag admission must call passes_score_gate,
    not the original `len(graphrag_results) >= 2` check."""
    src = (REPO_ROOT / "nano_notebooklm" / "skills" / "qa_skill.py").read_text(
        encoding="utf-8"
    )
    # The exact gate call (with the graphrag-specific floor helper).
    assert "router_intent.passes_score_gate(" in src
    assert "top1_threshold=_graphrag_score_floor()" in src
    # The original loose check must be gone.
    assert "len(graphrag_results) >= 2" not in src, \
        "graphrag admission must not still rely on len>=2 — use passes_score_gate"


def test_graphrag_floor_env_overrides_default(monkeypatch):
    """GRAPHRAG_SCORE_GATE_TOP1 env should override DEFAULT_GRAPHRAG_TOP1_THRESHOLD."""
    from nano_notebooklm.skills import qa_skill
    monkeypatch.setenv("GRAPHRAG_SCORE_GATE_TOP1", "0.42")
    assert qa_skill._graphrag_score_floor() == pytest.approx(0.42)
    monkeypatch.setenv("GRAPHRAG_SCORE_GATE_TOP1", "garbage")
    assert qa_skill._graphrag_score_floor() == qa_skill.DEFAULT_GRAPHRAG_TOP1_THRESHOLD


# ── #B4: lazy embed batches into a single embed_fn call ─────────────


def test_graph_search_batches_lazy_embed_into_single_call(isolated_artifacts):
    """Spy embed_fn call count. With 5 cache-miss nodes graph_search
    should make exactly 2 embed_fn calls: 1 for the query + 1 for the
    batched node list. Before #B4 it was 1 + 5 = 6 calls."""
    from nano_notebooklm.kb.graph_search import graph_search

    art = isolated_artifacts
    nodes = [
        _make_node(f"n_{i}", f"Node{i}", "zebra zebra", chunk_id=f"c{i}")
        for i in range(5)
    ]  # all 5 nodes have no concept_embedding → all cache-miss
    chunks = [_make_chunk_row(f"c{i}", f"text {i}") for i in range(5)]
    _seed_course(art, "cT", nodes, [], chunks)

    call_count = {"n": 0, "sizes": []}

    def spy_embed(texts):
        call_count["n"] += 1
        call_count["sizes"].append(len(texts))
        return _keyword_embed(texts)

    results = graph_search("zebra", "cT", spy_embed,
                           artifacts_dir=art, top_k_concepts=5)
    assert len(results) >= 1  # at least the seed chunks
    assert call_count["n"] == 2, \
        f"expected 2 embed_fn calls (query + batch), got {call_count['n']} with sizes {call_count['sizes']}"
    # First call = query (1 text), second = batched lazy nodes (5 texts).
    assert call_count["sizes"] == [1, 5]


# ── #B5: editing name/definition invalidates concept_embedding ──────


def test_edit_node_drops_concept_embedding_on_name_change(isolated_artifacts):
    """When update_node patches name, the cached embedding must be cleared
    so the next graph_search lazy-recomputes against the new text."""
    from api.server import apply_edit_ops_with_results

    kg_data = {
        "nodes": [
            _make_node("n_a", "Alpha", "zebra zebra", chunk_id="ca",
                       concept_embedding=[1.0, 0.0, 0.0, 0.0]),
        ],
        "edges": [],
    }
    edited, results = apply_edit_ops_with_results(kg_data, [
        {"op": "update_node", "id": "n_a", "label": "Renamed Alpha"},
    ])
    assert results == [{"op": "update_node", "status": "applied", "reason": None}] \
        or all(r["status"] == "applied" for r in results)
    node = next(n for n in edited["nodes"] if n["id"] == "n_a")
    assert node["name"] == "Renamed Alpha"
    assert node.get("concept_embedding") is None, \
        f"concept_embedding must be cleared after rename; got {node.get('concept_embedding')}"


def test_edit_node_drops_concept_embedding_on_definition_change(isolated_artifacts):
    """Same invalidation for definition edits."""
    from api.server import apply_edit_ops_with_results

    kg_data = {
        "nodes": [
            _make_node("n_a", "Alpha", "zebra zebra", chunk_id="ca",
                       concept_embedding=[1.0, 0.0, 0.0, 0.0]),
        ],
        "edges": [],
    }
    edited, _ = apply_edit_ops_with_results(kg_data, [
        {"op": "update_node", "id": "n_a", "definition": "new definition text"},
    ])
    node = next(n for n in edited["nodes"] if n["id"] == "n_a")
    assert node["definition"] == "new definition text"
    assert node.get("concept_embedding") is None


# ── #B6: GRAPHRAG_ENABLED kill switch ───────────────────────────────


def test_graphrag_enabled_kill_switch(monkeypatch):
    """GRAPHRAG_ENABLED=0 / false / no / off / disabled disable graphrag.

    fix-all v3 #L10 inverted the semantics to fail-safe: only an explicit
    enable token re-enables graphrag, and anything else (typos, unknown
    spellings) disables. Empty / missing defaults to on.
    """
    from nano_notebooklm.skills import qa_skill
    monkeypatch.delenv("GRAPHRAG_ENABLED", raising=False)
    assert qa_skill._graphrag_enabled() is True  # default
    for disabling in ("0", "false", "FALSE", "no", "off", "disabled"):
        monkeypatch.setenv("GRAPHRAG_ENABLED", disabling)
        assert qa_skill._graphrag_enabled() is False, \
            f"GRAPHRAG_ENABLED={disabling!r} should disable graphrag"
    # Explicit enable tokens keep graphrag on; empty also stays on
    # (operator hasn't expressed an intent).
    for enabling in ("1", "true", "yes", "on", "enabled", ""):
        monkeypatch.setenv("GRAPHRAG_ENABLED", enabling)
        assert qa_skill._graphrag_enabled() is True, \
            f"GRAPHRAG_ENABLED={enabling!r} should keep graphrag enabled"


def test_qa_skill_calls_graphrag_enabled_in_admission():
    """Source pin: the graphrag admission must consult _graphrag_enabled()
    so the kill switch actually short-circuits."""
    src = (REPO_ROOT / "nano_notebooklm" / "skills" / "qa_skill.py").read_text(
        encoding="utf-8"
    )
    assert "_graphrag_enabled()" in src, \
        "graphrag admission must check _graphrag_enabled() kill switch"


# ── ChatResponse docstring updated to "five" (cosmetic but pinned) ──


def test_chat_response_docstring_mentions_five_path_values():
    src = (REPO_ROOT / "api" / "server.py").read_text(encoding="utf-8")
    # The docstring lives inside ChatResponse; locate it via the class
    # header to avoid catching other "five" mentions in the file.
    cls = src[src.index("class ChatResponse"):src.index("class ChatResponse") + 2000]
    assert "five `path` values" in cls, \
        "ChatResponse docstring must say 'five' not 'four' after R4-4"
    assert "four `path` values" not in cls, \
        "stale 'four' wording left in ChatResponse docstring"
