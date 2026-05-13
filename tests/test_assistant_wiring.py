"""IME composition + ESC cancel wiring contract tests for the chat assistant.

User reported (2026-05-06): pressing Enter while a Chinese IME is still
showing candidate words sends a half-finished message; and there's no way to
abort a slow / wrong send. This file pins the wiring so future refactors
can't silently regress those affordances.

Project has no JS test runner (no jsdom, no babel-test) so we use string-grep
contract tests, mirroring the discipline already established in
test_chunks_endpoint.py for reader.jsx.
"""

from __future__ import annotations

import re
from pathlib import Path


FRONTEND = Path(__file__).resolve().parent.parent / "frontend"


def _read(name: str) -> str:
    return (FRONTEND / name).read_text(encoding="utf-8")


# ── IME composition guard ─────────────────────────────────────────────


def test_assistant_jsx_blocks_enter_during_ime_composition():
    """Pressing Enter while an IME (中文/日文/한국어) shows candidate words
    must NOT send the message. Enforced at three layers (browsers vary):
      - composingRef tracked via onCompositionStart / onCompositionEnd
      - e.isComposing on the keydown event
      - e.keyCode === 229 (Safari / older Firefox legacy signal)"""
    text = _read("assistant.jsx")

    # Layer 1: composition event listeners must be wired on the textarea
    assert re.search(r"onCompositionStart\s*=", text), \
        "textarea must wire onCompositionStart"
    assert re.search(r"onCompositionEnd\s*=", text), \
        "textarea must wire onCompositionEnd"

    # Layer 2: composingRef must guard the Enter handler
    assert "composingRef" in text, \
        "Assistant must track an IME composingRef"
    # `composingRef.current` must appear in the keydown guard alongside Enter
    enter_block = re.search(
        r'e\.key\s*===\s*"Enter"[\s\S]{0,200}?(composingRef|isComposing|keyCode)',
        text,
    )
    assert enter_block is not None, \
        "Enter keydown must be guarded by composingRef / isComposing / keyCode 229"

    # Layer 3: legacy 229 fallback explicitly mentioned
    assert "229" in text, \
        "keydown guard must include the legacy keyCode 229 fallback"


# ── ESC cancel ────────────────────────────────────────────────────────


def test_assistant_jsx_esc_aborts_inflight_chat():
    """Pressing Esc while a chat is in flight must abort the fetch and clear
    the thinking state, so the user can interrupt a slow / wrong send.
    Required wiring:
      - AbortController instantiated in handleSend
      - signal threaded through to API.chat
      - Esc handler in handleKeyDown that calls handleCancel
      - handleCancel function that calls .abort() on the active controller"""
    text = _read("assistant.jsx")

    # AbortController must be created and wired through to API.chat
    assert re.search(r"new\s+AbortController\s*\(", text), \
        "handleSend must create an AbortController"
    assert re.search(r"API\.chat\s*\([^)]*signal", text, re.DOTALL), \
        "API.chat must receive { signal } so the fetch can be aborted"

    # Esc handler in keydown
    assert re.search(r'e\.key\s*===\s*"Escape"', text), \
        "handleKeyDown must branch on Escape"

    # handleCancel must exist and call .abort()
    assert re.search(r"function\s+handleCancel\b", text), \
        "Assistant must declare a handleCancel function"
    assert re.search(r"\.abort\s*\(\s*\)", text), \
        "handleCancel must call AbortController.abort()"


def test_assistant_jsx_renders_cancel_button_while_thinking():
    """When `thinking` is true, the send arrow swaps to a cancel button so
    users without a keyboard (or who don't know the Esc affordance) can still
    interrupt. Pin both the className modifier and the onClick wiring."""
    text = _read("assistant.jsx")

    # `thinking ? (... cancel ...) : (... send ...)` ternary in the input bar
    assert "send cancel" in text, \
        "thinking-state button must use the `send cancel` className"
    assert re.search(r"onClick\s*=\s*\{\s*handleCancel\s*\}", text), \
        "cancel button must call handleCancel onClick"


def test_api_js_chat_accepts_abort_signal():
    """api.js: API.chat must accept a `{ signal }` option and pass it to fetch
    via _post → _request → fetch. Without this binding the AbortController in
    assistant.jsx is dead — abort() would have nothing to cancel."""
    text = _read("api.js")

    # API.chat signature must include `{ signal }`
    assert re.search(
        r"async\s+chat\s*\([^)]*\{\s*signal\s*\}",
        text,
    ), "API.chat must accept `{ signal }` as an option"

    # _post must accept opts and spread them into the fetch options
    assert re.search(r"function\s+_post\s*\([^)]*opts", text), \
        "_post must accept an `opts` parameter to thread signal through"


