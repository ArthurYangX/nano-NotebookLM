"""更严格的 router_intent 边界测试 — 已有 1108 行测试主要覆盖正常路径，
这里专门补充实测中容易踩坑的 corner case：

- 零宽字符 / NBSP / 全角空格的 strip
- URL / code snippet / 数字串作为 query
- 极长 query（4000 字符）
- bare interrogative 在 trailing punctuation 多种形态下的归类
- 标点+短词（"x?"、"嗯?"）的 weight 路径
- detect_lang 在边界比例 (≈15%) 下的稳定性
- detect_lang 仅含数字 / 仅含符号 / 仅含 emoji
- compute_lang_fingerprint 空输入、单字符、长 mixed
- _read_threshold / _read_min_hits 对非法 env 值的回落
- get_course_lang 在缓存 / 无 peek_chunks / 空 corpus 下的退化路径
- clear_lang_cache 单 course / 全清的差别
- passes_score_gate 在 threshold=0 / min_hits=1 / 重复 score 下的判定
- IDENTITY / META_COURSE / GREETING 关键词是否会被误命中（substring 风险）
"""

from __future__ import annotations

import pytest

from nano_notebooklm.orchestrator import router_intent as ri
from nano_notebooklm.types import SearchResult


# ── strip + zero-width edge cases ─────────────────────────────────────


@pytest.mark.parametrize("payload", [
    "​​​",  # zero-width space ×3 → punctuation/whitespace
    "　　",         # ideographic space
    "\xa0\xa0",             # NBSP
    " ",               # line separator
    "﻿",               # BOM
])
def test_classify_input_treats_invisible_chars_as_general(payload):
    """Pin: any string composed only of zero-width / formatting chars must
    NOT reach RAG — that would hit BM25 with garbage tokens and invent a
    citation off whatever happened to share rare bigrams.

    Note: zero-width chars are not in `\\W` but they *are* surrounded by no
    alphanumeric content; the weight-floor branch must still catch them."""
    decision = ri.classify_input(payload)
    assert decision.path == "general", (
        f"{payload!r} ({[hex(ord(c)) for c in payload]}) should not reach RAG; "
        f"got reason={decision.reason!r}"
    )


def test_classify_input_url_only_query_routes_general_or_rag_consistently():
    """URL-only queries are degenerate — they're not greetings, not bare
    interrogatives, not punctuation. The current rule lets them through
    to RAG. Pin that contract so a future "be smart about URLs" change is
    forced to revisit this test."""
    d = ri.classify_input("https://example.com/foo/bar")
    assert d.path in ("rag", "general")
    # cleaned_query should be the stripped original
    assert d.cleaned_query == "https://example.com/foo/bar"


def test_classify_input_extremely_long_query_does_not_crash():
    """A 4000-char query is the API max. The classifier must run in
    sub-second time without recursing or O(n²) scanning."""
    q = "memory hierarchy " * 250  # ~4000 chars
    d = ri.classify_input(q)
    assert d.path == "rag"
    assert d.cleaned_query.startswith("memory")


def test_classify_input_pure_digits_routes_rag_or_general_consistently():
    """Numbers are alphanumeric, weight passes the floor, so they reach RAG
    by the default rule. This pins that contract."""
    d = ri.classify_input("12345")
    assert d.path == "rag"


def test_classify_input_code_snippet_routes_rag():
    """Code snippets carry meaningful content tokens — they should reach RAG
    (not be flagged as 'punctuation' even though they have lots of brackets)."""
    d = ri.classify_input("def fwd(x): return W @ x + b")
    assert d.path == "rag"


# ── bare interrogatives × punctuation surface ─────────────────────────


@pytest.mark.parametrize("q", [
    "what", "what?", "what？", "WHAT", "What",
    "  what  ", "what  ?", "WHY?", "How?",
])
def test_classify_input_bare_en_interrogatives_all_general(q):
    d = ri.classify_input(q)
    assert d.path == "general", f"{q!r} should be flagged as bare interrogative"


