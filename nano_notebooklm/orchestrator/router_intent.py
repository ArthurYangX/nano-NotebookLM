"""Intent router + score gate + language detection.

Round 2 #1: classify the inbound chat input into one of four paths so the
qa_skill knows whether to do RAG, downgrade to a generic GPT response, or
trigger a translation retry.

Round 2 #2: language fingerprinting — used by qa_skill to decide whether a
0-hit RAG search on the current course should attempt one translation retry
(zh query on en course / vice versa).

The four allowed `path` values are constrained by GOAL.md:
    "rag" | "general" | "translated" | "cross-course"

This module is pure logic — no I/O, no LLM calls. The qa_skill orchestrates
when to call the LLM for translation; this module only tells it whether the
trigger conditions hold.
"""

from __future__ import annotations

import logging
import math
import os
import re
from dataclasses import dataclass
from typing import Iterable, Literal

from nano_notebooklm.types import SearchResult

logger = logging.getLogger(__name__)

Path = Literal["rag", "general", "translated", "cross-course", "graphrag"]

# ── Tunables ──────────────────────────────────────────────────────────

# Default RRF score floor. Real-data observation (see #R2 eval):
#   - "Carnegie" type query (matched #1 in both indices)  → top1 ≈ 0.0323
#   - "Figure" type query (only one index ranked it well) → top1 ≈ 0.0167
# Threshold 0.020 keeps real concept matches and excludes single-index junk.
# Tunable via env so we can sweep during eval without code changes.
DEFAULT_TOP1_THRESHOLD = 0.020
DEFAULT_MIN_HITS = 2

# "Weight" floor for the meaningful content of a query. CJK characters carry
# more semantic content than a single ASCII letter, so we weight them 2× when
# applying the floor. Floor 3 routes "ok" / "x" / "?" to general, but lets
# "内存" (2 CJK = weight 4) and "cache" (5 ASCII = weight 5) into RAG.
SHORT_INPUT_WEIGHT_LIMIT = 3
ASCII_CHAR_WEIGHT = 1
CJK_CHAR_WEIGHT = 2

# Greeting / chit-chat keywords — case-insensitive substring match on the
# stripped, lower-cased query. Order doesn't matter; we only need any match.
GREETING_KEYWORDS = (
    # zh
    "你好", "您好", "嗨", "在吗", "谢谢", "多谢", "好的", "请问",
    "早上好", "晚上好", "下午好", "晚安", "再见",
    # en
    "hi", "hello", "hey", "thanks", "thank you", "good morning",
    "good afternoon", "good evening", "good night", "bye",
)

# Identity / persona questions. These are about the *assistant* (Dr. Marginalia),
# not about course content. Routing them through RAG always misfires because
# course chunks never contain the assistant's identity. Match as substring of
# the cleaned (whitespace-collapsed) query.
IDENTITY_KEYWORDS = (
    # zh
    "你是谁", "你叫什么", "你是什么", "介绍一下自己", "自我介绍", "你是哪位",
    # en
    "who are you", "what are you", "your name", "introduce yourself",
    "who is this", "tell me about yourself",
)

# Meta-course questions — "what is this course about" / "这是什么课". User is
# asking about the course's *identity* (subject area, instructor, scope) — RAG
# rarely answers this well because the answer is usually in metadata, not in
# textbook chunks. General path can compose from course_id + lang + a short
# system note.
META_COURSE_KEYWORDS = (
    # zh
    "这是什么课", "什么课程", "这门课", "课程介绍", "课程是什么",
    "这是哪门", "这门是什么", "什么是这门课",
    # en
    "what is this course", "what course is this", "what's this course",
    "what is this class", "describe this course", "tell me about this course",
)

# Bare interrogatives — single-token questions like "what" / "why" / "什么" /
# "为什么" with no content noun. RAG on them returns whichever chunk happens
# to share BM25 mass with the interrogative — pure noise. Route to general +
# clarification ("could you tell me what topic you're asking about?").
BARE_INTERROGATIVES_EN = {
    "what", "why", "how", "when", "where", "who", "which",
    "what?", "why?", "how?", "when?", "where?", "who?", "which?",
}
# ZH set: keyword_target normalisation collapses trailing `?`/`？` so the
# bare-form alone is sufficient — no dead `?`-suffixed dupes (review-swarm
# fix-all v3 #3 cleanup).
BARE_INTERROGATIVES_ZH = {
    "什么", "为什么", "怎么", "怎样", "如何", "哪", "哪里", "哪个", "谁",
}

# Pure punctuation / whitespace pattern. \W in Python's re by default also
# matches unicode punctuation when re.UNICODE is on (the default in Py3).
_PUNCT_ONLY = re.compile(r"^[\s\W_]+$", re.UNICODE)


