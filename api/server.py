"""FastAPI backend for nano-NOTEBOOKLM — serves API + static frontend."""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, File, UploadFile, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nano_notebooklm.ai.router import ModelRouter
from nano_notebooklm.agents import run_subagent
from nano_notebooklm.agents.formatter import format_response
from nano_notebooklm.kb.store import KBStore
from nano_notebooklm.orchestrator.engine import Orchestrator
from nano_notebooklm.orchestrator.session_log import SessionLog
from nano_notebooklm import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)
access_log = logging.getLogger("nano.access")

# ── Init core components ─────────────────────────────────────────────
kb = KBStore()
router = ModelRouter()
orchestrator = Orchestrator(kb, router)
session_log = SessionLog(config.ARTIFACTS_DIR)
LATENCY_SAMPLES: dict[str, list[float]] = {"search": [], "chat": []}

app = FastAPI(
    title="nano-NOTEBOOKLM API",
    version="0.2.0",
    description="AI-powered study assistant — knowledge extraction, notes, quizzes, and reports.",
    contact={"name": "nano-NOTEBOOKLM"},
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Upload limits ────────────────────────────────────────────────────
MAX_UPLOAD_SIZE_MB = 50
MAX_UPLOAD_SIZE_BYTES = MAX_UPLOAD_SIZE_MB * 1024 * 1024
ALLOWED_UPLOAD_SUFFIXES = {".pdf", ".pptx", ".docx", ".md", ".txt"}

# ── Middleware: request ID, access log, latency ──────────────────────
@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    request.state.request_id = rid
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        elapsed_ms = (time.perf_counter() - start) * 1000
        access_log.exception(
            "rid=%s %s %s -> 500 in %.1fms (unhandled)",
            rid, request.method, request.url.path, elapsed_ms,
        )
        raise
    elapsed_ms = (time.perf_counter() - start) * 1000
    _record_latency(request.url.path, elapsed_ms)
    response.headers["x-request-id"] = rid
    response.headers["x-response-time-ms"] = f"{elapsed_ms:.1f}"
    # Dev-mode: prevent browser from caching JSX/JS so edits show up on plain
    # reload. Babel-standalone otherwise serves stale code and produces
    # confusing "fix didn't work" reports.
    if request.url.path.startswith("/static") or request.url.path == "/":
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    access_log.info(
        "rid=%s %s %s -> %d in %.1fms",
        rid, request.method, request.url.path, response.status_code, elapsed_ms,
    )
    return response


# ── Global exception handlers ────────────────────────────────────────
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    rid = getattr(request.state, "request_id", "?")
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": "validation_error",
            "request_id": rid,
            "detail": jsonable_encoder(exc.errors()),
        },
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    rid = getattr(request.state, "request_id", "?")
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": exc.detail if isinstance(exc.detail, str) else "http_error",
            "request_id": rid,
            "detail": exc.detail,
        },
        headers=exc.headers,
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    rid = getattr(request.state, "request_id", "?")
    logger.exception("rid=%s unhandled error in %s %s", rid, request.method, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": "internal_error",
            "request_id": rid,
            "detail": str(exc),
        },
    )


# ── Request/Response models (with validation) ───────────────────────
class ChatRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=4000)
    course_id: str | None = Field(None, max_length=128)
    top_k: int = Field(5, ge=1, le=50)
    checked_files: list[str] | None = None

    @field_validator("question")
    @classmethod
    def question_must_not_be_blank(cls, value: str) -> str:
        return _strip_nonempty(value, "question")


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)
    course_id: str | None = Field(None, max_length=128)
    top_k: int = Field(5, ge=1, le=50)

    @field_validator("query")
    @classmethod
    def query_must_not_be_blank(cls, value: str) -> str:
        return _strip_nonempty(value, "query")


class NoteRequest(BaseModel):
    course_id: str = Field(..., min_length=1, max_length=128)
    topic: str | None = Field(None, max_length=500)
    format: str = Field("markdown", pattern=r"^(markdown|text|html)$")


class QuizRequest(BaseModel):
    course_id: str = Field(..., min_length=1, max_length=128)
    topic: str | None = Field(None, max_length=500)
    num_questions: int = Field(6, ge=1, le=30)
    difficulty: str = Field("medium", pattern=r"^(easy|medium|hard)$")


class ReportRequest(BaseModel):
    course_id: str = Field(..., min_length=1, max_length=128)
    report_type: str = Field("summary", max_length=64)
    include_code: bool = False
    format: str = Field("markdown", pattern=r"^(markdown|text|html)$")


class IngestRequest(BaseModel):
    course_dir: str = Field(..., min_length=1)
    course_id: str | None = Field(None, max_length=128)


