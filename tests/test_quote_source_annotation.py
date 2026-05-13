"""Unit tests for `_annotate_quote_sources`, `_looks_like_formula_block`,
and `_formula_block_to_math` in qa_skill.py.

Added 2026-05-13 in response to review-swarm fix-now CRITICAL #3 +
HIGH #6 (phantom-citation regression). These functions sit on the hot
path of every qwen-backed chat answer, but had zero unit-test coverage
before this commit.
"""
from __future__ import annotations

import re

from nano_notebooklm.skills.qa_skill import (
    _annotate_quote_sources,
    _formula_block_to_math,
    _looks_like_formula_block,
)
from nano_notebooklm.types import SearchResult


def _r(
    *, chunk_id: str = "c1", course_id: str = "x",
    source_file: str = "ch1.pdf", location: str = "Page 1/10",
    text: str = "", score: float = 0.5,
) -> SearchResult:
    return SearchResult(
        chunk_id=chunk_id, course_id=course_id,
        source_file=source_file, location=location,
        text=text, score=score,
    )


# ── _looks_like_formula_block ─────────────────────────────────────


def test_formula_detect_real_pdf_multiline_formula():
    assert _looks_like_formula_block("P(X=x|ωk)= P(ωk|x) P(ωk) P(x)")


def test_formula_detect_rejects_chinese_prose():
    assert not _looks_like_formula_block("反向传播是由前向传播衍生的方法")


def test_formula_detect_rejects_english_prose():
    assert not _looks_like_formula_block(
        "Decision tree learning is a method for predicting class labels"
    )


def test_formula_detect_xss_payload_rejected():
    """Review-swarm fix-now CRITICAL #2: the formula-block path bypasses
    the frontend's _escapeHtml because renderMarkdown lifts $$...$$ into
    a math stash BEFORE escaping. Any quote containing `<` / `>` / `&`
    must therefore be rejected here even if it looks math-like."""
    payloads = [
        "</div><img src=x onerror=alert(1)>=x",
        "</div><svg/onload=alert(1)>=x",
        "x & y = z",  # bare ampersand
        "P(x) < y >",  # angle brackets inside math
    ]
    for p in payloads:
        assert not _looks_like_formula_block(p), f"should reject: {p!r}"


def test_formula_collapses_multiline_to_single_dollar_block():
    out = _formula_block_to_math("P(X=x|ωk)=\nP(ωk|x)\nP(ωk)\nP(x)")
    assert out == "$$P(X=x|ωk)= P(ωk|x) P(ωk) P(x)$$"


# ── _annotate_quote_sources ────────────────────────────────────────


def test_exact_substring_match_attaches_source():
    chunk_text = "贝叶斯决策规则比较后验概率，使用公式 P(X=x|ωk)= P(ωk|x) P(ωk) P(x) 计算。"
    results = [_r(source_file="ch5.pdf", location="Page 12/40", text=chunk_text)]
    ans = "答案。\n\n> P(X=x|ωk)=\n> P(ωk|x)\n> P(ωk)\n> P(x)"
    out = _annotate_quote_sources(ans, results)
    assert "[Source: ch5.pdf, Page 12/40]" in out


def test_no_match_does_not_phantom_cite():
    """Review-swarm fix-now HIGH #6: when neither substring nor fuzzy
    match clears the floor, NO citation should be emitted. Pre-fix the
    code fell back to results[0] and silently mislabeled the quote."""
    results = [
        _r(source_file="ch1.pdf", location="Page 1", text="完全不相关的内容 about apples"),
    ]
    ans = "答案。\n\n> 这是一段无法匹配任何 chunk 的引用文本"
    out = _annotate_quote_sources(ans, results)
    assert "[Source:" not in out, f"phantom citation regression: {out!r}"


def test_codex_path_no_blockquote_unchanged():
    """Codex answers don't emit markdown blockquotes; the function must
    be a no-op on them."""
    results = [_r(text="something")]
    ans = "Codex 风格的纯文段答案，没有 blockquote。"
    assert _annotate_quote_sources(ans, results) == ans


def test_formula_blockquote_rewritten_to_math():
    chunk_text = "P(X=x|ωk)= P(ωk|x) P(ωk) P(x) 用于贝叶斯计算"
    results = [_r(source_file="ch5.pdf", location="Page 12/40", text=chunk_text)]
    ans = "答案。\n\n> P(X=x|ωk)=\n> P(ωk|x)\n> P(ωk)\n> P(x)"
    out = _annotate_quote_sources(ans, results)
    assert "$$" in out, f"formula not rewrapped to math: {out!r}"
    assert "[Source: ch5.pdf, Page 12/40]" in out


def test_prose_blockquote_keeps_blockquote_prefix():
    chunk_text = "反向传播由前向传播衍生而来"
    results = [_r(text=chunk_text)]
    ans = "答案。\n\n> 反向传播由前向传播衍生而来"
    out = _annotate_quote_sources(ans, results)
    # Should still have `> ` prefix (NOT rewritten as math)
    assert re.search(r"^>", out, re.M), f"prose blockquote prefix lost: {out!r}"
    assert "$$" not in out


def test_empty_results_does_not_crash():
    assert _annotate_quote_sources("answer", []) == "answer"


def test_xss_payload_in_quote_falls_back_to_prose_blockquote():
    """Critical: when a chunk text legitimately contains `<` or `>`, the
    formula detector must reject the block. The blockquote then renders
    via the normal markdown path (which escapes HTML)."""
    payload = "</div><img src=x onerror=alert(1)>=x"
    chunk_text = "some context " + payload + " more context"
    results = [_r(source_file="evil.pdf", location="Page 1", text=chunk_text)]
    ans = "答案。\n\n> " + payload
    out = _annotate_quote_sources(ans, results)
    # The payload still appears in the output (we can't strip arbitrary
    # text without breaking real chunks), but it must remain inside a
    # blockquote (`> ...`) so the markdown renderer's escapeHtml fires.
    assert "$$" not in out, f"XSS payload promoted to math block: {out!r}"
    assert re.search(r"^>", out, re.M)
