/* global React */

// R4-2: Processing consumes the status snapshot from
// /api/upload/status/{task_id}, rendering one row per stage with a real
// percent progress bar that ticks as the bg task mutates state.
// On error the row keeps its last percent and shows a retry button.
// On done all rows flip to ✓.
//
// 2026-05-20: ETA countdown + per-stage sub copy folds in total_pages
// so users see "0 / 69 页" during extracting. ETA sourced from backend's
// _estimate_upload_duration_seconds (page-count × engine baseline +
// mineru cold start). The 5 rendered stages mirror UPLOAD_STAGES on
// the backend 1:1 — no UI-only synthesis. A briefly-considered
// "Preparing" UI-only step was dropped because the ETA row above
// already conveys "files saved, awaiting first event" via the elapsed
// timer + estimate, making the extra row pure noise.
//
// Props (all optional except `fileName`):
//   - fileName: the uploaded file's display name
//   - stages: { chunking: {progress,detail?}, embedding: ..., kg_stage_a: ..., kg_stage_b: ... }
//   - errorStage: which stage errored (string) — null when ok
//   - errorMsg: caller-supplied summary
//   - done: boolean — true when the task hit terminal "done" state
//   - onRetry: optional callback when the retry button is clicked
//   - estimatedSeconds: backend-provided ETA for the whole pipeline
//   - totalPages: total PDF pages + PPTX slides across uploaded files
//   - startedAt: client-side ms epoch the upload was initiated (for the
//                live elapsed timer); falls back to now() on first paint
//
// Backwards compat: when `stages` / `done` aren't provided, falls back
// to the legacy `activeStep` integer that app.jsx still emits during
// the brief 0-event window before the first stream tick.
// fix-all v3 (2026-05-22): stage labels go through i18n.js so the
// upload overlay isn't English-only. STAGE_KEYS stays a plain array
// (it pins ordering + drives per-key lookups); buildStageDefs(t)
// resolves the localized {lbl, sub} pair via the i18n table.
const STAGE_KEYS = ["extracting", "chunking", "embedding", "kg_stage_a", "kg_stage_b"];
function buildStageDefs(t) {
  return STAGE_KEYS.map((key) => ({
    key,
    lbl: t(`processing.stage.${key}.lbl`),
    sub: t(`processing.stage.${key}.sub`),
  }));
}

// Locale-aware clock formatter. Suffixes follow the user's UI language: CN uses
// the all-CJK form (5秒 / 3分2秒 / 1小时), EN uses compact (5s / 3m2s / 1h).
function _fmtClock(secs, lang) {
  if (!Number.isFinite(secs) || secs < 0) return "—";
  const cn = (lang || "en") === "zh";
  const S = cn ? "秒" : "s";
  const M = cn ? "分" : "m";
  const H = cn ? "小时" : "h";
  const s = Math.floor(secs);
  if (s < 60) return `${s}${S}`;
  const m = Math.floor(s / 60);
  const r = s % 60;
  if (m < 60) return r === 0 ? `${m}${M}` : `${m}${M}${r}${S}`;
  const h = Math.floor(m / 60);
  const mm = m % 60;
  return mm === 0 ? `${h}${H}` : `${h}${H}${mm}${M}`;
}

// fix-all v2 LOW F6: scope the per-second re-render to just the clock display
// so the parent <Processing/> tree (6 stage rows + bars + status icons) does
// not re-render once per second for 5-15 min per upload.
function ElapsedClock({ startedAt, estimatedSeconds, done, errorStage }) {
  const t = useT();
  const lang = React.useContext(window.LangContext) || "en";
  const [now, setNow] = React.useState(() => Date.now());
  React.useEffect(() => {
    if (done || errorStage) return undefined;
    const iv = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(iv);
  }, [done, errorStage]);
  const startedMs = Number.isFinite(startedAt) ? startedAt : now;
  const elapsedSecs = Math.max(0, Math.floor((now - startedMs) / 1000));
  const remainingSecs = Number.isFinite(estimatedSeconds)
    ? Math.max(0, estimatedSeconds - elapsedSecs)
    : null;
  return (
    <span>
      {t("processing.elapsed")} <strong>{_fmtClock(elapsedSecs, lang)}</strong>
      {remainingSecs != null && remainingSecs > 0 && !done && !errorStage && (
        <span> · {t("processing.remaining", { t: _fmtClock(remainingSecs, lang) })}</span>
      )}
    </span>
  );
}

