"""FastAPI backend for nano-NOTEBOOKLM — serves API + static frontend."""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, File, UploadFile, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AfterValidator, BaseModel, Field, field_validator

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nano_notebooklm.ai.router import ModelRouter
from nano_notebooklm.agents import run_subagent
from nano_notebooklm.agents.formatter import format_response
from nano_notebooklm.kb.store import KBStore
from nano_notebooklm.orchestrator import agent_loop
from nano_notebooklm.orchestrator.engine import Orchestrator
from nano_notebooklm.orchestrator.session_log import SessionLog
from nano_notebooklm.orchestrator import router_intent
from nano_notebooklm.orchestrator.tools import build_default_registry
from nano_notebooklm.ai.openai_backend import OpenAIBackend
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
# review-swarm fix-all v3 #C1: drop wildcard CORS. The previous `allow_origins=["*"]`
# turned every local-backend capability (memory write, ingest, upload, agent stream)
# into a drive-by web API. Default to same-origin localhost; override via
# NANO_NLM_ALLOWED_ORIGINS for non-local deploys (comma-separated, no spaces).
_DEFAULT_ALLOWED_ORIGINS = "http://localhost:8000,http://127.0.0.1:8000"
ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get("NANO_NLM_ALLOWED_ORIGINS", _DEFAULT_ALLOWED_ORIGINS).split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["content-type", "x-request-id"],
)

# ── Upload limits ────────────────────────────────────────────────────
MAX_UPLOAD_SIZE_MB = 50
MAX_UPLOAD_SIZE_BYTES = MAX_UPLOAD_SIZE_MB * 1024 * 1024
ALLOWED_UPLOAD_SUFFIXES = {".pdf", ".pptx", ".docx", ".md", ".txt"}

# review-swarm fix-all v3 #C2: ingest_course must not accept arbitrary local
# directories (`/Users/...` or `/etc/...`). Whitelist roots to artifacts/uploads
# by default; ops can extend via NANO_NLM_INGEST_ROOTS=path1:path2.
def _resolve_ingest_roots() -> list[Path]:
    raw = os.environ.get(
        "NANO_NLM_INGEST_ROOTS",
        str(config.ARTIFACTS_DIR / "uploads"),
    )
    out: list[Path] = []
    for piece in raw.split(":"):
        piece = piece.strip()
        if not piece:
            continue
        try:
            out.append(Path(piece).resolve())
        except Exception:
            continue
    return out


ALLOWED_INGEST_ROOTS: list[Path] = _resolve_ingest_roots()

# review-swarm fix-all v3 #H3: bound the expansion of a ZIP-based upload
# (pptx / docx are zip containers). Defaults are generous for normal academic
# decks but cut a 4GB-output zip-bomb fast.
ZIP_MAX_ENTRIES = int(os.environ.get("NANO_NLM_ZIP_MAX_ENTRIES", "5000"))
ZIP_MAX_UNCOMPRESSED_BYTES = int(os.environ.get(
    "NANO_NLM_ZIP_MAX_UNCOMPRESSED_BYTES",
    str(500 * 1024 * 1024),
))
ZIP_MAX_RATIO = int(os.environ.get("NANO_NLM_ZIP_MAX_RATIO", "100"))


def _check_zip_safety(path: Path, file_size: int) -> None:
    import zipfile
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            if len(infos) > ZIP_MAX_ENTRIES:
                try: path.unlink()
                except OSError: pass
                raise HTTPException(413, f"archive has {len(infos)} entries (>{ZIP_MAX_ENTRIES})")
            total_uncompressed = sum(int(i.file_size) for i in infos)
            if total_uncompressed > ZIP_MAX_UNCOMPRESSED_BYTES:
                try: path.unlink()
                except OSError: pass
                raise HTTPException(
                    413,
                    f"archive expansion {total_uncompressed} bytes exceeds limit",
                )
            if file_size > 0 and (total_uncompressed / max(file_size, 1)) > ZIP_MAX_RATIO:
                try: path.unlink()
                except OSError: pass
                raise HTTPException(
                    413,
                    f"archive compression ratio {total_uncompressed // max(file_size, 1)}× exceeds limit",
                )
    except zipfile.BadZipFile:
        try: path.unlink()
        except OSError: pass
        raise HTTPException(400, "uploaded file is not a valid ZIP archive")


def _validate_ingest_dir(course_dir_str: str) -> Path:
    """Resolve a user-supplied ingest path and require it to live inside one of
    the configured ALLOWED_INGEST_ROOTS. Raises HTTPException(403) on escape so
    the global handler returns the standard `{error, request_id, detail}`
    envelope. Without this check, a single open-CORS POST to /api/ingest can
    drag any server-readable directory into the indexed corpus and exfiltrate
    it via /api/search.
    """
    if not course_dir_str or not isinstance(course_dir_str, str):
        raise HTTPException(400, "course_dir is required")
    try:
        resolved = Path(course_dir_str).resolve()
    except Exception as exc:
        raise HTTPException(400, f"invalid course_dir: {exc}") from None
    if not resolved.exists() or not resolved.is_dir():
        raise HTTPException(404, f"Directory not found: {course_dir_str}")
    for root in ALLOWED_INGEST_ROOTS:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    raise HTTPException(
        403,
        "course_dir is outside allowed ingest roots; configure NANO_NLM_INGEST_ROOTS to extend",
    )

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
    # fix-all v3 #M1: don't surface `str(exc)` to the network — vendor
    # exception messages routinely carry URLs, model IDs, and sometimes
    # credential-shaped tokens. The full traceback is logged with the rid
    # so operators can correlate without leaking it to clients.
    logger.exception("rid=%s unhandled error in %s %s", rid, request.method, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": "internal_error",
            "request_id": rid,
            "detail": "internal error; see server logs for request_id",
        },
    )


# ── Request/Response models (with validation) ───────────────────────
# Round 2.1 review-swarm fix-all v1 #2: course_id values flow into LLM system
# prompts (META_COURSE_ADDENDUM.format) and onto the filesystem
# (artifacts/courses/<id>/, artifacts/uploads/<id>/). Without a character
# constraint a value like `"x\n\nIgnore previous instructions"` would land
# verbatim in the system message, and `/api/upload/{course_id}` would create
# a directory with arbitrary path bytes. Real courses are slug-shaped:
# 15-213, CSE 234, 机器人导论, 模式识别 — restrict to alnum + space + dash +
# underscore + dot + CJK. No slashes, no control chars, no `..`.
COURSE_ID_PATTERN = r"^[A-Za-z0-9_\-. 一-鿿]{1,128}$"


def _ensure_safe_course_id(value):
    """review-swarm fix-all v3 #H1: COURSE_ID_PATTERN allows `.` because
    real course names contain it (`15-213`); but pydantic-core's Rust regex
    engine can't express "no `..` substring" with negative lookahead. So
    the pattern stays permissive and this validator catches the dangerous
    cases — `..`, leading `.`, trailing `.` — that would otherwise let a
    request write into `artifacts/courses/..` or escape the per-course
    namespace.
    """
    if value is None:
        return None
    if ".." in value or value.startswith(".") or value.endswith("."):
        raise ValueError("course_id may not contain '..' or start/end with '.'")
    return value


