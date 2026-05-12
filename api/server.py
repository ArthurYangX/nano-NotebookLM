"""FastAPI backend for nano-NOTEBOOKLM — serves API + static frontend."""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse as urllib_parse
import uuid
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, File, UploadFile, HTTPException, Query, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AfterValidator, BaseModel, Field, field_validator

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nano_notebooklm.ai.router import ModelRouter
from nano_notebooklm.ai.prompt_templates import NOTE_LATEX_PREAMBLE, NOTE_LATEX_POSTAMBLE
from nano_notebooklm.agents import run_subagent
from nano_notebooklm.agents.formatter import format_response
from nano_notebooklm.kb.store import KBStore
from nano_notebooklm.orchestrator import agent_loop
from nano_notebooklm.orchestrator.engine import Orchestrator
from nano_notebooklm.orchestrator.session_log import SessionLog
from nano_notebooklm.orchestrator import router_intent
from nano_notebooklm.orchestrator.tools import build_default_registry
from nano_notebooklm.ai.openai_backend import OpenAIBackend
from nano_notebooklm.skills.latex_sanitizer import (
    check as latex_check,
    check_unbounded as latex_check_unbounded,
    LaTeXUnsafeError,
    MAX_LATEX_BYTES,
)
from nano_notebooklm.skills import notes_full_course
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


# Attribution (fix-all v3 #L11): the original embed_fn warm-up + status
# surface designed in R4-4 review-swarm fix-all v1/v2 (commits 764276d /
# abce190) but the actual server.py edits first landed in e60bca3
# (R4-6 notes pipeline) because a parallel session committed R4-6
# against a working tree that already contained the v1 changes.
# fix-all v1 #B7 (R4-4 review-swarm): pre-warm kb.embed_fn at boot so the
# first /api/mindmap, /api/upload, or graphrag /api/chat doesn't pay the
# 5-30s sentence-transformer model download + load on the request hot
# path. Wrapped in asyncio.to_thread so the import + model load don't
# block the event loop. Failure is non-fatal — embed_fn will lazy-load on
# first call with a longer latency tax, same as before this hook existed.
# NANO_NLM_DISABLE_EMBED_WARMUP=1 skips the warm-up entirely (used by the
# pytest suite, which creates and tears down `app` many times and would
# otherwise pay the model load on every TestClient reload).
@app.on_event("startup")
async def _warm_embed_fn() -> None:
    # fix-all v2 #V2 (R4-4 review-swarm v2): three changes:
    #   (a) fire-and-forget create_task so startup returns immediately and
    #       FastAPI accepts liveness probes during the model load;
    #   (b) EMBEDDING_MODE=api skip (no local model = nothing to warm);
    #   (c) app.state.embed_warm_ok surfaced via /api/status.
    app.state.embed_warm_ok = None
    if os.environ.get("NANO_NLM_DISABLE_EMBED_WARMUP"):
        # Test-mode skip — pretend warmed; kb.embed_fn lazy-loads on first call.
        app.state.embed_warm_ok = True
        return
    if (config.EMBEDDING_MODE or "local").lower() != "local":
        # API mode has no local model to load.
        app.state.embed_warm_ok = True
        return
    import asyncio as _aio

    async def _do_warmup() -> None:
        try:
            await _aio.to_thread(lambda: kb.embed_fn(["__warmup__"]))
            app.state.embed_warm_ok = True
            logger.info("kb.embed_fn warmed at startup")
        except Exception:  # noqa: BLE001 — never block boot on a warm-up failure
            app.state.embed_warm_ok = False
            logger.warning(
                "kb.embed_fn warm-up failed; will lazy-load on first request",
                exc_info=True,
            )

    _aio.create_task(_do_warmup())


# LaTeX-refactor: probe `tectonic` once at boot. The PDF compile endpoint
# checks this flag and returns 503 immediately when the binary is missing,
# so the frontend can hide the "高质量编译" button without a per-request
# subprocess invocation. shutil.which is sync but a single syscall — keeping
# the probe synchronous keeps app.state.tectonic_available ready before any
# request lands.
@app.on_event("startup")
async def _probe_tectonic() -> None:
    binary = shutil.which("tectonic")
    app.state.tectonic_available = bool(binary)
    app.state.tectonic_path = binary
    if binary:
        logger.info("tectonic detected at %s — PDF compile endpoint enabled", binary)
    else:
        logger.info("tectonic not found in PATH — /api/notes/export/pdf will return 503")


# PPTX → PDF sidecar converter (LibreOffice). Same pattern as tectonic:
# probe once at boot, surface availability via /api/status so the upload
# pipeline can decide whether to attempt sidecar generation and the
# Reader knows whether to expect viewable_as_pdf=True for pptx docs.
@app.on_event("startup")
async def _probe_soffice() -> None:
    from nano_notebooklm.ingest.pptx_pdf import find_soffice
    binary = find_soffice()
    app.state.soffice_path = binary
    app.state.pptx_pdf_available = bool(binary)
    if binary:
        logger.info("soffice detected at %s — pptx upload will generate pdf sidecar", binary)
    else:
        logger.info("soffice not found — pptx will fall back to text-mode Reader (install LibreOffice to enable)")

# ── Upload limits ────────────────────────────────────────────────────
MAX_UPLOAD_SIZE_MB = 50
MAX_UPLOAD_SIZE_BYTES = MAX_UPLOAD_SIZE_MB * 1024 * 1024

# fix-all v1 #V2 (R4-5 review v1): TTL for the qwen_raft health probe
# cached on app.state.qwen_health_cache. 15s > 10s frontend poll → cache
# hit rate ~100% for steady-state polling; cold pulse on operator config
# change still surfaces within one cycle.
QWEN_HEALTH_TTL_SECONDS = float(os.environ.get("QWEN_HEALTH_TTL_SECONDS", "15.0"))
ALLOWED_UPLOAD_SUFFIXES = {".pdf", ".pptx", ".docx", ".md", ".txt"}

# review-swarm fix-all v1 #6: reject any uploaded filename that contains
# C0 controls (\x00-\x1f), DEL (\x7f), or Unicode bidi/format characters.
# These slip past the suffix check, then flow into:
#   1. Content-Disposition: inline; filename="..."  (response splitting via
#      CR/LF embedded in the filename — splits HTTP headers)
#   2. \section{<source_file>} in generated LaTeX (newlines break parsing,
#      bidi overrides spoof file extensions visually)
#   3. structured access log lines (log injection)
# Suffix whitelist alone is not sufficient because "evil\r\n.pdf" passes.
_FILENAME_FORBIDDEN_RE = re.compile(
    "[\x00-\x1f\x7f"          # C0 controls + DEL
    "‎‏"            # LTR / RTL marks
    "‪-‮"           # bidi override block
    "⁦-⁩"           # isolate block
    "]"
)


def _safe_upload_name(raw: str | None) -> str:
    """Return a filesystem- and header-safe leaf filename, or raise 400.

    Strips directory components (defense in depth — `Path.name` already
    drops them), rejects empty / control-char / bidi-override filenames.
    Returned value is safe to use inside Content-Disposition's quoted
    filename= field AND inside a `\\section{}` LaTeX argument.
    """
    name = (raw or "").strip()
    if not name:
        raise HTTPException(400, "filename is empty")
    leaf = Path(name).name
    if not leaf or leaf in {".", ".."}:
        raise HTTPException(400, "filename resolves to empty leaf")
    if _FILENAME_FORBIDDEN_RE.search(leaf):
        raise HTTPException(400, "filename contains control or bidi characters")
    if len(leaf.encode("utf-8")) > 255:
        raise HTTPException(400, "filename exceeds 255 bytes")
    return leaf


