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

  // M2 (2026-05-06): parent-aware recursive radial layout. Pre-M2 layout
  // placed siblings on a uniform circle by array index, so children of one
  // topic scattered across the canvas. The new algorithm:
  //   - finds the explicit depth=0 root from M1 (or falls back to first
  //     node of legacy KG payloads)
  //   - assigns each topic an angular slice ∝ its subtree leaf count
  //   - places each child at the bisector of its sub-slice, radius =
  //     depth × RADIUS_PER_DEPTH
  //   - assigns one HSL hue per depth=1 topic, inherited by descendants,
  //     so the color tells the student which topic a leaf belongs to
  function prepareMindmapTree(kg, options) {
    const opts = options || {};
    const nodesIn = Array.isArray(kg && kg.nodes) ? kg.nodes : flattenTree(kg);
    const edgesIn = Array.isArray(kg && kg.edges) ? kg.edges : treeEdges(kg);
    if (!nodesIn.length) {
      return { empty: true, placeholder: "No concepts extracted yet.", nodes: [], edges: [] };
    }

    const RADIUS_PER_DEPTH = opts.radiusPerDepth || 220;
    const relationStyle = {
      "is-a": { dash: "", arrow: true },
      "part-of": { dash: "4 3", arrow: true },
      "depends-on": { dash: "", arrow: true },
      "example-of": { dash: "2 4", arrow: true },
      "related": { dash: "1 5", arrow: false },
      "related_to": { dash: "1 5", arrow: false },
    };

    // Build child→parent and parent→children maps. M1 emits part-of edges
    // pointing child→parent (source=child, target=parent). Legacy edges
    // and the frontend MINDMAP fallback use the source=parent convention,
    // so any non-part-of edge is treated as source→target = parent→child.
    const idOf = (node, idx) => String(node.id || node.concept_id || node.name || idx);
    const childrenOf = new Map();
    const parentOf = new Map();
    edgesIn.forEach(edge => {
      const src = String(edge.source || edge.from || "");
      const tgt = String(edge.target || edge.to || "");
      const rel = String(edge.relation || edge.relation_type || "").replace(/_/g, "-");
      if (!src || !tgt) return;
      if (rel === "part-of") {
        if (!childrenOf.has(tgt)) childrenOf.set(tgt, []);
        childrenOf.get(tgt).push(src);
        if (!parentOf.has(src)) parentOf.set(src, tgt);
      } else {
        if (!childrenOf.has(src)) childrenOf.set(src, []);
        childrenOf.get(src).push(tgt);
        if (!parentOf.has(tgt)) parentOf.set(tgt, src);
      }
    });

    // Find the root: explicit depth=0 from M1, else first node.
    let rootId = null;
    for (const n of nodesIn) {
      if (Number(n.depth) === 0) { rootId = idOf(n, 0); break; }
    }
    if (!rootId) rootId = idOf(nodesIn[0], 0);

    // Memoized leaf count per subtree (used to size each node's slice).
    const leafCache = new Map();
    function leafCount(id, ancestors) {
      const key = id;
      if (leafCache.has(key)) return leafCache.get(key);
      const seen = new Set(ancestors || []);
      if (seen.has(id)) return 1;
      seen.add(id);
      const kids = (childrenOf.get(id) || []).filter(c => !seen.has(c));
      const n = kids.length === 0 ? 1 : kids.reduce((s, c) => s + leafCount(c, seen), 0);
      leafCache.set(key, n);
      return n;
    }

    // Hues for depth=1 topics — evenly distributed around 360°.
    const topicChildren = childrenOf.get(rootId) || [];
    const hues = new Map();
    topicChildren.forEach((tid, i) => {
      hues.set(tid, Math.round((i / Math.max(topicChildren.length, 1)) * 360));
    });

    // Position assignment via recursive sub-wedge division.
    const positions = new Map();
    function place(id, depth, angleStart, angleEnd, hue, ancestors) {
      const seen = new Set(ancestors || []);
      if (seen.has(id)) return;
      seen.add(id);
      const angle = (angleStart + angleEnd) / 2;
      const r = depth * RADIUS_PER_DEPTH;
      positions.set(id, {
        x: depth === 0 ? 0 : Math.cos(angle) * r,
        y: depth === 0 ? 0 : Math.sin(angle) * r,
        depth,
        angle,
      });
      if (hue !== null && hue !== undefined && !hues.has(id)) hues.set(id, hue);

      const kids = (childrenOf.get(id) || []).filter(c => !seen.has(c));
      if (!kids.length) return;
      const totalLeaves = kids.reduce((s, c) => s + leafCount(c, seen), 0) || 1;

      // Root: children own the full circle. Below root: children stay
      // inside parent's slice (slightly inset so neighbors don't touch).
      let s, e;
      if (depth === 0) {
        s = -Math.PI / 2;
        e = s + Math.PI * 2;
      } else {
        const parentSlice = angleEnd - angleStart;
        const wedge = Math.min(parentSlice * 0.95, Math.PI * 0.6);
        const c = (angleStart + angleEnd) / 2;
        s = c - wedge / 2;
        e = c + wedge / 2;
      }

      let cursor = s;
      for (const k of kids) {
        const slice = (leafCount(k, seen) / totalLeaves) * (e - s);
        const childHue = hues.has(k) ? hues.get(k) : (hue === undefined ? null : hue);
        place(k, depth + 1, cursor, cursor + slice, childHue, seen);
        cursor += slice;
      }
    }
    place(rootId, 0, -Math.PI / 2, -Math.PI / 2 + Math.PI * 2, null, new Set());

    const maxWeight = Math.max(...nodesIn.map(n => Number(n.weight || 1)), 1);

    const nodes = nodesIn.map((node, idx) => {
      const id = idOf(node, idx);
      const pos = positions.get(id) || { x: 0, y: 0, depth: Number(node.depth || 1) };
      const weight = Number(node.weight || 1);
      const conceptType = String(
        node.concept_type ||
        (pos.depth === 0 ? "root" : pos.depth === 1 ? "topic" : "leaf")
      );
      const kind =
        conceptType === "root" || pos.depth === 0 ? "root" :
        conceptType === "topic" || pos.depth === 1 ? "branch" :
        "leaf";
      const hue = hues.has(id) ? hues.get(id) : null;
      // R3-3: pass through learning_order so MindMap can render a topic
      // badge ("1 / 2 / 3 ..."). Coerce non-int values to null so the
      // renderer can short-circuit on `n.learning_order != null`.
      const rawOrder = node.learning_order;
      const learningOrder = (typeof rawOrder === "number" && Number.isFinite(rawOrder))
        ? Math.trunc(rawOrder)
        : (typeof rawOrder === "string" && /^\d+$/.test(rawOrder))
          ? parseInt(rawOrder, 10)
          : null;
      return {
        id,
        label: String(node.name || node.label || node.id || `Node ${idx}`),
        kind,
        parent: parentOf.get(id) || null,
        depth: pos.depth,
        weight,
        concept_type: conceptType,
        source_chunks: node.source_chunks || node.chunk_ids || [],
        definition: node.definition || "",
        learning_order: learningOrder,
        x: pos.x,
        y: pos.y,
        style: {
          fontSize: 11 + Math.round((weight / maxWeight) * 9),
          saturation: Math.min(1, 0.25 + weight / maxWeight),
          hue,
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
    return { empty: false, nodes, edges, rootId };
  }

  // Back-compat alias for M1/M2/M3 tests and any older component code.
  function prepareMindmap(kg, options) {
    return prepareMindmapTree(kg, options);
  }

  // R4-3: node-link KG layout input for a force-directed view. This helper
  // intentionally does not run the force simulation; it returns a stable
  // node/link shape with deterministic initial positions so React can hand it
  // to d3.forceSimulation asynchronously without blocking large graphs.
  function prepareMindmapForce(kg, options) {
    const rawEdges = Array.isArray(kg && kg.edges) ? kg.edges : treeEdges(kg);
    function normalizeRelation(rel) {
      const value = String(rel || "related").trim().replace(/_/g, "-") || "related";
      return value;
    }
    const hierarchySeed = Object.assign({}, kg || {}, {
      edges: rawEdges.filter(edge => normalizeRelation(edge.relation || edge.relation_type) === "part-of"),
    });
    const tree = prepareMindmapTree(hierarchySeed, options);
    if (tree.empty) {
      // R4-3 fix-all v1 #A11: keep `links` AND `edges` present on every
      // return path so callers iterating `prepared.edges` don't crash
      // with `cannot read length of undefined` on first-run empty KGs
      // (the R4-1 upload-only default state for fresh users).
      return Object.assign({}, tree, { links: [], edges: [], relationTypes: [] });
    }
    const nodeIds = new Set((tree.nodes || []).map(n => n.id));
    const relationTypes = [];
    const seenRelations = new Set();
    const partOfParent = new Map();
    const links = rawEdges.map((edge, idx) => {
      const source = String(edge.source || edge.from || "");
      const target = String(edge.target || edge.to || "");
      const relation = normalizeRelation(edge.relation || edge.relation_type);
      if (!source || !target || !nodeIds.has(source) || !nodeIds.has(target)) return null;
      if (relation === "part-of" && !partOfParent.has(source)) {
        partOfParent.set(source, target);
      }
      if (!seenRelations.has(relation)) {
        seenRelations.add(relation);
        relationTypes.push(relation);
      }
      return {
        id: `${source}->${target}:${relation}:${idx}`,
        source,
        target,
        from: source,
        to: target,
        relation,
        style: edge.style || {},
      };
    }).filter(Boolean);
    const nodes = (tree.nodes || []).map((node, idx) => {
      const jitter = idx === 0 ? 0 : ((idx * 37) % 17) - 8;
      return Object.assign({}, node, {
        parent: partOfParent.get(node.id) || null,
        x: Number.isFinite(Number(node.x)) ? Number(node.x) + jitter : jitter,
        y: Number.isFinite(Number(node.y)) ? Number(node.y) - jitter : -jitter,
        vx: 0,
        vy: 0,
      });
    });
    return {
      empty: false,
      nodes,
      links,
      edges: links,
      rootId: tree.rootId,
      relationTypes,
    };
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

  // M3 (2026-05-06): client-side overlay of student edits. Mirrors the
  // backend `apply_edit_ops` semantics so we can optimistically update
  // the UI without waiting for the POST round-trip. The persisted KG on
  // the server is the source of truth — this helper is only for the
  // optimistic-render fast path.
  const _ALLOWED_RELATIONS = new Set(["is-a", "part-of", "depends-on", "example-of", "related"]);

  function newMindmapNodeId() {
    return "user_" + Date.now().toString(36) + "_" + Math.random().toString(36).slice(2, 8);
  }

  function applyMindmapOps(kg, ops) {
    const nodes = new Map();
    for (const n of (kg && kg.nodes) || []) {
      const id = String(n.id || n.concept_id || "");
      if (id) nodes.set(id, Object.assign({}, n, { id }));
    }
    let edges = ((kg && kg.edges) || []).map(e => Object.assign({}, e));
    const deleted = new Set();
    function edgeKey(e) {
      return [String(e.source || e.from || ""), String(e.target || e.to || ""),
              String(e.relation || e.relation_type || "")].join("|");
    }
    for (const op of ops || []) {
      if (!op || typeof op !== "object") continue;
      const kind = op.op;
      if (kind === "add_node") {
        const id = String(op.id || "").trim();
        if (!id) continue;
        const existing = nodes.get(id) || {};
        nodes.set(id, Object.assign({}, existing, {
          id,
          name: String(op.label || existing.name || id).trim(),
          definition: op.definition != null ? String(op.definition) : (existing.definition || ""),
          depth: existing.depth != null ? existing.depth : 2,
          concept_type: existing.concept_type || "user_added",
          weight: existing.weight != null ? existing.weight : 2,
          user_added: true,
        }));
        const parentId = String(op.parent_id || "").trim();
        if (parentId) {
          const candidate = { source: id, target: parentId, relation: "part-of", user_added: true };
          if (!edges.some(e => edgeKey(e) === edgeKey(candidate))) edges.push(candidate);
        }
      } else if (kind === "update_node") {
        const id = String(op.id || "").trim();
        if (!id || !nodes.has(id)) continue;
        const existing = nodes.get(id);
        const patch = {};
        if (op.label != null) patch.name = String(op.label).trim() || existing.name || id;
        if (op.definition != null) patch.definition = String(op.definition);
        if (Object.keys(patch).length) {
          nodes.set(id, Object.assign({}, existing, patch, { user_edited: true }));
        }
      } else if (kind === "delete_node") {
        const id = String(op.id || "").trim();
        if (id) deleted.add(id);
      } else if (kind === "add_edge") {
        const src = String(op.source || "").trim();
        const tgt = String(op.target || "").trim();
        let rel = String(op.relation || "related").trim().replace(/_/g, "-");
        if (!_ALLOWED_RELATIONS.has(rel)) rel = "related";
        if (!src || !tgt) continue;
        const candidate = { source: src, target: tgt, relation: rel, user_added: true };
        if (!edges.some(e => edgeKey(e) === edgeKey(candidate))) edges.push(candidate);
      } else if (kind === "delete_edge") {
        const src = String(op.source || "").trim();
        const tgt = String(op.target || "").trim();
        const rel = op.relation;
        edges = edges.filter(e => !(
          String(e.source || "") === src && String(e.target || "") === tgt
          && (rel == null || String(e.relation || e.relation_type || "") === rel)
        ));
      }
      // Unknown ops silently skipped — server logs them.
    }
    if (deleted.size) {
      for (const id of deleted) nodes.delete(id);
      edges = edges.filter(e => !deleted.has(String(e.source || "")) && !deleted.has(String(e.target || "")));
    }
    return { nodes: Array.from(nodes.values()), edges };
  }

  // R3-3: stream agent events for one mindmap node ("explain this topic").
  // Hits POST /api/mindmap/{courseId}/explain-node and parses the NDJSON
  // body line-by-line, calling onEvent for each parsed object. Returns a
  // Promise that resolves when the stream ends (or rejects on transport
  // failure).
  // fix-all v3 #H11: optional `options.signal` (AbortController.signal)
  // lets callers cancel the request when the panel is closed; the previous
  // version had no abort handle so closing the panel left the
  // StreamingResponse running on the server.
  function requestNodeDeepDive(courseId, nodeId, onEvent, fetchImpl, options) {
    const opts = options || {};
    const f = fetchImpl || (typeof fetch !== "undefined" ? fetch : null);
    if (!f) return Promise.reject(new Error("fetch not available"));
    const init = {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ node_id: nodeId }),
    };
    if (opts.signal) init.signal = opts.signal;
    return f(`/api/mindmap/${encodeURIComponent(courseId)}/explain-node`, init).then(resp => {
      if (!resp.ok) {
        return resp.text().then(t => {
          throw new Error(`explain-node ${resp.status}: ${t.slice(0, 200)}`);
        });
      }
      const reader = resp.body && resp.body.getReader && resp.body.getReader();
      if (!reader) throw new Error("explain-node: streaming body not supported");
      const decoder = new TextDecoder();
      // fix-all v4 #A5: hard cap so a missing newline can't OOM the
      // browser tab (mirrors the cap in frontend/api.js#_stream — the
      // sister parser already had this guard but this one didn't).
      const MAX_LINE_BYTES = 1024 * 1024;
      let buf = "";
      function pump() {
        return reader.read().then(({ value, done }) => {
          if (done) {
            const tail = buf.trim();
            if (tail) {
              try { onEvent && onEvent(JSON.parse(tail)); } catch (e) { /* ignore */ }
            }
            return;
          }
          buf += decoder.decode(value, { stream: true });
          if (buf.length > MAX_LINE_BYTES) {
            if (typeof console !== "undefined") {
              console.warn("explain-node NDJSON buffer over " + MAX_LINE_BYTES + "B without newline; dropping");
            }
            buf = "";
          }
          let nl;
          while ((nl = buf.indexOf("\n")) !== -1) {
            const line = buf.slice(0, nl).trim();
            buf = buf.slice(nl + 1);
            if (!line) continue;
            try { onEvent && onEvent(JSON.parse(line)); }
            catch (e) { /* malformed line — skip rather than abort */ }
          }
          return pump();
        });
      }
      return pump();
    });
  }

  // LaTeX-refactor: notes now persist as LaTeX source under a new storage
  // key. The old `notes:draft` key holds markdown — rendering it through
  // latex-to-html would show literal `##` / `**`, so we silently discard
  // on first read. A one-time console.info is emitted (per-course) for
  // debuggability. The project is pre-user (CLAUDE.md) so this breaking
  // change carries no real cost.
  //
  // review-swarm fix-all v1 #11: the flag was global so only the first
  // migrated course logged. Per-course flag means each course's discard
  // surfaces in console exactly once.
  const NOTES_DRAFT_KIND = "notes-latex:draft";
  const LEGACY_NOTES_DRAFT_KIND = "notes:draft";
  const LEGACY_DISCARD_FLAG_KIND = "notes-migration-logged";

  function _migrateLegacyNoteDraft(storage, courseId) {
    if (!storage) return;
    try {
      const legacy = storage.getItem(key(courseId, LEGACY_NOTES_DRAFT_KIND));
      if (legacy === null || legacy === "") return;
      storage.removeItem(key(courseId, LEGACY_NOTES_DRAFT_KIND));
      const flagKey = key(courseId, LEGACY_DISCARD_FLAG_KIND);
      if (typeof console !== "undefined" && !storage.getItem(flagKey)) {
        console.info(
          "[nano-nlm] discarded legacy markdown note draft (course=%s) — " +
          "the Note format switched to LaTeX.",
          courseId
        );
        try { storage.setItem(flagKey, "1"); } catch (e) {}
      }
    } catch (e) {
      /* legacy migration is best-effort */
    }
  }

  function saveNoteDraft(storage, courseId, content) {
    try {
      storage.setItem(key(courseId, NOTES_DRAFT_KIND), String(content || ""));
    } catch (e) {
      if (typeof console !== "undefined") console.warn("notes draft save failed", e);
    }
  }

  function loadNoteDraft(storage, courseId) {
    _migrateLegacyNoteDraft(storage, courseId);
    return storage.getItem(key(courseId, NOTES_DRAFT_KIND)) || "";
  }

  // Legacy markdown export — retained for backwards-compat with the
  // test suite (tests/test_frontend_helpers.py exercises this helper).
  // The Note path itself uses buildLatexExport now.
  function buildMarkdownExport(courseId, content) {
    const safeCourse = String(courseId || "course").replace(/[^\w.-]+/g, "-");
    return {
      filename: `${safeCourse}-notes.md`,
      mime: "text/markdown;charset=utf-8",
      content: String(content || ""),
    };
  }

  // LaTeX-refactor: download the raw .tex source. Mime type is `text/x-tex`
  // (the most widely recognised LaTeX mime); browsers offer Save As… for it.
  function buildLatexExport(courseId, content) {
    const safeCourse = String(courseId || "course").replace(/[^\w.-]+/g, "-");
    return {
      filename: `${safeCourse}-notes.tex`,
      mime: "text/x-tex;charset=utf-8",
      content: String(content || ""),
    };
  }

  // Legacy raw-text print helper. Retained so anything calling this gets
  // the old behaviour rather than a crash; the Note path now uses
  // buildPrintHtml below which wraps already-rendered HTML.
  function buildPdfPrintHtml(courseId, content) {
    const escaped = String(content || "").replace(/[&<>]/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[ch]));
    return `<!doctype html><title>${courseId} notes</title><pre>${escaped}</pre>`;
  }

  // LaTeX-refactor: print-friendly HTML wrapper. Takes already-rendered
  // HTML (output of NanoLatex.latexToHtml) and emits a self-contained page
  // with KaTeX styles + auto-render so math + theorem boxes survive the
  // browser-print path. Used by the Notes "PDF (print)" button.
  function buildPrintHtml(courseId, renderedHtml) {
    const safeTitle = String(courseId || "course").replace(/[<>]/g, "");
    // KaTeX assets — same versions as index.html (0.16.11).
    return [
      `<!doctype html>`,
      `<html lang="en"><head><meta charset="utf-8"/>`,
      `<title>${safeTitle} notes</title>`,
      `<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css">`,
      `<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js"></script>`,
      `<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/contrib/auto-render.min.js"></script>`,
      `<style>`,
      `  body { font-family: "Source Serif 4", "PingFang SC", "Microsoft YaHei", serif; `,
      `         max-width: 760px; margin: 32px auto; padding: 0 24px; color: #1a1a1a; line-height: 1.7; }`,
      `  h2, h3, h4 { font-weight: 600; margin-top: 1.4em; }`,
      `  .thm-box { border-left: 3px solid #888; background: #f6f6f4; padding: 10px 14px; `,
      `             margin: 10px 0; border-radius: 4px; }`,
      `  .thm-theorem { border-color: #5b8def; background: #eef3fd; }`,
      `  .thm-lemma { border-color: #4a90a4; background: #ecf3f5; }`,
      `  .thm-definition { border-color: #b8860b; background: #fbf3df; }`,
      `  .thm-example { border-color: #5fa052; background: #eef5ec; }`,
      `  .thm-remark { border-color: #a86432; background: #f5ece3; }`,
      `  .thm-proof { border-color: #888; background: #fafafa; }`,
      `  .thm-label { display: inline; font-weight: 600; }`,
      `  .qed { float: right; font-size: 110%; }`,
      `  .ref-chip { display: inline-block; background: #eee; border-radius: 3px; `,
      `              padding: 1px 6px; font-family: ui-monospace, monospace; `,
      `              font-size: 88%; color: #444; border: none; }`,
      `  .latex-unknown { background: #fff7e5; border: 1px solid #f0c873; `,
      `                   padding: 8px; border-radius: 4px; font-family: ui-monospace, monospace; `,
      `                   white-space: pre-wrap; font-size: 90%; }`,
      `  .math-display { margin: 12px 0; }`,
      `  @media print { body { max-width: none; margin: 0; padding: 0 1cm; } }`,
      `</style>`,
      `</head><body>`,
      `<h1>${safeTitle}</h1>`,
      String(renderedHtml || ""),
      `<script>document.addEventListener("DOMContentLoaded", function () {`,
      `  if (typeof renderMathInElement === "function") {`,
      `    renderMathInElement(document.body, {`,
      `      delimiters: [`,
      `        { left: "$$", right: "$$", display: true },`,
      `        { left: "$", right: "$", display: false },`,
      `        { left: "\\\\[", right: "\\\\]", display: true },`,
      `        { left: "\\\\(", right: "\\\\)", display: false },`,
      `      ],`,
      `      throwOnError: false,`,
      `    });`,
      `  }`,
      `});</script>`,
      `</body></html>`,
    ].join("\n");
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

  // Map stable error codes the backend emits in NDJSON `error` events to
  // user-facing text. fix-all v3 #M1 + v4 #A3 deliberately stripped vendor
  // exception strings (URLs / model ids / API-key shapes) and replaced
  // them with stable codes; the UI then has to translate them so users
  // don't see the raw "stream_failed" token.
  const STREAM_ERROR_MESSAGES = {
    stream_failed:        "生成失败，请稍后重试 / Generation failed; please retry.",
    upstream_error:       "上游服务异常 / Upstream service error.",
    endpoint_error:       "服务端错误 / Server endpoint error.",
    agent_error:          "Agent 出错 / Agent error.",
    no_assistant_message: "Agent 未返回任何内容 / Agent returned no message.",
    tool_execution_failed:"工具执行失败 / Tool execution failed.",
    quiz_generation_failed:"题目生成失败 / Quiz generation failed.",
  };

  function formatStreamErrorMessage(code, fallback) {
    if (!code) return String(fallback || "Generation failed");
    const friendly = STREAM_ERROR_MESSAGES[String(code)];
    if (friendly) return friendly;
    return String(fallback || code);
  }

  function recordGenerationFailure(state, err, count) {
    const failures = count || (Number(state.failures || 0) + 1);
    const raw = (err && err.message) || err || "generation failed";
    const friendly = formatStreamErrorMessage(String(raw), raw);
    return Object.assign({}, state, {
      status: failures >= 3 ? "failed" : "error",
      failures,
      retryable: failures < 3,
      errorDetail: String(friendly),
      errorCode: String(raw),
    });
  }

  function retryGeneration(state) {
    return Object.assign({}, state, { status: "retrying", retryable: false });
  }

  // ── Highlights / annotations (per-course) ─────────────────────────────
  // Schema: { id, text, before, after, color, note, created_at }
  //   text  = the exact selected text (used as primary anchor)
  //   before/after = up to 30 chars of context on each side, used for
  //                  disambiguation when the same text appears multiple times
  //   color ∈ {"yellow", "green", "pink"}
  //   note  = optional annotation string (may be "")
  // We anchor by text+context (Hypothes.is style) instead of DOM XPath so the
  // anchor survives re-render of markdownToHtml. We anchor by text instead of
  // raw-markdown offsets so editing the markdown a little doesn't shift every
  // following highlight.
  const HIGHLIGHT_COLORS = ["yellow", "green", "pink"];
  const CONTEXT_LEN = 30;

  function loadHighlights(storage, courseId) {
    if (!courseId) return [];
    const raw = storage.getItem(key(courseId, "notes:highlights"));
    const parsed = safeJsonParse(raw, []);
    if (!Array.isArray(parsed)) return [];
    // Defensive: drop entries with unknown colors (localStorage tampering /
    // future schema additions) so they never reach the className concat.
    return parsed.filter(h => h && typeof h.text === "string" && HIGHLIGHT_COLORS.includes(h.color));
  }

  function saveHighlights(storage, courseId, list) {
    if (!courseId) return;
    try {
      storage.setItem(key(courseId, "notes:highlights"), JSON.stringify(list || []));
    } catch (e) {
      // Safari private mode / quota exhaustion — keep in-memory state usable.
      if (typeof console !== "undefined") console.warn("highlights save failed", e);
    }
  }

  function genHighlightId() {
    return "h_" + Date.now().toString(36) + "_" + Math.random().toString(36).slice(2, 8);
  }

  function buildContextWindows(content, anchorIndex, text) {
    const safe = String(content || "");
    const start = Math.max(0, anchorIndex);
    const end = Math.min(safe.length, anchorIndex + (text || "").length);
    return {
      before: safe.slice(Math.max(0, start - CONTEXT_LEN), start),
      after: safe.slice(end, Math.min(safe.length, end + CONTEXT_LEN)),
    };
  }

  function addHighlight(storage, courseId, payload) {
    if (!courseId) return loadHighlights(storage, courseId);
    const text = String(payload && payload.text || "").trim();
    if (!text) return loadHighlights(storage, courseId); // reject empty selection
    const color = HIGHLIGHT_COLORS.includes(payload && payload.color) ? payload.color : "yellow";
    const list = loadHighlights(storage, courseId);
    const item = {
      id: genHighlightId(),
      text,
      before: String(payload && payload.before || ""),
      after: String(payload && payload.after || ""),
      color,
      note: String(payload && payload.note || ""),
      created_at: Date.now(),
    };
    list.push(item);
    saveHighlights(storage, courseId, list);
    return list;
  }

  function updateHighlight(storage, courseId, id, patch) {
    if (!courseId || !id) return loadHighlights(storage, courseId);
    const list = loadHighlights(storage, courseId).map(h => {
      if (h.id !== id) return h;
      const next = Object.assign({}, h);
      if (patch && typeof patch.note === "string") next.note = patch.note;
      if (patch && HIGHLIGHT_COLORS.includes(patch.color)) next.color = patch.color;
      return next;
    });
    saveHighlights(storage, courseId, list);
    return list;
  }

  function removeHighlight(storage, courseId, id) {
    if (!courseId || !id) return loadHighlights(storage, courseId);
    const list = loadHighlights(storage, courseId).filter(h => h.id !== id);
    saveHighlights(storage, courseId, list);
    return list;
  }

  function locateHighlight(content, hl) {
    // Returns the character index where the highlight should be applied in
    // `content`, disambiguating with before/after context. Returns -1 if the
    // text is no longer present (caller should treat as stale).
    const safe = String(content || "");
    const text = String(hl && hl.text || "");
    if (!text) return -1;
    let cursor = 0;
    let bestIdx = -1;
    let bestScore = -1;
    while (cursor <= safe.length - text.length) {
      const idx = safe.indexOf(text, cursor);
      if (idx < 0) break;
      const beforeWin = safe.slice(Math.max(0, idx - CONTEXT_LEN), idx);
      const afterWin = safe.slice(idx + text.length, idx + text.length + CONTEXT_LEN);
      let score = 0;
      const expectBefore = String(hl.before || "");
      const expectAfter = String(hl.after || "");
      if (expectBefore && beforeWin.endsWith(expectBefore.slice(-Math.min(expectBefore.length, 12)))) score += 2;
      if (expectAfter && afterWin.startsWith(expectAfter.slice(0, Math.min(expectAfter.length, 12)))) score += 2;
      if (!expectBefore && !expectAfter) score = 1;
      if (score > bestScore) { bestScore = score; bestIdx = idx; }
      if (bestScore >= 4) return bestIdx;
      cursor = idx + 1;
    }
    return bestIdx;
  }

  function pruneStaleHighlights(storage, courseId, content) {
    if (!courseId) return { kept: [], removed: [] };
    const list = loadHighlights(storage, courseId);
    const kept = [];
    const removed = [];
    list.forEach(h => {
      if (locateHighlight(content, h) >= 0) kept.push(h);
      else removed.push(h);
    });
    if (removed.length) saveHighlights(storage, courseId, kept);
    return { kept, removed };
  }

  function slugifyHeading(text) {
    // fix-all v3 #L5: extend the kept-character class to cover Japanese
    // hiragana/katakana (U+3040–U+30FF) and Korean Hangul syllables
    // (U+AC00–U+D7A3) in addition to CJK Unified Ideographs. Otherwise a
    // heading like "はじめに" was stripped to empty and the anchor jump
    // silently broke for that section.
    return String(text || "")
      .trim()
      .toLowerCase()
      .replace(/[^\w一-鿿぀-ヿ가-힣\s-]/g, "")
      .replace(/\s+/g, "-")
      .slice(0, 64) || "section";
  }

  // Single source of truth for heading slug ids — used by BOTH
  // extractHeadingTOC and markdownToHtml so the TOC click → DOM lookup is
  // guaranteed to match. Dedupe uses a Set of taken ids (not a counter) so
  // 3+ duplicates produce {a, a-1, a-2, a-3, ...} reliably.
  function slugifyHeadingsList(markdown) {
    const lines = String(markdown || "").split(/\n/);
    const out = [];
    const taken = new Set();
    lines.forEach((line, lineIdx) => {
      const m = line.match(/^(#{1,3})\s+(.+?)\s*$/);
      if (!m) return;
      const level = m[1].length;
      const text = m[2].trim();
      const base = slugifyHeading(text);
      let id = base;
      let n = 1;
      while (taken.has(id)) id = `${base}-${n++}`;
      taken.add(id);
      out.push({ level, text, id, lineIdx });
    });
    return out;
  }

  function extractHeadingTOC(markdown) {
    return slugifyHeadingsList(markdown);
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

  // ── R3-2 (2026-05-07): explicit user language preference ──────────────
  // The backend system prompt previously had only a soft "match the user's
  // language" hint, which let the LLM drift on mixed-language input. R3-2
  // adds an explicit per-user choice that we surface on first launch via a
  // modal and via a topbar chip thereafter. Persistence lives here so the
  // app component stays declarative; only zh / en are accepted (anything
  // else is treated as "no preference" so a tampered localStorage entry or
  // a future schema bump can't smuggle a bogus instruction through to the
  // server (server-side Pydantic also enforces the literal — defense in
  // depth, not the primary check).
  // Stable storage key — kept identical across releases so a refreshed page
  // never re-prompts the modal. Resolved value: "nano-nlm:v1:user-lang".
  const USER_LANG_KEY = `${PREFIX}:user-lang`;
  const _USER_LANG_VALID = new Set(["zh", "en"]);
  const DEFAULT_LANG_CHOICES = [
    { code: "zh", label: "中文", hint: "简体中文 / Reply in Chinese" },
    { code: "en", label: "English", hint: "English / Reply in English" },
  ];

  function loadUserLang(storage) {
    try {
      const raw = storage.getItem(USER_LANG_KEY);
      if (!raw) return null;
      return _USER_LANG_VALID.has(raw) ? raw : null;
    } catch (e) { return null; }
  }

  function saveUserLang(storage, lang) {
    if (!_USER_LANG_VALID.has(lang)) return false;
    try { storage.setItem(USER_LANG_KEY, lang); return true; }
    catch (e) {
      if (typeof console !== "undefined") console.warn("user-lang save failed", e);
      return false;
    }
  }

  // ── Hidden-courses (frontend-only, localStorage-backed) ─────────────
  // Lets the user hide stale/test courses (SmokeTest, abandoned uploads)
  // from the topbar dropdown WITHOUT touching backend data. Hidden state
  // is per-browser; "manage" panel toggles entries and "unhide all"
  // clears the set. Backend `/api/courses` is unaware of this list.
  const HIDDEN_COURSES_KEY = `${PREFIX}:hidden-courses`;

  function loadHiddenCourses(storage) {
    try {
      const raw = storage.getItem(HIDDEN_COURSES_KEY);
      if (!raw) return [];
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed.filter(s => typeof s === "string") : [];
    } catch (e) { return []; }
  }

  function saveHiddenCourses(storage, ids) {
    try {
      const list = Array.isArray(ids)
        ? Array.from(new Set(ids.filter(s => typeof s === "string"))).sort()
        : [];
      storage.setItem(HIDDEN_COURSES_KEY, JSON.stringify(list));
      return true;
    } catch (e) {
      if (typeof console !== "undefined") console.warn("hidden-courses save failed", e);
      return false;
    }
  }

  function isCourseHidden(storage, courseId) {
    if (!courseId) return false;
    return loadHiddenCourses(storage).includes(courseId);
  }

  function setCourseHidden(storage, courseId, hidden) {
    if (!courseId) return loadHiddenCourses(storage);
    const current = new Set(loadHiddenCourses(storage));
    if (hidden) current.add(courseId);
    else current.delete(courseId);
    const next = Array.from(current).sort();
    saveHiddenCourses(storage, next);
    return next;
  }

  function filterVisibleCourses(courses, hiddenIds) {
    if (!Array.isArray(courses)) return [];
    const hidden = new Set(Array.isArray(hiddenIds) ? hiddenIds : []);
    return courses.filter(c => c && !hidden.has(c.id));
  }

  function clearHiddenCourses(storage) {
    try { storage.removeItem(HIDDEN_COURSES_KEY); return true; }
    catch (e) { return false; }
  }

  // ── Notes scroll-position cache (per-course) ────────────────────────
  // Preserves the user's scroll spot in the Notes preview across tab
  // switches (Notes → Reader → back to Notes). Per-course so a switch to
  // a different course doesn't restore a wrong-document offset. Cleared
  // when content is about to be replaced (regeneration → streaming=true).
  function _notesScrollKey(courseId) {
    return `${PREFIX}:${courseId}:notes-scroll-y`;
  }

  function loadNotesScroll(storage, courseId) {
    if (!courseId) return null;
    try {
      const raw = storage.getItem(_notesScrollKey(courseId));
      if (raw == null) return null;
      const n = Number(raw);
      return Number.isFinite(n) && n >= 0 ? n : null;
    } catch (e) { return null; }
  }

  function saveNotesScroll(storage, courseId, scrollY) {
    if (!courseId) return false;
    const n = Number(scrollY);
    if (!Number.isFinite(n) || n < 0) return false;
    try {
      storage.setItem(_notesScrollKey(courseId), String(Math.round(n)));
      return true;
    } catch (e) { return false; }
  }

  function clearNotesScroll(storage, courseId) {
    if (!courseId) return false;
    try { storage.removeItem(_notesScrollKey(courseId)); return true; }
    catch (e) { return false; }
  }

  return {
    createMemoryStorage,
    createSkillEntries,
    getCheckedSourceFiles,
    resolveCitationNavigation,
    prepareMindmap,
    prepareMindmapTree,
    prepareMindmapForce,
    getMindmapNodeDetail,
    applyMindmapOps,
    newMindmapNodeId,
    requestNodeDeepDive,
    saveNoteDraft,
    loadNoteDraft,
    buildMarkdownExport,
    buildPdfPrintHtml,
    buildLatexExport,
    buildPrintHtml,
    saveQuizAnswers,
    loadQuizAnswers,
    filterWrongQuestions,
    generateWeakAreaQuiz,
    formatMasteryState,
    createGenerationState,
    recordPartialGeneration,
    recordGenerationFailure,
    retryGeneration,
    formatStreamErrorMessage,
    STREAM_ERROR_MESSAGES,
    formatStatusBar,
    loadHighlights,
    saveHighlights,
    addHighlight,
    updateHighlight,
    removeHighlight,
    locateHighlight,
    pruneStaleHighlights,
    buildContextWindows,
    extractHeadingTOC,
    slugifyHeading,
    slugifyHeadingsList,
    HIGHLIGHT_COLORS,
    loadUserLang,
    saveUserLang,
    USER_LANG_KEY,
    DEFAULT_LANG_CHOICES,
    loadHiddenCourses,
    saveHiddenCourses,
    isCourseHidden,
    setCourseHidden,
    filterVisibleCourses,
    clearHiddenCourses,
    HIDDEN_COURSES_KEY,
    loadNotesScroll,
    saveNotesScroll,
    clearNotesScroll,
  };
});