def _validate_course_id_path(value: str) -> str:
    """For path-param `course_id`: same pattern as the body-field validator,
    but raises HTTPException(400) so the global handler returns the standard
    `{error, request_id, detail}` envelope rather than a Pydantic 422."""
    import re as _re
    if not value or len(value) > 128 or not _re.match(COURSE_ID_PATTERN, value):
        raise HTTPException(400, f"invalid course_id: {value[:40]!r}")
    if ".." in value or value.startswith(".") or value.endswith("."):
        raise HTTPException(400, f"invalid course_id: {value[:40]!r}")
    return value


# Reusable Annotated types so every body model that carries `course_id`
# inherits the same shape + traversal guard. fix-all v3 #H1.
# fix-all v4 #B10: the AfterValidator's `..` / leading-dot / trailing-dot
# rejection cannot be expressed in the JSON-schema regex (pydantic-core's
# Rust regex engine has no lookahead). Surface the extra constraint via
# the field description so generated OpenAPI clients see the real shape.
_COURSE_ID_DESC = (
    "Course identifier. Allowed: alphanumeric, space, dot, dash, underscore, CJK; "
    "1-128 chars. Must not contain '..' or start/end with '.'"
)
OptCourseId = Annotated[
    str | None,
    Field(max_length=128, pattern=COURSE_ID_PATTERN, description=_COURSE_ID_DESC),
    AfterValidator(_ensure_safe_course_id),
]
ReqCourseId = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=COURSE_ID_PATTERN, description=_COURSE_ID_DESC),
    AfterValidator(_ensure_safe_course_id),
]


class ChatRequest(BaseModel):
    model_config = {"extra": "forbid"}
    question: str = Field(..., min_length=1, max_length=4000)
    course_id: OptCourseId = None
    top_k: int = Field(5, ge=1, le=50)
    checked_files: list[str] | None = None
    # Round 3 #R3-2: explicit student language preference. None = legacy
    # behaviour (system prompt's soft "match the user's language" rule
    # applies). zh / en append a strict binding addendum. Anything else is
    # rejected at the Pydantic layer so a stale localStorage value can't
    # smuggle a bogus instruction into the prompt.
    user_lang: Literal["zh", "en"] | None = None

    @field_validator("question")
    @classmethod
    def question_must_not_be_blank(cls, value: str) -> str:
        return _strip_nonempty(value, "question")


class ChatSource(BaseModel):
    chunk_id: str
    text: str
    source_file: str
    location: str
    score: float


class ChunkPayload(BaseModel):
    """One chunk in the /api/chunks/{id} response. `extra="forbid"` so future
    additions are explicit (matches `ChatResponse` discipline)."""
    chunk_id: str
    text: str
    source_file: str
    location: str
    page: int | None = None
    model_config = {"extra": "forbid"}


class ChunkResponse(BaseModel):
    """Schema for /api/chunks/{chunk_id}. `prev`/`next` are None at doc edges
    or for single-chunk docs. `page` mirrors `chunk.page` for the client banner.
    """
    chunk: ChunkPayload
    prev: ChunkPayload | None = None
    next: ChunkPayload | None = None
    source_file: str
    page: int | None = None
    course_id: str
    doc_id: str
    model_config = {"extra": "forbid"}


class ChatResponse(BaseModel):
    """Schema for /api/chat. The four `path` values are constrained by GOAL.md
    Round 2 #1 — anything else (e.g. typo `cross_course` underscore vs hyphen)
    will fail Pydantic serialisation before shipping to the frontend.

    `extra="forbid"` is intentional: when a future skill change adds a new
    sidecar field (e.g. `cross_course_origin` for path=cross-course), we want
    a loud ResponseValidationError in dev rather than silently dropping it
    on the wire. Add the field to this model when extending qa_skill.
    """
    answer: str
    sources: list[ChatSource] = Field(default_factory=list)
    model: str = "fallback"
    tokens_used: int = 0
    # `path` is omitted for the pre-routing #R1 boilerplate response (when
    # checked_files knocks all results to 0). Otherwise must be one of four.
    path: Literal["rag", "general", "translated", "cross-course"] | None = None
    original_query: str | None = None
    translated_query: str | None = None
    general_reason: str | None = None
    filter_empty: bool | None = None
    filter_low_quality: bool | None = None
    cross_course_origin: str | None = None
    model_config = {"extra": "forbid"}


class SearchRequest(BaseModel):
    model_config = {"extra": "forbid"}
    query: str = Field(..., min_length=1, max_length=2000)
    course_id: OptCourseId = None
    top_k: int = Field(5, ge=1, le=50)

    @field_validator("query")
    @classmethod
    def query_must_not_be_blank(cls, value: str) -> str:
        return _strip_nonempty(value, "query")


class NoteRequest(BaseModel):
    model_config = {"extra": "forbid"}
    course_id: ReqCourseId
    topic: str | None = Field(None, max_length=500)
    format: str = Field("markdown", pattern=r"^(markdown|text|html)$")
    user_lang: Literal["zh", "en"] | None = None


class QuizRequest(BaseModel):
    model_config = {"extra": "forbid"}
    course_id: ReqCourseId
    topic: str | None = Field(None, max_length=500)
    num_questions: int = Field(6, ge=1, le=30)
    difficulty: str = Field("medium", pattern=r"^(easy|medium|hard)$")
    user_lang: Literal["zh", "en"] | None = None


class ReportRequest(BaseModel):
    model_config = {"extra": "forbid"}
    course_id: ReqCourseId
    report_type: str = Field("summary", max_length=64)
    include_code: bool = False
    format: str = Field("markdown", pattern=r"^(markdown|text|html)$")
    user_lang: Literal["zh", "en"] | None = None


class IngestRequest(BaseModel):
    model_config = {"extra": "forbid"}
    course_dir: str = Field(..., min_length=1)
    course_id: OptCourseId = None


class ExamAnalysisRequest(BaseModel):
    model_config = {"extra": "forbid"}
    course_id: ReqCourseId


class MemoryUpdate(BaseModel):
    model_config = {"extra": "forbid"}
    key: str = Field(..., min_length=1, max_length=200)
    # fix-all v3 #M4: cap memory value size so a single PUT can't blow up
    # user_memory.json. Larger structured values should go to a dedicated
    # endpoint with its own quota.
    value: Any

    @field_validator("value")
    @classmethod
    def _bound_value(cls, v):
        try:
            payload = json.dumps(v)
        # fix-all v4 #B4: deeply nested dicts blow Python's recursion
        # limit inside json.dumps; without RecursionError catch every
        # such request becomes a 5xx that the unhandled-exception
        # handler swallows. Treat as a validation error instead.
        except RecursionError:
            raise ValueError("value too deeply nested") from None
        except (TypeError, ValueError) as exc:
            raise ValueError(f"value must be JSON-serializable: {exc}") from None
        if len(payload) > 200_000:
            raise ValueError("value exceeds 200KB cap")
        return v