class ExamAnalysisRequest(BaseModel):
    course_id: str = Field(..., min_length=1, max_length=128)


class MemoryUpdate(BaseModel):
    key: str = Field(..., min_length=1, max_length=200)
    value: Any


class SubagentRequest(BaseModel):
    name: str = Field(..., pattern=r"^(web_research|formatter)$")
    payload: dict[str, Any] = Field(default_factory=dict)


class SessionEntryRequest(BaseModel):
    course_id: str | None = Field(None, max_length=128)
    kind: str = Field(..., min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)


def _strip_nonempty(value: str, field_name: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field_name} must not be blank")
    return stripped


# ── Course endpoints ─────────────────────────────────────────────────
@app.get("/api/courses", tags=["courses"], summary="List courses with chunk counts")
async def list_courses():
    courses = orchestrator.list_courses()
    result = []
    for cid in courses:
        chunks = kb.get_chunks(cid)
        meta_path = config.ARTIFACTS_DIR / "courses" / cid / "course_meta.json"
        meta = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
            except json.JSONDecodeError:
                logger.warning("Corrupt course_meta.json for %s", cid)
        result.append({
            "id": cid,
            "name": meta.get("name", cid),
            "chunks": len(chunks),
            "documents": len(meta.get("documents", [])),
        })
    return {"courses": result}


@app.get("/api/sources/{course_id}", tags=["courses"], summary="List source files for a course")
async def get_sources(course_id: str):
    chunks = kb.get_chunks(course_id)
    if not chunks:
        return {"sources": []}
    sources: dict[str, dict] = {}
    for c in chunks:
        if c.source_file not in sources:
            sources[c.source_file] = {
                "id": c.doc_id,
                "type": c.file_type.value,
                "title": c.source_file,
                "chunks": 0,
                "checked": True,
            }
        sources[c.source_file]["chunks"] += 1
    return {"sources": list(sources.values())}


# ── Skill endpoints ──────────────────────────────────────────────────
@app.post("/api/chat", tags=["skills"], summary="RAG chat with source citations")
async def chat(req: ChatRequest):
    result = await orchestrator.skills["qa"].execute({
        "question": req.question,
        "course_filter": req.course_id,
        "top_k": req.top_k,
        "checked_files": req.checked_files,
    })
    if not result.success:
        raise HTTPException(status_code=502, detail=result.error or "qa skill failed")
    data = dict(result.data)
    if "answer" in data:
        data["answer"] = format_response(str(data["answer"]))
    session_log.append(req.course_id, "question", {"question": req.question, "answer": data.get("answer", "")})
    return data


@app.post("/api/search", tags=["skills"], summary="Hybrid search across knowledge base")
async def search(req: SearchRequest):
    results = kb.search(req.query, top_k=req.top_k, course_id=req.course_id)
    session_log.append(req.course_id, "search", {"query": req.query, "results": len(results)})
    return {
        "results": [
            {
                "chunk_id": r.chunk_id,
                "text": r.text,
                "source_file": r.source_file,
                "location": r.location,
                "score": r.score,
                "course_id": r.course_id,
            }
            for r in results
        ]
    }


@app.post("/api/notes", tags=["skills"], summary="Generate structured study notes")
async def generate_notes(req: NoteRequest):
    result = await orchestrator.run_skill("note_generator", {
        "course_id": req.course_id,
        "topic": req.topic,
        "format": req.format,
    })
    if not result.success:
        raise HTTPException(status_code=502, detail=result.error or "note generation failed")
    data = dict(result.data)
    if "content" in data:
        data["content"] = format_response(str(data["content"]))
    session_log.append(req.course_id, "generation", {"kind": "notes", "topic": req.topic})
    return data


@app.post("/api/quiz", tags=["skills"], summary="Generate a practice quiz")
async def generate_quiz(req: QuizRequest):
    result = await orchestrator.run_skill("quiz_generator", {
        "course_id": req.course_id,
        "topic": req.topic,
        "num_questions": req.num_questions,
        "difficulty": req.difficulty,
    })
    if not result.success:
        raise HTTPException(status_code=502, detail=result.error or "quiz generation failed")
    session_log.append(req.course_id, "generation", {"kind": "quiz", "topic": req.topic, "num_questions": req.num_questions})
    return result.data


@app.post("/api/exam-analysis", tags=["skills"], summary="Analyze exam patterns for a course")
async def analyze_exam(req: ExamAnalysisRequest):
    result = await orchestrator.run_skill("exam_analyzer", {"course_id": req.course_id})
    if not result.success:
        raise HTTPException(status_code=502, detail=result.error or "exam analysis failed")
    session_log.append(req.course_id, "generation", {"kind": "exam-analysis"})
    return result.data