@dataclass(frozen=True)
class RouteDecision:
    """Routing verdict for a single chat input.

    `reason` is **opaque** — its format is namespaced (`identity:`, `meta_course:`,
    `bare_q:`, `greeting:`, `weight_below`, etc.) for the qa_skill addendum
    selector and structured logs, but downstream code should match by
    `startswith()` on the namespace, not by exact-string equality. The format
    is internal and may change without breaking the API contract.
    """
    path: Path
    reason: str
    cleaned_query: str


def _read_threshold() -> float:
    raw = os.getenv("RAG_SCORE_GATE_TOP1")
    if raw is None:
        return DEFAULT_TOP1_THRESHOLD
    try:
        v = float(raw)
    except ValueError:
        logger.warning("RAG_SCORE_GATE_TOP1=%r is not a float; using default %s",
                       raw, DEFAULT_TOP1_THRESHOLD)
        return DEFAULT_TOP1_THRESHOLD
    if math.isnan(v) or math.isinf(v):
        logger.warning("RAG_SCORE_GATE_TOP1=%r is NaN/inf; using default %s",
                       raw, DEFAULT_TOP1_THRESHOLD)
        return DEFAULT_TOP1_THRESHOLD
    return min(max(v, 0.0), 1.0)  # clamp to [0, 1]


def _read_min_hits() -> int:
    raw = os.getenv("RAG_SCORE_GATE_MIN_HITS")
    if raw is None:
        return DEFAULT_MIN_HITS
    try:
        return max(1, int(raw))
    except ValueError:
        logger.warning("RAG_SCORE_GATE_MIN_HITS=%r is not an int; using default %s",
                       raw, DEFAULT_MIN_HITS)
        return DEFAULT_MIN_HITS


# ── Public API ────────────────────────────────────────────────────────


def classify_input(query: str) -> RouteDecision:
    """Decide whether `query` should hit RAG or short-circuit to general path.

    Rules (first match wins):
      1. strip-after-empty / pure punctuation / pure emoji → general
      2. matches an identity keyword (你是谁 / who are you) → general:identity
      3. matches a meta-course keyword (这是什么课 / what is this course) →
         general:meta_course
      4. bare interrogative (single-token "what" / "什么") → general:bare_q
      5. weight < SHORT_INPUT_WEIGHT_LIMIT → general (covers "ok", "?")
      6. matches a greeting keyword → general
      7. otherwise → rag (the score gate decides downgrade later)

    Reason strings are namespaced (`identity`, `meta_course`, `bare_q`,
    `greeting`, `weight_below`) so qa_skill can tailor the prompt path or
    surface a clarification UI without re-classifying.
    """
    stripped = (query or "").strip()
    if not stripped:
        return RouteDecision("general", "empty after strip", "")

    if _PUNCT_ONLY.match(stripped):
        return RouteDecision("general", "punctuation/whitespace only", stripped)

    lowered = stripped.lower()
    # Strip trailing/leading punctuation noise for keyword & bare-q matching
    # (without losing the original `cleaned_query` we report back).
    keyword_target = re.sub(r"[\s\W_]+", " ", lowered, flags=re.UNICODE).strip()

    for kw in IDENTITY_KEYWORDS:
        if kw in keyword_target or kw in lowered:
            return RouteDecision("general", f"identity: {kw}", stripped)

    for kw in META_COURSE_KEYWORDS:
        if kw in keyword_target or kw in lowered:
            return RouteDecision("general", f"meta_course: {kw}", stripped)

    # Bare interrogatives must check the original lowered (preserves trailing
    # `?`) AND the stripped form ("what" alone). Bare zh form is a single CJK
    # word; bare en form is a single token. Multi-token queries like
    # "what is convolution" never match these sets and continue to rag.
    if lowered in BARE_INTERROGATIVES_EN or stripped in BARE_INTERROGATIVES_ZH:
        return RouteDecision("general", f"bare_q: {stripped}", stripped)
    # Also catch "what?" with surrounding whitespace already stripped above.
    if keyword_target in BARE_INTERROGATIVES_EN or keyword_target in BARE_INTERROGATIVES_ZH:
        return RouteDecision("general", f"bare_q: {stripped}", stripped)

    meaningful = [c for c in stripped if c.isalnum() or _is_cjk(c)]
    weight = sum(CJK_CHAR_WEIGHT if _is_cjk(c) else ASCII_CHAR_WEIGHT
                 for c in meaningful)

    # Greeting check runs before the short-input check so "你好" / "hi" record
    # the more informative reason ("greeting") instead of "too short".
    for kw in GREETING_KEYWORDS:
        if kw in lowered and len(meaningful) <= 6:
            return RouteDecision("general", f"greeting: {kw}", stripped)

    if weight < SHORT_INPUT_WEIGHT_LIMIT:
        return RouteDecision("general", f"weight_below {weight}<{SHORT_INPUT_WEIGHT_LIMIT}", stripped)

    return RouteDecision("rag", "default → rag with score gate", stripped)