def _validate_memory_payload(payload: dict) -> dict:
    """fix-all v4 #A2: gatekeeper for PUT /api/memory. Previously the
    endpoint took a raw ``dict``, bypassing MemoryUpdate's 200KB cap
    (#M4); an unauthenticated CORS-allowed page could write a multi-MB
    blob and stuff a prompt-injection payload into ``learning_goals``
    (consumed by every system prompt via ``get_context_prompt``).
    """
    try:
        encoded = json.dumps(payload)
    except RecursionError:
        raise HTTPException(400, "memory payload too deeply nested")
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, f"memory payload must be JSON-serialisable: {exc}")
    if len(encoded) > 200_000:
        raise HTTPException(413, "memory payload exceeds 200KB cap")
    return payload


class SubagentRequest(BaseModel):
    model_config = {"extra": "forbid"}
    name: str = Field(..., pattern=r"^(web_research|formatter)$")
    payload: dict[str, Any] = Field(default_factory=dict)


class SessionEntryRequest(BaseModel):
    model_config = {"extra": "forbid"}
    course_id: OptCourseId = None
    kind: str = Field(..., min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)


class AgentRequest(BaseModel):
    model_config = {"extra": "forbid"}
    question: str = Field(..., min_length=1, max_length=4000)
    course_id: OptCourseId = None
    max_turns: int | None = Field(None, ge=1, le=32)
    user_lang: Literal["zh", "en"] | None = None

    @field_validator("question")
    @classmethod
    def question_must_not_be_blank(cls, value: str) -> str:
        return _strip_nonempty(value, "question")


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
        # Round 2 #3: per-course language fingerprint so the frontend dropdown
        # can show 🇨🇳 / 🇺🇸 / 🌐 — let the user see at a glance which language
        # the course materials are in. Lazily computed + cached by router_intent.
        lang = router_intent.get_course_lang(kb, cid) or "en"
        result.append({
            "id": cid,
            "name": meta.get("name", cid),
            "chunks": len(chunks),
            "documents": len(meta.get("documents", [])),
            "lang": lang,
        })
    return {"courses": result}


@app.get("/api/sources/{course_id}", tags=["courses"], summary="List source files for a course")
async def get_sources(course_id: str):
    course_id = _validate_course_id_path(course_id)
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
@app.post("/api/chat", tags=["skills"],
          summary="RAG chat with source citations",
          response_model=ChatResponse, response_model_exclude_none=True)
async def chat(req: ChatRequest):
    result = await orchestrator.skills["qa"].execute({
        "question": req.question,
        "course_filter": req.course_id,
        "top_k": req.top_k,
        "checked_files": req.checked_files,
        "user_lang": req.user_lang,
    })
    if not result.success:
        raise HTTPException(status_code=502, detail=result.error or "qa skill failed")
    data = dict(result.data)
    if "answer" in data:
        data["answer"] = format_response(str(data["answer"]))
    session_log.append(req.course_id, "question", {
        "question": req.question,
        "answer": data.get("answer", ""),
        "path": data.get("path"),
        "original_query": data.get("original_query"),
        "translated_query": data.get("translated_query"),
    })
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
        "user_lang": req.user_lang,
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
        "user_lang": req.user_lang,
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
        "user_lang": req.user_lang,
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
        "user_lang": req.user_lang,
    }, req.course_id, "notes")


@app.post("/api/quiz/stream", tags=["skills"], summary="Stream a practice quiz")
async def stream_quiz(req: QuizRequest):
    return _stream_response("quiz_generator", {
        "course_id": req.course_id,
        "topic": req.topic,
        "num_questions": req.num_questions,
        "difficulty": req.difficulty,
        "user_lang": req.user_lang,
    }, req.course_id, "quiz")


@app.post("/api/report/stream", tags=["skills"], summary="Stream a course report")
async def stream_report(req: ReportRequest):
    return _stream_response("report_generator", {
        "course_id": req.course_id,
        "report_type": req.report_type,
        "include_code": req.include_code,
        "format": req.format,
        "user_lang": req.user_lang,
    }, req.course_id, "report")


@app.post("/api/subagent", tags=["agents"], summary="Run a stateless subagent")
async def run_subagent_endpoint(req: SubagentRequest):
    return await run_subagent(req.name, req.payload)


# Module-level factory so tests can monkeypatch a fake LLM stream without
# touching the real OpenAI client. Production path: returns the
# chat.completions bridge bound to the configured OpenAI backend.
def _default_agent_llm_stream_factory(backend: OpenAIBackend):
    return agent_loop.make_chat_completions_stream(backend)


_agent_llm_stream_factory = _default_agent_llm_stream_factory

# Default registry (lock_course_id=None) — used when a request comes in
# without a course scope. Course-scoped requests build a per-request
# registry so read_chunk can refuse cross-course access. fix-all v3 #H4.
_AGENT_REGISTRY = build_default_registry(kb, orchestrator)


@app.post("/api/agent/stream", tags=["agents"],
          summary="Multi-turn tool-calling agent (NDJSON event stream)")
async def agent_stream(req: AgentRequest, request: Request):
    backend = router.backends.get("openai")
    if backend is None:
        raise HTTPException(
            status_code=503,
            detail="agent endpoint requires an OpenAI-compatible backend (set OPENAI_API_KEY)",
        )

    course_names = orchestrator.list_courses()
    llm_stream = _agent_llm_stream_factory(backend)
    max_turns = req.max_turns or agent_loop.DEFAULT_MAX_TURNS
    rid = getattr(request.state, "request_id", "?")
    registry = (
        build_default_registry(kb, orchestrator, lock_course_id=req.course_id)
        if req.course_id else _AGENT_REGISTRY
    )

    async def events():
        partial_answer = ""
        try:
            async for evt in agent_loop.run_agent(
                user_question=req.question,
                registry=registry,
                course_id=req.course_id,
                course_names=course_names,
                max_turns=max_turns,
                llm_stream=llm_stream,
                user_lang=req.user_lang,
            ):
                etype = evt.get("type")
                if etype == "text":
                    partial_answer += evt.get("delta", "")
                elif etype == "tool_call":
                    logger.info("rid=%s agent tool_call name=%s course=%s",
                                rid, evt.get("name"), req.course_id)
                    session_log.append(req.course_id, "agent_tool", {
                        "name": evt.get("name"),
                        "call_id": evt.get("call_id"),
                    })
                elif etype == "done":
                    if evt.get("max_turns_hit"):
                        logger.warning("rid=%s agent max_turns_hit turns=%s",
                                       rid, evt.get("turns"))
                    if evt.get("budget_hit"):
                        logger.warning("rid=%s agent budget_hit turns=%s",
                                       rid, evt.get("turns"))
                    session_log.append(req.course_id, "agent", {
                        "question": req.question,
                        "answer": evt.get("answer", ""),
                        "turns": evt.get("turns", 0),
                        "max_turns_hit": evt.get("max_turns_hit", False),
                        "budget_hit": evt.get("budget_hit", False),
                    })
                elif etype == "error":
                    session_log.append(req.course_id, "agent_error", {
                        "question": req.question,
                        "error": evt.get("error"),
                    })
                yield json.dumps(evt, ensure_ascii=False) + "\n"
        except Exception:
            logger.exception("rid=%s agent endpoint failed", rid)
            yield json.dumps({
                "type": "error",
                "error": "endpoint_error",
                "partial": partial_answer,
                "retryable": True,
            }, ensure_ascii=False) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson")


