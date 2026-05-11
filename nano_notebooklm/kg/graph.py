"""NetworkX-based knowledge graph storage and operations."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import networkx as nx

from nano_notebooklm.types import Concept, Relation

logger = logging.getLogger(__name__)


class KnowledgeGraph:
    """Stores and queries a knowledge graph of course concepts."""

    def __init__(self):
        self.graph = nx.DiGraph()

    def add_concepts(self, concepts: list[Concept]):
        """Add concepts as nodes.

        F6 (review-swarm): the explicit kwarg list previously dropped
        `parent_topic`, so the field round-tripped as None through
        save/load. Now it persists.

        fix-all v1 #A1 (R4-4 review-swarm): the same drop happened to
        `concept_embedding` — extract_from_chunks would compute it and
        merge_concepts.model_copy() would preserve it, but this method's
        explicit kwarg list silently stripped it before kg.save(), so the
        on-disk `knowledge_graph.json` carried NO concept_embedding and
        every /api/chat that took the graphrag branch ran the lazy
        per-node embed path. Now persists.
        """
        for c in concepts:
            if self.graph.has_node(c.concept_id):
                # Merge: extend chunk_ids and course_ids
                existing = self.graph.nodes[c.concept_id]
                existing["chunk_ids"] = list(set(existing.get("chunk_ids", []) + c.chunk_ids))
                existing["course_ids"] = list(set(existing.get("course_ids", []) + c.course_ids))
                existing["source_chunks"] = _merge_source_chunks(existing.get("source_chunks", []), c.source_chunks)
                existing["weight"] = max(float(existing.get("weight", 1.0)), float(c.weight))
                existing["depth"] = min(int(existing.get("depth", 1)), int(c.depth))
                if not existing.get("definition") and c.definition:
                    existing["definition"] = c.definition
                if not existing.get("parent_topic") and c.parent_topic:
                    existing["parent_topic"] = c.parent_topic
                if existing.get("learning_order") is None and c.learning_order is not None:
                    existing["learning_order"] = c.learning_order
                # fix-all v1 #A1 + v3 #L7: keep the first non-null
                # concept_embedding (first-seen discipline mirrors
                # parent_topic above) — *except* when the existing
                # embedding has a different dimension from the new one,
                # which signals the operator switched EMBEDDING_MODE
                # (e.g. local 384d → API 1536d). In that case the cached
                # value is stale and graph_search would silently drop it
                # on every query; overwrite so the next save persists the
                # fresh dimension.
                existing_emb = existing.get("concept_embedding")
                new_emb = c.concept_embedding
                if new_emb is not None:
                    if existing_emb is None:
                        existing["concept_embedding"] = new_emb
                    elif (isinstance(existing_emb, list)
                          and isinstance(new_emb, list)
                          and len(existing_emb) != len(new_emb)):
                        existing["concept_embedding"] = new_emb
            else:
                self.graph.add_node(
                    c.concept_id,
                    name=c.name,
                    definition=c.definition,
                    concept_type=c.concept_type,
                    course_ids=c.course_ids,
                    chunk_ids=c.chunk_ids,
                    depth=c.depth,
                    weight=c.weight,
                    source_chunks=c.source_chunks,
                    parent_topic=c.parent_topic,
                    learning_order=c.learning_order,
                    # fix-all v1 #A1: persist the cached embedding so
                    # graph_search hits the fast path on every chat.
                    concept_embedding=c.concept_embedding,
                )

    def add_relations(self, relations: list[Relation]):
        """Add relations as edges."""
        for r in relations:
            # Only add if both nodes exist
            if self.graph.has_node(r.source) and self.graph.has_node(r.target):
                self.graph.add_edge(
                    r.source, r.target,
                    relation_type=r.relation_type,
                )

    def get_concept(self, concept_id: str) -> dict | None:
        """Get a concept by ID."""
        if self.graph.has_node(concept_id):
            return {"concept_id": concept_id, **self.graph.nodes[concept_id]}
        return None

    def get_neighbors(self, concept_id: str, depth: int = 1) -> list[dict]:
        """Get concepts connected within N hops."""
        if not self.graph.has_node(concept_id):
            return []

        visited = set()
        frontier = {concept_id}

        for _ in range(depth):
            next_frontier = set()
            for node in frontier:
                for neighbor in list(self.graph.successors(node)) + list(self.graph.predecessors(node)):
                    if neighbor not in visited and neighbor != concept_id:
                        next_frontier.add(neighbor)
            visited.update(frontier)
            frontier = next_frontier

        visited.update(frontier)
        visited.discard(concept_id)

        return [
            {"concept_id": n, **self.graph.nodes[n]}
            for n in visited
            if self.graph.has_node(n)
        ]

    def get_subgraph(self, course_id: str | None = None) -> nx.DiGraph:
        """Get subgraph for a course."""
        if course_id is None:
            return self.graph

        nodes = [
            n for n, data in self.graph.nodes(data=True)
            if course_id in data.get("course_ids", [])
        ]
        return self.graph.subgraph(nodes)

    def search_concepts(self, query: str) -> list[dict]:
        """Simple text search over concept names and definitions."""
        query_lower = query.lower()
        results = []
        for node_id, data in self.graph.nodes(data=True):
            name = data.get("name", "").lower()
            definition = data.get("definition", "").lower()
            if query_lower in name or query_lower in definition:
                results.append({"concept_id": node_id, **data})
        return results

    def stats(self) -> dict:
        """Return graph statistics."""
        return {
            "nodes": self.graph.number_of_nodes(),
            "edges": self.graph.number_of_edges(),
            "components": nx.number_weakly_connected_components(self.graph),
        }

    def save(self, path: str | Path):
        """Save graph to JSON."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "nodes": [
                {"id": n, **d} for n, d in self.graph.nodes(data=True)
            ],
            "edges": [
                {"source": u, "target": v, **d}
                for u, v, d in self.graph.edges(data=True)
            ],
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str))

    def load(self, path: str | Path):
        """Load graph from JSON."""
        path = Path(path)
        if not path.exists():
            return

        data = json.loads(path.read_text())
        self.graph = nx.DiGraph()

        for node in data.get("nodes", []):
            node_id = node.pop("id")
            self.graph.add_node(node_id, **node)

        for edge in data.get("edges", []):
            source = edge.pop("source")
            target = edge.pop("target")
            self.graph.add_edge(source, target, **edge)


