/* global React */
const { useState: useStateA, useEffect: useEffectA, useRef: useRefA } = React;

function ThinkingSteps({ steps, activeIndex }) {
  return (
    <div className="thinking mono">
      {steps.map((s, i) => {
        const cls = i < activeIndex ? "step done" : i === activeIndex ? "step active" : "step";
        return (
          <div className={cls} key={i}>
            <span className="dot"></span>
            <span>{s}</span>
          </div>
        );
      })}
    </div>
  );
}

function Assistant({ mode, persona = "Dr. Marginalia", activeSources, onAction, streaming, streamProgress }) {
  const [text, setText] = useStateA("");
  const bodyRef = useRefA(null);

  useEffectA(() => {
    if (bodyRef.current) bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
  }, [streamProgress, mode]);

  const personaMeta = {
    "Dr. Marginalia": { desc: "Professor · formal · rigorous", av: "M" },
    "Wren": { desc: "Peer tutor · patient · plain-spoken", av: "W" },
    "Socrates": { desc: "Asks you questions back", av: "S" },
  }[persona] || { desc: "", av: "M" };

  const thinkingSteps = [
    "Indexing 3 sources · 298 pages",
    "Retrieving passages on anti / syn addition",
    "Cross-checking Clayden § 8 with lecture 12",
    "Drafting structured outline",
    "Generating key-term index",
  ];

  const suggestions = {
    reader: ["Explain this paragraph", "Define bromonium ion", "Find contradictions", "Related in other sources"],
    notes: ["Rewrite shorter", "Add worked example", "Export to flashcards", "Generate quiz from this"],
    mindmap: ["Expand ‘Evidence’ branch", "Connect to lecture 12", "Export as image", "Explain a node"],
    quiz: ["Make harder version", "Focus on weak areas", "Explain Q1", "Save wrong answers"],
    processing: [],
  };

  return (
    <aside className="assistant" data-screen-label="Assistant">
      <div className="asst-header">
        <div className="asst-title">
          <div className="asst-avatar">{personaMeta.av}</div>
          <div>
            <div>{persona}</div>
            <div style={{ fontFamily: "var(--mono)", fontSize: 9, color: "var(--ink-4)", textTransform: "uppercase", letterSpacing: "0.08em", fontStyle: "normal" }}>{personaMeta.desc}</div>
          </div>
        </div>
        <div className="asst-status">
          <span className="pulse"></span>
          {streaming ? "Drafting" : "Ready"}
        </div>
      </div>

      <div className="asst-context mono">
        <span>Context ·</span>
        {activeSources.slice(0, 3).map(s => (
          <span key={s.id} className="ctx-chip">{s.title.length > 24 ? s.title.slice(0, 24) + "…" : s.title}</span>
        ))}
        {activeSources.length > 3 && <span className="ctx-chip">+{activeSources.length - 3}</span>}
      </div>

      <div className="asst-body" ref={bodyRef}>
        <div className="msg user">
          <div className="who">You · just now</div>
          <div className="bubble">
            {mode === "notes" && "Generate structured notes from these three sources, covering §7.3 stereochemistry."}
            {mode === "mindmap" && "Build a mind map of the addition-reaction landscape."}
            {mode === "quiz" && "Make a practice quiz — midterm difficulty, 6 questions."}
            {mode === "reader" && "Summarize this page and flag anything I should memorize."}
            {mode === "processing" && "Just uploaded 'March 2024 midterm.pdf'. Ingest and index."}
          </div>
        </div>

        {streaming && <ThinkingSteps steps={thinkingSteps} activeIndex={Math.min(streamProgress, thinkingSteps.length - 1)} />}

        <div className="msg ai">
          <div className="who">{persona} · {streaming ? "drafting…" : "2s"}</div>
          <div className="bubble">
            {mode === "notes" && !streaming && (
              <>
                I've drafted a three-part outline: the two modes, the bromonium mechanism, and hydroboration as the canonical syn case. The middle section answers your earlier question about why Br₂ doesn't rearrange — the bridged ion prevents it.
              </>
            )}
            {mode === "notes" && streaming && (
              <>Drafting section {Math.min(streamProgress, 3)} of 3<span className="stream-cursor"></span></>
            )}
            {mode === "mindmap" && (
              <>Structured the chapter into three branches: <b>anti</b>, <b>syn</b>, and <b>evidence</b>. Click any node to see its source passages; click the <b>−</b> handle to collapse a subtree.</>
            )}
            {mode === "quiz" && (
              <>Six questions, weighted toward mechanism recognition (Q1, Q3) and applied reasoning (Q2). I used your last three problem-set scores to calibrate — you've been weakest on regiochemistry prompts, so I biased the pool.</>
            )}
            {mode === "reader" && (
              <>This page establishes the syn/anti distinction as a framework for the rest of Chapter 7. The key term to memorize is <b>bromonium ion</b> — it appears in 4 of the 6 past-paper questions I indexed.</>
            )}
            {mode === "processing" && (
              <>Parsing OCR'd text and extracting question structure. Once indexed, I'll align its topics with your current sources and flag overlaps.</>
            )}
          </div>
          {!streaming && mode !== "processing" && (
            <div className="refs">
              <span className="ref-chip mono">§ 7.3.1</span>
              <span className="ref-chip mono">Clayden p. 432</span>
              <span className="ref-chip mono">Lect. 12 · slide 23</span>
            </div>
          )}
        </div>
      </div>

      <div className="asst-suggest">
        {(suggestions[mode] || []).map(s => (
          <span className="suggest-chip mono" key={s} onClick={() => setText(s)}>{s}</span>
        ))}
      </div>

      <div className="asst-input">
        <textarea
          placeholder={`Ask ${persona.split(" ")[0]} about your sources…`}
          value={text}
          onChange={e => setText(e.target.value)}
          rows={2}
        ></textarea>
        <button className="send" onClick={() => { setText(""); }}>↑</button>
      </div>
    </aside>
  );
}

Object.assign(window, { Assistant });
