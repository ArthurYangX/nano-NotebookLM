"""R4-5 part 2 review-swarm fix-all v1 regression tests.

Closes the 1 CRITICAL + 10 MEDIUM findings from the 4-route review on
commit 6d2e590:

  V1 codex alias hard assumption → treat backend="codex" as default
     task routing (not a hard openai pin). Fallback follows the same.
  V2 /api/status caches qwen health probe with QWEN_HEALTH_TTL_SECONDS
     so the 10s frontend poll doesn't trigger an outbound request per
     tab per cycle.
  V3 QWEN_RAFT_URL validated at config-load (scheme + host allow-list)
     so SSRF / cloud-metadata probes are refused before reaching the
     backend client.
  V4 _complete_with_backend_fallback uses max_retries=1 (the 30s
     outer wait_for is the budget); except clause narrows to
     (QwenBackendError, RuntimeError, httpx.HTTPError); log scrubs
     `str(exc)` by preferring `getattr(exc, "code", type(exc).__name__)`.
  V5 QWEN_BACKEND_TIMEOUT_SECONDS reads from env (defaults to 30.0).
  V6 Frontend chip auto-rollbacks to "codex" when /api/status reports
     qwen unavailable; polling interval gets ±20% jitter.
  V7 .env.example documents the new env knobs.
  V8 ChatRequest.backend / ChatResponse.backend_fallback gain
     Field(description=...); the test_r4_4_fix_all_v2 grep window
     swaps a magic char count for a sentinel-based slice.
"""

from __future__ import annotations

import importlib
import json
import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent


# ── V1: codex alias no longer forces openai (claude-only safe) ────────


def test_qa_skill_helper_codex_uses_default_task_routing():
    """Source pin: the _complete_with_backend_fallback helper must NOT
    pass an explicit `backend='codex'` kwarg to router.complete at the
    fallback call site. v1 used to alias codex→openai and pin the
    router, which 500s when OPENAI_API_KEY is unset (claude-only /
    qwen-only deployments)."""
    src = (REPO_ROOT / "nano_notebooklm" / "skills" / "qa_skill.py").read_text(
        encoding="utf-8"
    )
    helper = src[src.index("async def _complete_with_backend_fallback"):
                 src.index("async def _complete_with_backend_fallback") + 4000]
    # Match `await self.router.complete(...)` calls inside the helper
    # and assert none of them carries a literal `backend="codex"` kwarg.
    call_pattern = re.compile(
        r"await\s+self\.router\.complete\(([^)]+)\)",
        re.DOTALL,
    )
    for match in call_pattern.finditer(helper):
        args = match.group(1)
        assert 'backend="codex"' not in args, (
            "router.complete must not pin backend='codex' at call site "
            "(claude-only deployments would 500): " + args[:200]
        )


@pytest.mark.asyncio
async def test_qa_skill_helper_codex_default_routing_works_without_openai_key(
    monkeypatch,
):
    """When backend="codex" arrives, the helper falls through to default
    task routing — no explicit openai pin, so a deployment with claude
    only (no OPENAI_API_KEY) still serves /api/chat normally."""
    from nano_notebooklm.skills.qa_skill import QASkill
    from nano_notebooklm.types import LLMResponse

    class _RouterStub:
        def __init__(self):
            self.calls: list[dict] = []

        async def complete(self, prompt, task_type="", system="", temperature=0.7,
                           max_tokens=4096, max_retries=3, backend=None):
            self.calls.append({"prompt": prompt, "task_type": task_type,
                               "backend": backend, "max_retries": max_retries})
            if backend == "qwen_raft":
                raise RuntimeError("qwen_raft not configured")
            # Default routing path — no exception, return a stub.
            return LLMResponse(content="default-backend-answer", model="claude",
                               input_tokens=1, output_tokens=1, latency_ms=1.0)

    # QASkill requires kb + router for Skill.__init__; we only exercise
    # _complete_with_backend_fallback which uses self.router, so kb=None
    # is fine.
    skill = QASkill.__new__(QASkill)
    skill.kb = None
    skill.router = _RouterStub()

    # backend="codex" path — must NOT pass backend kwarg downstream.
    resp, fell_back = await skill._complete_with_backend_fallback(
        "test prompt", task_type="qa_answer", system="", temperature=0.3,
        backend="codex",
    )
    assert fell_back is False
    assert resp.content == "default-backend-answer"
    # Verify router was called WITHOUT explicit backend kwarg.
    assert len(skill.router.calls) == 1
    assert skill.router.calls[0]["backend"] is None


# ── V2: /api/status caches qwen health probe ──────────────────────────


