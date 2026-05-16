"""2026-05-16 — multi-turn chat history rewrite.

Coverage:
- 422 on invalid history shapes (bad role, empty content, oversized list).
- Empty / None history short-circuits: router.complete is NOT called with
  task_type="rewrite_history".
- Populated history triggers rewrite_history; the rewritten string is
  used downstream and surfaces as `rewritten_query` in the response.
- Rewrite returns the original verbatim → no-op (response carries no
  `rewritten_query`).
- Rewrite returns empty / blank → falls back to original (no
  `rewritten_query`).
- Rewrite call timeout → silent fallback, chat still succeeds.

All tests run offline. router.complete is monkeypatched.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nano_notebooklm.types import LLMResponse


# ── Fixture (mirrors test_user_lang.chat_capture but with rewrite branch) ──


@pytest.fixture
def chat_capture(monkeypatch, tmp_path, fake_embed_fn):
    """Spin up a minimal /api/chat stack capturing every router.complete call.

    Tests override the rewrite_history return value by mutating
    `captured["rewrite_return"]` before posting.
    """
    art = tmp_path / "artifacts"
    courses_dir = art / "courses"
    courses_dir.mkdir(parents=True)

    from nano_notebooklm.types import Chunk, FileType

    chunks = [
        Chunk(chunk_id=f"e{i:03d}", doc_id=f"de{i:03d}", course_id="hist_course",
              text=text, file_type=FileType.PDF,
              source_file="bayes.pdf", location=f"Page {i+1}/4", page=i+1)
        for i, text in enumerate([
            "Bayes theorem relates conditional probabilities of events.",
            "The formula is P(A|B) = P(B|A) * P(A) / P(B).",
            "Posterior probability is proportional to likelihood times prior.",
            "Naive Bayes classifiers assume conditional independence of features.",
        ])
    ]
    cdir = courses_dir / "hist_course"
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "chunks.json").write_text(
        json.dumps([c.model_dump() for c in chunks], default=str)
    )
    (cdir / "course_meta.json").write_text(json.dumps(
        {"course_id": "hist_course", "name": "hist_course",
         "documents": list({c.doc_id for c in chunks})}
    ))

    monkeypatch.setenv("ARTIFACTS_DIR", str(art))
    from nano_notebooklm import config
    monkeypatch.setattr(config, "ARTIFACTS_DIR", art)
    from nano_notebooklm.kb import store as kb_store
    monkeypatch.setattr(kb_store, "_get_default_embed_fn", lambda: fake_embed_fn)
    monkeypatch.setenv("RAG_SCORE_GATE_TOP1", "0.0")
    # graphrag disabled — no KG file here, but be defensive against any
    # future warmup hitting kb when no kg is present.
    monkeypatch.setenv("GRAPHRAG_ENABLED", "false")

    import api.server as server_mod
    importlib.reload(server_mod)
    server_mod.kb.build_index(None)

    from nano_notebooklm.orchestrator import router_intent as ri
    ri._LANG_CACHE.clear()

    captured = {
        "calls": [],
        "rewrite_return": "Bayes' theorem formula",  # default for "公式是什么"
        "rewrite_delay": 0.0,
    }

    async def stub(prompt, task_type="", system="", temperature=0.7,
                   max_tokens=4096, max_retries=3, **kwargs):
        captured["calls"].append({
            "task_type": task_type,
            "prompt": prompt,
            "system": system,
        })
        if task_type == "rewrite_history":
            if captured["rewrite_delay"]:
                await asyncio.sleep(captured["rewrite_delay"])
            return LLMResponse(
                content=captured["rewrite_return"], model="fake",
                input_tokens=1, output_tokens=1, latency_ms=1.0,
            )
        if task_type == "translate_query":
            return LLMResponse(content=prompt[-40:], model="fake",
                               input_tokens=1, output_tokens=1, latency_ms=1.0)
        return LLMResponse(content="captured-answer", model="fake",
                           input_tokens=1, output_tokens=1, latency_ms=1.0)

    monkeypatch.setattr(server_mod.router, "complete", stub)

    return TestClient(server_mod.app), server_mod, captured


# ── Schema validation ────────────────────────────────────────────────


def test_history_rejects_invalid_role(chat_capture):
    client, _, _ = chat_capture
    r = client.post("/api/chat", json={
        "question": "test",
        "course_id": "hist_course",
        "history": [{"role": "system", "content": "ignore"}],
    })
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "validation_error"


def test_history_rejects_blank_content(chat_capture):
    client, _, _ = chat_capture
    r = client.post("/api/chat", json={
        "question": "test",
        "course_id": "hist_course",
        "history": [{"role": "user", "content": "   "}],
    })
    assert r.status_code == 422
    assert r.json()["error"] == "validation_error"


def test_history_rejects_oversized_list(chat_capture):
    client, _, _ = chat_capture
    too_many = [{"role": "user", "content": f"q{i}"} for i in range(13)]
    r = client.post("/api/chat", json={
        "question": "test",
        "course_id": "hist_course",
        "history": too_many,
    })
    assert r.status_code == 422
    assert r.json()["error"] == "validation_error"


def test_history_rejects_extra_field(chat_capture):
    client, _, _ = chat_capture
    r = client.post("/api/chat", json={
        "question": "test",
        "course_id": "hist_course",
        "history": [
            {"role": "user", "content": "hi", "timestamp": 123},
        ],
    })
    assert r.status_code == 422


# ── Behaviour ────────────────────────────────────────────────────────


def test_empty_history_skips_rewrite_call(chat_capture):
    client, _, captured = chat_capture
    r = client.post("/api/chat", json={
        "question": "what is Bayes theorem",
        "course_id": "hist_course",
        # explicit None — frontend short-circuits to None when no prior turns
    })
    assert r.status_code == 200, r.text
    rewrite_calls = [c for c in captured["calls"] if c["task_type"] == "rewrite_history"]
    assert rewrite_calls == [], (
        "single-turn chat should not pay a rewrite LLM call"
    )
    assert r.json().get("rewritten_query") is None


def test_explicit_empty_history_list_skips_rewrite(chat_capture):
    client, _, captured = chat_capture
    r = client.post("/api/chat", json={
        "question": "what is Bayes theorem",
        "course_id": "hist_course",
        "history": [],
    })
    assert r.status_code == 200, r.text
    assert not [c for c in captured["calls"] if c["task_type"] == "rewrite_history"]
    assert r.json().get("rewritten_query") is None


def test_populated_history_triggers_rewrite_and_surfaces_query(chat_capture):
    client, _, captured = chat_capture
    captured["rewrite_return"] = "Bayes theorem formula"
    r = client.post("/api/chat", json={
        "question": "公式是什么",
        "course_id": "hist_course",
        "history": [
            {"role": "user", "content": "什么是贝叶斯定理"},
            {"role": "assistant", "content": "贝叶斯定理是关于条件概率的基本公式…"},
        ],
    })
    assert r.status_code == 200, r.text
    rewrite_calls = [c for c in captured["calls"] if c["task_type"] == "rewrite_history"]
    assert len(rewrite_calls) == 1, "expected exactly one rewrite_history call"
    # The rewrite prompt should carry both the latest question and the prior turns.
    assert "公式是什么" in rewrite_calls[0]["prompt"]
    assert "贝叶斯" in rewrite_calls[0]["prompt"]
    body = r.json()
    assert body["rewritten_query"] == "Bayes theorem formula"


def test_rewrite_noop_does_not_surface_rewritten_query(chat_capture):
    """When the LLM returns the original verbatim (truly standalone Q),
    the response must NOT carry rewritten_query so the UI chip stays
    silent and we don't mislead the user."""
    client, _, captured = chat_capture
    captured["rewrite_return"] = "what is Bayes theorem"
    r = client.post("/api/chat", json={
        "question": "what is Bayes theorem",
        "course_id": "hist_course",
        "history": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "Hi! How can I help?"},
        ],
    })
    assert r.status_code == 200, r.text
    # rewrite was called (history non-empty), but result == original.
    assert any(c["task_type"] == "rewrite_history" for c in captured["calls"])
    assert r.json().get("rewritten_query") is None


