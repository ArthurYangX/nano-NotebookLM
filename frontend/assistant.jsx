/* global React, API */
const { useState: useStateA, useEffect: useEffectA, useRef: useRefA } = React;

/* ── Helpers ── */
function parseMessage(text) {
  // Extract [Source: ...] citations and separate them from body
  const sourceRegex = /\[Source:\s*([^\]]+)\]/g;
  const refs = [];
  let match;
  while ((match = sourceRegex.exec(text)) !== null) {
    const ref = match[1].trim();
    if (!refs.includes(ref)) refs.push(ref);
  }
  // Remove source tags from body text
  let body = text.replace(sourceRegex, "").replace(/\n{3,}/g, "\n\n").trim();
  return { body, refs };
}

function renderMarkdown(text) {
  // Basic markdown → HTML
  let html = text
    // Bold
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    // Italic
    .replace(/\*(.+?)\*/g, "<em>$1</em>")
    // Inline code
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    // LaTeX display math \[...\] or $$...$$
    .replace(/\\\[([\s\S]*?)\\\]/g, '<div class="math-block">$1</div>')
    .replace(/\$\$([\s\S]*?)\$\$/g, '<div class="math-block">$1</div>')
    // LaTeX inline math \(...\) or $...$
    .replace(/\\\(([\s\S]*?)\\\)/g, '<span class="math-inline">$1</span>')
    .replace(/\$([^$\n]+?)\$/g, '<span class="math-inline">$1</span>')
    // Bullet lists
    .replace(/^- (.+)$/gm, '<li>$1</li>')
    // Headers
    .replace(/^### (.+)$/gm, '<h4>$1</h4>')
    .replace(/^## (.+)$/gm, '<h3>$1</h3>')
    // Line breaks → paragraphs
    .replace(/\n\n/g, '</p><p>')
    .replace(/\n/g, '<br/>');

  // Wrap <li> runs in <ul>
  html = html.replace(/((?:<li>.*?<\/li>\s*)+)/g, '<ul>$1</ul>');

  return '<p>' + html + '</p>';
}

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

function MessageBubble({ content }) {
  const { body, refs } = parseMessage(content);
  const html = renderMarkdown(body);

  return (
    <>
      <div className="bubble" dangerouslySetInnerHTML={{ __html: html }} />
      {refs.length > 0 && (
        <div className="refs">
          {refs.slice(0, 5).map((r, i) => (
            <span className="ref-chip mono" key={i} title={r}>
              {r.length > 40 ? r.slice(0, 37) + "…" : r}
            </span>
          ))}
          {refs.length > 5 && <span className="ref-chip mono">+{refs.length - 5} more</span>}
        </div>
      )}
    </>
  );
}

function Assistant({ mode, persona = "Dr. Marginalia", activeSources, streaming, streamProgress, activeCourse, onGenerateNotes, onGenerateQuiz, onGenerateMindmap, checkedFiles }) {
  const [text, setText] = useStateA("");
  const [messages, setMessages] = useStateA([]);
  const [thinking, setThinking] = useStateA(false);
  const [thinkStep, setThinkStep] = useStateA(0);
  const bodyRef = useRefA(null);

  useEffectA(() => {
    if (bodyRef.current) bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
  }, [messages, thinking, streamProgress]);

  const personaMeta = {
    "Dr. Marginalia": { desc: "Professor · formal · rigorous", av: "M" },
    "Wren": { desc: "Peer tutor · patient · plain-spoken", av: "W" },
    "Socrates": { desc: "Asks you questions back", av: "S" },
  }[persona] || { desc: "", av: "M" };

  const thinkingSteps = [
    "Searching knowledge base",
    "Retrieving relevant passages",
    "Cross-referencing sources",
    "Generating answer",
    "Formatting response",
  ];

  // Suggestions: { label, action? } — action triggers a function directly instead of chat
  const suggestions = {
    reader: [
      { label: "Summarize this course" },
      { label: "What are the key concepts?" },
      { label: "List all definitions" },
      { label: "Generate study notes", action: onGenerateNotes },
      { label: "Generate quiz", action: onGenerateQuiz },
      { label: "Build knowledge graph", action: onGenerateMindmap },
    ],
    notes: [
      { label: "Rewrite shorter" },
      { label: "Add worked examples" },
      { label: "Generate quiz from notes", action: onGenerateQuiz },
    ],
    mindmap: [
      { label: "What is this concept?" },
      { label: "Find prerequisites" },
      { label: "Explain relationships" },
    ],
    quiz: [
      { label: "Generate new quiz", action: onGenerateQuiz },
      { label: "Focus on weak areas" },
      { label: "Make it harder" },
      { label: "Explain the answers" },
    ],
    processing: [],
  };

  // Intent detection — route certain messages to actions instead of chat
  const ACTION_PATTERNS = [
    { patterns: ["生成思维导图", "思维导图", "知识图谱", "mind map", "mindmap", "knowledge graph", "build graph"],
      action: () => onGenerateMindmap && onGenerateMindmap(), label: "Building knowledge graph..." },
    { patterns: ["生成笔记", "学习笔记", "generate notes", "study notes", "make notes"],
      action: () => onGenerateNotes && onGenerateNotes(), label: "Generating study notes..." },
    { patterns: ["生成测试", "出题", "练习题", "generate quiz", "practice quiz", "make quiz", "测试题"],
      action: () => onGenerateQuiz && onGenerateQuiz(), label: "Generating practice quiz..." },
  ];

  function detectAction(msg) {
    const lower = msg.toLowerCase();
    for (const { patterns, action, label } of ACTION_PATTERNS) {
      if (patterns.some(p => lower.includes(p))) {
        return { action, label };
      }
    }
    return null;
  }

  async function handleSend() {
    const msg = text.trim();
    if (!msg || thinking) return;
    setText("");

    setMessages(prev => [...prev, { role: "user", content: msg, time: "just now" }]);

    // Check for action intent first
    const detected = detectAction(msg);
    if (detected) {
      detected.action();
      setMessages(prev => [...prev, {
        role: "ai", content: detected.label + " Check the corresponding tab for results.", time: "just now",
      }]);
      return;
    }

    // Normal chat flow
    setThinking(true);
    setThinkStep(0);
    const iv = setInterval(() => {
      setThinkStep(prev => Math.min(prev + 1, thinkingSteps.length - 1));
    }, 700);

    try {
      const files = (checkedFiles && checkedFiles.length > 0) ? checkedFiles : null;
      const data = await API.chat(msg, activeCourse, 5, files);
      clearInterval(iv);
      setThinking(false);

      setMessages(prev => [...prev, {
        role: "ai",
        content: data.answer || "I couldn't find a relevant answer.",
        time: "just now",
      }]);
    } catch (e) {
      clearInterval(iv);
      setThinking(false);
      setMessages(prev => [...prev, {
        role: "ai",
        content: "Error: " + (e.message || "Failed to connect to backend"),
        time: "error",
      }]);
    }
  }

  function handleKeyDown(e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  }

  return (
    <aside className="assistant" data-screen-label="Assistant">
      <div className="asst-header">
        <div className="asst-title">
          <div className="asst-avatar">{personaMeta.av}</div>
          <div>
            <div>{persona}</div>
            <div style={{ fontFamily: "var(--mono)", fontSize: 9, color: "var(--ink-4)", textTransform: "uppercase", letterSpacing: "0.08em" }}>{personaMeta.desc}</div>
          </div>
        </div>
        <div className="asst-status">
          <span className="pulse"></span>
          {thinking ? "Thinking" : streaming ? "Drafting" : "Ready"}
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
        {messages.length === 0 && !thinking && (
          <div className="msg ai">
            <div className="who">{persona} · welcome</div>
            <div className="bubble">
              {activeCourse
                ? <>Ready to help with <b>{activeCourse}</b>. Ask questions or click a suggestion below.</>
                : <>Welcome! Select a course from the top bar, then ask me anything.</>
              }
            </div>
          </div>
        )}

        {messages.map((m, i) => (
          <div className={`msg ${m.role === "user" ? "user" : "ai"}`} key={i}>
            <div className="who">{m.role === "user" ? "You" : persona} · {m.time}</div>
            {m.role === "user"
              ? <div className="bubble">{m.content}</div>
              : <MessageBubble content={m.content} />
            }
          </div>
        ))}

        {thinking && <ThinkingSteps steps={thinkingSteps} activeIndex={thinkStep} />}

        {streaming && !thinking && (
          <div className="msg ai">
            <div className="who">{persona} · drafting…</div>
            <div className="bubble">Generating<span className="stream-cursor"></span></div>
          </div>
        )}
      </div>

      <div className="asst-suggest">
        {(suggestions[mode] || []).map(s => (
          <span
            className="suggest-chip mono"
            key={s.label}
            onClick={() => {
              if (s.action) {
                s.action();
                setMessages(prev => [...prev,
                  { role: "user", content: s.label, time: "just now" },
                  { role: "ai", content: `Generating... Check the corresponding tab for results.`, time: "just now" },
                ]);
              } else {
                setText(s.label);
              }
            }}
          >{s.label}</span>
        ))}
      </div>

      <div className="asst-input">
        <textarea
          placeholder={`Ask ${persona.split(" ")[0]} about your sources…`}
          value={text}
          onChange={e => setText(e.target.value)}
          onKeyDown={handleKeyDown}
          rows={2}
        ></textarea>
        <button className="send" onClick={handleSend} disabled={thinking}>↑</button>
      </div>
    </aside>
  );
}

Object.assign(window, { Assistant });