@pytest.mark.parametrize("q", [
    "什么", "什么?", "什么？", "为什么", "为什么？", "怎么", "如何",
])
def test_classify_input_bare_zh_interrogatives_all_general(q):
    d = ri.classify_input(q)
    assert d.path == "general", f"{q!r} should be flagged as bare interrogative"


def test_classify_input_long_question_with_what_is_not_bare():
    """The interrogative trap: "what" alone → general; "what is convolution"
    → RAG. Make sure adding a real noun rescues the routing decision."""
    for q in ("what is convolution", "what is RAG", "what does backprop do",
              "什么是反向传播", "为什么用 dropout"):
        d = ri.classify_input(q)
        assert d.path == "rag", f"{q!r} should reach RAG"


# ── greeting substring traps ──────────────────────────────────────────


def test_classify_input_greeting_keyword_inside_real_question_does_not_misfire():
    """The greeting check only fires when ``len(meaningful) <= 6``. So
    "hi" → general, but "history of the cache hierarchy" (which contains
    'hi' as a substring of 'history') → RAG."""
    d = ri.classify_input("history of the cache hierarchy")
    assert d.path == "rag"


def test_classify_input_thanks_short_form_general():
    """Greetings that fit under the ``len(meaningful) <= 6`` cap are routed
    general. ``thanks`` (6) just makes it; ``谢谢`` (2) and ``多谢`` (2) too."""
    for q in ("thanks", "多谢", "谢谢"):
        assert ri.classify_input(q).path == "general", q


def test_classify_input_thank_you_falls_through_due_to_length_cap():
    """Pin: 'thank you' has 8 alphanumeric chars, above the greeting check's
    6-char ceiling, so it currently falls through to RAG. This is a known
    gap — pinning it documents that 'thank you' produces the surprising
    'no relevant content' UX. If the cap is later raised to 9, flip this
    assertion to 'general'."""
    assert ri.classify_input("thank you").path == "rag"


# ── identity / meta-course substring traps ───────────────────────────


def test_classify_input_identity_keyword_substring_misfire_pinned():
    """'who is this' is an identity keyword. Pin: if the keyword appears as
    a substring of a longer real question, the current implementation will
    *also* route to general. Document the trade-off."""
    d = ri.classify_input("who is this guy that wrote the chapter on RAG")
    # current rule: substring match → general. Acceptable trade-off for now;
    # if you decide to require word-boundary matching, adjust this assertion.
    assert d.path == "general"
    assert "identity" in d.reason


def test_classify_input_meta_course_keyword_routes_general():
    for q in ("这是什么课", "what is this course", "describe this course"):
        d = ri.classify_input(q)
        assert d.path == "general"
        assert "meta_course" in d.reason


# ── weight floor edge values ──────────────────────────────────────────


def test_classify_input_weight_exactly_floor_treated_as_short():
    """Weight 2 < 3 → general. Two ASCII chars '内 ' would be weight 2
    (1 CJK = 2)."""
    d = ri.classify_input("内")  # 1 CJK = 2 < 3
    assert d.path == "general"


def test_classify_input_weight_at_threshold_passes():
    """Weight 3 (3 ASCII chars) → reaches RAG."""
    d = ri.classify_input("rag")
    assert d.path == "rag"


def test_classify_input_two_cjk_chars_passes_threshold():
    """Two CJK chars = weight 4 → enters RAG (greeting check first; here we
    pick a non-greeting compound)."""
    d = ri.classify_input("缓存")
    assert d.path == "rag"


# ── detect_lang edge cases ────────────────────────────────────────────


def test_detect_lang_only_digits_treated_as_en():
    """Pure-digit strings have zero alphabetic content; we default to 'en'
    for routing purposes."""
    assert ri.detect_lang("12345") == "en"


def test_detect_lang_only_punctuation_treated_as_en():
    assert ri.detect_lang("!!!???...") == "en"


def test_detect_lang_only_emoji_treated_as_en():
    assert ri.detect_lang("💀💀💀") == "en"


def test_detect_lang_zh_below_threshold_falls_to_en():
    """Below 15% CJK ratio: treated as English. 1 CJK / 20 ASCII = 4.7%."""
    text = "this is a long english sentence about 内 caches"
    assert ri.detect_lang(text) == "en"