@app.post("/api/report", tags=["skills"], summary="Generate a course report")
async def generate_report(req: ReportRequest):
    result = await orchestrator.run_skill("report_generator", {
        "course_id": req.course_id,
        "report_type": req.report_type,
        "include_code": req.include_code,
        "format": req.format,
    })
    if not result.success:
        raise HTTPException(status_code=502, detail=result.error or "report generation failed")
    data = dict(result.data)
    if "content" in data:
        data["content"] = format_response(str(data["content"]))
    session_log.append(req.course_id, "generation", {"kind": "report", "report_type": req.report_type})
    return data


@app.post("/api/notes/stream", tags=["skills"], summary="Stream structured study notes")
async def stream_notes(req: NoteRequest):
    return _stream_response("note_generator", {
        "course_id": req.course_id,
        "topic": req.topic,
        "format": req.format,
    }, req.course_id, "notes")


@app.post("/api/quiz/stream", tags=["skills"], summary="Stream a practice quiz")
async def stream_quiz(req: QuizRequest):
    return _stream_response("quiz_generator", {
        "course_id": req.course_id,
        "topic": req.topic,
        "num_questions": req.num_questions,
        "difficulty": req.difficulty,
    }, req.course_id, "quiz")


@app.post("/api/report/stream", tags=["skills"], summary="Stream a course report")
async def stream_report(req: ReportRequest):
    return _stream_response("report_generator", {
        "course_id": req.course_id,
        "report_type": req.report_type,
        "include_code": req.include_code,
        "format": req.format,
    }, req.course_id, "report")


@app.post("/api/subagent", tags=["agents"], summary="Run a stateless subagent")
async def run_subagent_endpoint(req: SubagentRequest):
    return await run_subagent(req.name, req.payload)


@app.post("/api/mindmap/{course_id}", tags=["skills"], summary="Get or generate knowledge graph as mindmap tree")
async def get_mindmap(course_id: str):
    kg_path = config.ARTIFACTS_DIR / "courses" / course_id / "knowledge_graph.json"

    if kg_path.exists():
        try:
            data = json.loads(kg_path.read_text())
            return _kg_to_mindmap(data, course_id)
        except json.JSONDecodeError:
            logger.warning("Corrupt knowledge_graph.json for %s, regenerating", course_id)

    from nano_notebooklm.kg.extractor import extract_from_chunks
    from nano_notebooklm.kg.graph import KnowledgeGraph
    from nano_notebooklm.kg.merger import merge_concepts, merge_relations

    chunks = kb.get_chunks(course_id)
    if not chunks:
        raise HTTPException(404, f"No chunks for course '{course_id}'")

    concepts, relations = await extract_from_chunks(chunks, course_id, router, max_chunks=30)
    concepts = merge_concepts(concepts)
    relations = merge_relations(relations)

    kg = KnowledgeGraph()
    kg.add_concepts(concepts)
    kg.add_relations(relations)
    kg.save(kg_path)

    data = json.loads(kg_path.read_text())
    return _kg_to_mindmap(data, course_id)


# ── Ingest / upload ──────────────────────────────────────────────────
@app.post("/api/ingest", tags=["ingest"], summary="Ingest a course directory")
async def ingest_course(req: IngestRequest):
    course_dir = Path(req.course_dir)
    if not course_dir.exists() or not course_dir.is_dir():
        raise HTTPException(404, f"Directory not found: {req.course_dir}")

    cid = req.course_id or course_dir.name
    course = kb.ingest_course(str(course_dir), cid)
    kb.build_index(cid)
    chunks = kb.get_chunks(cid)
    return {
        "course_id": cid,
        "chunks": len(chunks),
        "documents": len(course.documents),
    }