def passes_score_gate(
    results: list[SearchResult],
    top1_threshold: float | None = None,
    min_hits: int | None = None,
) -> bool:
    """RAG result quality gate. Returns True iff results are usable for QA.

    Two-branch acceptance keeps the gate fair on small courses (single-doc
    uploads where only one strong hit ever exists):
      A. top1.score ≥ threshold AND len(results) ≥ min_hits  (normal case)
      B. top1.score ≥ 2 × threshold AND len(results) ≥ 1     (single-strong-hit)
    Branch B requires double the score floor (relative to ``threshold``,
    NOT to whatever the caller passed for ``min_hits``) so we don't accept
    stray noise just because the corpus is tiny. When ``min_hits == 1``
    branch A subsumes branch B; this is intentional — A always tries the
    looser bar first.
    """
    if not results:
        return False

    threshold = top1_threshold if top1_threshold is not None else _read_threshold()
    minimum = min_hits if min_hits is not None else _read_min_hits()

    top1 = results[0].score
    if top1 >= threshold and len(results) >= minimum:
        return True
    if top1 >= 2 * threshold and len(results) >= 1:
        return True
    return False


# ── Language detection ────────────────────────────────────────────────


def _is_cjk(c: str) -> bool:
    """CJK unified ideograph range (covers everyday Chinese / Japanese kanji)."""
    return "一" <= c <= "鿿"


def _is_ascii_letter(c: str) -> bool:
    return ("a" <= c <= "z") or ("A" <= c <= "Z")


def detect_lang(text: str) -> Literal["zh", "en", "mixed"]:
    """Classify a string as zh / en / mixed by counting CJK vs ASCII letters.

    Threshold: a script with ≥15% of the meaningful chars qualifies as
    "present"; if both qualify → mixed. The lower bound matters for short
    queries like "什么是 backpropagation" where one CJK char weighs against
    a whole English word.
    """
    cjk = sum(1 for c in text if _is_cjk(c))
    en = sum(1 for c in text if _is_ascii_letter(c))
    total = cjk + en
    if total == 0:
        return "en"  # No alphabetic content → treat as en for routing purposes

    cjk_ratio = cjk / total
    en_ratio = en / total

    zh_present = cjk_ratio >= 0.15
    en_present = en_ratio >= 0.15
    if zh_present and en_present:
        return "mixed"
    if zh_present:
        return "zh"
    return "en"


def compute_lang_fingerprint(texts: Iterable[str]) -> dict:
    """Compute a per-course language fingerprint from a sample of chunk texts.

    Returns: {"lang": "zh"|"en"|"mixed", "zh_ratio": float, "en_ratio": float}
    """
    cjk = 0
    en = 0
    for t in texts:
        for c in t:
            if _is_cjk(c):
                cjk += 1
            elif _is_ascii_letter(c):
                en += 1
    total = cjk + en
    if total == 0:
        return {"lang": "en", "zh_ratio": 0.0, "en_ratio": 0.0}
    zh_ratio = cjk / total
    en_ratio = en / total
    if zh_ratio >= 0.30 and en_ratio >= 0.30:
        lang = "mixed"
    elif zh_ratio >= 0.30:
        lang = "zh"
    else:
        lang = "en"
    return {"lang": lang, "zh_ratio": zh_ratio, "en_ratio": en_ratio}


# ── Per-course language cache (populated lazily by qa_skill) ───────────

_LANG_CACHE: dict[str, dict] = {}


def get_course_lang(kb, course_id: str | None) -> str | None:
    """Return the cached language for `course_id`, computing it on first call.

    Sample size is capped at 30 chunks for performance; that's enough signal
    for the 30%-script-presence heuristic. Uses ``kb.peek_chunks`` when
    available so we don't pay to load the entire course from disk.
    """
    if not course_id:
        return None
    if course_id in _LANG_CACHE:
        return _LANG_CACHE[course_id]["lang"]

    peek = getattr(kb, "peek_chunks", None)
    if callable(peek):
        sample = [c.text for c in peek(course_id=course_id, n=30)]
    else:
        chunks = kb.get_chunks(course_id=course_id)
        sample = [c.text for c in chunks[:30]] if chunks else []
    fp = compute_lang_fingerprint(sample)
    _LANG_CACHE[course_id] = fp
    return fp["lang"]


def clear_lang_cache(course_id: str | None = None) -> None:
    """Drop cached language fingerprint(s).

    Call after a course is re-ingested or files are uploaded so the next
    chat call recomputes against the new content. Pass ``None`` to clear
    everything (e.g. on global re-index).
    """
    if course_id is None:
        _LANG_CACHE.clear()
    else:
        _LANG_CACHE.pop(course_id, None)
