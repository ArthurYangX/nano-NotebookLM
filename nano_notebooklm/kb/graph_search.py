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
import threading
from collections import deque
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np

from nano_notebooklm import config
from nano_notebooklm.kg.extractor import _concept_embed_text as _extractor_embed_text
from nano_notebooklm.types import Concept, SearchResult

logger = logging.getLogger(__name__)


# Cap chunks per query so a hub concept with 100 incident chunks can't blow
# the LLM context window. GOAL.md R4-4 spec: hop_limit=2 must keep chunks
# ≤ 30. Tunable via env for eval sweeps.
DEFAULT_TOP_K_CONCEPTS = 5
DEFAULT_HOP_LIMIT = 2
DEFAULT_MAX_CHUNKS = 30


# fix-all v2 (R4-4 review-swarm follow-up): mtime-keyed cache so each chat
# turn doesn't re-read + parse `knowledge_graph.json` + `mindmap_edits.json`
# (100-500ms of sync I/O on a 15K-chunk course). Value =
# (overlayed_kg_dict, kg_mtime, edits_mtime). Read-only after population
# per-course-version — we only ever REPLACE entries, never mutate them in
# place, so plain dict lookups across asyncio tasks are safe. The lock only
# serialises the read-and-replace path so two concurrent first-loads don't
# both pay disk cost.
_KG_LOAD_CACHE: dict[str, tuple[dict[str, Any], float, float]] = {}
_KG_LOAD_CACHE_LOCK = threading.Lock()


def _concept_embed_text(node: dict[str, Any]) -> str:
    """fix-all v1 #C8 + v3 #L5 (R4-4 review-swarm): adapter around the
    extractor's canonical helper so lazy-recompute embeddings can never
    drift from the cached batch-compute embeddings.

    v1 used a `class _Shim: pass` instance, which silently AttributeErrors
    if the extractor helper later reads any field beyond name/definition.
    v3 replaces it with a real `Concept` instance so any signature drift
    surfaces at construction time (Pydantic ValidationError) rather than
    deep inside a graph_search broad-except clause that would drop every
    cache-miss node.
    """
    return _extractor_embed_text(Concept(
        concept_id=str(node.get("id") or "shim"),
        name=str(node.get("name") or ""),
        definition=str(node.get("definition") or ""),
    ))


def _cache_key(course_id: str, artifacts_dir: Path) -> str:
    """Cache key folds artifacts_dir in so tests using ``tmp_path`` per case
    don't collide with a long-running server's cache entry for the same
    course_id under the production artifacts dir."""
    return f"{artifacts_dir}::{course_id}"


