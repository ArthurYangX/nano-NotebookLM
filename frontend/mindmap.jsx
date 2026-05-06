/* global React, MINDMAP, StudyState */
const { useMemo: useMemoM, useState: useStateM, useRef: useRefM, useEffect: useEffectM } = React;

// Simple radial layout: root center; branches on radial positions; leaves splayed from each branch.
function layoutMindmap(root, layout = "radial") {
  const nodes = [];
  const edges = [];

  if (layout === "radial") {
    const cx = 0, cy = 0;
    nodes.push({ id: root.id, label: root.label, kind: "root", x: cx, y: cy });
    const branches = root.children || [];
    const R1 = 200;
    const branchAngles = branches.map((_, i) => (i / branches.length) * Math.PI * 2 - Math.PI / 2);
    branches.forEach((b, i) => {
      const a = branchAngles[i];
      const x = cx + Math.cos(a) * R1;
      const y = cy + Math.sin(a) * R1;
      nodes.push({ id: b.id, label: b.label, kind: "branch", x, y, parent: root.id });
      edges.push({ from: root.id, to: b.id });

      const leaves = b.children || [];
      const R2 = 160;
      const spread = Math.PI / 3.5;
      leaves.forEach((l, j) => {
        const la = a + (j - (leaves.length - 1) / 2) * (spread / Math.max(leaves.length, 1));
        const lx = x + Math.cos(la) * R2;
        const ly = y + Math.sin(la) * R2;
        nodes.push({ id: l.id, label: l.label, kind: "leaf", x: lx, y: ly, parent: b.id, children: l.children });
        edges.push({ from: b.id, to: l.id });

        (l.children || []).forEach((gc, k) => {
          const gca = la + (k - ((l.children.length - 1) / 2)) * 0.35;
          const gx = lx + Math.cos(gca) * 110;
          const gy = ly + Math.sin(gca) * 110;
          nodes.push({ id: gc.id, label: gc.label, kind: "leaf", x: gx, y: gy, parent: l.id, depth: 2 });
          edges.push({ from: l.id, to: gc.id });
        });
      });
    });
  } else {
    const cx = -400, cy = 0;
    nodes.push({ id: root.id, label: root.label, kind: "root", x: cx, y: cy });
    const branches = root.children || [];
    const gap = 200;
    branches.forEach((b, i) => {
      const bx = cx + 260;
      const by = cy + (i - (branches.length - 1) / 2) * gap;
      nodes.push({ id: b.id, label: b.label, kind: "branch", x: bx, y: by, parent: root.id });
      edges.push({ from: root.id, to: b.id });
      const leaves = b.children || [];
      leaves.forEach((l, j) => {
        const lx = bx + 220;
        const ly = by + (j - (leaves.length - 1) / 2) * 60;
        nodes.push({ id: l.id, label: l.label, kind: "leaf", x: lx, y: ly, parent: b.id });
        edges.push({ from: b.id, to: l.id });
        (l.children || []).forEach((gc, k) => {
          const gx = lx + 180;
          const gy = ly + (k - ((l.children.length - 1) / 2)) * 34;
          nodes.push({ id: gc.id, label: gc.label, kind: "leaf", x: gx, y: gy, parent: l.id });
          edges.push({ from: l.id, to: gc.id });
        });
      });
    });
  }
  return { nodes, edges };
}

