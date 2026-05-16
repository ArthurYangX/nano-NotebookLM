/* global React */

// R4-2: Processing now consumes the NDJSON upload stream from
// /api/upload/{cid}, rendering one row per stage with a real percent
// progress bar that ticks as `{type:"stage", stage, progress}` events
// arrive. On `error` the row keeps its last percent and shows a retry
// button. On `done` all rows flip to ✓.
//
// Props (all optional except `fileName`):
//   - fileName: the uploaded file's display name
//   - stages: { chunking: number, embedding: number, kg_stage_a: number, kg_stage_b: number }
//   - errorStage: which stage errored (string) — null when ok
//   - errorMsg: caller-supplied summary
//   - done: boolean — true when the {type:"done"} event arrived
//   - onRetry: optional callback when the retry button is clicked
//
// Backwards compat: when `stages` / `done` aren't provided, falls back
// to the legacy `activeStep` integer that app.jsx still emits during
// the brief 0-event window before the first stream tick.
const STAGE_DEFS = [
  { key: "chunking",     lbl: "Chunking",       sub: "Extracting text + segmenting" },
  { key: "embedding",    lbl: "Embedding",      sub: "FAISS vector + BM25 index" },
  { key: "kg_stage_a",   lbl: "KG Stage A",     sub: "Macro topics + course overview" },
  { key: "kg_stage_b",   lbl: "KG Stage B",     sub: "Per-chunk concepts + relations" },
];

function Processing({ fileName, activeStep, stages, errorStage, errorMsg, done, onRetry }) {
  const useStream = stages && typeof stages === "object";

  // 2026-05-16: stage shape is now `{progress, detail}` (background-task
  // status endpoint) instead of a flat percent number. Tolerate both
  // shapes so a stale serialized state from before the refactor doesn't
  // throw.
  function pctOf(key) {
    if (!useStream) return 0;
    const v = stages[key];
    if (v == null) return 0;
    if (typeof v === "number") return v;
    if (typeof v === "object" && typeof v.progress === "number") return v.progress;
    return 0;
  }

  function stageCls(idx, key) {
    if (errorStage === key) return "pstep error";
    if (useStream) {
      const pct = pctOf(key);
      if (pct >= 100) return "pstep done";
      if (pct > 0) return "pstep active";
      return "pstep";
    }
    if (typeof activeStep === "number") {
      if (idx < activeStep) return "pstep done";
      if (idx === activeStep) return "pstep active";
    }
    return "pstep";
  }

  function stagePct(key) {
    if (!useStream) return null;
    return pctOf(key);
  }

  return (
    <div className="processing">
      <div className="processing-card">
        <div className="eye mono">Ingesting new source</div>
        <h2 className="serif">Preparing your document</h2>
        <div className="fname mono">{fileName}</div>
        <div className="processing-steps">
          {STAGE_DEFS.map((s, i) => {
            const cls = stageCls(i, s.key);
            const pct = stagePct(s.key);
            const glyph = cls.indexOf("done") >= 0 ? "✓"
                        : cls.indexOf("error") >= 0 ? "✕"
                        : (i + 1);
            return (
              <div className={cls} key={s.key}>
                <span className="idx">{glyph}</span>
                <span style={{ flex: 1 }}>
                  <span className="lbl">{s.lbl}</span>
                  <div className="sub mono">{s.sub}</div>
                  {pct !== null && (
                    <div className="pstep-bar" aria-label={`${s.lbl} ${pct}%`}>
                      <div className="pstep-bar-fill" style={{ width: `${pct}%` }}></div>
                    </div>
                  )}
                </span>
                <span className="tme">{pct !== null ? `${pct}%` : ""}</span>
              </div>
            );
          })}
        </div>
        {errorStage && (
          <div className="processing-error">
            <div className="processing-error-msg">
              {errorMsg || "上传管道在 " + errorStage + " 阶段失败"}
            </div>
            {onRetry && (
              <button className="processing-retry mono" onClick={onRetry}>retry</button>
            )}
          </div>
        )}
        {done && !errorStage && (
          <div className="processing-done mono">✓ Ready · Notes · Knowledge Graph · Quiz now available</div>
        )}
      </div>
    </div>
  );
}

Object.assign(window, { Processing });
