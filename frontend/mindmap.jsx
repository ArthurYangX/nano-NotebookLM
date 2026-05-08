/* global React, MINDMAP, StudyState, API */
const { useMemo: useMemoM, useState: useStateM, useRef: useRefM, useEffect: useEffectM } = React;

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

function MindMap({ data, layout, courseId, highlightedId, onNodeClick, onSourceClick, onPractice, onDataChange }) {
  const [pan, setPan] = useStateM({ x: 0, y: 0 });
  const [zoom, setZoom] = useStateM(1);
  const [collapsed, setCollapsed] = useStateM(new Set());
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
  // R3-3: alt+click on a node opens a side panel that streams a 5-line
  // explanation + 3 mini-quiz from /api/mindmap/{cid}/explain-node.
  // `null` = closed. Opening with a new nodeId resets the buffer and
  // kicks off requestNodeDeepDive. The panel keeps the partial answer
  // visible even after the stream ends.
  const [deepDivePanel, setDeepDivePanel] = useStateM(null);

  const graphData = data || MINDMAP;
  const prepared = useMemoM(
    () => StudyState.prepareMindmap(graphData, { layout }),
    [graphData, layout],
  );
  const { nodes, edges } = prepared.empty
    ? { nodes: [], edges: [] }
    : { nodes: prepared.nodes, edges: prepared.edges };
  const selected = highlightedId
    ? StudyState.getMindmapNodeDetail(prepared, highlightedId)
    : null;

  const visibleIds = useMemoM(() => {
    const vis = new Set();
    function walk(id) {
      vis.add(id);
      if (collapsed.has(id)) return;
      nodes.filter(n => n.parent === id).forEach(n => walk(n.id));
    }
    const rootId = prepared.rootId
      || nodes.find(n => n.depth === 0)?.id
      || graphData.id;
    if (rootId) walk(rootId);
    if (vis.size <= 1 && nodes.length > vis.size) nodes.forEach(n => vis.add(n.id));
    return vis;
  }, [nodes, collapsed, prepared.rootId, graphData.id]);

  const visNodes = nodes.filter(n => visibleIds.has(n.id));
  const visEdges = edges.filter(e => visibleIds.has(e.from || e.source) && visibleIds.has(e.to || e.target));

  // resolved position including user offset
  function posOf(n) {
    const o = offsets[n.id];
    return { x: n.x + (o?.dx || 0), y: n.y + (o?.dy || 0) };
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
      // fix-all v3 #H13: when the server reports skipped ops or the POST
      // outright fails, resync the local KG to the server-truth so the
      // optimistic state can't drift (e.g. the next commit referencing a
      // node the server refused to add).
      function _resyncFromServer() {
        if (typeof API.getMindmap === "function") {
          API.getMindmap(courseId).then(server => {
            if (server && (server.nodes || server.edges)) {
              onDataChange && onDataChange(server);
            }
          }).catch(() => { /* best-effort */ });
        }
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
      label: "新节点", parent_id: parentId,
    }]);
    // Auto-select + edit the new node so the student types the label immediately.
    setSelectedId(newId);
    setTimeout(() => startEditingNode(newId), 0);
  }

  function deleteNodeWithConfirm(id) {
    const node = nodes.find(n => n.id === id);
    if (!node) return;
    if (node.kind === "root") {
      window.alert("Cannot delete the course root.");
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
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selectedId, editingId, nodes, courseId]);

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
        setOffsets(prev => ({ ...prev, [d.id]: { dx: d.ox + dx, dy: d.oy + dy } }));
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
      } else if (d.kind === "connect") {
        // Hit-test: did we drop over another node?
        const targetEl = document.elementFromPoint(e.clientX, e.clientY);
        const nodeEl = targetEl && targetEl.closest && targetEl.closest(".mm-node");
        const targetId = nodeEl && nodeEl.getAttribute("data-node-id");
        if (targetId && targetId !== d.id) {
          setPendingEdge({ source: d.id, target: targetId });
        }
        setConnectDrag(null);
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
      dragRef.current = {
        kind: "connect", id,
        sx: e.clientX, sy: e.clientY,
        x0: node.x + (offsets[id]?.dx || 0),
        y0: node.y + (offsets[id]?.dy || 0),
        moved: false,
      };
      setConnectDrag({ fromId: id, x: node.x, y: node.y });
      return;
    }
    const existing = offsets[id] || { dx: 0, dy: 0 };
    dragRef.current = {
      kind: "node",
      id,
      sx: e.clientX, sy: e.clientY,
      ox: existing.dx, oy: existing.dy,
      moved: false,
    };
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

  const isDraggingSomething = !!dragRef.current;
  const rootNode = visNodes.find(n => n.kind === "root");

  return (
    <div className="mindmap-wrap"
      data-screen-label="Mind map"
      onMouseDown={startCanvasPan}
    >
      {prepared.empty && <div className="mindmap-empty">{prepared.placeholder}</div>}
      <div className="mindmap-toolbar" onMouseDown={(e) => e.stopPropagation()}>
        <button className="icon-btn" onClick={() => setZoom(z => Math.min(2, z + 0.15))}>+</button>
        <button className="icon-btn" onClick={() => setZoom(z => Math.max(0.5, z - 0.15))}>−</button>
        <button className="icon-btn" title="Reset zoom, pan, and node positions"
          onClick={() => { setZoom(1); setPan({x:0,y:0}); setCollapsed(new Set()); setOffsets({}); }}>⟲</button>
        <div className="sep"></div>
        <span style={{fontFamily:"var(--mono)",fontSize:10,color:"var(--ink-3)",padding:"0 6px",alignSelf:"center"}}>{Math.round(zoom*100)}%</span>
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
        {/* SVG edges */}
        <svg
          className="mm-edge"
          width="2400"
          height="1800"
          style={{ left: -1200, top: -900 }}
        >
          {visEdges.map((e, i) => {
            const a = nodes.find(n => n.id === (e.from || e.source));
            const b = nodes.find(n => n.id === (e.to || e.target));
            if (!a || !b) return null;
            const ap = posOf(a), bp = posOf(b);
            const ax = ap.x + 1200, ay = ap.y + 900;
            const bx = bp.x + 1200, by = bp.y + 900;
            const mx = (ax + bx) / 2;
            const isHot = highlightedId === b.id || highlightedId === a.id;
            // Color edges by the child's hue so the topic-wedge reads visually.
            const childHue = (b.kind === "branch" ? b : a).style?.hue;
            const stroke = isHot
              ? "var(--accent)"
              : childHue != null
                ? `hsl(${childHue} 40% 65%)`
                : "var(--rule-strong)";
            return (
              <path
                key={i}
                d={`M ${ax} ${ay} C ${mx} ${ay}, ${mx} ${by}, ${bx} ${by}`}
                stroke={stroke}
                strokeWidth={isHot ? 1.5 : 1}
                strokeDasharray={e.style?.dash || ""}
                fill="none"
              />
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

      <div className="mindmap-legend">
        <div className="row"><div className="sw" style={{ background: "var(--ink)", borderColor: "var(--ink)" }}></div>Course root</div>
        <div className="row"><div className="sw" style={{ background: "var(--accent-soft)", borderColor: "var(--accent)" }}></div>Topic · {visNodes.filter(n => n.kind === "branch").length}</div>
        <div className="row"><div className="sw" style={{ background: "var(--paper)", borderColor: "var(--rule-strong)" }}></div>Concept · {visNodes.filter(n => n.kind === "leaf").length}</div>
        <div className="row" style={{ marginTop: 4, paddingTop: 4, borderTop: "1px dashed var(--rule)" }}>
          <span style={{fontSize: 10, lineHeight: 1.4}}>
            click select · dblclick edit · <b>N</b> add child · <b>Del</b> delete · <b>shift+drag</b> connect · <b>alt+click</b> deep dive
          </span>
        </div>
      </div>

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
        <aside className="mindmap-detail">
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
