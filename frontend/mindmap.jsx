/* global React, MINDMAP, StudyState, API, d3 */
const { useMemo: useMemoM, useState: useStateM, useRef: useRefM, useEffect: useEffectM, useId: useIdM } = React;

// review-swarm fix-all v1 #2 + #4: unified zoom range so toolbar
// buttons and trackpad pinch share the same clamp. Previously toolbar
// was [0.5, 2] and wheel was [0.3, 3] — after pinching to 0.37 the
// toolbar `−` would jump up to 0.5, surprising the user.
const KG_ZOOM_MIN = 0.3;
const KG_ZOOM_MAX = 3;

// M2 (2026-05-06): the dead-code radial layout that lived here pre-M2 has
// been removed. `StudyState.prepareMindmap` is now a real parent-aware
// recursive radial layout — there's only one code path.
//
// M3 (2026-05-06): the mindmap is now editable. Interactions:
//   - dblclick a node      → inline-edit its label (Enter to save, Esc to cancel)
//   - select + N           → create a new child node under the selected one
//   - select + Delete/Backspace → remove the node (with confirm)
//   - shift+drag from a node onto another → create an edge (relation popup)
// Edits persist to artifacts/courses/<cid>/mindmap_edits.json on the server
// via /api/mindmap/<cid>/edit, and replay on every GET so re-extraction
// doesn't clobber student work.
//
// R4-3 (2026-05-10): the same KG payload now renders as a force-directed
// node-link graph with relation labels and relation filters. The edit and
// deep-dive affordances remain on the same DOM nodes.

