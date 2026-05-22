"""Round 4 #R4-3 — force-directed KG view frontend contracts."""

from __future__ import annotations

from pathlib import Path
import subprocess
import textwrap


def run_node(script: str) -> str:
    proc = subprocess.run(
        ["node", "-e", script],
        cwd=".",
        text=True,
        capture_output=True,
        check=True,
    )
    return proc.stdout


def test_prepare_mindmap_force_returns_node_link_shape():
    """Mini: force helper returns d3-ready nodes + links without changing the
    backend KG payload contract."""
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const kg = {
          nodes: [
            {id:'root', name:'Course', depth:0, concept_type:'root', weight:10},
            {id:'topic', name:'Topic', depth:1, concept_type:'topic', weight:5},
            {id:'leaf', name:'Leaf', depth:2, concept_type:'definition', weight:2}
          ],
          edges: [
            {source:'topic', target:'root', relation:'part-of'},
            {source:'leaf', target:'topic', relation:'depends-on'}
          ]
        };
        const layout = h.prepareMindmapForce(kg, {layout:'force'});
        if (layout.empty) throw new Error('layout should not be empty');
        if (!Array.isArray(layout.nodes) || layout.nodes.length !== 3) throw new Error('bad nodes');
        if (!Array.isArray(layout.links) || layout.links.length !== 2) throw new Error('bad links');
        if (layout.links[0].source !== 'topic' || layout.links[0].target !== 'root') {
          throw new Error('source/target not preserved: ' + JSON.stringify(layout.links[0]));
        }
        if (!layout.relationTypes.includes('part-of') || !layout.relationTypes.includes('depends-on')) {
          throw new Error('missing relation types: ' + JSON.stringify(layout.relationTypes));
        }
        if (!Number.isFinite(layout.nodes[0].x) || !Number.isFinite(layout.nodes[0].y)) {
          throw new Error('node missing initial position');
        }
        if (typeof h.prepareMindmapTree !== 'function' || typeof h.prepareMindmap !== 'function') {
          throw new Error('tree compatibility helpers missing');
        }
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_prepare_mindmap_force_handles_100_nodes():
    """Corner: 100+ nodes must produce a bounded node-link shape synchronously;
    simulation remains a React/d3 runtime concern."""
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const kg = {nodes: [{id:'root', name:'Course', depth:0, concept_type:'root', weight:10}], edges: []};
        for (let i = 0; i < 120; i++) {
          const tid = 'n' + i;
          kg.nodes.push({id: tid, name: 'Node ' + i, depth: i < 8 ? 1 : 2, concept_type: i < 8 ? 'topic' : 'definition', weight: 1 + (i % 9)});
          kg.edges.push({source: tid, target: i < 8 ? 'root' : 'n' + (i % 8), relation: i % 3 === 0 ? 'depends-on' : 'part-of'});
        }
        const layout = h.prepareMindmapForce(kg);
        if (layout.nodes.length !== 121) throw new Error('missing nodes: ' + layout.nodes.length);
        if (layout.links.length !== 120) throw new Error('missing links: ' + layout.links.length);
        if (layout.nodes.some(n => !Number.isFinite(n.x) || !Number.isFinite(n.y))) {
          throw new Error('non-finite coordinates');
        }
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_prepare_mindmap_force_keeps_cross_relations_out_of_parent_tree():
    """Corner: non-hierarchical KG edges must remain links, not collapse/tree
    parents. Otherwise force view hides or toggles cross-topic nodes as if
    they were children."""
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const kg = {
          nodes: [
            {id:'root', name:'Course', depth:0, concept_type:'root', weight:10},
            {id:'topic_a', name:'Topic A', depth:1, concept_type:'topic', weight:5},
            {id:'topic_b', name:'Topic B', depth:1, concept_type:'topic', weight:4},
            {id:'orphan', name:'Orphan Concept', depth:2, concept_type:'definition', weight:2}
          ],
          edges: [
            {source:'topic_a', target:'root', relation:'part-of'},
            {source:'topic_b', target:'topic_a', relation:'depends-on'},
            {source:'orphan', target:'topic_b', relation:'related'}
          ]
        };
        const layout = h.prepareMindmapForce(kg);
        const byId = Object.fromEntries(layout.nodes.map(n => [n.id, n]));
        if (byId.topic_a.parent !== 'root') throw new Error('part-of parent missing');
        if (byId.topic_b.parent !== null) throw new Error('depends-on became parent: ' + byId.topic_b.parent);
        if (byId.orphan.parent !== null) throw new Error('related became parent: ' + byId.orphan.parent);
        if (layout.links.length !== 3) throw new Error('all relations must remain links');
        if (!layout.relationTypes.includes('related')) throw new Error('related relation missing');
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_mindmap_jsx_uses_force_layout_grep():
    """Mini: MindMap uses the force helper and d3 force simulation path."""
    src = Path("frontend/mindmap.jsx").read_text(encoding="utf-8")
    assert "StudyState.prepareMindmapForce" in src
    assert "d3.forceSimulation" in src
    assert "forceLink" in src
    assert "kg-edge-label" in src