@app.post("/api/upload/{course_id}", tags=["ingest"], summary="Upload files to a course and index")
async def upload_files(course_id: str, files: Annotated[list[UploadFile], File(...)]):
    if not files:
        raise HTTPException(400, "No files provided")

    upload_dir = config.ARTIFACTS_DIR / "uploads" / course_id
    upload_dir.mkdir(parents=True, exist_ok=True)

    saved = 0
    for f in files:
        if not f.filename:
            continue
        suffix = Path(f.filename).suffix.lower()
        if suffix not in ALLOWED_UPLOAD_SUFFIXES:
            raise HTTPException(
                400,
                f"Unsupported file type '{suffix}'. Allowed: {sorted(ALLOWED_UPLOAD_SUFFIXES)}",
            )
        safe_name = Path(f.filename).name
        dest = upload_dir / safe_name
        content = await f.read()
        if len(content) > MAX_UPLOAD_SIZE_BYTES:
            raise HTTPException(
                413,
                f"File '{safe_name}' is {len(content) / 1024 / 1024:.1f}MB, exceeds limit of {MAX_UPLOAD_SIZE_MB}MB",
            )
        dest.write_bytes(content)
        saved += 1

    if saved == 0:
        raise HTTPException(400, "No valid files saved")

    course = kb.ingest_course(str(upload_dir), course_id)
    kb.build_index(course_id)
    chunks = kb.get_chunks(course_id)
    return {
        "course_id": course_id,
        "files": saved,
        "chunks": len(chunks),
        "documents": len(course.documents),
    }


# ── Mastery ──────────────────────────────────────────────────────────
@app.get("/api/mastery/{course_id}", tags=["learning"], summary="Get mastery scores and weak areas")
async def get_mastery(course_id: str):
    mastery_path = config.ARTIFACTS_DIR / "courses" / course_id / "mastery.json"
    if not mastery_path.exists():
        return {"mastery": {}, "weak_areas": []}
    try:
        data = json.loads(mastery_path.read_text())
    except json.JSONDecodeError:
        raise HTTPException(500, "Corrupt mastery.json")
    weak = sorted(
        [{"concept": v.get("concept", k), "score": v.get("score", 0.0)}
         for k, v in data.items() if v.get("score", 1.0) < 0.5],
        key=lambda x: x["score"],
    )
    return {"mastery": data, "weak_areas": weak}


# ── Status / health ──────────────────────────────────────────────────
@app.get("/api/status", tags=["system"], summary="System status and model usage")
async def status_endpoint():
    courses = orchestrator.list_courses()
    total_chunks = sum(len(kb.get_chunks(c)) for c in courses)
    usage = router.get_usage_summary()
    total_cost = usage.get("total_cost_usd", usage.get("total_cost", 0.0))
    return {
        "backends": list(router.backends.keys()),
        "courses": len(courses),
        "total_chunks": total_chunks,
        "usage": {**usage, "total_cost": total_cost},
        "latency_ms": {
            "search_p50": _p50(LATENCY_SAMPLES["search"]),
            "chat_p50": _p50(LATENCY_SAMPLES["chat"]),
        },
        "embedding_mode": config.EMBEDDING_MODE,
        "embedding_model": config.EMBEDDING_MODEL,
        "version": app.version,
    }


@app.get("/api/health", tags=["system"], summary="Liveness probe")
async def health():
    return {"ok": True, "version": app.version}


# ── Memory endpoints ────────────────────────────────────────────────
from nano_notebooklm.orchestrator.memory import load_memory, save_memory, update_memory


@app.get("/api/memory", tags=["memory"], summary="Get user memory")
async def get_memory():
    return load_memory()


@app.post("/api/memory", tags=["memory"], summary="Update a single memory field")
async def set_memory(req: MemoryUpdate):
    update_memory(req.key, req.value)
    return {"ok": True}


@app.put("/api/memory", tags=["memory"], summary="Replace entire memory")
async def replace_memory(data: dict):
    save_memory(data)
    return {"ok": True}


@app.get("/api/session-log", tags=["learning"], summary="List daily session log entries")
async def list_session_log():
    return {"days": session_log.list_grouped()}


@app.post("/api/session-log", tags=["learning"], summary="Append a session log entry")
async def append_session_log(req: SessionEntryRequest):
    return session_log.append(req.course_id, req.kind, req.payload)


# ── Helper ───────────────────────────────────────────────────────────
def _stream_response(skill_name: str, params: dict, course_id: str, kind: str) -> StreamingResponse:
    async def events():
        partial = ""
        try:
            result = await orchestrator.run_skill(skill_name, params)
            if not result.success:
                raise RuntimeError(result.error or f"{kind} generation failed")
            content = _skill_stream_content(result.data)
            if kind in {"notes", "report"}:
                content = format_response(content)
            for chunk in _chunk_text(content):
                partial += chunk
                yield json.dumps({"type": "chunk", "chunk": chunk, "partial": partial}, ensure_ascii=False) + "\n"
            session_log.append(course_id, "generation", {"kind": kind, "streamed": True})
            yield json.dumps({"type": "done", "content": content}, ensure_ascii=False) + "\n"
        except Exception as exc:
            yield json.dumps({
                "type": "error",
                "error": str(exc),
                "partial": partial,
                "retryable": True,
            }, ensure_ascii=False) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson")


