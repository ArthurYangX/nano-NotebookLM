"""M1 — `_kg_to_mindmap` payload shape contract.

The new KG persists an explicit course-root node (depth=0) plus topic nodes
(depth=1) plus concept leaves (depth>=2). The mindmap payload builder must:

  - use the depth=0 node as the tree root (not the legacy "in-degree=0"
    heuristic, which routinely picked orphan leaves)
  - surface course overview / doc count on the root for the course-card
    rendering in the new frontend
  - still degrade gracefully for legacy KG payloads that don't carry an
    explicit depth=0 root (Round 1 graphs already on disk)
"""

from __future__ import annotations


def test_kg_to_mindmap_uses_explicit_depth_zero_root():
    """Mini: depth=0 root present → tree root is that node, label = its name."""
    from api.server import _kg_to_mindmap

    kg_data = {
        "nodes": [
            {"id": "root_X", "name": "X · 12 docs",
             "depth": 0, "concept_type": "root", "definition": "Course about X."},
            {"id": "topic_X_a", "name": "Topic A", "depth": 1, "concept_type": "topic",
             "weight": 5, "definition": "Topic A summary."},
            {"id": "topic_X_b", "name": "Topic B", "depth": 1, "concept_type": "topic",
             "weight": 4, "definition": "Topic B summary."},
            {"id": "concept_X_1", "name": "Concept 1", "depth": 2, "weight": 2,
             "concept_type": "definition"},
        ],
        "edges": [
            {"source": "topic_X_a", "target": "root_X", "relation": "part-of"},
            {"source": "topic_X_b", "target": "root_X", "relation": "part-of"},
            {"source": "concept_X_1", "target": "topic_X_a", "relation": "part-of"},
        ],
    }
    result = _kg_to_mindmap(kg_data, "X")

    assert result["id"] == "root_X"
    assert result["label"] == "X · 12 docs"
    assert result.get("definition") == "Course about X."
    # nodes/edges payload must be preserved unaltered for prepareMindmap()
    assert len(result["nodes"]) == 4
    assert len(result["edges"]) == 3


def test_kg_to_mindmap_degrades_for_legacy_kg_without_root():
    """Corner: legacy KG (Round 1 graphs already on disk) lack a depth=0
    root. The builder must still return a usable payload using the course
    id as label, never crash, never pick an arbitrary orphan."""
    from api.server import _kg_to_mindmap

    kg_data = {
        "nodes": [
            {"id": "n1", "name": "N1", "weight": 5, "depth": 1},
            {"id": "n2", "name": "N2", "weight": 4, "depth": 2},
        ],
        "edges": [{"source": "n1", "target": "n2", "relation": "part-of"}],
    }
    result = _kg_to_mindmap(kg_data, "LegacyCourse")
    assert result["label"] == "LegacyCourse"
    # Whether builder synthesizes a virtual root or returns id="root", the
    # nodes/edges still need to be there for prepareMindmap to render them.
    assert len(result["nodes"]) == 2


def test_kg_to_mindmap_empty_returns_placeholder_shape():
    """Corner: zero-node KG (e.g. 模式识别 hit a Stage A failure path).
    Must return the empty-shape contract the frontend relies on."""
    from api.server import _kg_to_mindmap

    result = _kg_to_mindmap({"nodes": [], "edges": []}, "EmptyCourse")
    assert result["nodes"] == []
    assert result["edges"] == []
    assert result.get("children", []) == []
    assert result["label"] == "EmptyCourse"


# ── F4 — legacy KG fallback root must pick a real "parent" ──────────