def _content_disposition(filename: str, *, disposition: str = "inline") -> str:
    """Build a safe Content-Disposition header value.

    RFC 6266: prefer ``filename*=UTF-8''`` (percent-encoded) so non-ASCII
    filenames don't depend on the legacy quoted form, AND include an
    ASCII-only ``filename=`` fallback for clients that ignore the
    extended form. Embedded `"` / CR / LF in the input become %22 / %0D
    / %0A under percent-encoding, neutralising response-splitting.
    """
    safe = _safe_upload_name(filename)
    quoted = urllib_parse.quote(safe, safe="")
    ascii_fallback = re.sub(r"[^A-Za-z0-9._-]", "_", safe) or "file"
    return f'{disposition}; filename="{ascii_fallback}"; filename*=UTF-8\'\'{quoted}'

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
    # R4-5 part 2 + fix-all v1 #V8: optional backend override surfaced
    # via the topbar chip. "codex" = default task routing (the chip's
    # user-facing label for "use the configured main backend"; v1
    # treated this as a hard openai pin but that 500s in claude-only
    # deployments — fix-all v1 #V1 reverted to "default routing" so
    # codex never forces openai). "qwen_raft" = explicit pin on the
    # AutoDL Qwen2.5-7B-RAFT HTTP backend. None = same as "codex".
    # The chat endpoint 422s when backend="qwen_raft" but
    # QWEN_RAFT_URL is unset, so a stale localStorage chip selection
    # doesn't silently fall through.
    backend: Literal["codex", "qwen_raft"] | None = Field(
        default=None,
        description=(
            'Optional backend override surfaced via the topbar chip. '
            '"codex" = default task routing; "qwen_raft" = AutoDL '
            'Qwen2.5-7B-RAFT (requires QWEN_RAFT_URL configured).'
        ),
    )

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
    """Schema for /api/chat. The five `path` values are constrained by GOAL.md
    Round 2 #1 + Round 4 #R4-4 (which added "graphrag") — anything else
    (e.g. typo `cross_course` underscore vs hyphen) will fail Pydantic
    serialisation before shipping to the frontend.

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
    # checked_files knocks all results to 0). Otherwise one of five — R4-4
    # added `"graphrag"` for KG-driven retrieval (concept cosine + BFS
    # neighbour expansion) which fires before the BM25/vector path when the
    # course has a `knowledge_graph.json` and graph_search returns ≥2 hits.
    path: Literal["rag", "general", "translated", "cross-course", "graphrag"] | None = None
    original_query: str | None = None
    translated_query: str | None = None
    general_reason: str | None = None
    filter_empty: bool | None = None
    filter_low_quality: bool | None = None
    cross_course_origin: str | None = None
    # R4-5 part 2 + fix-all v1 #V8: True when an explicit
    # backend="qwen_raft" request silently degraded to the default
    # routing backend inside qa_skill (qwen timeout / upstream error).
    # `QWEN_RAFT_URL` unset would have 422'd at the endpoint earlier, so
    # this flag specifically signals a runtime degradation, not a config
    # miss. None when no fallback occurred.
    backend_fallback: bool | None = Field(
        default=None,
        description=(
            "True when the response degraded from the requested backend "
            '(typically qwen_raft) to the default routing backend due to '
            "timeout or upstream failure. None when no fallback occurred."
        ),
    )
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
    # LaTeX-refactor: Note output is now LaTeX only. The field is kept
    # rather than removed so old clients posting ``"markdown"`` get a clear
    # 422 (expected "latex") instead of a cryptic "extra fields not
    # permitted" envelope. New clients can omit it entirely.
    format: Literal["latex"] = "latex"
    user_lang: Literal["zh", "en"] | None = None


class NoteFullCourseRequest(BaseModel):
    """Request body for POST /api/notes/full-course/stream.

    Distinct from NoteRequest because full-course generation is always
    course-wide (no `topic` scoping), always LaTeX, and the only optional
    knob is concurrency. Kept as a separate model so the endpoint can
    reject a stray `topic` field with a clean 422 instead of silently
    ignoring it.
    """
    model_config = {"extra": "forbid"}
    course_id: ReqCourseId
    user_lang: Literal["zh", "en"] | None = None
    concurrency: int = Field(
        notes_full_course.DEFAULT_CONCURRENCY,
        ge=1, le=8,
        description="Max parallel per-file LLM calls. Capped at 8 to stay "
                    "well under codex rate limits.",
    )
    # Incremental cache (2026-05-11): default False reuses cached per-file
    # outputs from artifacts/courses/<id>/notes/per_file_cache.json when
    # the file's chunk_hash matches. Pass `true` from the UI's "Regenerate
    # all (force)" button to ignore the cache and re-run every file.
    force: bool = Field(
        False,
        description="When true, ignore per_file_cache.json and re-run "
                    "every per-file LLM call. Default false uses cached "
                    "entries whose chunk_hash matches the current chunks.",
    )


class NotePdfExportRequest(BaseModel):
    """Request body for POST /api/notes/export/pdf.

    Source-based, not topic-based: the frontend sends the (possibly user-
    edited) LaTeX body so compile decouples from a fresh LLM call. The
    `latex_sanitizer` module enforces the same forbidden-command list the
    LLM prompt warns about, plus an 80 KB cap.
    """
    model_config = {"extra": "forbid"}
    course_id: ReqCourseId
    latex_source: str = Field(..., min_length=1)


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


class ExamPrepPlanRequest(BaseModel):
    model_config = {"extra": "forbid"}
    course_id: ReqCourseId
    max_topics: int = Field(8, ge=3, le=15, description="Target number of exam topics to extract (3-15)")
    force: bool = Field(False, description="If true, re-run LLM and merge with existing bank by normalized topic name (preserves question history for matching names; orphans go to an archive bucket).")
    user_lang: Literal["zh", "en"] | None = Field(None, description="Reply language for generated topics + questions; None = follow source material.")


class ExamPrepSeedRequest(BaseModel):
    model_config = {"extra": "forbid"}
    course_id: ReqCourseId
    topic_ids: list[str] | None = Field(None, description="Subset of topic ids to seed; None = all topics in the bank.", max_length=32)
    seeds_per_type: int = Field(2, ge=1, le=5, description="Number of questions to generate per (topic × question type) pair.")
    user_lang: Literal["zh", "en"] | None = Field(None, description="Reply language for generated questions.")

    @field_validator("topic_ids")
    @classmethod
    def _bound_topic_ids(cls, v):
        if v is None:
            return v
        for tid in v:
            if not isinstance(tid, str) or len(tid) > 64:
                raise ValueError("topic_id must be string ≤ 64 chars")
        return v


class ExamPrepNextQuizRequest(BaseModel):
    model_config = {"extra": "forbid"}
    course_id: ReqCourseId
    size: int = Field(8, ge=1, le=20, description="How many questions to sample for this quiz round (1-20).")
    topic_ids: list[str] | None = Field(None, description="Restrict sampling to these topic ids; None = all non-mastered topics. Setting this also re-includes mastered questions within those topics for review.", max_length=32)
    user_lang: Literal["zh", "en"] | None = Field(None, description="Reply language for any newly-seeded questions.")

    @field_validator("topic_ids")
    @classmethod
    def _bound_topic_ids(cls, v):
        if v is None:
            return v
        for tid in v:
            if not isinstance(tid, str) or len(tid) > 64:
                raise ValueError("topic_id must be string ≤ 64 chars")
        return v


class ExamPrepSubmitRequest(BaseModel):
    model_config = {"extra": "forbid"}
    course_id: ReqCourseId
    answers: dict[str, str] = Field(default_factory=dict, description="Map of question_id → user answer (letter for multi-choice, free text for short answer). Capped at 50 entries per submit; question_id ≤ 64 chars, answer ≤ 2000 chars.")
    user_lang: Literal["zh", "en"] | None = Field(None, description="Reply language for any wrong-topic variant generation triggered by this submit.")

    @field_validator("answers")
    @classmethod
    def _bound_answers(cls, v: dict[str, str]) -> dict[str, str]:
        if len(v) > 50:
            raise ValueError("too many answers (cap 50 per submit)")
        for qid, ans in v.items():
            if not isinstance(qid, str) or not qid:
                raise ValueError("question_id must be non-empty string")
            if len(qid) > 64:
                raise ValueError("question_id too long")
            if isinstance(ans, str) and len(ans) > 2000:
                raise ValueError("answer too long (cap 2000 chars)")
        return v


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
async def list_courses(
    mode: Annotated[
        Literal["all", "user"],
        Query(description="'user' (default) hides preset courses; 'all' returns everything."),
    ] = "user",
):
    courses = orchestrator.list_courses()
    if mode == "user":
        courses = [c for c in courses if c not in config.PRESET_COURSE_IDS]
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


@app.delete(
    "/api/courses/{course_id}",
    tags=["courses"],
    summary="Permanently delete a course (artifacts + indices)",
)
async def delete_course(course_id: str):
    """Hard-delete a course: remove `artifacts/courses/<cid>/` directory,
    its per-course FAISS + BM25 indices, then rebuild the global index so
    subsequent search / chat sees the new corpus.

    This is **destructive and irreversible**: notes, quizzes, KG, exam
    bank, ingested chunks, source files — all gone. The frontend "管理"
    modal must show a `window.confirm` dialog before invoking. Preset
    courses (config.PRESET_COURSE_IDS) are still deletable here — they
    were physically kept as a Round-4 rollback hatch, but if the user
    explicitly chooses to delete one we honor that.
    """
    import shutil as _shutil
    import asyncio as _asyncio

    course_id = _validate_course_id_path(course_id)
    course_dir = config.ARTIFACTS_DIR / "courses" / course_id
    if not course_dir.exists():
        raise HTTPException(404, f"course not found: {course_id}")

    # Track which files we actually removed so the response can confirm.
    removed: list[str] = []

    def _do_delete():
        # 1. Drop the course's artifact directory (chunks / KG / notes /
        #    quizzes / exam_bank / mindmap_edits / file_hashes — everything
        #    under artifacts/courses/<cid>/).
        if course_dir.exists():
            _shutil.rmtree(course_dir)
            removed.append(f"courses/{course_id}/")

        # 2. Drop the per-course index files. The global indices are
        #    rebuilt below from disk so they'll exclude this course.
        idx_dir = config.ARTIFACTS_DIR / "indices"
        faiss_dir = idx_dir / "faiss" / course_id
        if faiss_dir.exists():
            _shutil.rmtree(faiss_dir)
            removed.append(f"indices/faiss/{course_id}/")
        for suffix in (".json", ".pkl"):
            bm25_path = idx_dir / "bm25" / f"{course_id}{suffix}"
            if bm25_path.exists():
                bm25_path.unlink()
                removed.append(f"indices/bm25/{course_id}{suffix}")

        # 3. Rebuild the global hybrid index from what's left on disk.
        #    Without this, in-memory _all_chunks still references the
        #    deleted course's chunks → search/chat keeps returning them.
        try:
            kb.build_index(None)
        except Exception as exc:
            # build_index already logs; we surface the partial-success in
            # the response. The directory is gone, just the index didn't
            # rebuild — operator can `python scripts/ingest_all.py` later.
            logger.warning(
                "post-delete index rebuild failed for %s: %s",
                course_id, type(exc).__name__,
            )

    await _asyncio.to_thread(_do_delete)
    session_log.append(course_id, "deletion", {"kind": "course-delete", "removed": removed})
    return {"deleted": True, "course_id": course_id, "removed": removed}


@app.get("/api/sources/{course_id}", tags=["courses"], summary="List source files for a course")
async def get_sources(course_id: str):
    course_id = _validate_course_id_path(course_id)
    chunks = kb.get_chunks(course_id)
    if not chunks:
        return {"sources": []}
    sources: dict[str, dict] = {}
    for c in chunks:
        if c.source_file not in sources:
            # `viewable_as_pdf=True` lets the Notes citation modal mount
            # the in-place PDF iframe for pptx-with-sidecar instead of
            # falling back to Reader text-mode (shouldPreviewCitation
            # in study-state.js reads this hint).
            ftype = c.file_type.value
            viewable_as_pdf = ftype == "pdf" or (
                ftype == "pptx"
                and _resolve_pptx_pdf_sidecar(course_id, c.source_file) is not None
            )
            sources[c.source_file] = {
                "id": c.doc_id,
                "type": ftype,
                "title": c.source_file,
                "chunks": 0,
                "checked": True,
                "viewable_as_pdf": viewable_as_pdf,
            }
        sources[c.source_file]["chunks"] += 1
    return {"sources": list(sources.values())}


_DOC_ID_RE = __import__("re").compile(r"^[A-Za-z0-9]{1,64}$")


def _resolve_source_path(course_id: str, source_file: str) -> Path | None:
    """Resolve a chunk's `source_file` to an on-disk path.

    Search order: uploads dir (R4-2 upload-only courses) → COURSE_DATA_DIR
    (preset courses ingested via scripts/ingest_all.py). Returns None when
    neither resolves to a real file inside its expected root (path-traversal
    guard via `Path.resolve()` + `relative_to`).
    """
    candidates: list[tuple[Path, Path]] = []
    uploads_root = (config.ARTIFACTS_DIR / "uploads" / course_id).resolve()
    candidates.append((uploads_root, uploads_root / source_file))
    if config.COURSE_DATA_DIR and str(config.COURSE_DATA_DIR):
        preset_root = (Path(config.COURSE_DATA_DIR) / course_id).resolve()
        candidates.append((preset_root, preset_root / source_file))
    for root, candidate in candidates:
        try:
            resolved = candidate.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        if resolved.is_file():
            return resolved
    return None


def _preview_dir_for(course_id: str) -> Path:
    """The per-course directory holding pptx → pdf sidecars.

    Lives under `artifacts/courses/<id>/previews/` (NOT under uploads/) so
    `kb.ingest_course(uploads_dir, ...)`'s recursive scan never picks the
    sidecar up as a second-class PDF and double-indexes the slide content.
    """
    return config.ARTIFACTS_DIR / "courses" / course_id / "previews"


# Per-(course, source_file) lock so a flurry of Reader clicks across
# tabs doesn't spawn N parallel soffice procs against the same deck.
# Bounded soft-cap mirrors _UPLOAD_LOCKS — see _maybe_evict_upload_lock.
_LAZY_RENDER_INFLIGHT: dict[tuple[str, str], bool] = {}
_LAZY_RENDER_INFLIGHT_MAX = 256


def _schedule_lazy_pptx_render(course_id: str, source_file: str, source_path: Path) -> None:
    """Fire-and-forget background sidecar conversion for a preset / pre-R5
    pptx doc that never went through the upload pipeline's sidecar pass.

    Idempotent: if a render is already in-flight for this (course, file)
    pair the call is a no-op. The caller does NOT await the task; this
    request returns viewable_as_pdf=False and the user's next visit picks
    up the cached sidecar.
    """
    key = (course_id, source_file)
    if _LAZY_RENDER_INFLIGHT.get(key):
        return
    if len(_LAZY_RENDER_INFLIGHT) > _LAZY_RENDER_INFLIGHT_MAX:
        # Drop one stale entry to keep growth bounded; safe because
        # entries only mark in-flight state, not pending work.
        try:
            _LAZY_RENDER_INFLIGHT.pop(next(iter(_LAZY_RENDER_INFLIGHT)))
        except (StopIteration, KeyError):
            pass
    _LAZY_RENDER_INFLIGHT[key] = True
    preview_dir = _preview_dir_for(course_id)

    async def _render() -> None:
        from nano_notebooklm.ingest.pptx_pdf import convert_pptx_to_pdf
        try:
            await asyncio.to_thread(
                convert_pptx_to_pdf, source_path, preview_dir,
            )
        except Exception:  # noqa: BLE001 — background task must never crash worker
            logger.exception("pptx_pdf.lazy_render_failed course=%s file=%s",
                             course_id, source_file)
        finally:
            _LAZY_RENDER_INFLIGHT.pop(key, None)

    try:
        asyncio.create_task(_render())
    except RuntimeError:
        # No running loop (only happens in some sync test contexts) —
        # drop the in-flight flag so a later request can retry.
        _LAZY_RENDER_INFLIGHT.pop(key, None)


def _resolve_pptx_pdf_sidecar(course_id: str, source_file: str) -> Path | None:
    """For a .pptx `source_file`, return the sidecar PDF path if generated.

    Returns None for non-pptx inputs and when no sidecar exists. Same
    path-traversal guard as `_resolve_source_path` (resolve + relative_to
    the preview root) so a hand-crafted `source_file="../../etc/passwd"`
    cannot escape the preview dir.
    """
    if not source_file or Path(source_file).suffix.lower() != ".pptx":
        return None
    from nano_notebooklm.ingest.pptx_pdf import sidecar_path
    preview_root = _preview_dir_for(course_id).resolve() if _preview_dir_for(course_id).exists() else None
    if preview_root is None:
        return None
    candidate = sidecar_path(_preview_dir_for(course_id), source_file)
    try:
        resolved = candidate.resolve()
        resolved.relative_to(preview_root)
    except (OSError, ValueError):
        return None
    return resolved if resolved.is_file() else None


@app.get("/api/source/{course_id}/{doc_id}/chunks",
         tags=["courses"], summary="List all chunks for a source document (ordered)")
async def get_source_chunks(course_id: str, doc_id: str):
    """Return every chunk belonging to one source document, ordered by
    page then chunk_id — powers the Reader's document-browse mode.

    Differs from `/api/chunks/{chunk_id}` (citation viewer with prev/next):
    here we hand back the *whole* document at once so the Reader can render
    the full text inline and jump to `activePage` without round-tripping.
    """
    course_id = _validate_course_id_path(course_id)
    if not doc_id or not _DOC_ID_RE.match(doc_id):
        raise HTTPException(400, f"invalid doc_id: {doc_id[:40]!r}")
    chunks = kb.get_chunks(course_id)
    if not chunks:
        raise HTTPException(404, f"course has no chunks: {course_id}")
    same_doc = [c for c in chunks if c.doc_id == doc_id]
    if not same_doc:
        raise HTTPException(404, f"doc not found in course: {doc_id}")
    same_doc.sort(key=lambda c: (c.page if c.page is not None else 10**9, c.chunk_id))
    pages = [c.page for c in same_doc if c.page is not None]
    file_path = _resolve_source_path(course_id, same_doc[0].source_file)
    # PPTX rendering: when LibreOffice produced a sidecar PDF during
    # upload, the Reader can mount the browser's native PDF viewer
    # (DocumentPdfFrame) instead of falling back to text-mode chunk dump.
    # `viewable_as_pdf=True` flips that switch on the frontend; the file
    # endpoint will then transparently serve the sidecar with mime=pdf.
    sidecar = _resolve_pptx_pdf_sidecar(course_id, same_doc[0].source_file)
    # Preset-course migration: existing courses (CS231N etc.) were ingested
    # before sidecar generation existed. Kick off a background conversion
    # the FIRST time a pptx doc is opened in Reader so the next visit gets
    # the better viewer. Fire-and-forget — this request still returns
    # viewable_as_pdf=False, frontend renders text mode, and the user's
    # second click into the same doc picks up the cached sidecar.
    if (
        sidecar is None
        and same_doc[0].file_type.value == "pptx"
        and file_path is not None
        and getattr(app.state, "pptx_pdf_available", False)
    ):
        _schedule_lazy_pptx_render(course_id, same_doc[0].source_file, file_path)
    return {
        "course_id": course_id,
        "doc_id": doc_id,
        "source_file": same_doc[0].source_file,
        "file_type": same_doc[0].file_type.value,
        "total_chunks": len(same_doc),
        "page_range": [min(pages), max(pages)] if pages else None,
        "file_available": file_path is not None or sidecar is not None,
        "viewable_as_pdf": sidecar is not None or (
            file_path is not None and same_doc[0].file_type.value == "pdf"
        ),
        "chunks": [
            {
                "chunk_id": c.chunk_id,
                "text": c.text,
                "page": c.page,
                "location": c.location,
                "section": c.section,
            }
            for c in same_doc
        ],
    }


@app.api_route(
    "/api/source/{course_id}/{doc_id}/file",
    methods=["GET", "HEAD"],
    tags=["courses"],
    summary="Serve the original source file (PDF/PPTX/DOCX/MD/PNG)",
)
async def get_source_file(course_id: str, doc_id: str):
    """Stream the original file for in-browser viewers (e.g. pdf.js / native
    `<iframe>` PDF viewer). Path resolution goes through
    `_resolve_source_path` which guards against `..`-traversal by resolving
    each candidate and rejecting anything outside its allowed root.

    HEAD is accepted so the Notes citation-preview modal can probe for a
    200 before mounting the iframe; falls back to Reader text-mode on 404.
    """
    course_id = _validate_course_id_path(course_id)
    if not doc_id or not _DOC_ID_RE.match(doc_id):
        raise HTTPException(400, f"invalid doc_id: {doc_id[:40]!r}")
    chunks = kb.get_chunks(course_id)
    target = next((c for c in chunks if c.doc_id == doc_id), None)
    if target is None:
        raise HTTPException(404, f"doc not found in course: {doc_id}")
    # PPTX-as-PDF: when LibreOffice produced a sidecar at upload time,
    # serve the PDF rendering with mime=application/pdf so Chrome/Safari
    # mount their native viewer in <iframe>. The original .pptx is still
    # on disk under uploads/ — we just prefer the renderable sibling.
    # The Content-Disposition filename keeps the original .pptx name so
    # "Save as" downloads under the user-recognisable filename.
    sidecar = _resolve_pptx_pdf_sidecar(course_id, target.source_file)
    if sidecar is not None:
        path = sidecar
        mime = "application/pdf"
    else:
        path = _resolve_source_path(course_id, target.source_file)
        if path is None:
            raise HTTPException(
                404,
                "source file not found on disk (may have been deleted after ingest)",
            )
        mime, _ = mimetypes.guess_type(str(path))
        if not mime:
            mime = "application/octet-stream"
    return FileResponse(
        path,
        media_type=mime,
        # `inline` keeps the browser's native viewer active for PDFs.
        # _content_disposition strips dir components, rejects CR/LF/NUL
        # (response splitting), and emits both an ASCII fallback and an
        # RFC 6266 percent-encoded filename* for non-ASCII names.
        # `nosniff` blocks browser MIME-sniffing — defense-in-depth in
        # case any future ingest path lets a non-PDF file ride a `.pdf`
        # suffix past the upload validator.
        headers={
            "Content-Disposition": _content_disposition(
                Path(target.source_file).name, disposition="inline"
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )


# ── Skill endpoints ──────────────────────────────────────────────────
@app.post("/api/chat", tags=["skills"],
          summary="RAG chat with source citations",
          response_model=ChatResponse, response_model_exclude_none=True)
async def chat(req: ChatRequest):
    # R4-5 part 2: guard the qwen_raft path behind explicit env config.
    # Without this, a stale frontend chip selection (or a curl with the
    # backend kwarg) would surface deep inside ModelRouter as a generic
    # RuntimeError. 422 mirrors the rest of the input-validation surface.
    if req.backend == "qwen_raft" and not config.QWEN_RAFT_URL:
        raise HTTPException(422, detail="qwen_raft backend not configured")
    result = await orchestrator.skills["qa"].execute({
        "question": req.question,
        "course_filter": req.course_id,
        "top_k": req.top_k,
        "checked_files": req.checked_files,
        "user_lang": req.user_lang,
        "backend": req.backend,
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
    # LaTeX-refactor: do NOT call format_response on note content — that
    # helper repairs markdown (## headers, ** bold, fenced code, $ math)
    # and would actively mangle a LaTeX body. Notes ship raw LaTeX source
    # so the frontend can populate CodeMirror + the PDF compile endpoint.
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


# ── Exam Prep — closed-loop exam preparation ──────────────────────────
# Six thin wrappers around ExamPrepSkill's `action` dispatch (4 POST +
# GET/DELETE on /state/{course_id}). The skill owns all persistence +
# LLM calls + self-evolution; the API layer just validates inputs, maps
# typed skill errors to HTTP status codes, and logs sessions.


def _exam_prep_status_for_error(error: str | None) -> int:
    """Map skill error tokens to HTTP status. Most failures are upstream
    (LLM timeout / malformed response) → 502; bank-version-too-new is a
    state mismatch the operator can resolve → 409; absent topics indicates
    a precondition the caller can fix → 400."""
    if error == "bank_version_too_new":
        return 409
    if error and error.startswith("no_topics"):
        return 400
    return 502


def _raise_exam_prep_error(result, default: str) -> None:
    raise HTTPException(
        status_code=_exam_prep_status_for_error(result.error),
        detail=result.error or default,
    )


@app.post("/api/exam-prep/plan", tags=["exam-prep"], summary="Extract exam-relevant topics for a course")
async def exam_prep_plan(req: ExamPrepPlanRequest):
    result = await orchestrator.run_skill("exam_prep", {
        "action": "plan",
        "course_id": req.course_id,
        "max_topics": req.max_topics,
        "force": req.force,
        "user_lang": req.user_lang,
    })
    if not result.success:
        _raise_exam_prep_error(result, "exam_prep_plan_failed")
    session_log.append(req.course_id, "generation", {"kind": "exam-prep-plan"})
    return result.data


@app.post("/api/exam-prep/seed", tags=["exam-prep"], summary="Seed initial questions for topics")
async def exam_prep_seed(req: ExamPrepSeedRequest):
    result = await orchestrator.run_skill("exam_prep", {
        "action": "seed",
        "course_id": req.course_id,
        "topic_ids": req.topic_ids,
        "seeds_per_type": req.seeds_per_type,
        "user_lang": req.user_lang,
    })
    if not result.success:
        _raise_exam_prep_error(result, "exam_prep_seed_failed")
    return result.data


@app.post("/api/exam-prep/quiz/next", tags=["exam-prep"], summary="Sample the next quiz from the bank")
async def exam_prep_next_quiz(req: ExamPrepNextQuizRequest):
    result = await orchestrator.run_skill("exam_prep", {
        "action": "next_quiz",
        "course_id": req.course_id,
        "size": req.size,
        "topic_ids": req.topic_ids,
        "user_lang": req.user_lang,
    })
    if not result.success:
        _raise_exam_prep_error(result, "exam_prep_quiz_failed")
    return result.data


@app.post("/api/exam-prep/quiz/submit", tags=["exam-prep"], summary="Grade answers + self-evolve variants for wrong topics")
async def exam_prep_submit(req: ExamPrepSubmitRequest):
    result = await orchestrator.run_skill("exam_prep", {
        "action": "submit",
        "course_id": req.course_id,
        "answers": req.answers,
        "user_lang": req.user_lang,
    })
    if not result.success:
        _raise_exam_prep_error(result, "exam_prep_submit_failed")
    session_log.append(req.course_id, "generation", {
        "kind": "exam-prep-submit",
        "wrong_topic_count": result.data.get("wrong_topic_count", 0),
        "variants_added": sum(result.data.get("variants_added", {}).values()),
        # fix-all v1 L11: did per-topic cap clip variant gen? (operators want
        # to know how often users hit the 5/topic ceiling for tuning).
        "budget_capped": result.data.get("budget_capped", False),
        "dropped_question_ids_count": len(result.data.get("dropped_question_ids") or []),
    })
    return result.data


# fix-all v1 M8: reserve the POST verb names so a GET typo for
# /api/exam-prep/plan (etc.) can't fall through to {course_id} and silently
# create a course literally named "plan". The check runs after the standard
# course-id validation so traversal attacks still 400 first.
_EXAM_PREP_RESERVED_PATH_NAMES = {"plan", "seed", "quiz", "state"}


def _validate_exam_prep_course_id(value: str) -> str:
    value = _validate_course_id_path(value)
    if value in _EXAM_PREP_RESERVED_PATH_NAMES:
        raise HTTPException(
            400,
            f"course_id '{value}' is a reserved path segment in this router",
        )
    return value


@app.get("/api/exam-prep/state/{course_id}", tags=["exam-prep"], summary="Inspect the current bank + mastery view")
async def exam_prep_view(course_id: str):
    course_id = _validate_exam_prep_course_id(course_id)
    result = await orchestrator.run_skill("exam_prep", {
        "action": "view", "course_id": course_id,
    })
    if not result.success:
        _raise_exam_prep_error(result, "exam_prep_view_failed")
    return result.data


@app.delete("/api/exam-prep/state/{course_id}", tags=["exam-prep"], summary="Wipe the exam bank for a course")
async def exam_prep_reset(course_id: str):
    course_id = _validate_exam_prep_course_id(course_id)
    result = await orchestrator.run_skill("exam_prep", {
        "action": "reset", "course_id": course_id,
    })
    if not result.success:
        _raise_exam_prep_error(result, "exam_prep_reset_failed")
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


# review-swarm fix-all v1 #8: cap concurrent full-course requests across the
# whole app. Without this, 8 simultaneous users × concurrency=8 = 64 in-flight
# codex calls — enough to wedge the router for chat/report/agent (all share
# the same backend). The lazy-init pattern mirrors _TECTONIC_SEMAPHORE.
# Override via NANO_NLM_MAX_FULL_COURSE_CONCURRENCY env var.
_FULL_COURSE_SEMAPHORE: "asyncio.Semaphore | None" = None


def _get_full_course_semaphore() -> "asyncio.Semaphore":
    global _FULL_COURSE_SEMAPHORE
    if _FULL_COURSE_SEMAPHORE is None:
        cap = max(1, int(os.environ.get("NANO_NLM_MAX_FULL_COURSE_CONCURRENCY", "2")))
        _FULL_COURSE_SEMAPHORE = asyncio.Semaphore(cap)
    return _FULL_COURSE_SEMAPHORE


@app.post("/api/notes/full-course/stream", tags=["skills"],
          summary="Generate full-course notes file-by-file (parallel) + merge + review")
async def stream_full_course_notes(req: NoteFullCourseRequest, request: Request):
    """Per-file parallel note generation with progressive NDJSON emission.

    Phases:
      1. plan          — group chunks by source_file, emit the file list
      2. per-file      — Semaphore(concurrency)-throttled LLM calls; each
                         result emits as soon as it finishes
      3. merging       — programmatic \\section{} concat (no LLM cost)
      4. reviewing     — single LLM polish pass, token-streamed
      5. done          — final reviewed LaTeX body

    Event shapes (NDJSON, one JSON object per line):
      {"type":"plan", "files":[{idx, source_file, chunk_count}], "total":N}
      {"type":"file_start", "idx":i, "source_file":..., "total":N}
      {"type":"file_done", "idx":i, "source_file":..., "content":"...",
                            "chunks_used":k}
      {"type":"file_error", "idx":i, "source_file":..., "error":"<code>"}
      {"type":"merging", "files_succeeded":m, "files_failed":f}
      {"type":"reviewing"}
      {"type":"review_chunk", "delta":"..."}    # client accumulates
      {"type":"done", "content":"...", "files_succeeded":m, "files_failed":f}
      {"type":"error", "error":"<code>", "partial":"...", "retryable":true}

    Event ordering contract: `file_start` and `file_done`/`file_error`
    events are NOT globally ordered relative to each other. A fast
    worker can emit `start(A) → done(A)` before a slower worker emits
    its `start(B)`. Consumers MUST key on `idx` (assigned in the plan
    event) to associate events with files — do not rely on event-arrival
    order matching plan-list order.

    Cost note: ~N+1 LLM calls per request (one per file plus one review).
    For a 20-file course at default concurrency=4 the wall clock is
    dominated by the slowest 5 sequential batches plus the review tail.
    """
    course_id = req.course_id
    user_lang = req.user_lang
    concurrency = req.concurrency
    rid = getattr(request.state, "request_id", "?")

    # fix-all v1 #20 (optional polish): plan_for_course reads chunks.json
    # off disk and instantiates Pydantic models — for a 15K-chunk course
    # that's 100-500ms of sync work before the first `plan` event can
    # ship. Push it to a thread so the event loop stays responsive.
    plans = await asyncio.to_thread(
        notes_full_course.plan_for_course, kb, course_id,
        user_lang=user_lang, force_refresh=req.force,
    )

    global_sem = _get_full_course_semaphore()

    async def events():
        partial = ""
        if not plans:
            yield json.dumps({
                "type": "error",
                "error": "no_chunks",
                "detail": f"course {course_id!r} has no indexed chunks",
                "retryable": False,
            }, ensure_ascii=False) + "\n"
            return
        # fix-all v1 #8: hold the global semaphore for the LLM-heavy span
        # (plan → review). Prevents 8 simultaneous users × concurrency=8 =
        # 64 in-flight codex calls from wedging chat/report/agent on the
        # shared router. async with ensures release even on early return
        # / cancellation / exception.
        async with global_sem:
            # Prune cache entries for source_files no longer present (e.g.
            # user deleted a PDF) before announcing the plan. Best-effort:
            # if the artifacts dir is missing for some reason, ignore.
            try:
                active_files = {p.source_file for p in plans}
                await asyncio.to_thread(
                    notes_full_course.prune_stale_cache, course_id, active_files,
                )
            except Exception:  # noqa: BLE001 — never block generation on cache cleanup
                logger.warning("prune_stale_cache failed for %s",
                               course_id, exc_info=True)

            # Partition plans: cached ones short-circuit (no LLM call),
            # non-cached ones flow through the worker pool. Report cache
            # stats in the plan event so the frontend can pre-light the
            # progress bar.
            cached_plans = [p for p in plans if p.cached_content is not None]
            fresh_plans = [p for p in plans if p.cached_content is None]

            try:
                yield json.dumps({
                    "type": "plan",
                    "files": [
                        {"idx": p.idx, "source_file": p.source_file,
                         "chunk_count": p.chunk_count,
                         "cached": p.cached_content is not None}
                        for p in plans
                    ],
                    "total": len(plans),
                    "concurrency": concurrency,
                    "cached_count": len(cached_plans),
                    "fresh_count": len(fresh_plans),
                    "force": req.force,
                }, ensure_ascii=False) + "\n"

                results: list[notes_full_course.FileResult] = [None] * len(plans)  # type: ignore[list-item]

                # Cache hits: emit synthesized file_cached events and seed
                # `results` so the merge pass sees them. No event ordering
                # constraint — fire them all up front so the UI lights the
                # cached files instantly.
                for p in cached_plans:
                    results[p.idx] = notes_full_course.FileResult(
                        idx=p.idx, source_file=p.source_file,
                        chunk_count=p.chunk_count,
                        content=p.cached_content, error=None,
                    )
                    yield json.dumps({
                        "type": "file_cached",
                        "idx": p.idx,
                        "source_file": p.source_file,
                        "content": p.cached_content,
                        "chunks_used": p.chunk_count,
                    }, ensure_ascii=False) + "\n"

                semaphore = asyncio.Semaphore(concurrency)
                # Bounded queue: per-file work produces 2 events (start + done/error).
                # 4× plans gives slack for the merging/reviewing/done tail without
                # an unbounded queue that could mask backpressure bugs.
                queue: asyncio.Queue = asyncio.Queue(maxsize=max(64, len(fresh_plans) * 4))

                async def run_one(plan):
                    # Acquire the semaphore first so file_start reflects "a
                    # worker actually picked this file up", not "scheduled at
                    # t=0 alongside the other 19".
                    async with semaphore:
                        await queue.put({
                            "type": "file_start",
                            "idx": plan.idx,
                            "source_file": plan.source_file,
                            "total": len(plans),
                        })
                        result = await notes_full_course.generate_file(router, plan)
                        results[plan.idx] = result
                        if result.content:
                            # Write fresh result to the cache. Best-effort:
                            # cache write failure (disk full, etc.) shouldn't
                            # break the generation — log and continue.
                            try:
                                await notes_full_course.write_cache_entry(
                                    course_id, plan.source_file,
                                    chunk_hash_value=plan.cache_key,
                                    content=result.content,
                                    model=getattr(router, "current_model", ""),
                                )
                            except Exception:  # noqa: BLE001
                                logger.warning(
                                    "write_cache_entry failed course=%s file=%s",
                                    course_id, plan.source_file, exc_info=True,
                                )
                            await queue.put({
                                "type": "file_done",
                                "idx": result.idx,
                                "source_file": result.source_file,
                                "content": result.content,
                                "chunks_used": result.chunk_count,
                            })
                        else:
                            await queue.put({
                                "type": "file_error",
                                "idx": result.idx,
                                "source_file": result.source_file,
                                "error": result.error or "unknown",
                            })

                tasks = [asyncio.create_task(run_one(p)) for p in fresh_plans]
                queue_waiter: asyncio.Task | None = None
                try:
                    # Drive the drain off task completion, NOT a done_count we
                    # increment from queue events. A naive `while done_count <
                    # N: await queue.get()` hangs forever if any producer
                    # fails between `router.complete()` returning and
                    # `queue.put(terminal_event)` — the terminal event never
                    # arrives and the main loop has no way out.
                    #
                    # Loop until every task is done. On each iteration: drain
                    # whatever's currently in the queue, then await EITHER the
                    # next queue.put OR any task completing. This way a
                    # producer that died without enqueuing its terminal event
                    # still unblocks us via task completion.
                    pending = set(tasks)
                    while pending:
                        while not queue.empty():
                            event = queue.get_nowait()
                            yield json.dumps(event, ensure_ascii=False) + "\n"
                        if queue_waiter is None or queue_waiter.done():
                            queue_waiter = asyncio.create_task(queue.get())
                        done, _ = await asyncio.wait(
                            pending | {queue_waiter},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if queue_waiter in done:
                            event = queue_waiter.result()
                            yield json.dumps(event, ensure_ascii=False) + "\n"
                            queue_waiter = None
                        pending = {t for t in pending if not t.done()}
                    # All tasks finished — drain any straggler events the
                    # final tasks pushed before exiting, then surface any
                    # task-level exceptions for the log.
                    if queue_waiter is not None and not queue_waiter.done():
                        queue_waiter.cancel()
                    while not queue.empty():
                        event = queue.get_nowait()
                        yield json.dumps(event, ensure_ascii=False) + "\n"
                    for t in tasks:
                        if t.done() and not t.cancelled():
                            exc = t.exception()
                            if exc is not None:
                                logger.warning(
                                    "per-file note task raised %s",
                                    type(exc).__name__,
                                )
                finally:
                    # Client disconnect → StreamingResponse cancels this
                    # generator; cancel outstanding LLM tasks so we don't keep
                    # burning tokens for a closed connection.
                    for t in tasks:
                        if not t.done():
                            t.cancel()
                    if queue_waiter is not None and not queue_waiter.done():
                        queue_waiter.cancel()

                settled = [r for r in results if r is not None]
                succeeded = [r for r in settled if r.content]
                failed = [r for r in settled if not r.content]

                if not succeeded:
                    yield json.dumps({
                        "type": "error",
                        "error": "all_files_failed",
                        "detail": f"{len(failed)} of {len(plans)} files failed; "
                                  f"nothing left to merge",
                        "retryable": True,
                    }, ensure_ascii=False) + "\n"
                    return

                yield json.dumps({
                    "type": "merging",
                    "files_succeeded": len(succeeded),
                    "files_failed": len(failed),
                }, ensure_ascii=False) + "\n"

                draft = notes_full_course.concat_draft(settled)

                yield json.dumps({"type": "reviewing"}, ensure_ascii=False) + "\n"

                review_inputs = notes_full_course.prepare_review_inputs(
                    course_id=course_id,
                    draft=draft,
                    file_count=len(succeeded),
                    user_lang=user_lang,
                )

                async for delta in router.complete_stream(
                    review_inputs["prompt"],
                    task_type=review_inputs["task_type"],
                    system=review_inputs["system"],
                    temperature=review_inputs["temperature"],
                    max_tokens=review_inputs["max_tokens"],
                ):
                    partial += delta
                    # Wire-cost note: ship `delta` only — DO NOT also ship
                    # `partial` here. Earlier versions included partial in
                    # every event, which made the cumulative wire bytes O(N²)
                    # in response length (a 30 KB reviewed body became 7.5 MB
                    # on the wire). The frontend accumulates deltas itself.
                    yield json.dumps({
                        "type": "review_chunk",
                        "delta": delta,
                    }, ensure_ascii=False) + "\n"

                # Sanitize the reviewed body with the no-cap variant — a
                # 20-file course legitimately exceeds the 80KB single-topic
                # cap, but the forbidden-command threat model still applies.
                final_content = partial.strip()
                if not final_content:
                    # Review pass returned nothing — fall back to the raw draft
                    # rather than failing the whole request. Better to give the
                    # user un-polished sections than no notes at all.
                    logger.warning("review pass empty for course %s; "
                                   "falling back to raw concat draft", course_id)
                    final_content = draft
                try:
                    final_content = latex_check_unbounded(final_content)
                except LaTeXUnsafeError as e:
                    logger.warning("merged notes failed sanitizer for course %s: %s",
                                   course_id, e.reason)
                    yield json.dumps({
                        "type": "error",
                        "error": "latex_unsafe",
                        "detail": e.reason,
                        "partial": partial,
                        "retryable": True,
                    }, ensure_ascii=False) + "\n"
                    return

                session_log.append(course_id, "generation", {
                    "kind": "notes-full-course",
                    "files_succeeded": len(succeeded),
                    "files_failed": len(failed),
                })
                yield json.dumps({
                    "type": "done",
                    "content": final_content,
                    "files_succeeded": len(succeeded),
                    "files_failed": len(failed),
                }, ensure_ascii=False) + "\n"

            except asyncio.CancelledError:
                # Client disconnect — let it bubble so StreamingResponse cleans up.
                raise
            except Exception:  # noqa: BLE001
                logger.exception("rid=%s full-course note stream failed for %s",
                                 rid, course_id)
                yield json.dumps({
                    "type": "error",
                    "error": "stream_failed",
                    "partial": partial,
                    "retryable": True,
                }, ensure_ascii=False) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson")


# LaTeX-refactor: tectonic-backed PDF compile. Takes the LaTeX body as
# input (decoupled from LLM regeneration), sanitises it, wraps with the
# server-owned preamble, runs `tectonic -X compile`, and streams the
# resulting PDF back. tectonic absence → 503. Sanitizer rejection → 422.
# Subprocess non-zero exit → 422 with log tail. Timeout → 504.
_TECTONIC_TIMEOUT_SECONDS = 60
# review-swarm fix-all v1 #2: cap concurrent tectonic processes so a burst
# of "PDF (compile)" clicks doesn't spawn N processes (each ~300-500 MB RSS).
# Lazy-init because asyncio.Semaphore needs a running event loop in Py<3.10.
# Override via NANO_NLM_MAX_TECTONIC_CONCURRENCY env var.
_TECTONIC_SEMAPHORE: "asyncio.Semaphore | None" = None


def _get_tectonic_semaphore() -> "asyncio.Semaphore":
    import asyncio as _aio
    global _TECTONIC_SEMAPHORE
    if _TECTONIC_SEMAPHORE is None:
        cap = max(1, int(os.environ.get("NANO_NLM_MAX_TECTONIC_CONCURRENCY", "2")))
        _TECTONIC_SEMAPHORE = _aio.Semaphore(cap)
    return _TECTONIC_SEMAPHORE


def _run_tectonic_blocking(tectonic_path: str, tex_path: Path,
                            outdir: Path) -> "subprocess.CompletedProcess[bytes] | None":
    """Run tectonic synchronously (intended for asyncio.to_thread).

    Returns the CompletedProcess on normal exit (any returncode), or None
    on TimeoutExpired (caller distinguishes via this sentinel rather than
    re-raising across the thread boundary). Reading the resulting PDF is
    handled by the caller after thread join.
    """
    try:
        return subprocess.run(
            [tectonic_path, "-X", "compile",
             "--outdir", str(outdir),
             "--keep-logs", str(tex_path)],
            capture_output=True,
            timeout=_TECTONIC_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None


@app.post("/api/notes/export/pdf", tags=["skills"],
          summary="Compile a LaTeX note body to PDF via tectonic")
async def export_note_pdf(req: NotePdfExportRequest, request: Request):
    import asyncio as _aio
    rid = getattr(request.state, "request_id", "?")

    if not getattr(app.state, "tectonic_available", False):
        # Surface as JSONResponse rather than HTTPException so the body
        # carries the structured `{error, request_id, detail}` envelope
        # the frontend reads. 503 = service unavailable per RFC.
        return JSONResponse(
            status_code=503,
            content={"error": "tectonic_unavailable",
                     "request_id": rid,
                     "detail": "tectonic binary not found on the server PATH"},
        )

    try:
        cleaned = latex_check(req.latex_source)
    except LaTeXUnsafeError as exc:
        return JSONResponse(
            status_code=422,
            content={"error": "latex_unsafe",
                     "request_id": rid,
                     "reason": exc.reason,
                     "snippet": exc.snippet},
        )

    document = f"{NOTE_LATEX_PREAMBLE}{cleaned}{NOTE_LATEX_POSTAMBLE}"

    # Each compile gets a fresh tempdir; tectonic writes intermediates
    # (.aux, .log, .pdf) into --outdir. Use `with` so even a crashing
    # subprocess can't leak the dir.
    tectonic_path = app.state.tectonic_path or "tectonic"

    # review-swarm fix-all v1 #2: gate the blocking subprocess.run via a
    # module-level Semaphore so a click-storm can't OOM the host. v1 #3:
    # run the whole compile + PDF read inside asyncio.to_thread so the
    # FastAPI event loop stays responsive for chat/mindmap/etc.
    async with _get_tectonic_semaphore():
        def _do_compile():
            with tempfile.TemporaryDirectory(prefix="nano_nlm_tex_") as tmpdir:
                tmpdir_path = Path(tmpdir)
                tex_path = tmpdir_path / "note.tex"
                tex_path.write_text(document, encoding="utf-8")
                result = _run_tectonic_blocking(tectonic_path, tex_path, tmpdir_path)
                if result is None:
                    return ("timeout", None, None)
                if result.returncode != 0:
                    log_bytes = (result.stderr or b"") + (result.stdout or b"")
                    return ("nonzero", result.returncode, log_bytes[-4000:])
                pdf_path = tmpdir_path / "note.pdf"
                if not pdf_path.exists():
                    return ("missing", None, None)
                return ("ok", None, pdf_path.read_bytes())

        outcome, exit_code, payload = await _aio.to_thread(_do_compile)

    if outcome == "timeout":
        logger.warning("tectonic compile timed out after %ss for course=%s rid=%s",
                       _TECTONIC_TIMEOUT_SECONDS, req.course_id, rid)
        return JSONResponse(
            status_code=504,
            content={"error": "latex_compile_timeout",
                     "request_id": rid,
                     "detail": f"compile exceeded {_TECTONIC_TIMEOUT_SECONDS}s"},
        )
    if outcome == "nonzero":
        log_tail = (payload or b"").decode("utf-8", errors="replace")
        logger.info("tectonic compile exit=%s course=%s rid=%s",
                    exit_code, req.course_id, rid)
        return JSONResponse(
            status_code=422,
            content={"error": "latex_compile_failed",
                     "request_id": rid,
                     "log": log_tail,
                     "exit_code": exit_code},
        )
    if outcome == "missing":
        logger.warning("tectonic exit=0 but note.pdf missing for course=%s rid=%s",
                       req.course_id, rid)
        return JSONResponse(
            status_code=502,
            content={"error": "latex_compile_failed",
                     "request_id": rid,
                     "detail": "tectonic returned 0 but no PDF was emitted"},
        )

    pdf_bytes = payload  # type: ignore[assignment]
    session_log.append(req.course_id, "generation",
                       {"kind": "notes-pdf-compile", "bytes": len(pdf_bytes)})
    safe_name = (req.course_id or "note").replace("/", "_").replace("\\", "_")
    return StreamingResponse(
        iter([pdf_bytes]),
        media_type="application/pdf",
        headers={
            "Content-Disposition": _content_disposition(
                f"{safe_name}.pdf", disposition="attachment"
            ),
            "Content-Length": str(len(pdf_bytes)),
        },
    )


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

        concepts, relations = await extract_from_chunks(
            chunks, course_id, router, max_chunks=30,
            # R4-4: pass kb.embed_fn so the extractor caches concept_embedding
            # on every non-root concept for GraphRAG cosine ranking. Falls
            # back to lazy graph_search compute if this call fails.
            embed_fn=kb.embed_fn,
        )
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
                merged = {**existing, **patch, "user_edited": True}
                # fix-all v1 #B5 (R4-4 review-swarm): editing name or
                # definition invalidates the cached concept_embedding —
                # `_concept_embed_text` is `f"{name}。{definition}"`, so a
                # stale embedding would silently misrank the renamed
                # concept in graph_search. Drop it; graph_search lazy-
                # recomputes on next chat (single-node addition to the
                # batch — cost is negligible).
                if ("name" in patch or "definition" in patch) and \
                        merged.get("concept_embedding") is not None:
                    merged["concept_embedding"] = None
                nodes_by_id[nid] = merged
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
    # R5-1 fix-all v1 #F5: an existing mindmap_edits.json from before the
    # chapter-roots refactor references the legacy single-root id
    # `root_{course_id}`. Post-R5-1 the KG ships N roots with ids
    # `root_{course_id}__{chapter_slug}`, so any op pointing at the old
    # id silently turns into a "skipped: parent_id not found" result and
    # the student sees their previously-applied edits vanish. We rewrite
    # stale `root_{course_id}` references to the FIRST chapter root id
    # (alphabetically by name in `_kg_to_mindmap`'s ordering) so the
    # student's edits stay attached to a real node. Only triggers when
    # the legacy id is genuinely absent from the new KG — if a future
    # extractor revives the legacy id, no rewriting happens.
    nodes = kg_data.get("nodes") or []
    node_ids = {str(n.get("id") or n.get("concept_id") or "") for n in nodes}
    legacy_root_id = f"root_{course_id}"
    if ops and legacy_root_id not in node_ids:
        chapter_roots = [
            n for n in nodes
            if n.get("depth") == 0 or n.get("concept_type") == "root"
        ]
        if chapter_roots:
            chapter_roots.sort(
                key=lambda n: (str(n.get("name") or ""), str(n.get("id") or "")),
            )
            new_root_id = str(chapter_roots[0].get("id") or "")
            if new_root_id:
                ops = _rewrite_legacy_root_refs(ops, legacy_root_id, new_root_id)
    return apply_edit_ops(kg_data, ops)


def _rewrite_legacy_root_refs(
    ops: list[dict], legacy_root_id: str, new_root_id: str,
) -> list[dict]:
    """Return a copy of `ops` with `legacy_root_id` references redirected
    to `new_root_id`. Touches `parent_id` / `source` / `target`; leaves
    `id` alone for delete/update ops (those silently no-op via the
    existing missing-id guard rather than accidentally targeting the new
    chapter root). The original ops file on disk is not mutated — this
    is a load-time shim only."""
    rewritten: list[dict] = []
    for op in ops:
        op_copy = dict(op)
        for field in ("parent_id", "source", "target"):
            if op_copy.get(field) == legacy_root_id:
                op_copy[field] = new_root_id
        rewritten.append(op_copy)
    return rewritten


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


# R4-2: per-course upload-pipeline lock — two concurrent uploads to the
# same course id race on `chunks.json` / `course_meta.json` / FAISS index
# rebuild. Serialise them so the second waits behind the first instead of
# corrupting state. Keys are course_id strings; entries are reused across
# requests so concurrent same-course requests synchronise on the same lock.
# fix-all v1 #A1: bounded LRU so a flood of unique course_ids can't grow
# the dict unbounded; eviction only happens for locks that are not held
# and have no waiters.
_UPLOAD_LOCKS: dict[str, "_asyncio.Lock"] = {}
_UPLOAD_LOCKS_MAX = 512


def _upload_lock_for(course_id: str) -> "_asyncio.Lock":
    """fix-all v1 #A1: ``setdefault`` is the atomic read-or-create —
    closes the read-then-write TOCTOU window the reviewer flagged. Even
    though CPython's GIL currently makes the prior `if lock is None:`
    safe, the setdefault form is the documented idiom and stays correct
    under any future refactor that interposes an await.
    """
    import asyncio as _asyncio
    return _UPLOAD_LOCKS.setdefault(course_id, _asyncio.Lock())


def _maybe_evict_upload_lock(course_id: str) -> None:
    """fix-all v1 #A1: bound `_UPLOAD_LOCKS` growth. Called from the
    pipeline generator on exit; drops the lock only when nobody else
    is holding or waiting on it. Cheap O(1) check + pop.
    """
    lock = _UPLOAD_LOCKS.get(course_id)
    if lock is None:
        return
    if lock.locked():
        return
    # ``_waiters`` is a deque; truthy means at least one queued. The
    # underscore is fine here — same convention agent_loop.py already
    # uses for the cancel-event watcher pool.
    waiters = getattr(lock, "_waiters", None)
    if waiters:
        return
    # Cap-based emergency eviction: if the dict has bloated past the cap
    # and we're at a quiescent point, drop this one to keep growth bounded.
    if len(_UPLOAD_LOCKS) > _UPLOAD_LOCKS_MAX:
        _UPLOAD_LOCKS.pop(course_id, None)


def _ndjson(event: dict) -> str:
    return json.dumps(event, ensure_ascii=False) + "\n"


async def _save_uploaded_file(f: UploadFile, dest: Path, suffix: str) -> int:
    """Stream one upload body into ``dest`` with the 50MB cap enforced
    in 64KB chunks (fix-all v3 #H2). Returns bytes written. Caller is
    responsible for the .pptx / .docx zip-bomb check."""
    import asyncio as _asyncio
    chunk_size = 64 * 1024
    written = 0
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
                    try:
                        dest.unlink()
                    except OSError:
                        pass
                    raise HTTPException(
                        413,
                        f"File '{dest.name}' exceeds limit of {MAX_UPLOAD_SIZE_MB}MB",
                    )
                await _asyncio.to_thread(out.write, chunk)
        finally:
            if not out.closed:
                out.close()
    except HTTPException:
        raise
    except Exception:
        try:
            dest.unlink()
        except OSError:
            pass
        raise
    return written


@app.post("/api/upload/{course_id}", tags=["ingest"], summary="Upload files to a course (NDJSON-streamed pipeline)")
async def upload_files(course_id: str, files: Annotated[list[UploadFile], File(...)]):
    """R4-2: NDJSON-streamed upload pipeline.

    Saves files synchronously (size + suffix + zip-bomb checks raise
    HTTPException straight to the caller — no half-streamed errors), then
    returns ``application/x-ndjson`` with one event per stage:

    - ``{type:"stage", stage, progress, detail?}`` — one or more per stage,
      stages are ``chunking | embedding | kg_stage_a | kg_stage_b``
    - ``{type:"done", course_id, files, chunks, documents}`` — terminal success
    - ``{type:"error", error, stage?, partial?}`` — terminal failure

    Existing v3/v4 hardening (file-cap, zip-bomb, ``asyncio.to_thread``
    off-load) preserved verbatim; the streaming wrapper sits *outside*
    those guards.
    """
    import asyncio as _asyncio

    course_id = _validate_course_id_path(course_id)
    if not files:
        raise HTTPException(400, "No files provided")

    upload_dir = config.ARTIFACTS_DIR / "uploads" / course_id
    upload_dir.mkdir(parents=True, exist_ok=True)

    # ── Pre-stream: save files (errors short-circuit with HTTP 4xx) ──
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
        # fix-all v1 #6: reject control / bidi chars; defense vs filename
        # injection into LaTeX \section{} and Content-Disposition headers.
        safe_name = _safe_upload_name(f.filename)
        dest = upload_dir / safe_name
        written = await _save_uploaded_file(f, dest, suffix)
        if suffix in (".pptx", ".docx"):
            _check_zip_safety(dest, written)
        saved += 1

    if saved == 0:
        raise HTTPException(400, "No valid files saved")

    # ── Pre-stream: best-effort pptx → pdf sidecar generation ──
    # Runs INSIDE the chunking stage from the user's perspective (the
    # subprocess is dispatched via asyncio.to_thread before the chunking
    # stage event fires). Failures are silent — Reader text-mode is the
    # fallback. We snapshot the .pptx file list here so concurrent uploads
    # to the same course can't race on the rename.
    pptx_to_render = sorted(p for p in upload_dir.glob("*.pptx") if p.is_file())

    # ── Stream: 4 ingest stages, NDJSON one event per line ──
    async def _events():
        # fix-all v1 #A3: track which stage is current so the error event
        # surfaces the failing stage to the frontend (previously
        # `getattr(e, "stage", None)` was always None).
        current_stage: str | None = None
        chunks = []
        course_obj = None
        concepts: list = []
        t_start = time.monotonic()
        # Per-course pipeline lock prevents two concurrent uploads from
        # racing on chunks.json / FAISS index. Acquire inside the
        # generator so the wait time itself is observable as a delay
        # before any stage event fires (the client just sees a slow
        # connection — fine).
        async with _upload_lock_for(course_id):
            try:
                # Stage 1: chunking (synchronous; off-loaded to worker).
                # PPTX → PDF sidecar conversion runs inside this stage too
                # so the Reader's pdf-iframe path is ready by the time the
                # user clicks into Reader. Sidecar gen is best-effort: a
                # missing soffice or a single broken deck only logs a
                # warning, never aborts the upload (Reader text-mode is
                # the fallback).
                current_stage = "chunking"
                yield _ndjson({"type": "stage", "stage": "chunking", "progress": 0})
                if pptx_to_render and getattr(app.state, "pptx_pdf_available", False):
                    yield _ndjson({
                        "type": "stage", "stage": "chunking", "progress": 25,
                        "detail": {"sub": f"rendering {len(pptx_to_render)} pptx preview(s)"},
                    })
                    from nano_notebooklm.ingest.pptx_pdf import convert_directory
                    preview_dir = _preview_dir_for(course_id)
                    sidecar_results = await _asyncio.to_thread(
                        convert_directory, upload_dir, preview_dir,
                    )
                    rendered = sum(1 for v in sidecar_results.values() if v is not None)
                    yield _ndjson({
                        "type": "stage", "stage": "chunking", "progress": 50,
                        "detail": {"pptx_previews_rendered": rendered,
                                   "pptx_previews_total": len(sidecar_results)},
                    })
                course_obj = await _asyncio.to_thread(
                    kb.ingest_course, str(upload_dir), course_id
                )
                yield _ndjson({
                    "type": "stage", "stage": "chunking", "progress": 100,
                    "detail": {"documents": len(course_obj.documents)},
                })

                # Stage 2: embedding (FAISS + BM25 rebuild for the course).
                current_stage = "embedding"
                yield _ndjson({"type": "stage", "stage": "embedding", "progress": 0})
                await _asyncio.to_thread(kb.build_index, course_id)
                router_intent.clear_lang_cache(course_id)
                chunks = kb.get_chunks(course_id)
                yield _ndjson({
                    "type": "stage", "stage": "embedding", "progress": 100,
                    "detail": {"chunks": len(chunks)},
                })

                # Stages 3 + 4: KG extraction. The extractor's
                # progress_callback is invoked from the same loop, so a
                # plain queue + drain is enough to interleave with our
                # async generator.
                current_stage = "kg_stage_a"
                kg_queue: _asyncio.Queue = _asyncio.Queue(maxsize=64)

                def _progress(stage: str, pct: int):
                    try:
                        kg_queue.put_nowait({"type": "stage", "stage": stage, "progress": pct})
                    except _asyncio.QueueFull:
                        # fix-all v1 #A4: intermediate frames can be
                        # dropped under backpressure, but `100%` markers
                        # mark the end of a stage and MUST land. Block on
                        # them via a synchronous put attempt with a long
                        # wait — only intermediates ever get dropped.
                        if pct == 100:
                            # We're inside a sync callback in an async
                            # coroutine; can't await. Best-effort: drain
                            # the queue head and retry once. Drain is safe
                            # because the consumer is the same task that
                            # invoked us (we are synchronous within their
                            # `await`), so consumer cannot be racing.
                            try:
                                kg_queue.get_nowait()
                                kg_queue.put_nowait({"type": "stage", "stage": stage, "progress": pct})
                            except (_asyncio.QueueEmpty, _asyncio.QueueFull):
                                pass

                from nano_notebooklm.kg.extractor import extract_from_chunks
                from nano_notebooklm.kg.graph import KnowledgeGraph
                from nano_notebooklm.kg.merger import merge_concepts, merge_relations

                async def _extract_task():
                    if not chunks:
                        # Empty corpus — emit zero-length stage events so
                        # the client sees the 4-stage contract regardless.
                        _progress("kg_stage_a", 100)
                        _progress("kg_stage_b", 100)
                        return [], []
                    return await extract_from_chunks(
                        chunks, course_id, router, max_chunks=30,
                        progress_callback=_progress,
                        # R4-4: pass kb.embed_fn so concept_embedding is
                        # cached during upload, eliminating the lazy-compute
                        # cost on the first GraphRAG /api/chat call.
                        embed_fn=kb.embed_fn,
                    )

                extract_task = _asyncio.create_task(_extract_task())
                # Drain the queue until extraction completes. Use wait()
                # so we can flush any final events after the task ends.
                while not extract_task.done():
                    try:
                        ev = await _asyncio.wait_for(kg_queue.get(), timeout=0.5)
                        # Update current_stage from the event so the error
                        # event reports the actual stage the extractor
                        # was inside when it threw.
                        if isinstance(ev, dict) and ev.get("stage"):
                            current_stage = ev["stage"]
                        yield _ndjson(ev)
                    except _asyncio.TimeoutError:
                        continue
                # fix-all v1 #A5: drain remaining events BEFORE awaiting
                # the task (which re-raises on failure). Previously the
                # exception path lost any final-event-just-queued frames.
                while not kg_queue.empty():
                    try:
                        ev = kg_queue.get_nowait()
                        if isinstance(ev, dict) and ev.get("stage"):
                            current_stage = ev["stage"]
                        yield _ndjson(ev)
                    except _asyncio.QueueEmpty:
                        break

                concepts, relations = await extract_task
                concepts = merge_concepts(concepts)
                relations = merge_relations(relations)
                kg = KnowledgeGraph()
                kg.add_concepts(concepts)
                kg.add_relations(relations)
                kg_path = config.ARTIFACTS_DIR / "courses" / course_id / "knowledge_graph.json"
                await _asyncio.to_thread(kg.save, kg_path)

                # Done.
                current_stage = None
                duration_ms = int((time.monotonic() - t_start) * 1000)
                # fix-all v1 #A11: one-line structured log for ops triage.
                # Matches the `qa.path=` / `chunks.fetch course=` patterns
                # elsewhere in this file.
                logger.info(
                    "upload.done course=%s files=%d chunks=%d documents=%d kg_nodes=%d duration_ms=%d",
                    course_id, saved, len(chunks),
                    len(course_obj.documents) if course_obj else 0,
                    len(concepts), duration_ms,
                )
                yield _ndjson({
                    "type": "done",
                    "course_id": course_id,
                    "files": saved,
                    "chunks": len(chunks),
                    "documents": len(course_obj.documents) if course_obj else 0,
                    "kg_nodes": len(concepts),
                    "duration_ms": duration_ms,
                })
            except Exception:
                # fix-all v4 #A3: stable error code, no vendor leakage.
                # fix-all v1 #A3: attach actual failing stage so the
                # frontend can mark the right row with ✕.
                duration_ms = int((time.monotonic() - t_start) * 1000)
                logger.exception(
                    "upload.error course=%s stage=%s duration_ms=%d",
                    course_id, current_stage, duration_ms,
                )
                yield _ndjson({
                    "type": "error",
                    "error": "upload_pipeline_failed",
                    "stage": current_stage,
                })
            finally:
                # fix-all v1 #A1: opportunistic eviction when the dict
                # exceeds the soft cap. Only runs at a quiescent point
                # (we're about to release the lock), so this can't race
                # with another acquirer.
                _maybe_evict_upload_lock(course_id)

    return StreamingResponse(_events(), media_type="application/x-ndjson")


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

    # R4-5 part 2: surface qwen_raft health to the frontend so the backend
    # chip can grey out when the AutoDL host is unreachable. Two facts:
    #   - qwen_raft_configured = env var is set (operator opted in)
    #   - qwen_raft_available  = health_check returned ok within 2s
    # The health probe is wrapped in wait_for + broad except so this
    # endpoint never 500s on a flaky AutoDL backend.
    # fix-all v1 #V2 (R4-5 review v1): cache the qwen health probe so
    # the frontend's 10s status poll across N tabs doesn't pin AutoDL
    # with 6N outbound requests/min. TTL=15s means worst-case rate is
    # 4 outbound/min regardless of tab count. Failure is cached too —
    # no point hammering a dead host once we know it's dead.
    qwen_configured = bool(config.QWEN_RAFT_URL)
    qwen_available = False
    if qwen_configured and "qwen_raft" in router.backends:
        cached = getattr(app.state, "qwen_health_cache", None)
        now = time.monotonic()
        if cached is not None and (now - cached[0]) < QWEN_HEALTH_TTL_SECONDS:
            qwen_available = bool(cached[1])
        else:
            try:
                qwen_health = await asyncio.wait_for(
                    router.backends["qwen_raft"].health_check(), timeout=2.0,
                )
                qwen_available = bool(qwen_health.get("ok"))
            except Exception:  # noqa: BLE001 — status must never 500
                qwen_available = False
            app.state.qwen_health_cache = (now, qwen_available)

    # Settings page (A 档, 2026-05-12): expose model + base-URL + API-key
    # *configuration state* so the frontend Settings tab can render
    # "已配置 / 未配置" badges without ever seeing the secret values. Booleans
    # only for API keys — `bool(config.OPENAI_API_KEY)` collapses the empty
    # string default to False without leaking the actual key.
    qwen_url_host: str | None = None
    if config.QWEN_RAFT_URL:
        try:
            qwen_url_host = urllib_parse.urlparse(config.QWEN_RAFT_URL).hostname
        except Exception:
            qwen_url_host = None

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
        # LaTeX-refactor: frontend reads this to decide whether to show the
        # "高质量编译" PDF button (vs. only the browser-print fallback).
        "tectonic_available": bool(getattr(app.state, "tectonic_available", False)),
        # PPTX → PDF sidecar converter (LibreOffice). When True, uploaded
        # pptx files get a renderable PDF preview and the Reader's
        # native iframe viewer instead of falling back to chunk-text mode.
        "pptx_pdf_available": bool(getattr(app.state, "pptx_pdf_available", False)),
        # fix-all v2 #V2: surface embed warm state so operators see a
        # degraded backend without grepping logs. None = warm-up still
        # in flight (fire-and-forget hasn't resolved yet); False = failed.
        "embed_warm_ok": getattr(app.state, "embed_warm_ok", None),
        # R4-5 part 2: qwen_raft backend chip uses these to decide
        # disabled state + tooltip wording.
        "qwen_raft_configured": qwen_configured,
        "qwen_raft_available": qwen_available,
        # Settings page read-only model/key/base-URL surface.
        "default_backend": config.DEFAULT_BACKEND,
        "openai_model": config.OPENAI_MODEL,
        "openai_base_url": config.OPENAI_BASE_URL,
        "openai_api_key_configured": bool(config.OPENAI_API_KEY),
        "claude_model": config.CLAUDE_MODEL,
        "anthropic_api_key_configured": bool(config.ANTHROPIC_API_KEY),
        "qwen_raft_model_name": config.QWEN_RAFT_MODEL_NAME if qwen_configured else None,
        "qwen_raft_url_host": qwen_url_host,
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
            # LaTeX-refactor: notes ship raw LaTeX — format_response would
            # corrupt it (markdown repairs vs LaTeX syntax). Reports still
            # go through the markdown repairer.
            if kind == "notes":
                content = partial
            else:
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
            # LaTeX-refactor: only "report" still uses markdown repair;
            # notes ship raw LaTeX so the frontend can edit + compile.
            if kind == "report":
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

    R5-1 (2026-05-11): KGs now ship N chapter roots (one per source_file)
    rather than a single course root. The payload surfaces all of them
    via `rootIds: list[str]`; `rootId` / `id` / `children` keep the
    first chapter's view for back-compat with any caller that hasn't
    migrated to multi-root awareness. The frontend's force layout reads
    `nodes` / `edges` directly so it always sees all roots regardless of
    which one is mirrored at the top level.
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
            "rootIds": [],
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
    # chapter-card root rather than fall through to the legacy heuristic.
    # R5-1: collect ALL roots (one per chapter), not just the first.
    explicit_roots = [
        n for n in normalized_nodes
        if n.get("depth") == 0 or n.get("concept_type") == "root"
    ]

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

    if explicit_roots:
        # Stable ordering: by node name so the first-root-mirrored view
        # doesn't flip between requests.
        explicit_roots.sort(key=lambda n: (str(n.get("name") or ""), str(n.get("id") or "")))
        primary = explicit_roots[0]
        tree = build_tree(primary["id"], depth=0)
        return {
            "id": primary["id"],
            "label": primary.get("name", course_id),
            "definition": primary.get("definition", ""),
            "concept_type": "root",
            "nodes": normalized_nodes,
            "edges": normalized_edges,
            "children": tree.get("children", []),
            # R5-1: surface every chapter root so the frontend's multi-root
            # layout can place them all. Single-root legacy KGs report a
            # one-element list — old clients reading only `id` still work.
            "rootIds": [n["id"] for n in explicit_roots],
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
        # R5-1: legacy fallback exposes a single rootId for the heuristic
        # pick so the frontend's multi-root code path can treat it uniformly.
        "rootIds": [chosen_root["id"]],
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
