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
    return _request("/courses");
  },

  async getSources(courseId) {
    return _request(`/sources/${encodeURIComponent(courseId)}`);
  },

  async chat(question, courseId = null, topK = 5, checkedFiles = null, { signal } = {}, { userLang = null } = {}) {
    const body = {
      question, course_id: courseId, top_k: topK, checked_files: checkedFiles,
    };
    if (userLang) body.user_lang = userLang;
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

  async generateNotes(courseId, topic = null, format = "markdown", { userLang = null } = {}) {
    const body = { course_id: courseId, topic, format };
    if (userLang) body.user_lang = userLang;
    return _post("/notes", body);
  },

  async streamNotes(courseId, topic = null, format = "markdown", onEvent = null, { userLang = null } = {}) {
    const body = { course_id: courseId, topic, format };
    if (userLang) body.user_lang = userLang;
    return _stream("/notes/stream", body, onEvent);
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

  async ingestCourse(courseDir, courseId = null) {
    return _post("/ingest", { course_dir: courseDir, course_id: courseId });
  },

  async uploadFiles(courseId, files) {
    const formData = new FormData();
    for (const file of files) {
      formData.append("files", file);
    }
    return _request(`/upload/${encodeURIComponent(courseId)}`, {
      method: "POST",
      body: formData,
    });
  },

  async getMastery(courseId) {
    return _request(`/mastery/${encodeURIComponent(courseId)}`);
  },

  async getChunk(chunkId, { signal } = {}) {
    return _request(`/chunks/${encodeURIComponent(chunkId)}`, { signal });
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
