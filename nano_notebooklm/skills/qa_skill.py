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

# fix-all v3 #L4 (R4-4 review-swarm v3): bound graph_search wall time so a
# stalled embed_fn (e.g. API-mode HTTP hang) doesn't block the chat path
# indefinitely. Local sentence-transformer batched call on 200 nodes is
# ~0.3-1.0s; API mode typical < 2s. 10s catches a stuck call quickly while
# leaving headroom for legacy KGs that pay the per-node batch on first use.
GRAPHRAG_TIMEOUT_SECONDS = 10.0

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
    raw = os.getenv("QWEN_BACKEND_TIMEOUT_SECONDS")
    if not raw:
        return 30.0
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "QWEN_BACKEND_TIMEOUT_SECONDS=%r is not a float; using default 30.0",
            raw,
        )
        return 30.0
    if value <= 0 or value != value or value in (float("inf"), float("-inf")):
        return 30.0
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
    return [
        {
            "chunk_id": r.chunk_id,
            "text": r.text[:200] + "..." if len(r.text) > 200 else r.text,
            "source_file": r.source_file,
            "location": r.location,
            "score": r.score,
        }
        for r in results
    ]


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
        question = params.get("question", "")
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

        if not question:
            return SkillResult(success=False, error="No question provided")

        decision = router_intent.classify_input(question)

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
            )

        # ── R4-4 Path graphrag: KG-driven retrieve fires *before* the
        # BM25/vector path when the course has a knowledge_graph.json.
        # The KG is the upload pipeline's product (R4-2) — its concept
        # nodes are L2-normalised embeddings the graph_search ranks by
        # cosine, then expands along part-of / prerequisite_of / depends-on
        # edges to surface the chunks the extractor already linked into
        # the same neighbourhood. Compared to plain RAG this nails
        # cross-concept queries ("how do X and Y relate?") where the
        # surface-lexical RRF would pull two independent passages.
        #
        # Skip conditions:
        #   - All Courses mode (no course_filter): no per-course KG to
        #     consult; the kb.search global path handles it.
        #   - checked_files set: user pinned a file subset; graph_search's
        #     hop expansion cannot honour per-file filtering without
        #     materially degrading the neighbourhood signal.
        # Skip → fall through to existing RAG → translation → cross-course
        # → general chain.
        if course_filter and not checked_files and _graphrag_enabled():
            graphrag_results = await self._maybe_graphrag(question, course_filter)
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
                logger.info("qa.path=graphrag course=%s top1=%.4f hits=%d",
                            course_filter, graphrag_results[0].score,
                            len(graphrag_results))
                return await self._answer_rag(
                    question, course_filter, graphrag_results,
                    path="graphrag",
                    user_lang=user_lang,
                    backend=backend,
                )

        # ── Path A (rag): retrieve, gate, fall back if low-quality ──
        raw = self.kb.search(question, top_k=top_k, course_id=course_filter)
        results = self._apply_checked_files(raw, checked_files,
                                            question, top_k, course_filter)

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
            )

        # ── Translation retry (#2): zh query on en course (or vice versa) ──
        translated = await self._maybe_translate_retry(
            question, course_filter, top_k, checked_files,
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
            )

        # ── Cross-course fallback (#3): own course + translation both 0 → ──
        # search All Courses; if a sibling course has the answer, surface it
        # with a "本课无相关内容" annotation. Skipped when the caller is
        # already in All-Courses mode (course_filter is None) — there is no
        # "current course" to fall back from.
        cross = self._maybe_cross_course_fallback(
            question, course_filter, top_k, checked_files,
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
            )

        # ── Final fallback: general ──
        logger.info("qa.path=general reason=gate-fail+no-translate+no-cross course=%s",
                    course_filter)
        return await self._answer_general(
            question, course_filter,
            reason="RAG score gate failed and translation retry did not help",
            user_lang=user_lang,
            backend=backend,
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
            logger.warning("graph_search failed for course=%s (%s: %s)",
                           course_filter, type(exc).__name__, exc)
            return None

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
            # #V1: fall back to **default task routing** (no explicit
            # backend pin). Avoids the codex→openai hard assumption.
            resp = await self.router.complete(
                prompt, task_type=task_type, system=system,
                temperature=temperature, max_tokens=max_tokens,
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
    ) -> SkillResult:
        context = "\n\n---\n\n".join(
            f"[Source: {r.source_file}, {r.location}]\n{r.text}" for r in results
        )
        memory_context = get_context_prompt(course_filter)
        system = prompts.QA_SYSTEM
        if memory_context:
            system += f"\n\nStudent context:\n{memory_context}"
        binding = prompts.USER_LANG_BINDING(user_lang)
        if binding:
            system += f"\n\n{binding}"

        prompt = prompts.QA_PROMPT.format(context=context, question=question)
        resp, fell_back = await self._complete_with_backend_fallback(
            prompt, task_type="qa_answer", system=system, temperature=0.3,
            backend=backend,
        )

        answer = resp.content
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
    ) -> SkillResult:
        memory_context = get_context_prompt(course_filter)
        system = prompts.GENERAL_QA_SYSTEM
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
                system += "\n\n" + prompts.IDENTITY_ADDENDUM
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