# ── styles ────────────────────────────────────────────────────────────


def test_styles_have_cancel_state():
    """The cancel button needs a distinct visual state (crimson, larger ×)
    so users can tell at a glance that the send action has flipped meaning."""
    text = _read("styles.css")
    assert ".send.cancel" in text, \
        "styles.css must define `.asst-input .send.cancel` for the abort state"


# ── Layout / formatting (user feedback 2026-05-06): equations should be
# visibly distinct from prose, and paragraph cadence should be tight. ──


def test_qa_system_prompts_include_formatting_discipline():
    """Both qa_system() and general_qa_system() must carry the formatting
    discipline that mandates `$...$` / `$$...$$` for math and forbids
    multi-blank-line gaps. Otherwise the LLM emits raw `T = a + b` prose
    which the renderer can't style."""
    from nano_notebooklm.ai.prompt_templates import (
        qa_system, general_qa_system, FORMATTING_DISCIPLINE,
    )
    for name, prompt in [("qa_system", qa_system()),
                         ("general_qa_system", general_qa_system())]:
        assert FORMATTING_DISCIPLINE in prompt, \
            f"{name} must embed FORMATTING_DISCIPLINE so equations get $...$ wrapping"
    # Spot-check the actual rules are mentioned (cheap drift detection)
    assert "$...$" in FORMATTING_DISCIPLINE
    assert "$$...$$" in FORMATTING_DISCIPLINE
    assert "ONE blank line" in FORMATTING_DISCIPLINE


def test_render_markdown_collapses_runaway_blank_lines():
    """assistant.jsx renderMarkdown must collapse 3+ consecutive newlines
    into the standard \\n\\n paragraph separator — otherwise an LLM that
    leaks 4 newlines produces giant visible vertical gaps."""
    text = _read("assistant.jsx")
    assert re.search(r"\\n\{3,\}/g.*?\\n\\n", text), \
        "renderMarkdown must collapse \\n{3,} into \\n\\n"
    # And collapse 2+ consecutive <br/>s (intra-paragraph runaway)
    assert re.search(r"<br\\s\*\\/\?>", text), \
        "renderMarkdown must collapse runaway <br/> sequences"


def test_render_markdown_promotes_math_block_out_of_paragraph():
    """A `$$...$$` block inside a paragraph would otherwise produce
    `<p>...<div>...</div></p>` which the browser auto-fixes with an empty
    trailing `<p></p>`. After the KaTeX retrofit the wrapper is `math-display`
    (a div KaTeX renders into) — same lift-out-of-<p> + drop-empty-<p></p>
    invariant applies."""
    text = _read("assistant.jsx")
    assert "math-display" in text, \
        "renderMarkdown must wrap display math in a `math-display` div"
    assert re.search(r"<p>\\s\*<\\/p>", text), \
        "renderMarkdown must drop empty <p></p> introduced by lifting math-display out"


def test_styles_have_distinct_math_treatment():
    """Math-block should be visually anchored (mono + accent border +
    centred). Math-inline should look like a chip distinct from prose."""
    text = _read("styles.css")
    # Math-block: centred + accent border + mono
    assert re.search(r"\.math-block\s*\{[^}]*text-align:\s*center", text, re.DOTALL), \
        "math-block must be text-align:center so equations anchor visually"
    assert re.search(r"\.math-block\s*\{[^}]*border-left:[^}]*var\(--accent\)", text, re.DOTALL), \
        "math-block must carry the accent left border"
    # Math-inline: chip border so it's visibly distinct from prose
    assert re.search(r"\.math-inline\s*\{[^}]*border:\s*1px\s+solid", text, re.DOTALL), \
        "math-inline must have a chip border so it stands apart from prose"


def test_styles_paragraph_cadence_is_tight():
    """`.bubble p { margin: 0 0 8px 0 }` was producing visibly too-large
    paragraph gaps per user feedback. Tighten to 4px and pin so it can't
    silently drift back."""
    text = _read("styles.css")
    assert re.search(r"\.bubble\s+p\s*\{\s*margin:\s*0\s+0\s+4px", text), \
        "bubble paragraph margin must be tight (4px) so the chat doesn't read as widely-spaced"


# ── KaTeX rendering (real LaTeX, not raw \frac{...}) ──────────────────


