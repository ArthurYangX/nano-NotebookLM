"""M2 — mindmap layout (parent-aware tree, course-card root, topic hues).

The pre-M2 layout placed sibling nodes by array index on a uniform circle,
which made the "tree" visually chaotic — children of one topic were
scattered across the whole canvas instead of clustering near the topic.

These tests pin the new contract:
  - root (depth=0) sits at the origin
  - depth=1 children (topics) get distinct HSL hues
  - within each topic's angular slice, all of that topic's children sit
  - long single-child chains don't overlap (each depth shifts radially)
  - legacy KG payloads without an explicit depth=0 root still render
"""

from __future__ import annotations

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


# Helper template that builds a 1-root + N-topics + M-leaves-per-topic KG.
def _build_kg_js(num_topics: int, leaves_per_topic: int) -> str:
    return textwrap.dedent(
        f"""
        const kg = {{nodes: [], edges: []}};
        kg.nodes.push({{
          id: 'root', name: 'TestCourse · 6 docs', depth: 0,
          concept_type: 'root', weight: 10,
          definition: 'A course about X.',
        }});
        for (let t = 0; t < {num_topics}; t++) {{
          const tid = 'topic_' + t;
          kg.nodes.push({{
            id: tid, name: 'Topic ' + t, depth: 1,
            concept_type: 'topic', weight: 5,
            definition: 'Topic ' + t + ' summary',
          }});
          kg.edges.push({{source: tid, target: 'root', relation: 'part-of'}});
          for (let l = 0; l < {leaves_per_topic}; l++) {{
            const lid = 'leaf_' + t + '_' + l;
            kg.nodes.push({{
              id: lid, name: 'Leaf ' + t + '/' + l, depth: 2,
              concept_type: 'definition', weight: 2,
              source_chunks: [{{source_file: 'a.pdf', page: 1, chunk_id: 'c'+t+l}}],
            }});
            kg.edges.push({{source: lid, target: tid, relation: 'part-of'}});
          }}
        }}
        """
    )


