"""Round 3 #R3-2 — explicit user_lang preference (zh/en).

Coverage:
- 4 mini tests: chat + zh / chat + en / frontend study-state helpers /
  frontend app.jsx wiring.
- 5 corner tests: invalid format → 422 / omitted compatibility / localStorage
  round-trip / agent_loop binding / quiz_generator strict zh.

All tests run offline. No real LLM keys required: we monkeypatch
`router.complete` and capture the system prompts that flow through.
"""

from __future__ import annotations

import importlib
import json
import re
import subprocess
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nano_notebooklm.types import LLMResponse


REPO_ROOT = Path(__file__).resolve().parent.parent


# ── Shared chat fixture (mirrors test_router_intent.chat_client minus zh course) ──


@pytest.fixture
def chat_capture(monkeypatch, tmp_path, fake_embed_fn):
    """Spin up a minimal /api/chat stack with a captured `router.complete`."""
    art = tmp_path / "artifacts"
    courses_dir = art / "courses"
    courses_dir.mkdir(parents=True)

    from nano_notebooklm.types import Chunk, FileType

    chunks = [
        Chunk(chunk_id=f"e{i:03d}", doc_id=f"de{i:03d}", course_id="en_course",
              text=text, file_type=FileType.PDF,
              source_file="en_textbook.pdf", location=f"Page {i+1}/3", page=i+1)
        for i, text in enumerate([
            "memory hierarchy organizes storage from registers to disk.",
            "cache exploits temporal and spatial locality of access.",
            "virtual memory isolates processes via page tables.",
        ])
    ]
    cdir = courses_dir / "en_course"
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "chunks.json").write_text(
        json.dumps([c.model_dump() for c in chunks], default=str)
    )
    (cdir / "course_meta.json").write_text(json.dumps(
        {"course_id": "en_course", "name": "en_course",
         "documents": list({c.doc_id for c in chunks})}
    ))

    monkeypatch.setenv("ARTIFACTS_DIR", str(art))
    from nano_notebooklm import config
    monkeypatch.setattr(config, "ARTIFACTS_DIR", art)
    from nano_notebooklm.kb import store as kb_store
    monkeypatch.setattr(kb_store, "_get_default_embed_fn", lambda: fake_embed_fn)
    # Ensure the score gate doesn't accidentally drop our hash-embedded fakes.
    monkeypatch.setenv("RAG_SCORE_GATE_TOP1", "0.0")

    import api.server as server_mod
    importlib.reload(server_mod)
    server_mod.kb.build_index(None)

    from nano_notebooklm.orchestrator import router_intent as ri
    ri._LANG_CACHE.clear()

    captured = {"systems": []}

    async def stub(prompt, task_type="", system="", temperature=0.7,
                   max_tokens=4096, max_retries=3):
        captured["systems"].append(system or "")
        if task_type == "translate_query":
            return LLMResponse(content=prompt[-40:], model="fake",
                               input_tokens=1, output_tokens=1, latency_ms=1.0)
        return LLMResponse(content="captured-answer", model="fake",
                           input_tokens=1, output_tokens=1, latency_ms=1.0)

    monkeypatch.setattr(server_mod.router, "complete", stub)

    return TestClient(server_mod.app), server_mod, captured


# ── Mini 1: zh user_lang reaches qa system prompt ─────────────────────


def test_chat_with_user_lang_zh_injects_zh_only_addendum(chat_capture):
    client, _, captured = chat_capture
    r = client.post("/api/chat", json={
        "question": "memory hierarchy",
        "course_id": "en_course",
        "user_lang": "zh",
    })
    assert r.status_code == 200, r.text
    qa_systems = [s for s in captured["systems"]
                  if "Study Assistant" in s or "Reference documents" in s]
    assert qa_systems, "no qa system prompt captured"
    assert any("Reply ONLY in zh" in s for s in qa_systems), \
        f"expected strict zh binding in qa system; got:\n{qa_systems}"


# ── Mini 2: en user_lang reaches qa system prompt ─────────────────────


def test_chat_with_user_lang_en_injects_en_only_addendum(chat_capture):
    client, _, captured = chat_capture
    r = client.post("/api/chat", json={
        "question": "memory hierarchy",
        "course_id": "en_course",
        "user_lang": "en",
    })
    assert r.status_code == 200, r.text
    qa_systems = [s for s in captured["systems"]
                  if "Study Assistant" in s or "Reference documents" in s]
    assert qa_systems, "no qa system prompt captured"
    assert any("Reply ONLY in en" in s for s in qa_systems), \
        f"expected strict en binding in qa system; got:\n{qa_systems}"


# ── Mini 3: frontend study-state has helpers + key constant ───────────


def test_frontend_user_lang_helpers_exist():
    src = (REPO_ROOT / "frontend" / "study-state.js").read_text(encoding="utf-8")
    assert "loadUserLang" in src, "loadUserLang helper missing"
    assert "saveUserLang" in src, "saveUserLang helper missing"
    assert "USER_LANG_KEY" in src, "USER_LANG_KEY constant missing"
    assert "DEFAULT_LANG_CHOICES" in src, "DEFAULT_LANG_CHOICES missing"
    # Storage key must use the project prefix so it co-exists with quiz/notes
    # keys without colliding.
    assert "nano-nlm:v1:user-lang" in src, \
        "expected stable localStorage key 'nano-nlm:v1:user-lang'"


# ── Mini 4: app.jsx wires modal + topbar chip ─────────────────────────


