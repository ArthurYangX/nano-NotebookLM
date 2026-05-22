"""Exam Prep — closed-loop, self-evolving exam preparation skill.

State machine persisted at `artifacts/courses/<id>/exam_bank.json`:

  Phase 1 — plan_topics:
    Extract 5–8 exam-focused topics from the course KB. Each topic has
    a name, weight (0–1), seed source_chunks, and an empty questions
    list. Stable `topic_id = sha1(name)[:10]`.

  Phase 2 — seed_questions / on-demand:
    Per topic, generate diverse multi-type questions (multiple_choice +
    short_answer). Triggered explicitly via action=seed or implicitly on
    the first `next_quiz` for unseeded topics.

  Phase 3 — next_quiz / submit:
    Sample non-mastered questions weighted by topic.weight × wrong-rate.
    On submit, wrong-answered topics trigger LLM variant generation —
    same topic, fresh angle, appended with `variant_of` provenance.

Mastery (per-question): consecutive_correct ≥ MASTERED_THRESHOLD (=3) →
mastered=True, excluded from future quizzes. Reset on any wrong answer.
Topic mastery: ≥ TOPIC_MASTERY_RATIO (0.8) of questions mastered AND
total ≥ TOPIC_MASTERY_MIN_QUESTIONS (3).

Variant budget per submit: `min(PER_TOPIC_CAP=5, max(1, TOTAL_CAP=20 //
wrong_topic_count))` so a single-topic miss can't burn 20 LLM calls and
many-topic misses still get at least one variant each.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nano_notebooklm.ai import prompt_templates as prompts
from nano_notebooklm import config
from nano_notebooklm.skills.base import Skill
from nano_notebooklm.types import SkillResult

logger = logging.getLogger(__name__)

EXAM_BANK_VERSION = 1
MASTERED_THRESHOLD = 3
TOPIC_MASTERY_RATIO = 0.8
TOPIC_MASTERY_MIN_QUESTIONS = 3
TOTAL_VARIANT_CAP = 20
PER_TOPIC_VARIANT_CAP = 5
DEFAULT_SEEDS_PER_TYPE = 2
QUIZ_DEFAULT_SIZE = 8
# Env-tunable so operators on slow providers can raise the ceiling.
EXAM_PREP_LLM_TIMEOUT_S = float(os.getenv("EXAM_PREP_LLM_TIMEOUT_SECONDS", "120.0"))
# fix-all v1 H3: cap concurrent variant-generation LLM calls. Notes pipeline
# uses _FULL_COURSE_SEMAPHORE=2 for the analogous burst; here we sit at 4 to
# match the shared ThreadPoolExecutor(max_workers=4) so we don't oversubscribe
# the backend's executor pool. Without this, a 20-wrong-topic submit fans out
# 20 concurrent codex calls → proxy rate-limits + starves notes/qa/report.
EXAM_PREP_VARIANT_CONCURRENCY = int(os.getenv("EXAM_PREP_VARIANT_CONCURRENCY", "4"))


# Module-level lazy-init semaphore. Built on first use inside the running
# event loop so test fixtures with their own loops don't race a global init.
# R5-2 review-swarm v2 follow-up F3: pre-fix this was a module-global
# `_VARIANT_SEMAPHORE` lazy-built once and reused across loops. Reusing
# an asyncio.Semaphore across different event loops is at best fragile
# (its internal fairness queue binds to a Future on first acquire) and
# at worst crashes "Future attached to a different loop" — exactly the
# pattern test fixtures hit when they wrap each test in `asyncio.run()`.
#
# Key by `id(loop)` but ALSO hold the loop itself in the dict value so
# the loop can't be garbage-collected and have its memory address
# (`id()` is just `addr` in CPython) reused under us. Without the
# loop-reference hold, `asyncio.run()` → return → GC → next
# `asyncio.run()` can land at the same address and `_get_variant_semaphore`
# would return a stale semaphore bound to the dead loop's futures.
_VARIANT_SEMAPHORES: dict[int, tuple["asyncio.AbstractEventLoop", asyncio.Semaphore]] = {}


def _get_variant_semaphore() -> asyncio.Semaphore:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop (sync caller — shouldn't happen in production but
        # keeps tests / scripts safe). Throwaway semaphore; the caller's
        # `async with` will still gate correctly.
        return asyncio.Semaphore(EXAM_PREP_VARIANT_CONCURRENCY)
    key = id(loop)
    cached = _VARIANT_SEMAPHORES.get(key)
    if cached is not None and cached[0] is loop:
        return cached[1]
    sem = asyncio.Semaphore(EXAM_PREP_VARIANT_CONCURRENCY)
    _VARIANT_SEMAPHORES[key] = (loop, sem)
    return sem


# fix-all v1 H2: per-course async lock keyed by (running_loop_id, course_id).
# Without this, two concurrent submits both load_bank → both mutate → both
# save_bank → last-writer-wins discards the other's grading history + any
# successful variants.
#
# review-swarm v2 follow-up F3: same loop-anchor trick as
# `_VARIANT_SEMAPHORES` — store the loop in the value so a GC'd loop
# can't have its id reused under us. Soft cap (512) mirrors
# `_UPLOAD_LOCKS_MAX` so a long-lived process doesn't accumulate one
# entry per (loop_id, course_id) seen.
_COURSE_LOCKS: dict[tuple[int, str], tuple["asyncio.AbstractEventLoop", asyncio.Lock]] = {}
_COURSE_LOCKS_MAX = 512


# 2026-05-13: hold strong refs to fire-and-forget variant-gen tasks so they
# don't get garbage-collected mid-await. asyncio.create_task only retains a
# WEAK ref via its parent; without an external strong ref the task can be
# silently cancelled. Tasks self-discard from the set on completion via
# add_done_callback.
_BACKGROUND_VARIANT_TASKS: set[asyncio.Task] = set()


def _lock_for(course_id: str) -> asyncio.Lock:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop (sync caller). Hand back a fresh lock — caller
        # almost certainly never reaches the `async with` so this is just
        # a safety belt.
        return asyncio.Lock()
    loop_id = id(loop)
    key = (loop_id, course_id)
    cached = _COURSE_LOCKS.get(key)
    if cached is not None and cached[0] is loop:
        return cached[1]
    # Eviction (only when actually inserting):
    if len(_COURSE_LOCKS) >= _COURSE_LOCKS_MAX:
        # Prefer evicting entries whose loop is DIFFERENT from the current
        # one (dead loops from earlier test invocations, etc.). Falls back
        # to oldest insertion when only current-loop entries remain.
        for k, (cached_loop, _cached_lock) in list(_COURSE_LOCKS.items()):
            if cached_loop is not loop:
                _COURSE_LOCKS.pop(k, None)
                break
        else:
            try:
                _COURSE_LOCKS.pop(next(iter(_COURSE_LOCKS)), None)
            except (StopIteration, KeyError):
                pass
    lock = asyncio.Lock()
    _COURSE_LOCKS[key] = (loop, lock)
    return lock


# ── persistence ───────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _bank_path(course_id: str) -> Path:
    return config.ARTIFACTS_DIR / "courses" / course_id / "exam_bank.json"


def _empty_bank(course_id: str) -> dict:
    now = _now_iso()
    return {
        "version": EXAM_BANK_VERSION,
        "course_id": course_id,
        "created_at": now,
        "updated_at": now,
        "topics": [],
    }


class BankVersionTooNewError(Exception):
    """The bank on disk was written by a newer client than this code knows
    how to read. We refuse to overwrite it — fix-all v1 H6: previously
    `load_bank` silently returned an empty bank for ANY version mismatch
    and the next mutating action would overwrite the file, wiping all data
    when a user temporarily ran an older binary against a newer bank.
    """


def load_bank(course_id: str) -> dict:
    """Load + validate + migrate an exam bank for a course.

    - Missing file → empty bank.
    - Unreadable / malformed JSON → empty bank (with a warn log; recoverable).
    - Version *older* than current: future-proofing seam; route through
      `_migrate_bank` (currently no-op since version=1 is the only one).
    - Version *newer* than current: raise BankVersionTooNewError so callers
      can surface 503/409 to the user instead of silently wiping data.
    """
    path = _bank_path(course_id)
    if not path.exists():
        return _empty_bank(course_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("exam_bank load failed for %s: %s", course_id, type(e).__name__)
        return _empty_bank(course_id)
    raw_version = data.get("version")
    try:
        version_int = int(raw_version) if raw_version is not None else None
    except (TypeError, ValueError):
        version_int = None
    if version_int is None:
        logger.warning("exam_bank missing version for %s", course_id)
        return _empty_bank(course_id)
    if version_int > EXAM_BANK_VERSION:
        # Refuse to read OR overwrite. Caller decides UX (503 with message).
        raise BankVersionTooNewError(
            f"bank version {version_int} > supported {EXAM_BANK_VERSION}"
        )
    if version_int < EXAM_BANK_VERSION:
        data = _migrate_bank(data, version_int)
    data.setdefault("topics", [])
    return data


def _migrate_bank(data: dict, from_version: int) -> dict:
    """Forward-migration shell. version=1 is the only schema today; future
    bumps add branches here. Keeps `load_bank` non-destructive: it produces a
    valid current-version bank without overwriting the on-disk file (the next
    save_bank call will eventually persist the migrated shape)."""
    # No migrations to apply yet — placeholder so future versions have a hook.
    data["version"] = EXAM_BANK_VERSION
    return data


def save_bank(course_id: str, bank: dict) -> None:
    path = _bank_path(course_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    bank["updated_at"] = _now_iso()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(bank, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ── mastery helpers ───────────────────────────────────────────────────


def question_mastered(q: dict) -> bool:
    return bool(q.get("mastered")) or int(q.get("consecutive_correct", 0) or 0) >= MASTERED_THRESHOLD


def topic_mastery(topic: dict) -> tuple[int, int, float, bool]:
    """Return (mastered_count, total_count, display_ratio, is_mastered).

    fix-all v1 M5: the display ratio is `mastered/total` (or 0 when empty),
    NOT the padded ratio that determines mastery. Pre-fix, `denom = max(total,
    3)` meant a 1-of-1-mastered topic rendered "1/1 mastered" + "33% bar"
    simultaneously — visually confusing. The padded denom still gates
    `is_mastered` so a 1-question topic can't claim full mastery (intent
    preserved), but the bar shows what the user actually sees.
    """
    qs = [q for q in topic.get("questions", []) if not q.get("archived")]
    total = len(qs)
    mastered = sum(1 for q in qs if question_mastered(q))
    display_ratio = (mastered / total) if total else 0.0
    mastery_denom = max(total, TOPIC_MASTERY_MIN_QUESTIONS)
    is_mastered = (
        total >= TOPIC_MASTERY_MIN_QUESTIONS
        and (mastered / mastery_denom) >= TOPIC_MASTERY_RATIO
    )
    return mastered, total, display_ratio, is_mastered


def variant_budget(wrong_topic_count: int) -> int:
    """How many variants to generate *per wrong topic* this submit.

    Total stays under TOTAL_VARIANT_CAP. With 1 wrong topic → PER_TOPIC_CAP
    (=5, not 20 — we don't burn the whole budget on one topic). With many
    wrong topics → at least 1 each.
    """
    if wrong_topic_count <= 0:
        return 0
    per_topic = TOTAL_VARIANT_CAP // wrong_topic_count
    per_topic = max(1, per_topic)
    return min(PER_TOPIC_VARIANT_CAP, per_topic)


def _stable_topic_id(name: str) -> str:
    h = hashlib.sha1(name.strip().lower().encode("utf-8")).hexdigest()[:10]
    return f"topic_{h}"


def _new_question_id() -> str:
    return f"q_{uuid.uuid4().hex[:12]}"


_TRAILING_PUNCT_RE = re.compile(r"[\s\.\?!,;:。？！，；：]+$")


def _norm_signature(text: str) -> str:
    """Normalize a prompt for dedup. fix-all v1 M13: pre-fix only lowercase +
    whitespace-collapse, so "What is X?" / "What is X." / "What is X!" all
    counted as distinct → variant accumulation degraded "fresh angle" promise.
    Now also strip trailing punctuation (including Chinese full-width) so
    near-duplicates collapse."""
    norm = " ".join(str(text or "").lower().split())
    return _TRAILING_PUNCT_RE.sub("", norm)


def _extract_letter(text: str) -> str:
    s = str(text or "").strip()
    if not s:
        return ""
    if s[0].isalpha():
        return s[0].upper()
    return ""


def check_answer(q: dict, user_answer: Any) -> bool:
    """Grade a single answer. Multi-choice = letter match; short_answer =
    case-insensitive substring overlap (the LLM's `answer` field includes
    the rationale, so exact-match would fail almost every time)."""
    if user_answer is None:
        return False
    user = str(user_answer).strip()
    if not user:
        return False
    expected = str(q.get("answer") or "").strip()
    if not expected:
        return False
    if q.get("type") == "multiple_choice":
        u = _extract_letter(user)
        e = _extract_letter(expected)
        return bool(u) and u == e
    u = user.lower()
    e = expected.lower()
    if u == e:
        return True
    short = min(u, e, key=len)
    long = max(u, e, key=len)
    return short in long and len(short) >= 3


# ── Skill ─────────────────────────────────────────────────────────────


class ExamPrepSkill(Skill):
    """Closed-loop exam prep state machine. Dispatched via the `action` param.

    Actions: plan | seed | next_quiz | submit | view | reset.
    """

    name = "exam_prep"
    description = (
        "Self-evolving exam prep: extract topics → seed multi-type questions "
        "→ grade → generate variants for wrong-answered topics"
    )

    async def execute(self, params: dict) -> SkillResult:
        """Dispatch an action. fix-all v1 H1: user_lang is no longer stashed
        on `self` — `ExamPrepSkill` is a singleton in the orchestrator and a
        shared attribute races across concurrent requests (zh user gets en
        questions if an en request lands during the zh request's await). It's
        passed as an explicit parameter to each handler instead, matching the
        QASkill / NoteGeneratorSkill pattern.
        """
        action = params.get("action", "")
        course_id = params.get("course_id", "")
        if not course_id:
            return SkillResult(success=False, error="course_id required")
        user_lang = params.get("user_lang")
        try:
            if action == "plan":
                return await self.plan_topics(course_id, params, user_lang)
            if action == "seed":
                return await self.seed_questions(course_id, params, user_lang)
            if action == "next_quiz":
                return await self.next_quiz(course_id, params, user_lang)
            if action == "submit":
                return await self.submit_answers(course_id, params, user_lang)
            if action == "view":
                return self.view(course_id)
            if action == "reset":
                return await self.reset(course_id)
            return SkillResult(success=False, error=f"unknown action: {action}")
        except BankVersionTooNewError as e:
            # H6: surface to caller as a typed error so the API can 409/503
            # instead of silently wiping the on-disk bank.
            logger.warning("exam_bank version too new for %s: %s", course_id, e)
            return SkillResult(success=False, error="bank_version_too_new")
        except Exception as exc:
            # M1: drop logger.exception to avoid serializing exception body
            # (openai-python exceptions can carry user query / sk-... bits).
            # Matches the qa_skill v2 fix.
            logger.warning("exam_prep action %s failed: %s", action, type(exc).__name__)
            return SkillResult(success=False, error="exam_prep_failed")

    # ── Phase 1: extract topics ────────────────────────────────────

    async def plan_topics(
        self, course_id: str, params: dict, user_lang: str | None,
    ) -> SkillResult:
        async with _lock_for(course_id):
            return await self._plan_topics_locked(course_id, params, user_lang)

    async def _plan_topics_locked(
        self, course_id: str, params: dict, user_lang: str | None,
    ) -> SkillResult:
        # 2026-05-13: default max_topics is now computed as 4 × number
        # of source_files (clamped to [4, 20]), set later once we've
        # grouped the chunks by source_file. Single-file courses get 4,
        # 2-file courses get 8, 5-file courses get 20. Callers can
        # still pass `max_topics` explicitly to override.
        explicit_max_topics = params.get("max_topics")
        force = bool(params.get("force", False))

        bank = load_bank(course_id)
        if bank.get("topics") and not force:
            return SkillResult(success=True, data={"bank": bank, "reused": True, "view": self._compute_view(bank)})

        # 2026-05-13: switched from a single hardcoded English search
        # ("exam midterm final test quiz key concepts review") to a
        # per-source-file even sample. The search-based approach
        # biased the top-15 hits toward whichever chapter had the
        # highest BM25/vector similarity to the English exam-prep
        # keywords — on a multi-chapter Chinese course this routinely
        # silenced 3-4 chapters out of 5 (e.g. NLP course only
        # surfaced ch4(2) attention chunks, dropping ch4's decision
        # tree / Naive Bayes / SVM entirely). Even sampling gives
        # every source_file a representative slice so the LLM sees
        # the whole course breadth when choosing topics.
        from collections import defaultdict
        from nano_notebooklm.types import SearchResult

        all_chunks = self.kb.get_chunks(course_id)
        if not all_chunks:
            return SkillResult(success=False, error="no_course_content")

        groups: dict[str, list] = defaultdict(list)
        for c in all_chunks:
            groups[c.source_file].append(c)

        # 2026-05-13: enforce "at least 3 topics per source_file" floor
        # in addition to the existing "4 per file" target ceiling. Without
        # the floor, the LLM observed a 5-20 range on the NLP course
        # (5 files → max 20) and chose 8 — one chapter (ch4 "传统机器
        # 学习") collapsed Naive Bayes / 决策树 / SVM into a single
        # topic, dropping legitimate exam coverage. Now:
        #   min_topics = num_files * 3   (LLM MUST hit at least this)
        #   max_topics = num_files * 4   (the "target" budget)
        # Cap raised from 20 → 30 so 7+ file courses (min would be ≥21)
        # don't get an inconsistent min > max constraint. Explicit
        # `params.max_topics` still overrides the upper bound; the
        # floor is then clamped to <= max so the prompt's
        # "{min}-{max}" range stays valid.
        target_per_file = 4
        floor_per_file = 3
        if explicit_max_topics is not None:
            max_topics = int(explicit_max_topics)
        else:
            max_topics = max(target_per_file, min(30, len(groups) * target_per_file))
        min_topics = max(floor_per_file, len(groups) * floor_per_file)
        if min_topics > max_topics:
            min_topics = max_topics

        # 2026-05-13: scale `per_file` with the topic-count floor. The
        # previous `sample_budget=20` → 4 chunks/file (on a 5-file course)
        # routinely starved the LLM of mid-chapter content: ch4 of the
        # NLP course (63 chunks covering Naive Bayes / 决策树 / SVM /
        # Boosting) only contributed 4 evenly-spaced samples, so the LLM
        # never saw the SVM-specific slides and naturally folded the
        # whole chapter into one broad "传统机器学习" topic — no matter
        # how loudly the prompt demanded 3 topics per file. New rule:
        # show the LLM at least `2 × floor_per_file` chunks per file
        # (== 6 for floor_per_file=3), and at least 10 for single-file
        # courses where there are no sibling files to bulk-context the
        # decision. Total budget grows ~linearly with file count, which
        # is still cheap for gpt-5.5 (60 chunks × ~300 chars ≈ 18K tok).
        per_file = max(10 if len(groups) == 1 else 6, floor_per_file * 2)
        sample_budget = per_file * max(1, len(groups))
        sampled: list = []
        for source_file, chunks in groups.items():
            if not chunks:
                continue
            n = len(chunks)
            if n <= per_file:
                picks = list(chunks)
            else:
                # Even sample across [0, n-1] so the picks include both
                # the FIRST and the LAST chunk of the file (and `per_file`
                # evenly-spaced indices in between). Previous version
                # used `chunks[::step][:per_file]` which only covered
                # the head N*step indices — for a 28-chunk file with
                # per_file=10 it stopped at index 18, missing the last
                # 9 chunks (where Transformer / Self-Attention live
                # in ch4(2).pptx, etc.).
                picks = [
                    chunks[round(i * (n - 1) / (per_file - 1))]
                    for i in range(per_file)
                ]
            sampled.extend(picks)
        sampled = sampled[:sample_budget * 2]  # safety cap

        results = [
            SearchResult(
                chunk_id=c.chunk_id, text=c.text, source_file=c.source_file,
                location=c.location, score=0.0, course_id=c.course_id,
            )
            for c in sampled
        ]
        if not results:
            return SkillResult(success=False, error="no_course_content")

        source_text = "\n\n---\n\n".join(
            f"[chunk_id={r.chunk_id} · {r.source_file} · {r.location}]\n{r.text}"
            for r in results
        )

        prompt = prompts.EXAM_PREP_TOPIC_PROMPT.format(
            course_name=course_id,
            min_topics=min_topics,
            max_topics=max_topics,
            source_text=source_text,
        )
        prompt += prompts.USER_LANG_REMINDER(user_lang)
        system = prompts.EXAM_PREP_SYSTEM
        binding = prompts.USER_LANG_BINDING(user_lang)
        if binding:
            system = f"{system}\n\n{binding}"
        try:
            data = await asyncio.wait_for(
                self.router.complete_structured(
                    prompt,
                    task_type="exam_prep_plan",
                    system=system,
                    temperature=0.3,
                ),
                timeout=EXAM_PREP_LLM_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            logger.warning("exam_prep plan LLM call timed out after %ss", EXAM_PREP_LLM_TIMEOUT_S)
            return SkillResult(success=False, error="topic_extraction_timeout")
        except Exception as exc:
            # M1: type-name only, no exception body in logs.
            logger.warning("exam_prep plan LLM call failed: %s", type(exc).__name__)
            return SkillResult(success=False, error="topic_extraction_failed")

        topics_raw = data.get("topics") if isinstance(data, dict) else data
        if not isinstance(topics_raw, list):
            return SkillResult(success=False, error="topic_extraction_malformed")
        # Diagnostic: always log the raw LLM count so we can see at a
        # glance whether undersampling, prompt-disobedience, or
        # downstream filtering is what limits final topic count.
        logger.info(
            "exam_prep plan: LLM returned %d raw topics (min_topics=%d max_topics=%d num_files=%d)",
            len(topics_raw), min_topics, max_topics, len(groups),
        )
        if len(topics_raw) < min_topics:
            logger.warning(
                "exam_prep plan: LLM disobeyed floor (%d < %d)",
                len(topics_raw), min_topics,
            )

        # H4: when force=True, preserve old topic.questions for any new topic
        # whose normalized name matches an old topic. Pre-fix, a slight name
        # drift ("Backpropagation" → "Back-propagation" after a zh→en re-extract)
        # changed topic_id and silently dropped all mastery history.
        old_questions_by_norm_name: dict[str, list[dict]] = {}
        if force:
            for old_t in bank.get("topics", []):
                key = _norm_signature(old_t.get("name", ""))
                if key:
                    # If multiple old topics normalize to same key (unlikely),
                    # last wins — they would have been deduped on the prior plan.
                    old_questions_by_norm_name[key] = list(old_t.get("questions", []))

        chunk_index = {r.chunk_id: r for r in results}
        topics: list[dict] = []
        seen_ids: set[str] = set()
        migrated_names: set[str] = set()
        for t in topics_raw[:max_topics]:
            if not isinstance(t, dict):
                continue
            name = str(t.get("name") or "").strip()
            if not name:
                continue
            tid = _stable_topic_id(name)
            if tid in seen_ids:
                continue
            weight = float(t.get("weight", 0.5) or 0.5)
            weight = max(0.0, min(1.0, weight))
            chunk_ids = [str(c) for c in (t.get("source_chunks") or []) if c]
            source_chunks = []
            for cid in chunk_ids:
                hit = chunk_index.get(cid)
                if hit:
                    source_chunks.append({
                        "chunk_id": cid,
                        "source_file": hit.source_file,
                        "location": hit.location,
                    })
            preserved_questions: list[dict] = []
            norm_key = _norm_signature(name)
            if force and norm_key in old_questions_by_norm_name:
                preserved_questions = old_questions_by_norm_name[norm_key]
                migrated_names.add(norm_key)
            topics.append({
                "id": tid,
                "name": name[:100],
                "weight": weight,
                "source_chunks": source_chunks,
                "questions": preserved_questions,
                "created_at": _now_iso(),
            })
            seen_ids.add(tid)

        # Orphan old topics whose names didn't survive re-extraction: their
        # questions move to an archive bucket so a user who suspects a
        # regression can `examPrepReset` AFTER inspecting what they lost.
        orphan_questions: list[dict] = []
        for key, qs in old_questions_by_norm_name.items():
            if key not in migrated_names:
                orphan_questions.extend(qs)
        if orphan_questions:
            archive_id = f"archive_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
            topics.append({
                "id": archive_id,
                "name": f"[archive] orphaned questions from previous re-extract",
                "weight": 0.0,
                "source_chunks": [],
                "questions": orphan_questions,
                "created_at": _now_iso(),
                "archived_topic": True,
            })

        bank["topics"] = topics
        save_bank(course_id, bank)
        return SkillResult(success=True, data={
            "bank": bank, "reused": False, "view": self._compute_view(bank),
            "migrated_topic_count": len(migrated_names),
            "orphan_question_count": len(orphan_questions),
        })

    # ── Phase 2: seed questions for a topic ────────────────────────

    async def seed_questions(
        self, course_id: str, params: dict, user_lang: str | None,
    ) -> SkillResult:
        async with _lock_for(course_id):
            bank = load_bank(course_id)
            if not bank.get("topics"):
                return SkillResult(success=False, error="no_topics — call action=plan first")

            topic_ids = params.get("topic_ids") or [t["id"] for t in bank["topics"]]
            seeds_per_type = int(params.get("seeds_per_type", DEFAULT_SEEDS_PER_TYPE))

            topic_by_id = {t["id"]: t for t in bank["topics"]}
            tasks = []
            targets = []
            for tid in topic_ids:
                topic = topic_by_id.get(tid)
                if topic is None:
                    continue
                tasks.append(self._generate_questions(
                    course_id, topic, count=seeds_per_type,
                    kinds=("multiple_choice", "short_answer"),
                    variant_of=None,
                    user_lang=user_lang,
                ))
                targets.append(topic["id"])

            added_per_topic: dict[str, int] = {}
            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for tid, r in zip(targets, results):
                    if isinstance(r, int):
                        added_per_topic[tid] = r

            save_bank(course_id, bank)
            return SkillResult(success=True, data={
                "added": added_per_topic,
                "view": self._compute_view(bank),
            })

    async def _generate_questions(
        self,
        course_id: str,
        topic: dict,
        count: int,
        kinds: tuple[str, ...],
        variant_of: str | None,
        user_lang: str | None = None,
    ) -> int:
        """Append `count` questions per kind to topic. Returns total added."""
        if count <= 0 or not kinds:
            return 0

        source_results = []
        for sc in topic.get("source_chunks", []):
            cid = sc.get("chunk_id")
            if not cid:
                continue
            chunk = self.kb.find_chunk(cid)
            if chunk is not None:
                source_results.append(chunk)
        if not source_results:
            results = self.kb.search(topic["name"], top_k=5, course_id=course_id)
            source_results = results
        if not source_results:
            return 0

        source_text = "\n\n---\n\n".join(
            f"[{getattr(r, 'source_file', '')}]\n{getattr(r, 'text', '')}"
            for r in source_results[:5]
        )

        avoid_block = ""
        if topic.get("questions"):
            recent = topic["questions"][-10:]
            avoid_block = (
                "\nAVOID rehashing these existing prompts (write fresh angles, not paraphrases):\n"
                + "\n".join(f"- {(q.get('prompt') or '')[:100]}" for q in recent)
            )

        num_total = count * len(kinds)
        prompt = prompts.EXAM_PREP_QUESTIONS_PROMPT.format(
            num_questions=num_total,
            topic_name=topic["name"],
            question_types=", ".join(kinds),
            source_text=source_text,
            avoid_block=avoid_block,
        )
        prompt += prompts.USER_LANG_REMINDER(user_lang)
        system = prompts.EXAM_PREP_SYSTEM
        binding = prompts.USER_LANG_BINDING(user_lang)
        if binding:
            system = f"{system}\n\n{binding}"
        # H3: gate concurrent variant generation through a module-level
        # semaphore so a 20-wrong-topic submit can't fan out 20× codex calls
        # in parallel.
        try:
            async with _get_variant_semaphore():
                data = await asyncio.wait_for(
                    self.router.complete_structured(
                        prompt,
                        task_type="exam_prep_questions",
                        system=system,
                        temperature=0.6,
                    ),
                    timeout=EXAM_PREP_LLM_TIMEOUT_S,
                )
        except asyncio.TimeoutError:
            logger.warning(
                "exam_prep question gen timed out after %ss for topic %s",
                EXAM_PREP_LLM_TIMEOUT_S, topic.get("id"),
            )
            return 0
        except Exception as exc:
            # M1: log exception type only, never body.
            logger.warning(
                "exam_prep question gen failed for topic %s: %s",
                topic.get("id"), type(exc).__name__,
            )
            return 0

        raw_qs = data.get("questions") if isinstance(data, dict) else data
        if not isinstance(raw_qs, list):
            return 0

        existing_sigs = {_norm_signature(q.get("prompt") or "") for q in topic.get("questions", [])}
        added = 0
        for rq in raw_qs:
            if not isinstance(rq, dict):
                continue
            prompt_text = str(rq.get("prompt") or rq.get("question") or "").strip()
            if not prompt_text:
                continue
            sig = _norm_signature(prompt_text)
            if sig in existing_sigs:
                continue
            qtype = str(rq.get("type") or "short_answer")
            if qtype not in {"multiple_choice", "short_answer", "calculation"}:
                qtype = "short_answer"
            options = rq.get("options") if qtype == "multiple_choice" else None
            if qtype == "multiple_choice" and not (isinstance(options, list) and len(options) >= 2):
                continue
            topic["questions"].append({
                "id": _new_question_id(),
                "type": qtype,
                "prompt": prompt_text,
                "options": options,
                "answer": str(rq.get("answer") or ""),
                "explanation": str(rq.get("explanation") or ""),
                "difficulty": str(rq.get("difficulty") or "medium"),
                "concepts": rq.get("concepts") or [topic["name"]],
                "variant_of": variant_of,
                "created_at": _now_iso(),
                "history": [],
                "consecutive_correct": 0,
                "mastered": False,
                "archived": False,
            })
            existing_sigs.add(sig)
            added += 1
        return added

    # ── Phase 3a: sample next quiz ─────────────────────────────────

    async def next_quiz(
        self, course_id: str, params: dict, user_lang: str | None,
    ) -> SkillResult:
        async with _lock_for(course_id):
            return await self._next_quiz_locked(course_id, params, user_lang)

    async def _next_quiz_locked(
        self, course_id: str, params: dict, user_lang: str | None,
    ) -> SkillResult:
        bank = load_bank(course_id)
        if not bank.get("topics"):
            return SkillResult(success=False, error="no_topics — call action=plan first")

        size = int(params.get("size", QUIZ_DEFAULT_SIZE))
        size = max(1, min(20, size))
        requested_topic_ids = params.get("topic_ids") or None
        # M7: when user explicitly drilled into a topic, allow re-quizzing
        # already-mastered questions (they may want to review). Without an
        # explicit topic_ids, exclude mastered to focus on weak areas.
        include_mastered = bool(requested_topic_ids)

        candidates = []
        for t in bank["topics"]:
            if t.get("archived_topic"):
                # H4: archive bucket from a prior force-replan is never
                # sampled for new quizzes (read-only history view).
                continue
            if requested_topic_ids and t["id"] not in requested_topic_ids:
                continue
            _, _, _, mastered = topic_mastery(t)
            if mastered and not requested_topic_ids:
                continue
            candidates.append(t)

        if not candidates:
            return SkillResult(success=True, data={
                "questions": [], "total_available": 0, "topic_count": 0,
                "reason": "all_mastered",
                "view": self._compute_view(bank),
            })

        # M6: track whether seeding failed so we can return reason=generation_failed
        # instead of misleading "all_mastered" / empty questions.
        seeding_attempted = False
        seeding_succeeded = False
        unseeded = [t for t in candidates if not t.get("questions")]
        if unseeded:
            seeding_attempted = True
            tasks = [
                self._generate_questions(
                    course_id, t, count=DEFAULT_SEEDS_PER_TYPE,
                    kinds=("multiple_choice", "short_answer"),
                    variant_of=None,
                    user_lang=user_lang,
                ) for t in unseeded
            ]
            seed_results = await asyncio.gather(*tasks, return_exceptions=True)
            seeding_succeeded = any(isinstance(r, int) and r > 0 for r in seed_results)
            save_bank(course_id, bank)

        scored: list[tuple[float, dict, dict]] = []
        for t in candidates:
            for q in t.get("questions", []):
                if q.get("archived"):
                    continue
                if not include_mastered and question_mastered(q):
                    continue
                hist = q.get("history", []) or []
                attempts = len(hist)
                wrongs = sum(1 for h in hist if not h.get("correct"))
                wrong_rate = (wrongs / attempts) if attempts else 0.5
                score = float(t.get("weight", 0.5)) * (0.4 + 0.6 * wrong_rate)
                scored.append((score, t, q))

        scored.sort(key=lambda x: -x[0])

        picked: list[dict] = []
        per_topic_seen: dict[str, int] = {}
        max_per_topic = max(1, size // max(len(candidates), 1) + 1)
        for _, t, q in scored:
            if len(picked) >= size:
                break
            if per_topic_seen.get(t["id"], 0) >= max_per_topic:
                continue
            picked.append({**q, "topic_id": t["id"], "topic_name": t["name"]})
            per_topic_seen[t["id"]] = per_topic_seen.get(t["id"], 0) + 1

        # Decide the empty-result reason so the UI can show the right copy.
        reason: str | None = None
        if not picked:
            if seeding_attempted and not seeding_succeeded:
                reason = "generation_failed"
            elif all(question_mastered(q) for t in candidates for q in t.get("questions", []) if not q.get("archived")):
                reason = "all_mastered"
            else:
                reason = "no_questions"

        return SkillResult(success=True, data={
            "questions": picked,
            "total_available": len(scored),
            "topic_count": len(candidates),
            "reason": reason,
            "view": self._compute_view(bank),
        })

    # ── Phase 3b: submit + self-evolution ──────────────────────────

    async def submit_answers(
        self, course_id: str, params: dict, user_lang: str | None,
    ) -> SkillResult:
        # 2026-05-13: split submit into a fast synchronous phase (grade +
        # save) and an asynchronous variant-gen phase (LLM calls). Pre-fix,
        # the user waited 8-15s on every submit because `asyncio.gather`
        # over the wrong-topic variant tasks blocked the response. Now the
        # response returns in ~50ms with grading + a `variants_pending`
        # flag; the variant LLM calls run as a fire-and-forget background
        # task that acquires the course lock on its own and merges into
        # the bank when done. Next quiz / next view pulls them up.
        async with _lock_for(course_id):
            response, wrong_topic_ids = await self._grade_and_save_locked(
                course_id, params,
            )
        # Lock released. If anything went wrong (no topics, bad input) or
        # there were no wrong answers, return immediately. Otherwise kick
        # off the background variant gen.
        if wrong_topic_ids:
            task = asyncio.create_task(
                self._generate_variants_background(
                    course_id, wrong_topic_ids, user_lang,
                )
            )
            _BACKGROUND_VARIANT_TASKS.add(task)
            task.add_done_callback(_BACKGROUND_VARIANT_TASKS.discard)
        return response

    async def _grade_and_save_locked(
        self, course_id: str, params: dict,
    ) -> tuple[SkillResult, list[str]]:
        """Grade phase only — no LLM calls. Returns (response, wrong_topic_ids).
        Caller is responsible for spawning background variant gen using the
        returned ids."""
        bank = load_bank(course_id)
        if not bank.get("topics"):
            return SkillResult(success=False, error="no_topics — call action=plan first"), []

        answers = params.get("answers") or {}
        if not isinstance(answers, dict):
            return SkillResult(success=False, error="answers_must_be_dict"), []

        topic_by_qid: dict[str, dict] = {}
        q_by_id: dict[str, dict] = {}
        for t in bank["topics"]:
            for q in t.get("questions", []):
                q_by_id[q["id"]] = q
                topic_by_qid[q["id"]] = t

        wrong_topic_ids: list[str] = []
        wrong_topic_seen: set[str] = set()
        graded: list[dict] = []
        dropped_question_ids: list[str] = []  # L4: visibility into archive/stale qids
        for qid, user_ans in answers.items():
            q = q_by_id.get(qid)
            if q is None:
                dropped_question_ids.append(qid)
                continue
            t = topic_by_qid[qid]
            correct = check_answer(q, user_ans)
            q.setdefault("history", []).append({
                "timestamp": _now_iso(),
                "user_answer": str(user_ans),
                "correct": correct,
            })
            if correct:
                q["consecutive_correct"] = int(q.get("consecutive_correct", 0)) + 1
                if q["consecutive_correct"] >= MASTERED_THRESHOLD:
                    q["mastered"] = True
            else:
                q["consecutive_correct"] = 0
                if t["id"] not in wrong_topic_seen:
                    wrong_topic_ids.append(t["id"])
                    wrong_topic_seen.add(t["id"])
            graded.append({
                "question_id": qid,
                "topic_id": t["id"],
                "topic_name": t["name"],
                "correct": correct,
                "expected": q.get("answer"),
                "explanation": q.get("explanation"),
            })

        budget = variant_budget(len(wrong_topic_ids))
        # L11 (preserved): did per-topic cap clip what WOULD have been
        # generated? Computed up front so we can surface in the immediate
        # response even though variant gen is now async.
        raw_per_topic = max(1, TOTAL_VARIANT_CAP // max(len(wrong_topic_ids), 1))
        budget_capped = budget > 0 and raw_per_topic > PER_TOPIC_VARIANT_CAP
        expected_variant_count = budget * len(wrong_topic_ids)

        save_bank(course_id, bank)
        return SkillResult(success=True, data={
            "graded": graded,
            "wrong_topic_count": len(wrong_topic_ids),
            "variant_budget_per_topic": budget,
            # `variants_added` is now populated by the background task
            # AFTER this response returns. Frontend uses `variants_pending`
            # + `expected_variant_count` for the immediate "+N queued" chip
            # and refreshes the bank later to see actual counts.
            "variants_added": {},
            "variants_pending": budget > 0 and bool(wrong_topic_ids),
            "expected_variant_count": expected_variant_count,
            "budget_capped": budget_capped,
            "dropped_question_ids": dropped_question_ids,
            "view": self._compute_view(bank),
        }), wrong_topic_ids

    async def _generate_variants_background(
        self,
        course_id: str,
        wrong_topic_ids: list[str],
        user_lang: str | None,
    ) -> None:
        """Fire-and-forget variant generation. Acquires the course lock
        freshly (the submit path already released it), reloads the bank,
        runs N concurrent LLM calls (gated by _get_variant_semaphore), and
        merges results back into the bank.

        Errors are swallowed and logged — there's no caller to report to.
        The next view / next_quiz will simply see the bank as-is.
        """
        budget = variant_budget(len(wrong_topic_ids))
        if budget <= 0 or not wrong_topic_ids:
            return
        try:
            async with _lock_for(course_id):
                bank = load_bank(course_id)
                if not bank.get("topics"):
                    return
                # Build tasks. L6 rotation preserved.
                tasks = []
                target_ids = []
                for idx, tid in enumerate(wrong_topic_ids):
                    topic = next((t for t in bank["topics"] if t["id"] == tid), None)
                    if topic is None:
                        continue
                    kinds = (("multiple_choice",) if idx % 2 == 0 else ("short_answer",))
                    source_q = next(
                        (q for q in reversed(topic.get("questions", []))
                         if q.get("history") and not q["history"][-1].get("correct")),
                        None,
                    )
                    tasks.append(self._generate_questions(
                        course_id, topic, count=budget, kinds=kinds,
                        variant_of=(source_q or {}).get("id"),
                        user_lang=user_lang,
                    ))
                    target_ids.append(tid)
                if not tasks:
                    return
                results = await asyncio.gather(*tasks, return_exceptions=True)
                added_total = 0
                for tid, r in zip(target_ids, results):
                    if isinstance(r, int) and r > 0:
                        added_total += r
                    elif isinstance(r, Exception):
                        logger.warning(
                            "background variant gen failed for %s: %s",
                            tid, type(r).__name__,
                        )
                save_bank(course_id, bank)
                logger.info(
                    "exam_prep variants: course=%s wrong=%d budget=%d added=%d",
                    course_id, len(wrong_topic_ids), budget, added_total,
                )
        except Exception as exc:
            # M1: type-name only — this is a fire-and-forget task, swallow.
            logger.warning(
                "background variant gen crashed for %s: %s",
                course_id, type(exc).__name__,
            )

    # ── view / reset ───────────────────────────────────────────────

    def view(self, course_id: str) -> SkillResult:
        bank = load_bank(course_id)
        return SkillResult(success=True, data={
            "bank": bank,
            "view": self._compute_view(bank),
        })

    async def reset(self, course_id: str) -> SkillResult:
        # Lock so we can't unlink mid-submit (which would crash the in-flight
        # save_bank with a vanished parent dir).
        async with _lock_for(course_id):
            path = _bank_path(course_id)
            if path.exists():
                path.unlink()
            return SkillResult(success=True, data={"reset": True})

    def _compute_view(self, bank: dict) -> dict:
        """Build the per-topic + overall mastery snapshot the UI renders.

        Beyond mastery (which only flips after 3 consecutive correct), we
        also surface `attempt_count` + `correct_rate` per topic so the user
        sees progress immediately after every submit — without these the
        topic cards looked frozen after one quiz round because mastered
        counts rarely change in a single submit.
        """
        topics_view: list[dict] = []
        total_mastered = 0
        total_questions = 0
        total_attempts = 0
        total_correct = 0
        total_unique_attempted = 0
        for t in bank.get("topics", []):
            if t.get("archived_topic"):
                # H4 archive bucket: ship as-is so the UI can render history,
                # but exclude from mastery rollups.
                topics_view.append({
                    "id": t["id"], "name": t["name"], "weight": 0.0,
                    "mastered_count": 0, "question_count": 0, "mastery_ratio": 0.0,
                    "is_mastered": False, "attempt_count": 0,
                    "correct_count": 0, "correct_rate": 0.0,
                    "unique_attempted": 0, "is_archived": True,
                })
                continue
            m, total, ratio, is_mastered = topic_mastery(t)
            attempts = 0
            corrects = 0
            unique_attempted = 0  # L7: separate count of questions touched ≥ 1
            for q in t.get("questions", []):
                if q.get("archived"):
                    continue
                hist = q.get("history") or []
                if hist:
                    unique_attempted += 1
                attempts += len(hist)
                corrects += sum(1 for h in hist if h.get("correct"))
            correct_rate = (corrects / attempts) if attempts else 0.0
            topics_view.append({
                "id": t["id"],
                "name": t["name"],
                "weight": float(t.get("weight", 0.5)),
                "mastered_count": m,
                "question_count": total,
                "mastery_ratio": ratio,
                "is_mastered": is_mastered,
                "attempt_count": attempts,
                "correct_count": corrects,
                "correct_rate": correct_rate,
                "unique_attempted": unique_attempted,
                "is_archived": False,
            })
            total_mastered += m
            total_questions += total
            total_attempts += attempts
            total_correct += corrects
            total_unique_attempted += unique_attempted
        topics_view.sort(key=lambda x: (-x["weight"], x["name"]))
        overall = total_mastered / total_questions if total_questions else 0.0
        overall_correct = (total_correct / total_attempts) if total_attempts else 0.0
        return {
            "topics": topics_view,
            "total_mastered": total_mastered,
            "total_questions": total_questions,
            "total_attempts": total_attempts,
            "total_correct": total_correct,
            "total_unique_attempted": total_unique_attempted,
            "overall_ratio": overall,
            "overall_correct_rate": overall_correct,
            "mastered_topics": sum(1 for tv in topics_view if tv.get("is_mastered")),
            "topic_count": len([tv for tv in topics_view if not tv.get("is_archived")]),
        }