def test_layout_root_at_origin():
    """Mini: root (depth=0) ends up at (0, 0)."""
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        """
    ) + _build_kg_js(3, 4) + textwrap.dedent(
        """
        const layout = h.prepareMindmap(kg, {layout: 'radial'});
        const root = layout.nodes.find(n => n.id === 'root');
        if (!root) throw new Error('root node missing');
        if (Math.abs(root.x) > 1e-6 || Math.abs(root.y) > 1e-6) {
          throw new Error('root not at origin: ' + JSON.stringify({x: root.x, y: root.y}));
        }
        if (root.kind !== 'root') throw new Error('root kind wrong: ' + root.kind);
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_layout_topic_subtree_slices_do_not_collide():
    """Mini: each topic owns an angular slice; all of its leaves' bearings
    from origin sit within that slice, never scattering into another topic's
    territory. Pin the algorithm: children cluster near their parent."""
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        """
    ) + _build_kg_js(4, 5) + textwrap.dedent(
        """
        const layout = h.prepareMindmap(kg, {layout: 'radial'});
        function bearing(n) { return Math.atan2(n.y, n.x); }
        // Group leaves by parent (topic id).
        const groups = {};
        for (const n of layout.nodes) {
          if (n.id.startsWith('leaf_')) {
            (groups[n.parent] = groups[n.parent] || []).push(n);
          }
        }
        const topicCount = Object.keys(groups).length;
        if (topicCount !== 4) throw new Error('expected 4 topic groups, got ' + topicCount);
        // For each topic, the angular range its leaves occupy must be strictly
        // smaller than 2π/topicCount (so neighboring topics' leaves don't mix).
        const sliceLimit = (2 * Math.PI) / 4;
        for (const tid of Object.keys(groups)) {
          const bearings = groups[tid].map(bearing);
          // Wrap-aware range: shift so min is at 0
          bearings.sort((a, b) => a - b);
          let maxGap = 0;
          for (let i = 0; i < bearings.length; i++) {
            const gap = i === bearings.length - 1
              ? (bearings[0] + 2*Math.PI - bearings[i])
              : (bearings[i+1] - bearings[i]);
            if (gap > maxGap) maxGap = gap;
          }
          const span = 2*Math.PI - maxGap;
          if (span > sliceLimit) {
            throw new Error('topic ' + tid + ' leaves span ' + span.toFixed(3) +
                            ' > slice ' + sliceLimit.toFixed(3));
          }
        }
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_layout_topics_get_distinct_hues():
    """Mini: each depth=1 topic node gets a hue, and 4 topics all distinct."""
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        """
    ) + _build_kg_js(4, 2) + textwrap.dedent(
        """
        const layout = h.prepareMindmap(kg, {layout: 'radial'});
        const topicHues = layout.nodes
          .filter(n => n.id.startsWith('topic_'))
          .map(n => n.style && n.style.hue);
        if (topicHues.length !== 4) throw new Error('missing topic nodes');
        if (topicHues.some(h => h === null || h === undefined)) {
          throw new Error('topic missing hue: ' + JSON.stringify(topicHues));
        }
        const uniq = new Set(topicHues);
        if (uniq.size !== 4) throw new Error('hues collide: ' + JSON.stringify(topicHues));
        // Leaves must inherit their topic's hue.
        for (const n of layout.nodes) {
          if (!n.id.startsWith('leaf_')) continue;
          const parentTid = n.parent;
          const parent = layout.nodes.find(x => x.id === parentTid);
          if (!parent) throw new Error('orphan leaf ' + n.id);
          if (n.style.hue !== parent.style.hue) {
            throw new Error('leaf ' + n.id + ' hue ' + n.style.hue +
                            ' != topic hue ' + parent.style.hue);
          }
        }
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_layout_long_chain_no_overlap():
    """Corner: a single deep chain (root → a → b → c → d) — each child
    sits at a strictly larger radius than its parent so they don't overlap.
    """
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const kg = {nodes: [], edges: []};
        kg.nodes.push({id: 'root', name: 'C', depth: 0, concept_type: 'root', weight: 10});
        let prev = 'root';
        for (const x of ['a', 'b', 'c', 'd']) {
          kg.nodes.push({id: x, name: x, depth: kg.nodes.length, concept_type: 'definition', weight: 2});
          kg.edges.push({source: x, target: prev, relation: 'part-of'});
          prev = x;
        }
        const layout = h.prepareMindmap(kg, {layout: 'radial'});
        const byId = {};
        for (const n of layout.nodes) byId[n.id] = n;
        function r(n) { return Math.hypot(n.x, n.y); }
        const order = ['root', 'a', 'b', 'c', 'd'];
        for (let i = 1; i < order.length; i++) {
          if (!(r(byId[order[i]]) > r(byId[order[i-1]]) + 1)) {
            throw new Error('depth ' + i + ' not farther: r=' +
                            r(byId[order[i]]).toFixed(1) + ' vs parent ' +
                            r(byId[order[i-1]]).toFixed(1));
          }
        }
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_layout_legacy_payload_without_explicit_root():
    """Corner: pre-M1 KG has no depth=0 node (Round 1 graphs already on disk).
    prepareMindmap must still return a usable layout, falling back to the
    first node or the highest-weight depth=1 node as virtual root."""
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const kg = {nodes: [], edges: []};
        for (let i = 0; i < 5; i++) {
          kg.nodes.push({id: 'n'+i, name: 'N'+i, depth: 1, concept_type: 'definition', weight: i+1});
        }
        for (let i = 1; i < 5; i++) kg.edges.push({source: 'n'+i, target: 'n0', relation: 'part-of'});
        const layout = h.prepareMindmap(kg, {layout: 'radial'});
        if (layout.empty) throw new Error('legacy KG should not return empty');
        if (layout.nodes.length !== 5) throw new Error('expected 5 nodes, got ' + layout.nodes.length);
        // Some node ends up at origin acting as root.
        const origin = layout.nodes.filter(n => Math.abs(n.x) < 1e-6 && Math.abs(n.y) < 1e-6);
        if (origin.length !== 1) {
          throw new Error('expected exactly one node at origin, got ' + origin.length);
        }
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_layout_preserves_existing_30node_contract():
    """Regression guard for tests/test_frontend_helpers.py::test_mindmap_layout_happy.

    Pin: 30-node legacy KG (n0 depth=0 with depends-on edges to n1..n29)
    still produces 30 nodes and weight→fontSize ordering."""
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const kg = {nodes: [], edges: []};
        for (let i = 0; i < 30; i++) {
          kg.nodes.push({id:'n'+i, name:'Node '+i, depth: i === 0 ? 0 : 1,
                         weight: i + 1,
                         source_chunks:[{source_file:'ml.pdf', page:1, chunk_id:'c'+i}]});
        }
        for (let i = 1; i < 30; i++) kg.edges.push({source:'n0', target:'n'+i, relation:'depends-on'});
        const layout = h.prepareMindmap(kg, {layout:'radial'});
        if (layout.nodes.length !== 30) throw new Error('lost nodes');
        if (!(layout.nodes[29].style.fontSize > layout.nodes[1].style.fontSize)) {
          throw new Error('weight should affect font size');
        }
        if (!h.getMindmapNodeDetail(layout, 'n12').source_chunks[0].chunk_id) {
          throw new Error('detail must surface source chunks');
        }
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"