function MindMap({ data, layout, highlightedId, onNodeClick, onSourceClick, onPractice }) {
  const [pan, setPan] = useStateM({ x: 0, y: 0 });
  const [zoom, setZoom] = useStateM(1);
  const [collapsed, setCollapsed] = useStateM(new Set());
  // user-applied per-node offsets: { [id]: {dx, dy} }
  const [offsets, setOffsets] = useStateM({});
  // in-progress drag state
  const dragRef = useRefM(null); // {kind: 'pan'|'node', ...}
  const [, forceRerender] = useStateM(0);

  const graphData = data || MINDMAP;
  const prepared = useMemoM(() => StudyState.prepareMindmap(graphData, { layout }), [graphData, layout]);
  const { nodes, edges } = useMemoM(() => {
    if (prepared.empty) return { nodes: [], edges: [] };
    return prepared.nodes.length ? prepared : layoutMindmap(graphData, layout);
  }, [prepared, graphData, layout]);
  const selected = highlightedId ? StudyState.getMindmapNodeDetail(prepared, highlightedId) : null;

  const visibleIds = useMemoM(() => {
    const vis = new Set();
    function walk(id) {
      vis.add(id);
      if (collapsed.has(id)) return;
      nodes.filter(n => n.parent === id).forEach(n => walk(n.id));
    }
    const rootId = nodes.find(n => n.depth === 0)?.id || graphData.id;
    if (rootId) walk(rootId);
    if (vis.size <= 1 && nodes.length > vis.size) nodes.forEach(n => vis.add(n.id));
    return vis;
  }, [nodes, collapsed, graphData.id]);

  const visNodes = nodes.filter(n => visibleIds.has(n.id));
  const visEdges = edges.filter(e => visibleIds.has(e.from) && visibleIds.has(e.to));

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
      }
      d.moved = true;
    }
    function onUp(e) {
      const d = dragRef.current;
      if (!d) return;
      if (d.kind === "node" && !d.moved) {
        // treat as click
        onNodeClick && onNodeClick(d.id);
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

  function startNodeDrag(e, id) {
    e.stopPropagation();
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

  const isDraggingSomething = !!dragRef.current;

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
            return (
              <path
                key={i}
                d={`M ${ax} ${ay} C ${mx} ${ay}, ${mx} ${by}, ${bx} ${by}`}
                stroke={isHot ? "var(--accent)" : "var(--rule-strong)"}
                strokeWidth={isHot ? 1.5 : 1}
                strokeDasharray={e.style?.dash || ""}
                fill="none"
              />
            );
          })}
        </svg>

        {visNodes.map(n => {
          const isCollapsed = collapsed.has(n.id);
          const cCount = childCount(n.id);
          const isHot = highlightedId === n.id;
          const p = posOf(n);
          const isBeingDragged = dragRef.current?.kind === "node" && dragRef.current?.id === n.id;
          return (
            <div
              key={n.id}
              className={`mm-node ${n.kind}${isCollapsed ? " collapsed" : ""}`}
              data-children={cCount}
              style={{
                left: p.x,
                top: p.y,
                transform: "translate(-50%, -50%)",
                outline: isHot ? "2px solid var(--accent)" : "none",
                outlineOffset: 2,
                cursor: isBeingDragged ? "grabbing" : "grab",
                zIndex: isBeingDragged ? 10 : isHot ? 2 : 1,
                boxShadow: isBeingDragged ? "var(--shadow)" : undefined,
                fontSize: n.style?.fontSize,
                filter: n.style?.saturation ? `saturate(${0.8 + n.style.saturation})` : undefined,
              }}
              onMouseDown={(e) => startNodeDrag(e, n.id)}
            >
              {n.label}
              {cCount > 0 && n.kind !== "root" && (
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
        <div className="row"><div className="sw" style={{ background: "var(--ink)", borderColor: "var(--ink)" }}></div>Root concept</div>
        <div className="row"><div className="sw" style={{ background: "var(--accent-soft)", borderColor: "var(--accent)" }}></div>Branch</div>
        <div className="row"><div className="sw" style={{ background: "var(--paper)", borderColor: "var(--rule-strong)" }}></div>Leaf · {visNodes.length} nodes</div>
        <div className="row" style={{ marginTop: 4, paddingTop: 4, borderTop: "1px dashed var(--rule)" }}>
          <span>drag node · pan canvas · click = select</span>
        </div>
      </div>
      {selected && (
        <aside className="mindmap-detail">
          <h3>{selected.label}</h3>
          <p>{selected.definition || "No definition captured yet."}</p>
          <button className="btn primary" onClick={() => onPractice && onPractice(selected.label)}>Practice 3</button>
          <div className="source-list">
            {(selected.source_chunks || []).map((chunk, i) => (
              <button key={i} className="source-link" onClick={() => onSourceClick && onSourceClick(chunk)}>
                {chunk.source_file || chunk.chunk_id || "source"} {chunk.page ? `p.${chunk.page}` : ""}
              </button>
            ))}
          </div>
        </aside>
      )}
    </div>
  );
}

Object.assign(window, { MindMap });