def test_relation_filter_zero_edges_renders_isolated_nodes():
    """Corner: relation filters can hide every edge while nodes remain visible
    and a status chip tells the student why links disappeared."""
    src = Path("frontend/mindmap.jsx").read_text(encoding="utf-8")
    assert "kg-relation-filter" in src
    assert "enabledRelations" in src
    assert "allRelationsFiltered" in src
    # i18n refactor (commit db0c7a8): the chip copy moved from inline
    # Chinese to a t() key. Pin the literal t()-callsite shape (not just
    # `'mindmap.filtered_all' in src`, which would pass for a stray
    # comment or console.log too) and verify the localized string lives
    # in i18n.js.
    import re
    assert re.search(r't\(\s*["\']mindmap\.filtered_all["\']', src), \
        "mindmap.filtered_all must be invoked via t(...) in mindmap.jsx"
    i18n_src = Path("frontend/i18n.js").read_text(encoding="utf-8")
    assert '"mindmap.filtered_all"' in i18n_src
    assert "已过滤所有关系" in i18n_src
    assert "visNodes.map" in src


def test_force_view_visibility_starts_from_all_nodes():
    """Corner: KG view must not start from root tree reachability, because
    disconnected KG components and cross-relation-only nodes are valid graph
    content."""
    src = Path("frontend/mindmap.jsx").read_text(encoding="utf-8")
    assert "const vis = new Set(nodes.map(n => n.id))" in src
    assert "if (vis.size <= 1 && nodes.length > vis.size)" not in src


def test_r3_3_edit_affordances_still_grepable():
    """Corner: R4-3 must not regress M3/R3-3 edit and deep-dive affordances."""
    src = Path("frontend/mindmap.jsx").read_text(encoding="utf-8")
    required = [
        "function commitOps",
        "onDoubleClick",
        "addChildOf(selectedId)",
        "deleteNodeWithConfirm(selectedId)",
        "e.shiftKey",
        "setPendingEdge",
        "e.altKey",
        "requestNodeDeepDive",
        "function NodeDeepDivePanel",
        "data-node-id",
        'closest(".mm-node")',
        'getAttribute("data-node-id")',
        "setPendingEdge({ source: d.id, target: targetId })",
    ]
    missing = [needle for needle in required if needle not in src]
    assert not missing, missing


def test_index_html_loads_d3_force_cdn():
    """Mini: CDN app has d3-force before Babel components run."""
    html = Path("frontend/index.html").read_text(encoding="utf-8")
    for dep in ["d3-dispatch@3", "d3-quadtree@3", "d3-timer@3"]:
        assert dep in html
        assert html.index(dep) < html.index("d3-force@3")
    assert "https://cdn.jsdelivr.net/npm/d3-force@3" in html
    assert html.index("d3-force@3") < html.index("type=\"text/babel\" src=\"/static/mindmap.jsx\"")


def test_styles_append_kg_edge_rules():
    """Mini: relation-specific edge classes exist for reviewer-visible styling."""
    css = Path("frontend/styles.css").read_text(encoding="utf-8")
    for selector in [
        ".kg-edge-part-of",
        ".kg-edge-prereq",
        ".kg-edge-depends",
        ".kg-edge-related",
        ".kg-edge-label",
        ".kg-filter-empty",
    ]:
      assert selector in css
