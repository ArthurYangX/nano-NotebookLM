"""R4-3 fix-all v1 regression tests.

Each test pins one fix from the review-swarm v1 round on the d3-force KG
view. A future refactor that reverts the fix lights up CI immediately.

  A1  drag-release no longer double-counts offsets[id] when sim is live
  A3  tick handler is rAF-throttled + childrenByParent Map replaces
      O(N²) parent filter walk in visibleIds
  A8  CDN scripts exact-version-pinned (not floating @3) + crossorigin
  A10 SVG marker IDs scoped per-instance via React.useId
  A11 prepareMindmapForce empty branch returns both `links` and `edges`
  A13 enabledRelations preserves user's disabled chips across re-extract
  A17 CLAUDE.md Maturity Notes append R4-3 paragraph
"""

from __future__ import annotations

import re
import subprocess
import textwrap
from pathlib import Path


def _run_node(script: str) -> str:
    proc = subprocess.run(
        ["node", "-e", script], cwd=".",
        text=True, capture_output=True, check=True,
    )
    return proc.stdout


# ── A1: drag-release in live-sim mode skips offsets dict ─────────────


def _strip_js_comments(src: str) -> str:
    """Remove `// line` and `/* block */` comments so grep tests can
    inspect the executable code without false-positive matches against
    explanatory comment text."""
    no_block = re.sub(r"/\*[\s\S]*?\*/", "", src)
    no_line = re.sub(r"//.*", "", no_block)
    return no_line


def test_drag_handler_skips_offsets_when_sim_is_live():
    """fix-all v1 #A1: when simRef.current is truthy, the move handler
    must NOT call setOffsets — it writes fx/fy on the sim node instead.
    setOffsets is reserved for the d3-unavailable fallback path."""
    src = _strip_js_comments(Path("frontend/mindmap.jsx").read_text(encoding="utf-8"))
    m = re.search(r"} else if \(d\.kind === \"node\"\) \{[\s\S]+?\} else if \(d\.kind === \"connect\"", src)
    assert m, "drag mousemove node branch not found"
    body = m.group(0)
    # simRef branch must come first, setOffsets must follow inside an else.
    sim_pos = body.index("if (simRef.current)")
    set_offsets_pos = body.index("setOffsets")
    assert sim_pos < set_offsets_pos
    # The `} else {` opening of the no-sim branch must appear between
    # the simRef block close and setOffsets.
    between = body[sim_pos:set_offsets_pos]
    assert "} else {" in between, (
        "setOffsets must be guarded by `} else {` after the sim branch; got: " + repr(between)
    )


def test_alphatarget_restart_only_on_mousedown_not_mousemove():
    """fix-all v1 #A4: alphaTarget(0.2).restart() fires once at
    mousedown — per-mousemove restart re-enters the d3 timer and
    multiplies the tick storm. Inside the mousemove `kind === "node"`
    branch, restart() must NOT appear in executable code (comments are
    allowed to reference it for historical context)."""
    src = _strip_js_comments(Path("frontend/mindmap.jsx").read_text(encoding="utf-8"))
    m = re.search(r"} else if \(d\.kind === \"node\"\) \{[\s\S]+?\} else if \(d\.kind === \"connect\"", src)
    assert m
    body = m.group(0)
    assert ".restart()" not in body, "drag mousemove must not call restart()"


# ── A3: rAF-throttled tick + childrenByParent Map ────────────────────


def test_tick_handler_is_raf_throttled():
    """fix-all v1 #A3: tick callback must coalesce setSimNodes through
    requestAnimationFrame (or setTimeout in non-rAF runtimes) so React
    re-renders at most once per frame, not per tick."""
    src = Path("frontend/mindmap.jsx").read_text(encoding="utf-8")
    # The tick subscription line must reference a scheduleFlush helper
    # (or equivalent named function), NOT call setSimNodes inline.
    assert ".on(\"tick\", scheduleFlush)" in src
    assert "requestAnimationFrame" in src
    # The scheduleFlush body must check a pendingRaf guard so multiple
    # ticks coalesce into one rAF.
    m = re.search(r"const scheduleFlush[\s\S]+?\};", src)
    assert m
    body = m.group(0)
    assert "pendingRaf" in body
    assert "if (pendingRaf) return" in body or "pendingRaf &&" in body


def test_children_by_parent_map_replaces_o_n_squared_filter():
    """fix-all v1 #A3: the visibleIds walk previously did
    nodes.filter(n => n.parent === id) recursively — O(N²). Verify the
    code now uses a Map<parentId, child[]> built once per prepared
    payload."""
    src = Path("frontend/mindmap.jsx").read_text(encoding="utf-8")
    assert "childrenByParent" in src
    # Map should be built in a useMemo over prepared.nodes.
    m = re.search(r"const childrenByParent[\s\S]+?\}, \[prepared\.nodes\]\);", src)
    assert m, "childrenByParent must be useMemo'd against prepared.nodes"
    # And the visibleIds walk must use it, not the inline filter.
    vis_m = re.search(r"const visibleIds[\s\S]+?\}, \[nodes, collapsed, childrenByParent\]\);", src)
    assert vis_m, "visibleIds must depend on childrenByParent"
    body = vis_m.group(0)
    # No more `nodes.filter(n => n.parent === id)` parent-walk fragments
    # in the visibleIds memo body.
    assert "nodes.filter(n => n.parent ===" not in body


