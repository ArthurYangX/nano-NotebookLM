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
  const [backendStatus, setBackendStatus] = useState(null);
  const [realNotes, setRealNotes] = useState(null);
  const [realQuiz, setRealQuiz] = useState(null);
  const [realMindmap, setRealMindmap] = useState(null);
  const [examAnalysis, setExamAnalysis] = useState(null);
  const [reportData, setReportData] = useState(null);
  const [masteryData, setMasteryData] = useState(null);
  const [sessionDays, setSessionDays] = useState({});

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

  // ── Load courses on mount ──
  useEffect(() => {
    API.getCourses().then(data => {
      const crs = data.courses || [];
      setCourses(crs);
      if (crs.length > 0) setActiveCourse(crs[0].id);
      // Update global collections for Library component
      const colors = ["oklch(0.42 0.08 160)", "oklch(0.48 0.12 25)", "oklch(0.45 0.1 255)", "oklch(0.44 0.09 310)", "oklch(0.46 0.11 50)", "oklch(0.43 0.08 200)", "oklch(0.47 0.10 100)", "oklch(0.41 0.09 280)"];
      window.SAMPLE_COLLECTIONS = crs.map((c, i) => ({
        id: c.id, name: c.name, count: c.chunks, color: colors[i % colors.length],
      }));
    }).catch(() => {});
    API.getStatus().then(setBackendStatus).catch(() => {});
  }, []);

  useEffect(() => {
    const iv = setInterval(() => {
      API.getStatus().then(setBackendStatus).catch(() => setBackendStatus(null));
    }, 10000);
    return () => clearInterval(iv);
  }, []);

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
      // "All Courses" mode — show all sources from all courses
      setSources([]);
      // Load sources from all courses
      Promise.all(courses.map(c => API.getSources(c.id).catch(() => ({ sources: [] }))))
        .then(results => {
          const allSrcs = [];
          results.forEach((data, ci) => {
            (data.sources || []).forEach((s, i) => {
              allSrcs.push({
                id: `${courses[ci].id}_${s.id || i}`,
                type: s.type === "pdf" ? "pdf" : s.type === "pptx" ? "ppt" : "txt",
                title: `[${courses[ci].id}] ${s.title}`,
                sourceFile: s.title,  // raw filename, used for backend filter
                meta: `${s.chunks} chunks`,
                checked: true,
                collection: courses[ci].id,
              });
            });
          });
          setSources(allSrcs);
        });
      return;
    }

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
  }, [activeCourse]);

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
  async function handleGenerateNotes() {
    if (!activeCourse) { alert("Please select a specific course first (not 'All Courses')"); return; }
    setMode("notes");
    setStreaming(true);
    setStreamProgress(0);
    setGenerationState(StudyState.createGenerationState());
    try {
      let partial = "";
      const final = await API.streamNotes(activeCourse, null, "markdown", event => {
        if (event.type === "chunk") {
          partial = event.partial;
          setRealNotes(partial);
          setGenerationState(s => StudyState.recordPartialGeneration(s, event.chunk));
          setStreamProgress(p => p + 1);
        } else if (event.type === "error") {
          setGenerationState(s => StudyState.recordGenerationFailure({ ...s, partial: event.partial || partial }, new Error(event.error), (s.failures || 0) + 1));
        }
      });
      if (final && final.type === "error") throw new Error(final.error);
      const content = (final && final.content) || partial || "Notes generation failed.";
      setRealNotes(content);
      saveCached(activeCourse, "notes", content);
      StudyState.saveNoteDraft(localStorage, activeCourse, content);
      await API.appendSessionLog(activeCourse, "generation", { kind: "notes" }).catch(() => {});
    } catch (e) {
      const msg = "Error: " + e.message;
      setRealNotes(prev => prev || msg);
      setGenerationState(s => StudyState.recordGenerationFailure(s, e, (s.failures || 0) + 1));
      // Don't cache errors
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
      const data = await API.generateQuiz(activeCourse, topic);
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
        });
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

    const input = document.createElement("input");
    input.type = "file";
    input.multiple = true;
    input.accept = ".pdf,.pptx,.docx,.md,.txt";
    input.onchange = async (e) => {
      const files = e.target.files;
      if (!files.length) return;

      setUploading({ name: files[0].name + (files.length > 1 ? ` (+${files.length - 1})` : ""), pct: 0 });
      let pct = 0;
      const iv = setInterval(() => {
        pct += 6;
        if (pct >= 90) { clearInterval(iv); setUploading(prev => prev ? { ...prev, pct: 90 } : null); }
        else { setUploading(prev => prev ? { ...prev, pct } : null); }
      }, 200);

      try {
        await API.uploadFiles(courseName, files);
        clearInterval(iv);
        setUploading(null);
        setProcessing({ file: files[0].name, step: 0 });
        const data = await API.getCourses();
        setCourses(data.courses || []);
        setActiveCourse(courseName);
      } catch (err) {
        clearInterval(iv);
        setUploading(null);
        alert("Upload failed: " + err.message);
      }
    };
    input.click();
  }

  // Processing animation
  useEffect(() => {
    if (!processing) return;
    const iv = setInterval(() => {
      setProcessing(p => {
        if (!p) return p;
        if (p.step >= 5) { clearInterval(iv); return null; }
        return { ...p, step: p.step + 1 };
      });
    }, 900);
    return () => clearInterval(iv);
  }, [processing?.file]);

  const effectiveMode = processing ? "processing" : mode;
  const activeSources = sources.filter(s => s.checked);
  const totalChunks = courses.reduce((sum, c) => sum + (c.chunks || 0), 0);

  const tabs = [
    { id: "reader", label: "Reader", num: activeCourse ? "§" : "—" },
    { id: "notes", label: "Notes", num: realNotes ? "✓" : "—" },
    { id: "mindmap", label: "Mind map", num: realMindmap ? "✓" : "—" },
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
            <option value="">All Courses ({totalChunks} chunks)</option>
            {courses.map(c => (
              <option key={c.id} value={c.id}>{c.name} ({c.chunks} chunks)</option>
            ))}
          </select>
        </div>
        <div className="spacer"></div>
        <div className="topbar-actions">
          <button className="icon-btn" title="Generate Notes" onClick={handleGenerateNotes} disabled={streaming}>📝</button>
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
          {effectiveMode === "reader" && (
            <Reader
              sources={sources}
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
                  layout={tweaks.mindmapLayout}
                  highlightedId={highlightedNode}
                  onNodeClick={setHighlightedNode}
                  onSourceClick={handleMindmapSource}
                  onPractice={(topic) => handleGenerateQuiz(topic)}
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
            <Processing fileName={processing.file} activeStep={processing.step} />
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
      />

      {/* ========= Status bar ========= */}
      <footer className="statusbar">
        <div className="item">
          <span className="dot"></span>
          <span>Indexed</span><b>{courses.length} courses · {totalChunks} chunks</b>
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
        <TweakSection title="Mind map">
          <TweakRadio tweaks={tweaks} tweakKey="mindmapLayout" label="Layout"
            options={[
              { value: "radial", label: "Radial" }, { value: "tree", label: "Tree (L→R)" },
            ]} />
        </TweakSection>
      </TweaksPanel>
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
function markdownToHtml(content) {
  return String(content || "")
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/^### (.+)$/gm, "<h3>$1</h3>")
    .replace(/^## (.+)$/gm, "<h2 style='margin-top:20px'>$1</h2>")
    .replace(/^# (.+)$/gm, "<h1>$1</h1>")
    .replace(/^- (.+)$/gm, "<li>$1</li>")
    .replace(/((?:<li>.*?<\/li>\s*)+)/g, "<ul>$1</ul>")
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\\\[([\s\S]*?)\\\]/g, '<div class="math-block">$1</div>')
    .replace(/\$\$([\s\S]*?)\$\$/g, '<div class="math-block">$1</div>')
    .replace(/\$([^$\n]+?)\$/g, '<span class="math-inline">$1</span>')
    .replace(/\[Source:\s*([^\]]+)\]/g, '<span class="ref-chip mono">$1</span>')
    .replace(/\n\n/g, "</p><p>")
    .replace(/\n/g, "<br/>");
}

/* ── Real Notes View ── */
function RealNotesView({ content, streaming, activeCourse, onContentChange, generationState, onRetry }) {
  const [draft, setDraft] = React.useState(content || "");
  const [editing, setEditing] = React.useState(false);

  React.useEffect(() => {
    const cached = activeCourse ? StudyState.loadNoteDraft(localStorage, activeCourse) : "";
    setDraft(cached || content || "");
  }, [activeCourse, content]);

  function updateDraft(value) {
    setDraft(value);
    if (activeCourse) StudyState.saveNoteDraft(localStorage, activeCourse, value);
    onContentChange && onContentChange(value);
  }

  function downloadMarkdown() {
    const exp = StudyState.buildMarkdownExport(activeCourse, draft);
    const blob = new Blob([exp.content], { type: exp.mime });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = exp.filename;
    a.click();
    URL.revokeObjectURL(url);
  }

  function exportPdf() {
    const html = StudyState.buildPdfPrintHtml(activeCourse, draft);
    const win = window.open("", "_blank");
    if (!win) return;
    win.document.write(html);
    win.document.close();
    win.print();
  }

  const html = markdownToHtml(draft);

  return (
    <div className="reader-body" style={{ padding: "28px 40px" }}>
      <div className="notes-toolbar">
        <button className="btn ghost" onClick={() => setEditing(!editing)}>{editing ? "Preview" : "Edit"}</button>
        <button className="btn ghost" onClick={downloadMarkdown}>Markdown</button>
        <button className="btn ghost" onClick={exportPdf}>PDF</button>
        {generationState?.retryable && <button className="btn primary" onClick={onRetry}>Retry</button>}
      </div>
      {streaming && <div style={{ color: "var(--accent)", marginBottom: 16, fontFamily: "var(--mono)", fontSize: 12 }}>Generating notes<span className="stream-cursor"></span></div>}
      {generationState?.status === "failed" && <div className="error-banner">{generationState.errorDetail}</div>}
      {editing
        ? <textarea className="notes-editor" value={draft} onChange={e => updateDraft(e.target.value)} />
        : <div dangerouslySetInnerHTML={{ __html: "<p>" + html + "</p>" }} />
      }
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
