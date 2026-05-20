"""Round 2 #1 + #2 — intent router + score gate + 0-hit translation retry.

Two layers:
  - Pure unit tests on `nano_notebooklm.orchestrator.router_intent` helpers
    (classify_input / passes_score_gate / detect_lang).
  - Integration tests on `/api/chat` exercising the full path branching
    (rag / general / translated) with monkeypatched `router.complete` and
    deterministic fake embeddings.

All tests offline. No LLM keys required.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nano_notebooklm.types import LLMResponse, SearchResult


# ── Pure unit tests ───────────────────────────────────────────────────


def test_classify_input_rag_happy():
    from nano_notebooklm.orchestrator import router_intent as ri

    d = ri.classify_input("什么是反向传播？")
    assert d.path == "rag"
    assert d.cleaned_query == "什么是反向传播？"


def test_classify_input_short_input_general():
    from nano_notebooklm.orchestrator import router_intent as ri

    d = ri.classify_input("ok")
    assert d.path == "general"
    assert "weight" in d.reason.lower() or "below" in d.reason.lower()


def test_classify_input_greeting_general():
    from nano_notebooklm.orchestrator import router_intent as ri

    d = ri.classify_input("你好")
    assert d.path == "general"
    assert "greet" in d.reason.lower() or "hello" in d.reason.lower() or "你好" in d.reason


def test_classify_input_strip_then_empty():
    from nano_notebooklm.orchestrator import router_intent as ri

    d = ri.classify_input("    \n　  ")
    assert d.path == "general"
    assert d.cleaned_query == ""


def test_classify_input_pure_punctuation():
    from nano_notebooklm.orchestrator import router_intent as ri

    for q in ("?", "？", "??", "...", "!!!", "。"):
        d = ri.classify_input(q)
        assert d.path == "general", f"{q!r} should not go to RAG"


def test_classify_input_emoji_only():
    from nano_notebooklm.orchestrator import router_intent as ri

    d = ri.classify_input("💀💀💀")
    assert d.path == "general"


def test_classify_input_real_question_kept():
    from nano_notebooklm.orchestrator import router_intent as ri

    for q in (
        "memory hierarchy",
        "如何理解局部性原理",
        "What is convolution and why is it used?",
    ):
        assert ri.classify_input(q).path == "rag", f"{q!r} should stay RAG"


# ── Score gate ──────────────────────────────────────────────────────


def _mk_result(score: float, idx: int = 0) -> SearchResult:
    return SearchResult(
        chunk_id=f"c{idx}",
        text=f"text {idx}",
        source_file="f.pdf",
        location="p.1",
        score=score,
        course_id="x",
    )


def test_passes_score_gate_top1_high_two_hits():
    from nano_notebooklm.orchestrator.router_intent import passes_score_gate

    results = [_mk_result(0.05, 0), _mk_result(0.04, 1)]
    assert passes_score_gate(results, top1_threshold=0.02, min_hits=2) is True


def test_passes_score_gate_low_top1():
    from nano_notebooklm.orchestrator.router_intent import passes_score_gate

    results = [_mk_result(0.005, 0), _mk_result(0.004, 1)]
    assert passes_score_gate(results, top1_threshold=0.02, min_hits=2) is False


def test_passes_score_gate_single_hit_borderline():
    """Single hit with moderate score should NOT pass — branch B requires 2×τ.
    This guards against tiny corpora where a noisy match becomes the only hit."""
    from nano_notebooklm.orchestrator.router_intent import passes_score_gate

    results = [_mk_result(0.03, 0)]
    # threshold=0.02, min_hits=2 → branch A fails (1<2); branch B requires
    # top1 ≥ 2×0.02 = 0.04, 0.03 < 0.04 → still false.
    assert passes_score_gate(results, top1_threshold=0.02, min_hits=2) is False


def test_passes_score_gate_single_hit_strong():
    """Single hit with very strong score SHOULD pass — accommodates small
    courses where only one rich match ever exists."""
    from nano_notebooklm.orchestrator.router_intent import passes_score_gate

    results = [_mk_result(0.10, 0)]
    # threshold=0.02 → branch B requires top1 ≥ 0.04, 0.10 ≥ 0.04 → pass.
    assert passes_score_gate(results, top1_threshold=0.02, min_hits=2) is True


def test_passes_score_gate_no_results():
    from nano_notebooklm.orchestrator.router_intent import passes_score_gate

    assert passes_score_gate([], top1_threshold=0.02, min_hits=2) is False


# ── Language detection / fingerprint ──────────────────────────────────


def test_detect_lang_zh():
    from nano_notebooklm.orchestrator.router_intent import detect_lang

    assert detect_lang("什么是反向传播") == "zh"
    assert detect_lang("内存是什么") == "zh"


def test_detect_lang_en():
    from nano_notebooklm.orchestrator.router_intent import detect_lang

    assert detect_lang("what is backpropagation") == "en"
    assert detect_lang("memory hierarchy and cache") == "en"


def test_detect_lang_mixed():
    from nano_notebooklm.orchestrator.router_intent import detect_lang

    # Substantial overlap of both scripts
    assert detect_lang("什么是 backpropagation") == "mixed"
    assert detect_lang("解释一下 convolution layer 的原理") == "mixed"


def test_compute_lang_fingerprint_zh_dominant():
    from nano_notebooklm.orchestrator.router_intent import compute_lang_fingerprint

    fp = compute_lang_fingerprint([
        "运动学正解描述了机器人末端位姿与关节角的关系",
        "这是一个关于机器人导论的章节内容",
    ])
    assert fp["lang"] == "zh"
    assert fp["zh_ratio"] > 0.5


def test_compute_lang_fingerprint_en_dominant():
    from nano_notebooklm.orchestrator.router_intent import compute_lang_fingerprint

    fp = compute_lang_fingerprint([
        "memory hierarchy with caches and registers",
        "the stack grows downward in modern x86 systems",
    ])
    assert fp["lang"] == "en"
    assert fp["zh_ratio"] < 0.1


# ── Integration: /api/chat ────────────────────────────────────────────


@pytest.fixture
def chat_client(monkeypatch, tmp_path, fake_embed_fn):
    """Two seeded courses:
      - en_course: English chunks ("memory hierarchy ...")
      - zh_course: Chinese chunks ("内存层次 ...")
    Lets us exercise rag / general / translated paths.
    """
    art = tmp_path / "artifacts"
    courses_dir = art / "courses"
    courses_dir.mkdir(parents=True)

    from nano_notebooklm.types import Chunk, FileType

    seeded: dict[str, list[Chunk]] = {
        "en_course": [
            Chunk(chunk_id=f"e{i:03d}", doc_id=f"de{i:03d}", course_id="en_course",
                  text=text, file_type=FileType.PDF,
                  source_file="en_textbook.pdf", location=f"Page {i+1}/5", page=i+1)
            for i, text in enumerate([
                "memory hierarchy organizes storage from registers to disk for performance.",
                "the cache exploits temporal and spatial locality of memory access.",
                "stack memory grows downward and is used for function call frames.",
                "virtual memory provides isolation between processes via page tables.",
                "RAM and registers are the fastest layers of the memory hierarchy.",
            ])
        ],
        "zh_course": [
            Chunk(chunk_id=f"z{i:03d}", doc_id=f"dz{i:03d}", course_id="zh_course",
                  text=text, file_type=FileType.PDF,
                  source_file="zh_textbook.pdf", location=f"Page {i+1}/5", page=i+1)
            for i, text in enumerate([
                "运动学正解描述了机器人末端位姿与关节角度之间的关系。",
                "传感器是机器人感知外部环境的输入装置。",
                "路径规划算法在已知环境中寻找最优可行路径。",
                "动力学分析关节力矩与运动状态的关系。",
                "机器人导论课程涵盖以上若干主题。",
            ])
        ],
    }

    for cid, chunks in seeded.items():
        cdir = courses_dir / cid
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "chunks.json").write_text(
            json.dumps([c.model_dump() for c in chunks], default=str)
        )
        (cdir / "course_meta.json").write_text(json.dumps(
            {"course_id": cid, "name": cid, "documents": list({c.doc_id for c in chunks})}
        ))

    monkeypatch.setenv("ARTIFACTS_DIR", str(art))
    from nano_notebooklm import config
    monkeypatch.setattr(config, "ARTIFACTS_DIR", art)
    from nano_notebooklm.kb import store as kb_store
    monkeypatch.setattr(kb_store, "_get_default_embed_fn", lambda: fake_embed_fn)

    # Lower the score gate so fake-embedding RRF scores can pass during tests.
    monkeypatch.setenv("RAG_SCORE_GATE_TOP1", "0.0")  # gated by min_hits only here

    import api.server as server_mod
    importlib.reload(server_mod)

    server_mod.kb.build_index(None)

    # Clear lang fingerprint cache between tests
    from nano_notebooklm.orchestrator import router_intent as ri
    ri._LANG_CACHE.clear()

    return TestClient(server_mod.app), server_mod


def _stub_complete_factory(answers: dict[str, str], default: str = "stubbed answer"):
    """Build an async stub for ModelRouter.complete that picks an answer based
    on a substring of the prompt."""
    async def _stub(prompt, task_type="", system="", temperature=0.7,
                    max_tokens=4096, max_retries=3, **kwargs):
        for needle, ans in answers.items():
            if needle in prompt or needle in (system or ""):
                return LLMResponse(content=ans, model="fake", input_tokens=1,
                                   output_tokens=1, latency_ms=1.0)
        return LLMResponse(content=default, model="fake", input_tokens=1,
                           output_tokens=1, latency_ms=1.0)
    return _stub


def test_chat_short_input_takes_general_path(chat_client, monkeypatch):
    client, server_mod = chat_client
    monkeypatch.setattr(server_mod.router, "complete",
                        _stub_complete_factory({}, default="hi there"))

    r = client.post("/api/chat", json={"question": "ok"})
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "general"
    assert body["sources"] == []
    assert "No relevant content" not in body["answer"]


def test_chat_greeting_takes_general_path(chat_client, monkeypatch):
    client, server_mod = chat_client
    monkeypatch.setattr(server_mod.router, "complete",
                        _stub_complete_factory({}, default="你好,有什么问题吗"))

    r = client.post("/api/chat", json={"question": "你好"})
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "general"
    assert body["sources"] == []


def test_chat_rag_hit_path(chat_client, monkeypatch):
    client, server_mod = chat_client
    monkeypatch.setattr(server_mod.router, "complete",
                        _stub_complete_factory({}, default="rag answer with citations"))

    r = client.post("/api/chat",
                    json={"question": "memory hierarchy", "course_id": "en_course"})
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "rag"
    assert len(body["sources"]) >= 1


def test_chat_score_gate_downgrade_to_general(chat_client, monkeypatch):
    """Corner: when score gate fails (too few hits / low score), path falls back
    to general — not to the boilerplate "No relevant content" string."""
    client, server_mod = chat_client

    # Force kb.search to return a single low-score result (gate fails on min_hits)
    def fake_search(query, top_k=5, course_id=None):
        return [_mk_result(0.001, 0)]

    monkeypatch.setattr(server_mod.kb, "search", fake_search)
    monkeypatch.setattr(server_mod.router, "complete",
                        _stub_complete_factory({}, default="general fallback answer"))

    # Use a high-gate so that even a real top1 wouldn't pass
    monkeypatch.setenv("RAG_SCORE_GATE_TOP1", "1.0")
    # Re-import config-bound module if any caches the threshold
    from nano_notebooklm.orchestrator import router_intent as ri
    importlib.reload(ri)

    r = client.post("/api/chat",
                    json={"question": "an obscure question", "course_id": "en_course"})
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "general"
    assert "No relevant content" not in body["answer"]


def _has_zh(s: str) -> bool:
    return any("一" <= c <= "鿿" for c in s)


def test_chat_translation_retry_happy(chat_client, monkeypatch):
    """Mini for #2: 中文 query 在英文课 0 hits → 自动翻译重试 → path=translated.
    fix-all v1 #4 ride-along: also pins that Persona reaches the QA system on
    translated path (Persona is in qa_system(), but no test pinned it for non-
    identity branches — review-swarm regression #4)."""
    client, server_mod = chat_client

    # Gate by query content (more robust than counting calls): any query that
    # contains 中文 returns 0 hits (the original query); the English
    # translation goes through the real fake-embedding search.
    real_search = server_mod.kb.search

    def gated_search(query, top_k=5, course_id=None):
        if _has_zh(query):
            return []
        return real_search(query, top_k=top_k, course_id=course_id)

    monkeypatch.setattr(server_mod.kb, "search", gated_search)

    captured_systems: list[str] = []

    # Stub: TRANSLATE prompt → return English. QA prompt → return answer.
    async def stub(prompt, task_type="", system="", temperature=0.7,
                   max_tokens=4096, max_retries=3, **kwargs):
        captured_systems.append(system or "")
        if task_type == "translate_query" or "translate" in (system or "").lower():
            return LLMResponse(content="memory hierarchy", model="fake",
                               input_tokens=1, output_tokens=1, latency_ms=1.0)
        return LLMResponse(content="answer about memory", model="fake",
                           input_tokens=1, output_tokens=1, latency_ms=1.0)

    monkeypatch.setattr(server_mod.router, "complete", stub)

    r = client.post("/api/chat",
                    json={"question": "什么是内存层次", "course_id": "en_course"})
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "translated", body
    assert body.get("original_query") == "什么是内存层次"
    assert body.get("translated_query") == "memory hierarchy"
    # Answer must include the translation note so the user sees what happened
    assert "translated" in body["answer"].lower() or "翻译" in body["answer"]
    # Persona pin: the QA-path system message (not the translation system) must
    # carry "Study Assistant" (the DEFAULT_PERSONA backstop from 2026-05-12
    # when persona became user-customisable). Both calls capture; assert at
    # least the QA call.
    qa_systems = [s for s in captured_systems if "Reference documents" in s
                  or "Study Assistant" in s]
    assert any("Study Assistant" in s for s in qa_systems), \
        "translated path must inherit DEFAULT_PERSONA via qa_system()"


def test_chat_translation_failure_falls_through(chat_client, monkeypatch):
    """Corner #2a: translate LLM call raises → graceful fall-through, no crash."""
    client, server_mod = chat_client

    monkeypatch.setattr(server_mod.kb, "search",
                        lambda q, top_k=5, course_id=None: [])  # always 0

    async def stub(prompt, task_type="", system="", temperature=0.7,
                   max_tokens=4096, max_retries=3, **kwargs):
        if task_type == "translate_query" or "translate" in (system or "").lower():
            raise RuntimeError("LLM translation failed")
        return LLMResponse(content="generic fallback", model="fake",
                           input_tokens=1, output_tokens=1, latency_ms=1.0)

    monkeypatch.setattr(server_mod.router, "complete", stub)

    r = client.post("/api/chat",
                    json={"question": "什么是内存层次", "course_id": "en_course"})
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "general"
    assert "No relevant content" not in body["answer"]


def test_chat_translation_still_zero_falls_through(chat_client, monkeypatch):
    """Corner #2b: translation succeeds but the SECOND search (with the
    English translation) also returns 0 hits → general path.
    Gate by query content (not call counter) so the test exercises the
    realistic branching: 中文 query → 0; English translation → 0 too."""
    client, server_mod = chat_client

    # Both the original 中文 query AND the translated English keyword come
    # back empty — simulates a course whose corpus genuinely has no coverage
    # of the topic in either language.
    monkeypatch.setattr(server_mod.kb, "search",
                        lambda q, top_k=5, course_id=None: [])

    async def stub(prompt, task_type="", system="", temperature=0.7,
                   max_tokens=4096, max_retries=3, **kwargs):
        if task_type == "translate_query":
            return LLMResponse(content="totally-unfindable-keyword", model="fake",
                               input_tokens=1, output_tokens=1, latency_ms=1.0)
        return LLMResponse(content="generic fallback", model="fake",
                           input_tokens=1, output_tokens=1, latency_ms=1.0)

    monkeypatch.setattr(server_mod.router, "complete", stub)

    r = client.post("/api/chat",
                    json={"question": "什么是内存层次", "course_id": "en_course"})
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "general"


def test_chat_mixed_query_does_not_translate(chat_client, monkeypatch):
    """Corner #2c: mixed-language query should not trigger translation retry."""
    client, server_mod = chat_client

    monkeypatch.setattr(server_mod.kb, "search",
                        lambda q, top_k=5, course_id=None: [])  # always 0

    translate_called = {"n": 0}

    async def stub(prompt, task_type="", system="", temperature=0.7,
                   max_tokens=4096, max_retries=3, **kwargs):
        if task_type == "translate_query" or "translate" in (system or "").lower():
            translate_called["n"] += 1
            return LLMResponse(content="x", model="fake",
                               input_tokens=1, output_tokens=1, latency_ms=1.0)
        return LLMResponse(content="general response", model="fake",
                           input_tokens=1, output_tokens=1, latency_ms=1.0)

    monkeypatch.setattr(server_mod.router, "complete", stub)

    r = client.post("/api/chat", json={
        "question": "解释一下 backpropagation 的原理",
        "course_id": "en_course",
    })
    assert r.status_code == 200
    body = r.json()
    assert translate_called["n"] == 0, "mixed-lang query must not be translated"
    assert body["path"] == "general"


# ── H3 corner tests: All-Courses no-translate / mixed-course no-translate /
#                    path enum membership / boilerplate has no `path` ──


def test_chat_all_courses_does_not_translate(chat_client, monkeypatch):
    """Corner: All Courses mode (course_id=None) must skip translation —
    there's no single course-language to mismatch against."""
    client, server_mod = chat_client

    monkeypatch.setattr(server_mod.kb, "search",
                        lambda q, top_k=5, course_id=None: [])

    translate_called = {"n": 0}

    async def stub(prompt, task_type="", system="", temperature=0.7,
                   max_tokens=4096, max_retries=3, **kwargs):
        if task_type == "translate_query":
            translate_called["n"] += 1
        return LLMResponse(content="general response", model="fake",
                           input_tokens=1, output_tokens=1, latency_ms=1.0)

    monkeypatch.setattr(server_mod.router, "complete", stub)

    # Note: course_id intentionally omitted from the request body
    r = client.post("/api/chat", json={"question": "什么是内存层次"})
    assert r.status_code == 200
    body = r.json()
    assert translate_called["n"] == 0, "All Courses mode must not translate"
    assert body["path"] == "general"


def test_chat_mixed_course_lang_does_not_translate(chat_client, monkeypatch):
    """Corner: when the course's own language fingerprint is `mixed` (e.g. a
    bilingual textbook), translation retry must not fire even on a mono-
    language query."""
    client, server_mod = chat_client

    # Force the course-lang cache to "mixed" without depending on chunk
    # contents. This isolates the branching logic under test.
    from nano_notebooklm.orchestrator import router_intent as ri
    ri._LANG_CACHE["en_course"] = {"lang": "mixed", "zh_ratio": 0.3, "en_ratio": 0.5}

    monkeypatch.setattr(server_mod.kb, "search",
                        lambda q, top_k=5, course_id=None: [])

    translate_called = {"n": 0}

    async def stub(prompt, task_type="", system="", temperature=0.7,
                   max_tokens=4096, max_retries=3, **kwargs):
        if task_type == "translate_query":
            translate_called["n"] += 1
        return LLMResponse(content="general response", model="fake",
                           input_tokens=1, output_tokens=1, latency_ms=1.0)

    monkeypatch.setattr(server_mod.router, "complete", stub)

    r = client.post("/api/chat", json={
        "question": "什么是内存层次",
        "course_id": "en_course",
    })
    assert r.status_code == 200
    assert translate_called["n"] == 0, "mixed-course-lang must not translate"
    assert r.json()["path"] == "general"


def test_chat_path_value_is_in_union_for_all_branches(chat_client, monkeypatch):
    """Contract guard: every chat response with a `path` field must use one of
    the four values exactly. Catches future typos like `cross_course`."""
    client, server_mod = chat_client

    async def stub(prompt, task_type="", system="", temperature=0.7,
                   max_tokens=4096, max_retries=3, **kwargs):
        return LLMResponse(content="answer", model="fake",
                           input_tokens=1, output_tokens=1, latency_ms=1.0)

    monkeypatch.setattr(server_mod.router, "complete", stub)

    allowed = {"rag", "general", "translated", "cross-course"}
    for body in (
        {"question": "ok"},                                  # general (short)
        {"question": "你好"},                                # general (greeting)
        {"question": "memory hierarchy", "course_id": "en_course"},  # rag
    ):
        r = client.post("/api/chat", json=body)
        assert r.status_code == 200
        path = r.json().get("path")
        # path may legitimately be absent (boilerplate); when present must be union
        if path is not None:
            assert path in allowed, f"path={path!r} not in {allowed}"


def test_chat_filter_empty_boilerplate_omits_path(chat_client, monkeypatch):
    """Corner: when checked_files knocks all results to 0, the response carries
    no `path` field (the four-value union is reserved for routing decisions
    that actually went through router_intent). Frontend should fallback-render
    when `path` is absent.

    fix-all v1 #3 (review-swarm contracts #4): use a REAL gate threshold +
    seeded strong-score raw so this test pins the new `raw_passes` discriminator
    in the filter_empty short-circuit. Without these knobs the chat_client
    fixture's `RAG_SCORE_GATE_TOP1=0.0` lets almost any raw pass the gate, so
    deleting the `and raw_passes` check would silently keep the test green."""
    client, server_mod = chat_client

    async def stub(prompt, task_type="", system="", temperature=0.7,
                   max_tokens=4096, max_retries=3, **kwargs):
        return LLMResponse(content="should not be called", model="fake",
                           input_tokens=1, output_tokens=1, latency_ms=1.0)

    # Seed raw with strong-score hits in unchecked files so:
    #   raw passes gate (top1=0.20 ≥ 0.05 ∧ 3 hits ≥ 2)
    #   filtered = [] (no chunk's source_file matches checked_files)
    # This is the canonical "filter is the cause" scenario.
    a = SearchResult(chunk_id="a", text="a", source_file="real-file-1.pdf",
                     location="p1", score=0.20, course_id="en_course")
    b = SearchResult(chunk_id="b", text="b", source_file="real-file-2.pdf",
                     location="p2", score=0.18, course_id="en_course")
    c = SearchResult(chunk_id="c", text="c", source_file="real-file-1.pdf",
                     location="p3", score=0.15, course_id="en_course")
    monkeypatch.setattr(server_mod.kb, "search",
                        lambda q, top_k=5, course_id=None: [a, b, c])
    monkeypatch.setenv("RAG_SCORE_GATE_TOP1", "0.05")
    monkeypatch.setattr(server_mod.router, "complete", stub)

    r = client.post("/api/chat", json={
        "question": "memory hierarchy",
        "course_id": "en_course",
        "checked_files": ["nope-not-a-real-file.pdf"],
    })
    assert r.status_code == 200
    body = r.json()
    # response_model_exclude_none drops absent fields → "path" key not in body
    assert "path" not in body, body
    assert body.get("filter_empty") is True
    assert "No relevant content" in body["answer"]


# ── Second-round fix-all tests (F8/F9/F10/F4) ─────────────────────────


def test_chat_response_model_rejects_typo_path():
    """F8: ChatResponse Literal must reject typos like `cross_course` (under-
    score vs hyphen) at the API boundary so a future qa_skill bug can't
    silently ship the wrong path value to the frontend."""
    from pydantic import ValidationError
    from api.server import ChatResponse

    # Each of the five canonical values must be accepted (R4-4 added "graphrag")
    for ok in ("rag", "general", "translated", "cross-course", "graphrag"):
        ChatResponse(answer="x", path=ok)

    # Common typo: hyphen → underscore
    try:
        ChatResponse(answer="x", path="cross_course")
    except ValidationError:
        pass
    else:
        raise AssertionError("Literal must reject 'cross_course' typo")

    # Unknown invented value
    try:
        ChatResponse(answer="x", path="rag_advanced")
    except ValidationError:
        pass
    else:
        raise AssertionError("Literal must reject unknown path values")


def test_chat_response_model_forbids_extra_fields():
    """F8/F6: model_config extra='forbid' should raise on unknown sidecar
    fields so silently-dropped data is impossible."""
    from pydantic import ValidationError
    from api.server import ChatResponse

    try:
        ChatResponse(answer="x", path="rag", invented_field_xyz="future_field")
    except ValidationError:
        pass
    else:
        raise AssertionError("extra='forbid' must reject unknown fields")


def test_peek_chunks_returns_n_without_loading_all(tmp_path, monkeypatch, fake_embed_fn):
    """F9: KBStore.peek_chunks reads chunks.json directly and returns at most
    `n` Chunk objects. Verify it returns exactly the requested slice and
    doesn't fall through to the full-load path on the happy case."""
    import json
    from nano_notebooklm.kb.store import KBStore
    from nano_notebooklm.types import FileType, Chunk

    art = tmp_path / "artifacts"
    cdir = art / "courses" / "x"
    cdir.mkdir(parents=True)

    seed = [
        Chunk(chunk_id=f"c{i:03d}", doc_id=f"d{i:03d}", course_id="x",
              text=f"text {i}", file_type=FileType.PDF,
              source_file="x.pdf", location=f"p{i}", page=i)
        for i in range(50)
    ]
    (cdir / "chunks.json").write_text(
        json.dumps([c.model_dump() for c in seed], default=str)
    )

    monkeypatch.setenv("ARTIFACTS_DIR", str(art))
    from nano_notebooklm import config as cfg
    monkeypatch.setattr(cfg, "ARTIFACTS_DIR", art)
    kb = KBStore(embed_fn=fake_embed_fn)

    # Sentinel: explode if get_chunks gets called (proves we didn't fall back)
    def boom(*a, **kw):
        raise AssertionError("peek_chunks fell through to get_chunks unexpectedly")
    monkeypatch.setattr(kb, "get_chunks", boom)

    out = kb.peek_chunks("x", n=5)
    assert len(out) == 5
    assert all(c.chunk_id.startswith("c") for c in out)
    assert out[0].chunk_id == "c000"


def test_peek_chunks_missing_course_returns_empty(tmp_path, monkeypatch, fake_embed_fn):
    """F9 corner: peek_chunks on an unknown course returns [] without raising."""
    from nano_notebooklm.kb.store import KBStore

    art = tmp_path / "artifacts"
    art.mkdir()
    monkeypatch.setenv("ARTIFACTS_DIR", str(art))
    from nano_notebooklm import config as cfg
    monkeypatch.setattr(cfg, "ARTIFACTS_DIR", art)
    kb = KBStore(embed_fn=fake_embed_fn)

    assert kb.peek_chunks("nope-no-such-course", n=10) == []


def test_peek_chunks_corrupt_json_returns_empty(tmp_path, monkeypatch, fake_embed_fn):
    """F7/F9: corrupt chunks.json must NOT silently fall back to a full
    get_chunks load (which would defeat the optimisation precisely when
    most needed). Returns [] + logs a warning."""
    from nano_notebooklm.kb.store import KBStore

    art = tmp_path / "artifacts"
    cdir = art / "courses" / "x"
    cdir.mkdir(parents=True)
    (cdir / "chunks.json").write_text("{this is not valid json}")

    monkeypatch.setenv("ARTIFACTS_DIR", str(art))
    from nano_notebooklm import config as cfg
    monkeypatch.setattr(cfg, "ARTIFACTS_DIR", art)
    kb = KBStore(embed_fn=fake_embed_fn)

    # Sentinel: full-load path must not be taken on parse failure
    def boom(*a, **kw):
        raise AssertionError("peek_chunks fell back to full load on corrupt JSON")
    monkeypatch.setattr(kb, "get_chunks", boom)

    assert kb.peek_chunks("x", n=10) == []


def test_clear_lang_cache_drops_cached_entry():
    """F9: router_intent.clear_lang_cache(course_id) must remove the cached
    fingerprint so the next chat call recomputes against new content."""
    from nano_notebooklm.orchestrator import router_intent as ri

    ri._LANG_CACHE.clear()
    ri._LANG_CACHE["unitcourse"] = {"lang": "en", "zh_ratio": 0.0, "en_ratio": 1.0}
    assert "unitcourse" in ri._LANG_CACHE

    ri.clear_lang_cache("unitcourse")
    assert "unitcourse" not in ri._LANG_CACHE

    # And the global form clears everything
    ri._LANG_CACHE["a"] = {"lang": "zh", "zh_ratio": 1.0, "en_ratio": 0.0}
    ri._LANG_CACHE["b"] = {"lang": "en", "zh_ratio": 0.0, "en_ratio": 1.0}
    ri.clear_lang_cache(None)
    assert ri._LANG_CACHE == {}


def test_chat_translation_timeout_falls_through(chat_client, monkeypatch):
    """F10: when translation LLM call exceeds TRANSLATION_TIMEOUT_SECONDS,
    asyncio.TimeoutError fires inside _maybe_translate_retry → graceful
    fallback to general path, no crash."""
    import asyncio as _asyncio
    client, server_mod = chat_client

    monkeypatch.setattr(server_mod.kb, "search",
                        lambda q, top_k=5, course_id=None: [])

    async def stub(prompt, task_type="", system="", temperature=0.7,
                   max_tokens=4096, max_retries=3, **kwargs):
        if task_type == "translate_query":
            # Raise TimeoutError directly — equivalent to wait_for elapsing.
            raise _asyncio.TimeoutError()
        return LLMResponse(content="general response", model="fake",
                           input_tokens=1, output_tokens=1, latency_ms=1.0)

    monkeypatch.setattr(server_mod.router, "complete", stub)

    r = client.post("/api/chat",
                    json={"question": "什么是内存层次", "course_id": "en_course"})
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "general"
    assert "No relevant content" not in body["answer"]


def test_chat_cross_course_fallback_happy(chat_client, monkeypatch):
    """Round 2 #3 mini: current course 0-hit + translation also 0-hit → search
    All Courses (course_id=None) → if hit, return path='cross-course' with
    `cross_course_origin` showing which course found it.
    fix-all v1 #4 ride-along: also pins Persona reaches qa_system() on the
    cross-course path (review-swarm regression #4)."""
    client, server_mod = chat_client

    # Force per-course searches to 0; All-Courses search returns a real hit.
    real_search = server_mod.kb.search

    def gated_search(query, top_k=5, course_id=None):
        if course_id is None:
            return real_search(query, top_k=top_k, course_id=None)
        return []  # current course has nothing

    monkeypatch.setattr(server_mod.kb, "search", gated_search)

    captured_systems: list[str] = []

    async def stub(prompt, task_type="", system="", temperature=0.7,
                   max_tokens=4096, max_retries=3, **kwargs):
        captured_systems.append(system or "")
        if task_type == "translate_query":
            return LLMResponse(content="memory hierarchy", model="fake",
                               input_tokens=1, output_tokens=1, latency_ms=1.0)
        return LLMResponse(content="answer from a sibling course", model="fake",
                           input_tokens=1, output_tokens=1, latency_ms=1.0)
    monkeypatch.setattr(server_mod.router, "complete", stub)

    r = client.post("/api/chat", json={
        "question": "memory hierarchy",
        "course_id": "zh_course",  # 中文课，但用 en query
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["path"] == "cross-course", body
    assert body.get("cross_course_origin"), "must record which course matched"
    # answer should carry a "from another course" annotation
    assert "本课" in body["answer"] or "另一" in body["answer"] or "another" in body["answer"].lower()
    # Persona pin: cross-course path reuses qa_system(), must carry the
    # DEFAULT_PERSONA fallback when ChatRequest.persona is unset.
    qa_systems = [s for s in captured_systems if "Reference documents" in s
                  or "Study Assistant" in s]
    assert any("Study Assistant" in s for s in qa_systems), \
        "cross-course path must inherit DEFAULT_PERSONA via qa_system()"


def test_chat_cross_course_fallback_also_empty(chat_client, monkeypatch):
    """Round 2 #3 corner: cross-course retry also returns 0 → final general
    fallback (graceful, no crash)."""
    client, server_mod = chat_client

    monkeypatch.setattr(server_mod.kb, "search",
                        lambda q, top_k=5, course_id=None: [])

    async def stub(prompt, task_type="", system="", temperature=0.7,
                   max_tokens=4096, max_retries=3, **kwargs):
        if task_type == "translate_query":
            return LLMResponse(content="totally-unfindable", model="fake",
                               input_tokens=1, output_tokens=1, latency_ms=1.0)
        return LLMResponse(content="generic", model="fake",
                           input_tokens=1, output_tokens=1, latency_ms=1.0)
    monkeypatch.setattr(server_mod.router, "complete", stub)

    r = client.post("/api/chat", json={
        "question": "memory hierarchy",
        "course_id": "zh_course",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "general"


def test_chat_cross_course_skipped_when_no_course_filter(chat_client, monkeypatch):
    """Corner: when caller is already in All Courses mode (course_id=None),
    cross-course retry is meaningless and should not fire — fall straight to
    general path on 0-hit."""
    client, server_mod = chat_client

    monkeypatch.setattr(server_mod.kb, "search",
                        lambda q, top_k=5, course_id=None: [])

    cross_search_attempts = {"n": 0}
    real_search = server_mod.kb.search

    async def stub(prompt, task_type="", system="", temperature=0.7,
                   max_tokens=4096, max_retries=3, **kwargs):
        return LLMResponse(content="general", model="fake",
                           input_tokens=1, output_tokens=1, latency_ms=1.0)
    monkeypatch.setattr(server_mod.router, "complete", stub)

    r = client.post("/api/chat", json={"question": "memory hierarchy"})
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "general"
    assert "cross_course_origin" not in body


def test_courses_endpoint_includes_lang_fingerprint(chat_client):
    """Round 2 #3 mini: /api/courses each entry must carry a `lang` field
    so the frontend can render the per-course language chip."""
    client, _ = chat_client
    r = client.get("/api/courses")
    assert r.status_code == 200
    courses = r.json()["courses"]
    assert len(courses) >= 1
    for c in courses:
        assert "lang" in c, c
        assert c["lang"] in ("zh", "en", "mixed"), c
    # And specifically the seeded en_course should fingerprint as "en"
    by_id = {c["id"]: c for c in courses}
    assert by_id["en_course"]["lang"] == "en"
    assert by_id["zh_course"]["lang"] == "zh"


# ── Round 2.1 — 5 条收尾测试（实测 bug fix 钉住） ──────────────────────


def test_classify_input_identity_zh_routes_general():
    """#R4-1 mini: 中文身份问题不能进 RAG（RAG 文档里没有 persona 信息）。"""
    from nano_notebooklm.orchestrator import router_intent as ri

    for q in ("你是谁?", "你是谁", "你叫什么名字", "介绍一下自己"):
        d = ri.classify_input(q)
        assert d.path == "general", f"{q!r} should route to general"
        assert d.reason.startswith("identity"), d.reason


def test_classify_input_identity_en_routes_general():
    """#R4-1 mini: 英文身份问题同样路由 general。"""
    from nano_notebooklm.orchestrator import router_intent as ri

    for q in ("who are you?", "Who are you", "what is your name", "Tell me about yourself"):
        d = ri.classify_input(q)
        assert d.path == "general", f"{q!r} should route to general"
        assert d.reason.startswith("identity"), d.reason


def test_classify_input_meta_course_routes_general():
    """#R4-1 mini: meta-course 问题（"这是什么课"/"what is this course"）路由 general。"""
    from nano_notebooklm.orchestrator import router_intent as ri

    for q in ("这是什么课?", "这是什么课", "what is this course about", "What is this course"):
        d = ri.classify_input(q)
        assert d.path == "general", f"{q!r} should route to general"
        assert d.reason.startswith("meta_course"), d.reason


def test_classify_input_bare_interrogative_routes_general():
    """#R4-1 mini: 单 token 疑问句不进 RAG（避免凑过 score gate 拿到伪相关引用）。"""
    from nano_notebooklm.orchestrator import router_intent as ri

    for q in ("what", "what?", "Why", "how?", "什么", "为什么", "怎么"):
        d = ri.classify_input(q)
        assert d.path == "general", f"{q!r} should route to general"
        assert d.reason.startswith("bare_q"), d.reason


# fix-all v3 #1 (review-swarm contracts #3): parametrize over the FULL
# BARE_INTERROGATIVES sets so removing any single entry red-tests instead of
# silently regressing routing.
def _all_bare_interrogatives():
    from nano_notebooklm.orchestrator import router_intent as ri
    return sorted(ri.BARE_INTERROGATIVES_EN | ri.BARE_INTERROGATIVES_ZH)


@pytest.mark.parametrize("q", _all_bare_interrogatives())
def test_classify_input_bare_interrogative_full_set(q):
    """Every entry in BARE_INTERROGATIVES_{EN,ZH} must route to general with
    `bare_q:` reason. Pinning the full set so coverage doesn't drift."""
    from nano_notebooklm.orchestrator import router_intent as ri
    d = ri.classify_input(q)
    assert d.path == "general", f"{q!r} (in BARE set) must route to general"
    assert d.reason.startswith("bare_q"), d.reason


def test_classify_input_multi_token_what_question_kept():
    """#R4-1 corner: 边界——`what is convolution` 是真问题，不能被 bare_q 误抓。"""
    from nano_notebooklm.orchestrator import router_intent as ri

    for q in ("what is convolution", "why does cache matter", "什么是卷积", "为什么需要缓存"):
        d = ri.classify_input(q)
        assert d.path == "rag", f"{q!r} should stay RAG"


def test_chat_identity_returns_persona_blurb(chat_client, monkeypatch):
    """#R4-3 mini: 用户问"你是谁"→ persona system prompt 触发，模型该收到带
    DEFAULT_PERSONA ("Study Assistant") 的 system，返回身份 blurb；不应回
    boilerplate；sources 必须空。"""
    client, server_mod = chat_client

    captured = {}

    async def stub(prompt, task_type="", system="", temperature=0.7,
                   max_tokens=4096, max_retries=3, **kwargs):
        captured["system"] = system
        captured["task_type"] = task_type
        return LLMResponse(content="I'm your Study Assistant.",
                           model="fake", input_tokens=1, output_tokens=1, latency_ms=1.0)

    monkeypatch.setattr(server_mod.router, "complete", stub)

    r = client.post("/api/chat",
                    json={"question": "你是谁?", "course_id": "en_course"})
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "general"
    assert body["sources"] == []
    assert "No relevant content" not in body["answer"]
    # Persona block reaches the model with default fallback — fix #R4-3
    assert "Study Assistant" in captured["system"]
    # Identity addendum is appended — fix #R4-1 routing → correct addendum
    assert "asking who you are" in captured["system"].lower()
    assert captured["task_type"] == "qa_general"


def test_chat_meta_course_does_not_short_circuit(chat_client, monkeypatch):
    """#R4-2 mini: 中文 meta query 即使带 default checked_files 也不能被
    filter_empty boilerplate 截胡 —— 路由要先把它判给 general。
    实测 bug 1：'你是谁? 这是什么课' 被中文 BM25 char-bigram 匹配到非勾选文件
    → filter 空 → 短路 return，从未到 general。本测试钉住 fix 后行为。"""
    client, server_mod = chat_client

    async def stub(prompt, task_type="", system="", temperature=0.7,
                   max_tokens=4096, max_retries=3, **kwargs):
        return LLMResponse(content="本课是 en_course，可以问关于它的问题。",
                           model="fake", input_tokens=1, output_tokens=1, latency_ms=1.0)

    monkeypatch.setattr(server_mod.router, "complete", stub)

    # 用户的 default — 勾了一个不会 BM25 命中"这是什么课"的文件
    r = client.post("/api/chat", json={
        "question": "这是什么课?",
        "course_id": "en_course",
        "checked_files": ["en_textbook.pdf"],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "general"
    assert body.get("filter_empty") is None  # 路由层已截走，不该有 filter_empty
    assert "No relevant content" not in body["answer"]


def test_chat_bare_interrogative_no_fake_sources(chat_client, monkeypatch):
    """#R4-4 mini: 单词 'what' 进 chat，要走 general clarification，
    sources 永远空（避免凑出伪相关引用）。"""
    client, server_mod = chat_client

    captured_prompts = []

    async def stub(prompt, task_type="", system="", temperature=0.7,
                   max_tokens=4096, max_retries=3, **kwargs):
        captured_prompts.append(prompt)
        return LLMResponse(content="What topic would you like to ask about?",
                           model="fake", input_tokens=1, output_tokens=1, latency_ms=1.0)

    monkeypatch.setattr(server_mod.router, "complete", stub)

    r = client.post("/api/chat",
                    json={"question": "what", "course_id": "en_course"})
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "general"
    assert body["sources"] == []
    # Clarification prompt addendum should override the question prompt — the
    # model is asked to produce a clarification, not an answer.
    assert any("bare interrogative" in p.lower() for p in captured_prompts), \
        "bare-q addendum prompt must be sent to model"


def test_chat_filter_empty_only_fires_when_raw_passes_gate(chat_client, monkeypatch):
    """#R4-2 corner: filter_empty 只在 raw 过 score gate 时短路。
    raw 如果本来就低质量（gate 失败），不该返 boilerplate，应当让 translation /
    cross-course / general 接力。这是 Round 2.1 #2 的核心收尾。"""
    client, server_mod = chat_client

    # raw 返回一个低分单 hit（gate 失败），且来自 user 没勾的文件
    weak = SearchResult(chunk_id="w", text="weak", source_file="not-checked.pdf",
                        location="p1", score=0.001, course_id="en_course")
    monkeypatch.setattr(server_mod.kb, "search",
                        lambda q, top_k=5, course_id=None: [weak])
    monkeypatch.setenv("RAG_SCORE_GATE_TOP1", "0.05")

    async def stub(prompt, task_type="", system="", temperature=0.7,
                   max_tokens=4096, max_retries=3, **kwargs):
        return LLMResponse(content="general fallback",
                           model="fake", input_tokens=1, output_tokens=1, latency_ms=1.0)

    monkeypatch.setattr(server_mod.router, "complete", stub)

    r = client.post("/api/chat", json={
        "question": "memory hierarchy and caches",
        "course_id": "en_course",
        "checked_files": ["weak.pdf"],  # filter killed the only weak hit
    })
    assert r.status_code == 200
    body = r.json()
    # raw failed the gate → not a filter problem → no boilerplate
    assert body.get("filter_empty") is None, body
    assert "No relevant content" not in body["answer"]
    assert body["path"] == "general"


def test_chat_filter_empty_logs_raw_top_files(chat_client, monkeypatch, caplog):
    """#R4-5 mini: filter_empty short-circuit 日志带 raw_top_files 字段，
    audit 时能看到 BM25 命中了哪些没勾的文件。"""
    import logging
    client, server_mod = chat_client

    # raw 三条强 hit 但都来自用户没勾的文件
    a = SearchResult(chunk_id="a", text="a", source_file="other-1.pdf",
                     location="p1", score=0.20, course_id="en_course")
    b = SearchResult(chunk_id="b", text="b", source_file="other-2.pdf",
                     location="p2", score=0.18, course_id="en_course")
    c = SearchResult(chunk_id="c", text="c", source_file="other-1.pdf",
                     location="p3", score=0.16, course_id="en_course")
    monkeypatch.setattr(server_mod.kb, "search",
                        lambda q, top_k=5, course_id=None: [a, b, c])
    monkeypatch.setenv("RAG_SCORE_GATE_TOP1", "0.05")

    async def stub(*a, **kw):
        raise AssertionError("LLM must not be called when filter_empty fires")
    monkeypatch.setattr(server_mod.router, "complete", stub)

    with caplog.at_level(logging.INFO):
        r = client.post("/api/chat", json={
            "question": "memory hierarchy",
            "course_id": "en_course",
            "checked_files": ["wrong.pdf"],  # 用户勾的文件 BM25 没命中
        })
    assert r.status_code == 200
    body = r.json()
    assert body.get("filter_empty") is True
    assert "No relevant content" in body["answer"]
    # 日志应带 raw_top_files 字段
    log_text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "raw_top_files" in log_text
    assert "other-1.pdf" in log_text or "other-2.pdf" in log_text


# ── 原有 #2-1 fix-all 测试段从这里继续 ─────────────────────────────────


def test_chat_filter_low_quality_signal(chat_client, monkeypatch):
    """F4: when raw search passes the score gate but the user's checked_files
    filter knocks it down to chunks that fail the gate, surface a
    `filter_low_quality` signal so the user knows the filter was causal —
    don't silently bypass narrowing by going general/translated.

    Critically: only fires when the filter is the cause. If raw itself would
    have failed the gate, this is NOT a filter problem and we fall through
    to the normal translation / general flow."""
    client, server_mod = chat_client

    # raw has 3 strong chunks; filter only matches the lowest-scoring one
    strong_a = SearchResult(chunk_id="a", text="a", source_file="strong.pdf",
                            location="p1", score=0.20, course_id="en_course")
    strong_b = SearchResult(chunk_id="b", text="b", source_file="strong.pdf",
                            location="p2", score=0.18, course_id="en_course")
    weak_c = SearchResult(chunk_id="c", text="c", source_file="weak.pdf",
                          location="p1", score=0.001, course_id="en_course")

    monkeypatch.setattr(server_mod.kb, "search",
                        lambda q, top_k=5, course_id=None: [strong_a, strong_b, weak_c])

    # Override the chat_client fixture's permissive 0.0 floor with a real
    # threshold so the gate distinguishes strong from weak. With τ=0.05:
    # - raw [0.20, 0.18, 0.001]: branch A 0.20≥0.05 ∧ 3≥2 → pass
    # - filtered [0.001]: branch A 0.001<0.05 fail; branch B 0.001<0.10 fail.
    monkeypatch.setenv("RAG_SCORE_GATE_TOP1", "0.05")

    async def stub(*a, **kw):
        raise AssertionError("LLM must not be called when filter signal fires")
    monkeypatch.setattr(server_mod.router, "complete", stub)

    r = client.post("/api/chat", json={
        "question": "memory hierarchy",
        "course_id": "en_course",
        "checked_files": ["weak.pdf"],
    })
    assert r.status_code == 200
    body = r.json()
    assert body.get("filter_low_quality") is True
    assert "path" not in body, body  # filter signals don't carry path
    assert "No relevant content" in body["answer"]