def test_kg_to_mindmap_legacy_fallback_root_picks_high_part_of_outdegree():
    """F4 mini: pre-M1 KG (CS231N-shaped: no depth=0 + concept_type=root,
    relations all part_of with source=child target=parent) must NOT pick
    a leaf as the radial center. The fallback must prefer the node that
    most other nodes attach to (highest inbound part-of count = real
    'parent' in the schema)."""
    from api.server import _kg_to_mindmap

    # Simulate a slice of the actual CS231N graph:
    # `data_parallelism`, `fsdp`, `activation_checkpointing` all attach
    # via part_of to `scaling_recipe`. `scaling_recipe` is the real root.
    kg_data = {
        "nodes": [
            {"id": "scaling_recipe", "name": "Scaling recipe", "weight": 5,
             "concept_type": "definition"},
            {"id": "data_parallelism", "name": "Data parallelism", "weight": 3,
             "concept_type": "definition"},
            {"id": "fsdp", "name": "FSDP", "weight": 3,
             "concept_type": "definition"},
            {"id": "activation_checkpointing", "name": "Activation ckpt", "weight": 3,
             "concept_type": "definition"},
            {"id": "stray_leaf", "name": "Stray leaf", "weight": 1,
             "concept_type": "definition"},
        ],
        "edges": [
            {"source": "data_parallelism", "target": "scaling_recipe", "relation": "part-of"},
            {"source": "fsdp", "target": "scaling_recipe", "relation": "part-of"},
            {"source": "activation_checkpointing", "target": "scaling_recipe", "relation": "part-of"},
        ],
    }
    result = _kg_to_mindmap(kg_data, "CS231N")
    # The actual radial center used by the frontend: prefer scaling_recipe
    # (3 inbound part-of) over stray_leaf or any of the children.
    assert result["id"] == "scaling_recipe", (
        f"legacy fallback picked {result['id']!r}; expected scaling_recipe"
    )


def test_kg_to_mindmap_legacy_fallback_no_part_of_ties_breaks_by_weight():
    """F4 corner: legacy KG with NO part-of edges (only related/depends-on)
    must still produce a sensible root — fall back to highest-weight
    node, never an arbitrary list-order pick."""
    from api.server import _kg_to_mindmap

    kg_data = {
        "nodes": [
            {"id": "low", "name": "Low", "weight": 1, "concept_type": "definition"},
            {"id": "heavy", "name": "Heavy", "weight": 9, "concept_type": "definition"},
            {"id": "mid", "name": "Mid", "weight": 4, "concept_type": "definition"},
        ],
        "edges": [
            {"source": "low", "target": "heavy", "relation": "related"},
        ],
    }
    result = _kg_to_mindmap(kg_data, "X")
    assert result["id"] == "heavy"


# ── F15 — explicit-root branch tolerates depth=0 OR concept_type=root ─


def test_kg_to_mindmap_accepts_root_by_depth_alone():
    """F15: a node with depth=0 should be honored as root even if
    concept_type was set to something else by an earlier migration."""
    from api.server import _kg_to_mindmap

    kg_data = {
        "nodes": [
            {"id": "course", "name": "Course X", "depth": 0, "concept_type": "course",
             "weight": 10, "definition": "intro"},
            {"id": "t1", "name": "Topic 1", "depth": 1, "concept_type": "topic",
             "weight": 5},
        ],
        "edges": [{"source": "t1", "target": "course", "relation": "part-of"}],
    }
    result = _kg_to_mindmap(kg_data, "X")
    assert result["id"] == "course"
    assert result["label"] == "Course X"


def test_kg_to_mindmap_accepts_root_by_concept_type_alone():
    """F15: a node with concept_type='root' but depth not explicitly 0
    must also be honored."""
    from api.server import _kg_to_mindmap

    kg_data = {
        "nodes": [
            {"id": "rooty", "name": "Course X", "concept_type": "root",
             "weight": 10, "definition": "intro"},
            {"id": "t1", "name": "Topic 1", "depth": 1, "concept_type": "topic"},
        ],
        "edges": [{"source": "t1", "target": "rooty", "relation": "part-of"}],
    }
    result = _kg_to_mindmap(kg_data, "X")
    assert result["id"] == "rooty"
