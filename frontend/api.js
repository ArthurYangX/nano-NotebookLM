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
  // R4-1: mode="user" (default) hides preset courses; "all" returns everything.
  // Frontend reads URL `?show_preset=1` to opt back into the all-courses view.
  async getCourses(mode = "user") {
    const qs = mode && mode !== "user" ? `?mode=${encodeURIComponent(mode)}` : "?mode=user";
    return _request(`/courses${qs}`);
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

  async chat(question, courseId = null, topK = 5, checkedFiles = null, { signal } = {}, { userLang = null, backend = null, persona = null } = {}) {
    const body = {
      question, course_id: courseId, top_k: topK, checked_files: checkedFiles,
    };
    if (userLang) body.user_lang = userLang;
    // R4-5 part 2: thread the backend chip selection through to ChatRequest.
    // Server-side Pydantic Literal rejects anything other than "codex"/"qwen_raft".
    if (backend === "codex" || backend === "qwen_raft") body.backend = backend;
    // 2026-05-12: persona chip — empty / null means use server default.
    if (persona && typeof persona === "string" && persona.trim()) {
      body.persona = persona.trim();
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
  async examPrepPlan(courseId, { maxTopics = 8, force = false, userLang = null } = {}) {
    const body = { course_id: courseId, max_topics: maxTopics, force };
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

  // R4-2: /api/upload/{cid} now streams NDJSON (4 stages → done|error).
  // ``onEvent`` receives `{type:"stage", stage, progress, detail?}` /
  // `{type:"done", course_id, files, chunks, documents, kg_nodes}` /
  // `{type:"error", error, stage?}`. Returns the final event so callers
  // who only care about completion can `await` it like the old endpoint.
  async uploadFiles(courseId, files, onEvent = null) {
    const formData = new FormData();
    for (const file of files) {
      formData.append("files", file);
    }
    const res = await fetch(`${API_BASE}/upload/${encodeURIComponent(courseId)}`, {
      method: "POST",
      body: formData,
    });
    if (!res.ok) {
      // fix-all v1 #A8: parity with _request — surface server's
      // {error, detail} envelope into err.message so the UI shows
      // "File 'foo.exe' exceeds 50MB limit" not bare "HTTP 413".
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
    if (!res.body || !window.TextDecoder) {
      // Fallback: consume the entire response as text and parse line-by-line.
      const text = await res.text();
      let last = null;
      for (const line of text.split("\n")) {
        if (!line.trim()) continue;
        try {
          const ev = JSON.parse(line);
          last = ev;
          if (onEvent) onEvent(ev);
        } catch { /* skip */ }
      }
      return last;
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    const MAX_LINE_BYTES = 1024 * 1024;
    let buffer = "";
    let finalEvent = null;
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      if (buffer.length > MAX_LINE_BYTES) {
        buffer = "";
      }
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";
      for (const line of lines) {
        if (!line.trim()) continue;
        let ev;
        try { ev = JSON.parse(line); } catch { continue; }
        finalEvent = ev;
        if (onEvent) onEvent(ev);
      }
    }
    if (buffer.trim()) {
      try {
        const ev = JSON.parse(buffer);
        finalEvent = ev;
        if (onEvent) onEvent(ev);
      } catch { /* skip */ }
    }
    return finalEvent;
  },

  async getMastery(courseId) {
    return _request(`/mastery/${encodeURIComponent(courseId)}`);
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
