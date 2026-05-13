"""Unit tests for `_strip_raft_preamble` — the post-processor that turns
Qwen-RAFT three-stage output (Analyze / Quote / Conclusion) into a clean
prose answer with markdown blockquotes.

Added 2026-05-13 in response to review-swarm fix-now CRITICAL #2 + the
"zero-test-coverage" finding. The function runs unconditionally on every
qwen response, so any regex regression silently corrupts user output.
"""
from __future__ import annotations

from nano_notebooklm.ai.qwen_raft_backend import _strip_raft_preamble


def test_plain_prose_with_answer_marker_passes_through():
    """Plain qwen output that happens to contain a line like 'Answer: X'
    must NOT have content before that line silently sliced off. Without
    the RAFT-format precondition gate, the strip regex would truncate
    normal Chinese / English prose."""
    s = "反向传播由前向传播衍生。Answer: 1986 年由 Rumelhart 提出。"
    assert _strip_raft_preamble(s) == s


def test_plain_chinese_prose_with_jiexi_marker_passes_through():
    """Common academic Chinese subheading '分析:' must NOT trigger the
    fallback strip path. Pre-fix, _RAFT_ANALYZE_RE.sub would delete the
    header inside legitimate prose."""
    s = "分析: 这是一道常见考题。答案是 ID3 算法。"
    assert _strip_raft_preamble(s) == s


def test_real_raft_with_quote_marker_strips_correctly():
    """When `##begin_quote##` is present, treat as RAFT format and
    extract the conclusion + render quote as markdown blockquote."""
    raft = (
        "Analyze key points: The question asks for the year.\n\n"
        "Quote evidence: ##begin_quote##反向传播\n"
        "由前向传播衍生\n"
        "Rumelhart, Hinton, Williams (1986)##end_quote##\n"
        "Final conclusion: 反向传播是由前向传播衍生的方法，1986 年提出。"
    )
    out = _strip_raft_preamble(raft)
    assert "Analyze key points" not in out
    assert "Quote evidence" not in out
    assert "##begin_quote##" not in out
    assert "1986" in out
    assert "> 反向传播" in out
    assert "> Rumelhart" in out


def test_real_raft_paraphrased_markers_strip_correctly():
    """RAFT model paraphrases its own markers; the gate still fires on
    the Analyze-section variant (`Key points to analyze:`)."""
    raft = (
        "Key points to analyze: paraphrase variant.\n\n"
        "Evidence from the document:\n\n"
        "Conclusion: 决策树的基础算法是 ID3。\n"
        "> 决策树的基础算法是 ID3 算法"
    )
    out = _strip_raft_preamble(raft)
    assert "Key points to analyze" not in out
    assert "Evidence from the document" not in out
    assert "ID3" in out


def test_markdown_list_prefix_markers_strip():
    """Some RAFT outputs prefix every marker with `- `. The regex must
    treat the list prefix as optional."""
    raft = (
        "- Analyze key points: x.\n"
        "- Quote evidence: \n"
        "- Final conclusion: 实际答案在这里。"
    )
    out = _strip_raft_preamble(raft)
    assert "Analyze key points" not in out
    assert "实际答案" in out


def test_empty_input_passes_through():
    assert _strip_raft_preamble("") == ""
    assert _strip_raft_preamble("   ") == "   "
    assert _strip_raft_preamble(None) is None  # type: ignore[arg-type]


def test_strip_is_idempotent_on_already_clean_output():
    clean = "干净的答案。\n\n> 引用内容"
    assert _strip_raft_preamble(clean) == clean


def test_raft_without_final_conclusion_falls_through_to_analyze_strip():
    """When format is RAFT (has Analyze marker) but no Conclusion marker,
    the fallback path strips Analyze section headers from the body."""
    raft = "Analyze key points: nothing.\n\n这是实际答案，没有总结标记。"
    out = _strip_raft_preamble(raft)
    assert "Analyze key points" not in out
    assert "这是实际答案" in out