@app.post("/api/mindmap/{course_id}", tags=["skills"], summary="Get or generate knowledge graph as mindmap tree")
async def get_mindmap(course_id: str):
    course_id = _validate_course_id_path(course_id)
    kg_path = config.ARTIFACTS_DIR / "courses" / course_id / "knowledge_graph.json"

    # fix-all v4 #A8: the cached path serves the existing
    # knowledge_graph.json without holding a lock. Previously the entire
    # body sat inside `_edit_lock_for(course_id)`, so a peer's first-
    # time generation (~30-90 s) blocked every concurrent GET as well as
    # every concurrent /edit. Now only the actual extract_from_chunks
    # call serialises (#H8 still satisfied — two concurrent first-time
    # requests cannot both write knowledge_graph.json).
    if kg_path.exists():
        try:
            data = json.loads(kg_path.read_text())
            data = _overlay_user_edits(data, course_id)
            return _kg_to_mindmap(data, course_id)
        except json.JSONDecodeError:
            logger.warning("Corrupt knowledge_graph.json for %s, regenerating", course_id)

    async with _edit_lock_for(course_id):
        # Re-check inside the lock — a peer may have generated while we
        # were queued, in which case we just serve the freshly-written
        # cache instead of duplicating extraction work.
        if kg_path.exists():
            try:
                data = json.loads(kg_path.read_text())
                data = _overlay_user_edits(data, course_id)
                return _kg_to_mindmap(data, course_id)
            except json.JSONDecodeError:
                logger.warning("Corrupt knowledge_graph.json for %s after race, regenerating", course_id)

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
        data = _overlay_user_edits(data, course_id)
        return _kg_to_mindmap(data, course_id)


# ── M3: editable mind map ────────────────────────────────────────────
# A student's edits live in `mindmap_edits.json` separate from the
# system-extracted KG, so re-running extraction never clobbers their
# notes. The endpoint append-records ops; GET replays them on top of the
# system KG via `_overlay_user_edits` → `apply_edit_ops`.

class MindmapEditOp(BaseModel):
    """One overlay op. All fields are optional because each op type uses a
    subset; the overlay function tolerates extras and unknown ops (logged,
    skipped) so a future client schema can add new op kinds without losing
    the student's other edits in the same batch."""
    op: Literal["add_node", "update_node", "delete_node", "add_edge", "delete_edge"]
    id: str | None = Field(None, max_length=200)
    label: str | None = Field(None, max_length=200)
    definition: str | None = Field(None, max_length=2000)
    parent_id: str | None = Field(None, max_length=200)
    source: str | None = Field(None, max_length=200)
    target: str | None = Field(None, max_length=200)
    relation: str | None = Field(None, max_length=40)
    model_config = {"extra": "forbid"}


class MindmapEditRequest(BaseModel):
    model_config = {"extra": "forbid"}
    ops: list[MindmapEditOp] = Field(..., min_length=1, max_length=50)


_ALLOWED_RELATIONS = {"is-a", "part-of", "depends-on", "example-of", "related"}


def _coerce_str(value: object) -> str:
    """F17: coerce a disk-loaded op field to a stripped string. A
    hand-edited mindmap_edits.json with `id: 123` (int) would otherwise
    crash the str-only `.strip()` chain inside apply_edit_ops_with_results.
    """
    if value is None:
        return ""
    return str(value).strip()


def apply_edit_ops_with_results(
    kg_data: dict, ops: list[dict],
) -> tuple[dict, list[dict]]:
    """Apply student-edit ops on a KG dict and return both the overlaid
    payload and a per-op outcome list.

    Each `op_results[i]` carries `{op, status, reason}` so the edit
    endpoint can surface skipped ops to the client (F7) instead of
    silently dropping them. Status values:
      - "applied"               op fully applied
      - "applied_with_warning"  op applied but one of its side-effects
                                was dropped (e.g. add_node landed but its
                                parent_id pointed to a missing node, so
                                we omitted the dangling part-of edge)
      - "skipped"               op had no effect; reason explains why

    The function never mutates `kg_data`. Unknown ops are skipped, never
    raised — one bad op shouldn't take out the rest of a batch.

    F5 (review-swarm): add_node / add_edge / update_node now require their
    referenced ids to exist in the KG. Dangling endpoints are dropped
    rather than persisted.
    F13 (review-swarm): delete_node refuses to remove a `concept_type ==
    "root"` node — direct API calls can't permanently destroy the
    course-card view.
    F17 (review-swarm): all op fields are coerced via `_coerce_str` so a
    hand-edited disk file with non-string ids doesn't AttributeError.
    """
    nodes_by_id: dict[str, dict] = {}
    for n in kg_data.get("nodes", []):
        nid = n.get("id") or n.get("concept_id")
        if nid:
            nodes_by_id[nid] = dict(n)
    edges = [dict(e) for e in kg_data.get("edges", [])]
    deleted_ids: set[str] = set()
    op_results: list[dict] = []

    def _edge_key(e: dict) -> tuple[str, str, str]:
        return (str(e.get("source", "")), str(e.get("target", "")),
                str(e.get("relation", e.get("relation_type", ""))))

    def _record(op_dict: dict, status: str, reason: str | None = None) -> None:
        entry = {"op": op_dict.get("op"), "status": status}
        if reason:
            entry["reason"] = reason
        op_results.append(entry)

    for op in ops or []:
        if not isinstance(op, dict):
            op_results.append({"op": None, "status": "skipped",
                               "reason": "op is not a dict"})
            continue
        kind = op.get("op")
        if kind == "add_node":
            nid = _coerce_str(op.get("id"))
            if not nid:
                logger.warning("mindmap edit add_node missing id; skipped")
                _record(op, "skipped", "add_node requires id")
                continue
            label = _coerce_str(op.get("label")) or nid
            existing = nodes_by_id.get(nid, {})
            nodes_by_id[nid] = {
                **existing,
                "id": nid,
                "name": label,
                "definition": (op.get("definition") or existing.get("definition", "")),
                "depth": existing.get("depth", 2),
                "concept_type": existing.get("concept_type", "user_added"),
                "weight": existing.get("weight", 2.0),
                "user_added": True,
            }
            parent_id = _coerce_str(op.get("parent_id"))
            if parent_id:
                if parent_id not in nodes_by_id:
                    # F5: don't persist a dangling part-of edge.
                    logger.warning(
                        "mindmap edit add_node %s parent_id=%r not found; "
                        "node added without parent edge", nid, parent_id,
                    )
                    _record(op, "applied_with_warning",
                            f"parent_id {parent_id!r} not found; "
                            "node added but parent edge omitted")
                    continue
                key = (nid, parent_id, "part-of")
                if not any(_edge_key(e) == key for e in edges):
                    edges.append({
                        "source": nid, "target": parent_id,
                        "relation": "part-of", "user_added": True,
                    })
            _record(op, "applied")
        elif kind == "update_node":
            nid = _coerce_str(op.get("id"))
            if not nid or nid not in nodes_by_id:
                logger.warning("mindmap edit update_node unknown id %r; skipped", nid)
                _record(op, "skipped",
                        f"update_node id {nid!r} not found in graph")
                continue
            existing = nodes_by_id[nid]
            patch = {}
            if op.get("label") is not None:
                patch["name"] = _coerce_str(op["label"]) or existing.get("name", nid)
            if op.get("definition") is not None:
                patch["definition"] = str(op["definition"])
            if patch:
                nodes_by_id[nid] = {**existing, **patch, "user_edited": True}
                _record(op, "applied")
            else:
                _record(op, "skipped", "no fields to update")
        elif kind == "delete_node":
            nid = _coerce_str(op.get("id"))
            if not nid:
                _record(op, "skipped", "delete_node requires id")
                continue
            existing = nodes_by_id.get(nid)
            if existing is None:
                _record(op, "skipped", f"id {nid!r} not found")
                continue
            # F13: refuse to delete the course root via the API. Frontend
            # already blocks this with window.alert; this is the
            # backend-side belt-and-suspenders.
            if str(existing.get("concept_type", "")).lower() == "root":
                logger.warning(
                    "mindmap edit refused delete_node on root %r", nid,
                )
                _record(op, "skipped", "root nodes cannot be deleted")
                continue
            deleted_ids.add(nid)
            _record(op, "applied")
        elif kind == "add_edge":
            src = _coerce_str(op.get("source"))
            tgt = _coerce_str(op.get("target"))
            rel = _coerce_str(op.get("relation")) or "related"
            rel = rel.replace("_", "-")
            if rel not in _ALLOWED_RELATIONS:
                rel = "related"
            if not src or not tgt:
                logger.warning("mindmap edit add_edge missing endpoints; skipped")
                _record(op, "skipped", "add_edge requires source and target")
                continue
            # F5: both endpoints must already exist (or be added in this
            # same batch — already in nodes_by_id since add_node inserts
            # eagerly above).
            missing = []
            if src not in nodes_by_id and src not in deleted_ids:
                missing.append(f"source {src!r}")
            if tgt not in nodes_by_id and tgt not in deleted_ids:
                missing.append(f"target {tgt!r}")
            if missing:
                logger.warning(
                    "mindmap edit add_edge endpoints not found: %s",
                    "; ".join(missing),
                )
                _record(op, "skipped", "; ".join(missing) + " not found")
                continue
            key = (src, tgt, rel)
            if not any(_edge_key(e) == key for e in edges):
                edges.append({
                    "source": src, "target": tgt, "relation": rel, "user_added": True,
                })
            _record(op, "applied")
        elif kind == "delete_edge":
            src = _coerce_str(op.get("source"))
            tgt = _coerce_str(op.get("target"))
            rel = op.get("relation")
            rel_str = _coerce_str(rel) if rel is not None else None
            before = len(edges)
            edges = [
                e for e in edges
                if not (
                    str(e.get("source", "")) == src
                    and str(e.get("target", "")) == tgt
                    and (rel_str is None
                         or str(e.get("relation", e.get("relation_type", ""))) == rel_str)
                )
            ]
            if len(edges) < before:
                _record(op, "applied")
            else:
                _record(op, "skipped", "no matching edge")
        else:
            logger.warning("mindmap edit unknown op %r; skipped", kind)
            _record(op, "skipped", f"unknown op {kind!r}")

    # Apply node deletions: drop the nodes themselves and any edge that
    # touches them so the overlay never returns dangling edges.
    if deleted_ids:
        for nid in deleted_ids:
            nodes_by_id.pop(nid, None)
        edges = [
            e for e in edges
            if str(e.get("source", "")) not in deleted_ids
            and str(e.get("target", "")) not in deleted_ids
        ]

    return {"nodes": list(nodes_by_id.values()), "edges": edges}, op_results


