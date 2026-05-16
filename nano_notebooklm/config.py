"""Central configuration for nano-NOTEBOOKLM."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

# ── Paths ────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS_DIR = Path(os.getenv("ARTIFACTS_DIR", str(PROJECT_ROOT / "artifacts")))
COURSE_DATA_DIR = Path(os.getenv("COURSE_DATA_DIR", ""))

# ── AI backends ──────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
DEFAULT_BACKEND = os.getenv("DEFAULT_BACKEND", "claude")

# ── Model defaults ───────────────────────────────────────────────────
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")

# ── Round 4 #R4-5 — Qwen-RAFT remote backend ────────────────────────
# AutoDL `scripts/app.py` exposes a Gradio service on :6006 with the
# RAFT-fine-tuned Qwen2.5-7B-Instruct. Leave the env vars empty in the
# default deployment; setting QWEN_RAFT_URL is the opt-in to surface
# the backend chip in /api/status and accept ChatRequest.backend="qwen_raft".
#
# fix-all v1 #V3 (R4-5 review v1): validate QWEN_RAFT_URL at config-load
# so a typo / supply-chain `.env` doesn't turn the backend client into
# an SSRF prober that POSTs the full chat prompt to a cloud metadata
# service. Unsafe values produce an empty URL + a warning log; the rest
# of the codebase already handles `configured=False` gracefully (chip
# greys out, /api/chat 422s if user pins backend="qwen_raft").
import logging as _logging
import urllib.parse as _urllib_parse

_logger = _logging.getLogger(__name__)

_BLOCKED_HOSTNAMES = frozenset({
    # Cloud instance metadata services
    "169.254.169.254",      # AWS / Azure / DigitalOcean / etc.
    "metadata.google.internal",
    "metadata",
    "100.100.100.200",      # Aliyun
})


def _validate_qwen_url(raw: str) -> str:
    """fix-all v1 #V3: scheme + host allow-list. Returns "" if invalid."""
    raw = (raw or "").strip().rstrip("/")
    if not raw:
        return ""
    try:
        parsed = _urllib_parse.urlparse(raw)
    except Exception:
        _logger.warning("QWEN_RAFT_URL=%r failed to parse; disabling qwen backend", raw)
        return ""
    if parsed.scheme not in ("http", "https"):
        _logger.warning(
            "QWEN_RAFT_URL must be http or https (got %r); disabling qwen backend",
            parsed.scheme,
        )
        return ""
    host = (parsed.hostname or "").lower()
    if not host:
        _logger.warning("QWEN_RAFT_URL=%r missing hostname; disabling qwen backend", raw)
        return ""
    # Reject IMDS endpoints regardless of scheme. The set is small; a
    # full link-local subnet block would also be safer but breaks
    # legitimate `127.0.0.1` / `localhost` dev setups.
    if host in _BLOCKED_HOSTNAMES:
        _logger.warning(
            "QWEN_RAFT_URL host %r is a cloud metadata endpoint; refusing", host,
        )
        return ""
    if parsed.scheme == "http" and host not in ("localhost", "127.0.0.1", "::1"):
        # Plaintext to a non-loopback host = prompt leaks over the wire.
        _logger.warning(
            "QWEN_RAFT_URL uses http scheme on non-loopback host %r; "
            "prompts will travel in plaintext", host,
        )
    return raw


QWEN_RAFT_URL = _validate_qwen_url(os.getenv("QWEN_RAFT_URL", ""))
QWEN_RAFT_TOKEN = os.getenv("QWEN_RAFT_TOKEN", "")
QWEN_RAFT_MODEL_NAME = os.getenv("QWEN_RAFT_MODEL_NAME", "qwen2.5-7b-raft")
QWEN_RAFT_HTTP_TIMEOUT = float(os.getenv("QWEN_RAFT_HTTP_TIMEOUT", "60"))
# 2026-05-13: parallel base Qwen2.5-7B-Instruct service for A/B compare
# against RAFT. Same validation contract as the RAFT URL. Empty / unset
# means base option is disabled in the Settings UI.
QWEN_BASE_URL = _validate_qwen_url(os.getenv("QWEN_BASE_URL", ""))
QWEN_BASE_MODEL_NAME = os.getenv("QWEN_BASE_MODEL_NAME", "qwen2.5-7b-instruct")

# ── Embedding ────────────────────────────────────────────────────────
# 2026-05-13: defaults switched to multilingual.
# - "api" mode + "text-embedding-3-small": cos(zh, en) ≈ 0.82 (cross-lingual)
# - "local" mode default now points at the multilingual MiniLM variant
#   (paraphrase-multilingual-MiniLM-L12-v2, 471M, 384-dim, 50+ languages).
#   The old "all-MiniLM-L6-v2" was English-only and ranked irrelevant Chinese
#   chunks above on-topic English ones for any mixed-language query.
EMBEDDING_MODE = os.getenv("EMBEDDING_MODE", "api")
EMBEDDING_MODEL = os.getenv(
    "EMBEDDING_MODEL",
    "text-embedding-3-small" if os.getenv("EMBEDDING_MODE", "api") == "api"
    else "paraphrase-multilingual-MiniLM-L12-v2",
)

# ── Chunking defaults ────────────────────────────────────────────────
CHUNK_SIZE_TOKENS = 512
CHUNK_OVERLAP_TOKENS = 64
MIN_CHUNK_TOKENS = 50

# ── Search defaults ──────────────────────────────────────────────────
DEFAULT_TOP_K = 5
RRF_K = 60  # Reciprocal Rank Fusion constant

# ── Orchestrator ─────────────────────────────────────────────────────
CHECKPOINT_MODE = os.getenv("CHECKPOINT_MODE", "interactive")
MAX_PARALLEL_WORKERS = 4

# ── Round 4 #R4-1 ────────────────────────────────────────────────────
# 8 门预置课是 Round 1 ingest 的产物。Round 4 改成 upload-only，UI 默认
# 调 /api/courses?mode=user 把这 8 门过滤掉，留作回滚点（?show_preset=1）。
# 物理文件保留在 artifacts/courses/<id>/ 直到 R4-4 GraphRAG 验收过。
PRESET_COURSE_IDS: frozenset[str] = frozenset({
    "15-213",
    "CS182",
    "CS231N",
    "CS285",
    "CSE 234",
    "机器人导论",
    "计算机组成原理",
    "模式识别",
})


# ── Task → model routing ────────────────────────────────────────────
TASK_ROUTES: dict[str, str] = {
    "concept_extraction": "claude",
    "note_generation": "claude",
    "quiz_generation": "openai",
    "qa_answer": "claude",
    "qa_general": "claude",
    "translate_query": "openai",
    # 2026-05-16: multi-turn history rewrite — small disambiguation task,
    # codex/openai handles it fast and reliably. Pin here so it doesn't
    # default-route to a heavier / less-available backend (qwen_raft on
    # dev hosts) where 502s would silently fall back to the original
    # question every time.
    "rewrite_history": "openai",
    "exam_analysis": "openai",
    "report_writing": "claude",
    "cross_review": "alternate",
}
