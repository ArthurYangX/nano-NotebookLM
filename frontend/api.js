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

function _post(path, payload) {
  return _request(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

const API = {
  async getCourses() {
    return _request("/courses");
  },

  async getSources(courseId) {
    return _request(`/sources/${encodeURIComponent(courseId)}`);
  },

  async chat(question, courseId = null, topK = 5, checkedFiles = null) {
    return _post("/chat", {
      question, course_id: courseId, top_k: topK, checked_files: checkedFiles,
    });
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

  async generateNotes(courseId, topic = null, format = "markdown") {
    return _post("/notes", { course_id: courseId, topic, format });
  },

  async generateQuiz(courseId, topic = null, numQuestions = 6, difficulty = "medium") {
    return _post("/quiz", {
      course_id: courseId, topic, num_questions: numQuestions, difficulty,
    });
  },

  async getMindmap(courseId) {
    return _request(`/mindmap/${encodeURIComponent(courseId)}`, { method: "POST" });
  },

  async generateReport(courseId, reportType = "summary", includeCode = false) {
    return _post("/report", {
      course_id: courseId, report_type: reportType, include_code: includeCode,
    });
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

  async getStatus() {
    return _request("/status");
  },

  async getHealth() {
    return _request("/health");
  },
};

window.API = API;