def test_status_endpoint_caches_qwen_health_probe():
    """Source pin: status_endpoint must consult app.state.qwen_health_cache
    + QWEN_HEALTH_TTL_SECONDS before firing a fresh health_check."""
    src = (REPO_ROOT / "api" / "server.py").read_text(encoding="utf-8")
    start = src.index("async def status_endpoint")
    end = src.index("\nasync def ", start + 1)
    block = src[start:end]
    assert "QWEN_HEALTH_TTL_SECONDS" in block
    assert "qwen_health_cache" in block
    # Constant must be defined at module level.
    assert "QWEN_HEALTH_TTL_SECONDS = " in src


# ── V3: QWEN_RAFT_URL validation rejects unsafe values ────────────────


def test_qwen_raft_url_rejects_metadata_host(monkeypatch):
    """A typo / supply-chain `.env` setting QWEN_RAFT_URL to the AWS
    IMDS endpoint must NOT make it into config.QWEN_RAFT_URL — the
    health probe + qwen complete would otherwise POST chat prompts to
    the metadata service."""
    from nano_notebooklm import config as cfg
    for evil_url in (
        "http://169.254.169.254/latest/meta-data/",
        "http://metadata.google.internal/v1/",
        "http://100.100.100.200/aliyun",
    ):
        result = cfg._validate_qwen_url(evil_url)
        assert result == "", f"{evil_url!r} should be refused"


def test_qwen_raft_url_rejects_non_http_scheme():
    from nano_notebooklm import config as cfg
    for bad in ("file:///etc/passwd", "ftp://internal", "ldap://corp"):
        assert cfg._validate_qwen_url(bad) == ""


def test_qwen_raft_url_accepts_https_external():
    from nano_notebooklm import config as cfg
    assert cfg._validate_qwen_url("https://autodl.example.gradio.live") == (
        "https://autodl.example.gradio.live"
    )
    # Trailing slash stripped.
    assert cfg._validate_qwen_url("https://x.gradio.live/") == "https://x.gradio.live"


def test_qwen_raft_url_accepts_loopback_http():
    """Loopback http is allowed (legitimate dev / docker-compose setup),
    but a warning is logged (not asserted here)."""
    from nano_notebooklm import config as cfg
    assert cfg._validate_qwen_url("http://localhost:6006") == "http://localhost:6006"
    assert cfg._validate_qwen_url("http://127.0.0.1:6006") == "http://127.0.0.1:6006"


def test_qwen_raft_url_empty_returns_empty():
    from nano_notebooklm import config as cfg
    assert cfg._validate_qwen_url("") == ""
    assert cfg._validate_qwen_url("   ") == ""


# ── V4: max_retries=1 + narrow except + log scrub ─────────────────────


def test_qa_skill_qwen_path_uses_max_retries_1():
    src = (REPO_ROOT / "nano_notebooklm" / "skills" / "qa_skill.py").read_text(
        encoding="utf-8"
    )
    helper = src[src.index("async def _complete_with_backend_fallback"):
                 src.index("async def _complete_with_backend_fallback") + 4000]
    # The qwen branch (inside the wait_for) must set max_retries=1.
    assert "max_retries=1" in helper, \
        "qwen call must use max_retries=1 — outer wait_for is the budget"


def test_qa_skill_qwen_except_narrowed_from_bare_exception():
    src = (REPO_ROOT / "nano_notebooklm" / "skills" / "qa_skill.py").read_text(
        encoding="utf-8"
    )
    helper = src[src.index("async def _complete_with_backend_fallback"):
                 src.index("async def _complete_with_backend_fallback") + 4000]
    # No bare `except Exception as exc:` in the qwen branch — must use
    # _QWEN_EXPECTED_ERRORS tuple instead.
    assert "_QWEN_EXPECTED_ERRORS" in helper
    assert "except Exception as exc:" not in helper, \
        "qwen except clause must narrow from `Exception` to specific types"


def test_qa_skill_qwen_log_scrubs_exception_str():
    src = (REPO_ROOT / "nano_notebooklm" / "skills" / "qa_skill.py").read_text(
        encoding="utf-8"
    )
    helper = src[src.index("async def _complete_with_backend_fallback"):
                 src.index("async def _complete_with_backend_fallback") + 4000]
    # The qwen failure log line must NOT format exc with %s body —
    # only the stable code / type name should reach logs.
    assert "type(exc).__name__" in helper or "getattr(exc, " in helper
    # Old leaky format `"%s: %s", type(exc).__name__, exc` must be gone.
    assert "type(exc).__name__, exc)" not in helper, \
        "log line must not interpolate raw exc body (PII risk)"


# ── V5: QWEN_BACKEND_TIMEOUT_SECONDS env override ─────────────────────


def test_qwen_backend_timeout_reads_env(monkeypatch):
    monkeypatch.setenv("QWEN_BACKEND_TIMEOUT_SECONDS", "45.5")
    from nano_notebooklm.skills import qa_skill
    assert qa_skill._qwen_backend_timeout() == pytest.approx(45.5)


