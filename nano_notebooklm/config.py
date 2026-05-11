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
QWEN_RAFT_URL = os.getenv("QWEN_RAFT_URL", "").rstrip("/")
QWEN_RAFT_TOKEN = os.getenv("QWEN_RAFT_TOKEN", "")
QWEN_RAFT_MODEL_NAME = os.getenv("QWEN_RAFT_MODEL_NAME", "qwen2.5-7b-raft")
QWEN_RAFT_HTTP_TIMEOUT = float(os.getenv("QWEN_RAFT_HTTP_TIMEOUT", "60"))

# ── Embedding ────────────────────────────────────────────────────────
EMBEDDING_MODE = os.getenv("EMBEDDING_MODE", "local")  # "local" or "api"
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2")

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
    "exam_analysis": "openai",
    "report_writing": "claude",
    "cross_review": "alternate",
}