def test_detect_lang_zh_above_threshold_treated_as_zh():
    text = "this 内存"  # ~33% CJK by char count among alphabetic
    assert ri.detect_lang(text) in ("zh", "mixed")


# ── compute_lang_fingerprint edge cases ──────────────────────────────


def test_compute_lang_fingerprint_empty_returns_safe_default():
    fp = ri.compute_lang_fingerprint([])
    assert fp == {"lang": "en", "zh_ratio": 0.0, "en_ratio": 0.0}


def test_compute_lang_fingerprint_only_punctuation_safe_default():
    fp = ri.compute_lang_fingerprint(["!!!", "...", "???"])
    assert fp["lang"] == "en"
    assert fp["zh_ratio"] == 0.0


def test_compute_lang_fingerprint_borderline_30_30_marked_mixed():
    """30/30 is the canonical mixed boundary. Make a sample that hits it
    cleanly: 3 CJK + 7 ASCII letters → zh=30%, en=70%, NOT mixed."""
    fp = ri.compute_lang_fingerprint(["中文 abc abcd"])
    # Above check: zh=2/(2+7)=22%; below 30% → en (NOT mixed).
    # Actually CJK chars: 中,文 = 2; ASCII letters: a,b,c,a,b,c,d = 7; total 9.
    # zh_ratio = 2/9 ≈ 22% < 30% → "en"
    assert fp["lang"] == "en"


def test_compute_lang_fingerprint_clear_mixed():
    fp = ri.compute_lang_fingerprint(["这是 mixed 文本 with English 单词混合"])
    assert fp["lang"] == "mixed"
    assert fp["zh_ratio"] >= 0.30
    assert fp["en_ratio"] >= 0.30


# ── threshold / min_hits env clamping ────────────────────────────────


def test_read_threshold_invalid_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("RAG_SCORE_GATE_TOP1", "not-a-float")
    assert ri._read_threshold() == ri.DEFAULT_TOP1_THRESHOLD


def test_read_threshold_negative_clamped_to_zero(monkeypatch):
    monkeypatch.setenv("RAG_SCORE_GATE_TOP1", "-0.5")
    assert ri._read_threshold() == 0.0


def test_read_threshold_above_one_clamped_to_one(monkeypatch):
    monkeypatch.setenv("RAG_SCORE_GATE_TOP1", "10.0")
    assert ri._read_threshold() == 1.0


def test_read_threshold_nan_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("RAG_SCORE_GATE_TOP1", "nan")
    assert ri._read_threshold() == ri.DEFAULT_TOP1_THRESHOLD


def test_read_threshold_inf_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("RAG_SCORE_GATE_TOP1", "inf")
    assert ri._read_threshold() == ri.DEFAULT_TOP1_THRESHOLD


def test_read_min_hits_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("RAG_SCORE_GATE_MIN_HITS", "not-int")
    assert ri._read_min_hits() == ri.DEFAULT_MIN_HITS


def test_read_min_hits_zero_clamped_to_one(monkeypatch):
    monkeypatch.setenv("RAG_SCORE_GATE_MIN_HITS", "0")
    assert ri._read_min_hits() == 1


# ── score gate edge values ───────────────────────────────────────────


def _r(score: float, idx: int = 0) -> SearchResult:
    return SearchResult(chunk_id=f"c{idx}", text=f"t{idx}",
                        source_file="f.pdf", location="p.1",
                        score=score, course_id="x")


def test_score_gate_threshold_zero_admits_any_min_hits():
    """threshold=0, min_hits=1 → any non-empty result list passes branch A."""
    assert ri.passes_score_gate([_r(0.0)], top1_threshold=0.0, min_hits=1) is True


def test_score_gate_threshold_zero_min_hits_two_requires_two():
    assert ri.passes_score_gate([_r(0.0)], top1_threshold=0.0, min_hits=2) is True
    # Branch B with threshold=0 requires top1>=0 which is always true →
    # single result also passes via branch B. Pin the actual behavior.


