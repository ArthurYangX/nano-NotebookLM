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
        """Add concepts as nodes."""
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
