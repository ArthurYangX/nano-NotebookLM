/* global React, Library, Reader, Notes, MindMap, Quiz, Assistant, Processing, API, StudyState,
   SAMPLE_SOURCES, TweaksPanel, useTweaks, TweakSection, TweakSelect,
   TweakRadio, TweakSlider, TweakToggle, NOTES_DATA, QUIZ_DATA, MINDMAP */
const { useState, useEffect, useRef } = React;

const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "theme": "paper",
  "density": "comfortable",
  "baseSize": 15,
  "persona": "Dr. Marginalia",
  "mindmapLayout": "radial",
  "noteStyle": "outline",
  "serifHeads": true
}/*EDITMODE-END*/;

function App() {
  const tweaks = useTweaks(TWEAK_DEFAULTS);
  const [mode, setMode] = useState("reader");
  const [sources, setSources] = useState([]);
  const [activeId, setActiveId] = useState(null);
  const [activePage, setActivePage] = useState(null);
  const [highlightedId, setHighlightedId] = useState(null);
  const [highlightedNode, setHighlightedNode] = useState(null);
  const [citationNotice, setCitationNotice] = useState("");
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
  const [masteryData, setMasteryData] = useState(null);
  const [sessionDays, setSessionDays] = useState({});

  // ── R3-2: explicit user language preference ──
  // Initialised from localStorage via the StudyState helpers (single source
  // of truth — also used by Node-side tests). When null we render a one-time
  // modal blocking the workspace until the user picks zh / en. Topbar chip
  // shows the current value and re-opens the modal so the choice is reversible.
  const [userLang, setUserLangState] = useState(() => StudyState.loadUserLang(window.localStorage));
  const [showLangModal, setShowLangModal] = useState(false);
  // ── R4-5 part 2: backend chip (codex GPT-5.4 / Qwen-RAFT) ──
  // Default = codex (the production main path). Qwen is opt-in and the
  // topbar chip greys out when /api/status reports the AutoDL host is
  // unreachable. Persist across reloads in localStorage so the
  // selection survives a tab refresh.
  const [backend, setBackend] = useState(() => {
    try {
      const v = window.localStorage.getItem("nano-nlm:v1:backend");
      return v === "qwen_raft" ? "qwen_raft" : "codex";
    } catch (e) { return "codex"; }
  });
  function commitBackend(value) {
    setBackend(value);
    try { window.localStorage.setItem("nano-nlm:v1:backend", value); }
    catch (e) {}
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

  // ── R4-1: course list mode (?show_preset=1 → "all", else "user") ──
  // Default hides 8 preset courses so the upload-only flow doesn't see stale
  // ingested chunks. URL flag is the rollback hatch until R4-4 验收 ok.
  const courseModeRef = useRef(
    (typeof window !== "undefined" && new URLSearchParams(window.location.search).get("show_preset") === "1")
      ? "all"
      : "user"
  );

  // fix-all v1 #A6: retry button needs to re-run the last upload. We
  // can't pass a closure through processing state (function identity
  // breaks; React DevTools complains; gc'd on rerender). Use a ref.
  const retryRef = useRef(null);

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
  // If the active course just got hidden, jump to the first visible one
  // (or to All Courses if every course is now hidden).
  useEffect(() => {
    if (!activeCourse) return;
    if (!hiddenCourseIds.includes(activeCourse)) return;
    const next = courses.find(c => !hiddenCourseIds.includes(c.id));
    setActiveCourse(next ? next.id : null);
  }, [hiddenCourseIds, courses, activeCourse]);

  // Keep Library's "Collections" sidebar in sync with the visible-course
  // filter. `window.SAMPLE_COLLECTIONS` is read inside Library's render so
  // a stale value used to linger here until the next page reload. Now it
  // re-derives whenever courses load OR the user toggles a hide. App
  // re-renders on hiddenCourseIds change → Library re-renders → reads the
  // refreshed global.
  useEffect(() => {
    const colors = [
      "oklch(0.42 0.08 160)", "oklch(0.48 0.12 25)",
      "oklch(0.45 0.1 255)", "oklch(0.44 0.09 310)",
      "oklch(0.46 0.11 50)", "oklch(0.43 0.08 200)",
      "oklch(0.47 0.10 100)", "oklch(0.41 0.09 280)",
    ];
    const hidden = new Set(hiddenCourseIds);
    const visible = courses.filter(c => !hidden.has(c.id));
    window.SAMPLE_COLLECTIONS = visible.map((c, i) => ({
      id: c.id, name: c.name, count: c.chunks, color: colors[i % colors.length],
    }));
  }, [courses, hiddenCourseIds]);

  // ── Load courses on mount ──
  useEffect(() => {
    API.getCourses(courseModeRef.current).then(data => {
      const crs = data.courses || [];
      setCourses(crs);
      // Pick the first VISIBLE course as the initial selection so we
      // don't auto-select a hidden one (which the dropdown wouldn't
      // even render). Falls back to All Courses (null) if every course
      // is hidden.
      const hidden = new Set(StudyState.loadHiddenCourses(localStorage));
      const firstVisible = crs.find(c => !hidden.has(c.id));
      if (firstVisible) setActiveCourse(firstVisible.id);
      // SAMPLE_COLLECTIONS now updated by a separate effect that watches
      // hiddenCourseIds so Library's "Collections" sidebar follows the
      // dropdown's visible scope when the user toggles hide/unhide.
    }).catch(() => {});
    API.getStatus().then(setBackendStatus).catch(() => {});
  }, []);

  useEffect(() => {
    // fix-all v1 #V6 (R4-5 review v1): ±20% jitter so concurrent tabs
    // opened together don't all poll in lockstep — the AutoDL host
    // (and the cached qwen health probe) sees a smoothed request rate
    // instead of a 6N req/min unison pulse.
    const POLL_BASE_MS = 10000;
    const POLL_JITTER_RATIO = 0.2;
    const interval = POLL_BASE_MS + (Math.random() * 2 - 1) * POLL_BASE_MS * POLL_JITTER_RATIO;
    const iv = setInterval(() => {
      API.getStatus().then(setBackendStatus).catch(() => setBackendStatus(null));
    }, interval);
    return () => clearInterval(iv);
  }, []);

  // fix-all v1 #V6 (R4-5 review v1): auto-rollback the chip when the
  // operator-side QWEN_RAFT_URL is unconfigured or the AutoDL host
  // becomes unreachable. Without this, localStorage persists a stale
  // "qwen_raft" selection across reloads → chip greys out but state
  // keeps sending backend="qwen_raft" → every chat gets a 422 (URL
  // unset) or a silent fallback (URL set, host down). Auto-rollback
  // resets to codex once status confirms qwen is unavailable.
  useEffect(() => {
    if (!backendStatus || backend !== "qwen_raft") return;
    if (!backendStatus.qwen_raft_configured || !backendStatus.qwen_raft_available) {
      commitBackend("codex");
    }
  }, [backendStatus, backend]);

  // ── Load sources when course changes; restore generated content from cache ──
  useEffect(() => {
    // Restore previous generated content for this course (if any)
    setRealNotes(loadCached(activeCourse, "notes"));
    setRealQuiz(loadCached(activeCourse, "quiz"));
    const cachedMm = loadCached(activeCourse, "mindmap");
    setRealMindmap(cachedMm);
    if (cachedMm) window.MINDMAP = cachedMm;
    setExamAnalysis(loadCached(activeCourse, "exam-analysis"));
    setReportData(loadCached(activeCourse, "report"));
    setMasteryData(loadCached(activeCourse, "mastery"));
    setGenerationState(StudyState.createGenerationState());

    if (!activeCourse) {
      // "All Courses" mode — show sources from every VISIBLE course.
      // Hidden courses are excluded so cross-course search and citation
      // resolution match the dropdown's visible scope.
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
              allSrcs.push({
                id: `${cid}_${s.id || i}`,
                type: s.type === "pdf" ? "pdf" : s.type === "pptx" ? "ppt" : "txt",
                title: `[${cid}] ${s.title}`,
                sourceFile: s.title,
                meta: `${s.chunks} chunks`,
                checked: true,
                collection: cid,
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
        type: s.type === "pdf" ? "pdf" : s.type === "pptx" ? "ppt" : "txt",
        title: s.title,
        sourceFile: s.title,  // raw filename, used for backend filter
        meta: `${s.chunks} chunks`,
        checked: true, // All checked by default
        collection: "main",
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
  useEffect(() => {
    const root = document.documentElement;
    root.setAttribute("data-theme", tweaks.theme === "paper" ? "" : tweaks.theme);
    document.body.style.setProperty("--density", tweaks.density === "compact" ? "0.92" : tweaks.density === "airy" ? "1.08" : "1");
    document.body.style.setProperty("--base-size", tweaks.baseSize + "px");
  }, [tweaks.theme, tweaks.density, tweaks.baseSize]);

  // ── Get checked source file names for context filtering ──
  // Delegates to StudyState.getCheckedSourceFiles which returns raw filenames
  // (matching chunk.source_file) so the backend qa_skill filter actually hits.
  function getCheckedSourceFiles() {
    return StudyState.getCheckedSourceFiles(sources);
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
    setStreamProgress(0);
    setNoteCacheStats(null);
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
    function rebuildDraftFromFiles() {
      // Incremental cache (2026-05-11): both `done` (fresh from LLM)
      // and `cached` (replayed from per_file_cache.json) contribute to
      // the merged draft. Order is plan-index order, matching backend's
      // concat_draft.
      return fileSections
        .filter(f => f && (f.status === "done" || f.status === "cached") && f.content)
        .map(f => `\\section{${escapeLatexTitle(f.source_file)}}\n${f.content}`)
        .join("\n\n");
    }
    let reviewPartial = "";
    let inReview = false;
    // Throttle setRealNotes during review_chunk (review-swarm fix-all):
    // backend ships ~10 deltas/sec; each setRealNotes invalidates the
    // useMemo for latexToHtml(draft), which re-runs the 8-stage regex
    // pipeline on an ever-growing string. Coalesce to ~250ms intervals.
    let reviewSetTimer = null;
    function scheduleReviewUpdate() {
      if (reviewSetTimer) return;
      reviewSetTimer = setTimeout(() => {
        reviewSetTimer = null;
        setRealNotes(reviewPartial);
      }, 250);
    }
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
        if (!inReview) setRealNotes(rebuildDraftFromFiles());
      }, 0);
    }
    try {
      const final = await API.streamFullCourseNotes(activeCourse, event => {
        if (event.type === "plan") {
          for (let i = 0; i < event.total; i += 1) {
            fileSections[i] = {
              source_file: event.files && event.files[i] ? event.files[i].source_file : `file_${i}`,
              status: "pending",
              content: null,
              error: null,
              cached: !!(event.files && event.files[i] && event.files[i].cached),
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
          }
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
            fileSections[event.idx].content = event.content;
            fileSections[event.idx].source_file = event.source_file || fileSections[event.idx].source_file;
          }
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
        } else if (event.type === "review_chunk") {
          // Backend ships `delta` only — review_chunk would otherwise be
          // O(N²) on the wire. Accumulate locally.
          reviewPartial = reviewPartial + (event.delta || "");
          // Throttle the React render to ~250ms (see scheduleReviewUpdate
          // above); setGenerationState stays un-throttled because it only
          // tracks partial text for retry state, no expensive re-render.
          scheduleReviewUpdate();
          setGenerationState(s => StudyState.recordPartialGeneration(s, event.delta || ""));
        } else if (event.type === "error") {
          setGenerationState(s => StudyState.recordGenerationFailure(
            { ...s, partial: event.partial || reviewPartial || rebuildDraftFromFiles() },
            new Error(event.error || "stream_failed"),
            (s.failures || 0) + 1,
          ));
        }
      }, { userLang, force });
      // Cancel any pending throttled review render — the next setRealNotes
      // below installs the canonical final content.
      if (reviewSetTimer) { clearTimeout(reviewSetTimer); reviewSetTimer = null; }
      if (final && final.type === "error") throw new Error(final.error || "stream_failed");
      const content = (final && final.content) || reviewPartial || rebuildDraftFromFiles() || "Notes generation failed.";
      setRealNotes(content);
      saveCached(activeCourse, "notes", content);
      StudyState.saveNoteDraft(localStorage, activeCourse, content);
      // fix-all v1 #18: backend's /api/notes/full-course/stream writes
      // its own session-log row (kind="notes-full-course"); previously
      // this followed up with a second row (kind="notes") on every
      // success, double-counting in any future kind aggregation.
    } catch (e) {
      const msg = "Error: " + e.message;
      setRealNotes(prev => prev || rebuildDraftFromFiles() || msg);
      setGenerationState(s => StudyState.recordGenerationFailure(s, e, (s.failures || 0) + 1));
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
      } else if (kind === "mastery") {
        const data = await API.getMastery(activeCourse);
        setMasteryData(data);
        saveCached(activeCourse, "mastery", data);
      }
      setMode("notes");
      await API.appendSessionLog(activeCourse, "generation", { kind }).catch(() => {});
    } catch (e) {
      if (kind === "exam-analysis") setExamAnalysis({ error: e.message });
      if (kind === "report") setReportData({ error: e.message });
      if (kind === "mastery") setMasteryData({ error: e.message, weak_areas: [] });
    }
    setStreaming(false);
  }

  function handleCitation(refText) {
    const nav = StudyState.resolveCitationNavigation(refText, sources);
    if (!nav.ok) {
      setCitationNotice(nav.message);
      return;
    }
    setCitationNotice("");
    setActiveId(nav.activeId);
    setActivePage(nav.page);
    setHighlightedId(nav.highlightedId);
    setMode("reader");
  }

  function handleMindmapSource(chunk) {
    const ref = `[Source: ${chunk.source_file || ""}, PDF p.${chunk.page || 1}, chunk ${chunk.chunk_id || ""}]`;
    handleCitation(ref);
  }

  async function handleRetryGeneration() {
    setGenerationState(s => StudyState.retryGeneration(s));
    await handleGenerateNotes();
  }

  function onStartUpload() {
    if (uploading) return;
    // Ask for course name first
    const existingNames = courses.map(c => c.name).join(", ");
    const defaultName = activeCourse || "";
    const courseName = prompt(
      `Upload to which course?\n\nExisting: ${existingNames || "none"}\n\nEnter a course name (new or existing):`,
      defaultName
    );
    if (!courseName) return;

    // fix-all v1 #A6: capture files in a closure so the retry button can
    // actually re-invoke the upload with the same payload (previously
    // onRetry={setProcessing(null)} only dismissed the modal — user
    // had to re-pick files manually).
    const runUpload = async (files) => {
      setUploading({ name: files[0].name + (files.length > 1 ? ` (+${files.length - 1})` : ""), pct: 0 });
      let pct = 0;
      const iv = setInterval(() => {
        pct += 6;
        if (pct >= 90) { clearInterval(iv); setUploading(prev => prev ? { ...prev, pct: 90 } : null); }
        else { setUploading(prev => prev ? { ...prev, pct } : null); }
      }, 200);

      try {
        setProcessing({
          file: files[0].name,
          step: 0,
          stages: { chunking: 0, embedding: 0, kg_stage_a: 0, kg_stage_b: 0 },
          errorStage: null,
          errorMsg: null,
          done: false,
          retryPayload: files,
        });
        const final = await API.uploadFiles(courseName, files, (ev) => {
          if (!ev) return;
          if (ev.type === "stage") {
            setProcessing(p => p ? {
              ...p,
              stages: { ...(p.stages || {}), [ev.stage]: ev.progress },
            } : p);
          } else if (ev.type === "done") {
            setProcessing(p => p ? { ...p, done: true } : p);
          } else if (ev.type === "error") {
            setProcessing(p => p ? { ...p, errorStage: ev.stage || "unknown", errorMsg: ev.error } : p);
          }
        });
        clearInterval(iv);
        setUploading(null);
        // fix-all v1 #A7: even on error, refresh courses so the
        // partially-ingested course (chunks landed before KG failed) is
        // visible in the dropdown — the test
        // `test_upload_stream_extractor_failure_emits_error_event` proves
        // chunks survive the extractor crash, but without this refresh
        // the user could never reach them.
        try {
          const data = await API.getCourses(courseModeRef.current);
          setCourses(data.courses || []);
          if (!final || final.type !== "error") {
            setActiveCourse(courseName);
          }
        } catch { /* best-effort refresh */ }
      } catch (err) {
        clearInterval(iv);
        setUploading(null);
        setProcessing(p => p ? { ...p, errorStage: "transport", errorMsg: err.message } : null);
      }
    };

    // Expose runUpload to the Processing render via retryRef so the
    // retry button re-invokes the upload with the original `files`
    // captured in this closure (preserves courseName too).
    retryRef.current = runUpload;

    const input = document.createElement("input");
    input.type = "file";
    input.multiple = true;
    input.accept = ".pdf,.pptx,.docx,.md,.txt";
    input.onchange = (e) => {
      const files = e.target.files;
      if (!files.length) return;
      runUpload(files);
    };
    input.click();
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

  const tabs = [
    { id: "reader", label: "Reader", num: activeCourse ? "§" : "—" },
    { id: "notes", label: "Notes", num: realNotes ? "✓" : "—" },
    { id: "mindmap", label: "Knowledge Graph", num: realMindmap ? "✓" : "—" },
    { id: "quiz", label: "Quiz", num: realQuiz && realQuiz.length ? `Q·${realQuiz.length}` : "—" },
    { id: "skills", label: "Skills", num: [examAnalysis, reportData, masteryData].filter(Boolean).length || "—" },
    { id: "history", label: "History", num: Object.keys(sessionDays || {}).length || "—" },
  ];
  const statusView = StudyState.formatStatusBar(backendStatus);
  const masteryView = StudyState.formatMasteryState(masteryData || {});

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
                  {flag} {c.name} ({c.chunks} chunks)
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
          {/* R4-5 part 2: backend chip — toggles codex / qwen_raft. Greys
              out when /api/status reports Qwen unavailable or unconfigured. */}
          <button
            className={"backend-chip mono backend-" + (backend === "qwen_raft" ? "qwen" : "codex")}
            title={
              !backendStatus
                ? "Loading backend status..."
                : !backendStatus.qwen_raft_configured
                ? "Qwen-RAFT 未配置 (设置 QWEN_RAFT_URL 启用)"
                : !backendStatus.qwen_raft_available
                ? "Qwen-RAFT 不可用，自动使用 codex GPT-5.4"
                : "当前后端: " + (backend === "qwen_raft" ? "Qwen2.5-7B-RAFT" : "codex GPT-5.4") + " (点击切换)"
            }
            onClick={() => {
              const next = backend === "qwen_raft" ? "codex" : "qwen_raft";
              commitBackend(next);
            }}
            disabled={streaming || !backendStatus || !backendStatus.qwen_raft_configured || !backendStatus.qwen_raft_available}
          >
            {backend === "qwen_raft" ? "🎓 Qwen" : "🤖 GPT-5.4"}
          </button>
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
          <button className="icon-btn" title="Generate Quiz" onClick={handleGenerateQuiz} disabled={streaming}>❓</button>
          <button className="icon-btn" title="Build Knowledge Graph" onClick={handleGenerateMindmap} disabled={streaming}>🧠</button>
          <button className="icon-btn" title="Exam Analysis" onClick={() => handleSkillEntry("exam-analysis")} disabled={streaming}>⌁</button>
          <button className="icon-btn" title="Course Report" onClick={() => handleSkillEntry("report")} disabled={streaming}>▤</button>
          <button className="icon-btn" title="Mastery Dashboard" onClick={() => handleSkillEntry("mastery")} disabled={streaming}>◎</button>
          <button className="icon-btn" title="Settings">✦</button>
        </div>
      </header>

      {/* ========= Library ========= */}
      <Library
        sources={sources}
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
          {(!uploading && !processing && visibleCourses.length === 0) && (
            <div className="empty-courses-cta" data-testid="empty-courses">
              <div className="empty-courses-card">
                <div className="empty-courses-glyph">📂</div>
                <h2>上传文档开始</h2>
                <p>nano-NOTEBOOKLM 现在是 upload-only 模式。先上传一份 PDF / PPTX / DOCX / Markdown，系统会自动抽取章节、构建知识图谱，再驱动问答与笔记。</p>
                <button className="btn-primary" onClick={onStartUpload}>上传第一个文档</button>
                <p className="hint mono">回滚到旧课程：在 URL 末尾加 <code>?show_preset=1</code></p>
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
                  onContentChange={(content) => {
                    setRealNotes(content);
                    saveCached(activeCourse, "notes", content);
                  }}
                  onRetry={handleRetryGeneration}
                  generationState={generationState}
                  onCitation={handleCitation}
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
                  onPractice={(topic) => handleGenerateQuiz(topic)}
                  onDataChange={(data) => {
                    setRealMindmap(data);
                    if (activeCourse && data) saveCached(activeCourse, "mindmap", data);
                  }}
                />
              : <ActionPlaceholder
                  title="Knowledge Graph"
                  desc={activeCourse ? `Extract concepts and relationships from ${activeCourse} materials` : "Select a course first"}
                  btnLabel={streaming ? "Generating (~30s)..." : "Build Knowledge Graph"}
                  onAction={handleGenerateMindmap}
                  disabled={!activeCourse || streaming}
                  hint="Uses AI to analyze course chunks and build a visual concept map."
                />
          )}
          {effectiveMode === "quiz" && (
            realQuiz && realQuiz.length > 0
              ? <RealQuizView questions={realQuiz} activeCourse={activeCourse} />
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
                // captured at onStartUpload time. Falls back to closing
                // the modal if the retry handler isn't wired (e.g. page
                // reload between original click and retry).
                if (retryRef.current && processing.retryPayload) {
                  retryRef.current(processing.retryPayload);
                } else {
                  setProcessing(null);
                }
              }}
            />
          )}
          {effectiveMode === "skills" && (
            <SkillsDashboard
              activeCourse={activeCourse}
              examAnalysis={examAnalysis}
              reportData={reportData}
              masteryData={masteryData}
              masteryView={masteryView}
              streaming={streaming}
              onRun={handleSkillEntry}
              onPractice={(topic) => handleGenerateQuiz(topic)}
            />
          )}
          {effectiveMode === "history" && (
            <SessionHistory days={sessionDays} />
          )}
        </div>
      </main>

      {/* ========= Assistant ========= */}
      <Assistant
        mode={effectiveMode}
        persona={tweaks.persona}
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
      />

      {/* ========= Status bar ========= */}
      <footer className="statusbar">
        <div className="item">
          <span className="dot"></span>
          <span>Indexed</span><b>{visibleCourses.length} courses · {totalChunks} chunks</b>
        </div>
        <div className="item">
          <span>Backend</span><b>{backendStatus ? backendStatus.backends.join(", ") || "none" : "..."}</b>
        </div>
        <div className={"item" + (statusView.degraded ? " degraded" : "")}>
          <span>Status</span><b>{statusView.text}</b>
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
      <TweaksPanel title="Tweaks">
        <TweakSection title="Appearance">
          <TweakRadio tweaks={tweaks} tweakKey="theme" label="Theme"
            options={[
              { value: "paper", label: "Paper" }, { value: "sepia", label: "Sepia" },
              { value: "slate", label: "Slate" }, { value: "dark", label: "Dark" },
            ]} />
          <TweakRadio tweaks={tweaks} tweakKey="density" label="Density"
            options={[
              { value: "compact", label: "Compact" }, { value: "comfortable", label: "Comfortable" },
              { value: "airy", label: "Airy" },
            ]} />
          <TweakSlider tweaks={tweaks} tweakKey="baseSize" label="Base font size" min={13} max={18} step={1} unit="px" />
        </TweakSection>
        <TweakSection title="Assistant">
          <TweakSelect tweaks={tweaks} tweakKey="persona" label="AI persona"
            options={[
              { value: "Dr. Marginalia", label: "Dr. Marginalia · formal" },
              { value: "Wren", label: "Wren · peer tutor" },
              { value: "Socrates", label: "Socrates · questioning" },
            ]} />
        </TweakSection>
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
              换浏览器或清 localStorage 后会重置。
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
                  <label
                    key={c.id}
                    style={{
                      display: "flex", alignItems: "center", gap: 8,
                      padding: "6px 8px", border: "1px solid var(--paper-3)",
                      borderRadius: 4, cursor: "pointer",
                      opacity: hidden ? 0.55 : 1,
                    }}
                  >
                    <input
                      type="checkbox"
                      checked={!hidden}
                      onChange={() => toggleCourseHidden(c.id)}
                    />
                    <span className="mono" style={{ fontSize: 12 }}>
                      {flag} {c.name}
                    </span>
                    <span className="mono" style={{ marginLeft: "auto", fontSize: 11, color: "var(--ink-3)" }}>
                      {c.chunks} chunks
                    </span>
                  </label>
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

function findTextRangeInRoot(root, text, before, after) {
  // Walk text nodes; concatenate to find `text` (preferring positions whose
  // surrounding chars best match before/after). Returns a Range or null.
  // fix: insert a phantom "\n\n" into `combined` whenever we cross a
  // block-level ancestor boundary, so the concatenated string aligns with
  // what `sel.toString()` and `range.toString()` produce in modern
  // browsers (which inject newline between block elements). Without this,
  // any selection that spans a heading + paragraph fails to re-apply
  // because the saved text contains "\n\n" but the walker produces a
  // glued string. The phantom characters do NOT enter `nodes`, so
  // start/end offsets resolved via `locate()` always land on real text.
  const BLOCK_SEL = "h1,h2,h3,h4,h5,h6,p,li,blockquote,pre,div";
  if (!root || !text) return null;
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

function NotesTOC({ items, activeId, onJump, onClose }) {
  if (!items.length) return null;
  return (
    <nav className="notes-toc" aria-label="Table of contents">
      <div className="toc-head mono">
        <span>Contents</span>
        {onClose && <button className="side-close" onClick={onClose} title="Hide TOC" aria-label="Hide TOC">×</button>}
      </div>
      <ul>
        {items.map(it => (
          <li key={it.id} className={`toc-l${it.level}` + (it.id === activeId ? " active" : "")}>
            <button onClick={() => onJump(it.id)} title={it.text}>{it.text}</button>
          </li>
        ))}
      </ul>
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
            <button className="hl-jump" onClick={() => onJump(h.id)} title={h.text}>
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

function RealNotesView({ content, streaming, activeCourse, onContentChange, generationState, onRetry, onCitation }) {
  const [draft, setDraft] = React.useState(content || "");
  const [editing, setEditing] = React.useState(false);
  const [highlights, setHighlights] = React.useState([]);
  const [tocItems, setTocItems] = React.useState([]);
  const [activeTocId, setActiveTocId] = React.useState(null);
  const [showToc, setShowToc] = React.useState(true);
  const [showDrawer, setShowDrawer] = React.useState(true);
  const [selMenu, setSelMenu] = React.useState(null); // {x, y, text, before, after}
  const [popover, setPopover] = React.useState(null); // {x, y, hl}
  const previewRef = React.useRef(null);

  // Course switch — full reset (clears edit-mode + popovers + restores cached draft).
  React.useEffect(() => {
    setSelMenu(null);
    setPopover(null);
    setEditing(false);
    const cached = activeCourse ? StudyState.loadNoteDraft(localStorage, activeCourse) : "";
    setDraft(cached || content || "");
  }, [activeCourse]);

  // Streaming chunks — overwrite draft only while streaming, so a regenerate
  // pass updates the preview without clobbering edits the user typed in
  // Edit mode after the previous generation finished.
  React.useEffect(() => {
    if (editing) return;
    if (!streaming) return;
    if (typeof content === "string") setDraft(content);
  }, [content, streaming, editing]);

  // Highlights / TOC. During streaming we extract the TOC from the partial
  // LaTeX but DO NOT prune highlights — the partial doesn't contain
  // sections that haven't streamed yet, and pruning would silently delete
  // their anchors from localStorage.
  // LaTeX-refactor: TOC is extracted from `\section{...}` macros via the
  // latex-to-html shim's extractor; falls back to the markdown helper for
  // older content (legacy partial drafts).
  React.useEffect(() => {
    if (!activeCourse) { setHighlights([]); setTocItems([]); return; }
    const toc = (typeof NanoLatex !== "undefined" && NanoLatex.extractTOC)
      ? NanoLatex.extractTOC(draft)
      : StudyState.extractHeadingTOC(draft);
    setTocItems(toc);
    if (streaming) return;
    const result = StudyState.pruneStaleHighlights(localStorage, activeCourse, draft);
    setHighlights(result.kept);
  }, [activeCourse, draft, streaming]);

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
    const scroller = previewRef.current && previewRef.current.closest(".notes-reader-body");
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
    function tryRestore() {
      if (stop) return;
      attempts += 1;
      const maxY = Math.max(0, scroller.scrollHeight - scroller.clientHeight);
      if (maxY >= saved) {
        applyScroll(saved);
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
    const tailTimer = setTimeout(() => {
      if (stop) return;
      applyScroll(saved);
    }, 600);

    return () => {
      stop = true;
      cancelAnimationFrame(raf);
      clearTimeout(tailTimer);
    };
  }, [activeCourse, editing, streaming]);

  React.useEffect(() => {
    if (!activeCourse) return;
    if (editing) return;
    const scroller = previewRef.current && previewRef.current.closest(".notes-reader-body");
    if (!scroller) return;
    // BUG FIX (round 2 of scroll cache): the original throttled-save
    // path queued the localStorage write inside a rAF. When the user
    // clicked a citation chip, RealNotesView unmounted and the rAF then
    // fired with a detached DOM whose .scrollTop reads as 0 —
    // overwriting the user's real position with 0 right at unmount.
    // Now we (a) flag the effect as detached in cleanup so the rAF
    // bails, and (b) do a synchronous final save on cleanup while the
    // DOM is still connected. That last-chance save is what lets the
    // restore on remount land at the right spot.
    let detached = false;
    let ticking = false;
    function onScroll() {
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(() => {
        ticking = false;
        if (detached) return;
        StudyState.saveNotesScroll(localStorage, activeCourse, scroller.scrollTop);
      });
    }
    scroller.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      detached = true;
      scroller.removeEventListener("scroll", onScroll);
      // Final flush: capture the user's last position before the
      // component unmounts. isConnected guards against the rare case
      // where React has already detached the node.
      if (scroller.isConnected) {
        StudyState.saveNotesScroll(localStorage, activeCourse, scroller.scrollTop);
      }
    };
  }, [activeCourse, editing]);

  // Regeneration kicks off → drop the saved offset so the freshly
  // streaming notes mount at top, not at the previous document's
  // halfway-down position.
  React.useEffect(() => {
    if (streaming && activeCourse) {
      StudyState.clearNotesScroll(localStorage, activeCourse);
    }
  }, [streaming, activeCourse]);

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
    const probe = { text, before: "", after: "" };
    const idx = StudyState.locateHighlight(draft, probe);
    if (idx >= 0) {
      const ctx = StudyState.buildContextWindows(draft, idx, text);
      probe.before = ctx.before;
      probe.after = ctx.after;
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
    <div className="reader-body notes-reader-body">
      <div className="notes-toolbar">
        <button className="btn ghost" onClick={() => { setEditing(!editing); setSelMenu(null); setPopover(null); }}>{editing ? "Preview" : "Edit"}</button>
        <button className="btn ghost" onClick={downloadLatex} title="下载 .tex 源文件">.tex</button>
        <button className="btn ghost" onClick={printPdfFromBrowser} title="浏览器打印（快速预览）">PDF (print)</button>
        <button
          className="btn ghost"
          onClick={compilePdfWithTectonic}
          disabled={tectonicAvailable === false}
          title={tectonicAvailable === false
            ? "Tectonic 不可用：服务器未安装 LaTeX 编译器"
            : "服务端 LaTeX 编译（学术排版）"}
        >PDF (compile)</button>
        <button className="btn ghost" onClick={() => setShowToc(v => !v)} disabled={editing}>{showToc ? "Hide TOC" : "Show TOC"}</button>
        <button className="btn ghost" onClick={() => setShowDrawer(v => !v)} disabled={editing}>{showDrawer ? "Hide Highlights" : `Highlights · ${highlights.length}`}</button>
        {generationState?.retryable && <button className="btn primary" onClick={onRetry}>Retry</button>}
      </div>
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
          {showToc && <NotesTOC items={tocItems} activeId={activeTocId} onJump={jumpToHeading} onClose={() => setShowToc(false)} />}
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
function RealQuizView({ questions, activeCourse }) {
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
          <button className="btn ghost" onClick={() => setReviewWrong(!reviewWrong)} disabled={!submitted}>
            {reviewWrong ? "All Questions" : "Wrong Only"}
          </button>
          <button className="btn primary" onClick={() => setSubmitted(!submitted)}>{submitted ? "Hide Answers" : "Grade"}</button>
        </div>
      </div>
      {stale && <div className="error-banner">Saved answers are stale because the question set changed.</div>}

      {visibleQuestions.map((q) => {
        const i = questions.indexOf(q);
        return (
        <div key={i} style={{ marginBottom: 20, paddingBottom: 16, borderBottom: "1px dashed var(--rule)" }}>
          <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 6 }}>
            <strong style={{ color: "var(--accent)" }}>Q{i + 1}.</strong>
            <span className="mono" style={{ fontSize: 10, color: "var(--ink-4)" }}>{q.type || "question"} · {q.difficulty || ""}</span>
          </div>
          <p style={{ marginBottom: 10 }}>{q.question}</p>

          {q.options && q.options.map((opt, j) => {
            const optText = typeof opt === "string" ? opt : `${opt.l}. ${opt.t}`;
            const isCorrect = q.correct && (typeof opt === "string" ? opt.charAt(0) === q.correct : opt.l === q.correct);
            const optValue = typeof opt === "string" ? opt.charAt(0) : opt.l;
            return (
              <label key={j} style={{
                display: "block", padding: "5px 8px", cursor: "pointer", borderRadius: 4, marginBottom: 2,
                background: submitted && isCorrect ? "oklch(0.92 0.04 160)" : submitted && answers[i] === optValue && !isCorrect ? "oklch(0.92 0.06 25)" : "transparent",
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
            <div style={{ marginTop: 8, padding: "10px 14px", background: "var(--paper-2)", borderLeft: "3px solid var(--accent)", borderRadius: "0 4px 4px 0", fontSize: 13 }}>
              {q.answer && <div><strong>Answer:</strong> {q.answer}</div>}
              {q.explanation && <p style={{ marginTop: 4, color: "var(--ink-3)" }}>{q.explanation}</p>}
            </div>
          )}
        </div>
      );})}
    </div>
  );
}

function SkillsDashboard({ activeCourse, examAnalysis, reportData, masteryData, masteryView, streaming, onRun, onPractice }) {
  const cards = [
    { id: "exam-analysis", title: "Exam Analysis", data: examAnalysis },
    { id: "report", title: "Course Report", data: reportData },
    { id: "mastery", title: "Mastery", data: masteryData },
  ];
  return (
    <div className="reader-body" style={{ padding: "28px 40px" }}>
      <div className="skill-grid">
        {cards.map(card => (
          <section className="skill-panel" key={card.id}>
            <div className="skill-head">
              <h3>{card.title}</h3>
              <button className="btn ghost" disabled={!activeCourse || streaming} onClick={() => onRun(card.id)}>Run</button>
            </div>
            {card.id === "mastery" && masteryData ? (
              <div>
                {masteryView.empty ? <p className="empty-state">{masteryView.text}</p> : (
                  <div className="weak-list">
                    {(masteryData.weak_areas || []).map(w => (
                      <button key={w.concept} className="weak-row" onClick={() => onPractice(w.concept)}>
                        <span>{w.concept}</span><b>{Math.round((w.score || 0) * 100)}%</b>
                      </button>
                    ))}
                  </div>
                )}
              </div>
            ) : (
              <pre className="skill-output">{card.data ? (card.data.error || card.data.content || JSON.stringify(card.data, null, 2)) : "Not generated yet."}</pre>
            )}
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