def topo_sort_topics(
    topic_ids: list[str],
    prereq_edges: list[tuple[str, str]],
    *,
    weights: dict[str, float] | None = None,
) -> list[str]:
    """R3-3: stable topological sort over Stage A topics.

    `prereq_edges` carries (a, b) meaning "a must be studied before b" —
    i.e. an edge a → b in the precedence DAG. Returns topic ids in study
    order. Ties broken by `weights[id]` (higher = earlier) so the heaviest
    topic surfaces first when nothing else constrains the order; among
    equal weights, by `topic_ids` input order so the result is stable for
    fixture-replay tests.

    On a cycle the precedence graph stops being a DAG. Rather than raise,
    we degrade to weight-desc / input-order sort over the remaining ids
    so the caller can still assign learning_order numbers and the mindmap
    renders. The whole feature is best-effort for the student.
    """
    in_topics = list(topic_ids)
    weight_of = dict(weights or {})
    rank = {tid: i for i, tid in enumerate(in_topics)}

    indeg: dict[str, int] = {tid: 0 for tid in in_topics}
    succ: dict[str, list[str]] = {tid: [] for tid in in_topics}
    for src, dst in prereq_edges:
        if src not in indeg or dst not in indeg or src == dst:
            continue
        succ[src].append(dst)
        indeg[dst] += 1

    def _key(tid: str) -> tuple[float, int]:
        return (-float(weight_of.get(tid, 0.0)), rank[tid])

    ready = sorted([tid for tid, d in indeg.items() if d == 0], key=_key)
    out: list[str] = []
    while ready:
        nxt = ready.pop(0)
        out.append(nxt)
        for child in succ[nxt]:
            indeg[child] -= 1
            if indeg[child] == 0:
                ready.append(child)
        ready.sort(key=_key)

    if len(out) != len(in_topics):
        # Cycle — the remaining nodes never reached indeg=0. Sort what's
        # left by weight-desc and append; better degraded order than no
        # learning_order at all.
        leftover = [tid for tid in in_topics if tid not in set(out)]
        leftover.sort(key=_key)
        out.extend(leftover)

    return out


def _merge_source_chunks(left: list[dict], right: list[dict]) -> list[dict]:
    seen = set()
    merged = []
    for item in list(left or []) + list(right or []):
        key = (item.get("chunk_id"), item.get("source_file"), item.get("page"))
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged
