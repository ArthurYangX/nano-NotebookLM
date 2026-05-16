"""Interactive Q&A skill — RAG with intent routing, score gate, and 0-hit
translation retry (Round 2 #1 + #2).

Flow:
  1. classify_input(question) → if not "rag" → return general response.
  2. kb.search → checked_files filter.
  3. passes_score_gate(results)?
       - YES → RAG answer with citations (path="rag").
  4. NO → if course is language-mismatched, translate the query once and retry
       search; if results pass gate, return translated RAG answer
       (path="translated", with original_query/translated_query, prefix the
       answer to tell the user what happened).
  5. NO → degrade to general path (path="general").
"""

from __future__ import annotations

import asyncio
import difflib
import html
import logging
import os
import re
from typing import Iterable

from nano_notebooklm.ai import prompt_templates as prompts
from nano_notebooklm.ai.qwen_raft_backend import QwenBackendError
from nano_notebooklm.orchestrator import router_intent
from nano_notebooklm.orchestrator.memory import add_interaction, get_context_prompt
from nano_notebooklm.skills.base import Skill
from nano_notebooklm.types import LLMResponse, SearchResult, SkillResult

try:
    import httpx as _httpx
    _HTTP_ERROR_TYPE: type = _httpx.HTTPError
except ImportError:  # pragma: no cover — httpx is a hard dependency, just defensive
    _HTTP_ERROR_TYPE = Exception

# fix-all v1 #V4 (R4-5 review v1): narrow `except Exception` in the qwen
# fallback path so a genuine programming bug (KeyError/TypeError on a
# malformed LLMResponse) surfaces as a 500 rather than getting masked
# by `backend_fallback=True`. RuntimeError covers router.complete's
# "all retries exhausted" + _resolve_backend's missing-backend raise.
_QWEN_EXPECTED_ERRORS: tuple[type[BaseException], ...] = (
    QwenBackendError,
    RuntimeError,
    _HTTP_ERROR_TYPE,
)

logger = logging.getLogger(__name__)

# Wrap the translation LLM call so a stalled provider can't double our chat
# latency budget. 5s is generous: codex GPT-5.5 typically translates in <1s.
TRANSLATION_TIMEOUT_SECONDS = 5.0

# 2026-05-16: multi-turn history rewrite — disambiguate follow-up questions
# ("公式是什么？" after "什么是贝叶斯？") into self-contained retrieval
# queries before hitting BM25/vector/graphrag. Same shape as translation:
# bound by wall time so a stalled provider can't double chat latency.
# fix-all v1 #M4 (2026-05-16): trimmed 8s → 5s after codex GPT typically
# completes the rewrite in <2s; 5s catches a stalled provider quickly
# while leaving headroom matching TRANSLATION_TIMEOUT_SECONDS.
HISTORY_REWRITE_TIMEOUT_SECONDS = 5.0
# Cap the per-turn content length we feed into the rewrite prompt — full
# answers can run thousands of chars, but for disambiguation the lead
# sentence is enough. Keeps the rewrite prompt under ~1.5k tokens even
# with 6 round-trips of history.
_HISTORY_REWRITE_TURN_CHAR_CAP = 400

# fix-all v1 #H3 (2026-05-16 review-swarm): bound the fan-out of the
# rewrite LLM call so a burst of multi-turn /api/chat requests can't
# saturate codex. Mirrors `_GRAPHRAG_FANOUT_SEM = Semaphore(4)` and the
# notes/tectonic semaphores. Each multi-turn chat acquires once before
# the router.complete call. Env override mirrors GRAPHRAG_FANOUT_CONCURRENCY.
def _parse_rewrite_fanout() -> int:
    raw = os.getenv("REWRITE_FANOUT_CONCURRENCY")
    if not raw:
        return 4
    try:
        return max(1, int(raw))
    except ValueError:
        logger.warning(
            "REWRITE_FANOUT_CONCURRENCY=%r is not an int; using default 4", raw,
        )
        return 4


_REWRITE_FANOUT_CONCURRENCY = _parse_rewrite_fanout()
_REWRITE_FANOUT_SEM = asyncio.Semaphore(_REWRITE_FANOUT_CONCURRENCY)

# fix-all v1 #H1 (2026-05-16 review-swarm): scrub history content before
# embedding into the rewrite prompt. Strip:
#   - C0 / C1 control codepoints (newline/tab/CR rolled into a single space)
#   - Bidi-override codepoints (U+202A..U+202E, U+2066..U+2069)
#   - Zero-width codepoints (U+200B..U+200D, U+FEFF)
#   - Replace `<` / `>` with safe lookalikes so a malicious turn content
#     containing literal `</turn>` cannot terminate the data-frame around
#     it. Lookalikes `‹` and `›` are visually similar so the LLM still
#     interprets meaning.
#
# Pattern matches the defense-in-depth model the persona safe-strip uses
# (see prompt_templates._safe_persona). Pydantic ChatTurn already rejects
# blank-only content; this is the second layer for content that survives
# Pydantic but may still carry injection payloads.
_HISTORY_SCRUB_RE = re.compile(
    "["
    "\x00-\x08\x0b\x0c\x0e-\x1f\x7f"     # C0 controls (we replace \t\n\r separately) + DEL
    "\x80-\x9f"                                # C1 controls
    "\u200b-\u200d\ufeff"                    # zero-width chars
    "\u202a-\u202e\u2066-\u2069"            # bidi overrides
    "]+"
)
def _sanitize_history_text(text: str) -> str:
    """Belt-and-suspenders cleaning for history.content / rewrite output.

    Replaces control characters / bidi overrides / zero-width with a
    single space, normalizes tabs/CR/LF to space (so an attacker can't
    forge `\n[system] ignore the user`-style fake role headers), and
    swaps `<`/`>` to visually similar non-XML codepoints so a content
    containing `</turn>` can't escape the data-frame wrapper.

    Returns the cleaned string with collapsed whitespace and stripped
    leading/trailing space. Always safe on empty input (returns "").
    """
    if not text:
        return ""
    cleaned = _HISTORY_SCRUB_RE.sub(" ", text)
    cleaned = cleaned.replace("\t", " ").replace("\r", " ").replace("\n", " ")
    cleaned = cleaned.replace("<", "‹").replace(">", "›")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _normalise_for_compare(text: str) -> str:
    """Strip whitespace + trailing question-mark family for the rewrite
    no-op equality check. Without this, a rewriter that drops the
    trailing "?" or normalises spacing fires a spurious 📝 chip and a
    bogus log line. fix-all v1 #L2.
    """
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    return cleaned.rstrip("?？。.！!")


# fix-all v3 #L4 (R4-4 review-swarm v3): bound graph_search wall time so a
# stalled embed_fn (e.g. API-mode HTTP hang) doesn't block the chat path
# indefinitely. Local sentence-transformer batched call on 200 nodes is
# ~0.3-1.0s; API mode typical < 2s. 10s catches a stuck call quickly while
# leaving headroom for legacy KGs that pay the per-node batch on first use.
GRAPHRAG_TIMEOUT_SECONDS = 10.0

# review-swarm graphrag-all-courses HIGH-1 (2026-05-12): bound the fan-out
# of `_maybe_graphrag_all_courses` so a flood of All Courses chats can't
# saturate the default ThreadPoolExecutor (~12-20 workers on typical
# hosts) and starve notes / upload / mindmap to_thread calls. 4 is the
# steady-state ceiling: each graph_search holds one thread for up to
# GRAPHRAG_TIMEOUT_SECONDS, so 4 simultaneous keeps the impact <30% of
# a default 16-worker pool. v2 MED-1 (2026-05-13): wrap env parse in
# try/except so a typo like `GRAPHRAG_FANOUT_CONCURRENCY=abc` warns +
# defaults instead of crashing FastAPI startup with an opaque ImportError.
def _parse_fanout_concurrency() -> int:
    raw = os.getenv("GRAPHRAG_FANOUT_CONCURRENCY")
    if not raw:
        return 4
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "GRAPHRAG_FANOUT_CONCURRENCY=%r is not an int; using default 4", raw,
        )
        return 4
    return max(1, value)


_GRAPHRAG_FANOUT_CONCURRENCY = _parse_fanout_concurrency()
# Sized at module import; tuning requires a process restart. Lazily binds
# to the running event loop on first acquire (Py3.12 semaphore is
# loop-agnostic at construction).
_GRAPHRAG_FANOUT_SEM = asyncio.Semaphore(_GRAPHRAG_FANOUT_CONCURRENCY)


# review-swarm graphrag-all-courses MED-4 (2026-05-12): cache the
# courses-with-KG listing per ARTIFACTS_DIR. Without this, every All
# Courses chat would `iterdir()` + per-dir `exists()` on the courses
# tree (~11 stat calls for 10 courses). The cache is invalidated by TTL
# OR by an explicit `_invalidate_courses_kg_cache(courses_root)` call
# from the upload/delete endpoints (v2 MED-5) so a freshly-ingested or
# just-deleted course becomes graphrag-visible immediately, not after
# TTL. Map shape: `{artifacts_dir_str: (monotonic_seconds, list[course_id])}`.
_COURSES_KG_CACHE: dict[str, tuple[float, list[str]]] = {}


