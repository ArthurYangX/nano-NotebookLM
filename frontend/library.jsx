/* global React, SAMPLE_SOURCES, SAMPLE_COLLECTIONS */
const { useState } = React;

function FileIcon({ type }) {
  const cls = "ficon " + type;
  return <div className={cls}>{type.toUpperCase()}</div>;
}

function SourceItem({ s, active, onPick, onToggle }) {
  return (
    <div className={"source-item" + (active ? " active" : "")} onClick={() => onPick(s.id)}>
      <FileIcon type={s.type} />
      <div className="title">{s.title}</div>
      <div
        className={"check" + (s.checked ? " on" : "")}
        onClick={(e) => { e.stopPropagation(); onToggle(s.id); }}
      ></div>
      <div className="meta mono">{s.meta}</div>
    </div>
  );
}

function Library({ sources, activeId, onPick, onToggle, onStartUpload, uploading }) {
  const [hot, setHot] = useState(false);
  return (
    <aside className="library" data-screen-label="Library">
      <div className="lib-section">
        <h3>Sources</h3>
        <span className="count mono">{sources.filter(s => s.checked).length} / {sources.length} in context</span>
      </div>

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
          <SourceItem key={s.id} s={s} active={s.id === activeId} onPick={onPick} onToggle={onToggle} />
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
