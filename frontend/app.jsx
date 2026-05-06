/* global React, Library, Reader, Notes, MindMap, Quiz, Assistant, Processing, API,
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
  const [highlightedId, setHighlightedId] = useState(null);
  const [highlightedNode, setHighlightedNode] = useState(null);
  const [uploading, setUploading] = useState(null);
  const [processing, setProcessing] = useState(null);
  const [streaming, setStreaming] = useState(false);
  const [streamProgress, setStreamProgress] = useState(0);

  // ── Core state ──
  const [courses, setCourses] = useState([]);
  const [activeCourse, setActiveCourse] = useState(null);
  const [backendStatus, setBackendStatus] = useState(null);
  const [realNotes, setRealNotes] = useState(null);
  const [realQuiz, setRealQuiz] = useState(null);
  const [realMindmap, setRealMindmap] = useState(null);

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

  // ── Load sources when course changes; restore generated content from cache ──
  useEffect(() => {
    // Restore previous generated content for this course (if any)
    setRealNotes(loadCached(activeCourse, "notes"));
    setRealQuiz(loadCached(activeCourse, "quiz"));
    const cachedMm = loadCached(activeCourse, "mindmap");
    setRealMindmap(cachedMm);
    if (cachedMm) window.MINDMAP = cachedMm;

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
        meta: `${s.chunks} chunks`,
        checked: true, // All checked by default
        collection: "main",
      }));
      setSources(srcs);
      if (srcs.length > 0) setActiveId(srcs[0].id);
    }).catch(() => {});
  }, [activeCourse]);

  // ── Theme ──
  useEffect(() => {
    const root = document.documentElement;
    root.setAttribute("data-theme", tweaks.theme === "paper" ? "" : tweaks.theme);
    document.body.style.setProperty("--density", tweaks.density === "compact" ? "0.92" : tweaks.density === "airy" ? "1.08" : "1");
    document.body.style.setProperty("--base-size", tweaks.baseSize + "px");
  }, [tweaks.theme, tweaks.density, tweaks.baseSize]);

  // ── Get checked source file names for context filtering ──
  function getCheckedSourceFiles() {
    return sources.filter(s => s.checked).map(s => s.title);
  }

  // ── API actions ──
  async function handleGenerateNotes() {
    if (!activeCourse) { alert("Please select a specific course first (not 'All Courses')"); return; }
    setMode("notes");
    setStreaming(true);
    try {
      const data = await API.generateNotes(activeCourse);
      const content = data.content || "Notes generation failed.";
      setRealNotes(content);
      saveCached(activeCourse, "notes", content);
    } catch (e) {
      const msg = "Error: " + e.message;
      setRealNotes(msg);
      // Don't cache errors
    }
    setStreaming(false);
  }

  async function handleGenerateQuiz() {
    if (!activeCourse) { alert("Please select a specific course first"); return; }
    setMode("quiz");
    setStreaming(true);
    try {
      const data = await API.generateQuiz(activeCourse);
      const quiz = data.quiz || data || [];
      setRealQuiz(quiz);
      if (Array.isArray(quiz) && quiz.length > 0) saveCached(activeCourse, "quiz", quiz);
    } catch (e) {
      setRealQuiz([]);
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
    } catch (e) {
      setRealMindmap(null);
    }
    setStreaming(false);
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
  ];

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
            <Reader highlightedId={highlightedId} onHighlight={setHighlightedId} onCite={() => {}} />
          )}
          {effectiveMode === "notes" && (
            realNotes
              ? <RealNotesView content={realNotes} streaming={streaming} />
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
              ? <MindMap layout={tweaks.mindmapLayout} highlightedId={highlightedNode} onNodeClick={setHighlightedNode} />
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
              ? <RealQuizView questions={realQuiz} />
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

/* ── Real Notes View ── */
function RealNotesView({ content, streaming }) {
  const html = content
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

  return (
    <div className="reader-body" style={{ padding: "28px 40px" }}>
      {streaming && <div style={{ color: "var(--accent)", marginBottom: 16, fontFamily: "var(--mono)", fontSize: 12 }}>Generating notes<span className="stream-cursor"></span></div>}
      <div dangerouslySetInnerHTML={{ __html: "<p>" + html + "</p>" }} />
    </div>
  );
}

/* ── Real Quiz View ── */
function RealQuizView({ questions }) {
  const [answers, setAnswers] = React.useState({});
  const [submitted, setSubmitted] = React.useState(false);

  return (
    <div className="reader-body" style={{ padding: "28px 40px" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 20 }}>
        <h2 style={{ fontFamily: "var(--serif)", margin: 0 }}>Practice Quiz — {questions.length} Questions</h2>
        <button
          onClick={() => setSubmitted(!submitted)}
          style={{
            padding: "8px 20px", background: submitted ? "var(--ink-3)" : "var(--accent)",
            color: "white", border: "none", borderRadius: 6, cursor: "pointer", fontSize: 13,
          }}
        >{submitted ? "Hide Answers" : "Grade with AI"}</button>
      </div>

      {questions.map((q, i) => (
        <div key={i} style={{ marginBottom: 20, paddingBottom: 16, borderBottom: "1px dashed var(--rule)" }}>
          <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 6 }}>
            <strong style={{ color: "var(--accent)" }}>Q{i + 1}.</strong>
            <span className="mono" style={{ fontSize: 10, color: "var(--ink-4)" }}>{q.type || "question"} · {q.difficulty || ""}</span>
          </div>
          <p style={{ marginBottom: 10 }}>{q.question}</p>

          {q.options && q.options.map((opt, j) => {
            const optText = typeof opt === "string" ? opt : `${opt.l}. ${opt.t}`;
            const isCorrect = q.correct && (typeof opt === "string" ? opt.charAt(0) === q.correct : opt.l === q.correct);
            return (
              <label key={j} style={{
                display: "block", padding: "5px 8px", cursor: "pointer", borderRadius: 4, marginBottom: 2,
                background: submitted && isCorrect ? "oklch(0.92 0.04 160)" : submitted && answers[i] === j && !isCorrect ? "oklch(0.92 0.06 25)" : "transparent",
              }}>
                <input type="radio" name={`q${i}`} checked={answers[i] === j}
                  onChange={() => !submitted && setAnswers(prev => ({ ...prev, [i]: j }))}
                  style={{ marginRight: 8 }} />
                {optText}
                {submitted && isCorrect && " ✓"}
              </label>
            );
          })}

          {!q.options && (
            <textarea placeholder="Your answer..." rows={3}
              style={{ width: "100%", padding: 8, border: "1px solid var(--rule)", borderRadius: 4, fontFamily: "var(--serif)", fontSize: 14 }}
              onChange={e => setAnswers(prev => ({ ...prev, [i]: e.target.value }))} />
          )}

          {submitted && (q.answer || q.explanation) && (
            <div style={{ marginTop: 8, padding: "10px 14px", background: "var(--paper-2)", borderLeft: "3px solid var(--accent)", borderRadius: "0 4px 4px 0", fontSize: 13 }}>
              {q.answer && <div><strong>Answer:</strong> {q.answer}</div>}
              {q.explanation && <p style={{ marginTop: 4, color: "var(--ink-3)" }}>{q.explanation}</p>}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
