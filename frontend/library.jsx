/* global React, SAMPLE_SOURCES, SAMPLE_COLLECTIONS */
const { useState, useRef, useEffect } = React;

function FileIcon({ type }) {
  const cls = "ficon " + type;
  return <div className={cls}>{type.toUpperCase()}</div>;
}

function SourceItem({ s, active, onPick, onCheckboxClick }) {
  const t = useT();
  return (
    <div className={"source-item" + (active ? " active" : "")} onClick={() => onPick(s.id)}>
      <FileIcon type={s.type} />
      <div className="title">{s.title}</div>
      <div
        className={"check" + (s.checked ? " on" : "")}
        title={t("library.row_toggle_tip")}
        onClick={(e) => { e.stopPropagation(); onCheckboxClick(e, s.id); }}
      ></div>
      <div className="meta mono">{s.meta}</div>
    </div>
  );
}

function Library({ sources, collections, activeId, onPick, onToggle, onToggleMany, onStartUpload, uploading }) {
  const t = useT();
  // Collections list — prefer the explicit prop (lifted to React state
  // in App by review-swarm v2 fix-soon #8). Fall back to the legacy
  // window global for any host that hasn't migrated yet (e.g. demo
  // data path).
  const collectionsList = Array.isArray(collections)
    ? collections
    : (typeof SAMPLE_COLLECTIONS !== "undefined" ? SAMPLE_COLLECTIONS : []);
  const [hot, setHot] = useState(false);
  // Anchor for shift-click range select — id of the last checkbox the user
  // clicked. Cleared when the source list changes underneath us (e.g.
  // course switch) since the previous id would no longer make sense.
  const lastToggledRef = useRef(null);
  // review-swarm v2 fix-now #3: the original implementation only said "is
  // cleared on source-list change" in a comment but never actually did it.
  // After a course switch, ids like `s0` get reused, so a Shift+Click on
  // the first checkbox in the new course range-toggled against a stale
  // anchor from the previous course. Reset whenever `sources` identity
  // changes (it's a fresh array reference on every getSources resolve).
  useEffect(() => { lastToggledRef.current = null; }, [sources]);

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
        <h3>{t("library.sources")}</h3>
        <span className="count mono">{t("library.in_context", { n: checkedCount, total })}</span>
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
            title={t("library.select_all_tip")}
          >{t("library.select_all")}</button>
          <button
            className="lib-bulk-btn"
            onClick={selectNone}
            disabled={noneChecked}
            title={t("library.select_none_tip")}
          >{t("library.select_none")}</button>
          <button
            className="lib-bulk-btn"
            onClick={invertSelection}
            title={t("library.invert_tip")}
          >{t("library.invert")}</button>
          <span style={{ marginLeft: "auto", color: "var(--ink-3)" }}>
            {t("library.shift_hint")}
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
        <div>{t("library.drop")}</div>
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
          <h3>{t("library.collections")}</h3>
        </div>
        {collectionsList.map(c => (
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
