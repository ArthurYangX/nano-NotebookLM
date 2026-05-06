"""Knowledge graph visualization — Mermaid and JSON export."""

from __future__ import annotations

import json

import networkx as nx

from nano_notebooklm.kg.graph import KnowledgeGraph


def to_mermaid(kg: KnowledgeGraph, course_id: str | None = None, max_nodes: int = 50) -> str:
    """Export knowledge graph as a Mermaid diagram string."""
    subgraph = kg.get_subgraph(course_id)

    # Limit nodes for readability
    nodes = list(subgraph.nodes(data=True))[:max_nodes]
    node_ids = {n[0] for n in nodes}

    lines = ["graph TD"]

    # Add nodes
    for node_id, data in nodes:
        name = data.get("name", node_id).replace('"', "'")
        concept_type = data.get("concept_type", "definition")
        shape = _mermaid_shape(concept_type)
        lines.append(f'    {_safe_id(node_id)}{shape[0]}"{name}"{shape[1]}')

    # Add edges
    for u, v, data in subgraph.edges(data=True):
        if u in node_ids and v in node_ids:
            rel = data.get("relation_type", "related")
            lines.append(f"    {_safe_id(u)} -->|{rel}| {_safe_id(v)}")

    return "\n".join(lines)


def to_d3_json(kg: KnowledgeGraph, course_id: str | None = None) -> dict:
    """Export as D3.js-compatible JSON for interactive visualization."""
    subgraph = kg.get_subgraph(course_id)

    nodes = []
    for node_id, data in subgraph.nodes(data=True):
        nodes.append({
            "id": node_id,
            "name": data.get("name", ""),
            "definition": data.get("definition", ""),
            "type": data.get("concept_type", "definition"),
            "group": data.get("course_ids", [""])[0] if data.get("course_ids") else "",
        })

    links = []
    for u, v, data in subgraph.edges(data=True):
        links.append({
            "source": u,
            "target": v,
            "type": data.get("relation_type", "related"),
        })

    return {"nodes": nodes, "links": links}


def _mermaid_shape(concept_type: str) -> tuple[str, str]:
    """Return Mermaid shape delimiters based on concept type."""
    shapes = {
        "definition": ("[", "]"),
        "theorem": ("((", "))"),
        "algorithm": ("[[", "]]"),
        "example": ("(", ")"),
    }
    return shapes.get(concept_type, ("[", "]"))


def _safe_id(node_id: str) -> str:
    """Make a node ID safe for Mermaid."""
    import re
    return re.sub(r"[^a-zA-Z0-9_]", "_", node_id)
