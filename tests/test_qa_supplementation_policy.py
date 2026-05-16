"""QA prompt supplementation policy (2026-05-16).

Before this change, `qa_system` rule 1 said "Answer based ONLY on the
provided reference documents" and rule 6 said "If documents don't cover
the question, say so honestly". This produced "材料中没有直接介绍 X"
refusals for questions where the documents touched adjacent concepts
(e.g. "最大熵" matched n-gram / 古德-图灵 / 朴素贝叶斯 chunks via
graphrag but no chunk defined 最大熵).

New policy: instead of refusing, split the reply in two — cite whatever
fragments the documents DO have, then add a "补充背景" section that
supplements with general knowledge (without citations). The model
should only refuse outright when documents have ZERO mention AND
general knowledge is unhelpful.

This file pins the policy by checking the prompt text + low_confidence
addendum. Behavioral verification (does the LLM actually two-part
answer?) needs manual curl testing since we don't want LLM calls in
unit tests.
"""

from __future__ import annotations

from nano_notebooklm.ai.prompt_templates import QA_PROMPT, qa_system


def test_qa_system_grounds_in_docs_when_covered():
    body = qa_system()
    # Rule 1 still anchors course-specific claims in docs.
    assert "Ground" in body or "ground" in body
    assert "documents" in body.lower()
    assert "[Source:" in body


def test_qa_system_allows_supplementation_with_general_knowledge():
    body = qa_system()
    # Rule 6 explicitly tells the model to supplement, not refuse.
    assert "补充背景" in body or "Background" in body
    assert "general knowledge" in body.lower() or "广义" in body
    # Two-part structure must be named.
    assert "课件覆盖" in body or "In the course materials" in body


def test_qa_system_refusal_only_when_zero_mention_AND_general_unhelpful():
    body = qa_system()
    # Refusal is gated by BOTH conditions (zero doc mention + general
    # knowledge wouldn't help). A simple "documents don't cover → refuse"
    # gate would re-introduce the over-conservative behaviour.
    assert "ZERO" in body or "完全不在" in body
    assert "general knowledge" in body.lower()


def test_qa_system_forbids_fabricated_citations():
    body = qa_system()
    # The supplementation policy must NOT loosen the citation contract.
    # General-knowledge parts have NO citations; only document-grounded
    # claims get [Source: ...] tags.
    assert "NEVER fabricate" in body or "never fabricate" in body.lower()


def test_qa_prompt_user_template_carries_two_part_instruction():
    rendered = QA_PROMPT.format(context="(dummy)", question="什么是最大熵")
    # User-message template (sent every turn) must also mention the
    # two-part structure so the model can't ignore the system rule.
    assert "课件覆盖" in rendered or "In the course materials" in rendered
    assert "补充背景" in rendered or "Background" in rendered
    # The literal-question delimiter is preserved (H2 from review-swarm
    # multi-turn fix-all).
    assert "<question>什么是最大熵</question>" in rendered


def test_qa_prompt_refusal_only_when_zero_and_unhelpful():
    rendered = QA_PROMPT.format(context="(x)", question="x")
    assert "完全不在课件覆盖范围内" in rendered or "ZERO mention" in rendered


def test_qa_low_confidence_addendum_still_helpful_not_refusing():
    """The graphrag low_confidence path used to literally tell the
    model 'refusing is better than confabulating' — that produced the
    user-reported over-conservative behaviour. The new addendum keeps
    the no-fabrication guard but DOES still ask the model to apply the
    two-part structure."""
    import inspect
    from nano_notebooklm.skills import qa_skill

    src = inspect.getsource(qa_skill._answer_rag if hasattr(qa_skill, "_answer_rag") else qa_skill.QASkill._answer_rag)
    # The low_confidence branch must keep the two-part structure ask.
    assert "补充背景" in src or "Background" in src
    # And must NOT re-introduce the absolute refusal language.
    # ("refusing is better than" was the old phrase that produced the
    # over-conservative behaviour.)
    assert "refusing is better than" not in src
    assert "this isn't covered directly in what I found" not in src
