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
# Per-LLM-call timeout for plan + variant generation. Without it a stuck
# codex connection silently hangs the entire submit endpoint (asyncio.gather
# waits for ALL tasks → frontend fetch has no default timeout → user sees a
# permanent spinner with no feedback). 45 s comfortably covers a healthy
# codex round-trip; anything longer is dead time worth surfacing as an error.
EXAM_PREP_LLM_TIMEOUT_S = 45.0


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


def load_bank(course_id: str) -> dict:
    path = _bank_path(course_id)
    if not path.exists():
        return _empty_bank(course_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("exam_bank load failed for %s: %s", course_id, type(e).__name__)
        return _empty_bank(course_id)
    if data.get("version") != EXAM_BANK_VERSION:
        logger.warning("exam_bank version mismatch for %s", course_id)
        return _empty_bank(course_id)
    data.setdefault("topics", [])
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
    """Return (mastered_count, total_count, ratio, is_mastered)."""
    qs = [q for q in topic.get("questions", []) if not q.get("archived")]
    total = len(qs)
    mastered = sum(1 for q in qs if question_mastered(q))
    denom = max(total, TOPIC_MASTERY_MIN_QUESTIONS)
    ratio = mastered / denom if denom else 0.0
    is_mastered = total >= TOPIC_MASTERY_MIN_QUESTIONS and ratio >= TOPIC_MASTERY_RATIO
    return mastered, total, ratio, is_mastered


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


def _norm_signature(text: str) -> str:
    return " ".join(str(text or "").lower().split())


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
        action = params.get("action", "")
        course_id = params.get("course_id", "")
        if not course_id:
            return SkillResult(success=False, error="course_id required")
        # user_lang is threaded into every LLM call via the system binding so
        # generated topics + questions respect the student's language choice
        # rather than echoing the source-material language.
        self._user_lang = params.get("user_lang")
        try:
            if action == "plan":
                return await self.plan_topics(course_id, params)
            if action == "seed":
                return await self.seed_questions(course_id, params)
            if action == "next_quiz":
                return await self.next_quiz(course_id, params)
            if action == "submit":
                return await self.submit_answers(course_id, params)
            if action == "view":
                return self.view(course_id)
            if action == "reset":
                return self.reset(course_id)
            return SkillResult(success=False, error=f"unknown action: {action}")
        except Exception:
            logger.exception("exam_prep action %s failed", action)
            return SkillResult(success=False, error="exam_prep_failed")

    # ── Phase 1: extract topics ────────────────────────────────────

    async def plan_topics(self, course_id: str, params: dict) -> SkillResult:
        max_topics = int(params.get("max_topics", 8))
        force = bool(params.get("force", False))

        bank = load_bank(course_id)
        if bank.get("topics") and not force:
            return SkillResult(success=True, data={"bank": bank, "reused": True, "view": self._compute_view(bank)})

        results = self.kb.search(
            "exam midterm final test quiz key concepts review",
            top_k=20,
            course_id=course_id,
        )
        if not results:
            chunks = self.kb.get_chunks(course_id)[:20]
            from nano_notebooklm.types import SearchResult
            results = [
                SearchResult(
                    chunk_id=c.chunk_id, text=c.text, source_file=c.source_file,
                    location=c.location, score=0.0, course_id=c.course_id,
                )
                for c in chunks
            ]
        if not results:
            return SkillResult(success=False, error="no_course_content")

        source_text = "\n\n---\n\n".join(
            f"[chunk_id={r.chunk_id} · {r.source_file} · {r.location}]\n{r.text}"
            for r in results[:15]
        )

        prompt = prompts.EXAM_PREP_TOPIC_PROMPT.format(
            course_name=course_id,
            max_topics=max_topics,
            source_text=source_text,
        )
        system = prompts.EXAM_PREP_SYSTEM
        binding = prompts.USER_LANG_BINDING(getattr(self, "_user_lang", None))
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
        except Exception:
            logger.exception("exam_prep plan LLM call failed")
            return SkillResult(success=False, error="topic_extraction_failed")

        topics_raw = data.get("topics") if isinstance(data, dict) else data
        if not isinstance(topics_raw, list):
            return SkillResult(success=False, error="topic_extraction_malformed")

        chunk_index = {r.chunk_id: r for r in results}
        topics: list[dict] = []
        seen_ids: set[str] = set()
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
            topics.append({
                "id": tid,
                "name": name[:100],
                "weight": weight,
                "source_chunks": source_chunks,
                "questions": [],
                "created_at": _now_iso(),
            })
            seen_ids.add(tid)

        bank["topics"] = topics
        save_bank(course_id, bank)
        return SkillResult(success=True, data={
            "bank": bank, "reused": False, "view": self._compute_view(bank),
        })

    # ── Phase 2: seed questions for a topic ────────────────────────

    async def seed_questions(self, course_id: str, params: dict) -> SkillResult:
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
        system = prompts.EXAM_PREP_SYSTEM
        binding = prompts.USER_LANG_BINDING(getattr(self, "_user_lang", None))
        if binding:
            system = f"{system}\n\n{binding}"
        try:
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
        except Exception:
            logger.exception("exam_prep question gen failed for topic %s", topic.get("id"))
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

    async def next_quiz(self, course_id: str, params: dict) -> SkillResult:
        bank = load_bank(course_id)
        if not bank.get("topics"):
            return SkillResult(success=False, error="no_topics — call action=plan first")

        size = int(params.get("size", QUIZ_DEFAULT_SIZE))
        size = max(1, min(20, size))
        requested_topic_ids = params.get("topic_ids") or None

        candidates = []
        for t in bank["topics"]:
            if requested_topic_ids and t["id"] not in requested_topic_ids:
                continue
            _, _, _, mastered = topic_mastery(t)
            if mastered and not requested_topic_ids:
                continue
            candidates.append(t)

        if not candidates:
            return SkillResult(success=True, data={"questions": [], "total_available": 0, "topic_count": 0, "view": self._compute_view(bank)})

        unseeded = [t for t in candidates if not t.get("questions")]
        if unseeded:
            tasks = [
                self._generate_questions(
                    course_id, t, count=DEFAULT_SEEDS_PER_TYPE,
                    kinds=("multiple_choice", "short_answer"),
                    variant_of=None,
                ) for t in unseeded
            ]
            await asyncio.gather(*tasks, return_exceptions=True)
            save_bank(course_id, bank)

        scored: list[tuple[float, dict, dict]] = []
        for t in candidates:
            for q in t.get("questions", []):
                if q.get("archived") or question_mastered(q):
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

        return SkillResult(success=True, data={
            "questions": picked,
            "total_available": len(scored),
            "topic_count": len(candidates),
            "view": self._compute_view(bank),
        })

    # ── Phase 3b: submit + self-evolution ──────────────────────────

    async def submit_answers(self, course_id: str, params: dict) -> SkillResult:
        bank = load_bank(course_id)
        if not bank.get("topics"):
            return SkillResult(success=False, error="no_topics — call action=plan first")

        answers = params.get("answers") or {}
        if not isinstance(answers, dict):
            return SkillResult(success=False, error="answers_must_be_dict")

        topic_by_qid: dict[str, dict] = {}
        q_by_id: dict[str, dict] = {}
        for t in bank["topics"]:
            for q in t.get("questions", []):
                q_by_id[q["id"]] = q
                topic_by_qid[q["id"]] = t

        wrong_topic_ids: list[str] = []
        wrong_topic_seen: set[str] = set()
        graded: list[dict] = []
        for qid, user_ans in answers.items():
            q = q_by_id.get(qid)
            if q is None:
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
        variants_added: dict[str, int] = {}
        if budget > 0:
            tasks = []
            target_ids = []
            for tid in wrong_topic_ids:
                topic = next((t for t in bank["topics"] if t["id"] == tid), None)
                if topic is None:
                    continue
                # Alternate kinds so variants don't all share the same shape.
                kinds = (("multiple_choice",) if len(topic.get("questions", [])) % 2 == 0
                         else ("short_answer",))
                source_q = next(
                    (q for q in reversed(topic.get("questions", []))
                     if q.get("history") and not q["history"][-1].get("correct")),
                    None,
                )
                tasks.append(self._generate_questions(
                    course_id, topic, count=budget, kinds=kinds,
                    variant_of=(source_q or {}).get("id"),
                ))
                target_ids.append(tid)
            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for tid, r in zip(target_ids, results):
                    if isinstance(r, int) and r > 0:
                        variants_added[tid] = r
                    elif isinstance(r, Exception):
                        logger.warning("variant gen failed for %s: %s", tid, r)

        save_bank(course_id, bank)
        return SkillResult(success=True, data={
            "graded": graded,
            "wrong_topic_count": len(wrong_topic_ids),
            "variant_budget_per_topic": budget,
            "variants_added": variants_added,
            "view": self._compute_view(bank),
        })

    # ── view / reset ───────────────────────────────────────────────

    def view(self, course_id: str) -> SkillResult:
        bank = load_bank(course_id)
        return SkillResult(success=True, data={
            "bank": bank,
            "view": self._compute_view(bank),
        })

    def reset(self, course_id: str) -> SkillResult:
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
        for t in bank.get("topics", []):
            m, total, ratio, is_mastered = topic_mastery(t)
            attempts = 0
            corrects = 0
            for q in t.get("questions", []):
                if q.get("archived"):
                    continue
                hist = q.get("history") or []
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
            })
            total_mastered += m
            total_questions += total
            total_attempts += attempts
            total_correct += corrects
        topics_view.sort(key=lambda x: (-x["weight"], x["name"]))
        overall = total_mastered / total_questions if total_questions else 0.0
        overall_correct = (total_correct / total_attempts) if total_attempts else 0.0
        return {
            "topics": topics_view,
            "total_mastered": total_mastered,
            "total_questions": total_questions,
            "total_attempts": total_attempts,
            "total_correct": total_correct,
            "overall_ratio": overall,
            "overall_correct_rate": overall_correct,
            "mastered_topics": sum(1 for tv in topics_view if tv["is_mastered"]),
            "topic_count": len(topics_view),
        }