def test_index_html_loads_katex_cdn():
    """KaTeX CSS + JS + auto-render extension must be referenced from
    index.html — without these the bubble's `_renderMathInBubble` no-ops."""
    text = _read("index.html")
    assert "katex.min.css" in text, "index.html must link katex.min.css"
    assert "katex.min.js" in text, "index.html must load katex.min.js"
    assert "auto-render.min.js" in text, \
        "index.html must load the auto-render extension (provides renderMathInElement)"


def test_assistant_jsx_calls_katex_after_innerhtml_lands():
    """The bubble runs dangerouslySetInnerHTML so React's effect cycle won't
    re-render math automatically. We must explicitly walk the DOM via
    `renderMathInElement` from a useEffect tied to the bubble ref."""
    text = _read("assistant.jsx")
    assert "renderMathInElement" in text, \
        "assistant.jsx must call window.renderMathInElement to render KaTeX"
    assert re.search(r"function\s+_renderMathInBubble\b", text), \
        "assistant.jsx must declare _renderMathInBubble helper"
    # MessageBubble must wire useRef + useEffect to call the helper
    bubble_block = re.search(
        r"function\s+MessageBubble[\s\S]+?return\s*\(",
        text,
    )
    assert bubble_block, "MessageBubble must exist"
    bubble_src = bubble_block.group(0)
    assert "useRefA" in bubble_src and "useEffectA" in bubble_src, \
        "MessageBubble must use useRef + useEffect to render KaTeX after html lands"
    # Delimiters configured for both inline + display math
    assert re.search(r'left:\s*"\$\$"', text) and re.search(r'left:\s*"\$"', text), \
        "renderMathInElement config must declare $...$ and $$...$$ delimiters"


def test_render_markdown_preserves_latex_through_html_escaping():
    """Earlier renderer ran html-escape over math content too, mangling
    `<`/`>` inside LaTeX. Pin that math gets stashed BEFORE escaping so
    `\\frac{a}{b}` survives intact and KaTeX can parse it."""
    text = _read("assistant.jsx")
    # The math-stashing pre-pass + escapeHtml after must both exist
    assert re.search(r"const\s+mathTokens\s*=\s*\[\]", text), \
        "renderMarkdown must stash math tokens before HTML-escape"
    assert "_escapeHtml" in text, \
        "renderMarkdown must HTML-escape non-math content"


# ── Suggestion-row toggle ─────────────────────────────────────────────


def test_assistant_jsx_suggestion_row_is_toggleable():
    """The quick-suggestion chip row (Summarize this course / Generate quiz /
    etc) must be hideable via × button and re-openable via "+ suggestions"
    chip, with the choice persisted in localStorage so it sticks across
    reloads."""
    text = _read("assistant.jsx")
    assert re.search(r"showSuggestions", text), \
        "Assistant must track a showSuggestions state"
    assert "localStorage" in text and "show-suggestions" in text, \
        "showSuggestions choice must persist via localStorage"
    assert re.search(r"function\s+toggleSuggestions\b", text), \
        "Assistant must declare toggleSuggestions helper"
    assert "suggest-toggle" in text, \
        "× / + chip must use the suggest-toggle className"
    assert "asst-suggest collapsed" in text, \
        "hidden state must use the `asst-suggest collapsed` className"


def test_styles_have_suggest_toggle_state():
    """The toggle button needs a chip-style affordance so it's discoverable
    in both expanded (×) and collapsed (+ suggestions) states."""
    text = _read("styles.css")
    assert ".suggest-toggle" in text, \
        "styles.css must define .suggest-toggle for the hide/show chip"
    assert re.search(r"\.asst-suggest\.collapsed\b", text), \
        "styles.css must define a collapsed state for the suggestion row"


def test_formatting_discipline_mentions_katex_explicitly():
    """The prompt now must tell the LLM that KaTeX is the renderer (so it
    knows raw `\\frac{...}` outside delimiters won't render). This is what
    flipped the user's last test from `\\frac{1}{(1-p)+p/s}` (raw) to a
    properly-wrapped `$$\\frac{1}{(1-p)+p/s}$$` (KaTeX-rendered)."""
    from nano_notebooklm.ai.prompt_templates import FORMATTING_DISCIPLINE
    assert "KaTeX" in FORMATTING_DISCIPLINE, \
        "FORMATTING_DISCIPLINE must name the renderer (KaTeX) so the LLM doesn't emit bare \\frac"
    assert "bare LaTeX" in FORMATTING_DISCIPLINE or "raw LaTeX" in FORMATTING_DISCIPLINE, \
        "Discipline must explicitly forbid bare/raw LaTeX outside delimiters"


