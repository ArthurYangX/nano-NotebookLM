/* Shared frontend state helpers.
 * Plain JavaScript on purpose: usable in the CDN app and in Node-based tests.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.StudyState = factory();
})(typeof window !== "undefined" ? window : globalThis, function () {
  const PREFIX = "nano-nlm:v1";

  function createMemoryStorage(seed) {
    const data = Object.assign({}, seed || {});
    return {
      getItem: key => Object.prototype.hasOwnProperty.call(data, key) ? data[key] : null,
      setItem: (key, value) => { data[key] = String(value); },
      removeItem: key => { delete data[key]; },
      dump: () => Object.assign({}, data),
    };
  }

  function key(courseId, kind) {
    return `${PREFIX}:${courseId || "_all_"}:${kind}`;
  }

  function safeJsonParse(raw, fallback) {
    try { return raw ? JSON.parse(raw) : fallback; } catch { return fallback; }
  }

  function createSkillEntries(api, courseId) {
    const wrap = (id, label, fn, render) => ({
      id,
      label,
      async run() {
        try {
          const data = await fn(courseId);
          return { status: "ok", data, text: render(data) };
        } catch (err) {
          return { status: "error", data: null, text: `${label} failed: ${err.message || err}` };
        }
      },
    });
    return [
      wrap("exam-analysis", "Exam analysis", api.analyzeExam, renderExamAnalysis),
      wrap("report", "Report", api.generateReport, renderReport),
      wrap("mastery", "Mastery", api.getMastery, data => formatMasteryState(data).text),
    ];
  }

  function renderExamAnalysis(data) {
    const parts = [];
    if (Array.isArray(data.patterns)) parts.push(`Patterns: ${data.patterns.join(", ")}`);
    if (Array.isArray(data.recommendations)) parts.push(`Recommendations: ${data.recommendations.join(", ")}`);
    if (data.content) parts.push(String(data.content));
    return parts.join("\n") || "No exam patterns found.";
  }

  function renderReport(data) {
    return String(data.content || data.report || data.summary || "No report content.");
  }

  function getCheckedSourceFiles(sources) {
    // Returns the raw filenames (matching chunk.source_file) for all checked
    // sources. Frontend may decorate the visible title with a "[course] "
    // prefix in All Courses mode — we must strip that here so the backend
    // qa_skill filter (`r.source_file in checked_files`) actually matches.
    return (sources || [])
      .filter(s => s && s.checked !== false)
      .map(s => {
        if (!s) return "";
        if (typeof s.sourceFile === "string" && s.sourceFile) return s.sourceFile;
        const title = String(s.title || "");
        // Strip a single leading "[…] " bracketed prefix, but only one — keeps
        // legitimate bracketed filenames intact.
        const stripped = title.replace(/^\[[^\]]*\]\s+/, "");
        return stripped;
      })
      .filter(Boolean);
  }

  function resolveCitationNavigation(refText, sources) {
    const raw = String(refText || "").replace(/^\[Source:\s*/i, "").replace(/\]$/, "").trim();
    const chunkMatch = raw.match(/\bchunk\s+([A-Za-z0-9_.:-]+)/i) || raw.match(/\b(c[0-9A-Za-z_.:-]+)\b/);
    const pageMatch = raw.match(/\bp(?:age|\.)?\s*([0-9]+)/i) || raw.match(/PDF\s*p\.?\s*([0-9]+)/i);
    const page = pageMatch ? Number(pageMatch[1]) : null;
    const sourcePart = raw.split(",")[0].replace(/^Source:\s*/i, "").trim();
    const source = (sources || []).find(s => {
      const title = String(s.title || "");
      return title === sourcePart || title.includes(sourcePart) || sourcePart.includes(title);
    });
    if (!source) {
      return { ok: false, message: `Source not found: ${sourcePart || raw}` };
    }
    return {
      ok: true,
      activeId: source.id,
      page,
      highlightedId: chunkMatch ? chunkMatch[1] : `${source.id}:${page || 1}`,
      message: "",
    };
  }

  function prepareMindmap(kg, options) {
    const opts = options || {};
    const nodesIn = Array.isArray(kg && kg.nodes) ? kg.nodes : flattenTree(kg);
    const edgesIn = Array.isArray(kg && kg.edges) ? kg.edges : treeEdges(kg);
    if (!nodesIn.length) {
      return { empty: true, placeholder: "No concepts extracted yet.", nodes: [], edges: [] };
    }

    const maxWeight = Math.max(...nodesIn.map(n => Number(n.weight || 1)), 1);
    const relationStyle = {
      "is-a": { dash: "", arrow: true },
      "part-of": { dash: "4 3", arrow: true },
      "depends-on": { dash: "", arrow: true },
      "example-of": { dash: "2 4", arrow: true },
      "related": { dash: "1 5", arrow: false },
      "related_to": { dash: "1 5", arrow: false },
    };
    const parentByTarget = {};
    edgesIn.forEach(edge => {
      const source = String(edge.source || edge.from || "");
      const target = String(edge.target || edge.to || "");
      if (source && target && !parentByTarget[target]) parentByTarget[target] = source;
    });
    const nodes = nodesIn.map((node, idx) => {
      const weight = Number(node.weight || 1);
      const depth = Number(node.depth || 0);
      const angle = (idx / Math.max(nodesIn.length, 1)) * Math.PI * 2 - Math.PI / 2;
      const radius = opts.layout === "tree" ? depth * 220 : Math.max(0, depth) * 180 + (depth ? 120 : 0);
      return {
        id: String(node.id || node.concept_id || node.name || idx),
        label: String(node.name || node.label || node.id || `Node ${idx}`),
        kind: depth === 0 ? "root" : depth === 1 ? "branch" : "leaf",
        parent: parentByTarget[String(node.id || node.concept_id || node.name || idx)] || null,
        depth,
        weight,
        source_chunks: node.source_chunks || node.chunk_ids || [],
        definition: node.definition || "",
        x: opts.layout === "tree" ? depth * 240 : Math.cos(angle) * radius,
        y: opts.layout === "tree" ? idx * 54 : Math.sin(angle) * radius,
        style: {
          fontSize: 11 + Math.round((weight / maxWeight) * 8),
          saturation: Math.min(1, 0.25 + weight / maxWeight),
        },
      };
    });
    const nodeIds = new Set(nodes.map(n => n.id));
    const edges = edgesIn
      .map(edge => ({
        source: String(edge.source || edge.from),
        target: String(edge.target || edge.to),
        relation: edge.relation || edge.relation_type || "related",
      }))
      .filter(edge => nodeIds.has(edge.source) && nodeIds.has(edge.target))
      .map(edge => Object.assign(edge, { style: relationStyle[edge.relation] || relationStyle.related }));
    return { empty: false, nodes, edges };
  }

  function flattenTree(root) {
    if (!root || !root.id) return [];
    const out = [];
    function walk(node, depth) {
      out.push({
        id: node.id,
        name: node.label || node.name,
        depth,
        weight: node.weight || Math.max(1, 5 - depth),
        source_chunks: node.source_chunks || [],
        definition: node.definition || "",
      });
      (node.children || []).forEach(child => walk(child, depth + 1));
    }
    walk(root, 0);
    return out;
  }

  function treeEdges(root) {
    const out = [];
    function walk(node) {
      (node.children || []).forEach(child => {
        out.push({ source: node.id, target: child.id, relation: child.relation || "related" });
        walk(child);
      });
    }
    if (root) walk(root);
    return out;
  }

  function getMindmapNodeDetail(layout, nodeId) {
    return (layout.nodes || []).find(n => n.id === nodeId) || null;
  }

  function saveNoteDraft(storage, courseId, content) {
    storage.setItem(key(courseId, "notes:draft"), String(content || ""));
  }

  function loadNoteDraft(storage, courseId) {
    return storage.getItem(key(courseId, "notes:draft")) || "";
  }

  function buildMarkdownExport(courseId, content) {
    const safeCourse = String(courseId || "course").replace(/[^\w.-]+/g, "-");
    return {
      filename: `${safeCourse}-notes.md`,
      mime: "text/markdown;charset=utf-8",
      content: String(content || ""),
    };
  }

  function buildPdfPrintHtml(courseId, content) {
    const escaped = String(content || "").replace(/[&<>]/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[ch]));
    return `<!doctype html><title>${courseId} notes</title><pre>${escaped}</pre>`;
  }

  function quizSignature(quiz) {
    return JSON.stringify((quiz || []).map(q => ({
      q: q.question || q.prompt || "",
      a: q.correct || q.answer || "",
      o: q.options || [],
    })));
  }

  function saveQuizAnswers(storage, courseId, quiz, answers) {
    storage.setItem(key(courseId, "quiz:answers"), JSON.stringify({
      signature: quizSignature(quiz),
      answers: answers || {},
    }));
  }

  function loadQuizAnswers(storage, courseId, quiz) {
    const raw = safeJsonParse(storage.getItem(key(courseId, "quiz:answers")), null);
    if (!raw) return { answers: {}, stale: false, message: "" };
    const stale = raw.signature !== quizSignature(quiz);
    return {
      answers: stale ? {} : (raw.answers || {}),
      stale,
      message: stale ? "Saved answers are stale because the question set changed." : "",
    };
  }

  function normalizeCorrect(q) {
    return q.correct || q.answer || q.correct_answer || "";
  }

  function filterWrongQuestions(quiz, answers) {
    return (quiz || []).filter((q, idx) => {
      const actual = answers[String(idx)] ?? answers[idx];
      if (actual == null || actual === "") return false;
      return String(actual).trim() !== String(normalizeCorrect(q)).trim();
    });
  }

  async function generateWeakAreaQuiz(api, courseId, weakArea) {
    return api.generateQuiz(courseId, weakArea.concept || weakArea.topic || weakArea.name);
  }

  function formatMasteryState(data) {
    const weak = (data && data.weak_areas) || [];
    if (!weak.length) return { empty: true, text: "No weak areas below 0.5 yet." };
    return {
      empty: false,
      weak_areas: weak,
      text: weak.map(w => `${w.concept}: ${Math.round(Number(w.score || 0) * 100)}%`).join("\n"),
    };
  }

  function createGenerationState() {
    return { status: "idle", partial: "", failures: 0, errorDetail: "", retryable: false };
  }

  function recordPartialGeneration(state, chunk) {
    return Object.assign({}, state, { status: "streaming", partial: (state.partial || "") + chunk });
  }

  function recordGenerationFailure(state, err, count) {
    const failures = count || (Number(state.failures || 0) + 1);
    return Object.assign({}, state, {
      status: failures >= 3 ? "failed" : "error",
      failures,
      retryable: failures < 3,
      errorDetail: String((err && err.message) || err || "generation failed"),
    });
  }

  function retryGeneration(state) {
    return Object.assign({}, state, { status: "retrying", retryable: false });
  }

  function formatStatusBar(status) {
    if (!status || !Array.isArray(status.backends) || status.backends.length === 0) {
      return { degraded: true, ok: false, text: "Backend degraded · no active backend" };
    }
    const lat = status.latency_ms || {};
    const usage = status.usage || {};
    const cost = usage.total_cost_usd ?? usage.total_cost ?? 0;
    const text = [
      `Backend ${status.backends.join(", ")}`,
      `search ${lat.search_p50 ?? "?"}ms`,
      `chat ${lat.chat_p50 ?? "?"}ms`,
      `cost $${Number(cost || 0).toFixed(3)}`,
    ].join(" · ");
    return { degraded: false, ok: true, text };
  }

  return {
    createMemoryStorage,
    createSkillEntries,
    getCheckedSourceFiles,
    resolveCitationNavigation,
    prepareMindmap,
    getMindmapNodeDetail,
    saveNoteDraft,
    loadNoteDraft,
    buildMarkdownExport,
    buildPdfPrintHtml,
    saveQuizAnswers,
    loadQuizAnswers,
    filterWrongQuestions,
    generateWeakAreaQuiz,
    formatMasteryState,
    createGenerationState,
    recordPartialGeneration,
    recordGenerationFailure,
    retryGeneration,
    formatStatusBar,
  };
});
