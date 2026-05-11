"""R4-4 GraphRAG retriever.

Given a course with an extracted knowledge graph
(``artifacts/courses/<id>/knowledge_graph.json``), rank concept nodes by
cosine similarity to the query embedding, BFS-expand the top-k seeds along
KG edges, and return the union of their ``source_chunks`` joined with each
chunk's text from ``chunks.json``.

Why this beats plain BM25/vector for cross-concept queries:
  "How do convolutional and pooling layers relate?" — plain RAG pulls two
  independent chunks ranked by surface lexical match. GraphRAG seeds both
  concept nodes, walks the ``part-of`` / ``prerequisite_of`` edges that
  connect them, and surfaces the chunks the extractor already linked to
  *that relationship*.

Graceful degradation (see qa_skill.py):
  - KG file missing → return ``[]``; qa_skill falls back to ``path="rag"``.
  - <2 hits → qa_skill falls back to ``path="rag"``.
  - Dimension mismatch between query embedding and cached
    ``concept_embedding`` (e.g. legacy KG cached 384d under sentence-
    transformers while a test injects 32d hash embeddings) → lazy
    per-node recompute for those nodes; others keep the cache.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np

from nano_notebooklm import config
from nano_notebooklm.types import SearchResult

logger = logging.getLogger(__name__)


# Cap chunks per query so a hub concept with 100 incident chunks can't blow
# the LLM context window. GOAL.md R4-4 spec: hop_limit=2 must keep chunks
# ≤ 30. Tunable via env for eval sweeps.
DEFAULT_TOP_K_CONCEPTS = 5
DEFAULT_HOP_LIMIT = 2
DEFAULT_MAX_CHUNKS = 30


def _concept_embed_text(node: dict[str, Any]) -> str:
    """Mirror nano_notebooklm.kg.extractor._concept_embed_text — kept in sync
    so a node's lazy embed matches its cached embed when the extractor was
    run with the same embed_fn."""
    name = (node.get("name") or "").strip()
    definition = (node.get("definition") or "").strip()
    text = f"{name}。{definition}" if definition else name
    return text[:600]


def _load_kg(course_id: str, artifacts_dir: Path) -> dict[str, Any] | None:
    kg_path = artifacts_dir / "courses" / course_id / "knowledge_graph.json"
    if not kg_path.exists():
        return None
    try:
        return json.loads(kg_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("graph_search: failed to load %s; treating as no-KG", kg_path)
        return None


def _load_chunks_index(course_id: str, artifacts_dir: Path) -> dict[str, dict[str, Any]]:
    """chunk_id → raw chunk dict (text/source_file/location/page). Missing
    chunks.json → {}; graph_search will then skip every source_chunks entry
    (no text to render). Empty dict is fine, not an error.
    """
    chunks_path = artifacts_dir / "courses" / course_id / "chunks.json"
    if not chunks_path.exists():
        return {}
    try:
        data = json.loads(chunks_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("graph_search: failed to load %s", chunks_path)
        return {}
    return {row["chunk_id"]: row for row in data if isinstance(row, dict) and row.get("chunk_id")}


def _node_embedding(
    node: dict[str, Any],
    embed_fn: Callable[[list[str]], Any],
    expected_dim: int,
    cache: dict[str, np.ndarray],
) -> np.ndarray | None:
    """Return an L2-normalised embedding for `node`. Prefers the cached
    `concept_embedding` field; falls back to a single-text embed_fn call
    when missing or when the cached dimension disagrees with `expected_dim`
    (the query's dimension is the source of truth).

    `cache` is the per-call lazy-recompute store keyed by node id, so the
    cost is paid at most once per node per query.
    """
    nid = node.get("id") or ""
    if nid in cache:
        return cache[nid]

    cached = node.get("concept_embedding")
    if cached is not None:
        try:
            arr = np.asarray(cached, dtype=np.float32)
            if arr.shape == (expected_dim,):
                cache[nid] = arr
                return arr
            # Dimension mismatch → drop the stale cache, lazy-recompute below.
        except (TypeError, ValueError):
            pass

    text = _concept_embed_text(node)
    if not text:
        return None
    try:
        out = embed_fn([text])
        arr = np.asarray(out, dtype=np.float32)
        if arr.ndim == 2 and arr.shape[0] == 1:
            arr = arr[0]
        if arr.shape != (expected_dim,):
            logger.debug(
                "graph_search: lazy embed for %s returned shape %s != expected (%d,)",
                nid, arr.shape, expected_dim,
            )
            return None
        cache[nid] = arr
        return arr
    except Exception:  # noqa: BLE001 — one bad node must not abort the whole query
        logger.warning("graph_search: lazy embed failed for node %s", nid, exc_info=True)
        return None


def _bfs_neighbors(
    seeds: Iterable[str],
    adjacency: dict[str, set[str]],
    hop_limit: int,
) -> dict[str, int]:
    """Return node_id → hop_distance (0 = seed) for every node reachable
    within hop_limit hops (undirected — `adjacency` already merges in/out
    edges so part-of, prerequisite_of, etc. all expand symmetrically)."""
    distances: dict[str, int] = {}
    queue: deque[tuple[str, int]] = deque()
    for s in seeds:
        if s not in distances:
            distances[s] = 0
            queue.append((s, 0))
    while queue:
        nid, dist = queue.popleft()
        if dist >= hop_limit:
            continue
        for nb in adjacency.get(nid, ()):
            if nb not in distances:
                distances[nb] = dist + 1
                queue.append((nb, dist + 1))
    return distances


def graph_search(
    query: str,
    course_id: str,
    embed_fn: Callable[[list[str]], Any],
    artifacts_dir: Path | None = None,
    top_k_concepts: int = DEFAULT_TOP_K_CONCEPTS,
    hop_limit: int = DEFAULT_HOP_LIMIT,
    max_chunks: int = DEFAULT_MAX_CHUNKS,
) -> list[SearchResult]:
    """Run GraphRAG retrieval against the named course's KG. See module
    docstring for the algorithm; see qa_skill.py for the fallback contract.
    """
    if not query or not query.strip():
        return []
    if not course_id:
        return []

    art = Path(artifacts_dir) if artifacts_dir is not None else Path(config.ARTIFACTS_DIR)
    kg = _load_kg(course_id, art)
    if not kg:
        return []
    nodes: list[dict[str, Any]] = kg.get("nodes") or []
    edges: list[dict[str, Any]] = kg.get("edges") or []
    if not nodes:
        return []

    # 1. Query embedding sets the expected dimension. Any KG node whose
    #    cached concept_embedding disagrees will be lazy-recomputed.
    try:
        q_out = embed_fn([query.strip()])
        q_emb = np.asarray(q_out, dtype=np.float32)
        if q_emb.ndim == 2:
            q_emb = q_emb[0]
        if q_emb.ndim != 1:
            logger.warning("graph_search: query embedding has rank %d, expected 1", q_emb.ndim)
            return []
    except Exception:  # noqa: BLE001 — embed_fn failure cannot crash chat
        logger.warning("graph_search: embed_fn failed on query", exc_info=True)
        return []

    expected_dim = int(q_emb.shape[0])

    # 2. Score every non-root node by cosine against the query (embed_fn
    #    outputs are L2-normalised so dot ≡ cosine).
    embed_cache: dict[str, np.ndarray] = {}
    scored: list[tuple[float, dict[str, Any]]] = []
    for node in nodes:
        if (node.get("concept_type") or "").lower() == "root":
            continue
        emb = _node_embedding(node, embed_fn, expected_dim, embed_cache)
        if emb is None:
            continue
        score = float(np.dot(q_emb, emb))
        scored.append((score, node))

    if not scored:
        return []

    # 3. Take top_k_concepts seeds, then BFS hop_limit hops along edges.
    scored.sort(key=lambda kv: kv[0], reverse=True)
    seeds = [n for _, n in scored[:top_k_concepts]]
    seed_ids = [n["id"] for n in seeds if n.get("id")]
    seed_scores = {n["id"]: s for s, n in scored[:top_k_concepts] if n.get("id")}

    adjacency: dict[str, set[str]] = {}
    for e in edges:
        src, tgt = e.get("source"), e.get("target")
        if not src or not tgt:
            continue
        adjacency.setdefault(src, set()).add(tgt)
        adjacency.setdefault(tgt, set()).add(src)

    distances = _bfs_neighbors(seed_ids, adjacency, hop_limit)
    nodes_by_id = {n.get("id"): n for n in nodes if n.get("id")}

    # 4. Collect source_chunks from every reachable node, dedup by chunk_id,
    #    rank by (-hop_distance, source-node seed_score, source-node weight).
    #    Smaller hop wins first; among same-hop, higher seed score; ties by
    #    node weight. A chunk shared by multiple nodes keeps the best key.
    best_by_chunk: dict[str, tuple[tuple[int, float, float], dict[str, Any], str]] = {}
    for nid, hop in distances.items():
        node = nodes_by_id.get(nid)
        if not node:
            continue
        node_weight = float(node.get("weight") or 0.0)
        # Seed nodes always have an explicit score; downstream neighbours
        # inherit a small penalty so BFS frontier nodes are deprioritised.
        seed_score = seed_scores.get(nid, 0.0)
        sort_key = (-hop, seed_score, node_weight)
        for sc in node.get("source_chunks") or []:
            cid = sc.get("chunk_id") if isinstance(sc, dict) else None
            if not cid:
                continue
            prev = best_by_chunk.get(cid)
            if prev is None or sort_key > prev[0]:
                best_by_chunk[cid] = (sort_key, sc, nid)

    if not best_by_chunk:
        return []

    chunks_idx = _load_chunks_index(course_id, art)

    # 5. Materialise SearchResult list, sorted by sort_key desc, capped.
    ordered = sorted(best_by_chunk.items(), key=lambda kv: kv[1][0], reverse=True)
    results: list[SearchResult] = []
    for cid, (sort_key, sc, nid) in ordered:
        if len(results) >= max_chunks:
            break
        chunk_row = chunks_idx.get(cid)
        text = ""
        source_file = ""
        location = ""
        if chunk_row:
            text = str(chunk_row.get("text") or "")
            source_file = str(chunk_row.get("source_file") or "")
            location = str(chunk_row.get("location") or "")
        else:
            # Fall back to source_chunks metadata (no text, but at least the
            # citation surface stays renderable in the UI). When chunks.json
            # is missing or the chunk_id was deleted, skip — no text means
            # no useful LLM context.
            continue
        # Convert the negative-hop component back to a positive cosine-like
        # score for the API surface so the existing `score` field stays
        # comparable in magnitude with kb.search RRF (both are 0-ish floats).
        # Seed score dominates; hop introduces a small offset.
        _hop_neg, seed_score, node_weight = sort_key
        api_score = float(seed_score) + 0.001 * float(node_weight) + 0.0001 * float(_hop_neg)
        results.append(SearchResult(
            chunk_id=cid,
            text=text,
            source_file=source_file or sc.get("source_file", "") or "",
            location=location or sc.get("location", "") or "",
            score=api_score,
            course_id=course_id,
        ))
    return results
