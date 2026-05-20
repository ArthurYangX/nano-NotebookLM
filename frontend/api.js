/**
 * API bridge — connects Claude Design frontend to FastAPI backend.
 */
const API_BASE = window.location.origin + "/api";

async function _request(path, opts = {}) {
  const res = await fetch(`${API_BASE}${path}`, opts);
  let body = null;
  try { body = await res.json(); } catch { body = null; }
  if (!res.ok) {
    const detail = body && (body.detail || body.error) || `HTTP ${res.status}`;
    const err = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    err.status = res.status;
    err.requestId = body && body.request_id;
    err.body = body;
    throw err;
  }
  return body;
}

function _post(path, payload, opts = {}) {
  // opts can carry { signal } for AbortController support — passed through to
  // fetch so callers (chat / search / notes) can cancel in-flight requests.
  return _request(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    ...opts,
  });
}

const API = {
  async getCourses() {
    return _request(`/courses`);
  },

  async getSources(courseId) {
    return _request(`/sources/${encodeURIComponent(courseId)}`);
  },

  // Hard-delete a course (artifacts + indices). Frontend MUST confirm
  // first — this is irreversible. Returns `{deleted, course_id, removed[]}`.
  async deleteCourse(courseId) {
    const res = await fetch(`${API_BASE}/courses/${encodeURIComponent(courseId)}`, { method: "DELETE" });
    if (!res.ok) {
      let detail = null;
      try { const b = await res.json(); detail = b.detail || b.error; } catch {}
      const err = new Error(detail || `HTTP ${res.status}`);
      err.status = res.status;
      throw err;
    }
    return res.json();
  },

  async chat(question, courseId = null, topK = 5, checkedFiles = null, { signal } = {}, { userLang = null, backend = null, persona = null, activeSourceFile = null, history = null } = {}) {
    const body = {
      question, course_id: courseId, top_k: topK, checked_files: checkedFiles,
    };
    if (userLang) body.user_lang = userLang;
    // Thread the backend chip selection. Server validates against the
    // actually-configured router backends.
    if (backend === "openai" || backend === "claude" || backend === "local") body.backend = backend;
    // 2026-05-12: persona chip — empty / null means use server default.
    if (persona && typeof persona === "string" && persona.trim()) {
      body.persona = persona.trim();
    }
    // 2026-05-13: source_file the user is currently viewing in Reader,
    // surfaced as a soft retrieval bias on the backend (graphrag boosts
    // hits from this file). null when on All Courses / no focused file.
    if (activeSourceFile && typeof activeSourceFile === "string") {
      body.active_source_file = activeSourceFile;
    }
    // 2026-05-16: multi-turn history. Each entry must be {role: "user"|
    // "assistant", content: string}. Server caps at 12 turns + 4000 chars/
    // turn; do client-side trimming defensively so a malformed local
    // state can't 422 the whole chat. Empty array → omit so it threads
    // through as None on the backend (single-turn short-circuit).
    if (Array.isArray(history) && history.length > 0) {
      const trimmed = history
        .filter(t => t && (t.role === "user" || t.role === "assistant"))
        .map(t => ({
          role: t.role,
          content: String(t.content || "").slice(0, 4000),
        }))
        .filter(t => t.content.trim().length > 0)
        .slice(-12);
      if (trimmed.length > 0) body.history = trimmed;
    }
    return _post("/chat", body, { signal });
  },

  async getMemory() {
    return _request("/memory");
  },

  async updateMemory(key, value) {
    return _post("/memory", { key, value });
  },

  async search(query, courseId = null, topK = 5) {
    return _post("/search", { query, course_id: courseId, top_k: topK });
  },

  // review-swarm fix-all v1 #7: backend Note pipeline is LaTeX-only since
  // R4-6 (NoteRequest.format = Literal["latex"]). Hard-coding "markdown"
  // here would 422 the request. Drop the param entirely — the field
  // defaults to "latex" server-side; old callers passing extra positional
  // args still work since trailing `format` arg is harmless (unused now).
  async generateNotes(courseId, topic = null, { userLang = null } = {}) {
    const body = { course_id: courseId, topic };
    if (userLang) body.user_lang = userLang;
    return _post("/notes", body);
  },

  async streamNotes(courseId, topic = null, onEvent = null, { userLang = null } = {}) {
    const body = { course_id: courseId, topic };
    if (userLang) body.user_lang = userLang;
    return _stream("/notes/stream", body, onEvent);
  },

  // Full-course note generation: per-file parallel LLM calls (concurrency
  // capped at 4 by default), programmatic merge, single LLM review pass.
  // Event vocabulary (see api/server.py /api/notes/full-course/stream):
  //   plan / file_start / file_delta / file_done / file_error /
  //   file_cached / merging / reviewing / review_chunk / done / error
  //
  // file_delta (2026-05-12): per-file LLM is now token-streamed via
  // router.complete_stream — each delta arrives as a file_delta event
  // keyed by idx. The terminal file_done's `content` is the sanitized
  // final body and overrides any accumulated deltas (truth-pin).
  //
  // Incremental cache (2026-05-11): files whose chunk_hash matches the
  // entry in per_file_cache.json short-circuit to a `file_cached` event
  // without an LLM call. Pass `force: true` to ignore the cache and
  // re-run every file.
  async streamFullCourseNotes(courseId, onEvent = null, { userLang = null, concurrency = null, force = false } = {}) {
    const body = { course_id: courseId };
    if (userLang) body.user_lang = userLang;
    if (concurrency != null) body.concurrency = concurrency;
    if (force) body.force = true;
    return _stream("/notes/full-course/stream", body, onEvent);
  },

  async generateQuiz(courseId, topic = null, numQuestions = 6, difficulty = "medium", { userLang = null } = {}) {
    const body = {
      course_id: courseId, topic, num_questions: numQuestions, difficulty,
    };
    if (userLang) body.user_lang = userLang;
    return _post("/quiz", body);
  },

  async streamQuiz(courseId, topic = null, numQuestions = 6, difficulty = "medium", onEvent = null, { userLang = null } = {}) {
    const body = {
      course_id: courseId, topic, num_questions: numQuestions, difficulty,
    };
    if (userLang) body.user_lang = userLang;
    return _stream("/quiz/stream", body, onEvent);
  },

  async getMindmap(courseId) {
    return _request(`/mindmap/${encodeURIComponent(courseId)}`, { method: "POST" });
  },

  async editMindmap(courseId, ops) {
    return _post(`/mindmap/${encodeURIComponent(courseId)}/edit`, { ops });
  },

  async generateReport(courseId, reportType = "summary", includeCode = false) {
    return _post("/report", {
      course_id: courseId, report_type: reportType, include_code: includeCode,
    });
  },

  async streamReport(courseId, reportType = "summary", includeCode = false, onEvent = null, { userLang = null } = {}) {
    const body = { course_id: courseId, report_type: reportType, include_code: includeCode };
    if (userLang) body.user_lang = userLang;
    return _stream("/report/stream", body, onEvent);
  },

  async analyzeExam(courseId) {
    return _post("/exam-analysis", { course_id: courseId });
  },

  // ── Exam Prep (closed-loop) ─────────────────────────────────────
  // `userLang` ∈ {"zh","en",null} threads the user's language preference
  // down to topic + question generation so the LLM doesn't echo the
  // source-material language when the student picked the other one.
  async examPrepPlan(courseId, { maxTopics = null, force = false, userLang = null } = {}) {
    // 2026-05-13: maxTopics now defaults to null so the backend's
    // auto-scaling kicks in (≥3 topics per file). Passing an explicit
    // number still pins the count via the API's `max_topics` field.
    const body = { course_id: courseId, force };
    if (maxTopics != null) body.max_topics = maxTopics;
    if (userLang) body.user_lang = userLang;
    return _post("/exam-prep/plan", body);
  },
  async examPrepSeed(courseId, { topicIds = null, seedsPerType = 2, userLang = null } = {}) {
    const body = { course_id: courseId, topic_ids: topicIds, seeds_per_type: seedsPerType };
    if (userLang) body.user_lang = userLang;
    return _post("/exam-prep/seed", body);
  },
  async examPrepNextQuiz(courseId, { size = 8, topicIds = null, userLang = null } = {}) {
    const body = { course_id: courseId, size, topic_ids: topicIds };
    if (userLang) body.user_lang = userLang;
    return _post("/exam-prep/quiz/next", body);
  },
  async examPrepSubmit(courseId, answers, { userLang = null } = {}) {
    const body = { course_id: courseId, answers };
    if (userLang) body.user_lang = userLang;
    return _post("/exam-prep/quiz/submit", body);
  },
  // GET/DELETE moved to /state/{course_id} so a verb typo (e.g.
  // GET /api/exam-prep/plan) can't fall through to here and silently create
  // a course literally named "plan" (fix-all v1 M8).
  async examPrepView(courseId) {
    const res = await fetch(`${API_BASE}/exam-prep/state/${encodeURIComponent(courseId)}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  },
  async examPrepReset(courseId) {
    const res = await fetch(`${API_BASE}/exam-prep/state/${encodeURIComponent(courseId)}`, { method: "DELETE" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  },

  async ingestCourse(courseDir, courseId = null) {
    return _post("/ingest", { course_dir: courseDir, course_id: courseId });
  },

  // Upload background-task pattern (2026-05-16): /api/upload/{cid} now
  // returns {task_id, course_id} immediately after files are saved. The
  // ingest pipeline (chunking → embedding → kg_stage_a → kg_stage_b) runs
  // in a background task; the caller polls GET /api/upload/status/{task_id}
  // for progress. Replaces the old NDJSON-streaming `uploadFiles` so a
  // tab close / network blip doesn't kill the upload.
  //
  // R5/MinerU: pass `{ engine: "mineru" | "pymupdf", lang: "ch" | "en" }`
  // to route PDFs through MinerU. Omitting opts keeps the default
  // fast pymupdf path.
  async startUpload(courseId, files, { engine, lang } = {}) {
    const formData = new FormData();
    for (const file of files) {
      formData.append("files", file);
    }
    const qs = new URLSearchParams();
    if (engine) qs.set("engine", engine);
    if (lang) qs.set("lang", lang);
    const url = `${API_BASE}/upload/${encodeURIComponent(courseId)}` + (qs.toString() ? `?${qs}` : "");
    const res = await fetch(url, {
      method: "POST",
      body: formData,
    });
    if (!res.ok) {
      // Parity with _request — surface server's {error, detail} envelope
      // into err.message so the UI shows the real reason
      // (e.g. "File 'foo.exe' exceeds 50MB limit") not bare "HTTP 413".
      let detail = null;
      let body = null;
      try {
        body = await res.json();
        detail = body.detail || body.error || null;
      } catch { /* non-JSON body */ }
      const err = new Error(detail || `HTTP ${res.status}`);
      err.status = res.status;
      err.body = body;
      err.requestId = res.headers.get("x-request-id") || null;
      throw err;
    }
    return res.json(); // { task_id, course_id }
  },

  // Poll endpoint for the background upload task. Returns:
  //   - null when the server reports 404 (task evicted / unknown id) —
  //     caller should stop polling and treat as "task gone".
  //   - state object otherwise: { task_id, course_id, status,
  //     stages: { chunking, embedding, kg_stage_a, kg_stage_b },
  //     result | null, error | null, error_stage | null, file_names }.
  // Throws on transport errors so the caller's retry loop can decide to
  // keep polling vs. surface the failure.
  async getUploadStatus(taskId) {
    const res = await fetch(`${API_BASE}/upload/status/${encodeURIComponent(taskId)}`);
    if (res.status === 404) return null;
    if (!res.ok) {
      let detail = null;
      let body = null;
      try {
        body = await res.json();
        detail = body.detail || body.error || null;
      } catch { /* non-JSON body */ }
      const err = new Error(detail || `HTTP ${res.status}`);
      err.status = res.status;
      err.body = body;
      throw err;
    }
    return res.json();
  },

  async getChunk(chunkId, { signal } = {}) {
    return _request(`/chunks/${encodeURIComponent(chunkId)}`, { signal });
  },

  async getSourceChunks(courseId, docId, { signal } = {}) {
    return _request(
      `/source/${encodeURIComponent(courseId)}/${encodeURIComponent(docId)}/chunks`,
      { signal },
    );
  },

  // Returns a URL string (not a fetch) so the Reader can hand it to an
  // `<iframe src>` and let the browser's native PDF viewer handle range
  // requests, scroll, zoom, and the `#page=N` anchor.
  //
  // `page` is appended only when it's a positive integer. Anything else
  // (null, NaN, "5; rm -rf /", Infinity) drops the fragment — defensive
  // belt against future callers that bypass `resolveCitationNavigation`
  // (which already pins `page` to a `Number(/[0-9]+/)` match).
  sourceFileUrl(courseId, docId, { page = null, hideOutline = false, navEpoch = null } = {}) {
    // PDFium URL fragments stack — `#page=N&navpanes=0` jumps to page N and
    // collapses the bookmarks/thumbnails side panel. We omit the `navpanes`
    // part when `hideOutline` is unset so we don't override the user's
    // PDF-viewer UI preference unnecessarily.
    //
    // R5-2 fix-all v8: PDFium honours `#page=N` at LOAD time only — any
    // post-load `iframe.src = '...#page=M'` or `contentWindow.location
    // .hash = '#page=M'` mutation is unreliable across Chromium versions.
    // Symptom: 1st citation click jumps pages OK, 2nd/3rd updates the
    // page indicator (React state) but the rendered slide stays pinned.
    // Fix: when caller passes `navEpoch`, append it as `?_nav=<epoch>` so
    // each navigation produces a DIFFERENT URL path+query (not just a
    // different fragment). Browser performs a fresh fetch; PDFium
    // re-initialises with the new hash. HTTP cache (ETag → 304) keeps the
    // wire cost near-zero; the visible cost is PDFium re-parse ~50-300ms.
    const base = `/api/source/${encodeURIComponent(courseId)}/${encodeURIComponent(docId)}/file`;
    const query = [];
    const ep = Number(navEpoch);
    if (Number.isInteger(ep) && ep > 0) query.push(`_nav=${ep}`);
    const urlBase = query.length ? `${base}?${query.join("&")}` : base;
    const frags = [];
    const n = Number(page);
    if (Number.isInteger(n) && n >= 1) frags.push(`page=${n}`);
    if (hideOutline) frags.push("navpanes=0");
    return frags.length ? `${urlBase}#${frags.join("&")}` : urlBase;
  },

  async getStatus() {
    return _request("/status");
  },

  // Switch the active embedding preset. Server persists the choice,
  // resets kb.embed_fn, and kicks off a background rebuild of every
  // course's FAISS index against the new model. UI should poll /api/status
  // (embedding_rebuild field) for progress; the per-preset FAISS namespace
  // means switching back to a previously-used preset is instant — its
  // index is still on disk.
  async setEmbeddingPreset(presetId) {
    return _post("/settings/embedding", { preset_id: presetId });
  },

  async runSubagent(name, payload = {}) {
    return _post("/subagent", { name, payload });
  },

  async getSessionLog() {
    return _request("/session-log");
  },

  async appendSessionLog(courseId, kind, payload = {}) {
    return _post("/session-log", { course_id: courseId, kind, payload });
  },

  async getHealth() {
    return _request("/health");
  },
};

window.API = API;

async function _stream(path, payload, onEvent) {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const err = new Error(`HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  if (!res.body || !window.TextDecoder) {
    return _request(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  // review-swarm fix-all v3 #C5 + M8: a single malformed NDJSON line (proxy
  // keepalive, half-chunk, upstream HTML error page) used to crash the
  // entire stream consumer with `JSON.parse` throwing — the user lost the
  // remaining stream including the final `done` event. Now each line is
  // parsed defensively and the running buffer has a hard cap so a missing
  // newline can't OOM the client.
  const MAX_LINE_BYTES = 1024 * 1024;
  let buffer = "";
  let finalEvent = null;
  function _emit(line) {
    let event;
    try { event = JSON.parse(line); }
    catch (e) {
      if (typeof console !== "undefined") {
        console.warn("NDJSON parse skip:", String(line).slice(0, 120));
      }
      return;
    }
    finalEvent = event;
    if (onEvent) onEvent(event);
  }
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    if (buffer.length > MAX_LINE_BYTES) {
      if (typeof console !== "undefined") {
        console.warn("NDJSON buffer over " + MAX_LINE_BYTES + "B without newline; dropping");
      }
      buffer = "";
    }
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";
    for (const line of lines) {
      if (!line.trim()) continue;
      _emit(line);
    }
  }
  if (buffer.trim()) _emit(buffer);
  return finalEvent;
}
