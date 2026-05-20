"""Central configuration for nano-NOTEBOOKLM."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS_DIR = Path(os.getenv("ARTIFACTS_DIR", str(PROJECT_ROOT / "artifacts")))

# ── AI backends ──────────────────────────────────────────────────────
# OpenAI-compatible provider. Works with OpenAI, DeepSeek, Moonshot,
# Together, Groq, Gemini (OpenAI-compat endpoint), any OneAPI-style
# proxy, etc. Just point OPENAI_BASE_URL at the provider's /v1.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# Anthropic Claude (native API).
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5")

# Local model via Ollama / vLLM / LM Studio / llama.cpp / etc. Any
# OpenAI-compatible /v1 endpoint works. LOCAL_LLM_API_KEY is sent as
# the Bearer token; most local servers ignore it but the OpenAI SDK
# requires a non-empty string.
LOCAL_LLM_BASE_URL = os.getenv("LOCAL_LLM_BASE_URL", "")
LOCAL_LLM_MODEL = os.getenv("LOCAL_LLM_MODEL", "")
LOCAL_LLM_API_KEY = os.getenv("LOCAL_LLM_API_KEY", "local")

# Default backend when a task doesn't specify one: openai | claude | local
DEFAULT_BACKEND = os.getenv("DEFAULT_BACKEND", "openai")

# Embeddings can run on a separate OpenAI-compatible endpoint (e.g.
# main chat goes to a provider without /v1/embeddings, embeddings go
# to OpenAI). Falls back to the main OPENAI_* settings.
EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY", "") or OPENAI_API_KEY
EMBEDDING_API_BASE_URL = os.getenv("EMBEDDING_API_BASE_URL", "") or OPENAI_BASE_URL

# ── Embedding ────────────────────────────────────────────────────────
# - "local" → sentence-transformers (offline, downloads model on first
#   use). Default multilingual MiniLM handles 50+ languages incl. CJK.
# - "api"   → OpenAI-compatible /v1/embeddings endpoint.
#
# Three user-selectable presets are surfaced in the Settings UI. The active
# preset writes its (mode, model) pair into a small JSON preference file
# under ARTIFACTS_DIR so the choice survives restart without env edits.
# Indices are kept in per-preset namespaces (kb/store.py → indices/faiss/
# <preset_id>/...) so toggling between presets is a cheap path-route, not
# a destructive rebuild — switching back to a previously-used preset is
# instant.
EMBEDDING_PRESETS: dict[str, dict] = {
    "local_mini": {
        "label": "Local MiniLM",
        "description": "本地 sentence-transformers · 多语言 · 0 配置",
        "mode": "local",
        "model": "paraphrase-multilingual-MiniLM-L12-v2",
        "dim": 384,
        "requires_api_key": False,
        "download_size_mb": 470,
    },
    "openai_large": {
        "label": "OpenAI API",
        "description": "OpenAI 兼容 /v1/embeddings · text-embedding-3-large · 需要 API key",
        "mode": "api",
        "model": "text-embedding-3-large",
        "dim": 3072,
        "requires_api_key": True,
        "download_size_mb": 0,
    },
    "bge_m3": {
        "label": "BGE-M3",
        "description": "BAAI/bge-m3 本地多语言强力模型 · 首次下载 ~2GB",
        "mode": "local",
        "model": "BAAI/bge-m3",
        "dim": 1024,
        "requires_api_key": False,
        "download_size_mb": 2200,
    },
}

EMBEDDING_PREFERENCE_FILE = ARTIFACTS_DIR / "embedding_preference.json"


def _load_embedding_preference() -> str | None:
    """Return the preset_id persisted in the preference file, or None.

    Defensive: missing file / unreadable / unknown preset all return None
    so the caller falls back to env-derived defaults.
    """
    try:
        if not EMBEDDING_PREFERENCE_FILE.exists():
            return None
        data = json.loads(EMBEDDING_PREFERENCE_FILE.read_text())
        if not isinstance(data, dict):
            return None
        pid = data.get("preset_id")
        if pid in EMBEDDING_PRESETS:
            return pid
    except Exception as exc:  # noqa: BLE001 — preference file is best-effort
        logger.warning(
            "embedding preference file unreadable (%s); falling back to env defaults",
            type(exc).__name__,
        )
    return None


def save_embedding_preference(preset_id: str) -> None:
    """Persist preset choice AND mutate module-level EMBEDDING_MODE/MODEL
    so subsequent reads (kb.store, /api/status) see the new values without
    a process restart.
    """
    if preset_id not in EMBEDDING_PRESETS:
        raise ValueError(f"unknown embedding preset: {preset_id!r}")
    EMBEDDING_PREFERENCE_FILE.parent.mkdir(parents=True, exist_ok=True)
    EMBEDDING_PREFERENCE_FILE.write_text(
        json.dumps({"preset_id": preset_id}, ensure_ascii=False, indent=2)
    )
    preset = EMBEDDING_PRESETS[preset_id]
    global EMBEDDING_MODE, EMBEDDING_MODEL
    EMBEDDING_MODE = preset["mode"]
    EMBEDDING_MODEL = preset["model"]


def active_preset_id() -> str:
    """Return the preset_id whose (mode, model) matches current config.
    Returns "custom" when the operator overrode EMBEDDING_MODEL via env to
    a value that doesn't match any preset — the UI then shows the active
    radio as "未选择（自定义 env 配置）" and switches are still allowed.
    """
    for pid, p in EMBEDDING_PRESETS.items():
        if p["mode"] == EMBEDDING_MODE and p["model"] == EMBEDDING_MODEL:
            return pid
    return "custom"


_env_embedding_mode = os.getenv("EMBEDDING_MODE", "local")
_env_embedding_model = os.getenv("EMBEDDING_MODEL", "")
_pref_preset_id = _load_embedding_preference()
if _pref_preset_id is not None:
    _pref_preset = EMBEDDING_PRESETS[_pref_preset_id]
    EMBEDDING_MODE = _pref_preset["mode"]
    EMBEDDING_MODEL = _pref_preset["model"]
else:
    EMBEDDING_MODE = _env_embedding_mode
    EMBEDDING_MODEL = _env_embedding_model or (
        "text-embedding-3-small" if EMBEDDING_MODE == "api"
        else "paraphrase-multilingual-MiniLM-L12-v2"
    )

# ── Chunking defaults ────────────────────────────────────────────────
CHUNK_SIZE_TOKENS = 512
CHUNK_OVERLAP_TOKENS = 64
MIN_CHUNK_TOKENS = 50

# ── Search defaults ──────────────────────────────────────────────────
DEFAULT_TOP_K = 5
RRF_K = 60  # Reciprocal Rank Fusion constant

# ── Orchestrator ─────────────────────────────────────────────────────
MAX_PARALLEL_WORKERS = 4
CHECKPOINT_MODE = os.getenv("CHECKPOINT_MODE", "auto")


# ── Task → backend routing ──────────────────────────────────────────
# Override per task type by editing this map. Unknown task types fall
# back to DEFAULT_BACKEND. The router picks any available backend if
# the requested one isn't configured.
TASK_ROUTES: dict[str, str] = {
    "concept_extraction": "openai",
    "note_generation": "openai",
    "quiz_generation": "openai",
    "qa_answer": "openai",
    "qa_general": "openai",
    "translate_query": "openai",
    "rewrite_history": "openai",
    "exam_analysis": "openai",
    "exam_prep_plan": "openai",
    "exam_prep_questions": "openai",
    "report_writing": "openai",
    "cross_review": "alternate",
}