def test_frontend_user_lang_modal_logic_in_app_jsx():
    src = (REPO_ROOT / "frontend" / "app.jsx").read_text(encoding="utf-8")
    # Modal is the entry point for first-time selection.
    assert "lang-modal" in src, "lang-modal class missing in app.jsx"
    # Topbar chip surfaces the current preference + lets the user re-pick.
    assert "lang-chip" in src, "lang-chip class missing in app.jsx"
    # Component must read/write through the StudyState helpers (single source
    # of truth) instead of poking localStorage directly.
    assert "StudyState.loadUserLang" in src, \
        "app.jsx must read user_lang via StudyState.loadUserLang"
    assert "StudyState.saveUserLang" in src, \
        "app.jsx must persist user_lang via StudyState.saveUserLang"


# ── Corner 1: invalid format returns 422 envelope ─────────────────────


@pytest.mark.parametrize("bad", ["fr", "Chinese", "ZH", "english", "zh-cn"])
def test_chat_user_lang_invalid_value_returns_422(chat_capture, bad):
    client, _, _ = chat_capture
    r = client.post("/api/chat", json={
        "question": "memory hierarchy",
        "course_id": "en_course",
        "user_lang": bad,
    })
    assert r.status_code == 422, f"{bad!r} should be rejected, got {r.status_code}"
    body = r.json()
    assert body["error"] == "validation_error"
    assert body.get("request_id"), "envelope must carry request_id"
    assert "detail" in body


# ── Corner 2: omitted user_lang → backwards compatibility ─────────────


def test_chat_user_lang_omitted_falls_back_to_match_query_lang(chat_capture):
    """Old clients (no user_lang) must keep working — the LANG_BINDING block
    must NOT be appended when the field is absent. Otherwise we regress
    every call site that hasn't been updated yet."""
    client, _, captured = chat_capture
    r = client.post("/api/chat", json={
        "question": "memory hierarchy",
        "course_id": "en_course",
    })
    assert r.status_code == 200, r.text
    assert all("Reply ONLY in" not in s for s in captured["systems"]), \
        "omitted user_lang must not inject lang binding"


# ── Corner 3: localStorage round-trip via study-state.js ──────────────


def test_user_lang_persists_in_localstorage():
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const storage = h.createMemoryStorage();
        if (h.loadUserLang(storage) !== null) throw new Error('expected null on empty');
        h.saveUserLang(storage, 'zh');
        if (h.loadUserLang(storage) !== 'zh') throw new Error('zh not persisted');
        h.saveUserLang(storage, 'en');
        if (h.loadUserLang(storage) !== 'en') throw new Error('en overwrite failed');
        // Tampered value (manual localStorage edit) → loader returns null so
        // the modal re-prompts instead of silently routing through an unknown
        // language code.
        storage.setItem(h.USER_LANG_KEY, 'fr');
        if (h.loadUserLang(storage) !== null) throw new Error('invalid stored value should be ignored');
        // Choices are exposed for the modal renderer.
        if (!Array.isArray(h.DEFAULT_LANG_CHOICES) || h.DEFAULT_LANG_CHOICES.length < 2) {
          throw new Error('expected at least zh + en choices');
        }
        const codes = h.DEFAULT_LANG_CHOICES.map(c => c.code).sort().join(',');
        if (codes !== 'en,zh') throw new Error('expected exactly zh + en codes, got ' + codes);
        console.log('ok');
        """
    )
    proc = subprocess.run(["node", "-e", script], cwd=str(REPO_ROOT),
                          text=True, capture_output=True, check=True)
    assert proc.stdout.strip() == "ok", proc.stderr


# ── Corner 4: agent_loop.compose_system_prompt propagates user_lang ───


def test_agent_stream_user_lang_propagates_to_system_prompt():
    from nano_notebooklm.orchestrator import agent_loop

    plain = agent_loop.compose_system_prompt("en_course", ["en_course"])
    zh = agent_loop.compose_system_prompt(
        "en_course", ["en_course"], user_lang="zh",
    )
    en = agent_loop.compose_system_prompt(
        "en_course", ["en_course"], user_lang="en",
    )

    assert "Reply ONLY in" not in plain, "user_lang=None must not inject binding"
    assert "Reply ONLY in zh" in zh, "user_lang=zh must inject zh binding"
    assert "Reply ONLY in en" in en, "user_lang=en must inject en binding"


# ── Corner 5: quiz path also receives the lang binding ────────────────


def test_quiz_with_user_lang_zh_question_text_constraint(chat_capture, monkeypatch):
    """Quiz keeps pseudo-stream JSON output, so its system prompt is the only
    place we can pin the language constraint. Verify the constraint flows
    through `/api/quiz` end-to-end."""
    client, server_mod, _ = chat_capture
    captured: list[str] = []

    async def stub_structured(prompt, task_type="", system="", temperature=0.7,
                              max_tokens=4096, max_retries=3):
        captured.append(system or "")
        return [{"question": "什么是局部性?", "type": "short_answer",
                 "answer": "时间与空间局部性。", "difficulty": "easy",
                 "concepts": ["locality"]}]

    monkeypatch.setattr(server_mod.router, "complete_structured", stub_structured)

    r = client.post("/api/quiz", json={
        "course_id": "en_course",
        "user_lang": "zh",
        "num_questions": 1,
    })
    assert r.status_code == 200, r.text
    assert captured, "quiz path should call complete_structured"
    assert any("Reply ONLY in zh" in s for s in captured), \
        f"quiz system must carry zh binding, got: {captured}"