def test_qwen_backend_timeout_rejects_invalid_values(monkeypatch):
    from nano_notebooklm.skills import qa_skill
    for bad in ("not-a-float", "0", "-5", "inf"):
        monkeypatch.setenv("QWEN_BACKEND_TIMEOUT_SECONDS", bad)
        assert qa_skill._qwen_backend_timeout() == 30.0


# ── V6: frontend chip auto-rollback + polling jitter ──────────────────


def test_app_jsx_has_chip_auto_rollback():
    """Pin the useEffect that resets backend → codex when qwen becomes
    unavailable. Without this, localStorage holds a stale "qwen_raft"
    across operator-side env changes and every chat 422s."""
    src = (REPO_ROOT / "frontend" / "app.jsx").read_text(encoding="utf-8")
    # The auto-rollback block must:
    # - watch backendStatus
    # - check qwen_raft_available / qwen_raft_configured
    # - call commitBackend("codex")
    assert 'backendStatus' in src
    assert "qwen_raft_available" in src and "qwen_raft_configured" in src
    assert 'commitBackend("codex")' in src


def test_app_jsx_status_polling_has_jitter():
    """Polling 10s base + ±20% jitter so concurrent tabs don't pulse
    AutoDL in unison."""
    src = (REPO_ROOT / "frontend" / "app.jsx").read_text(encoding="utf-8")
    # POLL_BASE_MS + POLL_JITTER_RATIO constants must be defined.
    assert "POLL_JITTER_RATIO" in src
    assert "Math.random()" in src


# ── V7: .env.example documents new envs ───────────────────────────────


def test_env_example_documents_qwen_raft_envs():
    src = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    for var in (
        "QWEN_RAFT_URL", "QWEN_RAFT_TOKEN", "QWEN_RAFT_MODEL_NAME",
        "QWEN_RAFT_HTTP_TIMEOUT", "QWEN_BACKEND_TIMEOUT_SECONDS",
        "QWEN_HEALTH_TTL_SECONDS",
    ):
        assert var in src, f"{var} missing from .env.example"


# ── V8: Field(description=...) lands in OpenAPI schema ────────────────


def test_chat_request_backend_field_has_description():
    """OpenAPI `/docs` should describe the backend field; Field
    description is the carrier that makes it into the schema."""
    src = (REPO_ROOT / "api" / "server.py").read_text(encoding="utf-8")
    cls = src[src.index("class ChatRequest"):src.index("class ChatRequest") + 3000]
    # The backend field's Field(...) must carry description=
    assert re.search(
        r"backend:\s*Literal\[[^\]]+\]\s*\|\s*None\s*=\s*Field\([\s\S]+?description=",
        cls,
    ), "ChatRequest.backend must use Field(description=...)"


def test_chat_response_backend_fallback_field_has_description():
    src = (REPO_ROOT / "api" / "server.py").read_text(encoding="utf-8")
    cls = src[src.index("class ChatResponse"):src.index("class ChatResponse") + 3000]
    assert re.search(
        r"backend_fallback:\s*bool\s*\|\s*None\s*=\s*Field\([\s\S]+?description=",
        cls,
    ), "ChatResponse.backend_fallback must use Field(description=...)"


# ── R4 F2: ChatRequest extra=forbid contract pin ──────────────────────


def test_chat_request_rejects_unknown_extra_field():
    """Reviewer 4 F2: pin Pydantic extra=forbid on ChatRequest so a
    future drop of `model_config={'extra':'forbid'}` is caught."""
    from pydantic import ValidationError

    # Use direct import — TestClient setup is heavy and not needed for
    # a pure Pydantic contract test.
    from api.server import ChatRequest
    with pytest.raises(ValidationError):
        ChatRequest(question="hello", invented_field="z")


# ── R4 F4: _BACKEND_NAME_ALIASES contract pin (unconditional) ─────────


def test_router_backend_name_aliases_is_pinned():
    """Pin the exact alias mapping so a future refactor that drops
    `"codex" → "openai"` raises an explicit test failure rather than
    silently breaking the chip's default backend."""
    from nano_notebooklm.ai.router import _BACKEND_NAME_ALIASES
    assert _BACKEND_NAME_ALIASES["codex"] == "openai"
    assert _BACKEND_NAME_ALIASES["qwen_raft"] == "qwen_raft"


# ── R1 F6: 422 envelope shape pinned ──────────────────────────────────


def test_unknown_backend_422_carries_standard_envelope(monkeypatch, tmp_path):
    """Reviewer 1 F6: the bogus-backend 422 must follow the canonical
    {error, request_id, detail} envelope — partial check in part 2
    test only asserted status_code."""
    from tests.test_qwen_backend import _build_chat_client
    client, _ = _build_chat_client(monkeypatch, tmp_path)
    r = client.post("/api/chat", json={
        "question": "hello",
        "backend": "bogus-backend",
    })
    assert r.status_code == 422
    body = r.json()
    # Standard envelope from app's RequestValidationError handler.
    assert "error" in body or "detail" in body
    assert "request_id" in body