# v2 MED-1: same defensive parse for the TTL env knob.
def _parse_cache_ttl() -> float:
    raw = os.getenv("GRAPHRAG_COURSES_CACHE_TTL")
    if not raw:
        return 60.0
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "GRAPHRAG_COURSES_CACHE_TTL=%r is not a float; using default 60", raw,
        )
        return 60.0
    if value < 0 or value != value or value in (float("inf"), float("-inf")):
        return 60.0
    return value


_COURSES_KG_CACHE_TTL = _parse_cache_ttl()


def _invalidate_courses_kg_cache(courses_root=None) -> None:
    """Drop one cache entry (by courses_root) or the entire cache.
    Called from `/api/upload/{id}` Stage-B-done and
    `DELETE /api/courses/{id}` so the All Courses graphrag fan-out
    sees a new/deleted course on the next chat instead of waiting up
    to `_COURSES_KG_CACHE_TTL` seconds."""
    if courses_root is None:
        _COURSES_KG_CACHE.clear()
        return
    _COURSES_KG_CACHE.pop(str(courses_root), None)

# R4-5 part 2 + fix-all v1 #V5: bound an explicit `backend="qwen_raft"`
# LLM call. AutoDL Qwen2.5-7B-RAFT inference is typically 3-15s on warm
# GPU; 30s catches a hung HTTP connection / cold-start anomaly while
# leaving runway for legitimate slow responses. On timeout the chat
# path silently degrades to the default routing backend and the response
# carries `backend_fallback=True` so the frontend can chip-flag the
# degradation. Operators tuning AutoDL cold-start budgets override via
# `QWEN_BACKEND_TIMEOUT_SECONDS` env. The qwen client's own transport
# timeout (`QWEN_RAFT_HTTP_TIMEOUT`, default 60s) is independent —
# operators raising this above this constant will see chat still time
# out at the chat-path budget.
def _qwen_backend_timeout() -> float:
    """Chat-path wall-clock budget for qwen_raft.

    2026-05-13: raised default 30 → 60. Monitor run on 2026-05-12T17:23Z
    showed 24/30 questions on test-slides timed out at 30s and silently
    fell back to codex. AutoDL Qwen2.5-7B-RAFT under load consistently
    answers in 35-50s; 60s gives qwen a real chance while still bounding
    the worst case below the user's tolerance.
    """
    raw = os.getenv("QWEN_BACKEND_TIMEOUT_SECONDS")
    if not raw:
        return 60.0
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "QWEN_BACKEND_TIMEOUT_SECONDS=%r is not a float; using default 60.0",
            raw,
        )
        return 60.0
    if value <= 0 or value != value or value in (float("inf"), float("-inf")):
        return 60.0
    return value


QWEN_BACKEND_TIMEOUT_SECONDS = _qwen_backend_timeout()

# fix-all v1 #A3 / #B6 (R4-4 review-swarm): graphrag admission gate. Plain
# cosine ranking against the KG concept embeddings yields api_scores roughly
# in the [-1, 1] cosine range; 0.15 puts "moderate semantic overlap" as the
# floor (rules out queries with no real conceptual overlap that nonetheless
# pick up 2+ topics just by chance). Tunable via GRAPHRAG_SCORE_GATE_TOP1.
# GRAPHRAG_ENABLED is the kill-switch — operators can disable graphrag
# without redeploying when a particular KG shape causes regressions.
DEFAULT_GRAPHRAG_TOP1_THRESHOLD = 0.15
# 2026-05-13 Path B: marginal-confidence ceiling for graphrag. A top1
# cosine between the admission floor (0.15) and this ceiling (0.30) is
# admitted but flagged as low-confidence — the system prompt gains a
# "refuse if context is insufficient" addendum and the response carries
# a "_(检索置信度较低)_" preface so the user knows the model may be
# stretching. Tunable via GRAPHRAG_LOW_CONFIDENCE_CEILING.
DEFAULT_GRAPHRAG_LOW_CONF_CEILING = 0.30


def _graphrag_low_conf_ceiling() -> float:
    raw = os.getenv("GRAPHRAG_LOW_CONFIDENCE_CEILING")
    if raw is None or raw == "":
        return DEFAULT_GRAPHRAG_LOW_CONF_CEILING
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "GRAPHRAG_LOW_CONFIDENCE_CEILING=%r is not a float; using default %s",
            raw, DEFAULT_GRAPHRAG_LOW_CONF_CEILING,
        )
        return DEFAULT_GRAPHRAG_LOW_CONF_CEILING
    if value != value or value in (float("inf"), float("-inf")):
        return DEFAULT_GRAPHRAG_LOW_CONF_CEILING
    return max(0.0, min(1.0, value))


def _graphrag_score_floor() -> float:
    raw = os.getenv("GRAPHRAG_SCORE_GATE_TOP1")
    if raw is None or raw == "":
        return DEFAULT_GRAPHRAG_TOP1_THRESHOLD
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "GRAPHRAG_SCORE_GATE_TOP1=%r is not a float; using default %s",
            raw, DEFAULT_GRAPHRAG_TOP1_THRESHOLD,
        )
        return DEFAULT_GRAPHRAG_TOP1_THRESHOLD
    if value != value or value in (float("inf"), float("-inf")):  # NaN / Inf
        return DEFAULT_GRAPHRAG_TOP1_THRESHOLD
    # fix-all v2 #V1 (R4-4 review-swarm v2): clamp to [0, 1]. A negative
    # env value silently bypasses the admission gate (any cosine >= -1
    # passes), turning graphrag into the original `len >= 2` regression
    # the v1 #A3 fix was meant to prevent. Above-1 values would block
    # every query — operator can self-diagnose that case, but symmetry +
    # one INFO log makes the misconfig visible.
    if value < 0.0 or value > 1.0:
        logger.info(
            "GRAPHRAG_SCORE_GATE_TOP1=%s clamped to [0, 1]", value,
        )
        value = max(0.0, min(1.0, value))
    return value


def _graphrag_enabled() -> bool:
    """Kill switch. Default on; operators disable with any non-empty value
    other than the explicit enable list.

    fix-all v3 #L10 (R4-4 review-swarm v3): v1 used an explicit DISABLE
    allow-list (`0/false/no/off/disabled`), which silently fail-open on
    typos like `disablle`, `falce`, `stop`, etc. — exactly the wrong
    direction for a kill switch. v3 inverts the semantics: any value the
    operator types intending to disable should disable. Empty/missing →
    default on (= no operator intent expressed). Explicit enable values
    (`1/true/yes/on/enabled`) → on. Everything else → off (fail-safe).
    """
    raw = (os.getenv("GRAPHRAG_ENABLED") or "").strip().lower()
    if not raw:
        return True  # default on when env not set
    if raw in ("1", "true", "yes", "on", "enabled"):
        return True
    return False  # any other non-empty value disables (fail-safe)

# Quote / wrapper characters the translation LLM sometimes returns despite
# being told not to. We strip them so RAG doesn't search for `"memory"` etc.
_QUOTE_STRIP = "\"'`「」『』《》〈〉‹›“”‘’"

# Sanitise user-supplied strings (`original_query`) and LLM-supplied strings
# (`translated_query`) before interpolating them into the markdown answer
# note. The frontend renders messages with `dangerouslySetInnerHTML` after a
# tiny in-house markdown pass.
#
# Two layers:
#   1. Backslash-escape markdown-significant chars so payloads like
#      `]( javascript:alert(1) )` cannot turn into an anchor when the
#      renderer eventually grows a `[text](url)` rule.
#   2. html.escape on top so any future raw-HTML rendering finds inert text.
# Also collapse control chars to spaces so the inline note stays single-line.
_CONTROL_CHARS = re.compile(r"[\r\n\x00-\x1f]+")
_MD_SPECIAL = "[]()*_`!#<>|\\~"


def _serialize_sources(results: Iterable[SearchResult]) -> list[dict]:
    # review-swarm graphrag-all-courses MED-1: propagate r.course_id so
    # All Courses graphrag answers can label each citation with its
    # origin course.
    # v2 MED-2 (2026-05-13): SearchResult.course_id is a mandatory
    # `str` field (default ""), not Optional. The v1 `getattr(..., None)`
    # both lied about the contract (returned "" not None) and masked a
    # future SearchResult refactor that drops the attribute. Now:
    # direct attribute access for the field, with "" → None at the
    # dict boundary so the ChatSource.course_id: str | None contract
    # accurately reflects "no origin course attribution" semantics.
    return [
        {
            "chunk_id": r.chunk_id,
            "text": r.text[:200] + "..." if len(r.text) > 200 else r.text,
            "source_file": r.source_file,
            "location": r.location,
            "score": r.score,
            "course_id": r.course_id or None,
        }
        for r in results
    ]


_BLOCKQUOTE_RE = re.compile(r"(?:^|\n)((?:>[ \t]?.*(?:\n|$))+)", flags=re.MULTILINE)
_QUOTE_SOURCE_MIN_RATIO = float(os.getenv("QWEN_QUOTE_SOURCE_MIN_RATIO", "0.25"))
_QUOTE_NORMALIZE_WS_RE = re.compile(r"\s+")