def _skill_stream_content(data: dict) -> str:
    if "content" in data:
        return str(data["content"])
    if "report" in data:
        return str(data["report"])
    if "quiz" in data:
        return json.dumps(data["quiz"], ensure_ascii=False)
    return json.dumps(data, ensure_ascii=False)


def _chunk_text(content: str, target_size: int = 24):
    text = str(content or "")
    if not text:
        return
    buf = ""
    for token in re_split_keep_space(text):
        buf += token
        if len(buf) >= target_size:
            yield buf
            buf = ""
    if buf:
        yield buf


def re_split_keep_space(text: str) -> list[str]:
    import re
    return [p for p in re.split(r"(\s+)", text) if p]


def _record_latency(path: str, elapsed_ms: float):
    key = None
    if path == "/api/search":
        key = "search"
    elif path == "/api/chat":
        key = "chat"
    if key:
        samples = LATENCY_SAMPLES[key]
        samples.append(elapsed_ms)
        del samples[:-200]


def _p50(samples: list[float]) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    return round(ordered[len(ordered) // 2], 1)


def _kg_to_mindmap(kg_data: dict, course_id: str) -> dict:
    """Convert KG JSON to a frontend KG payload plus tree-compatible fields."""
    nodes = kg_data.get("nodes", [])
    edges = kg_data.get("edges", [])

    if not nodes:
        return {"id": "root", "label": course_id, "nodes": [], "edges": [], "children": []}

    normalized_nodes = _normalize_kg_nodes(nodes)
    normalized_edges = _normalize_kg_edges(edges)
    node_map = {n["id"]: n for n in normalized_nodes}

    children_map: dict[str, list] = {}
    for edge in normalized_edges:
        src = edge.get("source", "")
        children_map.setdefault(src, []).append(edge.get("target", ""))

    targets = {e.get("target") for e in normalized_edges}
    roots = [n for n in normalized_nodes if n.get("id") not in targets]
    if not roots:
        roots = normalized_nodes[:5]

    def build_tree(node_id: str, depth: int = 0) -> dict:
        node = node_map.get(node_id, {})
        result = {
            "id": node_id,
            "label": node.get("name", node_id),
            "depth": node.get("depth", depth),
            "weight": node.get("weight", 1.0),
            "definition": node.get("definition", ""),
            "source_chunks": node.get("source_chunks", []),
        }
        if depth < 3:
            child_ids = children_map.get(node_id, [])
            if child_ids:
                result["children"] = [build_tree(cid, depth + 1) for cid in child_ids[:6]]
        return result

    root_children = [build_tree(r["id"]) for r in roots[:8]]
    return {
        "id": "root",
        "label": course_id,
        "nodes": normalized_nodes,
        "edges": normalized_edges,
        "children": root_children,
    }


def _normalize_kg_nodes(nodes: list[dict]) -> list[dict]:
    normalized = []
    for idx, node in enumerate(nodes):
        node_id = str(node.get("id", node.get("concept_id", f"node_{idx}")))
        chunk_ids = node.get("chunk_ids", [])
        source_chunks = node.get("source_chunks") or [
            {"chunk_id": cid, "source_file": node.get("source_file", ""), "page": node.get("page")}
            for cid in chunk_ids
        ]
        normalized.append({
            "id": node_id,
            "name": node.get("name", node_id),
            "definition": node.get("definition", ""),
            "concept_type": node.get("concept_type", "definition"),
            "depth": int(node.get("depth", 1 if idx else 0)),
            "weight": float(node.get("weight", max(1, len(chunk_ids) or 1))),
            "source_chunks": source_chunks,
            "chunk_ids": chunk_ids,
        })
    return normalized


def _normalize_kg_edges(edges: list[dict]) -> list[dict]:
    allowed = {"is-a", "part-of", "depends-on", "example-of", "related"}
    normalized = []
    for edge in edges:
        rel = str(edge.get("relation", edge.get("relation_type", "related"))).replace("_", "-")
        if rel == "related-to":
            rel = "related"
        if rel == "prerequisite":
            rel = "depends-on"
        if rel not in allowed:
            rel = "related"
        normalized.append({
            "source": edge.get("source", edge.get("from")),
            "target": edge.get("target", edge.get("to")),
            "relation": rel,
        })
    return normalized


# ── Serve frontend ──────────────────────────────────────────────────
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"


@app.get("/", include_in_schema=False)
async def serve_index():
    index = FRONTEND_DIR / "index.html"
    if index.exists():
        return FileResponse(index)
    return HTMLResponse("<h1>nano-NOTEBOOKLM</h1><p>Frontend not found. Run setup first.</p>")


app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR), html=False), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