# ── A8: CDN exact-version pins + crossorigin ─────────────────────────


def test_d3_cdn_scripts_pin_exact_versions():
    """fix-all v1 #A8: floating @3 major-only tags replaced with exact
    `@3.x.y` pins so the CDN can't serve a supply-chain-attacked patch."""
    html = Path("frontend/index.html").read_text(encoding="utf-8")
    for pkg in ["d3-dispatch", "d3-quadtree", "d3-timer", "d3-force"]:
        # Match the URL fragment immediately following the pkg name.
        m = re.search(rf"cdn\.jsdelivr\.net/npm/{re.escape(pkg)}@(\d+\.\d+\.\d+)", html)
        assert m, f"{pkg} should be pinned at exact x.y.z, got: {html[html.find(pkg):html.find(pkg)+80]!r}"
    # And the floating `@3` (major-only) pattern must be GONE for d3.
    assert not re.search(r"d3-(?:dispatch|quadtree|timer|force)@3(?!\.\d)", html), (
        "floating d3-*@3 major-only pins must be replaced with @x.y.z"
    )


def test_d3_cdn_scripts_set_crossorigin():
    """fix-all v1 #A8: crossorigin so script errors propagate to the
    page (matches the React/Babel CDN line posture)."""
    html = Path("frontend/index.html").read_text(encoding="utf-8")
    for pkg in ["d3-dispatch", "d3-quadtree", "d3-timer", "d3-force"]:
        # Find the entire <script> tag for this pkg and confirm crossorigin.
        m = re.search(rf'<script src="https://cdn\.jsdelivr\.net/npm/{re.escape(pkg)}@[^"]+"[^>]*>', html)
        assert m, f"{pkg} script tag missing"
        tag = m.group(0)
        assert 'crossorigin=' in tag, f"{pkg} script tag missing crossorigin: {tag}"


# ── A10: per-instance marker IDs via useId ───────────────────────────


def test_svg_marker_ids_scoped_per_instance():
    """fix-all v1 #A10: marker ids must be derived from React.useId so
    two co-mounted <MindMap> instances don't collide on document-global
    <marker> ids."""
    src = Path("frontend/mindmap.jsx").read_text(encoding="utf-8")
    # useId hook is imported / aliased.
    assert "useId" in src
    # arrowId helper exists.
    assert "const arrowId = (kind)" in src or "arrowId(\"prereq\")" in src
    # The literal kg-arrow-prereq/depends/related as a fixed id is gone.
    # (The substring may still appear inside the helper as a prefix.)
    assert 'id="kg-arrow-prereq"' not in src
    assert 'id="kg-arrow-depends"' not in src
    assert 'id="kg-arrow-related"' not in src
    # And url(#kg-arrow-*) static references gone too.
    assert '"url(#kg-arrow-prereq)"' not in src
    assert '"url(#kg-arrow-depends)"' not in src
    assert '"url(#kg-arrow-related)"' not in src


# ── A11: empty-force shape has both links AND edges keys ─────────────


def test_prepare_mindmap_force_empty_branch_has_links_and_edges():
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const layout = h.prepareMindmapForce({nodes: [], edges: []});
        if (!layout.empty) throw new Error('expected empty true');
        if (!Array.isArray(layout.links)) throw new Error('missing links');
        if (!Array.isArray(layout.edges)) throw new Error('missing edges on empty branch');
        if (!Array.isArray(layout.relationTypes)) throw new Error('missing relationTypes');
        console.log('ok');
        """
    )
    assert _run_node(script).strip() == "ok"


# ── A13: enabledRelations preserves user's disabled chips ────────────


def test_enabled_relations_preserves_disabled_chips_on_re_extract():
    """fix-all v1 #A13: useEffect that syncs enabledRelations to
    relationTypes must MERGE — keep previously-disabled chips disabled
    when new relations arrive. The previous implementation reset to
    all-enabled every time."""
    src = Path("frontend/mindmap.jsx").read_text(encoding="utf-8")
    m = re.search(r"setEnabledRelations\(prev =>[\s\S]+?\}\);[\s\S]+?\}, \[relationTypes\.join", src)
    assert m, "enabledRelations sync must use functional updater that reads prev"
    body = m.group(0)
    # The merge logic must reference prev and the new relationTypes list.
    assert "prev.has(r)" in body or "prev.forEach" in body
    # The naive `new Set(relationTypes)` reset is gone from the body.
    assert "setEnabledRelations(new Set(relationTypes))" not in src


# ── A17: CLAUDE.md Maturity Notes mention R4-3 ───────────────────────


def test_claude_md_documents_r4_3():
    md = Path("CLAUDE.md").read_text(encoding="utf-8")
    assert "Mind map R4-3" in md
    assert "prepareMindmapForce" in md
    assert "d3-force" in md