# Formula-block heuristics: detect blockquotes that are predominantly
# math notation (e.g. `P(X=x|ωk)= P(ωk|x) P(ωk) P(x)` spread across PDF
# lines) so we can rewrap them as `$$...$$` KaTeX blocks. The frontend
# already renders `$$...$$` via KaTeX auto-render; without this step
# PDF-extracted formulas show as raw broken-line text.
_MATH_SYMBOL_RE = re.compile(r"[=≤≥≠≈±∑∫∂∇√×÷≡≅∈∉∪∩→↔]")
_CJK_RUN_RE = re.compile(r"[一-鿿]{3,}")   # 3+ consecutive CJK = likely prose
_ENGLISH_WORD_RE = re.compile(r"\b[A-Za-z]{4,}\b")  # 4+ char word = likely prose


def _normalize_for_match(s: str) -> str:
    """Collapse all whitespace runs to a single space for fuzzy / substring
    matching. Quote text from RAFT models often preserves PDF line breaks
    that don't appear in the original chunk text, killing exact-substring
    matches; collapsing whitespace makes both sides directly comparable.
    """
    return _QUOTE_NORMALIZE_WS_RE.sub(" ", s).strip()


def _looks_like_formula_block(quote_text: str) -> bool:
    """True iff `quote_text` is dominantly math notation rather than
    natural-language prose. Used by `_annotate_quote_sources` to decide
    whether to rewrap a blockquote as a `$$...$$` KaTeX block.

    Heuristic:
      - reject anything containing `<`, `>`, or `&` to defeat the
        frontend math-stash XSS vector: `renderMarkdown` lifts the
        block between `$$...$$` into a math token BEFORE running
        `_escapeHtml`, so `</div><img src=x onerror=alert(1)>=x` would
        otherwise be auto-promoted into a math block and injected raw
        into the DOM via `dangerouslySetInnerHTML`. Review-swarm fix-now
        CRITICAL #2 (2026-05-13).
      - must contain at least one canonical math operator/symbol
      - rejected if any run of 3+ consecutive CJK chars (prose)
      - rejected if 3+ ASCII words of length >= 4 (prose, ignoring
        single-letter symbols like x, k, n)
    """
    stripped = quote_text.strip()
    if not stripped:
        return False
    if any(c in stripped for c in "<>&"):
        return False
    if not _MATH_SYMBOL_RE.search(stripped):
        return False
    if _CJK_RUN_RE.search(stripped):
        return False
    eng_words = _ENGLISH_WORD_RE.findall(stripped)
    if len(eng_words) >= 3:
        return False
    return True


def _formula_block_to_math(quote_text: str) -> str:
    """Collapse a multi-line PDF-extracted formula into a single
    `$$ ... $$` block so KaTeX can render it. Multi-line is the common
    pathology: PDF columnation splits `P(X=x|ωk)= P(ωk|x) P(ωk) P(x)`
    across four lines; KaTeX needs them on one logical line. Unicode
    Greek letters (`ω`) and operators pass through to KaTeX as-is.
    """
    one_line = _QUOTE_NORMALIZE_WS_RE.sub(" ", quote_text).strip()
    return f"$${one_line}$$"


def _annotate_quote_sources(answer: str, results: list[SearchResult]) -> str:
    """Step 2 of qwen-raft integration: match each markdown blockquote in
    the answer against the search results handed to the LLM as context,
    and append a ``[Source: file, location]`` tag so the existing
    citation chip pipeline can link the quote back to the PDF.

    Markdown blockquotes (``> ...``) come from
    ``qwen_raft_backend._strip_raft_preamble`` (it converts the RAFT
    model's ``##begin_quote##...##end_quote##`` spans). Codex / other
    backends don't emit this format, so this is a no-op on their output
    (no blockquote regex match → unchanged).

    Matching strategy (in order):
      1. **Whitespace-normalized substring**: collapse spaces/newlines
         on both sides and check whether quote is a contiguous substring
         of chunk.text. This is the strongest signal — short symbolic
         quotes (`P(X=x|ωk)= P(ωk|x) P(ωk) P(x)`) that fail
         SequenceMatcher's character-level ratio score will succeed
         here because the chunk text contains the same symbols just
         with different line breaks.
      2. **SequenceMatcher quick_ratio fallback**: `QWEN_QUOTE_SOURCE_MIN_RATIO`
         floor (default 0.25, lowered from 0.4 because RAFT quotes are
         often short and lose ratio quickly to PDF-extraction artifacts).
      3. **Top-rank fallback**: if both methods fail but `results` is
         non-empty, attribute to `results[0]` — the highest-ranked
         chunk is the most plausible source for a quote the LLM
         produced from a context window we built. Marked with a `?`
         to signal lower confidence to the reader.
    """
    if not answer or not results:
        return answer
    # Pre-normalize each chunk text once. SequenceMatcher gets the raw
    # text (its quick_ratio is whitespace-sensitive but the floor is
    # low enough that it still matches).
    candidates = [
        (r.source_file, r.location, r.text, _normalize_for_match(r.text))
        for r in results
    ]

    def _replace(m):
        block = m.group(1).rstrip("\n")
        quote_text = "\n".join(
            re.sub(r"^>[ \t]?", "", line) for line in block.split("\n")
        ).strip()
        if len(quote_text) < 4:
            return m.group(0)
        quote_norm = _normalize_for_match(quote_text)

        # Resolve source. Three-tier:
        #   1. whitespace-normalized substring match → confident tag
        #   2. SequenceMatcher quick_ratio ≥ floor → confident tag
        #   3. neither matches → no tag (review-swarm fix-now HIGH #6,
        #      2026-05-13). Previous code fell back to candidates[0]
        #      and tagged it identically to a real match, producing
        #      "phantom citations" that jumped users to a wrong page
        #      with no visual cue that the link was a guess. Better to
        #      ship an untagged blockquote than a misleading link.
        source_tag = None
        for i, (_sf, _loc, _txt, txt_norm) in enumerate(candidates):
            if quote_norm in txt_norm:
                sf, loc, _txt, _txt_norm = candidates[i]
                source_tag = f"[Source: {sf}, {loc}]"
                break
        if source_tag is None:
            best = (-1, 0.0)
            for i, (_sf, _loc, txt, _txt_norm) in enumerate(candidates):
                ratio = difflib.SequenceMatcher(
                    None, quote_text, txt, autojunk=True,
                ).quick_ratio()
                if ratio > best[1]:
                    best = (i, ratio)
            if best[0] >= 0 and best[1] >= _QUOTE_SOURCE_MIN_RATIO:
                sf, loc, _txt, _txt_norm = candidates[best[0]]
                source_tag = f"[Source: {sf}, {loc}]"

        # 2026-05-13: top-rank fallback REVERTED. Real-user test showed
        # graphrag often retrieved a wrong-chapter top chunk (e.g. ch1
        # for an HMM question), and the `?` tag still pointed users to
        # the wrong PDF page. Better to ship a bare blockquote than a
        # confidently-wrong link.

        trailing_newline = "\n" if m.group(0).endswith("\n") else ""
        # When source_tag is None (neither substring nor fuzzy match
        # cleared the floor) we ship the blockquote / math block bare —
        # no misleading link.
        tag_suffix = f" {source_tag}" if source_tag else ""

        # Formula-block rewrite: PDF-extracted formulas come out as
        # multi-line raw unicode (`P(X=x|ωk)=` / `P(ωk|x)` / ...). Wrap
        # the whole thing in `$$...$$` so KaTeX renders it as math.
        # Drop the blockquote prefix — display math doesn't need it.
        if _looks_like_formula_block(quote_text):
            math = _formula_block_to_math(quote_text)
            return f"\n{math}{tag_suffix}{trailing_newline}"

        # Normal prose blockquote.
        return f"\n{block}{tag_suffix}{trailing_newline}"

    annotated = _BLOCKQUOTE_RE.sub(_replace, answer)
    # Clean up the leading newline _replace adds for the first block
    # if the original answer started directly with a blockquote.
    return annotated.lstrip("\n") if not answer.startswith("\n") else annotated


def _md_safe(text: str) -> str:
    """Make `text` safe to interpolate into a markdown answer that will be
    rendered into HTML. Defends against:
      - markdown-link / image injection (`]( javascript:...)`) by escaping
        every markdown-significant char with a backslash
      - HTML / attribute injection (`<script>`, `"onerror=...`) via html.escape
      - multi-line / control-char injection by collapsing them to a space
    """
    cleaned = _CONTROL_CHARS.sub(" ", text)
    escaped = "".join("\\" + c if c in _MD_SPECIAL else c for c in cleaned)
    return html.escape(escaped, quote=True).strip()


