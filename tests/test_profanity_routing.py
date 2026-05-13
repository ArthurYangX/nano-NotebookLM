"""Unit tests for PROFANITY routing.

Review-swarm fix-now CRITICAL #4: 24 keywords were added with no test
asserting that (a) they route to general, (b) decision.reason carries
the `profanity:` namespace, (c) the profanity rule precedes IDENTITY
matching so a slur-laced "你是谁啊你这傻逼" routes to profanity not
identity.
"""
from __future__ import annotations

from nano_notebooklm.orchestrator.router_intent import classify_input


def test_zh_profanity_routes_to_general():
    for kw in ["傻逼", "操你", "妈的", "智障", "sb"]:
        d = classify_input(kw)
        assert d.path == "general"
        assert d.reason.startswith("profanity"), f"{kw} → {d.reason!r}"


def test_en_profanity_routes_to_general():
    for kw in ["fuck", "shit", "asshole", "bitch", "moron"]:
        d = classify_input(kw)
        assert d.path == "general"
        assert d.reason.startswith("profanity"), f"{kw} → {d.reason!r}"


def test_profanity_precedes_identity_match():
    """Order matters: a "你是谁啊你这傻逼" must classify as profanity
    (which deflects + redirects), not identity (which emits a friendly
    self-intro the user didn't ask for)."""
    d = classify_input("你是谁啊你这傻逼")
    assert d.path == "general"
    assert d.reason.startswith("profanity"), f"got {d.reason!r}"


def test_profanity_substring_match_in_long_query():
    """The keyword scan is substring-based so insults embedded in
    longer sentences still route correctly."""
    d = classify_input("go fuck yourself, you idiot")
    assert d.path == "general"
    assert d.reason.startswith("profanity")


def test_normal_query_still_routes_rag():
    """Sanity: profanity scan does not falsely route ordinary questions."""
    d = classify_input("什么是反向传播")
    assert d.path == "rag"


def test_normal_short_word_not_misrouted():
    """`shitake` would substring-match `shit` — confirm we accept the
    rare false-positive (better to over-deflect than miss a slur).
    Documenting current behavior, not asserting it's optimal."""
    d = classify_input("shitake mushroom")
    assert d.path == "general"  # would substring-match `shit` — known trade-off


def test_empty_or_whitespace_is_not_profanity():
    """Empty / whitespace must hit the punctuation/empty path first."""
    d = classify_input("")
    assert d.path == "general"
    assert "profanity" not in d.reason  # caught by empty-strip rule
