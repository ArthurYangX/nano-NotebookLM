/* global React, Library, Reader, Notes, MindMap, Quiz, ExamPrep, Settings, Assistant, Processing, API, StudyState,
   SAMPLE_SOURCES, TweaksPanel, useTweaks, TweakSection,
   TweakRadio, TweakSlider, TweakToggle, NOTES_DATA, QUIZ_DATA, MINDMAP */
const { useState, useEffect, useRef } = React;

const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "mindmapLayout": "radial",
  "noteStyle": "outline",
  "serifHeads": true
}/*EDITMODE-END*/;

// 2026-05-20: appearance defaults moved out of the EDITMODE block. Theme /
// density / baseSize are now end-user controls in Settings (persisted to
// localStorage), not design-host knobs. The dev-only TweaksPanel no longer
// renders these — see the Appearance section removed alongside this.
const APPEARANCE_DEFAULTS = {
  theme: "paper",
  density: "comfortable",
  baseSize: 15,
};

// CitationPreviewModal — opens a small floating window when the user clicks
// a Notes citation chip. Hands the file off to the browser's native PDF
// viewer via `<iframe>` + `#page=N` anchor (same approach as reader.jsx
// DocumentPdfFrame). Non-PDF sources and missing files fall through to the
// legacy Reader-tab path before this component is rendered.
function CitationPreviewModal({ preview, onClose, onOpenInReader }) {
  // `preview` truthy/null is the only thing that gates the listener.
  // `onClose` is intentionally NOT in deps — including it would re-bind
  // the listener on every parent render (App re-renders ~10×/sec while
  // notes are streaming) because callers pass a fresh arrow each time.
  // The closure captures `onClose` from the current render, which is
  // fine — React's setter identity is stable.
  useEffect(() => {
    if (!preview) return;
    function onKey(e) { if (e.key === "Escape") onClose(); }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [preview]);

  // Backdrop click → close, BUT only when the mousedown also originated on
  // the overlay. Without this guard, a text-selection drag that starts in
  // the toolbar and releases on the dark backdrop fires `click` on the
  // overlay (the common ancestor) and dismisses the modal mid-copy. The
  // ref lives across renders; the modal subtree unmounts when preview is
  // null, resetting it implicitly.
  const downOnOverlayRef = useRef(false);

  if (!preview) return null;
  const { courseId, docId, sourceFile, page } = preview;
  const url = API.sourceFileUrl(courseId, docId, { page });

  return (
    <div
      className="pdf-preview-overlay"
      role="dialog"
      aria-modal="true"
      onMouseDown={(e) => { downOnOverlayRef.current = e.target === e.currentTarget; }}
      onClick={(e) => {
        const ok = downOnOverlayRef.current && e.target === e.currentTarget;
        downOnOverlayRef.current = false;
        if (ok) onClose();
      }}
    >
      <div className="pdf-preview-modal">
        <div className="pdf-preview-toolbar">
          <div className="pdf-preview-title" title={sourceFile}>
            <span className="pdf-preview-filename">{sourceFile}</span>
            {page ? <span className="pdf-preview-page mono"> · p.{page}</span> : null}
          </div>
          <div className="pdf-preview-actions">
            <button
              className="pdf-preview-action mono"
              onClick={onOpenInReader}
              title="切换到 Reader 标签页全屏查看"
            >在 Reader 中打开 ↗</button>
            <button
              className="pdf-preview-close"
              onClick={onClose}
              aria-label="关闭预览"
              title="关闭 (Esc)"
            >✕</button>
          </div>
        </div>
        <iframe
          className="pdf-preview-frame"
          src={url}
          title={sourceFile}
          // No `sandbox` attribute: Chrome's PDFium plugin renders inline
          // PDFs as plugin content, and ANY `sandbox` value (even the
          // permissive `allow-scripts allow-same-origin allow-popups`)
          // suppresses plugin content → iframe shows the broken-doc icon
          // instead of the PDF. We tried adding sandbox in fix-all v1
          // and it broke Reader + modal across all PDFs in Chrome.
          // Defense-in-depth is preserved server-side via
          // `X-Content-Type-Options: nosniff` on `/api/source/.../file`,
          // so a renamed-`.pdf` upload can't ride MIME sniffing into
          // script execution. `referrerpolicy` still strips the Referer
          // header so the loaded subresource can't fingerprint the
          // parent path.
          referrerPolicy="no-referrer"
          allow="fullscreen"
        />
      </div>
    </div>
  );
}

// fix-all #H2: mirror the server-side `COURSE_ID_PATTERN` from api/server.py
// — alphanumeric + space + dot + dash + underscore + CJK Unified Ideographs
// (U+4E00..U+9FFF), 1..128 chars, no `..`, no leading/trailing dot. Catching
// invalid input at the picker is purely defence-in-depth (the server still
// 422s), but it lets us surface a friendly inline error before we write the
// localStorage resume-key (`nano-nlm:v1:upload-task:<id>`), which would
// otherwise be poisoned by `:`, RTL marks, or zero-width chars.
const COURSE_ID_RE = /^[A-Za-z0-9_\-. 一-鿿]{1,128}$/;
function isValidCourseId(s) {
  if (typeof s !== "string") return false;
  if (s.length === 0 || s.length > 128) return false;
  if (!COURSE_ID_RE.test(s)) return false;
  if (s.includes("..") || s.startsWith(".") || s.endsWith(".")) return false;
  return true;
}

// CoursePickerModal — replaces the legacy `prompt()` + `confirm()` + ad-hoc
// `<input type=file>` chain for the upload flow. The modal owns ALL three
// user decisions in one round trip:
//   1) Which course (existing chip click vs. new-name input).
//   2) Which PDF engine (PyMuPDF / MinerU radio).
//   3) Which files (hidden `<input type=file>` triggered SYNCHRONOUSLY from
//      the chip / new-form React click handler — this is the only way to
//      keep transient user activation alive through the modal's interaction
//      chain. The previous design `await pickCourseId()` → `confirm()` →
//      `document.createElement('input').click()` lost the activation token
//      somewhere along the await chain, and Chrome/Safari silently dropped
//      the file picker — symptom: user picks course + clicks OK on the
//      MinerU confirm, then nothing happens).
// fix-all #H1: chips hand back `c.id` (directory key), NOT `c.name` (display
// label) — a renamed course has `meta.name != cid` and `/api/upload/{course_id}`
// is keyed on cid.
function CoursePickerModal({ courses, defaultId, defaultEngine, onPick, onCancel }) {
  const [newName, setNewName] = useState("");
  const [engine, setEngine] = useState(defaultEngine === "mineru" ? "mineru" : "pymupdf");
  // Hidden file input + the course id captured at chip / form-submit time.
  // We can't pass the id through the event because the file dialog opens
  // asynchronously and onChange fires later in its own task.
  const fileInputRef = useRef(null);
  const pendingIdRef = useRef(null);
  const onCancelRef = useRef(onCancel);
  // Keep ref pointed at the latest onCancel without re-binding the listener;
  // see fix-all #H4 below.
  useEffect(() => { onCancelRef.current = onCancel; });
  // fix-all #H4: deps `[]` (not `[onCancel]`) so we bind the keydown listener
  // exactly once per mount. The parent re-creates `onCancel` on every App
  // render (App re-renders often during note streaming), and the previous
  // `[onCancel]` caused a cleanup+rebind churn matching the exact anti-pattern
  // CitationPreviewModal already documents at the top of this file. The ref
  // bridges the closure so Esc still calls the latest onCancel.
  useEffect(() => {
    function onKey(e) { if (e.key === "Escape") onCancelRef.current(); }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const trimmed = newName.trim();
  // Duplicate detection is case- and whitespace-insensitive over BOTH
  // the course id and the course display name. " Foo " or "foo" typed
  // against an existing `id="Foo"` must not create a sibling course.
  const existingKeys = new Set();
  for (const c of courses) {
    if (typeof c?.id === "string") existingKeys.add(c.id.trim().toLowerCase());
    if (typeof c?.name === "string") existingKeys.add(c.name.trim().toLowerCase());
  }
  const trimmedKey = trimmed.toLowerCase();
  const duplicateNew = trimmed.length > 0 && existingKeys.has(trimmedKey);
  const newInputValid = trimmed.length === 0 || isValidCourseId(trimmed);
  // Only block submit when the typed name actually fails validation. Empty
  // input still disables submit via `!trimmed` below; `newInputValid` is
  // there to surface the inline error for non-empty bad input.
  const submitDisabled = !trimmed || duplicateNew || !newInputValid;

  // User-gesture critical: this MUST be called synchronously inside the
  // React onClick / onSubmit handler. Any `await` between the user's click
  // and `fileInputRef.current.click()` invalidates transient activation
  // and Chrome silently drops the file dialog. No setState / no
  // microtask between here and the click.
  function triggerFileChooser(courseId) {
    pendingIdRef.current = courseId;
    const el = fileInputRef.current;
    if (el) {
      el.value = "";  // allow re-picking the same files after a prior cancel
      el.click();
    }
  }

  function handleChipClick(cid) {
    if (!isValidCourseId(cid)) return;
    triggerFileChooser(cid);
  }

  function submitNew(e) {
    if (e) e.preventDefault();
    if (submitDisabled) return;
    triggerFileChooser(trimmed);
  }

  function handleFileChange(e) {
    const files = e.target.files;
    // User cancelled the OS file dialog — modal stays open so they can try
    // a different course / engine. Reset target.value so the same files
    // can be picked again next time.
    if (!files || !files.length) {
      e.target.value = "";
      return;
    }
    const cid = pendingIdRef.current;
    if (!cid) return;
    onPick(cid, files, engine);
  }

  return (
    <div
      className="course-picker-overlay"
      role="dialog"
      aria-modal="true"
      onClick={e => { if (e.target === e.currentTarget) onCancel(); }}
    >
      <div className="course-picker-modal">
        <div className="course-picker-head">
          <div className="course-picker-title">上传到哪个课程？</div>
          <button
            className="course-picker-close"
            onClick={onCancel}
            aria-label="关闭"
            title="关闭 (Esc)"
          >✕</button>
        </div>

        {courses.length > 0 ? (
          <div className="course-picker-section">
            <div className="course-picker-label">添加到已有课程</div>
            <div className="course-picker-existing">
              {courses.map(c => {
                // fix-all #M2: chip click is defence-in-depth — today the
                // backend only ever serves ids that already passed
                // `_ensure_safe_course_id`, but if a future endpoint adds
                // user-supplied / imported / shared course metadata the chip
                // would hand the raw value to localStorage + the URL path.
                // A non-conforming id renders disabled with a tooltip.
                const cid = typeof c?.id === "string" ? c.id : "";
                const valid = isValidCourseId(cid);
                const label = typeof c?.name === "string" && c.name ? c.name : cid;
                return (
                  <button
                    key={cid || label}
                    className={
                      "course-picker-chip"
                      + (valid && cid === defaultId ? " is-default" : "")
                      + (valid ? "" : " is-invalid")
                    }
                    onClick={() => handleChipClick(cid)}
                    disabled={!valid}
                    title={valid ? label : `课程 id 不规范: ${cid}`}
                  >
                    <span className="course-picker-chip-name">{label}</span>
                    {typeof c?.chunks === "number" && c.chunks > 0
                      ? <span className="course-picker-chip-meta">{c.chunks} chunks</span>
                      : null}
                  </button>
                );
              })}
            </div>
          </div>
        ) : null}

        <div className="course-picker-section">
          <div className="course-picker-label">
            {courses.length > 0 ? "或新建课程" : "新建课程"}
          </div>
          <form className="course-picker-newform" onSubmit={submitNew}>
            <input
              className="course-picker-input"
              type="text"
              value={newName}
              onChange={e => setNewName(e.target.value)}
              placeholder="输入新课程名称"
              autoFocus
            />
            <button
              type="submit"
              className="course-picker-create"
              disabled={submitDisabled}
              title={
                duplicateNew
                  ? "已存在同名课程，请直接点上方按钮"
                  : !newInputValid
                  ? "名称含非法字符 — 仅支持字母 / 数字 / 中文 / 空格 / . - _"
                  : ""
              }
            >新建并上传</button>
          </form>
          {duplicateNew ? (
            <div className="course-picker-warn">
              已存在同名课程「{trimmed}」 — 请直接点上方按钮，或换一个名字。
            </div>
          ) : !newInputValid ? (
            <div className="course-picker-warn">
              名称仅支持字母 / 数字 / 中文 / 空格以及 . - _，且不能含 ".." 或以 "." 开头结尾。
            </div>
          ) : null}
        </div>

        <div className="course-picker-section course-picker-engine-row">
          <div className="course-picker-label">PDF 提取引擎</div>
          <div className="course-picker-engine-choices">
            <label className={"course-picker-engine-choice" + (engine === "pymupdf" ? " is-on" : "")}>
              <input
                type="radio"
                name="course-picker-engine"
                checked={engine === "pymupdf"}
                onChange={() => setEngine("pymupdf")}
              />
              <span className="course-picker-engine-name">PyMuPDF</span>
              <span className="course-picker-engine-meta">默认 · 毫秒级 · 不解析公式</span>
            </label>
            <label className={"course-picker-engine-choice" + (engine === "mineru" ? " is-on" : "")}>
              <input
                type="radio"
                name="course-picker-engine"
                checked={engine === "mineru"}
                onChange={() => setEngine("mineru")}
              />
              <span className="course-picker-engine-name">MinerU</span>
              <span className="course-picker-engine-meta">高质量 · ~10s/页 · LaTeX + 表格</span>
            </label>
          </div>
        </div>

        {/* Hidden file input — triggered synchronously by chip / submitNew
            handlers so transient user activation is still valid when the OS
            opens the picker. accept list mirrors the previous ad-hoc input. */}
        <input
          ref={fileInputRef}
          type="file"
          multiple
          accept=".pdf,.pptx,.docx,.md,.txt"
          onChange={handleFileChange}
          style={{ display: "none" }}
          aria-hidden="true"
          tabIndex={-1}
        />
      </div>
    </div>
  );
}

function App() {
  const tweaks = useTweaks(TWEAK_DEFAULTS);

  // 2026-05-20: TweaksPanel is dev-only (gated on parent-window postMessage
  // from the design host) — never reaches end users. We hoist theme /
  // density / base font size into App state with localStorage persistence
  // so the Settings page can expose them. TWEAK_DEFAULTS still drives the
  // initial value, keeping the EDITMODE block as the single source of
  // defaults for both the design host and the runtime app.
  const [theme, setTheme] = useState(() => {
    try { return window.localStorage.getItem("nano-nlm:v1:theme") || APPEARANCE_DEFAULTS.theme; }
    catch (e) { return APPEARANCE_DEFAULTS.theme; }
  });
  const [density, setDensity] = useState(() => {
    try { return window.localStorage.getItem("nano-nlm:v1:density") || APPEARANCE_DEFAULTS.density; }
    catch (e) { return APPEARANCE_DEFAULTS.density; }
  });
  const [baseSize, setBaseSize] = useState(() => {
    try {
      const v = parseInt(window.localStorage.getItem("nano-nlm:v1:base-size") || "", 10);
      // Must match the Settings slider min/max (13–18) — a wider range here
      // would let stale localStorage values survive hydration that the UI
      // can't reproduce, locking the user out of fixing the size.
      return Number.isFinite(v) && v >= 13 && v <= 18 ? v : APPEARANCE_DEFAULTS.baseSize;
    } catch (e) { return APPEARANCE_DEFAULTS.baseSize; }
  });
  // For Auto mode: the resolved theme ("paper" or "dark") so Settings can
  // show "Auto · 现在 = Dark". Updated by the theme effect below.
  const [autoResolved, setAutoResolved] = useState("paper");
  const commitTheme = React.useCallback((v) => {
    setTheme(v);
    try { window.localStorage.setItem("nano-nlm:v1:theme", v); } catch (e) {}
  }, []);
  const commitDensity = React.useCallback((v) => {
    setDensity(v);
    try { window.localStorage.setItem("nano-nlm:v1:density", v); } catch (e) {}
  }, []);
  const commitBaseSize = React.useCallback((v) => {
    setBaseSize(v);
    try { window.localStorage.setItem("nano-nlm:v1:base-size", String(v)); } catch (e) {}
  }, []);

  const [mode, setMode] = useState("reader");
  const [sources, setSources] = useState([]);
  const [activeId, setActiveId] = useState(null);
  const [activePage, setActivePage] = useState(null);
  const [highlightedId, setHighlightedId] = useState(null);
  const [highlightedNode, setHighlightedNode] = useState(null);
  const [citationNotice, setCitationNotice] = useState("");
  // Citation navigation epoch — bumped on EVERY citation dispatch so the
  // Reader can react even when activeId + activePage are unchanged (e.g.
  // clicking the same `lecture_8.pdf, p.85` twice in a row). Without this
  // React short-circuits the no-op state writes, the iframe `src` stays
  // identical, and the second click silently does nothing.
  const [navEpoch, setNavEpoch] = useState(0);
  // In-Notes PDF preview modal: shows the cited page in a floating window
  // instead of yanking the user out to the Reader tab. Set by
  // handleCitationPreview when the source is a PDF; null otherwise (the
  // user is in the Notes view but no chip has been clicked, or the cited
  // source is non-PDF and fell back to the Reader-tab path).
  const [pdfPreview, setPdfPreview] = useState(null);
  // Promise-resolver state for the course-picker modal. Holds the pending
  // `resolve` function so `onStartUpload` can `await pickCourseId()` and the
  // modal's chip / form callbacks can hand the chosen id back. `null` ↔
  // modal closed. The ref below mirrors the same value so the App-unmount
  // cleanup can settle a pending promise without keeping `coursePickerResolve`
  // in its deps (which would re-fire on every set).
  const [coursePickerResolve, setCoursePickerResolve] = useState(null);
  const coursePickerResolveRef = useRef(null);
  useEffect(() => { coursePickerResolveRef.current = coursePickerResolve; });
  // fix-all #H3: if <App> unmounts (HMR, future Vite migration, hard route
  // change) while the picker is open, the awaiting `onStartUpload` would
  // otherwise hang forever — the resolver closure leaks and the file-input
  // closure with it. Settle with null on unmount so the awaiter sees the
  // same "user cancelled" branch.
  useEffect(() => () => {
    const fn = coursePickerResolveRef.current;
    if (fn) fn(null);
  }, []);
  const [uploading, setUploading] = useState(null);
  const [processing, setProcessing] = useState(null);
  const [streaming, setStreaming] = useState(false);
  const [streamProgress, setStreamProgress] = useState(0);
  const [generationState, setGenerationState] = useState(StudyState.createGenerationState());

  // ── Core state ──
  const [courses, setCourses] = useState([]);
  const [activeCourse, setActiveCourse] = useState(null);
  // Frontend-only hidden-course set (per-browser localStorage). Backend
  // /api/courses is unaware — the data stays on disk, only the dropdown
  // filters it out. Initialised from storage in the courses-load effect.
  const [hiddenCourseIds, setHiddenCourseIds] = useState(() =>
    typeof localStorage !== "undefined" ? StudyState.loadHiddenCourses(localStorage) : []
  );
  const [showCourseManager, setShowCourseManager] = useState(false);
  const [backendStatus, setBackendStatus] = useState(null);
  const [realNotes, setRealNotes] = useState(null);
  const [realQuiz, setRealQuiz] = useState(null);
  const [realMindmap, setRealMindmap] = useState(null);
  const [examAnalysis, setExamAnalysis] = useState(null);
  const [reportData, setReportData] = useState(null);
  // 2026-05-20: masteryData state retired — UI consumers (topbar ◎ icon,
  // SkillsDashboard card) were removed once Exam Prep covered the loop.
  // Backend /api/mastery + mastery_tracker still live but have no frontend
  // reader. Restore by un-deleting this state + the `kind === "mastery"`
  // branch in `handleSkillEntry` if a new UI surface needs it.
  const [sessionDays, setSessionDays] = useState({});

  // ── R3-2: explicit user language preference ──
  // Initialised from localStorage via the StudyState helpers (single source
  // of truth — also used by Node-side tests). When null we render a one-time
  // modal blocking the workspace until the user picks zh / en. Topbar chip
  // shows the current value and re-opens the modal so the choice is reversible.
  const [userLang, setUserLangState] = useState(() => StudyState.loadUserLang(window.localStorage));
  const [showLangModal, setShowLangModal] = useState(false);
  // ── Backend chip: openai / claude / local ──
  // Default = openai (the configured main provider). Selection persists in
  // localStorage; the chip greys out if the chosen backend isn't configured.
  const [backend, setBackend] = useState(() => {
    try {
      const v = window.localStorage.getItem("nano-nlm:v1:backend");
      if (v === "openai" || v === "claude" || v === "local") return v;
      // Stale value from an older build (or unknown string). Reset
      // storage so cross-tab listeners + the post-`/api/status`
      // rollback effect agree on the new default.
      if (v != null) {
        try { window.localStorage.setItem("nano-nlm:v1:backend", "openai"); } catch (e) {}
      }
      return "openai";
    } catch (e) { return "openai"; }
  });
  function commitBackend(value) {
    setBackend(value);
    try { window.localStorage.setItem("nano-nlm:v1:backend", value); }
    catch (e) {}
  }
  // ── R5/MinerU: extraction engine preference ─────────────────────
  // `pymupdf` (default, fast, drops formulae) or `mineru` (slow ~10s/page,
  // recovers LaTeX equations + HTML tables + figures). Persisted as
  // `nano-nlm:v1:upload-engine` — global, not per-course, matches the
  // backend chip convention.
  const [uploadEngine, setUploadEngine] = useState(() => {
    try {
      const v = window.localStorage.getItem("nano-nlm:v1:upload-engine");
      return v === "mineru" ? "mineru" : "pymupdf";
    } catch (e) { return "pymupdf"; }
  });
  function commitUploadEngine(value) {
    setUploadEngine(value);
    try { window.localStorage.setItem("nano-nlm:v1:upload-engine", value); }
    catch (e) {}
  }
  // 2026-05-12: user-customisable assistant name. Surfaced via the
  // Settings tab (⚙ icon button in the topbar opens it); flows to
  // /api/chat's `persona` field → qa_skill injects it into every system
  // prompt path so "你是谁" returns the chosen name. Empty → backend
  // falls back to DEFAULT_PERSONA ("Study Assistant"). Length capped to
  // 40 chars by the server-side Pydantic validator (PERSONA_MAX_LEN);
  // the frontend cap mirrors it as defense in depth.
  const PERSONA_MAX = 40;
  const [persona, setPersona] = useState(() => {
    try {
      const v = window.localStorage.getItem("nano-nlm:v1:persona");
      return (v || "").slice(0, PERSONA_MAX);
    } catch (e) { return ""; }
  });
  function commitPersona(value) {
    const next = (value || "").slice(0, PERSONA_MAX);
    // review-swarm fix-all (LOW R3-C): short-circuit identity writes —
    // Settings input fires onBlur on every focus loss, including
    // tabbing through without edits. Without this guard each blur
    // would cascade a localStorage write + setPersona re-render +
    // Assistant prop change (no-op but still ~10ms of React work).
    if (next === persona) return;
    setPersona(next);
    try {
      if (next) window.localStorage.setItem("nano-nlm:v1:persona", next);
      else window.localStorage.removeItem("nano-nlm:v1:persona");
    } catch (e) {}
  }
  function commitUserLang(code) {
    if (StudyState.saveUserLang(window.localStorage, code)) {
      setUserLangState(code);
      setShowLangModal(false);
    }
  }
  // First-render guard: if no preference is persisted yet, open the modal.
  // Re-running on mount (not on every state change) keeps the modal off when
  // the user already picked, including across hot-reloads.
  useEffect(() => {
    if (userLang == null) setShowLangModal(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── localStorage helpers (per-course persistence) ──
  const STORAGE_PREFIX = "nano-nlm:v1";
  function storageKey(courseId, kind) {
    return `${STORAGE_PREFIX}:${courseId || "_all_"}:${kind}`;
  }
  function loadCached(courseId, kind) {
    try {
      const raw = localStorage.getItem(storageKey(courseId, kind));
      return raw ? JSON.parse(raw) : null;
    } catch { return null; }
  }
  function saveCached(courseId, kind, value) {
    try {
      if (value == null) localStorage.removeItem(storageKey(courseId, kind));
      else localStorage.setItem(storageKey(courseId, kind), JSON.stringify(value));
    } catch {}
  }


  // fix-all v1 #A6: retry button needs to re-run the last upload. We
  // can't pass a closure through processing state (function identity
  // breaks; React DevTools complains; gc'd on rerender). Use a ref.
  const retryRef = useRef(null);
  // Background-upload poll state: { iv, task_id, courseName } — the
  // interval handle lives here so resume-on-mount + the runUpload caller
  // can both clear it. Cleared on done / error / unmount.
  const pollRef = useRef(null);
  // review-swarm H2 (2026-05-16): App unmount cleanup. The root <App/>
  // rarely unmounts in production, but React.StrictMode double-mount
  // (dev), HMR reloads, and a future Vite migration would otherwise
  // leak the 1.5s setInterval — the orphaned closure keeps calling
  // setProcessing on a defunct tree and hammering /api/upload/status
  // indefinitely. Empty dep array so the cleanup fires only on real
  // unmount, not on every render.
  useEffect(() => () => {
    if (pollRef.current) {
      try { clearInterval(pollRef.current.iv); } catch { /* nop */ }
      pollRef.current = null;
    }
  }, []);
  // review-swarm v2 fix-now #2: the sources-load effect now lists
  // hiddenCourseIds in its dep array (for the All-Courses recompute).
  // In specific-course mode, hiddenCourseIds-only changes were also
  // causing a full re-fetch + setActiveId(null), which snapped the
  // Reader off the user's current source. Track the last specific-
  // course we fetched for so we can skip the reset on no-op re-runs.
  const lastFetchedSpecificCourse = useRef(null);

  // ── Hidden-course toggle handlers ─────────────────────────────────
  function toggleCourseHidden(courseId) {
    if (!courseId) return;
    const next = StudyState.setCourseHidden(localStorage, courseId,
      !hiddenCourseIds.includes(courseId));
    setHiddenCourseIds(next);
  }
  function unhideAllCourses() {
    StudyState.clearHiddenCourses(localStorage);
    setHiddenCourseIds([]);
  }

  // R5-2 fix-all v3: hard-delete a course (artifacts + indices + per-course
  // localStorage cache). User-driven only — invoked from the "管理" modal
  // after a `window.confirm` (we hold a 2-step confirm for safety: first
  // confirm the action, then require the user to type the course name).
  // Side effects:
  //   1. Backend DELETE /api/courses/{cid} rmtrees artifacts/courses/<cid>/
  //      + per-course indices, then rebuilds global index.
  //   2. Frontend clears every `nano-nlm:v1:<cid>:*` key so the resurfacing
  //      helper (findCoursesWithCache) doesn't immediately re-add the
  //      course on the next mount.
  //   3. Drops the course from `courses` state; if it was active, falls
  //      back to the first remaining visible course (or null = All).
  async function handleDeleteCourse(courseId) {
    if (!courseId) return;
    const step1 = window.confirm(
      `彻底删除课程 "${courseId}"？\n\n` +
      `这会移除：\n` +
      ` - artifacts/courses/${courseId}/  (chunks / KG / notes / quizzes / exam bank)\n` +
      ` - 该课程的 FAISS + BM25 索引\n` +
      ` - 浏览器中该课程的所有 localStorage 缓存\n\n` +
      `该操作不可撤销。若是预置课程，删除后无法回滚（rollback hatch 失效）。`
    );
    if (!step1) return;
    const typed = window.prompt(
      `再次确认：请输入完整课程 ID（区分大小写）以执行删除：\n${courseId}`
    );
    if (typed == null) return;
    if (typed.trim() !== courseId) {
      alert(`输入不匹配：你输入了 "${typed}" 但期望 "${courseId}"。已取消。`);
      return;
    }
    try {
      const data = await API.deleteCourse(courseId);
      // 1. Drop from courses state immediately.
      setCourses(prev => prev.filter(c => c.id !== courseId));
      // 2. If deleted course was active, fall back to the first remaining
      //    visible course (or All Courses).
      if (activeCourse === courseId) {
        const hidden = new Set(hiddenCourseIds);
        const next = courses.find(c => c.id !== courseId && !hidden.has(c.id));
        setActiveCourse(next ? next.id : null);
      }
      // 3. Purge per-course localStorage cache (notes / highlights / KG /
      //    quiz / exam-prep / notes-toc-collapsed / notes-scroll-y / etc).
      try {
        const prefix = `nano-nlm:v1:${courseId}:`;
        const victims = [];
        for (let i = 0; i < localStorage.length; i++) {
          const k = localStorage.key(i);
          if (k && k.startsWith(prefix)) victims.push(k);
        }
        victims.forEach(k => localStorage.removeItem(k));
      } catch { /* private browsing / quota — non-fatal */ }
      // 4. Also un-hide it in case it was in hiddenCourseIds (so a
      //    future course with the same id doesn't start hidden).
      if (hiddenCourseIds.includes(courseId)) {
        const next = StudyState.setCourseHidden(localStorage, courseId, false);
        setHiddenCourseIds(next);
      }
      const removedCount = (data.removed || []).length;
      alert(`已删除课程 "${courseId}"（${removedCount} 个文件 / 目录）。`);
    } catch (e) {
      const status = e && e.status;
      if (status === 404) {
        alert(`课程 "${courseId}" 已不存在（可能在另一标签页已删除）。`);
        // Best-effort refresh so the state catches up.
        setCourses(prev => prev.filter(c => c.id !== courseId));
      } else {
        alert(`删除失败：${e.message || "未知错误"}`);
      }
    }
  }
  // If the active course just got hidden, jump to the first visible one
  // (or to All Courses if every course is now hidden).
  useEffect(() => {
    if (!activeCourse) return;
    if (!hiddenCourseIds.includes(activeCourse)) return;
    const next = courses.find(c => !hiddenCourseIds.includes(c.id));
    setActiveCourse(next ? next.id : null);
  }, [hiddenCourseIds, courses, activeCourse]);

  // review-swarm v2 fix-soon #8: collections (Library's "Collections"
  // sidebar) now lives in React state and is passed as a prop. The old
  // implementation mutated `window.SAMPLE_COLLECTIONS` after-the-fact
  // in a useEffect — Library happened to re-render on hide-toggle
  // (because hiddenCourseIds is App state) and pick up the new global,
  // but any path that re-rendered Library independently would see
  // stale data. Lifting to state eliminates the implicit-global
  // coupling. data.jsx still seeds the initial `window.SAMPLE_COLLECTIONS`
  // for any code that hasn't migrated yet (none left in frontend/).
  const collections = React.useMemo(() => {
    const colors = [
      "oklch(0.42 0.08 160)", "oklch(0.48 0.12 25)",
      "oklch(0.45 0.1 255)", "oklch(0.44 0.09 310)",
      "oklch(0.46 0.11 50)", "oklch(0.43 0.08 200)",
      "oklch(0.47 0.10 100)", "oklch(0.41 0.09 280)",
    ];
    const hidden = new Set(hiddenCourseIds);
    const visible = courses.filter(c => !hidden.has(c.id));
    return visible.map((c, i) => ({
      id: c.id, name: c.name, count: c.chunks, color: colors[i % colors.length],
    }));
  }, [courses, hiddenCourseIds]);

  // ── Load courses on mount ──
  useEffect(() => {
    API.getCourses().then(data => {
      const crs = data.courses || [];
      setCourses(crs);
      const hidden = new Set(StudyState.loadHiddenCourses(localStorage));
      const firstVisible = merged.find(c => !hidden.has(c.id));
      if (firstVisible) setActiveCourse(firstVisible.id);
      // Resume-on-mount: a pending upload-task entry in localStorage
      // means a previous page-load started an upload that the
      // background task may still be processing. Probe each course's
      // pending task; mount the processing modal for the (single)
      // course we activate, evict completed/dead entries for others.
      _resumePendingUploads(merged, firstVisible ? firstVisible.id : null);
    }).catch(() => {});
    API.getStatus().then(setBackendStatus).catch(() => {});
  }, []);

  // Mount-time resume of background uploads. Single modal at a time:
  // only the active course's pending task triggers the processing UI.
  // Other courses' pending tasks are polled once and pruned if the
  // server has forgotten / finished them, but no modal is mounted.
  async function _resumePendingUploads(courseList, activeId) {
    const candidates = [];
    for (const c of courseList) {
      const key = `nano-nlm:v1:upload-task:${c.id}`;
      let raw;
      try { raw = localStorage.getItem(key); } catch { continue; }
      if (!raw) continue;
      let parsed;
      try { parsed = JSON.parse(raw); } catch { try { localStorage.removeItem(key); } catch {} continue; }
      if (!parsed || !parsed.task_id) {
        try { localStorage.removeItem(key); } catch {}
        continue;
      }
      candidates.push({ courseId: c.id, ...parsed });
    }
    if (!candidates.length) return;
    for (const cand of candidates) {
      const isActive = cand.courseId === activeId;
      let s;
      try { s = await API.getUploadStatus(cand.task_id); }
      catch { continue; /* transient — try again on next mount */ }
      if (!s) {
        try { localStorage.removeItem(`nano-nlm:v1:upload-task:${cand.courseId}`); } catch {}
        continue;
      }
      if (s.status === "done" || s.status === "error") {
        try { localStorage.removeItem(`nano-nlm:v1:upload-task:${cand.courseId}`); } catch {}
        continue;
      }
      if (!isActive) {
        // review-swarm H3 (2026-05-16): non-active in-flight candidate.
        // Don't mount a modal (single-modal invariant), but schedule a
        // lightweight one-shot recheck so the localStorage key gets
        // cleaned up when the server-side task eventually terminates.
        // 90s strikes a balance for MinerU's ~10-30 min pipelines —
        // most uploads complete within 1-2 rechecks; in the worst case
        // the user just sees a slightly stale key until their next
        // visit to that course (resume runs again on mount).
        _scheduleInactiveUploadCleanup(cand.courseId, cand.task_id);
        continue;
      }
      // Active course has an in-flight task — mount the processing modal
      // and start polling. retryPayload is null because original File
      // objects are gone from JS memory after reload.
      setProcessing({
        file: (cand.file_names && cand.file_names[0]) || "uploading...",
        step: 0,
        stages: s.stages,
        errorStage: s.error_stage,
        errorMsg: s.error,
        done: false,
        retryPayload: null,
      });
      _startUploadPolling(cand.task_id, cand.courseId);
    }
  }

  // review-swarm H3 (2026-05-16): for in-flight uploads on non-active
  // courses, poll once every 90s with no UI side effects. On terminal
  // status (done / error / 404), drop the localStorage hint so future
  // mounts don't show a stale entry. Self-clears on terminal or after
  // ~15 min ceiling (10 attempts × 90s). Cap protects against an
  // accidentally-stuck task pinning the timer forever.
  function _scheduleInactiveUploadCleanup(courseId, task_id) {
    const key = `nano-nlm:v1:upload-task:${courseId}`;
    let attempts = 0;
    const MAX_ATTEMPTS = 10;
    const tick = async () => {
      attempts += 1;
      let s = null;
      try { s = await API.getUploadStatus(task_id); }
      catch { /* transient — try again */ }
      // 404 (s === null) or terminal → cleanup.
      if (s === null || (s && (s.status === "done" || s.status === "error"))) {
        try { localStorage.removeItem(key); } catch { /* nop */ }
        return;
      }
      if (attempts >= MAX_ATTEMPTS) return; // ceiling — give up silently
      setTimeout(tick, 90000);
    };
    setTimeout(tick, 90000);
  }

  useEffect(() => {
    // ±20% jitter so concurrent tabs don't poll the server in lockstep.
    const POLL_BASE_MS = 10000;
    const POLL_JITTER_RATIO = 0.2;
    const interval = POLL_BASE_MS + (Math.random() * 2 - 1) * POLL_BASE_MS * POLL_JITTER_RATIO;
    const iv = setInterval(() => {
      API.getStatus().then(setBackendStatus).catch(() => setBackendStatus(null));
    }, interval);
    return () => clearInterval(iv);
  }, []);

  // Auto-rollback the chip when the selected backend isn't configured
  // on the server (e.g. user picked "local" but operator removed
  // LOCAL_LLM_BASE_URL from .env). Falls back to "openai", then to the
  // first available backend, so the chip never points at a 422 path.
  useEffect(() => {
    if (!backendStatus) return;
    const available = new Set(backendStatus.available_backends || backendStatus.backends || []);
    if (available.size && !available.has(backend)) {
      const fallback = available.has("openai") ? "openai" : [...available][0];
      if (fallback) commitBackend(fallback);
    }
  }, [backendStatus, backend]);

  // ── Close the citation preview modal when the user navigates away from
  // Notes (Reader/Quiz/Mindmap/Skills/History). The modal is the Notes
  // view's affordance; leaving it open across tabs floats stale PDF
  // chrome on top of an unrelated workspace. We don't include
  // `pdfPreview` in deps — only `mode` — because we want the close to
  // fire on the mode transition, not on the (impossible) modal-open
  // during a non-notes mode.
  useEffect(() => {
    if (mode !== "notes") setPdfPreview(null);
  }, [mode]);

  // ── Load sources when course changes; restore generated content from cache ──
  useEffect(() => {
    // Restore previous generated content for this course (if any).
    // R4-6 LaTeX migration: pre-R4-6 caches hold Markdown ("## section",
    // "- bullet"). The LaTeX preview shim can't render that — the user
    // would see literal '##' text. Detect and discard so the placeholder
    // CTA appears instead, prompting a regenerate that produces LaTeX.
    const cachedNotes = loadCached(activeCourse, "notes");
    if (cachedNotes && !StudyState.isLatexNotesContent(cachedNotes)) {
      if (activeCourse) saveCached(activeCourse, "notes", null);
      console.info(
        `[nano-nlm] discarded pre-R4-6 markdown notes cache for ${activeCourse || "(no course)"}; click Generate to produce fresh LaTeX`
      );
      setRealNotes(null);
    } else {
      setRealNotes(cachedNotes);
    }
    setRealQuiz(loadCached(activeCourse, "quiz"));
    const cachedMm = loadCached(activeCourse, "mindmap");
    setRealMindmap(cachedMm);
    if (cachedMm) window.MINDMAP = cachedMm;
    setExamAnalysis(loadCached(activeCourse, "exam-analysis"));
    setReportData(loadCached(activeCourse, "report"));
    setGenerationState(StudyState.createGenerationState());
    // Cross-course leak guard: a citation modal opened for the *previous*
    // course must close on switch. Otherwise the iframe keeps showing
    // courseA's PDF while the UI labels everything as courseB, and the
    // "Open in Reader" button would dispatch a stale `nav` whose
    // `activeId` no longer exists in the new `sources` list.
    setPdfPreview(null);

    if (!activeCourse) {
      // "All Courses" mode — show sources from every VISIBLE course.
      // Hidden courses are excluded so cross-course search and citation
      // resolution match the dropdown's visible scope.
      lastFetchedSpecificCourse.current = null;
      setSources([]);
      const visible = courses.filter(c => !hiddenCourseIds.includes(c.id));
      Promise.all(visible.map(c => API.getSources(c.id).catch(() => ({ sources: [] }))))
        .then(results => {
          const allSrcs = [];
          results.forEach((data, ci) => {
            // BUGFIX: use visible[ci], not courses[ci]. After filtering
            // out hidden courses, the index `ci` points into `visible` —
            // indexing into `courses` (the unfiltered array) shifted
            // each chunk's labelled course by however many earlier
            // courses were hidden.
            const cid = visible[ci].id;
            (data.sources || []).forEach((s, i) => {
              // `fileType` is the raw backend enum value (pdf/pptx/docx/
              // md/txt) used by the citation modal to decide preview vs
              // Reader fallback. `type` is the legacy 3-way display tag
              // (pdf/ppt/txt) — kept distinct so the Library icon mapping
              // doesn't have to learn the full enum and so a future PPTX
              // preview branch can opt in by reading `fileType` directly.
              allSrcs.push({
                id: `${cid}_${s.id || i}`,
                docId: s.id || null,
                courseId: cid,
                fileType: s.type || null,
                type: s.type === "pdf" ? "pdf" : s.type === "pptx" ? "ppt" : "txt",
                title: `[${cid}] ${s.title}`,
                sourceFile: s.title,
                meta: `${s.chunks} chunks`,
                checked: true,
                collection: cid,
                viewableAsPdf: !!s.viewable_as_pdf,
              });
            });
          });
          setSources(allSrcs);
          // If the previously-active file belongs to a course that just
          // got hidden, `activeId` is now stale (no longer in allSrcs).
          // Fall back to the first visible source so the Reader pane
          // doesn't sit on a dead id (which `activeIdInSources` would
          // freeze, leaving the user looking at stale content).
          setActiveId(prev => (prev && allSrcs.some(s => s.id === prev))
            ? prev
            : (allSrcs[0] ? allSrcs[0].id : null));
        });
      return;
    }

    // review-swarm v2 fix-now #2: this branch resets activeId + sources
    // every time the effect re-runs. With hiddenCourseIds in deps, the
    // effect re-runs on every hide-toggle — even when the user is in
    // specific-course mode reading lecture 5. That snapped the Reader
    // back to lecture 1. Now we short-circuit when the active course
    // hasn't actually changed.
    if (lastFetchedSpecificCourse.current === activeCourse) {
      // Cached-content restore + session log re-run is harmless (setState
      // bail-out on === for identical values); the expensive part is the
      // API fetch + setActiveId(null) reset, which we skip here.
      API.getSessionLog().then(data => setSessionDays(data.days || {})).catch(() => {});
      return;
    }
    lastFetchedSpecificCourse.current = activeCourse;
    // Clear activeId + sources synchronously when activeCourse changes.
    // Without this the Reader briefly sees (new course, old course's
    // activeId, old course's sources) — the `activeIdInSources` guard
    // there incorrectly passes against the stale sources list and fires a
    // (new_course, old_doc_id) fetch that 404s. The new course's sources
    // and a fresh activeId are populated once getSources resolves below.
    setActiveId(null);
    setActivePage(null);
    setSources([]);
    API.getSources(activeCourse).then(data => {
      const srcs = (data.sources || []).map((s, i) => ({
        id: s.id || `s${i}`,
        docId: s.id || null,
        courseId: activeCourse,
        fileType: s.type || null,
        type: s.type === "pdf" ? "pdf" : s.type === "pptx" ? "ppt" : "txt",
        title: s.title,
        sourceFile: s.title,  // raw filename, used for backend filter
        meta: `${s.chunks} chunks`,
        checked: true, // All checked by default
        collection: "main",
        viewableAsPdf: !!s.viewable_as_pdf,
      }));
      setSources(srcs);
      if (srcs.length > 0) setActiveId(srcs[0].id);
    }).catch(() => {});
    API.getSessionLog().then(data => setSessionDays(data.days || {})).catch(() => {});
    // hiddenCourseIds in deps: re-run when the user un/hides a course
    // while in "All Courses" mode, so the cross-course source list
    // matches the dropdown's visible scope. courses also in deps so the
    // initial load resolves once courses arrive.
  }, [activeCourse, courses, hiddenCourseIds]);

  // ── Theme ──
  // `auto` follows the OS via `prefers-color-scheme`: dark scheme → dark
  // theme, otherwise the light Paper baseline. We listen for OS-level
  // changes so the user doesn't need to refresh when switching modes.
  // For paper we `removeAttribute` rather than setting an empty string so
  // future `[data-theme]` (attribute-presence) selectors don't false-match.
  useEffect(() => {
    const root = document.documentElement;
    const apply = () => {
      let resolved = theme;
      if (theme === "auto") {
        const prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
        resolved = prefersDark ? "dark" : "paper";
        setAutoResolved(resolved);
      }
      if (resolved === "paper") root.removeAttribute("data-theme");
      else root.setAttribute("data-theme", resolved);
    };
    apply();
    if (theme === "auto" && window.matchMedia) {
      const mq = window.matchMedia("(prefers-color-scheme: dark)");
      const onChange = () => apply();
      mq.addEventListener ? mq.addEventListener("change", onChange) : mq.addListener(onChange);
      return () => {
        mq.removeEventListener ? mq.removeEventListener("change", onChange) : mq.removeListener(onChange);
      };
    }
  }, [theme]);
  useEffect(() => {
    document.body.style.setProperty("--density", density === "compact" ? "0.92" : density === "airy" ? "1.08" : "1");
    document.body.style.setProperty("--base-size", baseSize + "px");
  }, [density, baseSize]);

  // ── Get checked source file names for context filtering ──
  // Delegates to StudyState.getCheckedSourceFiles which returns raw filenames
  // (matching chunk.source_file) so the backend qa_skill filter actually hits.
  //
  // R5-2 fix-all v7: when EVERY source is checked (the default state on
  // course load), the user did NOT pin a subset — return `null` instead of
  // the full list so qa_skill's "user pinned files → skip graphrag" branch
  // doesn't fire on the default. Previously the assistant sent the full
  // checked list to `/api/chat`, qa_skill saw `checked_files = [<all>]`,
  // skipped graphrag, and short course-specific queries like "什么是精度"
  // bounced through RAG → translation → cross-course → general because
  // BM25 char-bigram alone couldn't bridge the query→chunk gap that the KG
  // would have spanned.
  function getCheckedSourceFiles() {
    const all = StudyState.getCheckedSourceFiles(sources);
    if (!Array.isArray(all) || all.length === 0) return null;
    // Count UI sources actually toggleable: ignore non-source rows just in
    // case Library state ever wraps another shape.
    const visibleSourceCount = (sources || []).filter(s => s).length;
    if (visibleSourceCount > 0 && all.length === visibleSourceCount) {
      return null;
    }
    return all;
  }

  // ── API actions ──
  // Notes generation now runs the full-course pipeline: per-file parallel
  // LLM calls (concurrency=4), programmatic \section{} concat, then one
  // LLM review pass for terminology/cross-ref polish. As each per-file
  // result lands we append its \section{...} to the visible draft so the
  // user sees progress immediately; once the review pass starts streaming
  // we swap the draft for the streamed partial; on done we install the
  // final reviewed body.
  // Incremental cache UI (2026-05-11): set by the `plan` event so the
  // toolbar can show "⚡ 10 cached · 1 fresh" stats during streaming.
  // Cleared when streaming starts and on course switch.
  const [noteCacheStats, setNoteCacheStats] = useState(null);
  // Truncation surface: backend tags file_done / done events when the
  // upstream LLM stopped at max_output_tokens / finish_reason='length'.
  // Shape: null | { files: string[], review: bool }. Cleared when
  // streaming starts and on course switch (alongside noteCacheStats).
  const [notesTruncated, setNotesTruncated] = useState(null);
  // Review-pass progress chip (2026-05-13): the second LLM pass polishes
  // the merged per-file draft (unifies terminology, adds cross-refs,
  // collapses duplicate definitions). Previously the frontend reset the
  // visible notes to "" on `reviewing` and re-streamed the polished
  // content from scratch → users perceived it as "regenerated twice".
  // Now we keep the merged draft visible and only flip this flag so
  // the topbar shows a chip explaining what's happening. Cleared when
  // streaming starts and on course switch.
  const [noteReviewing, setNoteReviewing] = useState(false);

  async function handleGenerateNotes({ force = false } = {}) {
    if (!activeCourse) { alert("Please select a specific course first (not 'All Courses')"); return; }
    // Force-regenerate confirm (review-swarm fix-all): the 🔄 button skips
    // the per-file cache and re-runs every section through the LLM. Guard
    // against accidental clicks — cache hits are cheap, force runs cost ~2
    // min + LLM tokens.
    if (force) {
      if (typeof window !== "undefined" && typeof window.confirm === "function") {
        if (!window.confirm("Force-regenerate all sections? This ignores the cache and may take ~2 minutes + LLM cost. Continue?")) {
          return;
        }
      }
    }
    setMode("notes");
    setStreaming(true);
    // review-swarm v2 fix-now #1: drop the saved scroll offset BEFORE
    // notes streaming begins so the new document mounts at top instead
    // of restoring an offset into the previous version. Scoped here
    // (not in a generic `streaming` effect) so quiz/mindmap/report
    // regenerations don't accidentally wipe the notes scroll cache.
    if (typeof localStorage !== "undefined") {
      StudyState.clearNotesScroll(localStorage, activeCourse);
    }
    setStreamProgress(0);
    setNoteCacheStats(null);
    setNotesTruncated(null);
    setNoteReviewing(false);
    setGenerationState(StudyState.createGenerationState());
    const fileSections = [];
    // fix-all v1 #19: mirror backend's _escape_latex_title (in
    // nano_notebooklm/skills/notes_full_course.py) — strip directory
    // components and escape the LaTeX-special set. Earlier code only
    // escaped `[{}\\]`, which let a filename like `chapter_3.pdf` slip
    // into the mid-stream draft with a raw underscore; if the user hit
    // PDF compile before the review pass overwrote the draft, tectonic
    // would choke on the unescaped `_`.
    function escapeLatexTitle(name) {
      const base = String(name || "untitled").split("/").pop() || "untitled";
      let out = "";
      for (const ch of base) {
        if ("&%$#_{}".includes(ch)) out += "\\" + ch;
        else if (ch === "\\") out += "\\textbackslash{}";
        else if (ch === "~") out += "\\textasciitilde{}";
        else if (ch === "^") out += "\\textasciicircum{}";
        else out += ch;
      }
      return out;
    }
    // Partial-state persistence: when review pass is too slow to finish
    // (5+ chapter courses can take 10+ min server-side), if the user reloads
    // before `done` fires, the try block's saveNoteDraft never runs and
    // localStorage retains a stale older version. Persist after each
    // sanitized file_done / file_cached so reload at least restores the
    // per-file baseline (no cross-file polish, but all sections present).
    function persistPartialDraft(body) {
      if (!activeCourse) return;
      const content = String(body || "");
      if (!content) return;
      try { StudyState.saveNoteDraft(localStorage, activeCourse, content); }
      catch (e) { /* best-effort partial save */ }
    }
    function rebuildDraftFromFiles() {
      // 2026-05-12: include `running` files too so token-streamed
      // file_delta deltas show up in the rendered draft. Earlier code
      // only emitted `done`/`cached` here, which meant the entire
      // per-file phase rendered as an empty document under the
      // "Generating..." overlay — defeating the whole point of
      // file_delta streaming. The terminal file_done event overwrites
      // the accumulated partial with the sanitized authoritative body,
      // so this can't ship unsanitized content past the LLM call.
      //
      // 2026-05-13 batch split: when one source_file is split into
      // multiple batches (large PDFs > MAX_CHUNKS_PER_FILE chunks),
      // each batch arrives as a separate `fileSections[idx]` entry
      // with the same source_file. Wrap `\section{<file>}` only the
      // FIRST time we see a given source_file in plan-index order;
      // continuation batches append their content directly. Mirrors
      // the backend's concat_draft fix.
      const seen = new Set();
      return fileSections
        .filter(f => f && (f.status === "done" || f.status === "cached" || f.status === "running") && f.content)
        .map(f => {
          let body = String(f.content || "").replace(/^\\section\{[^}]*\}\s*/, "");
          if (seen.has(f.source_file)) {
            return body;
          }
          seen.add(f.source_file);
          return `\\section{${escapeLatexTitle(f.source_file)}}\n${body}`;
        })
        .join("\n\n");
    }
    let reviewPartial = "";
    let inReview = false;
    // 2026-05-13: review pass no longer pushes deltas to `realNotes`
    // mid-stream. Previously each `review_chunk` accumulated into
    // `reviewPartial` then scheduleReviewUpdate() called
    // `setRealNotes(reviewPartial)` every ~250ms — the visible document
    // reset to empty on the first review_chunk (since reviewPartial
    // started as "") and re-grew from scratch, which users perceived as
    // "notes regenerated twice". We keep accumulating `reviewPartial`
    // locally for two reasons: (a) terminal swap at end-of-stream uses
    // `final.content || reviewPartial` as the canonical body, (b) the
    // error catch block surfaces it as the retry-state partial. The
    // `noteReviewing` chip (set on the `reviewing` event) gives users
    // the progress feedback that mid-stream rendering used to provide.
    // No reviewSetTimer needed — nothing to throttle.
    // Coalesce setRealNotes during cache-batch (review-swarm fix-all):
    // when N file_cached events arrive back-to-back, each previously
    // triggered an O(N) rebuildDraftFromFiles + latexToHtml render. Defer
    // to a single render at the next event-loop turn.
    let cachedBatchPending = false;
    function scheduleCachedRender() {
      if (cachedBatchPending) return;
      cachedBatchPending = true;
      setTimeout(() => {
        cachedBatchPending = false;
        const draft = rebuildDraftFromFiles();
        if (!inReview) setRealNotes(draft);
        // Partial persistence: each batch of file_done / file_cached
        // saves the assembled per-file body so reload-before-done
        // restores complete-but-unreviewed content (better than the
        // months-old stale draft localStorage would otherwise return).
        persistPartialDraft(draft);
      }, 0);
    }
    // file_delta (2026-05-12): per-file streaming now ships ~10-20
    // deltas/sec per file × up to 4 concurrent files = 40-80 events/sec.
    // Each setRealNotes invalidates the latexToHtml useMemo and re-runs
    // the 8-stage regex pipeline on the merged draft, so throttle to
    // 250ms (mirrors scheduleReviewUpdate) to keep the UI responsive.
    let fileDeltaTimer = null;
    function scheduleFileDeltaRender() {
      if (fileDeltaTimer) return;
      fileDeltaTimer = setTimeout(() => {
        fileDeltaTimer = null;
        if (!inReview) setRealNotes(rebuildDraftFromFiles());
      }, 250);
    }
    try {
      const final = await API.streamFullCourseNotes(activeCourse, event => {
        if (event.type === "plan") {
          for (let i = 0; i < event.total; i += 1) {
            const f = event.files && event.files[i] ? event.files[i] : null;
            fileSections[i] = {
              source_file: f ? f.source_file : `file_${i}`,
              status: "pending",
              content: null,
              error: null,
              cached: !!(f && f.cached),
              // 2026-05-13 batch split metadata, used by the TOC + the
              // streaming progress chip to show "lecture_8.pdf · 1/2".
              batchIndex: f && typeof f.batch_index === "number" ? f.batch_index : 0,
              batchTotal: f && typeof f.batch_total === "number" ? f.batch_total : 1,
            };
          }
          setStreamProgress(0);
          // Incremental cache stats: backend reports cached_count + fresh_count
          // plus the `force` echo. Frontend toolbar uses this to render
          // "⚡ 10 cached · ⏳ 1 fresh" so the user knows why generation is fast.
          setNoteCacheStats({
            total: event.total,
            // Nullish-coalescing (review-swarm fix-all): a legitimate
            // `cached_count: 0` / `fresh_count: 0` (all-fresh / all-cached
            // scenarios) must NOT fall back to `event.total`; `||` mis-
            // routes 0 to the fallback and the chip then shows e.g.
            // "N cached · 0 fresh" instead of the correct counts.
            cached: event.cached_count ?? 0,
            fresh: event.fresh_count ?? event.total,
            force: !!event.force,
          });
        } else if (event.type === "file_start") {
          if (fileSections[event.idx]) {
            fileSections[event.idx].status = "running";
            fileSections[event.idx].source_file = event.source_file || fileSections[event.idx].source_file;
            // Reset content so file_delta can accumulate from scratch
            // — protects against a retry that lands on the same idx.
            fileSections[event.idx].content = "";
          }
        } else if (event.type === "file_delta") {
          // 2026-05-12: token-streamed per-file output. Accumulate the
          // delta into the section's content + render-throttle so the
          // user sees text growing in real time, not a 20s frozen
          // "Generating..." followed by a single dump.
          if (fileSections[event.idx]) {
            const prev = fileSections[event.idx].content || "";
            fileSections[event.idx].content = prev + (event.delta || "");
          }
          scheduleFileDeltaRender();
        } else if (event.type === "file_cached") {
          // Incremental cache (2026-05-11): backend short-circuits the
          // LLM call when per_file_cache.json has a matching chunk_hash.
          // Same payload shape as file_done; track the `cached: true`
          // flag so the UI can ⚡-flag the section.
          if (fileSections[event.idx]) {
            fileSections[event.idx].status = "cached";
            fileSections[event.idx].content = event.content;
            fileSections[event.idx].source_file = event.source_file || fileSections[event.idx].source_file;
          }
          scheduleCachedRender();
          setStreamProgress(p => p + 1);
        } else if (event.type === "file_done") {
          if (fileSections[event.idx]) {
            fileSections[event.idx].status = "done";
            // file_done.content is the SANITIZED final body (server-
            // side check() may differ from the raw accumulated deltas
            // by stripped whitespace). Always overwrite — truth-pin.
            fileSections[event.idx].content = event.content;
            fileSections[event.idx].source_file = event.source_file || fileSections[event.idx].source_file;
            fileSections[event.idx].truncated = !!event.truncated;
          }
          if (event.truncated) {
            // Surface in the toolbar chip immediately, not just on `done`,
            // so the user sees "⚠️ 1 truncated" while later files are
            // still streaming.
            setNotesTruncated(prev => {
              const files = new Set((prev && prev.files) || []);
              if (event.source_file) files.add(event.source_file);
              return { files: Array.from(files), review: !!(prev && prev.review) };
            });
          }
          // Cancel any pending throttled file_delta render — the
          // immediate render below installs the authoritative content.
          if (fileDeltaTimer) { clearTimeout(fileDeltaTimer); fileDeltaTimer = null; }
          scheduleCachedRender();
          setStreamProgress(p => p + 1);
        } else if (event.type === "file_error") {
          if (fileSections[event.idx]) {
            fileSections[event.idx].status = "error";
            fileSections[event.idx].error = event.error;
          }
          setStreamProgress(p => p + 1);
        } else if (event.type === "merging") {
          // Programmatic concat is instant — no UI swap needed; the draft
          // already shows the assembled sections from file_done events.
        } else if (event.type === "reviewing") {
          inReview = true;
          reviewPartial = "";
          // Show the review-pass chip so the user knows the second LLM
          // call is running (term unification / cross-refs). The merged
          // draft from the per-file pass stays on screen until the final
          // setRealNotes(content) below swaps in the polished body.
          setNoteReviewing(true);
        } else if (event.type === "review_chunk") {
          // Backend ships `delta` only — review_chunk would otherwise be
          // O(N²) on the wire. Accumulate locally for the terminal swap
          // and retry-state surface, but DON'T setRealNotes here — see
          // the comment on `reviewPartial`/`inReview` above.
          reviewPartial = reviewPartial + (event.delta || "");
          setGenerationState(s => StudyState.recordPartialGeneration(s, event.delta || ""));
        } else if (event.type === "error") {
          setGenerationState(s => StudyState.recordGenerationFailure(
            { ...s, partial: event.partial || reviewPartial || rebuildDraftFromFiles() },
            new Error(event.error || "stream_failed"),
            (s.failures || 0) + 1,
          ));
        }
      }, { userLang, force });
      // Cancel any pending throttled file_delta render — the next
      // setRealNotes below installs the canonical final content.
      if (fileDeltaTimer) { clearTimeout(fileDeltaTimer); fileDeltaTimer = null; }
      if (final && final.type === "error") throw new Error(final.error || "stream_failed");
      const content = (final && final.content) || reviewPartial || rebuildDraftFromFiles() || "Notes generation failed.";
      setRealNotes(content);
      setNoteReviewing(false);
      saveCached(activeCourse, "notes", content);
      StudyState.saveNoteDraft(localStorage, activeCourse, content);
      // Truncation surface: backend's terminal `done` event carries
      // `review_truncated: bool` and `files_truncated: string[]`. Merge
      // them in over whatever the per-file events already accumulated
      // — `done` is authoritative for the review-pass flag (per-file
      // events can't know it).
      if (final && (final.review_truncated || (final.files_truncated && final.files_truncated.length))) {
        setNotesTruncated({
          files: Array.isArray(final.files_truncated) ? final.files_truncated : [],
          review: !!final.review_truncated,
        });
      }
      // fix-all v1 #18: backend's /api/notes/full-course/stream writes
      // its own session-log row (kind="notes-full-course"); previously
      // this followed up with a second row (kind="notes") on every
      // success, double-counting in any future kind aggregation.
    } catch (e) {
      // Clear pending throttled renders BEFORE setting the error state —
      // otherwise a fire ~250ms later would overwrite the error banner
      // with a stale partial draft via setRealNotes(rebuildDraftFromFiles()).
      if (fileDeltaTimer) { clearTimeout(fileDeltaTimer); fileDeltaTimer = null; }
      const msg = "Error: " + e.message;
      const partial = reviewPartial || rebuildDraftFromFiles();
      setRealNotes(prev => prev || partial || msg);
      // Persist whatever progress we made (review_chunk accumulation
      // wins over per-file baseline when review was in flight). Keeps
      // a reload after a stream abort from reverting to a stale draft.
      persistPartialDraft(partial);
      setGenerationState(s => StudyState.recordGenerationFailure(s, e, (s.failures || 0) + 1));
      setNoteReviewing(false);
    }
    setStreaming(false);
  }

  async function handleGenerateQuiz(topic = null) {
    if (topic && typeof topic !== "string") topic = null;
    if (!activeCourse) { alert("Please select a specific course first"); return; }
    setMode("quiz");
    setStreaming(true);
    setStreamProgress(0);
    try {
      const data = await API.generateQuiz(activeCourse, topic, 6, "medium", { userLang });
      const quiz = data.quiz || data || [];
      setRealQuiz(quiz);
      if (Array.isArray(quiz) && quiz.length > 0) saveCached(activeCourse, "quiz", quiz);
      await API.appendSessionLog(activeCourse, "generation", { kind: "quiz", topic }).catch(() => {});
    } catch (e) {
      setRealQuiz([]);
      setGenerationState(s => StudyState.recordGenerationFailure(s, e, (s.failures || 0) + 1));
    }
    setStreaming(false);
  }

  async function handleGenerateMindmap() {
    if (!activeCourse) { alert("Please select a specific course first"); return; }
    setMode("mindmap");
    setStreaming(true);
    try {
      const data = await API.getMindmap(activeCourse);
      window.MINDMAP = data;
      setRealMindmap(data);
      if (data) saveCached(activeCourse, "mindmap", data);
      await API.appendSessionLog(activeCourse, "generation", { kind: "mindmap" }).catch(() => {});
    } catch (e) {
      setRealMindmap(null);
    }
    setStreaming(false);
  }

  async function handleSkillEntry(kind) {
    if (!activeCourse) { alert("Please select a specific course first"); return; }
    setStreaming(true);
    try {
      if (kind === "exam-analysis") {
        const data = await API.analyzeExam(activeCourse);
        setExamAnalysis(data);
        saveCached(activeCourse, "exam-analysis", data);
      } else if (kind === "report") {
        let partial = "";
        const final = await API.streamReport(activeCourse, "summary", false, event => {
          if (event.type === "chunk") {
            partial = event.partial;
            setReportData({ content: partial });
            setStreamProgress(p => p + 1);
          }
        }, { userLang });
        const data = { content: (final && final.content) || partial };
        setReportData(data);
        saveCached(activeCourse, "report", data);
      }
      setMode("notes");
      await API.appendSessionLog(activeCourse, "generation", { kind }).catch(() => {});
    } catch (e) {
      if (kind === "exam-analysis") setExamAnalysis({ error: e.message });
      if (kind === "report") setReportData({ error: e.message });
    }
    setStreaming(false);
  }

  // Reader-tab dispatch shared by handleCitation AND the modal's
  // "Open in Reader" button. Accepting a pre-resolved `nav` lets the
  // modal avoid re-parsing refText (the parser sees `sources` at click
  // time, which may have changed between modal-open and modal-close).
  function dispatchNavToReader(nav, notice) {
    setCitationNotice(notice || "");
    setActiveId(nav.activeId);
    setActivePage(nav.page);
    setHighlightedId(nav.highlightedId);
    setMode("reader");
    setNavEpoch(e => e + 1);
  }

  function handleCitation(refText) {
    const nav = StudyState.resolveCitationNavigation(refText, sources);
    if (!nav.ok) {
      setCitationNotice(nav.message);
      return;
    }
    dispatchNavToReader(nav, "");
  }

  // Notes-view citation click: try to open the cited page in a floating
  // PDF modal first. Falls back to the Reader tab when:
  //   • the source isn't a PDF (pptx/docx/md/txt need text-mode rendering)
  //   • the source object is missing docId / courseId / fileType
  //   • the file is gone from disk (HEAD probe returns 404 — common after
  //     a re-ingest that drops a previously-indexed file)
  // The HEAD probe is best-effort: any error other than a clean 404 lets
  // the modal open anyway (the iframe will show whatever the browser can
  // render, and the user can hit "Open in Reader" themselves).
  async function handleCitationPreview(refText) {
    const nav = StudyState.resolveCitationNavigation(refText, sources);
    if (!nav.ok) {
      setCitationNotice(nav.message);
      return;
    }
    const source = (sources || []).find((s) => s.id === nav.activeId);
    const decision = StudyState.shouldPreviewCitation(source);
    if (!decision.canPreview) {
      // Surface *why* we're falling back so the user isn't surprised by a
      // silent tab switch after expecting a modal.
      dispatchNavToReader(nav, decision.reason);
      return;
    }
    // HEAD preflight: degrade to Reader text-mode when the file is gone
    // from disk. Server returns 404 from `_resolve_source_path` failure.
    try {
      const probeUrl = API.sourceFileUrl(source.courseId, source.docId, {});
      const head = await fetch(probeUrl, { method: "HEAD" });
      if (head.status === 404) {
        dispatchNavToReader(nav, "源文件不在磁盘 · 在 Reader 文本视图查看");
        return;
      }
    } catch {
      // Network blip / CORS / server doesn't support HEAD on old build.
      // Open the modal anyway; iframe handles its own failure modes.
    }
    setCitationNotice("");
    setPdfPreview({
      courseId: source.courseId,
      docId: source.docId,
      sourceFile: source.sourceFile || source.title,
      page: nav.page,
      nav,  // captured so "Open in Reader" doesn't re-parse refText
    });
    // Telemetry: per-user-tab session log so we can measure preview vs
    // tab-switch usage without standing up Prometheus.
    if (typeof API !== "undefined" && API.appendSessionLog) {
      API.appendSessionLog(activeCourse, "citation_preview", {
        doc_id: source.docId,
        page: nav.page,
      }).catch(() => {});
    }
  }

  function handleOpenPreviewInReader() {
    if (!pdfPreview) return;
    const nav = pdfPreview.nav;
    const docId = pdfPreview.docId;
    setPdfPreview(null);
    if (nav) {
      dispatchNavToReader(nav, "");
      if (typeof API !== "undefined" && API.appendSessionLog) {
        API.appendSessionLog(activeCourse, "citation_preview_to_reader", {
          doc_id: docId,
        }).catch(() => {});
      }
    }
  }

  function handleMindmapSource(chunk) {
    // Prefer the original-document path (PDF preview modal) over the
    // Reader text view: `handleCitationPreview` opens an in-place PDF
    // iframe when the resolved source is a `.pdf` (or pptx-with-sidecar)
    // and falls back to the Reader for `.md`/`.docx`/`.txt`. Previously
    // this went straight to `handleCitation`, so a KG concept anchored
    // to a PDF chunk would route the user into Reader text mode instead
    // of the underlying slide — which read as "the KG only knows about
    // text" from the student's perspective.
    //
    // 2026-05-13: fall back from `chunk.page` (pdf/docx) to `chunk.slide`
    // (pptx) so KG citations on pptx courses no longer all land on
    // page 1. The pptx sidecar PDFs preserve slide order, so slide N
    // ≈ page N in the rendered PDF. If neither is present, parse the
    // location string (e.g. "Slide 3/97" / "Page 75/122") as a last
    // resort, then default to 1.
    let pageNum = chunk.page || chunk.slide;
    if (!pageNum && typeof chunk.location === "string") {
      const m = chunk.location.match(/(?:Slide|Page|第)\s*([0-9]+)/i);
      if (m) pageNum = Number(m[1]);
    }
    pageNum = pageNum || 1;
    const ref = `[Source: ${chunk.source_file || ""}, PDF p.${pageNum}, chunk ${chunk.chunk_id || ""}]`;
    handleCitationPreview(ref);
  }

  async function handleRetryGeneration() {
    setGenerationState(s => StudyState.retryGeneration(s));
    await handleGenerateNotes();
  }

  // Background-upload polling helper. Used by both onStartUpload (fresh
  // upload) and the mount-time resume effect (page reload mid-upload).
  // Polls /api/upload/status/{task_id} every 1.5s; on done | error,
  // clears the interval + localStorage entry. Transient network errors
  // are swallowed so a brief blip doesn't kill the modal.
  function _startUploadPolling(task_id, courseName) {
    if (pollRef.current) {
      try { clearInterval(pollRef.current.iv); } catch { /* nop */ }
      pollRef.current = null;
    }
    // review-swarm M2 (2026-05-16): track consecutive failures so a
    // sustained 5xx outage doesn't spam the endpoint at 40 req/min
    // forever. After MAX consecutive transient errors, surface a
    // transport error and stop polling — the user can retry manually.
    let warned = false;
    let failures = 0;
    const MAX_FAILURES = 10;
    const iv = setInterval(async () => {
      let s;
      try {
        s = await API.getUploadStatus(task_id);
      } catch (e) {
        failures += 1;
        if (!warned) {
          warned = true;
          if (typeof console !== "undefined") {
            console.warn("upload status poll transient error:", e && e.message);
          }
        }
        if (failures >= MAX_FAILURES) {
          clearInterval(iv);
          pollRef.current = null;
          setProcessing(p => p ? { ...p, errorStage: "transport", errorMsg: "状态轮询连续失败，请稍后重试" } : null);
        }
        return;
      }
      failures = 0;
      if (!s) {
        // 404 — server lost the task (eviction / restart). Drop the
        // localStorage trace and surface an error.
        clearInterval(iv);
        pollRef.current = null;
        try { localStorage.removeItem(`nano-nlm:v1:upload-task:${courseName}`); } catch { /* nop */ }
        setProcessing(p => p ? { ...p, errorStage: "transport", errorMsg: "上传任务已不可恢复，请重试" } : null);
        return;
      }
      warned = false;
      setProcessing(p => p ? {
        ...p,
        stages: s.stages || p.stages,
        done: s.status === "done",
        errorStage: s.error_stage || (s.status === "error" ? "unknown" : null),
        errorMsg: s.error || null,
      } : p);
      if (s.status === "done" || s.status === "error") {
        clearInterval(iv);
        pollRef.current = null;
        try { localStorage.removeItem(`nano-nlm:v1:upload-task:${courseName}`); } catch { /* nop */ }
        // Refresh course list (chunks may have landed even on KG-stage
        // error) and activate the new course on success.
        try {
          const data = await API.getCourses();
          setCourses(data.courses || []);
          if (s.status === "done") {
            setActiveCourse(courseName);
          }
        } catch { /* best-effort refresh */ }
      }
    }, 1500);
    pollRef.current = { iv, task_id, courseName };
  }

  // Promise-based wrapper around the course-picker modal — owns the entire
  // user-interaction phase (course + engine + files) in a single round trip
  // so transient user activation stays intact through to the OS file dialog.
  // Resolves with `{ courseId, files, engine }` or null on Cancel/Esc/backdrop.
  // fix-all #H1: returns the course_id (directory key), not the display name.
  // 2026-05-20: rewired from the old `pickCourseId()` → `confirm()` →
  // `document.createElement('input').click()` chain because that chain's
  // post-await `input.click()` was being silently blocked by Chrome's
  // transient-activation rule.
  function pickCourseAndFiles() {
    // Concurrent-call guard: if a picker is already open, resolve the new
    // promise immediately with null instead of clobbering the stored
    // resolver (which would leave the first promise pending forever). The
    // outer `onStartUpload` guard already blocks the typical double-click,
    // but a future caller could re-enter; cheap insurance.
    if (coursePickerResolveRef.current) return Promise.resolve(null);
    return new Promise(resolve => {
      setCoursePickerResolve(() => (value) => {
        setCoursePickerResolve(null);
        resolve(value);
      });
    });
  }

  async function onStartUpload() {
    if (uploading) return;
    // review-swarm M1 (2026-05-16): a double-click on the upload button
    // would otherwise post a second upload immediately, overwrite the
    // localStorage key with the new task_id, and leave the first task
    // orphaned (still running server-side, never visible to the user).
    // Block while a modal is mounted and not yet terminal.
    if (processing && !processing.done && !processing.errorStage) {
      try { console.warn("upload already in flight — ignoring duplicate trigger"); } catch {}
      return;
    }
    // Modal handles course + engine + file picking in one user-gesture
    // window. Cancel / Esc / backdrop click resolves with null.
    const picked = await pickCourseAndFiles();
    if (!picked) return;
    const { courseId, files, engine } = picked;
    if (!courseId || !files || !files.length) return;
    const courseName = courseId;
    const chosenEngine = engine === "mineru" ? "mineru" : "pymupdf";
    if (chosenEngine !== uploadEngine) commitUploadEngine(chosenEngine);

    // Background-task upload (2026-05-16): POST returns {task_id} immediately,
    // then a 1.5s setInterval polls /api/upload/status/{task_id} until
    // status === "done" | "error". localStorage tracks the active task so a
    // tab reload can resume the polling without losing the modal.
    const runUpload = async (files) => {
      setUploading({ name: files[0].name + (files.length > 1 ? ` (+${files.length - 1})` : ""), pct: 0 });
      // Old-style fake progress for the topbar chip — done in ~3s so the
      // chip dismisses even though the background task may still run.
      let pct = 0;
      const fakeIv = setInterval(() => {
        pct += 6;
        if (pct >= 90) { clearInterval(fakeIv); setUploading(prev => prev ? { ...prev, pct: 90 } : null); }
        else { setUploading(prev => prev ? { ...prev, pct } : null); }
      }, 200);

      try {
        setProcessing({
          file: files[0].name,
          step: 0,
          stages: { chunking: { progress: 0 }, embedding: { progress: 0 }, kg_stage_a: { progress: 0 }, kg_stage_b: { progress: 0 } },
          errorStage: null,
          errorMsg: null,
          done: false,
          retryPayload: files,
        });
        const { task_id } = await API.startUpload(courseName, files, { engine: chosenEngine, lang: "ch" });
        try {
          localStorage.setItem(
            `nano-nlm:v1:upload-task:${courseName}`,
            JSON.stringify({ task_id, started_at: Date.now(), file_names: Array.from(files).map(f => f.name) })
          );
        } catch { /* localStorage flaky / quota; in-memory state still drives the UI */ }

        clearInterval(fakeIv);
        setUploading(null);

        _startUploadPolling(task_id, courseName);
      } catch (err) {
        clearInterval(fakeIv);
        setUploading(null);
        setProcessing(p => p ? { ...p, errorStage: "transport", errorMsg: err.message } : null);
      }
    };

    // Expose runUpload to the Processing render via retryRef so the
    // retry button re-invokes the upload with the original `files`
    // captured in this closure (preserves courseName + chosenEngine too).
    retryRef.current = runUpload;

    runUpload(files);
  }

  // R4-2: auto-dismiss the processing screen ~1.2s after `done` fires.
  // No longer fakes progress — the stream provides real percentages.
  useEffect(() => {
    if (!processing || !processing.done) return;
    const t = setTimeout(() => setProcessing(null), 1200);
    return () => clearTimeout(t);
  }, [processing?.done]);

  const effectiveMode = processing ? "processing" : mode;
  const activeSources = sources.filter(s => s.checked);
  // visibleCourses applies the frontend-only hidden-course filter. The
  // unfiltered `courses` array stays the source of truth — the manager
  // panel shows ALL courses (with toggle state) and "All Courses" chunk
  // counts use only visible ones (consistent with what the dropdown lists).
  const hiddenSet = new Set(hiddenCourseIds);
  const visibleCourses = courses.filter(c => !hiddenSet.has(c.id));
  const totalChunks = visibleCourses.reduce((sum, c) => sum + (c.chunks || 0), 0);

  // Quiz tab hidden 2026-05-12: Exam Prep (R5-2) supersedes it with topic
  // mastery tracking, variant generation, and per-question history. Backend
  // /api/quiz endpoint, frontend/quiz.jsx, and tests stay in place as a
  // rollback hatch — only the entry point is removed from the nav.
  const tabs = [
    { id: "reader", label: "Reader", num: activeCourse ? "§" : "—" },
    { id: "notes", label: "Notes", num: realNotes ? "✓" : "—" },
    { id: "mindmap", label: "Knowledge Graph", num: realMindmap ? "✓" : "—" },
    { id: "exam-prep", label: "Exam Prep", num: "★" },
    // 2026-05-13: Skills tab hidden — its three cards (Exam Analysis /
    // Course Report / Mastery Dashboard) overlap conceptually with Exam
    // Prep, which has a clearer closed-loop UX. The view + handlers
    // still exist (effectiveMode === "skills" renders normally; the ⌁
    // / ▤ / ◎ topbar icon-buttons keep working) so power users can
    // still reach those reports via the topbar icons, but the tab bar
    // doesn't carry the slot anymore. Restore by uncommenting if you
    // want Skills back in the main nav.
    // { id: "skills", label: "Skills", num: [examAnalysis, reportData, masteryData].filter(Boolean).length || "—" },
    { id: "history", label: "History", num: Object.keys(sessionDays || {}).length || "—" },
    // Settings tab retired 2026-05-12 — single entry point is the ⚙
    // icon-btn in the topbar. The Settings view (effectiveMode==="settings")
    // still renders normally; only the tab-bar trigger is gone.
  ];
  const backendDegraded = !backendStatus || !Array.isArray(backendStatus.backends) || backendStatus.backends.length === 0;

  return (
    <div className="app">
      {/* ========= Top bar ========= */}
      <header className="topbar">
        <div className="brand">
          <span className="mark">nano-NOTEBOOKLM</span>
          <span className="ed mono">v0.1</span>
        </div>
        <div className="crumbs mono">
          <select
            value={activeCourse || ""}
            onChange={e => setActiveCourse(e.target.value || null)}
            style={{ background: "transparent", border: "1px solid var(--paper-3)", borderRadius: 4, padding: "2px 8px", fontFamily: "var(--mono)", fontSize: 11, color: "var(--ink-2)", minWidth: 180 }}
          >
            <option value="">🌐 All Courses ({totalChunks} chunks)</option>
            {visibleCourses.map(c => {
              const flag = c.lang === "zh" ? "🇨🇳" : c.lang === "mixed" ? "🌐" : "🇺🇸";
              return (
                <option key={c.id} value={c.id}>
                  {flag} {c.name} ({c.chunks || 0} chunks)
                </option>
              );
            })}
          </select>
          <button
            className="course-manage-btn mono"
            title="管理课程显示 / Manage course visibility (frontend-only hide; backend data is preserved)"
            onClick={() => setShowCourseManager(true)}
            style={{
              marginLeft: 6, background: "transparent",
              border: "1px solid var(--paper-3)", borderRadius: 4,
              padding: "2px 6px", fontFamily: "var(--mono)", fontSize: 11,
              color: "var(--ink-2)", cursor: "pointer",
            }}
          >
            管理{hiddenCourseIds.length ? ` · ${hiddenCourseIds.length} 已隐藏` : ""}
          </button>
        </div>
        <div className="spacer"></div>
        <div className="topbar-actions">
          <button
            className="lang-chip mono"
            title={userLang ? "Reply language preference (click to change)" : "Pick reply language"}
            onClick={() => setShowLangModal(true)}
            disabled={streaming}
          >
            {userLang === "zh" ? "中" : userLang === "en" ? "EN" : "?"}
          </button>
          {/* Backend chip — cycles through configured backends (openai /
              claude / local). Each click moves to the next available one;
              chip is disabled when only one backend is configured. */}
          {(() => {
            const available = (backendStatus?.available_backends || backendStatus?.backends || []);
            const cycle = ["openai", "claude", "local"].filter(b => available.includes(b));
            const next = () => {
              if (!cycle.length) return backend;
              const i = cycle.indexOf(backend);
              return cycle[(i + 1) % cycle.length];
            };
            const labelFor = (b) => {
              if (b === "claude") return "🧠 " + ((backendStatus?.claude_model) || "Claude");
              if (b === "local") return "💻 " + ((backendStatus?.local_llm_model) || "Local");
              return "🤖 " + ((backendStatus?.openai_model) || "OpenAI");
            };
            const variant = backend === "claude" ? "claude" : backend === "local" ? "local" : "openai";
            const tip = cycle.length > 1 ? "点击切换后端" : "唯一已配置后端";
            return (
              <button
                className={"backend-chip mono backend-" + variant}
                title={tip}
                onClick={() => commitBackend(next())}
                disabled={streaming || cycle.length <= 1}
              >{labelFor(backend)}</button>
            );
          })()}
          <button className="icon-btn" title="Generate Notes (uses cache when available)" onClick={() => handleGenerateNotes()} disabled={streaming}>📝</button>
          <button
            className="icon-btn"
            title="Force regenerate all sections (ignore per-file cache)"
            onClick={() => handleGenerateNotes({ force: true })}
            disabled={streaming}
          >🔄</button>
          {noteCacheStats && (noteCacheStats.cached > 0 || streaming) && (
            <span
              className={"cache-chip mono" + (noteCacheStats.cached > 0 ? " cache-hit" : " cache-miss")}
              title={noteCacheStats.force
                ? "Force regenerate — cache ignored"
                : `${noteCacheStats.cached} cached · ${noteCacheStats.fresh} fresh`}
            >
              {noteCacheStats.force ? "🔄" : "⚡"}{noteCacheStats.cached}/{noteCacheStats.total}
            </span>
          )}
          {noteReviewing && (
            <span
              className="cache-chip mono cache-reviewing"
              title="第二轮：统一术语 / 加交叉引用 / 折叠重复定义。完成后会一次性替换为润色版。"
            >
              ✨ 润色中
            </span>
          )}
          {notesTruncated && (notesTruncated.review || notesTruncated.files.length > 0) && (
            <span
              className="cache-chip mono cache-truncated"
              title={(() => {
                const lines = [];
                if (notesTruncated.files.length > 0) {
                  lines.push("以下文件因输出 token 上限被截断:");
                  lines.push(...notesTruncated.files.map(f => "  · " + f));
                }
                if (notesTruncated.review) {
                  lines.push("review 阶段也被截断 — 笔记结尾可能不完整");
                }
                lines.push("");
                lines.push("提示: 设置 NOTES_PER_FILE_MAX_TOKENS / NOTES_REVIEW_MAX_TOKENS 提高上限后重试");
                return lines.join("\n");
              })()}
            >
              ⚠️ {notesTruncated.files.length + (notesTruncated.review ? 1 : 0)} 截断
            </span>
          )}
          {/* Quiz icon-btn hidden 2026-05-12: superseded by Exam Prep.
              handleGenerateQuiz + /api/quiz remain so Knowledge Graph's
              "Practice 3" affordance and the legacy entry can be restored. */}
          <button className="icon-btn" title="Build Knowledge Graph" onClick={handleGenerateMindmap} disabled={streaming}>🧠</button>
          <button className="icon-btn" title="Exam Analysis" onClick={() => handleSkillEntry("exam-analysis")} disabled={streaming}>⌁</button>
          <button className="icon-btn" title="Course Report" onClick={() => handleSkillEntry("report")} disabled={streaming}>▤</button>
          {/* Mastery Dashboard icon 2026-05-20 retired: superseded by ★ Exam
              Prep tab. Backend mastery_tracker + /api/mastery still wired
              server-side; the frontend state, GET call, and SkillsDashboard
              card were removed at the same time (see notes near
              masteryData declaration). */}
          {/* 2026-05-12: settings entry. Single source of truth — the
              Settings tab was retired from the main tab bar so this icon
              is the only way in. Persona / language / backend / cache
              management all live in the Settings view.

              2026-05-13: do NOT mirror the neighbour icon-buttons'
              `disabled={streaming}`. Those start generations (KG / exam
              analysis / report / mastery) so blocking them mid-stream
              prevents accidental double-fires. Settings is a navigation
              switch to a read-only preferences view — it should stay
              reachable during a notes/quiz/report stream so the user
              can clear cache or change language while waiting.

              Also dismiss any terminal-state processing modal (done or
              error) on click. Without this, an upload that errored
              leaves `processing` non-null with `errorStage` set, which
              locks `effectiveMode = "processing"` (see line ~1195) and
              hides Settings even after mode === "settings". The retry
              button inside the Processing screen is the normal exit,
              but a user who just wants to bail to Settings shouldn't
              be trapped. Mid-flight uploads (no done, no errorStage)
              are NOT dismissed — those still need to finish. */}
          <button
            className={"icon-btn" + (mode === "settings" ? " active" : "")}
            title="Settings (helper name, language, backend, cache)"
            onClick={() => {
              if (processing && (processing.done || processing.errorStage)) {
                setProcessing(null);
              }
              setMode("settings");
            }}
          >⚙</button>
        </div>
      </header>

      {/* ========= Library ========= */}
      <Library
        sources={sources}
        collections={collections}
        activeId={activeId}
        onPick={setActiveId}
        onToggle={(id) => setSources(ss => ss.map(s => s.id === id ? { ...s, checked: !s.checked } : s))}
        onToggleMany={(ids, checked) => {
          // Batch update so Library's 全选/全不选/反选/Shift-click range
          // produces a single React render, not N renders.
          const idSet = new Set(ids);
          setSources(ss => ss.map(s => idSet.has(s.id) ? { ...s, checked: !!checked } : s));
        }}
        onStartUpload={onStartUpload}
        uploading={uploading}
      />

      {/* ========= Main ========= */}
      <main className="main">
        <div className="tabs">
          {tabs.map(t => (
            <button
              key={t.id}
              className={"tab" + (effectiveMode === t.id ? " active" : "")}
              onClick={() => !processing && setMode(t.id)}
              disabled={!!processing}
            >
              <span>{t.label}</span>
              <span className="num mono">{t.num}</span>
            </button>
          ))}
          <div className="spacer"></div>
          <button className="tool mono" style={{ fontSize: 11 }}>{activeSources.length}/{sources.length} sources</button>
        </div>
        <div className="workspace">
          {/* 2026-05-13: the empty-courses CTA is `height/width:100%` and
              `.workspace` is `overflow:hidden`, so when visibleCourses is
              empty (e.g. all courses hidden via 管理 modal) it covers
              whatever mode-specific view rendered below it. Settings and
              History are course-agnostic and are the exact escape hatches
              the user needs in that state ("全部恢复显示" lives in
              Settings) — gate the CTA off those two modes so the user
              can actually reach them. */}
          {(!uploading && !processing
            && visibleCourses.length === 0
            && effectiveMode !== "settings"
            && effectiveMode !== "history") && (
            <div className="empty-courses-cta" data-testid="empty-courses">
              <div className="empty-courses-card">
                <div className="empty-courses-glyph">📂</div>
                <h2>上传文档开始</h2>
                <p>上传一份 PDF / PPTX / DOCX / Markdown，系统会自动抽取章节、构建知识图谱，再驱动问答与笔记。</p>
                <button className="btn-primary" onClick={onStartUpload}>上传第一个文档</button>
              </div>
            </div>
          )}
          {effectiveMode === "reader" && (
            <Reader
              sources={sources}
              activeCourse={activeCourse}
              activeId={activeId}
              activePage={activePage}
              highlightedId={highlightedId}
              notice={citationNotice}
              navEpoch={navEpoch}
              onHighlight={setHighlightedId}
              onCite={handleCitation}
            />
          )}
          {effectiveMode === "notes" && (
            realNotes
              ? <RealNotesView
                  content={realNotes}
                  streaming={streaming}
                  activeCourse={activeCourse}
                  sources={sources}
                  onContentChange={(content) => {
                    setRealNotes(content);
                    saveCached(activeCourse, "notes", content);
                  }}
                  onRetry={handleRetryGeneration}
                  generationState={generationState}
                  onCitation={handleCitationPreview}
                />
              : <ActionPlaceholder
                  title="Study Notes"
                  desc={activeCourse ? `Generate structured study notes for ${activeCourse}` : "Select a course first"}
                  btnLabel={streaming ? "Generating..." : "Generate Notes"}
                  onAction={handleGenerateNotes}
                  disabled={!activeCourse || streaming}
                />
          )}
          {effectiveMode === "mindmap" && (
            realMindmap
              ? <MindMap
                  data={realMindmap}
                  courseId={activeCourse}
                  layout={tweaks.mindmapLayout}
                  highlightedId={highlightedNode}
                  onNodeClick={setHighlightedNode}
                  onSourceClick={handleMindmapSource}
                  /* onPractice unwired 2026-05-12 — Quiz tab hidden; KG's
                     "Practice 3" affordance hides via mindmap.jsx's
                     `onPractice && (...)` guard until Exam Prep wires
                     up a per-concept practice CTA. */
                  onDataChange={(data) => {
                    setRealMindmap(data);
                    if (activeCourse && data) saveCached(activeCourse, "mindmap", data);
                  }}
                />
              : <ActionPlaceholder
                  title="Knowledge Graph"
                  desc={activeCourse ? `Extract concepts and relationships from ${activeCourse} materials` : "Select a course first"}
                  btnLabel={streaming ? "Generating (1-5 min)..." : "Build Knowledge Graph"}
                  onAction={handleGenerateMindmap}
                  disabled={!activeCourse || streaming}
                  hint="Uses AI to analyze course chunks and build a visual concept map."
                />
          )}
          {effectiveMode === "quiz" && (
            realQuiz && realQuiz.length > 0
              ? <RealQuizView questions={realQuiz} activeCourse={activeCourse} onRegenerate={handleGenerateQuiz} regenerating={streaming} />
              : <ActionPlaceholder
                  title="Practice Quiz"
                  desc={activeCourse ? `Generate a practice quiz for ${activeCourse}` : "Select a course first"}
                  btnLabel={streaming ? "Generating..." : "Generate Quiz"}
                  onAction={handleGenerateQuiz}
                  disabled={!activeCourse || streaming}
                />
          )}
          {effectiveMode === "processing" && (
            <Processing
              fileName={processing.file}
              activeStep={processing.step}
              stages={processing.stages}
              errorStage={processing.errorStage}
              errorMsg={processing.errorMsg}
              done={processing.done}
              onRetry={() => {
                // fix-all v1 #A6: re-invoke upload with the SAME files
                // captured at onStartUpload time. After a tab reload
                // (resume-on-mount path) retryPayload is null because
                // File objects don't survive JSON serialization — fall
                // through to a dismiss + user-facing toast so the user
                // knows they need to re-pick files.
                if (retryRef.current && processing.retryPayload) {
                  retryRef.current(processing.retryPayload);
                } else {
                  setProcessing(null);
                  try {
                    alert("原始文件已不在内存中（页面已刷新）。请重新选择文件并上传。");
                  } catch { /* nop */ }
                }
              }}
            />
          )}
          {effectiveMode === "exam-prep" && (
            <ExamPrep activeCourse={activeCourse} userLang={userLang} />
          )}
          {effectiveMode === "skills" && (
            <SkillsDashboard
              activeCourse={activeCourse}
              examAnalysis={examAnalysis}
              reportData={reportData}
              streaming={streaming}
              onRun={handleSkillEntry}
              onPractice={(topic) => handleGenerateQuiz(topic)}
            />
          )}
          {effectiveMode === "history" && (
            <SessionHistory days={sessionDays} />
          )}
          {effectiveMode === "settings" && (
            <Settings
              backendStatus={backendStatus}
              backend={backend}
              onCommitBackend={commitBackend}
              userLang={userLang}
              onPickLang={commitUserLang}
              persona={persona}
              onCommitPersona={commitPersona}
              hiddenCourseIds={hiddenCourseIds}
              onUnhideAll={unhideAllCourses}
              courses={courses}
              theme={theme}
              onCommitTheme={commitTheme}
              autoResolved={autoResolved}
              density={density}
              onCommitDensity={commitDensity}
              baseSize={baseSize}
              onCommitBaseSize={commitBaseSize}
              onStatusRefresh={() => API.getStatus().then(setBackendStatus).catch(() => {})}
            />
          )}
        </div>
      </main>

      {/* ========= Assistant ========= */}
      {/* 2026-05-13: derive the source_file the user is currently viewing
          in Reader (activeId → matching source.sourceFile) and pass it
          down to Assistant so chat sends `active_source_file` to /api/chat.
          The backend uses it as a soft retrieval bias (graphrag boosts
          hits from this file). null when the user has no focused file
          (e.g. All Courses with nothing picked, or sources list empty). */}
      <Assistant
        mode={effectiveMode}
        persona={persona}
        activeSources={activeSources}
        streaming={streaming}
        streamProgress={streamProgress}
        activeCourse={activeCourse}
        onGenerateNotes={handleGenerateNotes}
        onGenerateQuiz={handleGenerateQuiz}
        onGenerateMindmap={handleGenerateMindmap}
        onSkillEntry={handleSkillEntry}
        onCitation={handleCitation}
        checkedFiles={getCheckedSourceFiles()}
        userLang={userLang}
        backend={backend}
        activeSourceFile={(sources.find(s => s.id === activeId) || {}).sourceFile || null}
      />

      {/* ========= Status bar ========= */}
      <footer className="statusbar">
        <div className="item">
          <span className="dot"></span>
          <span>Indexed</span><b>{visibleCourses.length} courses · {totalChunks} chunks</b>
        </div>
        <div className={"item" + (backendDegraded ? " degraded" : "")}>
          <span>Backend</span><b>{backendStatus ? backendStatus.backends.join(", ") || "none" : "..."}</b>
        </div>
        <div className="item">
          <span>Active</span><b>{activeCourse || "—"}</b>
        </div>
        <div className="item">
          <span>Context</span><b>{activeSources.length} / {sources.length} sources</b>
        </div>
        <div className="spacer"></div>
        <div className="item"><span>v0.1.0</span></div>
      </footer>

      {/* ========= Tweaks ========= */}
      {/* 2026-05-20: Appearance section (theme / density / baseSize) moved
          to the Settings page so end users can reach it. The dev-only
          TweaksPanel still hosts Notes / KG layout knobs for the design
          host. 2026-05-12 note about the `tweaks`/`tweakKey` wrapper-prop
          interface mismatch still applies — these calls are inert under
          TweakRadio's real `{label, value, options, onChange}` signature
          and only render at all when __activate_edit_mode arrives. */}
      <TweaksPanel title="Tweaks">
        <TweakSection title="Notes layout">
          <TweakRadio tweaks={tweaks} tweakKey="noteStyle" label="Note style"
            options={[
              { value: "outline", label: "Outline" }, { value: "cornell", label: "Cornell" },
              { value: "cards", label: "Cards" },
            ]} />
        </TweakSection>
        <TweakSection title="Knowledge Graph">
          <TweakRadio tweaks={tweaks} tweakKey="mindmapLayout" label="Layout"
            options={[
              { value: "radial", label: "Radial" }, { value: "tree", label: "Tree (L→R)" },
            ]} />
        </TweakSection>
      </TweaksPanel>

      {/* ========= In-Notes citation PDF preview modal ========= */}
      <CitationPreviewModal
        preview={pdfPreview}
        onClose={() => setPdfPreview(null)}
        onOpenInReader={handleOpenPreviewInReader}
      />

      {/* ========= Upload course picker modal ========= */}
      {coursePickerResolve && (
        <CoursePickerModal
          courses={visibleCourses}
          defaultId={activeCourse || ""}
          defaultEngine={uploadEngine}
          onPick={(courseId, files, engine) => coursePickerResolve({ courseId, files, engine })}
          onCancel={() => coursePickerResolve(null)}
        />
      )}

      {/* ========= R3-2 first-run / re-pick language modal ========= */}
      {showLangModal && (
        <div className="lang-modal-overlay" role="dialog" aria-modal="true">
          <div className="lang-modal">
            <h3 className="lang-modal-title">Choose your reply language</h3>
            <p className="lang-modal-hint">
              The assistant will reply ONLY in this language for chat, notes,
              quiz, and report generations. You can change this anytime via
              the topbar chip.
            </p>
            <div className="lang-modal-choices">
              {StudyState.DEFAULT_LANG_CHOICES.map(c => (
                <button
                  key={c.code}
                  className={"lang-modal-choice" + (userLang === c.code ? " active" : "")}
                  onClick={() => commitUserLang(c.code)}
                >
                  <div className="lang-modal-choice-label">{c.label}</div>
                  <div className="lang-modal-choice-hint mono">{c.hint}</div>
                </button>
              ))}
            </div>
            {userLang && (
              <button
                className="lang-modal-close mono"
                onClick={() => setShowLangModal(false)}
              >Cancel</button>
            )}
          </div>
        </div>
      )}

      {/* ========= Course visibility manager (frontend-only hide) ========= */}
      {showCourseManager && (
        <div className="lang-modal-overlay" role="dialog" aria-modal="true"
             onClick={e => { if (e.target === e.currentTarget) setShowCourseManager(false); }}>
          <div className="lang-modal" style={{ maxWidth: 480, maxHeight: "70vh", overflowY: "auto" }}>
            <h3 className="lang-modal-title">课程显示管理</h3>
            <p className="lang-modal-hint">
              勾掉的课程会从顶栏下拉里隐藏 —
              <b>仅前端</b>过滤，后端 <code>artifacts/courses/</code> 下的数据完整保留。
              换浏览器或清 localStorage 后会重置。<br />
              红色 <b>🗑 删除</b> 按钮则是<b>彻底删除</b>：移除磁盘文件 + 索引 + 浏览器缓存，不可撤销。
            </p>
            <div style={{ display: "flex", flexDirection: "column", gap: 6, margin: "12px 0" }}>
              {courses.length === 0 && (
                <div className="mono" style={{ color: "var(--ink-3)", fontSize: 12 }}>
                  没有课程可管理。
                </div>
              )}
              {courses.map(c => {
                const hidden = hiddenCourseIds.includes(c.id);
                const flag = c.lang === "zh" ? "🇨🇳" : c.lang === "mixed" ? "🌐" : "🇺🇸";
                return (
                  <div
                    key={c.id}
                    style={{
                      display: "flex", alignItems: "center", gap: 8,
                      padding: "6px 8px", border: "1px solid var(--paper-3)",
                      borderRadius: 4,
                      opacity: hidden ? 0.55 : 1,
                    }}
                  >
                    <label style={{ display: "flex", alignItems: "center", gap: 8, flex: 1, cursor: "pointer" }}>
                      <input
                        type="checkbox"
                        checked={!hidden}
                        onChange={() => toggleCourseHidden(c.id)}
                      />
                      <span className="mono" style={{ fontSize: 12 }}>
                        {flag} {c.name}
                      </span>
                      <span className="mono" style={{ marginLeft: "auto", fontSize: 11, color: "var(--ink-3)" }}>
                        {c.chunks != null ? `${c.chunks} chunks` : ""}
                      </span>
                    </label>
                    <button
                      className="mono course-delete-btn"
                      onClick={() => handleDeleteCourse(c.id)}
                      title={`彻底删除课程 ${c.id}（磁盘 + 索引 + 浏览器缓存）`}
                    >
                      🗑 删除
                    </button>
                  </div>
                );
              })}
            </div>
            <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
              {hiddenCourseIds.length > 0 && (
                <button
                  className="lang-modal-close mono"
                  onClick={unhideAllCourses}
                >全部显示 ({hiddenCourseIds.length})</button>
              )}
              <button
                className="lang-modal-close mono"
                onClick={() => setShowCourseManager(false)}
              >完成</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

/* ── Shared placeholder component ── */
function ActionPlaceholder({ title, desc, btnLabel, onAction, disabled, hint }) {
  return (
    <div style={{ padding: "60px 40px", textAlign: "center" }}>
      <h2 style={{ fontFamily: "var(--serif)", marginBottom: 12, fontSize: 22 }}>{title}</h2>
      <p style={{ color: "var(--ink-3)", marginBottom: 24, maxWidth: 420, margin: "0 auto 24px" }}>{desc}</p>
      <button
        onClick={onAction}
        disabled={disabled}
        style={{
          padding: "12px 28px", background: disabled ? "var(--ink-4)" : "var(--accent)", color: "white",
          border: "none", borderRadius: 6, cursor: disabled ? "default" : "pointer", fontSize: 14, fontWeight: 500,
        }}
      >{btnLabel}</button>
      {hint && <p style={{ marginTop: 16, fontSize: 12, color: "var(--ink-4)", maxWidth: 360, margin: "16px auto 0" }}>{hint}</p>}
    </div>
  );
}

/* ── Markdown helpers ── */
function escapeAttr(s) {
  return String(s || "").replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function markdownToHtml(content) {
  // Pull slug ids from the SAME function the TOC uses, so TOC click → DOM
  // lookup is guaranteed to match (incl. 3+ duplicate heading dedupe).
  const tocList = StudyState.slugifyHeadingsList(content);
  const headingQueue = { 1: [], 2: [], 3: [] };
  tocList.forEach(item => headingQueue[item.level].push(item.id));
  function unescapeForSlug(s) {
    return String(s)
      .replace(/&amp;/g, "&").replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&#39;/g, "'");
  }
  function headingHtml(level, text) {
    // text is HTML-escaped at this point (see escapeHtmlSafe pass below);
    // unescape for slug derivation so the fallback id matches the toc list,
    // which slugs from raw markdown.
    const id = headingQueue[level].shift() || StudyState.slugifyHeading(unescapeForSlug(text));
    const inline = level === 2 ? " style='margin-top:20px'" : "";
    return `<h${level} id="${escapeAttr(id)}" data-toc-id="${escapeAttr(id)}"${inline}>${text}</h${level}>`;
  }
  // review-swarm fix-all v3 #C3+#C4: escape ALL untrusted markdown content
  // before running markdown regexes, so LLM output containing `<script>` or
  // `<img onerror>` lands as text inside dangerouslySetInnerHTML instead of
  // executing. Citations are stashed BEFORE escape so the visible chip text
  // and `data-cite` attribute can carry the raw filename without
  // double-escaping the wrapping `&` (the previous code escaped only the
  // attribute and left the visible inner unescaped — second XSS path).
  const escapeHtmlSafe = (typeof NanoMarkdown !== "undefined" && NanoMarkdown.escapeHtml)
    ? NanoMarkdown.escapeHtml
    : (s) => String(s == null ? "" : s)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;")
        .replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  const _CITE_RE = /\[Source:\s*([^\]]+)\]/g;
  const citationStore = [];
  const withCitePlaceholders = String(content || "").replace(_CITE_RE, (_m, inner) => {
    citationStore.push(inner);
    return `CITE${citationStore.length - 1}`;
  });
  // Stash math BEFORE markdown regexes so $...$ / $$...$$ survive intact for
  // KaTeX. The Notes panel uses a useEffect (RealNotesView) to call
  // NanoMarkdown.renderMath after the html lands in the DOM — same path as
  // the chat bubble. Falls back to in-line math-inline / math-block style if
  // KaTeX failed to load.
  const stash = (typeof NanoMarkdown !== "undefined" && NanoMarkdown.stashMath)
    ? NanoMarkdown.stashMath(withCitePlaceholders)
    : { text: withCitePlaceholders, restore: (h) => h };
  let html = escapeHtmlSafe(stash.text)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/^### (.+)$/gm, (_m, t) => headingHtml(3, t))
    .replace(/^## (.+)$/gm, (_m, t) => headingHtml(2, t))
    .replace(/^# (.+)$/gm, (_m, t) => headingHtml(1, t))
    .replace(/^- (.+)$/gm, "<li>$1</li>")
    .replace(/((?:<li>.*?<\/li>\s*)+)/g, "<ul>$1</ul>")
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\n\n/g, "</p><p>")
    .replace(/\n/g, "<br/>");
  html = stash.restore(html);
  // Restore citation placeholders with safe button HTML — both the visible
  // inner text and the data-cite attribute escape the raw source string.
  html = html.replace(/CITE(\d+)/g, (_m, idx) => {
    const inner = citationStore[Number(idx)] || "";
    const safeInner = escapeHtmlSafe(inner);
    const safeFull = escapeHtmlSafe(`[Source: ${inner}]`);
    return `<button type="button" class="ref-chip mono" data-cite="${safeFull}">${safeInner}</button>`;
  });
  // Drop empty <p></p> introduced when math-display is hoisted out.
  html = html.replace(/<p>\s*<\/p>/g, "");
  return html;
}

/* ── CodeMirror 6 editor wrapper ── */
// Babel-standalone-friendly React wrapper around CodeMirror 6 (loaded as
// ES modules via index.html, exposed on window.__CM6). Falls back to a
// plain <textarea> when CM6 isn't ready (offline / esm.sh failure / loading).
// Polls + listens for `cm6-ready` for up to 5s before settling on fallback.
function CodeMirror6Editor({ value, onChange, language, placeholder }) {
  const hostRef = React.useRef(null);
  const viewRef = React.useRef(null);
  const [ready, setReady] = React.useState(
    typeof window !== "undefined" && window.__CM6 && window.__CM6.ready
  );
  const [fallback, setFallback] = React.useState(false);

  // Wait for cm6-ready event, with a 5s ceiling before fallback to textarea.
  React.useEffect(() => {
    if (ready || fallback) return;
    if (typeof window === "undefined") return;
    let cancelled = false;
    const onReady = () => { if (!cancelled) setReady(true); };
    const onFailed = () => { if (!cancelled) setFallback(true); };
    window.addEventListener("cm6-ready", onReady);
    window.addEventListener("cm6-failed", onFailed);
    const timer = setTimeout(() => {
      if (cancelled) return;
      if (window.__CM6 && window.__CM6.ready) setReady(true);
      else setFallback(true);
    }, 5000);
    return () => {
      cancelled = true;
      clearTimeout(timer);
      window.removeEventListener("cm6-ready", onReady);
      window.removeEventListener("cm6-failed", onFailed);
    };
  }, [ready, fallback]);

  // Mount CM6 once ready. Recreate on language change.
  React.useEffect(() => {
    if (!ready || !hostRef.current) return;
    const CM6 = window.__CM6;
    if (!CM6 || !CM6.ready) { setFallback(true); return; }
    const langExt = (language === "stex" || language === "latex")
      ? CM6.StreamLanguage.define(CM6.stex)
      : [];
    const updateListener = CM6.EditorView.updateListener.of((vu) => {
      if (vu.docChanged) {
        const next = vu.state.doc.toString();
        onChange && onChange(next);
      }
    });
    const state = CM6.EditorState.create({
      doc: String(value || ""),
      extensions: [CM6.basicSetup, langExt, updateListener],
    });
    const view = new CM6.EditorView({ state, parent: hostRef.current });
    viewRef.current = view;
    return () => {
      view.destroy();
      viewRef.current = null;
    };
    // language switch is the only reason to rebuild; `value` is synced via
    // the imperative effect below to avoid recreating the view on every keystroke.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, language]);

  // Sync external value changes (course switch, streaming overwrite) into
  // the editor without losing cursor on no-op updates.
  //
  // review-swarm fix-all v1 #8: a full-doc replace via dispatch({changes:
  // {from:0, to:cur.length, ...}}) collapses the selection to position 0.
  // When the editor has focus (user mid-type), this yanks the cursor on
  // every external update — unusable during a streaming regenerate.
  // Skip the sync entirely when the editor is focused; the user's own
  // edits are the source of truth in that window. When unfocused, do the
  // replace and try to preserve the previous selection anchor.
  React.useEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    const cur = view.state.doc.toString();
    if (cur === (value || "")) return;
    if (view.hasFocus) return; // user is typing — don't yank their cursor
    const next = String(value || "");
    const prevAnchor = view.state.selection.main.anchor;
    view.dispatch({
      changes: { from: 0, to: cur.length, insert: next },
      // Clamp the old anchor into the new doc length so a shorter `next`
      // doesn't blow up the selection model.
      selection: { anchor: Math.min(prevAnchor, next.length) },
    });
  }, [value]);

  if (fallback) {
    return <textarea
      className="notes-editor"
      placeholder={placeholder}
      value={value || ""}
      onChange={e => onChange && onChange(e.target.value)}
    />;
  }
  if (!ready) {
    return <div className="notes-editor cm6-loading mono">
      Loading editor…
    </div>;
  }
  return <div ref={hostRef} className="notes-editor cm6-host" />;
}

/* ── Real Notes View — reading UX (Range API + highlights + TOC + chip routing) ── */

// Shared block-aware DOM text walker. Returns `{ combined, nodes }` where
// `combined` injects "\n\n" at every block-level ancestor boundary (matching
// what `sel.toString()` / `range.toString()` produce in modern browsers).
// Used by `findTextRangeInRoot` (Range resolution), `captureSelection`
// (before/after context capture), and the highlight prune effect — so the
// stored text / before / after fields stay aligned with what we search at
// prune and re-apply time. The LaTeX preview renders via `latexToHtml`, so
// the raw `draft` string no longer matches the visible text (e.g. selecting
// rendered "Theorem 1.2" while the raw source is `\begin{theorem}`); doing
// every step against the same rendered-DOM string keeps the three callers
// in sync.
function getBlockAwareDomText(root) {
  const BLOCK_SEL = "h1,h2,h3,h4,h5,h6,p,li,blockquote,pre,div";
  if (!root) return { combined: "", nodes: [] };
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
  const nodes = [];
  let combined = "";
  let lastBlockEl = null;
  while (walker.nextNode()) {
    const n = walker.currentNode;
    if (n.parentElement && n.parentElement.closest(".sel-menu, .hl-popover")) continue;
    const blockEl = n.parentElement && n.parentElement.closest(BLOCK_SEL);
    if (blockEl && lastBlockEl && blockEl !== lastBlockEl) {
      combined += "\n\n";
    }
    lastBlockEl = blockEl || lastBlockEl;
    nodes.push({ node: n, start: combined.length, end: combined.length + n.nodeValue.length });
    combined += n.nodeValue;
  }
  return { combined, nodes };
}

function findTextRangeInRoot(root, text, before, after) {
  // Walk text nodes via `getBlockAwareDomText`; find `text` preferring
  // positions whose surrounding chars best match before/after. Returns a
  // Range or null.
  if (!root || !text) return null;
  const { combined, nodes } = getBlockAwareDomText(root);
  if (!combined) return null;
  let cursor = 0;
  let bestIdx = -1;
  let bestScore = -1;
  while (cursor <= combined.length - text.length) {
    const idx = combined.indexOf(text, cursor);
    if (idx < 0) break;
    let score = 0;
    if (before) {
      const ctxBefore = combined.slice(Math.max(0, idx - before.length), idx);
      if (ctxBefore.endsWith(before.slice(-Math.min(before.length, 12)))) score += 2;
    }
    if (after) {
      const ctxAfter = combined.slice(idx + text.length, idx + text.length + after.length);
      if (ctxAfter.startsWith(after.slice(0, Math.min(after.length, 12)))) score += 2;
    }
    if (!before && !after) score = 1;
    if (score > bestScore) { bestScore = score; bestIdx = idx; }
    if (bestScore >= 4) break;
    cursor = idx + 1;
  }
  if (bestIdx < 0) return null;
  const startAbs = bestIdx;
  const endAbs = bestIdx + text.length;
  function locate(absOffset) {
    for (const entry of nodes) {
      if (absOffset >= entry.start && absOffset <= entry.end) {
        return { node: entry.node, offset: absOffset - entry.start };
      }
    }
    return null;
  }
  const a = locate(startAbs);
  const b = locate(endAbs);
  if (!a || !b) return null;
  const r = document.createRange();
  try {
    r.setStart(a.node, a.offset);
    r.setEnd(b.node, b.offset);
  } catch { return null; }
  return r;
}

function wrapRangeWithMark(range, hl) {
  // Wraps each text node segment in [range.startContainer .. range.endContainer]
  // with its own <mark>. Cross-element selections get multiple marks but render
  // continuously.
  const root = range.commonAncestorContainer;
  const walker = document.createTreeWalker(
    root.nodeType === Node.TEXT_NODE ? root.parentNode : root,
    NodeFilter.SHOW_TEXT,
    {
      acceptNode(node) {
        const r = document.createRange();
        try {
          r.selectNodeContents(node);
          if (range.compareBoundaryPoints(Range.END_TO_START, r) >= 0) return NodeFilter.FILTER_REJECT;
          if (range.compareBoundaryPoints(Range.START_TO_END, r) <= 0) return NodeFilter.FILTER_REJECT;
        } catch { return NodeFilter.FILTER_REJECT; }
        if (node.parentElement && node.parentElement.closest("mark.hl")) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      },
    },
  );
  const targets = [];
  while (walker.nextNode()) targets.push(walker.currentNode);
  targets.forEach(node => {
    const startOffset = node === range.startContainer ? range.startOffset : 0;
    const endOffset = node === range.endContainer ? range.endOffset : node.nodeValue.length;
    if (endOffset <= startOffset) return;
    const before = node.nodeValue.slice(0, startOffset);
    const middle = node.nodeValue.slice(startOffset, endOffset);
    const after = node.nodeValue.slice(endOffset);
    if (!middle) return;
    const mark = document.createElement("mark");
    mark.className = `hl hl-${hl.color}`;
    mark.dataset.hid = hl.id;
    if (hl.note) mark.dataset.hasNote = "1";
    mark.appendChild(document.createTextNode(middle));
    const parent = node.parentNode;
    if (before) parent.insertBefore(document.createTextNode(before), node);
    parent.insertBefore(mark, node);
    if (after) parent.insertBefore(document.createTextNode(after), node);
    parent.removeChild(node);
  });
}

function unwrapMark(mark) {
  const parent = mark.parentNode;
  if (!parent) return;
  while (mark.firstChild) parent.insertBefore(mark.firstChild, mark);
  parent.removeChild(mark);
  if (parent.normalize) parent.normalize();
}

function applyHighlightsToDom(root, highlights) {
  if (!root) return;
  // Step 1 — unwrap any <mark.hl> whose hid is no longer in the new list.
  // Without this the DOM mark survives `removeHighlight` because
  // `dangerouslySetInnerHTML` only re-paints when `draft` itself changes.
  const wantedIds = new Set((highlights || []).map(h => h.id));
  root.querySelectorAll("mark.hl[data-hid]").forEach(m => {
    if (!wantedIds.has(m.dataset.hid)) unwrapMark(m);
  });
  if (!highlights || !highlights.length) return;
  // Step 2 — apply remaining highlights (longest first so big ones don't get
  // split by short ones). Skip ones whose mark is already in the DOM (idempotent).
  const ordered = highlights.slice().sort((a, b) => (b.text || "").length - (a.text || "").length);
  ordered.forEach(hl => {
    if (root.querySelector(`mark.hl[data-hid="${hl.id}"]`)) return;
    const range = findTextRangeInRoot(root, hl.text, hl.before, hl.after);
    if (!range) return;
    try { wrapRangeWithMark(range, hl); } catch { /* ignore */ }
  });
}

// Multi-level TOC: each node is { level, text, id, children: [...] }.
// L1 is the source-file wrapper, L2 is in-file section, L3 is
// subsubsection. L1 rows show a ▼ / ▶ triangle the user can click to
// collapse the file's children — collapse state is per-course and
// persists across mounts via StudyState.loadTocCollapsed.
function NotesTOC({ items, activeId, onJump, onClose, collapsedIds, onToggleCollapse }) {
  if (!items || !items.length) return null;
  const collapsedSet = new Set(collapsedIds || []);
  function expandTo(id) {
    // When the active section is hidden inside a collapsed L1, force-
    // expand its parent so the user can see what's active. We walk the
    // tree to find the chain of ancestors and uncollapse all of them.
    const trail = [];
    function dfs(nodes, path) {
      for (const n of nodes) {
        if (n.id === id) { trail.push(...path); return true; }
        if (n.children && n.children.length) {
          if (dfs(n.children, path.concat(n.id))) return true;
        }
      }
      return false;
    }
    dfs(items, []);
    trail.forEach(parentId => {
      if (collapsedSet.has(parentId)) onToggleCollapse(parentId, false);
    });
  }
  function handleJump(id) {
    expandTo(id);
    onJump(id);
  }
  function renderNode(node) {
    const hasChildren = !!(node.children && node.children.length);
    const isCollapsed = collapsedSet.has(node.id);
    const isActive = node.id === activeId;
    return (
      <li key={node.id} className={`toc-l${node.level}` + (isActive ? " active" : "")}>
        <div className="toc-row">
          {hasChildren && node.level === 1 ? (
            <button
              type="button"
              className={"toc-toggle" + (isCollapsed ? " collapsed" : "")}
              onClick={() => onToggleCollapse(node.id, !isCollapsed)}
              title={isCollapsed ? "Expand" : "Collapse"}
              aria-expanded={!isCollapsed}
            >{isCollapsed ? "▶" : "▼"}</button>
          ) : (
            <span className="toc-toggle-spacer" />
          )}
          <button className="toc-jump" onClick={() => handleJump(node.id)} title={node.text}>
            {node.text}
          </button>
        </div>
        {hasChildren && !isCollapsed && (
          <ul>{node.children.map(renderNode)}</ul>
        )}
      </li>
    );
  }
  return (
    <nav className="notes-toc" aria-label="Table of contents">
      <div className="toc-head mono">
        <span>Contents</span>
        {onClose && <button className="side-close" onClick={onClose} title="Hide TOC" aria-label="Hide TOC">×</button>}
      </div>
      <ul className="toc-tree">{items.map(renderNode)}</ul>
    </nav>
  );
}

function HighlightDrawer({ highlights, onJump, onRemove, onClose }) {
  return (
    <aside className="notes-hl-drawer">
      <div className="hl-head mono">
        <span>Highlights · {highlights.length}</span>
        {onClose && <button className="side-close" onClick={onClose} title="Hide highlights" aria-label="Hide highlights">×</button>}
      </div>
      {!highlights.length && <p className="empty-state">Select text in the preview to highlight.</p>}
      <ul>
        {highlights.map(h => (
          <li key={h.id} className={`hl-row hl-row-${h.color}`}>
            <button className="hl-jump" data-hid={h.id} onClick={() => onJump(h.id)} title={h.text}>
              <span className={`hl-dot hl-${h.color}`}></span>
              <span className="hl-text">{h.text.length > 60 ? h.text.slice(0, 57) + "…" : h.text}</span>
            </button>
            {h.note && <p className="hl-note">{h.note}</p>}
            <button className="hl-remove" title="Remove" onClick={() => onRemove(h.id)}>×</button>
          </li>
        ))}
      </ul>
    </aside>
  );
}

function RealNotesView({ content, streaming, activeCourse, sources, onContentChange, generationState, onRetry, onCitation }) {
  const [draft, setDraft] = React.useState(content || "");
  const [editing, setEditing] = React.useState(false);
  const [highlights, setHighlights] = React.useState([]);
  const [tocItems, setTocItems] = React.useState([]);
  const [activeTocId, setActiveTocId] = React.useState(null);
  // Persisted per-browser (global key, not per-course) so the user's
  // "I find the TOC too noisy" preference survives a reload. Default is
  // visible — the TOC is the killer feature, hide only on demand.
  const [showToc, setShowTocRaw] = React.useState(
    () => !StudyState.loadNotesTocHidden(localStorage),
  );
  const setShowToc = React.useCallback(updater => {
    setShowTocRaw(prev => {
      const next = typeof updater === "function" ? updater(prev) : updater;
      StudyState.saveNotesTocHidden(localStorage, !next);
      return next;
    });
  }, []);
  // Per-course collapsed-section ids. Loaded from localStorage on
  // course switch; toggle handler writes through immediately.
  const [tocCollapsedIds, setTocCollapsedIds] = React.useState([]);
  const [showDrawer, setShowDrawer] = React.useState(true);
  // Notes toolbar is collapsible — user complained the 6-button row eats
  // vertical real estate above the LaTeX preview. Persisted globally
  // (same convention as kg-legend-hidden / notes-toc-hidden); default
  // expanded so first-time users still discover the actions.
  // Optional-chain the StudyState helpers so a stale browser cache
  // (old study-state.js + new app.jsx) degrades to default-expanded
  // instead of crashing RealNotesView with TypeError.
  const [toolbarCollapsed, setToolbarCollapsedRaw] = React.useState(
    () => StudyState.loadNotesToolbarCollapsed?.(localStorage) ?? false,
  );
  const setToolbarCollapsed = React.useCallback(updater => {
    setToolbarCollapsedRaw(prev => {
      const next = typeof updater === "function" ? updater(prev) : updater;
      StudyState.saveNotesToolbarCollapsed?.(localStorage, next);
      return next;
    });
  }, []);
  const [selMenu, setSelMenu] = React.useState(null); // {x, y, text, before, after}
  const [popover, setPopover] = React.useState(null); // {x, y, hl}
  const previewRef = React.useRef(null);
  // Outer scrolling container ref. Stays mounted across Edit↔Preview
  // toggles (the conditional swap happens inside this div), so attaching
  // the scroll listener here lets us drop `editing` from the listener
  // effect's dep array and avoid spurious save-on-toggle writes.
  const rootRef = React.useRef(null);
  // Mirror `editing` into a ref so the long-lived scroll-save listener
  // can skip writes while the user is in Edit mode WITHOUT re-attaching
  // on every editing flip.
  const editingRef = React.useRef(false);
  React.useEffect(() => { editingRef.current = editing; }, [editing]);

  // Course switch — full reset (clears edit-mode + popovers + restores cached draft).
  React.useEffect(() => {
    setSelMenu(null);
    setPopover(null);
    setEditing(false);
    const cached = activeCourse ? StudyState.loadNoteDraft(localStorage, activeCourse) : "";
    setDraft(cached || content || "");
    setTocCollapsedIds(activeCourse ? StudyState.loadTocCollapsed(localStorage, activeCourse) : []);
  }, [activeCourse]);

  // Streaming chunks — overwrite draft only while streaming, so a regenerate
  // pass updates the preview without clobbering edits the user typed in
  // Edit mode after the previous generation finished.
  React.useEffect(() => {
    if (editing) return;
    if (!streaming) return;
    if (typeof content === "string") setDraft(content);
  }, [content, streaming, editing]);

  // Stable cache-key for the file-name whitelist passed to extractTOC.
  // We hash to a string so the TOC effect's identity comparison ignores
  // unrelated `setSources` updates (checkbox toggle, bulk select, etc.)
  // that don't change the file names themselves.
  const fileNamesKey = React.useMemo(() => {
    if (!Array.isArray(sources)) return "";
    return sources
      .map(s => (s && (s.sourceFile || s.title)) || "")
      .filter(Boolean)
      .join("\n");
  }, [sources]);

  // Highlights / TOC. During streaming we extract the TOC from the partial
  // LaTeX but DO NOT prune highlights — the partial doesn't contain
  // sections that haven't streamed yet, and pruning would silently delete
  // their anchors from localStorage.
  // LaTeX-refactor: TOC is extracted from `\section{...}` macros via the
  // latex-to-html shim's extractor; falls back to the markdown helper for
  // older content (legacy partial drafts).
  React.useEffect(() => {
    if (!activeCourse) { setHighlights([]); setTocItems([]); return; }
    let toc;
    if (typeof NanoLatex !== "undefined" && NanoLatex.extractTOC) {
      const fileNames = fileNamesKey ? fileNamesKey.split("\n") : [];
      toc = NanoLatex.extractTOC(draft, { fileNames });
    } else {
      // Markdown legacy path returns a flat list — wrap into the
      // tree-shape NotesTOC consumes so both paths use the same renderer.
      toc = StudyState.adaptFlatTocToTree(StudyState.extractHeadingTOC(draft));
    }
    setTocItems(toc);
    if (streaming) return;
    // Prune against the rendered preview text (block-aware), not the raw
    // LaTeX draft. Highlight text/before/after are captured from the
    // rendered DOM, so the same view must be used when checking whether
    // they still exist — otherwise valid highlights get dropped on every
    // draft change and the in-DOM <mark> disappears, breaking both
    // "click the highlight" and the drawer's jump-to-highlight button.
    const root = previewRef.current;
    if (root) {
      const { combined } = getBlockAwareDomText(root);
      if (combined) {
        const result = StudyState.pruneStaleHighlights(localStorage, activeCourse, combined);
        setHighlights(result.kept);
      } else {
        setHighlights(StudyState.loadHighlights(localStorage, activeCourse));
      }
    } else {
      // Preview hasn't mounted yet (initial render); keep stored
      // highlights as-is — the next effect tick will prune against DOM.
      setHighlights(StudyState.loadHighlights(localStorage, activeCourse));
    }
    // review-swarm v2 fix-soon #4: depend on `fileNamesKey` (a stable
    // string derived via useMemo, below), NOT `sources` (which gets a
    // new array reference on every setSources — including each Library
    // checkbox toggle). Toggling 50 sources used to re-walk the LaTeX
    // 50× even though TOC only cares about filenames, not check state.
  }, [activeCourse, draft, streaming, fileNamesKey]);

  // Re-apply highlights to DOM whenever preview html or highlights change.
  // Skip during streaming — DOM is being rewritten per-token so any wrap is
  // immediately discarded. Marks reappear once streaming completes.
  React.useEffect(() => {
    if (editing) return;
    if (streaming) return;
    const root = previewRef.current;
    if (!root) return;
    applyHighlightsToDom(root, highlights);
  }, [draft, highlights, editing, streaming]);

  // Run KaTeX after the preview HTML lands so $...$ / $$...$$ become real
  // math. Skipped while editing (the textarea path doesn't render math) and
  // throttled during streaming so partial chunks don't flicker — final state
  // is rendered by the trailing-edge call after the stream settles.
  const renderMathThrottled = React.useMemo(() => {
    const fn = (typeof NanoMarkdown !== "undefined" && NanoMarkdown.renderMath)
      ? NanoMarkdown.renderMath : null;
    if (!fn) return () => {};
    if (typeof NanoMarkdown.throttle === "function") {
      return NanoMarkdown.throttle(fn, 200);
    }
    return fn;
  }, []);
  React.useEffect(() => {
    if (editing) return;
    const root = previewRef.current;
    if (!root) return;
    renderMathThrottled(root);
  }, [draft, editing, streaming, renderMathThrottled]);

  // Track which TOC section is currently in view (rAF-throttled).
  React.useEffect(() => {
    if (editing) return;
    const root = previewRef.current;
    if (!root) return;
    // The scrolling ancestor is .notes-reader-body (.workspace / .main both
    // overflow:hidden). Anchor the active-section heuristic to its viewport.
    const scroller = root.closest(".notes-reader-body");
    if (!scroller) return;
    let ticking = false;
    function compute() {
      ticking = false;
      const headings = root.querySelectorAll("h1[id], h2[id], h3[id]");
      if (!headings.length) return;
      const scrollerTop = scroller.getBoundingClientRect().top;
      let current = headings[0].id;
      for (const h of headings) {
        const r = h.getBoundingClientRect();
        if (r.top - scrollerTop < 100) current = h.id;
        else break;
      }
      setActiveTocId(current);
    }
    function onScroll() {
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(compute);
    }
    compute();
    scroller.addEventListener("scroll", onScroll, { passive: true });
    return () => scroller.removeEventListener("scroll", onScroll);
  }, [draft, editing, tocItems]);

  // Notes scroll cache: when the user navigates Notes → Reader → back to
  // Notes (via citation chip click + tab switch), RealNotesView remounts
  // and the browser resets `notes-reader-body` scrollTop to 0 — losing
  // the user's place in a 30-section study note. Two effects:
  //   1. Persist scrollTop on scroll (throttled via rAF). Keyed per
  //      activeCourse so a course switch doesn't restore a stranger's
  //      offset.
  //   2. Restore scrollTop on mount, after the layout + KaTeX render
  //      settle. rAF×2 gives one full paint cycle; math-heavy notes may
  //      still land slightly off because KaTeX auto-render is async
  //      throttled at 200ms, but that's a survivable jitter.
  // Cleared when streaming flips back on (regeneration about to replace
  // content), so the next mount restores 0 instead of an offset into the
  // old document.
  React.useEffect(() => {
    if (!activeCourse) return;
    if (editing) return;
    if (streaming) return;
    // review-swarm v2 fix-soon #5: scroll listener now anchors on
    // rootRef (the persistent .notes-reader-body root) instead of
    // previewRef.current.closest(...). The preview unmounts on Edit
    // toggle but the root stays — avoids the tear-down/re-attach +
    // synchronous cleanup-flush each toggle that could write a 0.
    const scroller = rootRef.current;
    if (!scroller) return;
    const saved = StudyState.loadNotesScroll(localStorage, activeCourse);
    if (saved == null || saved <= 0) return;

    // Suppress the `.notes-reader-body { scroll-behavior: smooth }` CSS
    // so the user doesn't see a 1-2 second animated scroll from top to
    // their saved position — looks like the page is dragging itself.
    function applyScroll(targetY) {
      const prev = scroller.style.scrollBehavior;
      scroller.style.scrollBehavior = "auto";
      const maxY = Math.max(0, scroller.scrollHeight - scroller.clientHeight);
      scroller.scrollTop = Math.min(targetY, maxY);
      // Force reflow so the browser commits the scrollTop before we
      // hand the smooth-scroll behavior back; otherwise the next user
      // interaction can animate from a partial state.
      // eslint-disable-next-line no-unused-expressions
      scroller.offsetHeight;
      scroller.style.scrollBehavior = prev;
    }

    // Retry across animation frames until the document is tall enough
    // to actually hold our saved offset — KaTeX auto-render is async
    // and grows content height after the initial paint, so a single
    // rAF×2 restore would silently land short and the user would still
    // see the page near the top. Cap attempts to ~20 frames (~330ms).
    let stop = false;
    let attempts = 0;
    let tailTimer = 0;
    function tryRestore() {
      if (stop) return;
      attempts += 1;
      const maxY = Math.max(0, scroller.scrollHeight - scroller.clientHeight);
      if (maxY >= saved) {
        applyScroll(saved);
        // review-swarm v2 fix-soon #6: cancel the tail timer so it
        // doesn't fire applyScroll a second time at t=600ms, which
        // would otherwise yank the user back to `saved` if they had
        // already scrolled somewhere else in those 600ms.
        stop = true;
        if (tailTimer) clearTimeout(tailTimer);
        return;
      }
      if (attempts < 20) {
        requestAnimationFrame(tryRestore);
      }
    }
    const raf = requestAnimationFrame(tryRestore);

    // Tail-time fallback: after 600ms, force a best-effort restore
    // regardless. By then KaTeX has run (its throttle is 200ms) and
    // any large \begin{align} blocks have laid out. If the document
    // still isn't tall enough we clamp via the maxY in applyScroll —
    // the user lands as close as the doc permits, not at the top.
    tailTimer = setTimeout(() => {
      if (stop) return;
      applyScroll(saved);
      stop = true;
    }, 600);

    return () => {
      stop = true;
      cancelAnimationFrame(raf);
      if (tailTimer) clearTimeout(tailTimer);
    };
  }, [activeCourse, streaming]);

  // Round 3 of scroll cache: useEffect cleanup is *passive* and runs AFTER
  // React removes the DOM node, so `scroller.isConnected` is false in the
  // unmount path and the previous `isConnected`-gated final flush never
  // executed. Switch to useLayoutEffect: its cleanup fires synchronously
  // during the mutation phase BEFORE the DOM mutation, so scroller is
  // still in the tree and scrollTop reads the user's real position. This
  // guarantees the final scrollTop is captured even when the user scrolls
  // and clicks a citation chip within the same 16ms tick (rAF tick may
  // not have fired yet → without this, no save → restore bails to 0).
  React.useLayoutEffect(() => {
    if (!activeCourse) return;
    const scroller = rootRef.current;
    if (!scroller) return;
    let detached = false;
    let ticking = false;
    function onScroll() {
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(() => {
        ticking = false;
        if (detached) return;
        // Skip saves while the user is in Edit mode — the inner DOM
        // (CodeMirror) has independent scroll semantics and we don't
        // want its scrollTop to clobber the preview's saved position
        // that we want to restore when the user toggles back.
        if (editingRef.current) return;
        StudyState.saveNotesScroll(localStorage, activeCourse, scroller.scrollTop);
      });
    }
    scroller.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      detached = true;
      scroller.removeEventListener("scroll", onScroll);
      // Final flush runs in layout-effect cleanup, i.e. BEFORE DOM removal,
      // so scroller is still connected and scrollTop is the live value.
      if (!editingRef.current) {
        StudyState.saveNotesScroll(localStorage, activeCourse, scroller.scrollTop);
      }
    };
  }, [activeCourse]);

  // review-swarm v2 fix-now #1: the previous implementation cleared the
  // notes-scroll cache whenever `streaming` flipped true — but `streaming`
  // is a shared global (also flipped by quiz / mindmap / report / mastery
  // generations). The bug: read notes → "Generate Quiz" → return to Notes
  // → notes scroll cache was wiped, user lands at top. Now the clear
  // happens INSIDE handleGenerateNotes in App, after setStreaming(true)
  // — guaranteed Notes-only.

  function updateDraft(value) {
    setDraft(value);
    if (activeCourse) StudyState.saveNoteDraft(localStorage, activeCourse, value);
    onContentChange && onContentChange(value);
  }

  // LaTeX-refactor: 3-way export. Source-of-truth is always the LaTeX body
  // in `draft`. Browser-print uses the same HTML we render here (so math +
  // theorem boxes look right), tectonic compile sends the source up.
  const [tectonicAvailable, setTectonicAvailable] = React.useState(null);
  const [compileError, setCompileError] = React.useState(null);
  React.useEffect(() => {
    let cancelled = false;
    fetch("/api/status").then(r => r.ok ? r.json() : null).then(s => {
      if (!cancelled && s) setTectonicAvailable(Boolean(s.tectonic_available));
    }).catch(() => { /* status probe is best-effort */ });
    return () => { cancelled = true; };
  }, []);

  function downloadLatex() {
    const exp = StudyState.buildLatexExport(activeCourse, draft);
    const blob = new Blob([exp.content], { type: exp.mime });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = exp.filename;
    a.click();
    URL.revokeObjectURL(url);
  }

  function printPdfFromBrowser() {
    // Re-render the LaTeX through the same shim used in the preview, so
    // the print output carries theorem boxes + math (via KaTeX inline-rendered
    // HTML) rather than raw source.
    const rendered = (typeof NanoLatex !== "undefined" && NanoLatex.latexToHtml)
      ? NanoLatex.latexToHtml(draft)
      : "<pre>" + (draft || "").replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c])) + "</pre>";
    const html = StudyState.buildPrintHtml(activeCourse, rendered);
    const win = window.open("", "_blank");
    if (!win) return;
    win.document.write(html);
    win.document.close();
    // Defer print to give KaTeX a tick to render in the new window.
    setTimeout(() => { try { win.print(); } catch (e) {} }, 350);
  }

  async function compilePdfWithTectonic() {
    if (!activeCourse) return;
    setCompileError(null);
    try {
      const resp = await fetch("/api/notes/export/pdf", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ course_id: activeCourse, latex_source: draft }),
      });
      if (!resp.ok) {
        let body = null;
        try { body = await resp.json(); } catch (e) {}
        if (body && body.error === "tectonic_unavailable") {
          setTectonicAvailable(false);
          setCompileError("Tectonic 不可用：服务器未安装 LaTeX 编译器。");
          return;
        }
        if (body && body.error === "latex_unsafe") {
          setCompileError(`安全检查拦截：${body.reason || "包含禁止的 LaTeX 命令"}`);
          return;
        }
        if (body && body.error === "latex_compile_failed") {
          const tail = (body.log || "").slice(-800);
          setCompileError(`LaTeX 编译失败 (exit ${body.exit_code || "?"})：\n${tail}`);
          return;
        }
        if (body && body.error === "latex_compile_timeout") {
          setCompileError("编译超时（>60s）。文档可能含死循环或复杂的图。");
          return;
        }
        setCompileError(`Compile failed: HTTP ${resp.status}`);
        return;
      }
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const safeCourse = String(activeCourse || "course").replace(/[^\w.-]+/g, "-");
      const a = document.createElement("a");
      a.href = url;
      a.download = `${safeCourse}-notes.pdf`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setCompileError(`网络错误：${e.message || e}`);
    }
  }

  function captureSelection() {
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0 || sel.isCollapsed) { setSelMenu(null); return; }
    const range = sel.getRangeAt(0);
    const root = previewRef.current;
    if (!root || !root.contains(range.commonAncestorContainer)) { setSelMenu(null); return; }
    const text = sel.toString().trim();
    if (!text || text.length < 2) { setSelMenu(null); return; }
    // A new selection always wins over any open popover — without this the
    // popover from a previously clicked highlight blocks fresh highlighting.
    setPopover(null);
    // Capture before/after context from the rendered DOM (block-aware).
    // CRITICAL: we use the Range's own startContainer/startOffset to compute
    // the *exact* absolute position in `combined`, NOT a top-to-bottom
    // `indexOf(text)` — if the same text appears multiple times in the
    // notes ("gradient" × 12), `indexOf` would return the first match and
    // we'd silently capture the wrong occurrence's context, then re-apply
    // the highlight onto the first hit instead of the one the user picked.
    const probe = { text, before: "", after: "" };
    const { combined, nodes } = getBlockAwareDomText(root);
    if (combined && nodes.length) {
      // Resolve range.startContainer → absolute offset in `combined`. The
      // walker may split selection into adjacent text nodes (e.g.
      // start-of-mark / end-of-mark); we accept the lowest-offset node
      // ancestor for the start and the highest for the end so multi-node
      // ranges still map correctly.
      function absOffset(container, off, mode) {
        for (const entry of nodes) {
          if (entry.node === container) return entry.start + off;
        }
        // Selection started in an element (e.g. <p>) rather than a text
        // node — find the first/last text-node descendant.
        if (container && container.nodeType === Node.ELEMENT_NODE) {
          const descendants = nodes.filter(n => container.contains(n.node));
          if (descendants.length) {
            return mode === "end"
              ? descendants[descendants.length - 1].end
              : descendants[0].start;
          }
        }
        return -1;
      }
      const startAbs = absOffset(range.startContainer, range.startOffset, "start");
      const endAbs = absOffset(range.endContainer, range.endOffset, "end");
      if (startAbs >= 0 && endAbs > startAbs) {
        probe.before = combined.slice(Math.max(0, startAbs - 30), startAbs);
        probe.after = combined.slice(endAbs, Math.min(combined.length, endAbs + 30));
        // Re-derive `text` from combined so the saved string includes the
        // exact block-boundary "\n\n" that `findTextRangeInRoot` expects;
        // `sel.toString()` uses single "\n" between blocks, which can drift
        // from the walker's separator and break re-apply on cross-block
        // selections.
        probe.text = combined.slice(startAbs, endAbs);
      }
    }
    const rect = range.getBoundingClientRect();
    const stageRect = (root.closest(".notes-stage") || root).getBoundingClientRect();
    setSelMenu({
      x: rect.left + rect.width / 2 - stageRect.left,
      y: rect.bottom - stageRect.top + 8,
      text: probe.text,
      before: probe.before,
      after: probe.after,
    });
  }

  function applyHighlightColor(color) {
    if (!selMenu || !activeCourse) return;
    const list = StudyState.addHighlight(localStorage, activeCourse, {
      text: selMenu.text, before: selMenu.before, after: selMenu.after, color,
    });
    setHighlights(list);
    setSelMenu(null);
    window.getSelection() && window.getSelection().removeAllRanges();
  }

  function handlePreviewClick(e) {
    // Source chip → Reader
    const chip = e.target.closest && e.target.closest(".ref-chip[data-cite]");
    if (chip) {
      e.preventDefault();
      const cite = chip.dataset.cite;
      onCitation && onCitation(cite);
      return;
    }
    // Existing highlight → popover (unless the click was the start of a new selection)
    const mark = e.target.closest && e.target.closest("mark.hl[data-hid]");
    if (mark) {
      const hid = mark.dataset.hid;
      const hl = highlights.find(h => h.id === hid);
      if (!hl) return;
      const stage = previewRef.current && (previewRef.current.closest(".notes-stage") || previewRef.current);
      const stageRect = stage ? stage.getBoundingClientRect() : { left: 0, top: 0 };
      const rect = mark.getBoundingClientRect();
      setPopover({
        x: rect.left + rect.width / 2 - stageRect.left,
        y: rect.bottom - stageRect.top + 6,
        hl,
      });
      // Reveal the corresponding drawer entry — click-in-notes → see-in-index
      // round-trip. Scrolls the side drawer to the entry and flashes it so
      // the user can tell which highlight just got tapped, even with 30+
      // entries in the list. Best-effort: drawer may be hidden, in which
      // case there's no DOM to scroll and we silently skip.
      const drawerEntry = document.querySelector(`.notes-hl-drawer .hl-jump[data-hid="${hid}"]`);
      if (drawerEntry) {
        drawerEntry.scrollIntoView({ behavior: "smooth", block: "center" });
        drawerEntry.classList.add("hl-flash");
        setTimeout(() => drawerEntry.classList.remove("hl-flash"), 900);
      }
      return;
    }
    setPopover(null);
  }

  function updatePopoverHighlight(patch) {
    if (!popover || !activeCourse) return;
    const list = StudyState.updateHighlight(localStorage, activeCourse, popover.hl.id, patch);
    setHighlights(list);
    const next = list.find(h => h.id === popover.hl.id);
    if (next) setPopover({ ...popover, hl: next });
  }

  function removePopoverHighlight() {
    if (!popover || !activeCourse) return;
    const list = StudyState.removeHighlight(localStorage, activeCourse, popover.hl.id);
    setHighlights(list);
    setPopover(null);
  }

  function jumpToHeading(id) {
    const root = previewRef.current;
    if (!root) return;
    const target = root.querySelector(`#${CSS.escape(id)}`);
    if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function jumpToHighlight(hid) {
    const root = previewRef.current;
    if (!root) return;
    const target = root.querySelector(`mark.hl[data-hid="${hid}"]`);
    if (target) {
      target.scrollIntoView({ behavior: "smooth", block: "center" });
      target.classList.add("hl-flash");
      setTimeout(() => target.classList.remove("hl-flash"), 900);
    }
  }

  function removeHighlightFromDrawer(hid) {
    if (!activeCourse) return;
    const list = StudyState.removeHighlight(localStorage, activeCourse, hid);
    setHighlights(list);
  }

  // LaTeX-refactor: render LaTeX → HTML via the latex-to-html shim. Math
  // placeholders are restored to $...$ / $$...$$ inside the HTML; the
  // existing KaTeX renderMath effect (above) sweeps them after mount.
  //
  // review-swarm fix-all v1 #9: memoise on `draft` so a streaming regenerate
  // (~10 chunks/s, full-text accumulating in `draft`) doesn't re-run the
  // 6-pass env-stash regex pipeline + HTML escape every keystroke and
  // every chunk. KaTeX render is already throttled (`renderMathThrottled`);
  // pairing useMemo here keeps the React render itself cheap.
  const html = React.useMemo(
    () => (typeof NanoLatex !== "undefined" && NanoLatex.latexToHtml)
      ? NanoLatex.latexToHtml(draft)
      : "", // shim missing → empty preview rather than mangled raw source
    [draft]
  );

  return (
    <div ref={rootRef} className="reader-body notes-reader-body">
      {/* Edit mode forces the toolbar expanded — the Edit/Preview button is
          the only escape hatch out of Edit mode, so hiding it behind the
          collapse would trap the user. In Preview mode the user's
          persisted preference (toolbarCollapsed) is honored. */}
      {(() => { const effectiveCollapsed = toolbarCollapsed && !editing; return (
      <div className={`notes-toolbar${effectiveCollapsed ? " notes-toolbar-collapsed" : ""}`}>
        <button
          type="button"
          className="btn ghost notes-toolbar-toggle"
          onClick={() => setToolbarCollapsed(v => !v)}
          disabled={editing}
          title={editing
            ? "Edit 模式下工具栏始终展开"
            : (effectiveCollapsed ? "展开工具栏" : "隐藏工具栏")}
          aria-expanded={!effectiveCollapsed}
        >{effectiveCollapsed ? "▸ Tools" : "▾ Tools"}</button>
        {!effectiveCollapsed && (
          <>
            {/* 2026-05-13: Edit button hidden by user request. The
                CodeMirror editor + setEditing state machinery stays in
                place — if you want it back, restore this button. The
                Preview rendering, highlights, TOC, and export paths
                are all independent of editing mode. */}
            <button className="btn ghost" onClick={downloadLatex} title="下载 .tex 源文件">.tex</button>
            <button className="btn ghost" onClick={printPdfFromBrowser} title="浏览器打印（快速预览）">PDF (print)</button>
            {tectonicAvailable !== false && (
              <button
                className="btn ghost"
                onClick={compilePdfWithTectonic}
                disabled={tectonicAvailable === null}
                title={tectonicAvailable === null
                  ? "检查 tectonic 状态中…"
                  : "服务端 LaTeX 编译（学术排版）"}
              >PDF (compile)</button>
            )}
            <button className="btn ghost" onClick={() => setShowToc(v => !v)} disabled={editing}>{showToc ? "Hide TOC" : "Show TOC"}</button>
            <button className="btn ghost" onClick={() => setShowDrawer(v => !v)} disabled={editing}>{showDrawer ? "Hide Highlights" : `Highlights · ${highlights.length}`}</button>
          </>
        )}
        {generationState?.retryable && <button className="btn primary" onClick={onRetry}>Retry</button>}
      </div>
      ); })()}
      {streaming && <div style={{ color: "var(--accent)", marginBottom: 16, fontFamily: "var(--mono)", fontSize: 12 }}>Generating notes<span className="stream-cursor"></span></div>}
      {generationState?.status === "failed" && <div className="error-banner">{generationState.errorDetail}</div>}
      {compileError && (
        <div className="error-banner" style={{ whiteSpace: "pre-wrap", fontFamily: "var(--mono)", fontSize: 12 }}>
          {compileError}
          <button className="btn ghost" style={{ marginLeft: 8 }} onClick={() => setCompileError(null)}>×</button>
        </div>
      )}
      {editing ? (
        <div>
          <p className="notes-edit-hint mono">Editing raw LaTeX — highlights stay saved and reappear in Preview.</p>
          <CodeMirror6Editor
            value={draft}
            onChange={updateDraft}
            language="stex"
            placeholder="\\section{Introduction} ..."
          />
        </div>
      ) : (
        <div className="notes-stage">
          {showToc && (
            <NotesTOC
              items={tocItems}
              activeId={activeTocId}
              onJump={jumpToHeading}
              onClose={() => setShowToc(false)}
              collapsedIds={tocCollapsedIds}
              onToggleCollapse={(id, nextCollapsed) => {
                if (!activeCourse) return;
                const next = StudyState.setTocCollapsed(
                  localStorage, activeCourse, id, nextCollapsed
                );
                setTocCollapsedIds(next);
              }}
            />
          )}
          <div
            ref={previewRef}
            className="notes-preview"
            onMouseUp={captureSelection}
            onKeyUp={captureSelection}
            onClick={handlePreviewClick}
            dangerouslySetInnerHTML={{ __html: "<p>" + html + "</p>" }}
          />
          {showDrawer && <HighlightDrawer highlights={highlights} onJump={jumpToHighlight} onRemove={removeHighlightFromDrawer} onClose={() => setShowDrawer(false)} />}
          {selMenu && (
            <div className="sel-menu" style={{ left: selMenu.x, top: selMenu.y }} onMouseDown={e => e.preventDefault()}>
              {StudyState.HIGHLIGHT_COLORS.map(c => (
                <button key={c} className={`sel-color hl-${c}`} onClick={() => applyHighlightColor(c)} title={`Highlight (${c})`} />
              ))}
              <button className="sel-cancel" onClick={() => setSelMenu(null)}>×</button>
            </div>
          )}
          {popover && (
            <div className="hl-popover" style={{ left: popover.x, top: popover.y }} onMouseDown={e => e.stopPropagation()}>
              <div className="hl-popover-row">
                {StudyState.HIGHLIGHT_COLORS.map(c => (
                  <button key={c} className={`sel-color hl-${c}` + (popover.hl.color === c ? " active" : "")}
                    onClick={() => updatePopoverHighlight({ color: c })} title={c} />
                ))}
                <button className="hl-popover-del" onClick={removePopoverHighlight} title="Delete">Delete</button>
                <button className="sel-cancel" onClick={() => setPopover(null)}>×</button>
              </div>
              <textarea
                className="hl-note-input"
                placeholder="Add note…"
                value={popover.hl.note || ""}
                onChange={e => updatePopoverHighlight({ note: e.target.value })}
              />
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* ── Real Quiz View ── */
// fix-all v1 H5 + M4: `correctLetter` now lives in study-state.js so the
// Wrong-Only review filter and this view share the same helper. The local
// copy's regex `/^([A-Za-z])[.\s)]/` also missed bare-letter answers (the
// EXAM_PREP_QUESTIONS_PROMPT explicitly mandates `"B"` for multi-choice);
// the shared version uses `/^([A-Za-z])(?:$|[.\s)])/` to accept both.
const correctLetter = StudyState.correctLetter;

function RealQuizView({ questions, activeCourse, onRegenerate, regenerating }) {
  const loaded = StudyState.loadQuizAnswers(localStorage, activeCourse, questions);
  const [answers, setAnswers] = React.useState(loaded.answers || {});
  const [submitted, setSubmitted] = React.useState(false);
  const [reviewWrong, setReviewWrong] = React.useState(false);

  React.useEffect(() => {
    const restored = StudyState.loadQuizAnswers(localStorage, activeCourse, questions);
    setAnswers(restored.answers || {});
    setSubmitted(false);
    setReviewWrong(false);
  }, [activeCourse, questions]);

  function updateAnswer(index, value) {
    const next = { ...answers, [index]: value };
    setAnswers(next);
    StudyState.saveQuizAnswers(localStorage, activeCourse, questions, next);
  }

  const visibleQuestions = reviewWrong ? StudyState.filterWrongQuestions(questions, answers) : questions;
  const stale = StudyState.loadQuizAnswers(localStorage, activeCourse, questions).stale;

  return (
    <div className="reader-body" style={{ padding: "28px 40px" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 20 }}>
        <h2 style={{ fontFamily: "var(--serif)", margin: 0 }}>Practice Quiz — {questions.length} Questions</h2>
        <div style={{ display: "flex", gap: 8 }}>
          {onRegenerate && (
            <button
              className="btn ghost"
              onClick={() => {
                if (!window.confirm("Generate a fresh quiz? Current answers will be discarded.")) return;
                onRegenerate();
              }}
              disabled={regenerating}
              title="Discard this quiz and generate a brand-new question set"
            >
              {regenerating ? "Generating…" : "↻ Regenerate"}
            </button>
          )}
          <button className="btn ghost" onClick={() => setReviewWrong(!reviewWrong)} disabled={!submitted}>
            {reviewWrong ? "All Questions" : "Wrong Only"}
          </button>
          <button className="btn primary" onClick={() => setSubmitted(!submitted)}>{submitted ? "Hide Answers" : "Grade"}</button>
        </div>
      </div>
      {stale && <div className="error-banner">Saved answers are stale because the question set changed.</div>}

      {visibleQuestions.map((q) => {
        const i = questions.indexOf(q);
        // Whole-question correctness — only meaningful for multi-choice
        // (the answer letter comes from `q.correct` or, for LLM-generated
        // payloads, the leading "X." of `q.answer`). Essay questions can't
        // be auto-graded so they show no red/green frame on submit.
        const userAnswered = answers[i] != null && answers[i] !== "";
        const correct = correctLetter(q);
        const isGraded = submitted && q.options && correct;
        const gotItRight = isGraded && userAnswered && answers[i] === correct;
        const gotItWrong = isGraded && userAnswered && answers[i] !== correct;
        return (
        <div key={i} style={{
          marginBottom: 20, paddingBottom: 16, borderBottom: "1px dashed var(--rule)",
          // Tag the whole question card with a coloured left rail so the
          // user can scan a long quiz and spot wrong / right answers
          // without parsing each option's border.
          borderLeft: gotItRight ? "4px solid oklch(0.65 0.18 145)"
                    : gotItWrong ? "4px solid oklch(0.62 0.22 25)"
                    : "4px solid transparent",
          paddingLeft: 10,
        }}>
          <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 6, alignItems: "center" }}>
            <strong style={{ color: "var(--accent)" }}>
              Q{i + 1}.
              {gotItRight && <span style={{ marginLeft: 8, fontSize: 11, color: "oklch(0.55 0.18 145)", fontWeight: 600 }}>✓ Correct</span>}
              {gotItWrong && <span style={{ marginLeft: 8, fontSize: 11, color: "oklch(0.55 0.22 25)", fontWeight: 600 }}>✗ Wrong</span>}
            </strong>
            <span className="mono" style={{ fontSize: 10, color: "var(--ink-4)" }}>{q.type || "question"} · {q.difficulty || ""}</span>
          </div>
          <p style={{ marginBottom: 10 }}>{q.question}</p>

          {q.options && q.options.map((opt, j) => {
            const optText = typeof opt === "string" ? opt : `${opt.l}. ${opt.t}`;
            const isCorrect = correct && (typeof opt === "string" ? opt.charAt(0).toUpperCase() === correct : opt.l === correct);
            const optValue = typeof opt === "string" ? opt.charAt(0) : opt.l;
            return (
              <label key={j} style={{
                display: "block", padding: "5px 8px", cursor: "pointer", borderRadius: 4, marginBottom: 2,
                border: "2px solid " + (submitted && isCorrect ? "oklch(0.65 0.18 145)" : submitted && answers[i] === optValue && !isCorrect ? "oklch(0.62 0.22 25)" : "transparent"),
                background: submitted && isCorrect ? "oklch(0.96 0.03 145)" : submitted && answers[i] === optValue && !isCorrect ? "oklch(0.96 0.04 25)" : "transparent",
              }}>
                <input type="radio" name={`q${i}`} checked={answers[i] === optValue}
                  onChange={() => !submitted && updateAnswer(i, optValue)}
                  style={{ marginRight: 8 }} />
                {optText}
                {submitted && isCorrect && " ✓"}
              </label>
            );
          })}

          {!q.options && (
            <textarea placeholder="Your answer..." rows={3}
              style={{ width: "100%", padding: 8, border: "1px solid var(--rule)", borderRadius: 4, fontFamily: "var(--serif)", fontSize: 14 }}
              value={answers[i] || ""}
              onChange={e => updateAnswer(i, e.target.value)} />
          )}

          {submitted && (q.answer || q.explanation) && (
            <div style={{
              marginTop: 8, padding: "10px 14px",
              background: gotItRight ? "oklch(0.96 0.03 145)"
                        : gotItWrong ? "oklch(0.96 0.04 25)"
                        : "var(--paper-2)",
              borderLeft: "3px solid " + (gotItRight ? "oklch(0.65 0.18 145)"
                                        : gotItWrong ? "oklch(0.62 0.22 25)"
                                        : "var(--accent)"),
              borderRadius: "0 4px 4px 0", fontSize: 13,
            }}>
              {q.answer && (
                <div>
                  <strong>Answer:</strong> {q.answer}
                  {gotItWrong && userAnswered && (
                    <span style={{ marginLeft: 10, color: "oklch(0.55 0.22 25)" }}>
                      (your answer: <b>{answers[i]}</b>)
                    </span>
                  )}
                </div>
              )}
              {q.explanation && <p style={{ marginTop: 4, color: "var(--ink-3)" }}>{q.explanation}</p>}
            </div>
          )}
        </div>
      );})}
    </div>
  );
}

function SkillsDashboard({ activeCourse, examAnalysis, reportData, streaming, onRun, onPractice }) {
  // 2026-05-20: Mastery card retired (the 2026-05-12 transition note above
  // said "retire once Exam Prep covers everything" — that's now). Backend
  // mastery_tracker + /api/mastery still wired; SkillsDashboard simply
  // doesn't render it. Restore by re-adding a `{ id: "mastery", … }` entry
  // and reviving the `card.id === "mastery"` branch below.
  const cards = [
    { id: "exam-analysis", title: "Exam Analysis", data: examAnalysis },
    { id: "report", title: "Course Report", data: reportData },
  ];
  return (
    <div className="reader-body" style={{ padding: "28px 40px" }}>
      <div className="skill-grid">
        {cards.map(card => (
          <section className="skill-panel" key={card.id}>
            <div className="skill-head">
              <h3>
                {card.title}
                {card.legacy && (
                  <span className="legacy-pill mono" title="See Exam Prep tab for the up-to-date mastery loop">legacy</span>
                )}
              </h3>
              <button className="btn ghost" disabled={!activeCourse || streaming} onClick={() => onRun(card.id)}>Run</button>
            </div>
            {card.legacyHint && (
              <p className="skill-legacy-hint">{card.legacyHint}</p>
            )}
            <pre className="skill-output">{card.data ? (card.data.error || card.data.content || JSON.stringify(card.data, null, 2)) : "Not generated yet."}</pre>
          </section>
        ))}
      </div>
    </div>
  );
}

function SessionHistory({ days }) {
  const entries = Object.entries(days || {}).reverse();
  return (
    <div className="reader-body" style={{ padding: "28px 40px" }}>
      {entries.length === 0 && <p className="empty-state">No session history yet.</p>}
      {entries.map(([date, items]) => (
        <section className="history-day" key={date}>
          <h3>{date}</h3>
          {(items || []).map(item => (
            <div className="history-entry" key={item.id}>
              <span className="mono">{item.timestamp}</span>
              <b>{item.course_id || "All"}</b>
              <span>{item.kind}</span>
            </div>
          ))}
        </section>
      ))}
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