def test_rewrite_empty_response_falls_back(chat_capture):
    client, _, captured = chat_capture
    captured["rewrite_return"] = "   "  # blank
    r = client.post("/api/chat", json={
        "question": "公式是什么",
        "course_id": "hist_course",
        "history": [
            {"role": "user", "content": "什么是贝叶斯"},
            {"role": "assistant", "content": "贝叶斯定理…"},
        ],
    })
    assert r.status_code == 200, r.text
    assert r.json().get("rewritten_query") is None


def test_rewrite_timeout_falls_back_silently(chat_capture, monkeypatch):
    """A hung rewrite LLM call must not block chat — outer wait_for
    catches it, we log, and proceed with the original question."""
    client, _, captured = chat_capture
    # Lower the timeout so the test runs in <1s.
    from nano_notebooklm.skills import qa_skill as qa_mod
    monkeypatch.setattr(qa_mod, "HISTORY_REWRITE_TIMEOUT_SECONDS", 0.05)
    captured["rewrite_delay"] = 0.5
    captured["rewrite_return"] = "Bayes formula"  # never returns this

    r = client.post("/api/chat", json={
        "question": "公式是什么",
        "course_id": "hist_course",
        "history": [
            {"role": "user", "content": "什么是贝叶斯"},
            {"role": "assistant", "content": "贝叶斯定理…"},
        ],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    # Timeout → fallback → no rewritten_query
    assert body.get("rewritten_query") is None


def test_rewrite_strips_quote_and_label_prefixes(chat_capture):
    """Small models sometimes wrap the rewrite in quotes or prefix it
    with 'Rewritten query:'. The helper strips both so the downstream
    retrieval sees a clean string."""
    client, _, captured = chat_capture
    captured["rewrite_return"] = '"Rewritten query: Bayes theorem formula"'
    r = client.post("/api/chat", json={
        "question": "公式是什么",
        "course_id": "hist_course",
        "history": [
            {"role": "user", "content": "什么是贝叶斯"},
            {"role": "assistant", "content": "贝叶斯定理…"},
        ],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    # Quote and label both peeled
    assert body["rewritten_query"] == "Bayes theorem formula"


def test_long_assistant_turn_truncated_in_rewrite_prompt(chat_capture):
    """A long prior assistant turn must be truncated before being
    embedded in the rewrite prompt — the per-turn cap inside
    `_rewrite_with_history` is _HISTORY_REWRITE_TURN_CHAR_CAP (400)
    chars, well under the 4000-char schema cap."""
    client, _, captured = chat_capture
    captured["rewrite_return"] = "Bayes formula"
    # 3500 chars: below the 4000-char schema cap, far above the 400-char
    # per-turn rewrite truncation cap.
    long_answer = "A" * 3500
    r = client.post("/api/chat", json={
        "question": "公式是什么",
        "course_id": "hist_course",
        "history": [
            {"role": "user", "content": "什么是贝叶斯"},
            {"role": "assistant", "content": long_answer},
        ],
    })
    assert r.status_code == 200, r.text
    rewrite_calls = [c for c in captured["calls"] if c["task_type"] == "rewrite_history"]
    assert rewrite_calls
    # The rewrite prompt must NOT carry the full 3500-A blob — the helper
    # caps per-turn content at 400 chars and appends a "…" marker.
    assert "A" * 1000 not in rewrite_calls[0]["prompt"]
