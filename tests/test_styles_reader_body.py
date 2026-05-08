"""R3-1 — pin .reader-body as the scroll container for Quiz / Skills / History.

`RealQuizView` (app.jsx:1148), `SkillsDashboard` (app.jsx:1214) and
`SessionHistory` (app.jsx:1247) all use `<div className="reader-body">`,
but pre-R3-1 there was no `.reader-body` CSS rule — only the R6 Notes
rule `.notes-reader-body`. Parent `.workspace` is `overflow:hidden`, so
those three tabs could not scroll past the viewport.

The fix appends a `.reader-body { height:100%; overflow-y:auto }` rule.
These tests are grep-only (no DOM rendering); they pin that the rule
exists and that it does NOT shadow the existing `.notes-reader-body`
declarations (Notes view uses BOTH classes simultaneously).
"""

from __future__ import annotations

import re
from pathlib import Path

STYLES = Path(__file__).resolve().parent.parent / "frontend" / "styles.css"


def _find_rule_body(text: str, selector: str) -> str:
    """Return the body of the first CSS rule whose selector matches exactly
    (anchored to start-of-line, no leading comma-grouping). Raises if absent.
    """
    pattern = re.compile(
        r"^" + re.escape(selector) + r"\s*\{([^}]*)\}",
        re.MULTILINE,
    )
    match = pattern.search(text)
    assert match, f"selector {selector!r} not found in styles.css"
    return match.group(1)


def test_reader_body_has_scroll_container():
    """The .reader-body class must define overflow-y:auto + height:100% so
    Quiz / Skills / History tabs can scroll inside the
    `.workspace { overflow:hidden }` parent. Without this, content past the
    viewport is silently clipped (the user-reported bug).
    """
    text = STYLES.read_text(encoding="utf-8")
    body = _find_rule_body(text, ".reader-body")
    assert re.search(r"overflow-y\s*:\s*auto", body), (
        f".reader-body must contain overflow-y:auto; got block: {body!r}"
    )
    assert re.search(r"height\s*:\s*100%", body), (
        f".reader-body must contain height:100%; got block: {body!r}"
    )


def test_reader_body_independent_from_notes_reader_body():
    """Notes view uses `<div className="reader-body notes-reader-body">`,
    so both rules apply simultaneously. With equal class-specificity the
    later source-order rule wins for any conflicting property. The
    `.reader-body` rule must therefore:

      - exist as its own standalone selector (no comma-grouping that
        would couple future edits across Notes and Quiz/Skills);
      - NOT declare `padding` or `max-width` (those belong to
        `.notes-reader-body` and the inline `style` on the consumer
        divs respectively — overriding them here would silently break
        Notes layout).
    """
    text = STYLES.read_text(encoding="utf-8")

    # Both rules exist as standalone selectors.
    assert re.search(r"^\.reader-body\s*\{", text, re.MULTILINE), (
        ".reader-body rule not found"
    )
    assert re.search(r"^\.notes-reader-body\s*\{", text, re.MULTILINE), (
        ".notes-reader-body rule not found"
    )

    # Neither side comma-grouped with the other.
    assert not re.search(r"^\.reader-body\s*,", text, re.MULTILINE), (
        ".reader-body must NOT be comma-grouped with another selector"
    )
    assert not re.search(r",\s*\.reader-body\s*\{", text), (
        "another selector must NOT be comma-grouped before .reader-body { ... }"
    )
    assert not re.search(r",\s*\.notes-reader-body\s*\{", text), (
        ".notes-reader-body must NOT be comma-grouped with .reader-body"
    )

    # `.reader-body` block must NOT set padding / max-width — those would
    # shadow .notes-reader-body's own padding (Notes uses both classes,
    # equal specificity, last-source-wins) and the consumer divs'
    # inline `style={{ padding: "28px 40px" }}`.
    body = _find_rule_body(text, ".reader-body")
    assert not re.search(r"\bpadding\s*:", body), (
        f".reader-body must NOT set padding (would shadow .notes-reader-body / "
        f"inline style); got block: {body!r}"
    )
    assert not re.search(r"\bmax-width\s*:", body), (
        f".reader-body must NOT set max-width (would shadow consumer layout); "
        f"got block: {body!r}"
    )