def test_formatting_discipline_has_glossary_good_bad_examples():
    """User reported (2026-05-06) that the LLM emits glossary rows as
    `$p$\\np: ...` (math token on its own line, description on next), which
    the markdown renderer turns into separate paragraphs with visible gaps.
    The prompt must show explicit GOOD vs BAD examples so the LLM uses a
    bullet list instead."""
    from nano_notebooklm.ai.prompt_templates import FORMATTING_DISCIPLINE
    assert "GOOD" in FORMATTING_DISCIPLINE and "BAD" in FORMATTING_DISCIPLINE, \
        "Discipline must show GOOD vs BAD glossary examples"
    # The good example uses a bullet list with $var$: 描述 inline
    assert "- $p$" in FORMATTING_DISCIPLINE or "- $\\text" in FORMATTING_DISCIPLINE, \
        "GOOD example must use markdown bullet list with inline math + 描述"


# ── Shared markdown / KaTeX layer (markdown.js) ────────────────────────


def test_markdown_js_exposes_shared_helpers():
    """frontend/markdown.js must declare window.NanoMarkdown with the four
    helpers chat / Notes / Reader rely on. Without this the per-surface
    fallback paths run and consumers can drift apart again."""
    text = _read("markdown.js")
    for name in ("escapeHtml", "renderMath", "stashMath", "throttle"):
        assert re.search(rf"\b{name}\b", text), \
            f"markdown.js must export {name}"
    assert "window.NanoMarkdown" in text, \
        "markdown.js must publish window.NanoMarkdown"


def test_index_html_loads_markdown_js_before_components():
    """index.html must load markdown.js before any Babel-transpiled component
    that depends on NanoMarkdown — otherwise assistant.jsx and app.jsx would
    silently fall through to their inline implementations."""
    text = _read("index.html")
    md_idx = text.find("markdown.js")
    assistant_idx = text.find("assistant.jsx")
    app_idx = text.find("app.jsx")
    assert md_idx > 0, "index.html must <script src=markdown.js>"
    assert md_idx < assistant_idx, "markdown.js must load before assistant.jsx"
    assert md_idx < app_idx, "markdown.js must load before app.jsx"


def test_assistant_uses_shared_helpers_when_present():
    """assistant.jsx must call NanoMarkdown.renderMath / .escapeHtml when
    available. Inline fallbacks remain so chat keeps working even if
    markdown.js fails to load — but the happy path goes through the shared
    layer for consistency with the Notes panel."""
    text = _read("assistant.jsx")
    assert "NanoMD" in text or "NanoMarkdown" in text, \
        "assistant.jsx must reference the shared NanoMarkdown helpers"
    assert "NanoMD.renderMath" in text or "NanoMarkdown.renderMath" in text, \
        "assistant.jsx must defer to shared renderMath when available"


def test_app_jsx_runs_katex_on_notes_preview():
    """The Notes preview ships a `<div ref={previewRef} dangerouslySetInnerHTML/>`
    — same dangerous-set + post-mount problem as the chat bubble. RealNotesView
    must call NanoMarkdown.renderMath after the html lands so $...$ / $$...$$
    render in Notes too (user feedback: notes side was leaving raw \\frac)."""
    text = _read("app.jsx")
    assert "NanoMarkdown.renderMath" in text, \
        "RealNotesView must call NanoMarkdown.renderMath on the preview ref"
    assert "renderMathThrottled" in text, \
        "RealNotesView must throttle KaTeX during streaming so partial chunks don't flicker"
    # markdownToHtml must stash math BEFORE markdown regexes (so $...$ survives)
    assert re.search(r"NanoMarkdown\.stashMath", text), \
        "markdownToHtml must use NanoMarkdown.stashMath to protect LaTeX from regex passes"


def test_render_markdown_collapses_orphan_math_paragraph():
    """Defensive: if the LLM still produces `<p>$p$</p><p>p：...</p>` despite
    the prompt rule, the renderer should merge them into a single paragraph
    so the visual gap closes. Pin the merge regex so it can't silently drift."""
    text = _read("assistant.jsx")
    # Look for the iterative merge pattern (regex + do/while loop)
    assert re.search(
        r"do\s*\{\s*prev\s*=\s*html;\s*html\s*=\s*html\.replace",
        text,
    ), "renderMarkdown must iteratively merge orphan-math <p> with next <p>"
    # And must NOT merge across math-display divs (would cross block boundary)
    assert "math-display" in text and re.search(
        r"\(\?!\\s\*<div class=\\?\"math-display",
        text,
    ), "merge must skip when next <p> starts with a math-display block"
