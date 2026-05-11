/* global React, SAMPLE_SOURCES, SAMPLE_COLLECTIONS */
const { useState, useRef } = React;

function FileIcon({ type }) {
  const cls = "ficon " + type;
  return <div className={cls}>{type.toUpperCase()}</div>;
}

function SourceItem({ s, active, onPick, onCheckboxClick }) {
  return (
    <div className={"source-item" + (active ? " active" : "")} onClick={() => onPick(s.id)}>
      <FileIcon type={s.type} />
      <div className="title">{s.title}</div>
      <div
        className={"check" + (s.checked ? " on" : "")}
        title="Click to toggle · Shift+Click to range-select"
        onClick={(e) => { e.stopPropagation(); onCheckboxClick(e, s.id); }}
      ></div>
      <div className="meta mono">{s.meta}</div>
    </div>
  );
}

function Library({ sources, activeId, onPick, onToggle, onToggleMany, onStartUpload, uploading }) {
  const [hot, setHot] = useState(false);
  // Anchor for shift-click range select — id of the last checkbox the user
  // clicked. Cleared when the source list changes underneath us (e.g.
  // course switch) since the previous id would no longer make sense.
  const lastToggledRef = useRef(null);

  const checkedCount = sources.filter(s => s.checked).length;
  const total = sources.length;
  const allChecked = total > 0 && checkedCount === total;
  const noneChecked = checkedCount === 0;

  function selectAll() {
    if (typeof onToggleMany === "function") {
      onToggleMany(sources.map(s => s.id), true);
    } else {
      // Legacy fallback: emit per-id toggles for unchecked ones only.
      sources.filter(s => !s.checked).forEach(s => onToggle(s.id));
    }
    lastToggledRef.current = null;
  }
  function selectNone() {
    if (typeof onToggleMany === "function") {
      onToggleMany(sources.map(s => s.id), false);
    } else {
      sources.filter(s => s.checked).forEach(s => onToggle(s.id));
    }
    lastToggledRef.current = null;
  }
  function invertSelection() {
    if (typeof onToggleMany === "function") {
      const on = sources.filter(s => !s.checked).map(s => s.id);
      const off = sources.filter(s => s.checked).map(s => s.id);
      if (on.length) onToggleMany(on, true);
      if (off.length) onToggleMany(off, false);
    } else {
      sources.forEach(s => onToggle(s.id));
    }
    lastToggledRef.current = null;
  }

  function handleCheckboxClick(e, id) {
    // Shift+Click: toggle every source between the anchor and the clicked
    // id (inclusive) to MATCH the clicked id's NEW state. This mirrors
    // GitHub / Gmail / VS Code range-select semantics: you set one end
    // explicitly, then Shift+Click the other end and everything in
    // between snaps to the same state.
    const idx = sources.findIndex(s => s.id === id);
    if (idx < 0) return;
    const target = sources[idx];
    const desiredState = !target.checked;  // what `id` itself becomes after this click

    if (e.shiftKey && lastToggledRef.current && typeof onToggleMany === "function") {
      const anchorIdx = sources.findIndex(s => s.id === lastToggledRef.current);
      if (anchorIdx >= 0 && anchorIdx !== idx) {
        const [lo, hi] = anchorIdx < idx ? [anchorIdx, idx] : [idx, anchorIdx];
        const ids = sources.slice(lo, hi + 1).map(s => s.id);
        onToggleMany(ids, desiredState);
        lastToggledRef.current = id;
        return;
      }
    }
    onToggle(id);
    lastToggledRef.current = id;
  }

  return (
    <aside className="library" data-screen-label="Library">
      <div className="lib-section">
        <h3>Sources</h3>
        <span className="count mono">{checkedCount} / {total} in context</span>
      </div>

      {total > 0 && (
        <div className="lib-bulk-bar mono" style={{
          display: "flex", gap: 4, padding: "2px 4px 6px", flexWrap: "wrap",
          fontSize: 11,
        }}>
          <button
            className="lib-bulk-btn"
            onClick={selectAll}
            disabled={allChecked}
            title="勾选全部 sources / Select all"
          >全选</button>
          <button
            className="lib-bulk-btn"
            onClick={selectNone}
            disabled={noneChecked}
            title="清空所有勾选 / Select none"
          >全不选</button>
          <button
            className="lib-bulk-btn"
            onClick={invertSelection}
            title="反选 / Invert selection"
          >反选</button>
          <span style={{ marginLeft: "auto", color: "var(--ink-3)" }}>
            Shift+Click 区间选
          </span>
        </div>
      )}

      <div
        className={"dropzone" + (hot ? " hot" : "")}
        onDragOver={(e) => { e.preventDefault(); setHot(true); }}
        onDragLeave={() => setHot(false)}
        onDrop={(e) => { e.preventDefault(); setHot(false); onStartUpload(); }}
        onClick={onStartUpload}
      >
        <div className="plus">+</div>
        <div>Drop files or click to upload</div>
        <div className="hint">pdf · pptx · docx · png · md</div>
      </div>

      {uploading && (
        <div className="uploading">
          <div className="lbl mono">{uploading.name}</div>
          <div className="bar"><div style={{ width: uploading.pct + "%" }}></div></div>
          <div className="lbl mono">{uploading.pct}%</div>
        </div>
      )}

      <div className="lib-list">
        {sources.map(s => (
          <SourceItem
            key={s.id}
            s={s}
            active={s.id === activeId}
            onPick={onPick}
            onCheckboxClick={handleCheckboxClick}
          />
        ))}
      </div>

      <div className="collections">
        <div className="lib-section" style={{ padding: "4px 4px 6px" }}>
          <h3>Collections</h3>
        </div>
        {SAMPLE_COLLECTIONS.map(c => (
          <div key={c.id} className="collection-row">
            <div className="dot" style={{ color: c.color }}></div>
            <span>{c.name}</span>
            <span className="n">{c.count}</span>
          </div>
        ))}
      </div>
    </aside>
  );
}

Object.assign(window, { Library });