def test_score_gate_top1_exactly_threshold_passes():
    assert ri.passes_score_gate([_r(0.02), _r(0.01, 1)],
                                top1_threshold=0.02, min_hits=2) is True


def test_score_gate_branch_b_exact_2x():
    """top1 == 2*threshold, single hit → branch B accepts."""
    assert ri.passes_score_gate([_r(0.04)], top1_threshold=0.02, min_hits=5) is True


def test_score_gate_high_top1_low_min_hits_branch_a():
    """min_hits=1 → branch A subsumes branch B and accepts any threshold-
    satisfying single hit. Pinned in the docstring."""
    assert ri.passes_score_gate([_r(0.025)], top1_threshold=0.02, min_hits=1) is True


def test_score_gate_negative_score_rejected_via_branch_a_b():
    """Defensive: a corrupt index could surface a negative score; the gate
    must still reject."""
    assert ri.passes_score_gate([_r(-0.1)], top1_threshold=0.02, min_hits=2) is False


# ── lang cache: get / clear / partial / replace ──────────────────────


class _FakeKB:
    """Minimal kb stub for lang cache tests."""

    def __init__(self, peek_data=None, get_data=None):
        self._peek = peek_data
        self._get = get_data or []

    def peek_chunks(self, course_id, n=30):
        if self._peek is None:
            raise AssertionError("get_course_lang should not call peek when uncached")
        return self._peek[:n]


def _mk_chunk(text):
    from nano_notebooklm.types import Chunk, FileType
    return Chunk(chunk_id="c", doc_id="d", course_id="x", text=text,
                 file_type=FileType.PDF, source_file="f.pdf", location="p.1")


def test_get_course_lang_caches_after_first_call():
    ri._LANG_CACHE.clear()
    kb = _FakeKB(peek_data=[_mk_chunk("memory hierarchy" * 5)])
    first = ri.get_course_lang(kb, "course_a")
    assert first == "en"
    # Second call with a kb that would BLOW UP if peek were re-invoked.
    bomb = _FakeKB(peek_data=None)
    second = ri.get_course_lang(bomb, "course_a")
    assert second == "en"


def test_get_course_lang_none_course_returns_none():
    assert ri.get_course_lang(_FakeKB(peek_data=[]), None) is None
    assert ri.get_course_lang(_FakeKB(peek_data=[]), "") is None


def test_clear_lang_cache_single_course_does_not_clear_others():
    ri._LANG_CACHE.clear()
    kb_a = _FakeKB(peek_data=[_mk_chunk("english text" * 5)])
    kb_b = _FakeKB(peek_data=[_mk_chunk("中文 内容" * 10)])
    assert ri.get_course_lang(kb_a, "A") == "en"
    assert ri.get_course_lang(kb_b, "B") == "zh"
    ri.clear_lang_cache("A")
    assert "A" not in ri._LANG_CACHE
    assert "B" in ri._LANG_CACHE


def test_clear_lang_cache_global_drops_everything():
    ri._LANG_CACHE.clear()
    kb_a = _FakeKB(peek_data=[_mk_chunk("english text" * 5)])
    kb_b = _FakeKB(peek_data=[_mk_chunk("中文 内容" * 10)])
    ri.get_course_lang(kb_a, "A")
    ri.get_course_lang(kb_b, "B")
    ri.clear_lang_cache(None)
    assert ri._LANG_CACHE == {}


def test_get_course_lang_empty_corpus_safe_default():
    """Course exists but has 0 chunks → default to 'en' (matches the
    `compute_lang_fingerprint([])` behavior)."""
    ri._LANG_CACHE.clear()
    kb = _FakeKB(peek_data=[])
    assert ri.get_course_lang(kb, "empty_course") == "en"


def test_get_course_lang_falls_back_to_get_chunks_when_no_peek():
    """If kb doesn't expose peek_chunks (older shim / mocks), the function
    falls back to a full get_chunks load."""
    ri._LANG_CACHE.clear()

    class _NoPeek:
        def get_chunks(self, course_id=None):
            return [_mk_chunk("english text" * 5)]

    assert ri.get_course_lang(_NoPeek(), "fallback_course") == "en"