def apply_edit_ops(kg_data: dict, ops: list[dict]) -> dict:
    """Backwards-compat wrapper used by `_overlay_user_edits` (whose
    callers don't need the per-op outcome list). The discipline lives in
    `apply_edit_ops_with_results`; this just throws the results away.
    """
    payload, _ = apply_edit_ops_with_results(kg_data, ops)
    return payload


def _edits_path(course_id: str) -> Path:
    return config.ARTIFACTS_DIR / "courses" / course_id / "mindmap_edits.json"


def _load_edits(course_id: str) -> list[dict]:
    p = _edits_path(course_id)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning("Corrupt mindmap_edits.json for %s; ignoring", course_id)
        return []
    ops = data.get("ops") if isinstance(data, dict) else None
    return ops if isinstance(ops, list) else []


def _save_edits(course_id: str, ops: list[dict]) -> None:
    """F2 + fix-all v3 #H9: atomic + durable write.

    `os.replace` is atomic on POSIX but only durable once the file's data
    AND the parent directory's rename entry have been fsynced. Without
    those, a crash/power loss after the endpoint returned `ok: true` can
    still lose the edit. We fsync both before returning.
    """
    p = _edits_path(course_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    payload = json.dumps({"version": 1, "ops": ops}, ensure_ascii=False, indent=2).encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, p)
    # Parent-dir fsync — Linux/macOS only; ignored on filesystems that
    # don't support it (e.g., some Windows mounts).
    try:
        dir_fd = os.open(str(p.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


# F2: per-course asyncio.Lock so two concurrent POST /edit requests can't
# read-modify-write race against each other. Single-user tool, but the
# assistant + UI button can race in the same browser session.
_EDIT_LOCKS: dict[str, "asyncio.Lock"] = {}


def _edit_lock_for(course_id: str) -> "asyncio.Lock":
    import asyncio as _asyncio
    lock = _EDIT_LOCKS.get(course_id)
    if lock is None:
        lock = _asyncio.Lock()
        _EDIT_LOCKS[course_id] = lock
    return lock


def _overlay_user_edits(kg_data: dict, course_id: str) -> dict:
    """Apply persisted student edits on top of a freshly-loaded KG."""
    ops = _load_edits(course_id)
    if not ops:
        return kg_data
    return apply_edit_ops(kg_data, ops)


@app.post("/api/mindmap/{course_id}/edit", tags=["skills"],
          summary="Apply student edits (add/update/delete nodes & edges) to the mindmap")
async def edit_mindmap(course_id: str, req: MindmapEditRequest):
    course_id = _validate_course_id_path(course_id)
    kg_path = config.ARTIFACTS_DIR / "courses" / course_id / "knowledge_graph.json"
    if not kg_path.exists():
        raise HTTPException(404, f"No knowledge graph for course '{course_id}'")
    new_ops = [op.model_dump(exclude_none=True) for op in req.ops]
    # F7: compute per-op outcomes by replaying the FULL op log against the
    # current KG, then surface only the slice for this batch. That way the
    # client sees exactly which of its ops applied / were skipped / had a
    # warning. Replaying the full log keeps semantics consistent with the
    # GET overlay path.
    # F2: serialize concurrent edit requests for the same course so the
    # load+append+save sequence below isn't subject to last-write-wins.
    async with _edit_lock_for(course_id):
        existing_ops = _load_edits(course_id)
        try:
            kg_data = json.loads(kg_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            kg_data = {"nodes": [], "edges": []}
        all_ops = existing_ops + new_ops
        _, all_results = apply_edit_ops_with_results(kg_data, all_ops)
        new_results = all_results[len(existing_ops):]
        _save_edits(course_id, all_ops)
    return {
        "ok": True,
        "ops_applied": sum(1 for r in new_results if r["status"] != "skipped"),
        "ops_skipped": sum(1 for r in new_results if r["status"] == "skipped"),
        "total_ops": len(all_ops),
        "op_results": new_results,
    }


# ── R3-3: mindmap node deep-dive (agent stream) ─────────────────────
# Click a concept on the mindmap → fire `agent_loop.run_agent` against a
# strict subset of tools (search_kb + read_chunk only — no generate_note,
# no list_courses), capped at 4 turns, with the EXPLAIN_NODE_SYSTEM
# persona injected into the user message. NDJSON event vocabulary is
# identical to /api/agent/stream so the frontend can reuse the renderer.

class NodeExplainRequest(BaseModel):
    model_config = {"extra": "forbid"}
    node_id: str = Field(..., min_length=1, max_length=200)


_EXPLAIN_NODE_MAX_TURNS = 4
_EXPLAIN_NODE_TOOL_WHITELIST = ("search_kb", "read_chunk")


def _build_explain_node_registry(course_id: str | None = None):
    """Subset registry — strict 2-tool whitelist for explain-node.

    Hand-built rather than `build_default_registry` minus filter so a
    future tool added to the default registry can't silently leak into
    explain-node and let the agent write notes / list other courses
    while pretending to be a 'just explain' affordance.

    fix-all v4 #A1: forward ``course_id`` as ``lock_course_id`` so the
    explain-node agent can't read or search chunks from sibling courses.
    The path-param scope is the only thing the user actually authorised.
    """
    from nano_notebooklm.orchestrator.agent_tools import ToolRegistry
    from nano_notebooklm.orchestrator.tools.search_kb import build_search_kb
    from nano_notebooklm.orchestrator.tools.read_chunk import build_read_chunk
    reg = ToolRegistry()
    reg.register(build_search_kb(kb, orchestrator, lock_course_id=course_id))
    reg.register(build_read_chunk(kb, lock_course_id=course_id))
    return reg


@app.post("/api/mindmap/{course_id}/explain-node", tags=["skills"],
          summary="Stream a 5-line explanation + 3 mini quiz for one mindmap node")
async def explain_mindmap_node(course_id: str, req: NodeExplainRequest, request: Request):
    course_id = _validate_course_id_path(course_id)
    kg_path = config.ARTIFACTS_DIR / "courses" / course_id / "knowledge_graph.json"
    if not kg_path.exists():
        raise HTTPException(404, f"No knowledge graph for course '{course_id}'")
    try:
        kg_data = json.loads(kg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise HTTPException(500, "knowledge_graph_corrupt")
    kg_data = _overlay_user_edits(kg_data, course_id)

    target_node = next(
        (n for n in kg_data.get("nodes", [])
         if str(n.get("id") or n.get("concept_id") or "") == req.node_id),
        None,
    )
    if target_node is None:
        raise HTTPException(404, f"node_id {req.node_id!r} not found in mindmap")

    backend = router.backends.get("openai")
    if backend is None:
        raise HTTPException(
            status_code=503,
            detail="explain-node requires an OpenAI-compatible backend (set OPENAI_API_KEY)",
        )

    from nano_notebooklm.ai import prompt_templates as _prompts
    concept_name = str(target_node.get("name") or req.node_id)
    concept_def = str(target_node.get("definition") or "(none)")
    # We don't extend agent_loop.compose_system_prompt with a per-call
    # override (that would race with R3-2's user_lang kwarg) — instead we
    # prepend the explain-node persona as the first paragraph of the
    # user message so it dominates the standard agent system prompt.
    user_question = (
        _prompts.EXPLAIN_NODE_SYSTEM
        + "\n\n"
        + _prompts.EXPLAIN_NODE_PROMPT.format(
            concept_name=concept_name,
            course_id=course_id,
            concept_definition=concept_def,
        )
    )

    explain_registry = _build_explain_node_registry(course_id=course_id)
    course_names = orchestrator.list_courses()
    llm_stream = _agent_llm_stream_factory(backend)
    rid = getattr(request.state, "request_id", "?")

    async def events():
        partial_answer = ""
        try:
            async for evt in agent_loop.run_agent(
                user_question=user_question,
                registry=explain_registry,
                course_id=course_id,
                course_names=course_names,
                max_turns=_EXPLAIN_NODE_MAX_TURNS,
                llm_stream=llm_stream,
            ):
                etype = evt.get("type")
                if etype == "text":
                    partial_answer += evt.get("delta", "")
                elif etype == "tool_call":
                    logger.info(
                        "rid=%s explain-node tool_call name=%s course=%s node=%s",
                        rid, evt.get("name"), course_id, req.node_id,
                    )
                elif etype == "done":
                    if evt.get("max_turns_hit"):
                        logger.info(
                            "rid=%s explain-node max_turns_hit turns=%s",
                            rid, evt.get("turns"),
                        )
                    session_log.append(course_id, "mindmap_explain_node", {
                        "node_id": req.node_id,
                        "concept_name": concept_name,
                        "turns": evt.get("turns", 0),
                        "max_turns_hit": evt.get("max_turns_hit", False),
                        "budget_hit": evt.get("budget_hit", False),
                    })
                yield json.dumps(evt, ensure_ascii=False) + "\n"
        except Exception:
            logger.exception("rid=%s explain-node endpoint failed", rid)
            yield json.dumps({
                "type": "error",
                "error": "endpoint_error",
                "partial": partial_answer,
                "retryable": True,
            }, ensure_ascii=False) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson")


# ── Ingest / upload ──────────────────────────────────────────────────
@app.post("/api/ingest", tags=["ingest"], summary="Ingest a course directory")
async def ingest_course(req: IngestRequest):
    course_dir = _validate_ingest_dir(req.course_dir)

    cid = req.course_id or course_dir.name
    # fix-all v4 #B3: when course_id is omitted we fall back to the
    # resolved directory's basename. That basename comes from the
    # filesystem (whatever the operator has under ALLOWED_INGEST_ROOTS)
    # and was previously written verbatim into artifacts/courses/<cid>/
    # and into LLM system prompts via META_COURSE_ADDENDUM. Validate it
    # with the same regex + traversal guard the body-field uses.
    cid = _validate_course_id_path(cid)
    # Clear lang cache both before and after build so any chat request that
    # arrives during the rebuild window sees a freshly recomputed fingerprint
    # rather than a stale one from the previous corpus.
    router_intent.clear_lang_cache(cid)
    # fix-all v4 #A6 + #A7: same off-loop pattern as /api/upload — heavy
    # CPU and disk I/O on a worker thread so the asyncio loop stays free.
    import asyncio as _asyncio
    course = await _asyncio.to_thread(kb.ingest_course, str(course_dir), cid)
    await _asyncio.to_thread(kb.build_index, cid)
    router_intent.clear_lang_cache(cid)
    chunks = kb.get_chunks(cid)
    return {
        "course_id": cid,
        "chunks": len(chunks),
        "documents": len(course.documents),
    }


@app.post("/api/upload/{course_id}", tags=["ingest"], summary="Upload files to a course and index")
async def upload_files(course_id: str, files: Annotated[list[UploadFile], File(...)]):
    course_id = _validate_course_id_path(course_id)
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
        # review-swarm fix-all v3 #H2: stream the upload body in 64KB chunks
        # and abort as soon as we exceed the cap. Previously
        # `content = await f.read()` loaded the entire body into memory
        # before the size check — a single multi-GB body could OOM the
        # worker before the 50MB limit ever ran.
        chunk_size = 64 * 1024
        written = 0
        # fix-all v4 #A7: keep `await f.read(chunk_size)` (already async,
        # off-loop) but push the synchronous file write to a worker
        # thread so 800 sync writes for a 50MB upload don't block the
        # asyncio loop and starve concurrent endpoints.
        import asyncio as _asyncio
        try:
            out = open(dest, "wb")
            try:
                while True:
                    chunk = await f.read(chunk_size)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_UPLOAD_SIZE_BYTES:
                        out.close()
                        try: dest.unlink()
                        except OSError: pass
                        raise HTTPException(
                            413,
                            f"File '{safe_name}' exceeds limit of {MAX_UPLOAD_SIZE_MB}MB",
                        )
                    await _asyncio.to_thread(out.write, chunk)
            finally:
                if not out.closed:
                    out.close()
        except HTTPException:
            raise
        except Exception:
            try: dest.unlink()
            except OSError: pass
            raise
        # fix-all v3 #H3: ZIP-based formats (.pptx/.docx) get a decompression
        # bomb sanity check before extractors run.
        if suffix in (".pptx", ".docx"):
            _check_zip_safety(dest, written)
        saved += 1

    if saved == 0:
        raise HTTPException(400, "No valid files saved")

    router_intent.clear_lang_cache(course_id)
    # fix-all v4 #A6 + #A7: ingest + build_index can take minutes for the
    # full corpus (PDF parse loop + sentence-transformers embedding +
    # FAISS/BM25 build). Run on a worker thread so the asyncio loop
    # doesn't stall and other requests stay responsive.
    import asyncio as _asyncio
    course = await _asyncio.to_thread(kb.ingest_course, str(upload_dir), course_id)
    await _asyncio.to_thread(kb.build_index, course_id)
    router_intent.clear_lang_cache(course_id)
    chunks = kb.get_chunks(course_id)
    return {
        "course_id": course_id,
        "files": saved,
        "chunks": len(chunks),
        "documents": len(course.documents),
    }


# ── Chunks (Reader content) ──────────────────────────────────────────
@app.get("/api/chunks/{chunk_id}", tags=["learning"],
         summary="Fetch a single chunk plus its in-document neighbors",
         response_model=ChunkResponse, response_model_exclude_none=True)
async def get_chunk(chunk_id: str):
    """Round 2.2: power the Reader's real-content rendering. Given a chunk_id
    (delivered as part of a citation in /api/chat sources), return the chunk's
    text + the immediately preceding and following chunks from the same
    document, plus the source file / page / course id so the Reader can
    render a banner and scroll to the highlight without faking it against
    `READER_DOC` defaults.

    Lookup strategy (review-swarm fix-all v2 #1, #2):
      1. Try `kb.find_chunk(chunk_id)` — O(1) when KB indices have been built
         (i.e., after any prior /api/search or /api/chat call).
      2. If the chunk_id index isn't populated yet, do a single course scan,
         capture the matching course's chunk list, and reuse it for the
         neighbor sweep — no second `kb.get_chunks(target_course)` call.

    Neighbor ordering: same-doc chunks sorted by `page` (None last) then
    `chunk_id` for stable ordering — matches the on-disk extraction order.
    """
    if not chunk_id or len(chunk_id) > 256:
        raise HTTPException(400, "invalid chunk_id")

    target = None
    target_course = None
    course_chunks: list = []  # captured during the scan, reused for neighbors

    fast = kb.find_chunk(chunk_id)
    if fast is not None:
        target = fast
        target_course = fast.course_id
        course_chunks = kb.get_chunks(target_course)
    else:
        for cid in orchestrator.list_courses():
            chunks = kb.get_chunks(cid)
            for c in chunks:
                if c.chunk_id == chunk_id:
                    target = c
                    target_course = cid
                    course_chunks = chunks  # reuse — fix-all v1 F2 dedupe
                    break
            if target is not None:
                break

    if target is None:
        logger.info("chunks.miss chunk=%s scanned=%d courses",
                    chunk_id, len(orchestrator.list_courses()))
        raise HTTPException(404, f"chunk not found: {chunk_id}")

    same_doc = [c for c in course_chunks if c.doc_id == target.doc_id]
    same_doc.sort(key=lambda c: (c.page if c.page is not None else 10**9, c.chunk_id))
    idx = next((i for i, c in enumerate(same_doc) if c.chunk_id == chunk_id), -1)
    prev_chunk = same_doc[idx - 1] if idx > 0 else None
    next_chunk = same_doc[idx + 1] if 0 <= idx < len(same_doc) - 1 else None

    def _serialize(c):
        if c is None:
            return None
        return {
            "chunk_id": c.chunk_id,
            "text": c.text,
            "source_file": c.source_file,
            "location": c.location,
            "page": c.page,
        }

    logger.info("chunks.fetch course=%s chunk=%s doc=%s page=%s",
                target_course, chunk_id, target.doc_id, target.page)
    return {
        "chunk": _serialize(target),
        "prev": _serialize(prev_chunk),
        "next": _serialize(next_chunk),
        "source_file": target.source_file,
        "page": target.page,
        "course_id": target_course,
        "doc_id": target.doc_id,
    }


# ── Mastery ──────────────────────────────────────────────────────────
@app.get("/api/mastery/{course_id}", tags=["learning"], summary="Get mastery scores and weak areas")
async def get_mastery(course_id: str):
    course_id = _validate_course_id_path(course_id)
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
    # fix-all v4 #A2: re-uses MemoryUpdate's 200KB cap discipline. Body
    # is still a free-form dict (FastAPI auto-parses JSON body into a
    # dict for `data: dict`) but now goes through the size + recursion
    # validator before save_memory writes to disk.
    save_memory(_validate_memory_payload(data))
    return {"ok": True}


@app.get("/api/session-log", tags=["learning"], summary="List daily session log entries")
async def list_session_log():
    return {"days": session_log.list_grouped()}


@app.post("/api/session-log", tags=["learning"], summary="Append a session log entry")
async def append_session_log(req: SessionEntryRequest):
    return session_log.append(req.course_id, req.kind, req.payload)


# ── Helper ───────────────────────────────────────────────────────────
def _stream_response(skill_name: str, params: dict, course_id: str, kind: str) -> StreamingResponse:
    """Round 2 #5: real streaming for notes / report (text outputs).

    For skills that expose ``prepare_inputs``, build the LLM inputs without
    invoking the skill, then pipe `router.complete_stream` deltas straight
    into NDJSON events — first-token latency drops from ~13s to <1.5s.

    Quiz keeps the pseudo-stream path because it returns structured JSON; a
    half-streamed JSON body would be unparseable mid-flight."""
    skill = orchestrator.skills.get(skill_name)
    use_real_stream = (
        kind in {"notes", "report"}
        and skill is not None
        and hasattr(skill, "prepare_inputs")
    )

    async def real_stream_events():
        partial = ""
        try:
            prepared = skill.prepare_inputs(params)
            if prepared is None:
                raise RuntimeError(f"{kind} generation failed: missing inputs (course/topic)")
            async for delta in router.complete_stream(
                prepared["prompt"],
                task_type=prepared["task_type"],
                system=prepared["system"],
                temperature=prepared["temperature"],
                max_tokens=prepared["max_tokens"],
            ):
                partial += delta
                yield json.dumps({"type": "chunk", "chunk": delta, "partial": partial},
                                 ensure_ascii=False) + "\n"
            content = format_response(partial)
            session_log.append(course_id, "generation",
                               {"kind": kind, "streamed": True, "real": True})
            yield json.dumps({"type": "done", "content": content},
                             ensure_ascii=False) + "\n"
        except Exception:
            # fix-all v4 #A3: don't ship str(exc) to the client. Same
            # discipline as the global 5xx handler — log the trace, return
            # a stable code. Preserves partial buffer for the retry UX.
            logger.exception("real_stream_events kind=%s failed", kind)
            yield json.dumps({
                "type": "error",
                "error": "stream_failed",
                "partial": partial,
                "retryable": True,
            }, ensure_ascii=False) + "\n"

    async def pseudo_stream_events():
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
                yield json.dumps({"type": "chunk", "chunk": chunk, "partial": partial},
                                 ensure_ascii=False) + "\n"
            session_log.append(course_id, "generation", {"kind": kind, "streamed": True})
            yield json.dumps({"type": "done", "content": content},
                             ensure_ascii=False) + "\n"
        except Exception:
            logger.exception("pseudo_stream_events kind=%s failed", kind)
            yield json.dumps({
                "type": "error",
                "error": "stream_failed",
                "partial": partial,
                "retryable": True,
            }, ensure_ascii=False) + "\n"

    events = real_stream_events if use_real_stream else pseudo_stream_events
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
    """Convert KG JSON to a frontend mindmap payload.

    M1 (2026-05-06): root selection comes from the explicit depth=0
    "root" concept persisted by the two-stage extractor. The legacy
    "in-degree=0" heuristic routinely picked orphan leaves as roots and
    is dropped here. Legacy KG files (Round 1, no explicit root) still
    work via the fallback branch below.
    """
    nodes = kg_data.get("nodes", [])
    edges = kg_data.get("edges", [])

    if not nodes:
        return {
            "id": "root",
            "label": course_id,
            "nodes": [],
            "edges": [],
            "children": [],
        }

    normalized_nodes = _normalize_kg_nodes(nodes)
    normalized_edges = _normalize_kg_edges(edges)
    node_map = {n["id"]: n for n in normalized_nodes}

    # part-of edges in our schema point child → parent, so children of a
    # node are the *sources* of inbound part-of edges to it. Other relation
    # types still get added in source→target direction so the legacy radial
    # walk doesn't drop them.
    children_map: dict[str, list[str]] = {}
    for edge in normalized_edges:
        src = edge.get("source", "")
        tgt = edge.get("target", "")
        rel = edge.get("relation", "")
        if not src or not tgt:
            continue
        if rel == "part-of":
            children_map.setdefault(tgt, []).append(src)
        else:
            children_map.setdefault(src, []).append(tgt)

    # F15 (review-swarm): accept either signal as evidence of root, not
    # both. M1 always sets both, but a partial-migration KG with depth=0
    # but a stale `concept_type` (or vice versa) should still render as a
    # course-card root rather than fall through to the legacy heuristic.
    explicit_root = next(
        (n for n in normalized_nodes
         if n.get("depth") == 0 or n.get("concept_type") == "root"),
        None,
    )

    def build_tree(node_id: str, depth: int = 0, seen: set[str] | None = None) -> dict:
        seen = set(seen or [])
        seen.add(node_id)
        node = node_map.get(node_id, {})
        result = {
            "id": node_id,
            "label": node.get("name", node_id),
            "depth": node.get("depth", depth),
            "weight": node.get("weight", 1.0),
            "definition": node.get("definition", ""),
            "source_chunks": node.get("source_chunks", []),
            "concept_type": node.get("concept_type", "definition"),
        }
        if depth < 3:
            child_ids = [c for c in children_map.get(node_id, []) if c not in seen]
            if child_ids:
                result["children"] = [build_tree(c, depth + 1, seen) for c in child_ids[:12]]
        return result

    if explicit_root is not None:
        tree = build_tree(explicit_root["id"], depth=0)
        return {
            "id": explicit_root["id"],
            "label": explicit_root.get("name", course_id),
            "definition": explicit_root.get("definition", ""),
            "concept_type": "root",
            "nodes": normalized_nodes,
            "edges": normalized_edges,
            "children": tree.get("children", []),
        }

    # Legacy fallback (Round 1 KG without explicit depth=0 / concept_type
    # =root). F4 (review-swarm): pre-fix, this picked nodes with no
    # inbound edges as "roots" — but in a part-of schema (source=child,
    # target=parent), in-degree-zero nodes are LEAVES. The result was 4/8
    # of the user's existing courses rendering an arbitrary leaf as the
    # radial center. New rule: prefer the node that the most other nodes
    # attach to via part-of (i.e. real parents in the existing schema),
    # tie-broken by weight.
    inbound_part_of: dict[str, int] = {}
    for e in normalized_edges:
        if e.get("relation") == "part-of":
            tgt = e.get("target")
            if tgt:
                inbound_part_of[tgt] = inbound_part_of.get(tgt, 0) + 1

    def _legacy_root_score(node: dict) -> tuple[int, float]:
        return (
            inbound_part_of.get(node.get("id"), 0),
            float(node.get("weight", 0.0) or 0.0),
        )

    chosen_root = max(normalized_nodes, key=_legacy_root_score)
    return {
        "id": chosen_root["id"],
        "label": course_id,
        "nodes": normalized_nodes,
        "edges": normalized_edges,
        "children": build_tree(chosen_root["id"]).get("children", []),
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
        # R3-3: learning_order absent on legacy / no-prereq KGs → None.
        # Coerce non-int (e.g. stringified) values to None defensively
        # so the frontend doesn't have to guard on type.
        raw_order = node.get("learning_order")
        try:
            learning_order = int(raw_order) if raw_order is not None else None
        except (TypeError, ValueError):
            learning_order = None
        normalized.append({
            "id": node_id,
            "name": node.get("name", node_id),
            "definition": node.get("definition", ""),
            "concept_type": node.get("concept_type", "definition"),
            # F15 follow-up: never silently default the first node to
            # depth=0 — that masquerades any KG without an explicit root
            # as having one and trips the explicit-root branch in
            # `_kg_to_mindmap`. Default to depth=1 (a generic concept)
            # and let the explicit-root detection rely on real signal.
            "depth": int(node.get("depth", 1)),
            "weight": float(node.get("weight", max(1, len(chunk_ids) or 1))),
            "source_chunks": source_chunks,
            "chunk_ids": chunk_ids,
            "learning_order": learning_order,
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