function MindMap({ data, layout, courseId, highlightedId, onNodeClick, onSourceClick, onPractice, onDataChange }) {
  const t = useT();
  // fix-all v1 #A10: per-instance marker IDs so two MindMap subtrees
  // (e.g. course-compare view, split-pane) don't fight over global
  // SVG <marker> ids. React.useId returns a stable, unique identifier
  // per component instance.
  const markerUid = useIdM().replace(/:/g, "_");
  const arrowId = (kind) => `kg-arrow-${kind}-${markerUid}`;
  const [pan, setPan] = useStateM({ x: 0, y: 0 });
  const [zoom, setZoom] = useStateM(1);
  // review-swarm fix-all v2 #2: refs mirror zoom/pan so the wheel
  // handler can read current values synchronously without depending on
  // React state closures. Updated in an effect after each render.
  // Earlier the wheel handler used `setZoom(prev => { setPan(...); ... })`
  // — calling another setter inside an updater function is brittle
  // (works in production React 18 batching, but ambiguous under
  // StrictMode dev double-invoke). The ref-mirror pattern lets us do
  // both math AND both setters sequentially at the top level.
  const zoomRef = useRefM(1);
  const panRef = useRefM({ x: 0, y: 0 });
  useEffectM(() => { zoomRef.current = zoom; }, [zoom]);
  useEffectM(() => { panRef.current = pan; }, [pan]);
  const [collapsed, setCollapsed] = useStateM(new Set());
  // 2026-05-11: ref on the wrap div + a persisted hide flag for the
  // bottom-right legend. Trackpad pinch fires `wheel` with `ctrlKey:
  // true` on macOS; we hook a non-passive wheel listener so we can
  // preventDefault() and zoom/pan around the cursor instead of letting
  // the browser scroll the page.
  const wrapRef = useRefM(null);
  // review-swarm fix-all v1 #1: transient flag set during pinch/pan so
  // the 200ms transform transition is suppressed while wheel events are
  // firing. Without this the rendered transform chases the latest
  // setZoom/setPan with 200ms ease, visibly desyncing the cursor anchor.
  const isWheelingRef = useRefM(false);
  const wheelEndTimerRef = useRefM(null);
  const [, bumpWheel] = useStateM(0);
  // review-swarm fix-all v2 #1: cached bounding-client-rect, refreshed
  // via ResizeObserver + window resize/scroll instead of stat'd on every
  // wheel event. Eliminates a per-event forced reflow when wheel runs
  // adjacent to DOM writes (the inline transform style is rewritten on
  // every setPan/setZoom). Fallback to live getBoundingClientRect when
  // the cache is uninitialised.
  const rectRef = useRefM(null);
  // review-swarm fix-all v2 #3: gate wheel capture when the graph is
  // empty (no nodes rendered). An empty-KG placeholder shouldn't trap
  // page scroll. Read via ref so the [] dep array on the wheel effect
  // stays valid (no re-attach on data change).
  const isEmptyRef = useRefM(false);
  const [legendHidden, setLegendHidden] = useStateM(() => {
    try { return window.localStorage.getItem("nano-nlm:v1:kg-legend-hidden") === "1"; }
    catch (e) { return false; }
  });
  function toggleLegend() {
    setLegendHidden(prev => {
      const next = !prev;
      try { window.localStorage.setItem("nano-nlm:v1:kg-legend-hidden", next ? "1" : "0"); }
      catch (e) {}
      return next;
    });
  }
  // user-applied per-node offsets: { [id]: {dx, dy} }
  const [offsets, setOffsets] = useStateM({});
  // in-progress drag state
  const dragRef = useRefM(null); // {kind: 'pan'|'node'|'connect', ...}
  const [, forceRerender] = useStateM(0);
  // M3 — selection + edit + connect drag
  const [selectedId, setSelectedId] = useStateM(null);
  const [editingId, setEditingId] = useStateM(null);
  const [editingLabel, setEditingLabel] = useStateM("");
  // Pending connect drag: cursor coords in graph space.
  const [connectDrag, setConnectDrag] = useStateM(null);
  // Edge relation picker after a successful connect drop.
  const [pendingEdge, setPendingEdge] = useStateM(null);
  // F8: surface POST /edit failures + skipped ops to the user. Cleared
  // when a subsequent commit succeeds with no skipped ops.
  const [syncError, setSyncError] = useStateM(null);
  // fix-all v4 #B7: coalesce rapid resync requests so a sequence of
  // skipped/failed commitOps doesn't fan out to multiple GET
  // /api/mindmap calls that pile up behind the per-course generation
  // lock on the server side.
  const resyncRef = useRefM({ inflight: false, queued: false });
  // R3-3: alt+click on a node opens a side panel that streams a 5-line
  // explanation + 3 mini-quiz from /api/mindmap/{cid}/explain-node.
  // `null` = closed. Opening with a new nodeId resets the buffer and
  // kicks off requestNodeDeepDive. The panel keeps the partial answer
  // visible even after the stream ends.
  const [deepDivePanel, setDeepDivePanel] = useStateM(null);

  const graphData = data || MINDMAP;
  const prepared = useMemoM(
    () => StudyState.prepareMindmapForce(graphData, { layout }),
    [graphData, layout],
  );
  const preparedTree = useMemoM(
    () => StudyState.prepareMindmapTree(graphData, { layout }),
    [graphData, layout],
  );
  const [simNodes, setSimNodes] = useStateM([]);
  const simRef = useRefM(null);
  // Mirror prepared.empty into a ref for the wheel handler (closure-free).
  useEffectM(() => { isEmptyRef.current = !!prepared.empty; }, [prepared.empty]);
  const { nodes, edges } = prepared.empty
    ? { nodes: [], edges: [] }
    : { nodes: (simNodes.length ? simNodes : prepared.nodes), edges: prepared.links || prepared.edges || [] };
  const selected = highlightedId
    ? StudyState.getMindmapNodeDetail(preparedTree, highlightedId)
    : null;

  const relationTypes = prepared.relationTypes || [];
  const [enabledRelations, setEnabledRelations] = useStateM(() => new Set(relationTypes));
  useEffectM(() => {
    // fix-all v1 #A13: when the KG re-extracts (R4-2 upload land emits
    // new relationTypes for the same course), preserve the user's
    // existing chip preferences instead of resetting to all-enabled.
    // Newcomer relations default to enabled; previously-disabled ones
    // stay disabled. Relations that disappear are dropped.
    setEnabledRelations(prev => {
      const known = new Set(relationTypes);
      const next = new Set();
      // 1) keep every previously-enabled relation that still exists.
      prev.forEach(r => { if (known.has(r)) next.add(r); });
      // 2) enable every newcomer (not present in prev at all).
      relationTypes.forEach(r => { if (!prev.has(r)) next.add(r); });
      return next;
    });
  }, [relationTypes.join("|")]);

  useEffectM(() => {
    if (prepared.empty || !(prepared.nodes || []).length) {
      setSimNodes([]);
      if (simRef.current) simRef.current.stop();
      return;
    }
    const forceNodes = (prepared.nodes || []).map(n => Object.assign({}, n));
    const forceLinks = (prepared.links || []).map(l => Object.assign({}, l));
    if (simRef.current) simRef.current.stop();
    const forceApi = (typeof d3 !== "undefined" && d3.forceSimulation) ? d3 : null;
    if (!forceApi) {
      // CDN fallback: keep initial node-link positions usable. The primary
      // path above still runs through d3.forceSimulation when d3 is loaded.
      setSimNodes(forceNodes);
      return;
    }
    let sim;
    // fix-all v1 #A3: rAF-throttle the tick → setSimNodes pump. Without
    // this, every d3 tick (~60Hz × 37-67 ticks per layout settle) drops
    // a fresh node-array clone into React state and re-renders the
    // entire MindMap subtree (visibleIds walk is O(N²), visEdges is
    // O(E)). With rAF coalescing, at most one React render per frame.
    let pendingRaf = 0;
    const scheduleFlush = () => {
      if (pendingRaf) return;
      pendingRaf = (typeof requestAnimationFrame === "function")
        ? requestAnimationFrame(() => {
            pendingRaf = 0;
            setSimNodes(forceNodes.map(n => Object.assign({}, n)));
          })
        : (setTimeout(() => {
            pendingRaf = 0;
            setSimNodes(forceNodes.map(n => Object.assign({}, n)));
          }, 16), 1);
    };
    try {
      // 2026-05-13: tuned down the simulation aggressiveness. Pre-fix
      // values (charge=-420, velocityDecay default 0.4) produced the
      // "nodes punch each other" effect on KGs with ~100+ nodes where
      // a strong collide push fought the strong charge repulsion across
      // tightly-coupled subtrees, causing visible oscillation for ~30s
      // after load. New tuning:
      //   charge -420 → -260 (softer global repulsion)
      //   collide iterations 2 → 1 (less hard push back)
      //   velocityDecay 0.4 → 0.55 (more friction, settles faster)
      //   alpha 0.9 → 0.7 (smaller initial kick)
      //   alphaDecay slightly higher → sim freezes ~30% faster
      sim = forceApi.forceSimulation(forceNodes)
        .force("link", forceApi.forceLink(forceLinks).id(d => d.id).distance(d => {
          const rel = String(d.relation || "");
          if (rel === "part-of") return 135;
          if (rel === "depends-on" || rel === "prerequisite-of") return 180;
          return 155;
        }).strength(d => String(d.relation || "") === "part-of" ? 0.6 : 0.3))
        .force("charge", forceApi.forceManyBody().strength(-260).distanceMax(420))
        .force("collide", forceApi.forceCollide().radius(d => d.kind === "root" ? 112 : d.kind === "branch" ? 78 : 54).iterations(1))
        .force("center", forceApi.forceCenter(0, 0))
        .force("x", forceApi.forceX(0).strength(0.04))
        .force("y", forceApi.forceY(0).strength(0.04))
        .velocityDecay(0.55)
        .alpha(0.7)
        .alphaDecay(forceNodes.length > 100 ? 0.1 : 0.06)
        .on("tick", scheduleFlush);
    } catch (err) {
      if (typeof console !== "undefined") console.warn("d3 force layout unavailable:", err);
      setSimNodes(forceNodes);
      return;
    }
    simRef.current = sim;
    return () => {
      sim.stop();
      if (pendingRaf && typeof cancelAnimationFrame === "function") {
        cancelAnimationFrame(pendingRaf);
      }
    };
  }, [prepared.rootId, (prepared.nodes || []).map(n => n.id).join("|"), (prepared.links || []).map(l => l.id || `${l.source}->${l.target}:${l.relation}`).join("|")]);

  // fix-all v1 #A3: precompute parent→children index from the stable
  // `prepared` payload (NOT `nodes`, which is replaced every tick).
  // visibleIds and the walk below were doing N×O(N) filter-by-parent
  // every render → O(N²) per tick storm. With the map it's O(N) per
  // render and O(1) per parent lookup.
  const childrenByParent = useMemoM(() => {
    const idx = new Map();
    (prepared.nodes || []).forEach(n => {
      const p = n.parent;
      if (!idx.has(p)) idx.set(p, []);
      idx.get(p).push(n);
    });
    return idx;
  }, [prepared.nodes]);

  const visibleIds = useMemoM(() => {
    const vis = new Set(nodes.map(n => n.id));
    function walk(id) {
      if (collapsed.has(id)) return;
      (childrenByParent.get(id) || []).forEach(n => {
        vis.add(n.id);
        walk(n.id);
      });
    }
    collapsed.forEach(id => {
      (childrenByParent.get(id) || []).forEach(child => {
        function hideDescendants(nid) {
          vis.delete(nid);
          (childrenByParent.get(nid) || []).forEach(n => hideDescendants(n.id));
        }
        hideDescendants(child.id);
      });
    });
    nodes.forEach(n => { if (collapsed.has(n.id)) vis.add(n.id); });
    (childrenByParent.get(null) || []).forEach(n => walk(n.id));
    return vis;
  }, [nodes, collapsed, childrenByParent]);

  const visNodes = nodes.filter(n => visibleIds.has(n.id));
  const visEdges = edges.filter(e => {
    const sourceId = edgeNodeId(e.source || e.from);
    const targetId = edgeNodeId(e.target || e.to);
    return visibleIds.has(sourceId) && visibleIds.has(targetId)
      && enabledRelations.has(String(e.relation || "related").replace(/_/g, "-"));
  });
  const allRelationsFiltered = edges.length > 0 && visEdges.length === 0;

  function edgeNodeId(value) {
    if (value && typeof value === "object") return String(value.id || value.concept_id || "");
    return String(value || "");
  }

  function nodeById(id) {
    return nodes.find(n => n.id === id);
  }

  // resolved position including user offset
  function posOf(n) {
    const o = offsets[n.id];
    return {
      x: Number(n.x || 0) + (o?.dx || 0),
      y: Number(n.y || 0) + (o?.dy || 0),
    };
  }

  function relationClass(rel) {
    const normalized = String(rel || "related").replace(/_/g, "-");
    if (normalized === "part-of") return "kg-edge-part-of";
    if (normalized === "prerequisite-of") return "kg-edge-prereq";
    if (normalized === "depends-on") return "kg-edge-depends";
    return "kg-edge-related";
  }

  function relationLabel(rel) {
    const normalized = String(rel || "related").replace(/_/g, "-");
    if (normalized === "prerequisite-of") return "prereq";
    return normalized;
  }

  function setRelationEnabled(rel, checked) {
    setEnabledRelations(prev => {
      const next = new Set(prev);
      if (checked) next.add(rel);
      else next.delete(rel);
      return next;
    });
  }

  function toggleCollapse(id) {
    setCollapsed(prev => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  }

  // ── M3 — apply edit ops locally (optimistic) and persist to server ─
  // F8 (review-swarm): commitOps was previously console.warn-only on POST
  // failure, so a network blip looked like success in the UI but the
  // next GET would silently overwrite local state. Now we surface a
  // syncError + a list of skipped op_results in the toolbar so the
  // student knows their edit didn't actually save (or that the server
  // rejected it for, say, a missing parent_id).
  function commitOps(ops) {
    if (!ops || !ops.length) return;
    const next = StudyState.applyMindmapOps(graphData, ops);
    // Preserve top-level metadata the server returns (label/definition/rootId).
    const merged = Object.assign({}, graphData, next);
    onDataChange && onDataChange(merged);
    if (courseId && API && typeof API.editMindmap === "function") {
      // fix-all v3 #H13 + v4 #B7: when the server reports skipped ops or
      // the POST outright fails, resync the local KG to server-truth.
      // Coalesce rapid bursts (e.g. 5 quick edits all rejected) so we
      // never have more than one inflight GET /api/mindmap; a queued
      // marker triggers exactly one follow-up after the inflight returns.
      function _resyncFromServer() {
        if (typeof API.getMindmap !== "function") return;
        if (resyncRef.current.inflight) {
          resyncRef.current.queued = true;
          return;
        }
        resyncRef.current.inflight = true;
        API.getMindmap(courseId).then(server => {
          if (server && (server.nodes || server.edges)) {
            onDataChange && onDataChange(server);
          }
        }).catch(() => { /* best-effort */ }).then(() => {
          resyncRef.current.inflight = false;
          if (resyncRef.current.queued) {
            resyncRef.current.queued = false;
            _resyncFromServer();
          }
        });
      }
      API.editMindmap(courseId, ops).then(resp => {
        const skipped = (resp && Array.isArray(resp.op_results))
          ? resp.op_results.filter(r => r && r.status === "skipped")
          : [];
        if (skipped.length) {
          setSyncError({
            kind: "skipped",
            count: skipped.length,
            reasons: skipped.map(r => r.reason || r.op).slice(0, 3),
          });
          _resyncFromServer();
        } else {
          setSyncError(null);
        }
      }).catch(err => {
        console.warn("mindmap edit failed:", err);
        setSyncError({
          kind: "failed",
          message: (err && err.message) || "save failed",
        });
        _resyncFromServer();
      });
    }
  }

  function startEditingNode(id) {
    const node = nodes.find(n => n.id === id);
    if (!node) return;
    setEditingId(id);
    setEditingLabel(node.label);
  }

  function commitEdit() {
    if (!editingId) return;
    const trimmed = (editingLabel || "").trim();
    const node = nodes.find(n => n.id === editingId);
    if (trimmed && node && trimmed !== node.label) {
      commitOps([{ op: "update_node", id: editingId, label: trimmed }]);
    }
    setEditingId(null);
    setEditingLabel("");
  }

  function cancelEdit() {
    setEditingId(null);
    setEditingLabel("");
  }

  function addChildOf(parentId) {
    const newId = StudyState.newMindmapNodeId();
    commitOps([{
      op: "add_node", id: newId,
      label: t("mindmap.new_node"), parent_id: parentId,
    }]);
    // Auto-select + edit the new node so the student types the label immediately.
    setSelectedId(newId);
    setTimeout(() => startEditingNode(newId), 0);
  }

  function deleteNodeWithConfirm(id) {
    const node = nodes.find(n => n.id === id);
    if (!node) return;
    if (node.kind === "root") {
      // R5-1: any chapter root (depth=0) is protected, not just the
      // primary one. The server-side overlay endpoint also refuses
      // `delete_node` on `concept_type=="root"` so this is a UX-only
      // guard — F13 invariant holds on the backend regardless.
      window.alert("Cannot delete a chapter root.");
      return;
    }
    const childCount = nodes.filter(n => n.parent === id).length;
    const msg = childCount
      ? `Delete "${node.label}" and its ${childCount} descendant link(s)?`
      : `Delete "${node.label}"?`;
    if (!window.confirm(msg)) return;
    commitOps([{ op: "delete_node", id }]);
    if (selectedId === id) setSelectedId(null);
  }

  function confirmEdgeRelation(rel) {
    if (!pendingEdge) return;
    commitOps([{
      op: "add_edge", source: pendingEdge.source,
      target: pendingEdge.target, relation: rel,
    }]);
    setPendingEdge(null);
  }

  // ── Keyboard shortcuts (when not in input field) ───────────────────
  useEffectM(() => {
    function onKey(e) {
      const tag = (e.target?.tagName || "").toLowerCase();
      if (tag === "input" || tag === "textarea" || editingId) return;
      if (!selectedId) return;
      if (e.key === "n" || e.key === "N") {
        e.preventDefault();
        addChildOf(selectedId);
      } else if (e.key === "Delete" || e.key === "Backspace") {
        e.preventDefault();
        deleteNodeWithConfirm(selectedId);
      } else if (e.key === "F2" || e.key === "Enter") {
        e.preventDefault();
        startEditingNode(selectedId);
      } else if (e.key === "Escape") {
        // 2026-05-13: ESC deselects. Matches the click-on-background
        // path; gives a keyboard escape hatch for users navigating
        // without a mouse / trackpad. Guarded by the `editingId`
        // check above so ESC during inline rename keeps cancelling
        // the rename instead. Also clear the parent's
        // `highlightedNode` via onNodeClick(null) so the dashed
        // "hot" outline (isHot reads `highlightedId === n.id`)
        // disappears alongside the solid "selected" outline.
        e.preventDefault();
        setSelectedId(null);
        onNodeClick && onNodeClick(null);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selectedId, editingId, nodes, courseId]);

  // ---- Trackpad wheel: pinch → zoom around cursor; two-finger swipe → pan.
  //
  // React's synthetic `onWheel` is passive by default in modern React,
  // which forbids preventDefault. Attach a native non-passive listener
  // on the wrap div so we can suppress the page scroll/zoom.
  //
  // macOS reports trackpad pinch as a `wheel` event with `ctrlKey: true`
  // (the OS synthesizes this — the user is NOT actually holding ctrl).
  // Other platforms vary, but ctrlKey is the de-facto detection.
  //
  // Zoom anchors at the cursor: the point under the cursor stays under
  // the cursor across the zoom. Math: if `cx/cy` is the cursor position
  // relative to the transform origin (the wrap center), then after
  // scaling by `ratio = newZoom / prevZoom`, the same world point is
  // now at `(cx * ratio, cy * ratio)` in screen-space — shift `pan` by
  // the delta to keep it under the cursor.
  useEffectM(() => {
    const el = wrapRef.current;
    if (!el) return;
    // review-swarm fix-all v2 #1: cache the wrap's bounding rect via
    // ResizeObserver + window resize/scroll instead of stat'ing it on
    // every wheel event. Removes per-event forced-reflow risk and
    // matches the per-render performance budget of the d3-force sim.
    function refreshRect() { rectRef.current = el.getBoundingClientRect(); }
    refreshRect();
    const ro = (typeof ResizeObserver !== "undefined") ? new ResizeObserver(refreshRect) : null;
    if (ro) ro.observe(el);
    // Capture-phase scroll so nested scrollables (the sidebar, etc.)
    // also invalidate the cached rect.
    window.addEventListener("resize", refreshRect);
    window.addEventListener("scroll", refreshRect, true);

    // review-swarm fix-all v1: gate which wheel events the graph captures.
    //   #6 — only pixel-delta (`deltaMode === 0`) events are treated as
    //   trackpad gestures. Mouse-wheel events on most browsers report
    //   `deltaMode === 1` (line) or `2` (page); those bubble through so
    //   the surrounding page scrolls normally and Windows users keep
    //   their ctrl+wheel browser-zoom shortcut.
    //   #4 — wheel events on the toolbar / legend / detail panel are
    //   skipped via `closest()` so pinching while reaching for `+` does
    //   not accidentally zoom the canvas. Mirrors startCanvasPan's guard.
    // fix-all v2 #3: also skip when the graph has no rendered nodes
    // (placeholder state) so the empty pane doesn't trap page scroll.
    function markWheeling() {
      if (!isWheelingRef.current) {
        isWheelingRef.current = true;
        bumpWheel(n => n + 1);
      }
      if (wheelEndTimerRef.current) clearTimeout(wheelEndTimerRef.current);
      wheelEndTimerRef.current = setTimeout(() => {
        isWheelingRef.current = false;
        wheelEndTimerRef.current = null;
        bumpWheel(n => n + 1);
      }, 150);
    }
    function onWheel(e) {
      if (isEmptyRef.current) return;
      if (e.target && e.target.closest &&
          e.target.closest(".mindmap-toolbar, .mindmap-legend, .mindmap-detail, .mindmap-legend-toggle, .mm-edge-picker")) {
        return;
      }
      if (e.deltaMode !== 0) return;
      // Cached rect (refreshed on resize/scroll); fallback to live read
      // if for some reason the cache hasn't initialised.
      const rect = rectRef.current || el.getBoundingClientRect();
      if (e.ctrlKey) {
        e.preventDefault();
        markWheeling();
        const cx = e.clientX - rect.left - rect.width / 2;
        const cy = e.clientY - rect.top - rect.height / 2;
        const scaleFactor = Math.exp(-e.deltaY * 0.01);
        if (!Number.isFinite(scaleFactor)) return;
        // review-swarm fix-all v2 #2: read zoom/pan via refs, compute
        // both next values at the top level, call setters sequentially.
        // Production React 18 batches the two setters into one render
        // for native events; no nested-updater brittleness.
        const prevZoom = zoomRef.current;
        const prevPan = panRef.current;
        const nextZoom = Math.max(KG_ZOOM_MIN, Math.min(KG_ZOOM_MAX, prevZoom * scaleFactor));
        if (!Number.isFinite(nextZoom) || nextZoom === prevZoom) return;
        const ratio = nextZoom / prevZoom;
        const nx = cx - (cx - prevPan.x) * ratio;
        const ny = cy - (cy - prevPan.y) * ratio;
        if (!Number.isFinite(nx) || !Number.isFinite(ny)) return;
        zoomRef.current = nextZoom;
        panRef.current = { x: nx, y: ny };
        setZoom(nextZoom);
        setPan({ x: nx, y: ny });
      } else if (Math.abs(e.deltaX) > 0 || Math.abs(e.deltaY) > 0) {
        // Two-finger trackpad swipe → pan. Mouse-wheel was already gated
        // out above via `deltaMode !== 0`, so only trackpad reaches here.
        e.preventDefault();
        markWheeling();
        const dx = e.deltaX;
        const dy = e.deltaY;
        if (!Number.isFinite(dx) || !Number.isFinite(dy)) return;
        const prevPan = panRef.current;
        const next = { x: prevPan.x - dx, y: prevPan.y - dy };
        panRef.current = next;
        setPan(next);
      }
    }
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => {
      el.removeEventListener("wheel", onWheel);
      if (ro) ro.disconnect();
      window.removeEventListener("resize", refreshRect);
      window.removeEventListener("scroll", refreshRect, true);
      if (wheelEndTimerRef.current) clearTimeout(wheelEndTimerRef.current);
    };
  }, []);

  // ---- Dragging: window-level listeners so drag doesn't die if cursor leaves a node
  useEffectM(() => {
    function onMove(e) {
      const d = dragRef.current;
      if (!d) return;
      if (d.kind === "pan") {
        setPan({ x: d.px + (e.clientX - d.sx), y: d.py + (e.clientY - d.sy) });
      } else if (d.kind === "node") {
        // account for zoom so node follows cursor at current scale
        const dx = (e.clientX - d.sx) / zoom;
        const dy = (e.clientY - d.sy) / zoom;
        if (simRef.current) {
          // fix-all v1 #A1: when force-sim is live, the sim owns node
          // positions; updating `offsets` here double-counts dx/dy on
          // top of the sim's authoritative x,y on every tick → node
          // visually jumps after mouseup. Only write to fx/fy (sim's
          // pin mechanism) during drag, and skip the offsets state.
          // fix-all v1 #A4 / 2026-05-13: warm the sim on the FIRST real
          // drag movement, not on mousedown — a pure click (mousedown
          // → mouseup, no move) leaves `simWarmed=false` and the sim
          // stays at rest, so tap-to-select doesn't shake the graph.
          // The `simWarmed` flag keeps the original single-warm
          // invariant (one alphaTarget bump per drag, not per frame).
          if (!d.simWarmed) {
            simRef.current.alphaTarget(0.2).restart();
            d.simWarmed = true;
          }
          const simNode = simRef.current.nodes().find(n => n.id === d.id);
          if (simNode) {
            simNode.fx = d.baseX + dx;
            simNode.fy = d.baseY + dy;
          }
        } else {
          // d3 unavailable fallback path: legacy offsets dict drives
          // visible position because there's no sim to write fx/fy to.
          setOffsets(prev => ({ ...prev, [d.id]: { dx: d.ox + dx, dy: d.oy + dy } }));
        }
      } else if (d.kind === "connect") {
        // Track cursor in graph-space coordinates.
        const dx = (e.clientX - d.sx) / zoom;
        const dy = (e.clientY - d.sy) / zoom;
        setConnectDrag({ fromId: d.id, x: d.x0 + dx, y: d.y0 + dy });
      }
      d.moved = true;
    }
    function onUp(e) {
      const d = dragRef.current;
      if (!d) return;
      if (d.kind === "node" && !d.moved) {
        // treat as click
        setSelectedId(d.id);
        onNodeClick && onNodeClick(d.id);
      } else if (d.kind === "node") {
        if (simRef.current) {
          const simNode = simRef.current.nodes().find(n => n.id === d.id);
          if (simNode) {
            simNode.fx = null;
            simNode.fy = null;
          }
          simRef.current.alphaTarget(0);
        }
      } else if (d.kind === "connect") {
        // Hit-test: did we drop over another node?
        const targetEl = document.elementFromPoint(e.clientX, e.clientY);
        const nodeEl = targetEl && targetEl.closest && targetEl.closest(".mm-node");
        const targetId = nodeEl && nodeEl.getAttribute("data-node-id");
        if (targetId && targetId !== d.id) {
          setPendingEdge({ source: d.id, target: targetId });
        }
        setConnectDrag(null);
      } else if (d.kind === "pan" && !d.moved) {
        // 2026-05-13: pure click on canvas background (mousedown +
        // mouseup, no pan movement) → deselect any selected node. Gives
        // the user a way out of the selected state without having to
        // click another node. Drag-to-pan stays unchanged (d.moved
        // becomes true on first mousemove → this branch doesn't fire).
        // Also notify the parent so the App-level `highlightedNode`
        // clears — otherwise the node keeps its dashed "hot" outline
        // (isHot reads `highlightedId === n.id`, driven by the parent).
        setSelectedId(null);
        onNodeClick && onNodeClick(null);
      }
      dragRef.current = null;
      forceRerender(n => n + 1);
    }
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, [zoom, onNodeClick]);

  function startCanvasPan(e) {
    // only pan if the user pressed the background
    if (e.target.closest(".mm-node") || e.target.closest(".mindmap-toolbar")) return;
    dragRef.current = { kind: "pan", sx: e.clientX, sy: e.clientY, px: pan.x, py: pan.y, moved: false };
  }

  // R3-3: alt+click → deep-dive panel; never a drag/select.
  function openDeepDive(nodeId) {
    const node = nodes.find(n => n.id === nodeId);
    if (!node) return;
    if (!courseId) return;
    // fix-all v3 #H11: AbortController so closing the panel cancels the
    // upstream stream instead of leaving it running and burning LLM cost.
    const ac = (typeof AbortController !== "undefined") ? new AbortController() : null;
    setDeepDivePanel({
      nodeId,
      label: node.label,
      events: [],
      answer: "",
      status: "streaming",
      error: null,
      abort: ac,
    });
    if (StudyState && typeof StudyState.requestNodeDeepDive === "function") {
      const onEvent = (evt) => {
        setDeepDivePanel(prev => {
          if (!prev || prev.nodeId !== nodeId) return prev;
          const next = Object.assign({}, prev);
          // fix-all v3 #H10: only retain events the panel actually
          // renders (tool_call / tool_result). Text events were
          // accumulating O(turns·tokens) objects in React state and
          // re-rendering the panel each time; the running answer is
          // already concatenated into `prev.answer`.
          if (evt.type === "text") {
            next.answer = (prev.answer || "") + (evt.delta || "");
          } else if (evt.type === "tool_call" || evt.type === "tool_result") {
            const buf = (prev.events || []).concat([evt]);
            next.events = buf.length > 100 ? buf.slice(-100) : buf;
          } else if (evt.type === "done") {
            next.status = "done";
            if (evt.answer && !prev.answer) next.answer = evt.answer;
          } else if (evt.type === "error") {
            next.status = "error";
            next.error = evt.error || "stream error";
            // fix-all v3 #M (error.partial drop): surface the partial
            // answer the agent had buffered when the stream died, so
            // the user sees what came before instead of an empty bubble.
            if (evt.partial && !prev.answer) next.answer = evt.partial;
          }
          return next;
        });
      };
      StudyState.requestNodeDeepDive(
        courseId, nodeId, onEvent, undefined,
        ac ? { signal: ac.signal } : undefined,
      ).catch(err => {
        // AbortError when the user closed the panel — silent dismiss.
        const aborted = err && (err.name === "AbortError"
          || (ac && ac.signal && ac.signal.aborted));
        if (aborted) return;
        setDeepDivePanel(prev => prev && prev.nodeId === nodeId
          ? Object.assign({}, prev, { status: "error", error: (err && err.message) || "request failed" })
          : prev);
      });
    }
  }

  function startNodeDrag(e, id) {
    e.stopPropagation();
    // R3-3: alt+click intercepts before drag-start so the node neither
    // moves nor enters edit mode. Reading e.altKey at mousedown matches
    // shiftKey behavior above (modifier sampled when gesture begins).
    if (e.altKey) {
      openDeepDive(id);
      return;
    }
    // Shift-drag from a node initiates an edge-create gesture, not a move.
    if (e.shiftKey) {
      const node = nodes.find(n => n.id === id);
      if (!node) return;
      const p = posOf(node);
      dragRef.current = {
        kind: "connect", id,
        sx: e.clientX, sy: e.clientY,
        x0: p.x,
        y0: p.y,
        moved: false,
      };
      setConnectDrag({ fromId: id, x: p.x, y: p.y });
      return;
    }
    const existing = offsets[id] || { dx: 0, dy: 0 };
    dragRef.current = {
      kind: "node",
      id,
      sx: e.clientX, sy: e.clientY,
      ox: existing.dx, oy: existing.dy,
      baseX: Number(nodes.find(n => n.id === id)?.x || 0),
      baseY: Number(nodes.find(n => n.id === id)?.y || 0),
      moved: false,
      simWarmed: false,
    };
    // 2026-05-13: pure clicks (mousedown → mouseup, no movement) used
    // to call `alphaTarget(0.2).restart()` here unconditionally, which
    // re-heated the d3 force sim → every node twitched / drifted for a
    // few seconds even though the user only wanted to select. Defer the
    // warm-up to the FIRST mousemove (see onMove branch below), so a
    // tap-to-select doesn't disturb layout. `dragRef.current.simWarmed`
    // tracks whether the bump already fired so subsequent mousemoves
    // don't restart the sim per frame (the original fix-all v1 #A4
    // single-warm invariant is preserved).
  }

  function childCount(id) {
    return nodes.filter(n => n.parent === id).length;
  }

  // M2: derive HSL background / border from style.hue if present. The hue
  // is set per-topic and inherited by descendants in study-state.js, so a
  // student instantly sees which topic a leaf belongs to.
  function colorStyleFor(n) {
    if (n.style?.hue == null) return null;
    const h = n.style.hue;
    if (n.kind === "branch") {
      return {
        background: `hsl(${h} 70% 92%)`,
        borderColor: `hsl(${h} 60% 55%)`,
        color: `hsl(${h} 60% 32%)`,
      };
    }
    if (n.kind === "leaf") {
      return {
        background: `hsl(${h} 50% 97%)`,
        borderColor: `hsl(${h} 40% 75%)`,
        color: "var(--ink)",
      };
    }
    return null;
  }

  // review-swarm fix-all v1 #1: suppress the 200ms transform transition
  // not only during pointer drags but also during in-flight wheel events,
  // so pinch-zoom's cursor-anchor math doesn't visibly desync from the
  // rendered transform.
  const isDraggingSomething = !!dragRef.current || isWheelingRef.current;

  return (
    <div className="mindmap-wrap"
      ref={wrapRef}
      data-screen-label="Knowledge Graph"
      onMouseDown={startCanvasPan}
    >
      {prepared.empty && <div className="mindmap-empty">{prepared.placeholder}</div>}
      <div className="mindmap-toolbar" onMouseDown={(e) => e.stopPropagation()}>
        <button className="icon-btn" onClick={() => setZoom(z => Math.min(KG_ZOOM_MAX, z + 0.15))}>+</button>
        <button className="icon-btn" onClick={() => setZoom(z => Math.max(KG_ZOOM_MIN, z - 0.15))}>−</button>
        <button className="icon-btn" title="Reset zoom, pan, and node positions"
          onClick={() => { setZoom(1); setPan({x:0,y:0}); setCollapsed(new Set()); setOffsets({}); }}>⟲</button>
        <div className="sep"></div>
        <span style={{fontFamily:"var(--mono)",fontSize:10,color:"var(--ink-3)",padding:"0 6px",alignSelf:"center"}}>{Math.round(zoom*100)}%</span>
        {relationTypes.length > 0 && (
          <>
            <div className="sep"></div>
            <div className="kg-relation-filter" role="group" aria-label="Relation filters">
              {relationTypes.map(rel => (
                <label key={rel} className={"kg-filter-chip " + relationClass(rel)}>
                  <input
                    type="checkbox"
                    checked={enabledRelations.has(rel)}
                    onChange={(e) => setRelationEnabled(rel, e.target.checked)}
                  />
                  <span>{relationLabel(rel)}</span>
                </label>
              ))}
            </div>
          </>
        )}
        {syncError && (
          <>
            <div className="sep"></div>
            <span
              role="status"
              data-sync-error={syncError.kind}
              title={syncError.kind === "failed"
                ? `Save failed: ${syncError.message}`
                : `Server skipped ${syncError.count} op(s): ${(syncError.reasons || []).join("; ")}`}
              onClick={() => setSyncError(null)}
              style={{
                fontFamily: "var(--mono)", fontSize: 10,
                color: syncError.kind === "failed" ? "var(--crimson)" : "var(--amber)",
                padding: "0 8px", alignSelf: "center",
                cursor: "pointer", userSelect: "none",
              }}
            >
              {syncError.kind === "failed"
                ? "● save failed (click to dismiss)"
                : `● ${syncError.count} op skipped`}
            </span>
          </>
        )}
      </div>

      <div
        style={{
          position: "absolute",
          left: "50%", top: "50%",
          transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`,
          transformOrigin: "center",
          transition: isDraggingSomething ? "none" : "transform 200ms ease-out"
        }}
      >
        {/* SVG edges. overflow="visible": SVG by default clips children to
            its viewport (2400×1800 here). Big graphs push leaves past that
            box → edges silently disappear while node divs (outside the SVG)
            still render. overflow="visible" lifts the clip so a path with
            an endpoint at e.g. x=3000 still draws. The width/height +
            negative left/top are kept so the SVG's coord origin stays
            aligned with the +1200/+900 translation in the path d="" below. */}
        <svg
          className="mm-edge"
          width="2400"
          height="1800"
          overflow="visible"
          style={{ left: -1200, top: -900 }}
        >
          <defs>
            <marker id={arrowId("prereq")} viewBox="0 0 10 10" refX="8" refY="5"
                    markerWidth="5" markerHeight="5" orient="auto-start-reverse">
              <path d="M 0 0 L 10 5 L 0 10 z" className="kg-marker-prereq" />
            </marker>
            <marker id={arrowId("depends")} viewBox="0 0 10 10" refX="8" refY="5"
                    markerWidth="5" markerHeight="5" orient="auto-start-reverse">
              <path d="M 0 0 L 10 5 L 0 10 z" className="kg-marker-depends" />
            </marker>
            <marker id={arrowId("related")} viewBox="0 0 10 10" refX="8" refY="5"
                    markerWidth="4" markerHeight="4" orient="auto-start-reverse">
              <path d="M 0 0 L 10 5 L 0 10 z" className="kg-marker-related" />
            </marker>
          </defs>
          {visEdges.map((e, i) => {
            const sourceId = edgeNodeId(e.source || e.from);
            const targetId = edgeNodeId(e.target || e.to);
            const a = nodeById(sourceId);
            const b = nodeById(targetId);
            if (!a || !b) return null;
            const ap = posOf(a), bp = posOf(b);
            const ax = ap.x + 1200, ay = ap.y + 900;
            const bx = bp.x + 1200, by = bp.y + 900;
            const mx = (ax + bx) / 2;
            const my = (ay + by) / 2;
            const isHot = highlightedId === b.id || highlightedId === a.id;
            const rel = String(e.relation || "related").replace(/_/g, "-");
            const cls = relationClass(rel);
            const label = relationLabel(rel);
            const marker = rel === "depends-on"
              ? `url(#${arrowId("depends")})`
              : rel === "prerequisite-of"
                ? `url(#${arrowId("prereq")})`
                : rel === "part-of" ? "" : `url(#${arrowId("related")})`;
            return (
              <g key={e.id || i} className={`kg-edge-group ${cls}${isHot ? " hot" : ""}`}>
                <path
                  d={`M ${ax} ${ay} C ${mx} ${ay}, ${mx} ${by}, ${bx} ${by}`}
                  markerEnd={marker}
                  fill="none"
                />
                <text
                  className="kg-edge-label"
                  x={mx}
                  y={my - 5}
                  textAnchor="middle"
                  dominantBaseline="central"
                >
                  {label}
                </text>
              </g>
            );
          })}
          {/* M3 connect-drag preview */}
          {connectDrag && (() => {
            const from = nodes.find(n => n.id === connectDrag.fromId);
            if (!from) return null;
            const fp = posOf(from);
            return (
              <line
                x1={fp.x + 1200} y1={fp.y + 900}
                x2={connectDrag.x + 1200} y2={connectDrag.y + 900}
                stroke="var(--accent)" strokeWidth={2}
                strokeDasharray="4 3" pointerEvents="none"
              />
            );
          })()}
        </svg>

        {visNodes.map(n => {
          const isCollapsed = collapsed.has(n.id);
          const cCount = childCount(n.id);
          const isHot = highlightedId === n.id;
          const p = posOf(n);
          const isBeingDragged = dragRef.current?.kind === "node" && dragRef.current?.id === n.id;
          const hueStyle = colorStyleFor(n);
          // Course-card root: bigger, two-line (label + overview).
          if (n.kind === "root") {
            return (
              <div
                key={n.id}
                className="mm-node root"
                style={{
                  left: p.x,
                  top: p.y,
                  transform: "translate(-50%, -50%)",
                  outline: isHot ? "2px solid var(--accent)" : "none",
                  outlineOffset: 2,
                  cursor: isBeingDragged ? "grabbing" : "grab",
                  zIndex: isBeingDragged ? 10 : isHot ? 2 : 1,
                  boxShadow: isBeingDragged ? "var(--shadow)" : undefined,
                  maxWidth: 280,
                  textAlign: "center",
                }}
                onMouseDown={(e) => startNodeDrag(e, n.id)}
              >
                <div style={{fontWeight: 700, fontSize: 16, lineHeight: 1.2}}>{n.label}</div>
                {n.definition && (
                  <div style={{
                    marginTop: 4,
                    fontSize: 10.5,
                    fontWeight: 400,
                    fontFamily: "var(--serif)",
                    fontStyle: "italic",
                    opacity: 0.85,
                    lineHeight: 1.35,
                  }}>{n.definition}</div>
                )}
              </div>
            );
          }
          const isSelected = selectedId === n.id;
          const isEditing = editingId === n.id;
          return (
            <div
              key={n.id}
              data-node-id={n.id}
              className={`mm-node ${n.kind}${isCollapsed ? " collapsed" : ""}${isSelected ? " selected" : ""}`}
              data-children={cCount}
              style={{
                left: p.x,
                top: p.y,
                transform: "translate(-50%, -50%)",
                outline: isSelected
                  ? "2px solid var(--accent)"
                  : isHot ? "2px dashed var(--accent)" : "none",
                outlineOffset: 2,
                cursor: isBeingDragged ? "grabbing" : "grab",
                zIndex: isBeingDragged || isEditing ? 10 : isHot || isSelected ? 2 : 1,
                boxShadow: isBeingDragged ? "var(--shadow)" : undefined,
                fontSize: n.style?.fontSize,
                ...(hueStyle || {}),
              }}
              onMouseDown={(e) => startNodeDrag(e, n.id)}
              onDoubleClick={(e) => { e.stopPropagation(); startEditingNode(n.id); }}
            >
              {isEditing ? (
                <input
                  autoFocus
                  value={editingLabel}
                  onChange={(e) => setEditingLabel(e.target.value)}
                  onBlur={commitEdit}
                  onMouseDown={(e) => e.stopPropagation()}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") { e.preventDefault(); commitEdit(); }
                    else if (e.key === "Escape") { e.preventDefault(); cancelEdit(); }
                  }}
                  style={{
                    font: "inherit", color: "inherit", background: "transparent",
                    border: "none", outline: "none", width: "100%",
                  }}
                />
              ) : n.label}
              {/* R3-3: learning-order badge on topic nodes when the
                  extractor produced a topological position. Hidden on
                  legacy KGs where learning_order is null. */}
              {n.kind === "branch" && n.learning_order != null && !isEditing && (
                <div className="mm-order-badge" aria-label={`Study step ${n.learning_order}`}>
                  {n.learning_order}
                </div>
              )}
              {cCount > 0 && !isEditing && (
                <div className="toggle"
                  onMouseDown={(e) => e.stopPropagation()}
                  onClick={(e) => { e.stopPropagation(); toggleCollapse(n.id); }}>
                  {isCollapsed ? "+" : "−"}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {allRelationsFiltered && (
        <div className="kg-filter-empty" role="status">
          {t("mindmap.filtered_all")}
        </div>
      )}

      {legendHidden ? (
        <button
          type="button"
          className="mindmap-legend-toggle"
          title={t("mindmap.show_legend")}
          aria-label={t("mindmap.show_legend")}
          onClick={toggleLegend}
        >▤</button>
      ) : (
        <div className="mindmap-legend">
          <button
            type="button"
            className="mindmap-legend-close"
            title={t("mindmap.hide_legend")}
            onClick={toggleLegend}
            aria-label={t("mindmap.hide_legend")}
          >×</button>
          <div className="row"><div className="sw" style={{ background: "var(--ink)", borderColor: "var(--ink)" }}></div>Chapter · {(prepared.rootIds || []).length || visNodes.filter(n => n.kind === "root").length}</div>
          <div className="row"><div className="sw" style={{ background: "var(--accent-soft)", borderColor: "var(--accent)" }}></div>Topic · {visNodes.filter(n => n.kind === "branch").length}</div>
          <div className="row"><div className="sw" style={{ background: "var(--paper)", borderColor: "var(--rule-strong)" }}></div>Concept · {visNodes.filter(n => n.kind === "leaf").length}</div>
          <div className="row"><div className="sw" style={{ background: "transparent", borderColor: "var(--rule-strong)" }}></div>Relations · {visEdges.length}/{edges.length}</div>
          <div className="row" style={{ marginTop: 4, paddingTop: 4, borderTop: "1px dashed var(--rule)" }}>
            <span style={{fontSize: 10, lineHeight: 1.4}}>
              click select · dblclick edit · <b>N</b> add child · <b>Del</b> delete · <b>shift+drag</b> connect · <b>alt+click</b> deep dive
            </span>
          </div>
        </div>
      )}

      {/* M3: relation picker after a successful connect-drag */}
      {pendingEdge && (() => {
        const src = nodes.find(n => n.id === pendingEdge.source);
        const tgt = nodes.find(n => n.id === pendingEdge.target);
        return (
          <div style={{
            position: "absolute", left: "50%", top: "50%",
            transform: "translate(-50%, -50%)",
            background: "var(--paper)", border: "1px solid var(--rule-strong)",
            padding: 16, borderRadius: 8, boxShadow: "var(--shadow)",
            zIndex: 100, minWidth: 260,
          }} onMouseDown={(e) => e.stopPropagation()}>
            <div style={{fontSize: 12, marginBottom: 12}}>
              Connect <b>{src?.label}</b> → <b>{tgt?.label}</b> as:
            </div>
            <div style={{display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 8}}>
              {["part-of", "depends-on", "is-a", "example-of", "related"].map(rel => (
                <button key={rel} className="btn"
                  onClick={() => confirmEdgeRelation(rel)}
                  style={{fontSize: 11, padding: "4px 10px"}}>
                  {rel}
                </button>
              ))}
            </div>
            <button className="btn" onClick={() => setPendingEdge(null)}
              style={{fontSize: 10, padding: "2px 8px"}}>Cancel</button>
          </div>
        );
      })()}
      {selected && selected.kind !== "root" && (
        // stopPropagation: without it, mousedown on a source-link button
        // bubbles to .mindmap-wrap → startCanvasPan sets dragRef as "pan",
        // and the window-level mouseup handler hits the "tap on canvas
        // background" branch (line ~668) → setSelectedId(null) unmounts
        // this aside *before* React dispatches the button's click, so the
        // citation modal never opens. Mirrors the same guard on the
        // .mindmap-toolbar and the pendingEdge picker.
        <aside className="mindmap-detail" onMouseDown={(e) => e.stopPropagation()}>
          <h3>{selected.label}</h3>
          <p>{selected.definition || "No definition captured yet."}</p>
          {onPractice && (
            <button className="btn primary" onClick={() => onPractice(selected.label)}>Practice 3</button>
          )}
          <div className="source-list">
            {(selected.source_chunks || []).map((chunk, i) => (
              <button key={i} className="source-link" onClick={() => onSourceClick && onSourceClick(chunk)}>
                {chunk.source_file || chunk.chunk_id || "source"} {chunk.page ? `p.${chunk.page}` : ""}
              </button>
            ))}
          </div>
        </aside>
      )}
      {deepDivePanel && (
        <NodeDeepDivePanel panel={deepDivePanel} onClose={() => {
          // fix-all v3 #H11: abort the upstream stream so the server (and
          // billable LLM call) actually stops when the user closes.
          if (deepDivePanel.abort) {
            try { deepDivePanel.abort.abort(); } catch (e) { /* noop */ }
          }
          setDeepDivePanel(null);
        }} />
      )}
    </div>
  );
}

// R3-3: side panel that streams a 5-line explanation + 3 mini-quiz from
// `/api/mindmap/{cid}/explain-node`. Renders the running answer plus a
// transcript of tool_call / tool_result events for transparency.
// `panel.events` carries untrusted text from agent tools — the panel
// surfaces them inside <pre> blocks; never as HTML or markdown.
function NodeDeepDivePanel({ panel, onClose }) {
  const status = panel.status || "streaming";
  const events = Array.isArray(panel.events) ? panel.events : [];
  return (
    <aside className="mm-deepdive-panel" role="complementary"
           aria-label={`Deep dive on ${panel.label || ''}`}>
      <header style={{display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 10}}>
        <h3 style={{margin: 0, font: "14px var(--serif)"}}>
          {panel.label || "Concept"} <span style={{fontSize: 10, color: "var(--ink-3)", marginLeft: 6}}>· deep dive</span>
        </h3>
        <button className="btn" style={{fontSize: 10, padding: "1px 6px"}} onClick={onClose}>×</button>
      </header>
      <div className="mm-deepdive-panel-msg" style={{whiteSpace: "pre-wrap", fontSize: 12, lineHeight: 1.55}}>
        {panel.answer || (status === "error" ? "" : "…")}
      </div>
      {status === "error" && (
        <div style={{marginTop: 10, fontSize: 11, color: "var(--crimson)"}}>
          Stream failed: {panel.error || "unknown error"}
        </div>
      )}
      {events.filter(e => e && (e.type === "tool_call" || e.type === "tool_result")).length > 0 && (
        <details style={{marginTop: 10, fontSize: 11, color: "var(--ink-3)"}}>
          <summary>Tool calls ({events.filter(e => e.type === "tool_call").length})</summary>
          {events.filter(e => e && (e.type === "tool_call" || e.type === "tool_result")).map((e, i) => (
            <div key={i} style={{marginTop: 4}}>
              {e.type === "tool_call" ? (
                <div><b>→ {e.name}</b> <code style={{fontSize: 10}}>{JSON.stringify(e.arguments || {}).slice(0, 120)}</code></div>
              ) : (
                <pre style={{margin: 0, padding: 4, background: "var(--paper-2)", maxHeight: 100, overflow: "auto", fontSize: 10}}>{String(e.result || "").slice(0, 600)}</pre>
              )}
            </div>
          ))}
        </details>
      )}
      <div style={{marginTop: 8, fontSize: 10, color: "var(--ink-3)", fontFamily: "var(--mono)"}}>
        {status === "streaming" ? "streaming…" : status === "done" ? "done" : "stopped"}
      </div>
    </aside>
  );
}

Object.assign(window, { MindMap, NodeDeepDivePanel });