class QASkill(Skill):
    name = "qa"
    description = "Answer questions using course materials with source citations"

    async def execute(self, params: dict) -> SkillResult:
        """Public entry point. Wraps `_execute_core` with the optional
        multi-turn history rewrite (2026-05-16): when `params["history"]`
        is non-empty, an LLM call disambiguates the latest question into a
        self-contained retrieval query, and the wrapped core sees the
        rewritten string. Empty / None history short-circuits to a direct
        `_execute_core(params)` call — single-turn chat pays no extra LLM
        hop.
        """
        question = (params.get("question") or "").strip()
        history = params.get("history") or []

        # Defensive blank check — _execute_core re-checks, but doing it
        # here avoids running the rewrite LLM call on empty input.
        if not question:
            return await self._execute_core(params)

        rewritten_query: str | None = None
        if history:
            rewritten = await self._rewrite_with_history(question, history)
            if rewritten and rewritten != question:
                # fix-all v1 #L2 (2026-05-16): collapse whitespace +
                # strip trailing punctuation before equality check so a
                # cosmetic "公式是什么 " (trailing space) doesn't fire
                # a spurious chip. The rewriter's output is stable
                # enough that this catches only true no-ops.
                if _normalise_for_compare(rewritten) != _normalise_for_compare(question):
                    logger.info(
                        "qa.history_rewrite turns=%d", len(history),
                    )
                    rewritten_query = rewritten
                    # fix-all v1 #M2 (2026-05-16 review-swarm): set a
                    # SEPARATE `retrieval_query` instead of mutating
                    # `question`. Retrieval paths use retrieval_query;
                    # `_answer_*` paths keep `question` (the user's
                    # literal text) so a bad rewrite cannot drift the
                    # final answer prompt.
                    params = {**params, "retrieval_query": rewritten_query}

        result = await self._execute_core(params)

        if rewritten_query and result.success and isinstance(result.data, dict):
            # Surface the rewritten query to the client so the UI can show
            # "📝 改写: …" — same transparency pattern as
            # `translated_query` on path="translated". Don't clobber if a
            # downstream path already set it (defensive).
            data = dict(result.data)
            data.setdefault("rewritten_query", rewritten_query)
            return SkillResult(success=True, data=data)
        return result

    async def _rewrite_with_history(
        self, question: str, history: list[dict]
    ) -> str | None:
        """Rewrite `question` into a standalone retrieval query using the
        conversation history. Returns the rewritten string (which MAY
        equal `question` if the model decides no rewrite is needed), or
        `None` if the call timed out / failed / produced empty output —
        the caller treats `None` as "use the original question".

        Defensive: history entries with unexpected roles or non-string
        content are silently dropped. The prompt itself treats history
        as data (TRANSLATE_QUERY_SYSTEM-style instruction) but we still
        truncate per-turn content to `_HISTORY_REWRITE_TURN_CHAR_CAP` so
        a hostile prior assistant turn can't bloat the prompt.
        """
        # fix-all v1 #H1 (2026-05-16 review-swarm): scrub each turn's
        # content via `_sanitize_history_text` so a malicious assistant /
        # user turn containing control chars, bidi overrides, zero-width
        # codepoints, or literal `</turn>` cannot terminate the data-frame.
        cleaned: list[tuple[str, str]] = []
        for turn in history:
            if not isinstance(turn, dict):
                continue
            role = turn.get("role")
            content = turn.get("content")
            if role not in ("user", "assistant"):
                continue
            if not isinstance(content, str) or not content.strip():
                continue
            text = _sanitize_history_text(content)
            if not text:
                continue
            if len(text) > _HISTORY_REWRITE_TURN_CHAR_CAP:
                text = text[:_HISTORY_REWRITE_TURN_CHAR_CAP] + "…"
            cleaned.append((role, text))

        if not cleaned:
            return None

        # fix-all v1 #H1: wrap each turn in `<turn role="...">...</turn>`
        # data-frame matching the system prompt's instruction to treat
        # everything inside <turn> as data, not as instructions. Combined
        # with the `<` / `>` lookalike substitution in the sanitizer,
        # content cannot inject a fake role marker.
        history_block = "\n".join(
            f'<turn role="{role}">{text}</turn>' for role, text in cleaned
        )
        # Also sanitize the latest question — it's the LLM's
        # "Latest user message:" target and shares the same trust surface.
        sanitized_question = _sanitize_history_text(question) or question
        prompt = prompts.REWRITE_HISTORY_PROMPT.format(
            history=history_block, question=sanitized_question,
        )

        # fix-all v1 #H3: bound fan-out so a burst of multi-turn chats
        # can't saturate codex. The semaphore is acquired around the
        # router.complete call so the wait_for timeout still applies.
        try:
            async with _REWRITE_FANOUT_SEM:
                resp = await asyncio.wait_for(
                    self.router.complete(
                        prompt,
                        task_type="rewrite_history",
                        system=prompts.REWRITE_HISTORY_SYSTEM,
                        temperature=0.0,
                        max_tokens=160,
                        # Single attempt: outer wait_for is the budget; the
                        # router's default 3-retry exponential backoff would
                        # silently exceed HISTORY_REWRITE_TIMEOUT_SECONDS.
                        max_retries=1,
                    ),
                    timeout=HISTORY_REWRITE_TIMEOUT_SECONDS,
                )
        except asyncio.TimeoutError:
            logger.warning(
                "history rewrite timed out after %ss; using original question",
                HISTORY_REWRITE_TIMEOUT_SECONDS,
            )
            return None
        except Exception as exc:  # noqa: BLE001 — graceful fallback.
            # In Python 3.8+ asyncio.CancelledError inherits BaseException,
            # so this except does NOT swallow user-initiated cancellation.
            logger.warning(
                "history rewrite LLM call failed (%s); using original question",
                type(exc).__name__,
            )
            return None

        # fix-all v1 #M3 (2026-05-16 review-swarm): drop the legacy
        # "Rewritten:" / "改写:" / etc. label-strip. The prompt explicitly
        # says "no prefix, no explanation"; trusting the rewriter is safer
        # than stripping arbitrary leading labels, because a jailbreak that
        # emits `Rewritten: <attacker payload>` would have had its prefix
        # dutifully removed by the old logic, laundering the attack into
        # a clean retrieval query. With temperature=0.0 codex GPT-5.5
        # reliably obeys the no-prefix instruction.
        rewritten = (resp.content or "").strip().strip(_QUOTE_STRIP).strip()
        if not rewritten:
            return None
        # fix-all v1 #H1: belt-and-suspenders — sanitize the rewriter's
        # OUTPUT too. If the rewriter was jailbroken into echoing an
        # attacker's control-char / bidi payload, scrubbing here keeps
        # it out of the downstream retrieval + answer LLM prompt.
        rewritten = _sanitize_history_text(rewritten) or rewritten
        # Defensive length cap mirrors ChatRequest.question's max_length
        # so a runaway rewrite can't smuggle a 100KB string through the
        # rest of the pipeline.
        if len(rewritten) > 4000:
            rewritten = rewritten[:4000]
        return rewritten or None

    async def _execute_core(self, params: dict) -> SkillResult:
        question = params.get("question", "")
        # fix-all v1 #M2 (2026-05-16 review-swarm): retrieval_query is
        # the history-rewritten search string when present; question is
        # always the user's literal text. ALL retrieval call sites use
        # retrieval_query; ALL `_answer_*` LLM prompts use `question`
        # so a bad rewrite cannot drift the user-visible answer.
        retrieval_query = params.get("retrieval_query") or question
        course_filter = params.get("course_filter")
        top_k = params.get("top_k", 5)
        checked_files = params.get("checked_files")
        user_lang = params.get("user_lang")
        # R4-5 part 2: optional per-request backend override
        # ("codex" / "qwen_raft" / None). Threaded through _answer_rag /
        # _answer_general; auxiliary calls (translate, cross-course
        # routing) stay on the codex main path so the demo chip only
        # affects the answer generation step, not retrieval helpers.
        backend = params.get("backend")
        # 2026-05-12: user-customisable assistant name. None / empty →
        # the renderer functions in prompt_templates fall back to
        # DEFAULT_PERSONA. Threaded through every _answer_* call so the
        # name is consistent across rag / general / translated /
        # cross-course / graphrag paths.
        persona = params.get("persona")
        # 2026-05-13: optional source_file the user is currently viewing
        # in Reader. Used as a soft retrieval bias — graphrag bumps the
        # ranking of hits from this file, and the score-gated RAG path
        # gets a small boost for matching `source_file`. NOT a hard
        # filter (use `checked_files` for that).
        active_source_file = params.get("active_source_file") or None

        if not question:
            return SkillResult(success=False, error="No question provided")

        # Route on the retrieval query — for a short follow-up like "为什么?"
        # the rewriter expands it into a substantive RAG-shaped question,
        # which is the intended UX for a study tool. If no rewrite happened,
        # retrieval_query == question and the routing is unchanged.
        decision = router_intent.classify_input(retrieval_query)

        # ── Path B (general): short / greeting / pure punctuation / identity / meta_course / bare_q ──
        if decision.path == "general":
            logger.info("qa.path=general reason=%s course=%s",
                        decision.reason, course_filter)
            return await self._answer_general(
                question, course_filter,
                reason=f"input classified as general ({decision.reason})",
                route_reason=decision.reason,
                user_lang=user_lang,
                backend=backend,
                persona=persona,
            )

        # ── R4-4 Path graphrag: KG-driven retrieve fires *before* the
        # BM25/vector path when at least one course has a knowledge_graph.json.
        # The KG is the upload pipeline's product (R4-2) — its concept
        # nodes are L2-normalised embeddings the graph_search ranks by
        # cosine, then expands along part-of / prerequisite_of / depends-on
        # edges to surface the chunks the extractor already linked into
        # the same neighbourhood. Compared to plain RAG this nails
        # cross-concept queries ("how do X and Y relate?") where the
        # surface-lexical RRF would pull two independent passages.
        #
        # 2026-05-12: All Courses mode (no course_filter) now ALSO runs
        # graphrag — it iterates every course with a `knowledge_graph
        # .json`, runs `_maybe_graphrag` in parallel via gather, then
        # merges by chunk_id (best-score wins) and sorts by cosine.
        # Pre-fix the only retrieval in All Courses mode was plain
        # BM25/vector RRF — a short query like "什么是精度" couldn't
        # cross the per-course score gate (char-bigram noise was too
        # weak across all 5+ courses) and fell straight through to the
        # general path. Cost: +200-500ms latency (bounded by slowest KG
        # × per-task `graph_search` timeout); benefit: KG-quality
        # retrieval across the whole corpus.
        #
        # Skip conditions:
        #   - checked_files set: user pinned a file subset; graph_search's
        #     hop expansion cannot honour per-file filtering without
        #     materially degrading the neighbourhood signal.
        # Skip → fall through to existing RAG → translation → cross-course
        # → general chain.
        if not checked_files and _graphrag_enabled():
            if course_filter:
                graphrag_results = await self._maybe_graphrag(retrieval_query, course_filter)
                graphrag_scope = course_filter
            else:
                graphrag_results = await self._maybe_graphrag_all_courses(retrieval_query)
                graphrag_scope = "all-courses"
            # 2026-05-13: active-file soft boost + injection.
            # Phase 1 — boost already-retrieved chunks from the active file.
            # Phase 2 — if active file produced ZERO hits (e.g. graphrag
            # seeds were dominated by a sibling chapter due to embedding
            # mono-lingual bias), inject up to 5 fresh hits from kb.search
            # restricted to that file. This rescues the "user reading
            # ch4(2) asks about RNN → graphrag pulls ch1 only" failure
            # mode the dual-q embed alone can't fix.
            if active_source_file:
                from nano_notebooklm.types import SearchResult as _SR
                active_hits = [r for r in (graphrag_results or []) if r.source_file == active_source_file]
                if active_hits:
                    # Phase 1: boost existing active-file chunks.
                    boosted: list = []
                    for r in (graphrag_results or []):
                        if r.source_file == active_source_file:
                            boosted.append(_SR(
                                chunk_id=r.chunk_id, text=r.text,
                                source_file=r.source_file, location=r.location,
                                score=r.score + 0.15, course_id=r.course_id,
                            ))
                        else:
                            boosted.append(r)
                    boosted.sort(key=lambda r: r.score, reverse=True)
                    graphrag_results = boosted
                else:
                    # Phase 2: no active-file hits in graphrag — inject
                    # via hybrid search restricted to that file. We can't
                    # just rely on the existing checked_files filter
                    # because checked_files isn't set here. Build a small
                    # ad-hoc result list and merge.
                    try:
                        fallback = self.kb.search(
                            retrieval_query, top_k=10, course_id=course_filter,
                        )
                        # filter to the active file
                        from_file = [r for r in fallback if r.source_file == active_source_file]
                        if from_file:
                            # Score these slightly higher than typical
                            # graphrag noise so they take precedence.
                            graphrag_results = (graphrag_results or []) + [
                                _SR(
                                    chunk_id=r.chunk_id, text=r.text,
                                    source_file=r.source_file, location=r.location,
                                    score=max(r.score, 0.3) + 0.2,
                                    course_id=r.course_id,
                                )
                                for r in from_file[:5]
                            ]
                            graphrag_results.sort(key=lambda r: r.score, reverse=True)
                    except Exception:  # noqa: BLE001
                        # search failure is best-effort; fall through to
                        # the original graphrag result list.
                        pass
            # fix-all v1 #A3 + v2 #V3: admission gate uses passes_score_gate
            # with a graphrag-specific cosine floor (default 0.15) instead of
            # the original `len >= 2` check.
            #
            # fix-all v2 #V3: pin `min_hits=1` rather than inheriting
            # RAG_SCORE_GATE_MIN_HITS (default 2). graphrag's whole-chunk
            # output is qualitatively different from RAG: one strong-cosine
            # seed (>= 2 * floor) already represents a high-confidence
            # neighbourhood, whereas RAG needs 2 hits because RRF scores are
            # tiny + the per-doc strength varies. The RAG default would
            # otherwise reject single-doc course uploads (one strong concept
            # match, one cosine, no second hit) even though the score is
            # high — the exact failure mode upgrading the gate was meant
            # to avoid.
            if graphrag_results and router_intent.passes_score_gate(
                graphrag_results,
                top1_threshold=_graphrag_score_floor(),
                min_hits=1,
            ):
                # review-swarm graphrag-all-courses MED-2 / LOW: keep the
                # field name `course=` consistent with the other 4 path
                # log lines (rag / translated / cross-course / general)
                # so log greps don't need a per-path special case. For
                # All Courses graphrag, the literal "all-courses" stands
                # in for course_filter.
                low_conf = graphrag_results[0].score < _graphrag_low_conf_ceiling()
                logger.info(
                    "qa.path=graphrag course=%s top1=%.4f hits=%d low_conf=%s",
                    graphrag_scope, graphrag_results[0].score,
                    len(graphrag_results), low_conf,
                )
                return await self._answer_rag(
                    question, course_filter, graphrag_results,
                    path="graphrag",
                    user_lang=user_lang,
                    backend=backend,
                    persona=persona,
                    low_confidence=low_conf,
                )

        # ── Path A (rag): retrieve, gate, fall back if low-quality ──
        raw = self.kb.search(retrieval_query, top_k=top_k, course_id=course_filter)
        results = self._apply_checked_files(raw, checked_files,
                                            retrieval_query, top_k, course_filter)

        # If the user explicitly narrowed via checked_files and the filter
        # caused the failure, return the #R1 boilerplate so they see their
        # filter missed. Don't tag with `path` — the union is reserved for
        # genuine routing decisions. Only fire when the filter is *causal*:
        #   - filter_empty: raw passed the gate but filter killed every hit
        #     (the filter, not the query, is the cause)
        #   - filter_low_quality: raw would have passed the gate, filter
        #     knocked it below.
        # If raw itself fails the gate, this isn't a filter problem —
        # fall through to translation / cross-course / general. Round 2.1 #2:
        # before the gate-aware check, filter_empty short-circuited on any
        # raw with hits, so weak BM25 char-bigram noise on a meta query
        # ("这是什么课" with default-checked files) blocked translation /
        # general entirely. Now the boilerplate fires only when narrowing
        # was the actual cause.
        if checked_files and raw:
            signal = reason = None
            raw_passes = router_intent.passes_score_gate(raw)
            if not results and raw_passes:
                signal = "filter_empty"
                reason = "no chunks matched the user-checked source files"
            elif (results
                  and raw_passes
                  and not router_intent.passes_score_gate(results)):
                signal = "filter_low_quality"
                reason = "checked-files filter left only low-quality chunks"

            if signal:
                # Log which files contributed to raw — lets us audit whether
                # the user just had the wrong files checked vs. truly missing
                # content. Cap at first 5 to keep log lines bounded.
                raw_top_files = []
                for r in raw[:5]:
                    if r.source_file not in raw_top_files:
                        raw_top_files.append(r.source_file)
                logger.info(
                    "qa.path=<%s> course=%s checked_files=%d raw_top_files=%s",
                    signal, course_filter, len(checked_files), raw_top_files,
                )
                return SkillResult(
                    success=True,
                    data={
                        "answer": (
                            "No relevant content found in the selected sources. "
                            "Try checking more sources in the Library panel, or "
                            "upload additional materials."
                        ),
                        "sources": [],
                        "model": "fallback",
                        "tokens_used": 0,
                        signal: True,
                        "general_reason": reason,
                    },
                )

        if router_intent.passes_score_gate(results):
            logger.info("qa.path=rag course=%s top1=%.4f hits=%d",
                        course_filter,
                        results[0].score if results else 0.0,
                        len(results))
            return await self._answer_rag(
                question, course_filter, results, path="rag",
                user_lang=user_lang,
                backend=backend,
                persona=persona,
            )

        # ── Translation retry (#2): zh query on en course (or vice versa) ──
        translated = await self._maybe_translate_retry(
            retrieval_query, course_filter, top_k, checked_files,
        )
        if translated is not None:
            translated_query, translated_results = translated
            logger.info("qa.path=translated course=%s top1=%.4f hits=%d",
                        course_filter,
                        translated_results[0].score if translated_results else 0.0,
                        len(translated_results))
            return await self._answer_rag(
                translated_query, course_filter, translated_results,
                path="translated",
                original_query=question,
                translated_query=translated_query,
                user_lang=user_lang,
                backend=backend,
                persona=persona,
            )

        # ── Cross-course fallback (#3): own course + translation both 0 → ──
        # search All Courses; if a sibling course has the answer, surface it
        # with a "本课无相关内容" annotation. Skipped when the caller is
        # already in All-Courses mode (course_filter is None) — there is no
        # "current course" to fall back from.
        cross = self._maybe_cross_course_fallback(
            retrieval_query, course_filter, top_k, checked_files,
        )
        if cross is not None:
            cross_results, origin = cross
            logger.info("qa.path=cross-course course=%s origin=%s hits=%d",
                        course_filter, origin, len(cross_results))
            return await self._answer_rag(
                question, course_filter, cross_results,
                path="cross-course",
                backend=backend,
                cross_course_origin=origin,
                user_lang=user_lang,
                persona=persona,
            )

        # ── Final fallback: general ──
        logger.info("qa.path=general reason=gate-fail+no-translate+no-cross course=%s",
                    course_filter)
        return await self._answer_general(
            question, course_filter,
            reason="RAG score gate failed and translation retry did not help",
            user_lang=user_lang,
            backend=backend,
            persona=persona,
        )

    # ── Helpers ────────────────────────────────────────────────────────

    def _apply_checked_files(
        self,
        results: list[SearchResult],
        checked_files: list[str] | None,
        question: str,
        top_k: int,
        course_filter: str | None,
    ) -> list[SearchResult]:
        if not checked_files:
            return results
        filtered = [r for r in results if r.source_file in checked_files]
        if len(filtered) < 2:
            more = self.kb.search(question, top_k=top_k * 3, course_id=course_filter)
            second = [r for r in more if r.source_file in checked_files]
            if second:
                filtered = second[:top_k]
        return filtered

    async def _maybe_graphrag(
        self,
        question: str,
        course_filter: str,
        query_embedding=None,
    ) -> list[SearchResult] | None:
        """R4-4: try GraphRAG retrieve for `course_filter`. Returns:
          - ``None`` when the course has no ``knowledge_graph.json`` (caller
            falls through to plain RAG without logging a path miss).
          - ``[]`` when the KG exists but graph_search returned zero hits
            (caller treats as a miss and falls through, same as None).
          - ``list[SearchResult]`` of length ≥1 on a positive retrieve;
            caller's ``>=2`` gate decides whether to commit to path=graphrag.

        The KG file existence check + the synchronous load + cosine pass
        are off-loaded via ``asyncio.to_thread`` so event-loop responsiveness
        is preserved during the 30-100 ms KG load on a cold course.

        `query_embedding` (review-swarm graphrag-all-courses #MED-3):
        precomputed query embedding shared across an All Courses fan-out
        so each per-course `graph_search` skips its own `embed_fn([query])`
        call (~80-640ms saved on 8 courses, API mode).
        """
        from nano_notebooklm import config
        kg_path = config.ARTIFACTS_DIR / "courses" / course_filter / "knowledge_graph.json"
        if not kg_path.exists():
            return None
        try:
            # Imported lazily so qa_skill stays importable if a future
            # refactor moves graph_search; circular-import safety.
            from nano_notebooklm.kb.graph_search import graph_search
            # fix-all v3 #L4 (R4-4 review-swarm v3): bound graph_search wall
            # time via asyncio.wait_for. embed_fn in API mode can hang on a
            # stalled HTTP connection; without a timeout, qa_skill never
            # falls through to plain RAG. 10s is generous — local sentence-
            # transformer batched call is < 1s; API mode typical < 2s.
            return await asyncio.wait_for(
                asyncio.to_thread(
                    graph_search, question, course_filter, self.kb.embed_fn,
                    query_embedding=query_embedding,
                ),
                timeout=GRAPHRAG_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "graph_search timed out (>%ss) for course=%s; falling back to RAG",
                GRAPHRAG_TIMEOUT_SECONDS, course_filter,
            )
            return None
        except Exception as exc:  # noqa: BLE001 — never crash chat on KG error
            # fix-all v2 #V5 (R4-4 review-swarm v2): drop exc_info=True so
            # API-mode openai-python tracebacks don't ship user query text
            # into log shippers / on-call alerts. Exception type + repr is
            # enough for triage.
            # graphrag-all-courses #LOW: drop str(exc) per same reason —
            # APIError.__str__ includes request body. Type-name only.
            code = getattr(exc, "code", type(exc).__name__)
            logger.warning("graph_search failed for course=%s (%s)",
                           course_filter, code)
            return None

    async def _maybe_graphrag_all_courses(
        self,
        question: str,
    ) -> list[SearchResult]:
        """All Courses graphrag: iterate every course with a
        `knowledge_graph.json`, run `_maybe_graphrag` in parallel via
        `asyncio.gather`, merge results across courses, and return the
        top-N by cosine score.

        Returns `[]` when no courses have KGs or all returned empty —
        caller falls through to plain RAG / cross-course / general the
        same way single-course graphrag does on a miss.

        Cost / safety guarantees:
          - Wall time bounded by `GRAPHRAG_TIMEOUT_SECONDS` (per-task)
            plus an outer `wait_for` backstop = 15s (review-swarm #LOW).
            If `embed_fn` isn't truly parallel-safe (e.g. tiny httpx
            connection pool serialising the per-course calls), the outer
            backstop prevents N×10s degenerate cases.
          - Fan-out concurrency bounded by `_GRAPHRAG_FANOUT_SEM` so a
            burst of All Courses chats can't saturate the default
            ThreadPoolExecutor and starve notes / upload / mindmap
            to_thread calls (HIGH-1).
          - `courses_with_kg` cached by `(ARTIFACTS_DIR, mtime)` for 60s
            so a hot path doesn't `iterdir()` on every chat (MED-4 cache).
          - Per-course `embed_fn([query])` is amortised: this method
            precomputes the query embedding once and threads it through
            `graph_search.query_embedding` (MED-3).
        """
        from nano_notebooklm import config
        courses_root = config.ARTIFACTS_DIR / "courses"
        courses_with_kg = self._discover_courses_with_kg(courses_root)
        if not courses_with_kg:
            return []

        # Precompute the query embedding once. Off-loaded to a thread
        # because embed_fn is sync (sentence-transformer or HTTP). If the
        # embed fails we still fan-out without a precomputed value —
        # graph_search falls back to its own embed_fn path, identical to
        # single-course behaviour. So the failure mode is "no amortisation
        # benefit", not "no retrieval".
        precomputed_q_emb = None
        try:
            import numpy as np
            q_out = await asyncio.wait_for(
                asyncio.to_thread(self.kb.embed_fn, [question.strip()]),
                timeout=GRAPHRAG_TIMEOUT_SECONDS,
            )
            precomputed_q_emb = np.asarray(q_out, dtype=np.float32)
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "code", type(exc).__name__)
            logger.warning(
                "graphrag_all_courses: shared query embed failed (%s); "
                "falling back to per-course embed", code,
            )

        # Bounded fan-out. `Semaphore` caps simultaneous to_thread calls
        # so a flood of All Courses chats can't drain the default pool.
        # Module-level instance is sized by review-swarm HIGH-1 default.
        async def _bounded(cid: str):
            async with _GRAPHRAG_FANOUT_SEM:
                return await self._maybe_graphrag(
                    question, cid, query_embedding=precomputed_q_emb,
                )

        tasks = [_bounded(cid) for cid in courses_with_kg]
        # review-swarm v2 HIGH (2026-05-13): the v1 fix wrapped the gather
        # in a 15s outer `wait_for`. Two reviewers flagged this:
        #   (a) on cancel, `_bounded`'s `async with _GRAPHRAG_FANOUT_SEM`
        #       releases the permit on __aexit__, but the underlying
        #       `asyncio.to_thread(graph_search, ...)` thread keeps
        #       running (Python has no thread-cancel primitive). The
        #       next batch then acquires fresh permits while the
        #       previous batch's threads are still alive → actual
        #       thread-pool worker count exceeds the semaphore limit,
        #       breaking the HIGH-1 DoS-defense premise.
        #   (b) for N≥5 courses + 1 cold-start (one 10s outlier), the
        #       outer 15s cap pre-empts the second batch's healthy
        #       tasks before their own 10s ceiling fires, so a chat
        #       with 6/8 courses ready silently returns [].
        # Fix: drop the outer wait_for. Each per-task `wait_for` inside
        # `_maybe_graphrag` already enforces 10s per course; combined
        # with semaphore=4 this naturally bounds total wall time at
        # `ceil(N / _GRAPHRAG_FANOUT_CONCURRENCY) * GRAPHRAG_TIMEOUT_SECONDS`.
        # For N=10, k=4 → 30s worst case; the chat path is acceptable
        # in that pathological "every course has cold-start API embed"
        # scenario, and crucially we no longer cancel threads we can't
        # actually stop.
        per_course = await asyncio.gather(*tasks, return_exceptions=True)

        # Merge by chunk_id: best cosine score wins. chunk_id embeds
        # course_id in this codebase (chunker.py), so cross-course
        # collisions are content-equivalent — the score-max picks the
        # higher cosine, not "different content overwrites lower".
        merged: dict[str, SearchResult] = {}
        for cid, r in zip(courses_with_kg, per_course):
            if isinstance(r, Exception):
                # review-swarm MED-4 / R3-1: surface unexpected per-course
                # failures. `_maybe_graphrag` already swallows known
                # errors and returns None; an exception here means
                # something escaped its catch — log code + course, no
                # str(exc) (could leak query in API mode).
                code = getattr(r, "code", type(r).__name__)
                logger.warning(
                    "graphrag_all_courses: course=%s unexpected exception (%s)",
                    cid, code,
                )
                continue
            if r is None:
                continue
            # Defensive intake cap (review-swarm #LOW): graph_search
            # currently returns ≤30, but if a future bump raises the
            # per-course cap the merge dict shouldn't grow N×K
            # unboundedly. Local guard makes the invariant explicit.
            for hit in r[:30]:
                existing = merged.get(hit.chunk_id)
                if existing is None or hit.score > existing.score:
                    merged[hit.chunk_id] = hit
        if not merged:
            return []
        # Cap at 30 chunks — matches the single-course graphrag cap so
        # the LLM context size stays bounded irrespective of fan-out.
        return sorted(merged.values(), key=lambda x: -x.score)[:30]

    def _discover_courses_with_kg(self, courses_root) -> list[str]:
        """List of course_ids that have a `knowledge_graph.json` on disk.

        Cached for `_COURSES_KG_CACHE_TTL` seconds (review-swarm MED-4)
        so each All Courses chat doesn't `iterdir()` + stat each course.
        Invalidates on TTL expiry; upload pipeline doesn't push to this
        cache, but TTL ≤ 60s ensures a newly-ingested course is visible
        within a minute. Acceptable for the typical "upload → wait for
        Stage B → chat" flow.
        """
        import time
        global _COURSES_KG_CACHE
        now = time.monotonic()
        root_str = str(courses_root)
        entry = _COURSES_KG_CACHE.get(root_str)
        if entry is not None:
            cached_at, cached_list = entry
            if now - cached_at < _COURSES_KG_CACHE_TTL:
                return list(cached_list)
        if not courses_root.exists():
            _COURSES_KG_CACHE[root_str] = (now, [])
            return []
        discovered: list[str] = []
        try:
            for course_dir in courses_root.iterdir():
                if not course_dir.is_dir():
                    continue
                if (course_dir / "knowledge_graph.json").exists():
                    discovered.append(course_dir.name)
        except OSError as exc:
            # v2 LOW (R1-M3): symmetric short cache so a transient OSError
            # (NFS hiccup, permission flap) doesn't make every All Courses
            # chat re-pay the iterdir attempt. Cache the empty result for
            # 5s — enough to deflect a burst, short enough that recovery
            # is bounded.
            logger.debug(
                "_discover_courses_with_kg iterdir OSError: %s; caching empty 5s",
                type(exc).__name__,
            )
            _COURSES_KG_CACHE[root_str] = (now - _COURSES_KG_CACHE_TTL + 5.0, [])
            return []
        _COURSES_KG_CACHE[root_str] = (now, list(discovered))
        return discovered

    def _maybe_cross_course_fallback(
        self,
        question: str,
        course_filter: str | None,
        top_k: int,
        checked_files: list[str] | None,
    ) -> tuple[list[SearchResult], str] | None:
        """If the current course turned up nothing usable, try a global search.

        Returns (results, origin_course_id) where ``origin_course_id`` is the
        course of the first hit (used in the answer annotation). Returns
        ``None`` when:
          - caller is already in All Courses mode (no current course to fall
            back from)
          - caller specified ``checked_files`` (the user was explicit about
            which files to use; cross-course would silently override that)
          - global search itself returns nothing usable
        """
        if not course_filter:
            return None
        if checked_files:
            return None

        # Global search (course_id=None) bypasses the per-course filter.
        results = self.kb.search(question, top_k=top_k, course_id=None)
        if not router_intent.passes_score_gate(results):
            return None
        # Filter out any hit whose course_id matches the current course (we
        # already searched there and got nothing). The remaining set is what
        # other courses contribute.
        sibling = [r for r in results if r.course_id != course_filter]
        if not router_intent.passes_score_gate(sibling):
            return None
        origin = sibling[0].course_id
        return sibling[:top_k], origin

    async def _maybe_translate_retry(
        self,
        question: str,
        course_filter: str | None,
        top_k: int,
        checked_files: list[str] | None,
    ) -> tuple[str, list[SearchResult]] | None:
        """Translate the query once if course/query languages mismatch and the
        query is not already mixed-language. Returns (translated_query, results)
        on success, else None."""
        if not course_filter:
            return None
        query_lang = router_intent.detect_lang(question)
        if query_lang == "mixed":
            return None
        course_lang = router_intent.get_course_lang(self.kb, course_filter)
        if course_lang in (None, "mixed"):
            return None
        if query_lang == course_lang:
            return None

        target = "English" if course_lang == "en" else "Chinese"
        try:
            resp = await asyncio.wait_for(
                self.router.complete(
                    prompts.TRANSLATE_QUERY_PROMPT.format(
                        target_lang=target, query=question),
                    task_type="translate_query",
                    system=prompts.TRANSLATE_QUERY_SYSTEM,
                    temperature=0.0,
                    max_tokens=128,
                    # Single attempt only — the router's default 3-retry budget
                    # with exponential backoff (1s + 2s) would silently exceed
                    # TRANSLATION_TIMEOUT_SECONDS and force a fall-through to
                    # general path on the second attempt anyway. Make the
                    # "no retry" intent explicit.
                    max_retries=1,
                ),
                timeout=TRANSLATION_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning("translation retry timed out after %ss",
                           TRANSLATION_TIMEOUT_SECONDS)
            return None
        except Exception as exc:  # graceful: translation failure → general
            logger.warning("translation retry LLM call failed: %s", exc)
            return None

        translated = (resp.content or "").strip().strip(_QUOTE_STRIP).strip()
        if not translated:
            return None

        results = self.kb.search(translated, top_k=top_k, course_id=course_filter)
        results = self._apply_checked_files(results, checked_files,
                                            translated, top_k, course_filter)
        if not router_intent.passes_score_gate(results):
            return None
        return translated, results

    async def _complete_with_backend_fallback(
        self,
        prompt: str,
        task_type: str,
        system: str,
        temperature: float,
        max_tokens: int = 4096,
        backend: str | None = None,
    ) -> tuple[LLMResponse, bool]:
        """R4-5 part 2 + fix-all v1: wrap router.complete with
        qwen_raft → default fallback semantics.

        When `backend="qwen_raft"`, the call is bounded by
        `QWEN_BACKEND_TIMEOUT_SECONDS` and any failure (timeout, HTTP
        error, transient 5xx) silently degrades. Returns
        (response, fell_back) so the caller can surface
        `backend_fallback=True` to the client.

        **fix-all v1 #V1 (R4-5 review v1)**: `backend="codex"` is
        treated as **default task routing** (same as `None`), NOT as
        an explicit "openai" pin. The original v1 wired codex→openai
        via alias + disabled router auto-fallback, which 500s on any
        deployment without `OPENAI_API_KEY` set (claude-only +
        qwen-only configs). codex is the chip's user-facing label for
        "use the default backend", not a hard pin on the openai key.

        **fix-all v1 #V4**: when qwen_raft is pinned, set
        `max_retries=1` so the router's exponential-backoff retry
        loop doesn't burn 3.3s before the outer `wait_for` catches a
        fast-failing 5xx. The `wait_for(30s)` is the budget; retries
        within it are wasted.

        **fix-all v1 #V4**: narrow the broad `except Exception` to
        `(httpx.HTTPError, RuntimeError, QwenBackendError)` so a
        genuine programming bug (TypeError, KeyError) surfaces as 500
        rather than getting silently masked by the fallback.

        **fix-all v1 #V4 PII scrub**: log only `getattr(exc, "code",
        type(exc).__name__)` — QwenBackendError carries a stable
        `code` attribute designed for safe logging. Drop `str(exc)`
        which may contain prompts / URLs.
        """
        if backend == "qwen_raft":
            try:
                resp = await asyncio.wait_for(
                    self.router.complete(
                        prompt, task_type=task_type, system=system,
                        temperature=temperature, max_tokens=max_tokens,
                        backend="qwen_raft",
                        max_retries=1,  # #V4: outer wait_for is the budget
                    ),
                    timeout=QWEN_BACKEND_TIMEOUT_SECONDS,
                )
                return resp, False
            except asyncio.TimeoutError:
                logger.warning(
                    "qwen_raft backend timed out (>%ss); falling back to default routing",
                    QWEN_BACKEND_TIMEOUT_SECONDS,
                )
            except _QWEN_EXPECTED_ERRORS as exc:
                # #V4 PII scrub: prefer stable error code; never log exc body.
                code = getattr(exc, "code", type(exc).__name__)
                logger.warning(
                    "qwen_raft backend failed (%s); falling back to default routing",
                    code,
                )
            # 2026-05-13 hotfix: when qwen_raft fails, fall back to
            # qwen_base (also Qwen, same RAG output style) instead of
            # running default routing. Default routing on a codex-
            # daily-exhausted day hits openai → 403 ×2 → router internal
            # fallback to qwen_raft → another wrapper timeout → 7-min
            # double-loop. Pinning qwen_base here makes the fallback
            # deterministic and fast (~30s). #V1 originally pointed at
            # default routing to support claude-only deployments; that
            # path still works because qwen_base is configured iff the
            # operator opted into the dual-Qwen setup.
            resp = await self.router.complete(
                prompt, task_type=task_type, system=system,
                temperature=temperature, max_tokens=max_tokens,
                backend="qwen_base",
            )
            return resp, True

        # backend is None or "codex": default task-type routing.
        # #V1: "codex" is the user-facing chip label; treat it as
        # "use whatever backend the operator configured" rather than
        # forcing openai. Operator's DEFAULT_BACKEND + TASK_ROUTES rule.
        resp = await self.router.complete(
            prompt, task_type=task_type, system=system,
            temperature=temperature, max_tokens=max_tokens,
        )
        return resp, False

    async def _answer_rag(
        self,
        question: str,
        course_filter: str | None,
        results: list[SearchResult],
        path: str,
        original_query: str | None = None,
        translated_query: str | None = None,
        cross_course_origin: str | None = None,
        user_lang: str | None = None,
        backend: str | None = None,
        persona: str | None = None,
        low_confidence: bool = False,
    ) -> SkillResult:
        context = "\n\n---\n\n".join(
            f"[Source: {r.source_file}, {r.location}]\n{r.text}" for r in results
        )
        memory_context = get_context_prompt(course_filter)
        system = prompts.qa_system(persona)
        if memory_context:
            system += f"\n\nStudent context:\n{memory_context}"
        binding = prompts.USER_LANG_BINDING(user_lang)
        if binding:
            system += f"\n\n{binding}"
        # 2026-05-13 Path B (relaxed 2026-05-16): marginal-confidence
        # graphrag. The retrieval passed the admission floor but is below
        # the high-confidence ceiling, so the chunks may not directly
        # answer the question. Per the supplementation policy (system
        # rules 6 + 7), instead of refusing the model should split the
        # reply: cite whatever the chunks actually mention, then add a
        # "补充背景" part with general knowledge — unless the topic is
        # so obscure that general knowledge wouldn't help.
        if low_confidence:
            system += (
                "\n\nNOTE — retrieval confidence is LOW. The retrieved "
                "chunks may only loosely touch the user's question. "
                "Don't fabricate course-specific claims from tangential "
                "context (no inventing citations or extrapolating from "
                "what isn't there). DO still split the reply into the "
                "two-part structure: name the partial / adjacent content "
                "the chunks DO have (with [Source: ...] citations), "
                "then under '补充背景 / Background' answer the "
                "user's real question from general knowledge. This is "
                "more helpful than a refusal."
            )

        # 2026-05-13: Qwen-RAFT empirically weights user-message instructions
        # more than system, AND tends to echo the prompt's own language.
        # When user_lang=zh + backend=qwen_raft, swap the QA scaffolding
        # to a Chinese version so the model sees Chinese instructions and
        # naturally answers in Chinese. Codex follows the soft system-
        # prompt binding fine, so we only flip the scaffolding for qwen.
        if user_lang == "zh" and backend == "qwen_raft":
            prompt = (
                "参考资料：\n\n"
                f"{context}\n\n"
                "---\n\n"
                f"问题：{question}\n\n"
                "请根据上述参考资料用**中文**简洁作答；关键论断要标注引用 [Source: 文件名, 页码]。"
                "即使参考资料是英文写的，你也必须输出中文翻译后的解释，不要直接照搬英文句子。"
                "**重要**：如果参考资料中出现 PPT/OCR 抽取出的碎片化数学符号"
                "（例如 `q1 k1 ρ1 α3,1 α3,2 ρ1, ρ2, ρ3` 这种零散字符），"
                "请用标准 LaTeX 公式重新表达（例如 `$\\mathrm{Attention}(Q,K,V) = "
                "\\mathrm{softmax}(QK^\\top/\\sqrt{d_k}) V$`），"
                "**绝对不要**逐字复述这些碎片符号 — 复述会陷入 token 循环。"
                "如果参考资料里没有完整公式，明确说"
                "「资料中没有给出完整公式」即可，不要编造。"
            )
        elif user_lang == "en" and backend == "qwen_raft":
            prompt = prompts.QA_PROMPT.format(context=context, question=question)
            prompt += "\n\n[OUTPUT LANGUAGE — MANDATORY] Reply ONLY in English; do not echo Chinese verbatim."
        else:
            prompt = prompts.QA_PROMPT.format(context=context, question=question)
        resp, fell_back = await self._complete_with_backend_fallback(
            prompt, task_type="qa_answer", system=system, temperature=0.3,
            backend=backend,
        )

        answer = resp.content
        # 2026-05-13 Step 2: qwen-raft Quote → citation. When the response
        # came from the qwen backend, _strip_raft_preamble has already
        # converted ##begin_quote##...##end_quote## spans into markdown
        # blockquotes. Tag each blockquote with the best-matching source
        # so the frontend citation pipeline can route clicks to the PDF.
        # Idempotent on codex answers (no blockquotes to match).
        if resp.model and "qwen" in resp.model.lower():
            answer = _annotate_quote_sources(answer, results)
        if path == "translated" and original_query and translated_query:
            note = (
                f"_(原问：「{_md_safe(original_query)}」在本课无直接资料；"
                f"已自动翻译为「{_md_safe(translated_query)}」后检索。"
                "Translated retrieval.)_"
            )
            answer = f"{note}\n\n{answer}"
        elif path == "cross-course" and cross_course_origin:
            note = (
                f"_(本课无相关内容，从《{_md_safe(cross_course_origin)}》"
                "课中找到相关材料；Found in another course.)_"
            )
            answer = f"{note}\n\n{answer}"
        elif low_confidence:
            note = (
                "_(本次检索置信度较低，回答可能不完全贴合问题。"
                "Low retrieval confidence — the answer may not directly match.)_"
            )
            answer = f"{note}\n\n{answer}"

        add_interaction(
            course_id=course_filter or "general",
            question=original_query or question,
            summary=answer[:200],
        )

        data: dict = {
            "answer": answer,
            "sources": _serialize_sources(results),
            "model": resp.model,
            "tokens_used": resp.input_tokens + resp.output_tokens,
            "path": path,
        }
        if original_query is not None:
            data["original_query"] = original_query
        if translated_query is not None:
            data["translated_query"] = translated_query
        if cross_course_origin is not None:
            data["cross_course_origin"] = cross_course_origin
        # R4-5 part 2: surface qwen→codex fallback so the frontend chip
        # can flag the degradation. Only set when fell_back is True;
        # ChatResponse model treats False as no-fallback.
        if fell_back:
            data["backend_fallback"] = True
        return SkillResult(success=True, data=data)

    async def _answer_general(
        self,
        question: str,
        course_filter: str | None,
        reason: str,
        route_reason: str | None = None,
        user_lang: str | None = None,
        backend: str | None = None,
        persona: str | None = None,
    ) -> SkillResult:
        memory_context = get_context_prompt(course_filter)
        system = prompts.general_qa_system(persona)
        if memory_context:
            system += f"\n\nStudent context:\n{memory_context}"
        binding = prompts.USER_LANG_BINDING(user_lang)
        if binding:
            system += f"\n\n{binding}"

        # Route-reason-specific addenda. The router classified this as general;
        # we tell the model *why* so the response shape matches the user's
        # intent (identity blurb / course meta / clarification request).
        prompt = question
        if route_reason:
            if route_reason.startswith("identity"):
                system += "\n\n" + prompts.identity_addendum(persona)
            elif route_reason.startswith("meta_course"):
                system += "\n\n" + prompts.META_COURSE_ADDENDUM.format(
                    course=course_filter or "All Courses",
                )
            elif route_reason.startswith("bare_q"):
                # Single-token interrogative — ask the user to clarify rather
                # than guessing. We override the prompt entirely so the model
                # produces a clarification, not an attempt at an answer.
                system += "\n\n" + prompts.BARE_INTERROGATIVE_ADDENDUM
                prompt = (
                    f"The user said only \"{question}\" — a bare interrogative "
                    "with no topic. Reply with a single short clarification "
                    "question that matches their language."
                )
            elif route_reason.startswith("profanity"):
                # 2026-05-13 Path A: hostile input — don't retrieve, don't
                # explain, don't lecture. The model overrides everything to
                # acknowledge briefly and pivot back to the course.
                system += (
                    "\n\nThe user's message contains hostility or insults "
                    "directed at you. Do NOT lecture them or moralise. Reply "
                    "in their language, in one short sentence: politely "
                    "acknowledge that you're here to help with their course "
                    "and invite them to ask a real question about the "
                    "material. No more than 25 words."
                )
                prompt = (
                    f"The user wrote: \"{question}\". Reply per the rules in "
                    "the system prompt — short, calm, redirect to course help."
                )

        fell_back = False
        try:
            resp, fell_back = await self._complete_with_backend_fallback(
                prompt,
                task_type="qa_general",
                system=system,
                temperature=0.7,
                max_tokens=1024,
                backend=backend,
            )
            content = resp.content
            model = resp.model
            tokens = resp.input_tokens + resp.output_tokens
        except Exception as exc:  # last-resort: keep the chat alive
            logger.warning("general path LLM call failed: %s", exc)
            content = "暂时无法生成回答，请稍后重试。"
            model = "fallback"
            tokens = 0

        add_interaction(
            course_id=course_filter or "general",
            question=question,
            summary=content[:200],
        )
        data = {
            "answer": content,
            "sources": [],
            "model": model,
            "tokens_used": tokens,
            "path": "general",
            "general_reason": reason,
        }
        # R4-5 part 2: surface qwen→codex fallback flag (general path).
        if fell_back:
            data["backend_fallback"] = True
        return SkillResult(success=True, data=data)