function Processing({
  fileName, activeStep, stages, errorStage, errorMsg, done, onRetry,
  estimatedSeconds, totalPages, startedAt,
}) {
  const t = useT();
  const lang = React.useContext(window.LangContext) || "en";
  const useStream = stages && typeof stages === "object";
  const STAGE_DEFS = React.useMemo(() => buildStageDefs(t), [t]);

  // fix-all v2 LOW F6: per-second timer state lives in <ElapsedClock/>
  // so the parent tree (stage rows + bars) doesn't re-render every
  // second.

  // 2026-05-16: stage shape is `{progress, detail}` (background-task
  // status endpoint). Tolerate the flat-number shape too for legacy
  // serialized state from before the refactor.
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

  // Sub-line per step. Extracting gets the page count when available.
  function stageSub(s) {
    if (s.key === "extracting" && totalPages > 0) {
      const pct = pctOf("extracting");
      // fix-all v3 (2026-05-22): backend no longer carves the bar for
      // soffice pptx→pdf conversion. During that prep phase the bar
      // stays at 0 and detail carries structured pptx_previews_total
      // counters; we localize via i18n so zh users see Chinese copy.
      // After prep, pct ticks 0-98 reflecting only the real extraction
      // work (MinerU / PyMuPDF), and done ramps linearly across.
      const stageObj = useStream ? stages["extracting"] : null;
      const detail = (stageObj && typeof stageObj === "object" && stageObj.detail) || null;
      const pptxTotal = detail && typeof detail.pptx_previews_total === "number"
        ? detail.pptx_previews_total : 0;
      if (pct === 0 && pptxTotal > 0) {
        // Soffice prep window — show the localized prep message + a friendly 0/N.
        const sub = t("processing.with_pptx_render", { n: pptxTotal });
        return `${sub} · ${t("processing.pages_progress", { done: 0, total: totalPages })}`;
      }
      const realPct = Math.min(1, pct / 98);
      const done = Math.min(totalPages, Math.round(totalPages * realPct));
      return `MinerU / PyMuPDF · ${t("processing.pages_progress", { done, total: totalPages })}`;
    }
    return s.sub;
  }

  return (
    <div className="processing">
      <div className="processing-card">
        <div className="eye mono">Ingesting new source</div>
        <h2 className="serif">Preparing your document</h2>
        <div className="fname mono">{fileName}</div>
        {useStream && (estimatedSeconds > 0 || totalPages > 0) && (
          <div className="processing-eta mono">
            {estimatedSeconds > 0 && (
              <span>
                {t("processing.estimate_about")} <strong>{_fmtClock(estimatedSeconds, lang)}</strong>
                {totalPages > 0 && <span> · {t("processing.pages_total", { n: totalPages })}</span>}
              </span>
            )}
            {estimatedSeconds <= 0 && totalPages > 0 && (
              <span>{t("processing.pages_total", { n: totalPages })}</span>
            )}
            <span className="processing-eta-sep"> · </span>
            {/* fix-all v2 LOW F6: clock isolated in its own component */}
            <ElapsedClock
              startedAt={startedAt}
              estimatedSeconds={estimatedSeconds}
              done={done}
              errorStage={errorStage}
            />
          </div>
        )}
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
                  <div className="sub mono">{stageSub(s)}</div>
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
              {errorMsg || t("processing.failed_at", { stage: errorStage })}
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