def _load_kg(course_id: str, artifacts_dir: Path) -> dict[str, Any] | None:
    """Load + overlay the course KG with mtime-keyed memoisation.

    fix-all v2 (R4-4 follow-up): every graphrag chat used to re-read +
    parse `knowledge_graph.json` + `mindmap_edits.json` (~100-500ms of
    sync I/O on a 15K-chunk course). We now cache the overlayed dict
    keyed by both file mtimes — on a cache hit, no disk read; on mtime
    bump (re-extraction or new student edit), we silently re-read.
    """
    kg_path = artifacts_dir / "courses" / course_id / "knowledge_graph.json"
    if not kg_path.exists():
        return None

    try:
        kg_mtime = kg_path.stat().st_mtime
    except OSError:
        return None

    edits_path = artifacts_dir / "courses" / course_id / "mindmap_edits.json"
    try:
        edits_mtime = edits_path.stat().st_mtime if edits_path.exists() else 0.0
    except OSError:
        edits_mtime = 0.0

    key = _cache_key(course_id, artifacts_dir)
    # Fast-path: matched mtimes → return cached dict without touching disk.
    cached = _KG_LOAD_CACHE.get(key)
    if cached is not None and cached[1] == kg_mtime and cached[2] == edits_mtime:
        return cached[0]

    # Slow-path: read + overlay, then publish under the lock so concurrent
    # first-loads don't both pay the cost (one wins; the other's overwrite
    # is idempotent — same mtimes, same content).
    try:
        kg = json.loads(kg_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # fix-all v2 #V5: log only course_id (not full absolute path) to
        # avoid disclosing server filesystem layout in shared log shippers.
        logger.warning(
            "graph_search: failed to load knowledge_graph.json for course=%s; "
            "treating as no-KG", course_id,
        )
        return None
    # fix-all v2 #V6 (R4-4 review-swarm v2): apply user-edit overlay so a
    # node the student deleted via /api/mindmap/{id}/edit doesn't keep
    # seeding graphrag retrieval. The system-KG file is append-only
    # (re-extraction rewrites the whole file); student edits live in a
    # sidecar mindmap_edits.json replayed on every read in api/server.py
    # — graphrag previously bypassed this overlay by reading the raw KG.
    overlayed = _apply_minimal_edit_overlay(kg, course_id, artifacts_dir)

    with _KG_LOAD_CACHE_LOCK:
        _KG_LOAD_CACHE[key] = (overlayed, kg_mtime, edits_mtime)
    return overlayed


def _apply_minimal_edit_overlay(
    kg: dict[str, Any], course_id: str, artifacts_dir: Path
) -> dict[str, Any]:
    """fix-all v2 #V6: apply the subset of student edits that affects
    graphrag retrieval — `delete_node`, `delete_edge`. We don't replay
    `add_node` / `add_edge` (student-added nodes carry no source_chunks
    and add no value to retrieval) or `update_node` text edits (the v1
    #B5 fix already pops the stale embedding in api/server.py so the
    lazy recompute on next graph_search reads the fresh name/definition).

    Keeping this overlay minimal + local avoids a circular dependency
    between nano_notebooklm.kb and api.server.
    """
    edits_path = artifacts_dir / "courses" / course_id / "mindmap_edits.json"
    if not edits_path.exists():
        return kg
    try:
        ops = json.loads(edits_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return kg
    if not isinstance(ops, list):
        return kg

    deleted_nodes: set[str] = set()
    deleted_edges: set[tuple[str, str]] = set()
    for op in ops:
        if not isinstance(op, dict):
            continue
        kind = op.get("op")
        if kind == "delete_node":
            nid = op.get("id")
            if isinstance(nid, str):
                deleted_nodes.add(nid)
        elif kind == "delete_edge":
            src, tgt = op.get("source"), op.get("target")
            if isinstance(src, str) and isinstance(tgt, str):
                deleted_edges.add((src, tgt))

    if not deleted_nodes and not deleted_edges:
        return kg

    out = dict(kg)  # shallow copy; nodes/edges are list refs (we replace them)
    if deleted_nodes:
        out["nodes"] = [n for n in (kg.get("nodes") or [])
                        if n.get("id") not in deleted_nodes]
    if deleted_nodes or deleted_edges:
        out["edges"] = [
            e for e in (kg.get("edges") or [])
            if e.get("source") not in deleted_nodes
            and e.get("target") not in deleted_nodes
            and (e.get("source"), e.get("target")) not in deleted_edges
        ]
    return out


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
        # fix-all v2 #V5: log only course_id, not full absolute path.
        logger.warning("graph_search: failed to load chunks.json for course=%s", course_id)
        return {}
    return {row["chunk_id"]: row for row in data if isinstance(row, dict) and row.get("chunk_id")}


def _resolve_node_embeddings(
    nodes: list[dict[str, Any]],
    embed_fn: Callable[[list[str]], Any],
    expected_dim: int,
) -> dict[str, np.ndarray]:
    """fix-all v1 #B4 (R4-4 review-swarm): single-pass embedding resolver.

    Original implementation called embed_fn([single_text]) once per cache-
    miss node, so a 200-concept legacy KG paid ~200 sequential embed
    calls (~1-2s on warm sentence-transformer). Now: scan once to pull
    every cached-and-dimension-matching embedding into the cache, collect
    every cache-miss node's text into one list, then make a SINGLE
    batched embed_fn(list) call. sentence-transformer internally batches at
    32; the OpenAI-compatible API client batches at 64; either way one
    call amortises tokenizer/HTTP overhead.

    Returns {node_id: ndarray} only for nodes whose embedding could be
    obtained (cached + shape OK, or batch result + shape OK). Missing
    entries → graph_search skips that node from cosine ranking.
    """
    cache: dict[str, np.ndarray] = {}
    to_compute: list[tuple[str, str]] = []  # (node_id, text)

    for node in nodes:
        nid = node.get("id") or ""
        if not nid:
            continue
        cached = node.get("concept_embedding")
        if cached is not None:
            try:
                arr = np.asarray(cached, dtype=np.float32)
                if arr.shape == (expected_dim,):
                    cache[nid] = arr
                    continue
                # Dimension mismatch → drop the stale cache, batch-recompute.
            except (TypeError, ValueError):
                pass
        text = _concept_embed_text(node)
        if text:
            to_compute.append((nid, text))

    if not to_compute:
        return cache

    try:
        batch_out = embed_fn([t for _, t in to_compute])
        batch_arr = np.asarray(batch_out, dtype=np.float32)
        # embed_fn may return shape (n, d) or (d,) when n==1.
        if batch_arr.ndim == 1 and len(to_compute) == 1:
            batch_arr = batch_arr.reshape(1, -1)
        if batch_arr.ndim != 2:
            logger.warning(
                "graph_search: batched embed returned rank %d (expected 2); "
                "falling back to per-node embed", batch_arr.ndim,
            )
            _resolve_per_node(to_compute, embed_fn, expected_dim, cache)
            return cache
    except Exception as exc:  # noqa: BLE001
        # fix-all v2 #V4 (R4-4 review-swarm v2): the v1 batched path
        # returned `cache` empty-handed on any batch failure, so a single
        # bad token / tokenizer error / rate limit wiped out the whole
        # cache-miss list. On legacy KGs (no cached embeddings on disk)
        # that's the entire ranking input → graph_search silently
        # returned []. Fall back to per-node embed so a single bad text
        # only loses its own node, not all 200.
        # fix-all v3 (R4-4 follow-up review-swarm): drop `str(exc)` from
        # the format args — under EMBEDDING_MODE=api the openai-python
        # APIError carries the request body (the failing input texts —
        # i.e. concept names + definitions) in its repr, which would
        # leak into log shippers. Type name (+ optional `code` attr on
        # APIError) is sufficient for triage; mirrors qa_skill.py:622-628.
        code = getattr(exc, "code", type(exc).__name__)
        logger.warning(
            "graph_search: batched lazy embed failed (%s); "
            "falling back to per-node embed for %d nodes",
            code, len(to_compute),
        )
        _resolve_per_node(to_compute, embed_fn, expected_dim, cache)
        return cache

    for (nid, _), emb in zip(to_compute, batch_arr):
        if emb.shape == (expected_dim,):
            cache[nid] = emb
        else:
            logger.debug(
                "graph_search: batch embed for %s shape %s != expected (%d,)",
                nid, emb.shape, expected_dim,
            )
    return cache


def _resolve_per_node(
    to_compute: list[tuple[str, str]],
    embed_fn: Callable[[list[str]], Any],
    expected_dim: int,
    cache: dict[str, np.ndarray],
) -> None:
    """fix-all v2 #V4: per-node embed_fn fallback after a batch failure.

    Each failed node loses only itself, not the whole batch — preserves
    partial-cache ranking when the corpus has a poison-text outlier
    (e.g. a chunk that the tokenizer rejects) or a transient API blip.
    Mutates `cache` in place.
    """
    for nid, text in to_compute:
        try:
            out = embed_fn([text])
            arr = np.asarray(out, dtype=np.float32)
            if arr.ndim == 2 and arr.shape[0] == 1:
                arr = arr[0]
            if arr.shape == (expected_dim,):
                cache[nid] = arr
        except Exception as exc:  # noqa: BLE001 — one bad node must not abort the rest
            logger.debug(
                "graph_search: per-node embed failed for %s (%s)",
                nid, type(exc).__name__,
            )


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
    except Exception as exc:  # noqa: BLE001 — embed_fn failure cannot crash chat
        # fix-all v2 #V5 (R4-4 review-swarm v2): drop exc_info=True on the
        # query-embed path. In EMBEDDING_MODE=api, the openai-python SDK's
        # exception object often carries the request body (`input=[query]`)
        # in its repr/traceback, which would land the user's question in
        # log shippers.
        # fix-all v3 (R4-4 follow-up review-swarm): also drop `str(exc)`
        # from the format args — `APIError.__str__` includes the request
        # body, so even the message-only render (without exc_info) leaks
        # the user's question text. Mirrors qa_skill.py:622-628.
        code = getattr(exc, "code", type(exc).__name__)
        logger.warning("graph_search: embed_fn failed on query (%s)", code)
        return []

    expected_dim = int(q_emb.shape[0])

    # 2. Resolve every non-root node's embedding in one pass (cached +
    #    batched lazy recompute). Score by cosine (dot = cosine on L2-
    #    normalised vectors).
    non_root = [
        n for n in nodes
        if (n.get("concept_type") or "").lower() != "root"
    ]
    embed_cache = _resolve_node_embeddings(non_root, embed_fn, expected_dim)
    scored: list[tuple[float, dict[str, Any]]] = []
    for node in non_root:
        nid = node.get("id") or ""
        emb = embed_cache.get(nid)
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
