/* global React, Library, Reader, Notes, MindMap, Quiz, Assistant, Processing,
   SAMPLE_SOURCES, TweaksPanel, useTweaks, TweakSection, TweakSelect,
   TweakRadio, TweakSlider, TweakToggle */
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
  const [mode, setMode] = useState("reader"); // reader | notes | mindmap | quiz
  const [sources, setSources] = useState(SAMPLE_SOURCES);
  const [activeId, setActiveId] = useState("s1");
  const [highlightedId, setHighlightedId] = useState(null);
  const [highlightedNode, setHighlightedNode] = useState(null);
  const [uploading, setUploading] = useState(null);
  const [processing, setProcessing] = useState(null); // { file, step }
  const [streaming, setStreaming] = useState(false);
  const [streamProgress, setStreamProgress] = useState(0);

  // Apply theme + density + base size to root
  useEffect(() => {
    const root = document.documentElement;
    root.setAttribute("data-theme", tweaks.theme === "paper" ? "" : tweaks.theme);
    document.body.style.setProperty("--density", tweaks.density === "compact" ? "0.92" : tweaks.density === "airy" ? "1.08" : "1");
    document.body.style.setProperty("--base-size", tweaks.baseSize + "px");
  }, [tweaks.theme, tweaks.density, tweaks.baseSize]);

  // When switching into notes/quiz, simulate streaming
  useEffect(() => {
    if (mode === "notes") {
      setStreaming(true);
      setStreamProgress(1);
      const t1 = setTimeout(() => setStreamProgress(2), 1400);
      const t2 = setTimeout(() => setStreamProgress(3), 2800);
      const t3 = setTimeout(() => { setStreaming(false); setStreamProgress(NOTES_DATA.outline.length); }, 4200);
      return () => { clearTimeout(t1); clearTimeout(t2); clearTimeout(t3); };
    }
  }, [mode, tweaks.noteStyle]);

  function onStartUpload() {
    if (uploading) return;
    const fake = { name: "Thermodynamics — problem set 6.pdf", pct: 0 };
    setUploading(fake);
    let pct = 0;
    const iv = setInterval(() => {
      pct += 12;
      if (pct >= 100) {
        clearInterval(iv);
        setUploading(null);
        // enter processing
        setProcessing({ file: fake.name, step: 0 });
      } else {
        setUploading({ ...fake, pct });
      }
    }, 180);
  }

  // Processing animation
  useEffect(() => {
    if (!processing) return;
    const iv = setInterval(() => {
      setProcessing(p => {
        if (!p) return p;
        if (p.step >= 5) {
          clearInterval(iv);
          return null;
        }
        return { ...p, step: p.step + 1 };
      });
    }, 900);
    return () => clearInterval(iv);
  }, [processing?.file]);

  // While processing, show it in main via mode
  const effectiveMode = processing ? "processing" : mode;

  const activeSources = sources.filter(s => s.checked);

  const tabs = [
    { id: "reader", label: "Reader", num: "§ 7.3" },
    { id: "notes", label: "Notes", num: "OUT" },
    { id: "mindmap", label: "Mind map", num: "17" },
    { id: "quiz", label: "Quiz", num: "Q·6" },
  ];

  return (
    <div className="app">
      {/* ========= Top bar ========= */}
      <header className="topbar">
        <div className="brand">
          <span className="mark">Marginalia</span>
          <span className="ed mono">ed · 0.4</span>
        </div>
        <div className="crumbs mono">
          <span>Organic Chemistry 301</span>
          <span className="sep">›</span>
          <span>Chapter 7</span>
          <span className="sep">›</span>
          <span className="current">Stereochemistry</span>
        </div>
        <div className="spacer"></div>
        <div className="topbar-actions">
          <button className="icon-btn" title="Search">⌕</button>
          <button className="icon-btn" title="Command palette">⌘K</button>
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
          <button className="tool mono">⤓ Export</button>
          <button className="tool mono">◫ Focus</button>
        </div>
        <div className="workspace">
          {effectiveMode === "reader" && (
            <Reader
              highlightedId={highlightedId}
              onHighlight={setHighlightedId}
              onCite={() => {}}
            />
          )}
          {effectiveMode === "notes" && (
            <Notes style={tweaks.noteStyle} streaming={streaming} streamProgress={streamProgress} />
          )}
          {effectiveMode === "mindmap" && (
            <MindMap
              layout={tweaks.mindmapLayout}
              highlightedId={highlightedNode}
              onNodeClick={setHighlightedNode}
            />
          )}
          {effectiveMode === "quiz" && <Quiz />}
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
      />

      {/* ========= Status bar ========= */}
      <footer className="statusbar">
        <div className="item">
          <span className="dot"></span>
          <span>Indexed</span><b>3 sources · 298 pp</b>
        </div>
        <div className="item">
          <span>Study streak</span><b>7 days</b>
        </div>
        <div className="item">
          <span>Chapter 7</span>
          <div className="ring" style={{ "--p": 68 }}></div>
          <b>68%</b>
        </div>
        <div className="item">
          <span>Weak · Regiochem.</span>
          <div className="heatmap" style={{ width: 80 }}>
            {Array.from({length: 28}).map((_, i) => {
              const v = [0,0,1,2,0,1,0,1,2,3,1,0,2,3,2,1,0,1,3,2,3,1,2,0,1,2,3,1][i];
              const bg = ["var(--paper-3)","oklch(0.86 0.04 160)","oklch(0.7 0.07 160)","var(--accent)"][v];
              return <div key={i} className="cell" style={{ background: bg }}></div>;
            })}
          </div>
        </div>
        <div className="spacer"></div>
        <div className="item"><span>Last saved</span><b>just now</b></div>
        <div className="item"><span>v0.4.2</span></div>
      </footer>

      {/* ========= Tweaks ========= */}
      <TweaksPanel title="Tweaks">
        <TweakSection title="Appearance">
          <TweakRadio
            tweaks={tweaks}
            tweakKey="theme"
            label="Theme"
            options={[
              { value: "paper", label: "Paper" },
              { value: "sepia", label: "Sepia" },
              { value: "slate", label: "Slate" },
              { value: "dark", label: "Dark" },
            ]}
          />
          <TweakRadio
            tweaks={tweaks}
            tweakKey="density"
            label="Density"
            options={[
              { value: "compact", label: "Compact" },
              { value: "comfortable", label: "Comfortable" },
              { value: "airy", label: "Airy" },
            ]}
          />
          <TweakSlider
            tweaks={tweaks}
            tweakKey="baseSize"
            label="Base font size"
            min={13}
            max={18}
            step={1}
            unit="px"
          />
        </TweakSection>

        <TweakSection title="Assistant">
          <TweakSelect
            tweaks={tweaks}
            tweakKey="persona"
            label="AI persona"
            options={[
              { value: "Dr. Marginalia", label: "Dr. Marginalia · formal" },
              { value: "Wren", label: "Wren · peer tutor" },
              { value: "Socrates", label: "Socrates · questioning" },
            ]}
          />
        </TweakSection>

        <TweakSection title="Notes layout">
          <TweakRadio
            tweaks={tweaks}
            tweakKey="noteStyle"
            label="Note style"
            options={[
              { value: "outline", label: "Outline" },
              { value: "cornell", label: "Cornell" },
              { value: "cards", label: "Cards" },
            ]}
          />
        </TweakSection>

        <TweakSection title="Mind map">
          <TweakRadio
            tweaks={tweaks}
            tweakKey="mindmapLayout"
            label="Layout"
            options={[
              { value: "radial", label: "Radial" },
              { value: "tree", label: "Tree (L→R)" },
            ]}
          />
        </TweakSection>
      </TweaksPanel>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
