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
