"""Tests for the closed-loop ExamPrepSkill.

The skill owns persistence + LLM dispatch + self-evolution. These tests fake
both the router (canned JSON responses, FIFO) and the KB (chunk lookup),
so we exercise the state machine without any network or sentence-transformer
load.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from nano_notebooklm.skills.exam_prep import (
    DEFAULT_SEEDS_PER_TYPE,
    MASTERED_THRESHOLD,
    PER_TOPIC_VARIANT_CAP,
    TOPIC_MASTERY_MIN_QUESTIONS,
    TOTAL_VARIANT_CAP,
    ExamPrepSkill,
    check_answer,
    load_bank,
    save_bank,
    topic_mastery,
    variant_budget,
)
from nano_notebooklm.types import Chunk, FileType, SearchResult


# ── Fakes ─────────────────────────────────────────────────────────────


class _FakeRouter:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def complete_structured(self, prompt, *, system="", task_type="", **kwargs):
        self.calls.append({"prompt": prompt, "system": system, "task_type": task_type, **kwargs})
        if not self.responses:
            raise RuntimeError("FakeRouter ran out of canned responses")
        return self.responses.pop(0)


class _FakeKB:
    """Minimal KB with the methods exam_prep touches: search, get_chunks, find_chunk."""

    def __init__(self, chunks):
        self._chunks = chunks
        self._by_id = {c.chunk_id: c for c in chunks}

    def search(self, query, top_k=10, course_id=None):
        # Pretend every chunk is a hit, ordered by id stability
        results = [
            SearchResult(
                chunk_id=c.chunk_id, text=c.text, source_file=c.source_file,
                location=c.location, score=0.5, course_id=c.course_id,
            )
            for c in self._chunks
        ]
        return results[:top_k]

    def get_chunks(self, course_id=None):
        return list(self._chunks)

    def find_chunk(self, chunk_id):
        return self._by_id.get(chunk_id)


def _mk_chunk(cid: str, text: str) -> Chunk:
    return Chunk(
        chunk_id=cid, doc_id=cid, course_id="testcourse", text=text,
        file_type=FileType.PDF, source_file="ml.pdf", location="PDF p.1", page=1,
    )


# ── Pure-function tests (no fake needed) ──────────────────────────────


def test_variant_budget_zero_wrong_returns_zero():
    assert variant_budget(0) == 0
    assert variant_budget(-1) == 0


def test_variant_budget_single_wrong_capped_at_per_topic_cap():
    # 20 // 1 = 20, but we cap so a single-topic miss doesn't burn 20 LLM calls
    assert variant_budget(1) == PER_TOPIC_VARIANT_CAP


def test_variant_budget_many_wrong_floors_to_one():
    # 20 // 25 = 0, must round up to 1 so every wrong topic still gets a variant
    assert variant_budget(25) == 1


def test_variant_budget_scales_inversely_with_wrong_count():
    # 5 wrong → 4 each (20/5); 10 wrong → 2 each (20/10)
    assert variant_budget(5) == 4
    assert variant_budget(10) == 2


def test_check_answer_multi_choice_letter_match():
    q = {"type": "multiple_choice", "answer": "B"}
    assert check_answer(q, "B")
    assert check_answer(q, "b")
    assert check_answer(q, "B. some text")
    assert not check_answer(q, "A")
    assert not check_answer(q, "")
    assert not check_answer(q, None)


def test_check_answer_multi_choice_full_answer_in_field():
    """LLM sometimes returns answer as 'B. printf is part of...'; we extract letter."""
    q = {"type": "multiple_choice", "answer": "B. printf is part of the standard I/O library"}
    assert check_answer(q, "B")
    assert not check_answer(q, "A")


def test_check_answer_short_answer_substring_match():
    q = {"type": "short_answer", "answer": "Backpropagation"}
    assert check_answer(q, "backpropagation")
    assert check_answer(q, "Backpropagation")
    assert not check_answer(q, "")
    assert not check_answer(q, "convolution")
    # Substring overlap (≥3 chars) accepted in either direction
    q2 = {"type": "short_answer", "answer": "gradient descent"}
    assert check_answer(q2, "gradient")
    assert check_answer(q2, "gradient descent algorithm")


# ── Bank file persistence ─────────────────────────────────────────────


def test_load_bank_returns_empty_for_missing_file(isolated_artifacts):
    bank = load_bank("ghost-course")
    assert bank["version"] == 1
    assert bank["topics"] == []
    assert bank["course_id"] == "ghost-course"


def test_save_then_load_roundtrip(isolated_artifacts):
    bank = load_bank("c1")
    bank["topics"].append({"id": "topic_x", "name": "X", "weight": 0.5, "questions": []})
    save_bank("c1", bank)
    reread = load_bank("c1")
    assert reread["topics"][0]["name"] == "X"
    assert (isolated_artifacts / "courses" / "c1" / "exam_bank.json").exists()


def test_load_bank_recovers_from_malformed_json(isolated_artifacts):
    path = isolated_artifacts / "courses" / "c1" / "exam_bank.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not valid json")
    bank = load_bank("c1")
    assert bank["topics"] == []


def test_load_bank_future_version_raises_instead_of_wiping(isolated_artifacts):
    """fix-all v1 H6: a bank written by a newer client must NOT be silently
    overwritten when an older binary loads it. Pre-fix, any version mismatch
    returned an empty bank and the next mutating action saved the empty
    version=1 envelope on top → user data gone. Now we raise so the API
    surfaces 502/409."""
    from nano_notebooklm.skills.exam_prep import BankVersionTooNewError

    path = isolated_artifacts / "courses" / "c1" / "exam_bank.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 99, "topics": [{"id": "old"}]}))
    with pytest.raises(BankVersionTooNewError):
        load_bank("c1")
    # The file is untouched (we don't downgrade).
    assert json.loads(path.read_text())["version"] == 99


def test_load_bank_missing_version_returns_empty(isolated_artifacts):
    """A bank without a `version` field (corrupt write?) falls back to empty
    — recoverable, since this is most likely a pre-versioned dev file."""
    path = isolated_artifacts / "courses" / "c1" / "exam_bank.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"topics": [{"id": "old"}]}))
    bank = load_bank("c1")
    assert bank["topics"] == []
    assert bank["version"] == 1


# ── Mastery semantics ────────────────────────────────────────────────


def test_topic_mastery_empty_topic_is_not_mastered():
    m, total, ratio, is_mastered = topic_mastery({"questions": []})
    assert (m, total, is_mastered) == (0, 0, False)


def test_topic_mastery_requires_min_questions():
    """A topic with 1 mastered question can't be 'mastered' overall — we
    require ≥ TOPIC_MASTERY_MIN_QUESTIONS so a tiny topic doesn't claim
    full mastery."""
    topic = {"questions": [{"mastered": True}]}
    _, _, _, is_mastered = topic_mastery(topic)
    assert not is_mastered


def test_topic_mastery_passes_above_threshold():
    qs = [{"mastered": True} for _ in range(TOPIC_MASTERY_MIN_QUESTIONS)]
    _, _, ratio, is_mastered = topic_mastery({"questions": qs})
    assert is_mastered
    assert ratio == pytest.approx(1.0)


def test_archived_questions_excluded_from_mastery():
    qs = [{"mastered": True} for _ in range(2)] + [{"archived": True, "mastered": True}]
    m, total, _, _ = topic_mastery({"questions": qs})
    assert total == 2  # archived skipped
    assert m == 2


# ── Skill state-machine integration ──────────────────────────────────


@pytest.mark.asyncio
async def test_plan_topics_persists_topics_and_returns_view(isolated_artifacts):
    chunks = [_mk_chunk(f"c{i}", f"Concept {i} explanation about backprop.") for i in range(5)]
    router = _FakeRouter([{
        "topics": [
            {"name": "Backprop", "weight": 0.9, "source_chunks": ["c0", "c1"]},
            {"name": "Convolutions", "weight": 0.7, "source_chunks": ["c2"]},
            {"name": "Optimization", "weight": 0.5, "source_chunks": ["c3"]},
        ]
    }])
    skill = ExamPrepSkill(kb=_FakeKB(chunks), router=router)

    res = await skill.execute({"action": "plan", "course_id": "c1"})
    assert res.success
    assert len(res.data["bank"]["topics"]) == 3
    names = [t["name"] for t in res.data["bank"]["topics"]]
    assert "Backprop" in names
    assert res.data["view"]["topic_count"] == 3
    # Re-plan without force reuses
    res2 = await skill.execute({"action": "plan", "course_id": "c1"})
    assert res2.data["reused"]
    assert len(router.calls) == 1  # No second LLM call


@pytest.mark.asyncio
async def test_plan_topics_force_regenerates(isolated_artifacts):
    chunks = [_mk_chunk("c0", "Concept text")]
    router = _FakeRouter([
        {"topics": [{"name": "T1", "weight": 0.5, "source_chunks": ["c0"]}]},
        {"topics": [{"name": "T2", "weight": 0.5, "source_chunks": ["c0"]}]},
    ])
    skill = ExamPrepSkill(kb=_FakeKB(chunks), router=router)
    await skill.execute({"action": "plan", "course_id": "c1"})
    res2 = await skill.execute({"action": "plan", "course_id": "c1", "force": True})
    assert res2.data["reused"] is False
    assert res2.data["bank"]["topics"][0]["name"] == "T2"


@pytest.mark.asyncio
async def test_seed_questions_appends_and_dedups(isolated_artifacts):
    chunks = [_mk_chunk("c0", "Backprop explanation.")]
    plan_resp = {"topics": [{"name": "Backprop", "weight": 0.8, "source_chunks": ["c0"]}]}
    seed_resp = {"questions": [
        {"type": "multiple_choice", "prompt": "What is backprop?",
         "options": ["A. chain rule", "B. random", "C. luck", "D. magic"],
         "answer": "A", "explanation": "Chain rule.", "difficulty": "easy"},
        {"type": "short_answer", "prompt": "Define backpropagation.",
         "answer": "An algorithm that propagates loss gradients backwards through the network.",
         "difficulty": "medium"},
    ]}
    # Plan call + seed call (only one topic so one seed call regardless of kinds)
    router = _FakeRouter([plan_resp, seed_resp])
    skill = ExamPrepSkill(kb=_FakeKB(chunks), router=router)

    await skill.execute({"action": "plan", "course_id": "c1"})
    res = await skill.execute({"action": "seed", "course_id": "c1"})
    assert res.success
    topic = load_bank("c1")["topics"][0]
    assert len(topic["questions"]) == 2
    assert topic["questions"][0]["type"] == "multiple_choice"
    assert topic["questions"][1]["type"] == "short_answer"


@pytest.mark.asyncio
async def test_next_quiz_excludes_mastered_questions(isolated_artifacts):
    """Mastered question's `mastered=True` flag → must be filtered out
    even though it's the only one for its topic. The other topic's question
    is the only valid pick."""
    chunks = [_mk_chunk("c0", "ml content")]
    router = _FakeRouter([])  # No LLM needed — bank pre-seeded
    skill = ExamPrepSkill(kb=_FakeKB(chunks), router=router)

    # Manually seed a bank: one mastered question (excluded), one fresh
    bank = load_bank("c1")
    bank["topics"] = [
        {
            "id": "topic_a", "name": "A", "weight": 0.9, "source_chunks": [{"chunk_id": "c0"}],
            "questions": [
                {"id": "q_done", "type": "short_answer", "prompt": "p1", "answer": "ans1",
                 "mastered": True, "consecutive_correct": 3, "history": [], "archived": False},
                {"id": "q_fresh", "type": "short_answer", "prompt": "p2", "answer": "ans2",
                 "mastered": False, "consecutive_correct": 0, "history": [], "archived": False},
            ],
        }
    ]
    save_bank("c1", bank)

    res = await skill.execute({"action": "next_quiz", "course_id": "c1", "size": 5})
    assert res.success
    qids = [q["id"] for q in res.data["questions"]]
    assert "q_fresh" in qids
    assert "q_done" not in qids


@pytest.mark.asyncio
async def test_submit_marks_correct_advances_streak_and_masters(isolated_artifacts):
    """3 consecutive correct answers should flip mastered=True."""
    chunks = [_mk_chunk("c0", "x")]
    router = _FakeRouter([])
    skill = ExamPrepSkill(kb=_FakeKB(chunks), router=router)

    bank = load_bank("c1")
    bank["topics"] = [{
        "id": "topic_a", "name": "A", "weight": 0.5, "source_chunks": [],
        "questions": [{
            "id": "q_streak", "type": "short_answer", "prompt": "p", "answer": "alpha",
            "mastered": False, "consecutive_correct": 0, "history": [], "archived": False,
        }]
    }]
    save_bank("c1", bank)

    for _ in range(MASTERED_THRESHOLD):
        res = await skill.execute({
            "action": "submit", "course_id": "c1",
            "answers": {"q_streak": "alpha"},
        })
        assert res.success

    q = load_bank("c1")["topics"][0]["questions"][0]
    assert q["mastered"] is True
    assert q["consecutive_correct"] == MASTERED_THRESHOLD


@pytest.mark.asyncio
async def test_submit_wrong_answer_resets_streak(isolated_artifacts):
    chunks = [_mk_chunk("c0", "x")]
    router = _FakeRouter([
        # one variant generation call for the wrong topic
        {"questions": [{
            "type": "short_answer", "prompt": "fresh angle on alpha",
            "answer": "alpha", "difficulty": "medium",
        }]},
    ])
    skill = ExamPrepSkill(kb=_FakeKB(chunks), router=router)

    bank = load_bank("c1")
    bank["topics"] = [{
        "id": "topic_a", "name": "A", "weight": 0.5, "source_chunks": [{"chunk_id": "c0"}],
        "questions": [{
            "id": "q_streak", "type": "short_answer", "prompt": "p", "answer": "alpha",
            "mastered": False, "consecutive_correct": 2, "history": [], "archived": False,
        }]
    }]
    save_bank("c1", bank)

    res = await skill.execute({
        "action": "submit", "course_id": "c1",
        "answers": {"q_streak": "wrongthing"},
    })
    assert res.success
    q = load_bank("c1")["topics"][0]["questions"][0]
    assert q["consecutive_correct"] == 0
    assert not q["mastered"]
    # History recorded
    assert q["history"][-1]["correct"] is False


@pytest.mark.asyncio
async def test_submit_wrong_triggers_variant_generation(isolated_artifacts):
    """One wrong topic should fire exactly one variant-gen LLM call,
    appending fresh questions to that topic only."""
    chunks = [_mk_chunk("c0", "ml")]
    variant_resp = {"questions": [
        {"type": "multiple_choice", "prompt": "Fresh angle Q1",
         "options": ["A. x", "B. y", "C. z", "D. w"], "answer": "B"},
        {"type": "multiple_choice", "prompt": "Fresh angle Q2",
         "options": ["A. p", "B. q", "C. r", "D. s"], "answer": "C"},
    ]}
    router = _FakeRouter([variant_resp])
    skill = ExamPrepSkill(kb=_FakeKB(chunks), router=router)

    bank = load_bank("c1")
    bank["topics"] = [
        {  # WRONG topic
            "id": "topic_wrong", "name": "Backprop", "weight": 0.9,
            "source_chunks": [{"chunk_id": "c0"}],
            "questions": [{
                "id": "q1", "type": "multiple_choice", "prompt": "?",
                "options": ["A", "B", "C", "D"], "answer": "A",
                "history": [], "consecutive_correct": 0, "mastered": False, "archived": False,
            }]
        },
        {  # NOT involved in this submit
            "id": "topic_other", "name": "Conv", "weight": 0.5,
            "source_chunks": [{"chunk_id": "c0"}],
            "questions": [{
                "id": "q2", "type": "short_answer", "prompt": "?",
                "answer": "x", "history": [], "consecutive_correct": 0,
                "mastered": False, "archived": False,
            }]
        }
    ]
    save_bank("c1", bank)

    res = await skill.execute({
        "action": "submit", "course_id": "c1",
        "answers": {"q1": "D"},  # wrong (correct is A)
    })
    assert res.success
    assert res.data["wrong_topic_count"] == 1
    assert res.data["variants_added"]["topic_wrong"] == 2
    # Only 1 LLM call (one wrong topic, no call for the other)
    assert len(router.calls) == 1

    reloaded = load_bank("c1")
    wrong_topic = reloaded["topics"][0]
    other_topic = reloaded["topics"][1]
    assert len(wrong_topic["questions"]) == 3  # 1 original + 2 variants
    assert len(other_topic["questions"]) == 1  # untouched
    # Variant provenance recorded
    new_qs = [q for q in wrong_topic["questions"] if q["id"] != "q1"]
    assert all(q["variant_of"] == "q1" for q in new_qs)


@pytest.mark.asyncio
async def test_submit_all_correct_no_variants_generated(isolated_artifacts):
    chunks = [_mk_chunk("c0", "ml")]
    router = _FakeRouter([])  # No variant gen
    skill = ExamPrepSkill(kb=_FakeKB(chunks), router=router)

    bank = load_bank("c1")
    bank["topics"] = [{
        "id": "topic_a", "name": "A", "weight": 0.5, "source_chunks": [],
        "questions": [{
            "id": "q1", "type": "short_answer", "prompt": "?", "answer": "alpha",
            "history": [], "consecutive_correct": 0, "mastered": False, "archived": False,
        }]
    }]
    save_bank("c1", bank)

    res = await skill.execute({
        "action": "submit", "course_id": "c1", "answers": {"q1": "alpha"},
    })
    assert res.success
    assert res.data["wrong_topic_count"] == 0
    assert res.data["variants_added"] == {}
    assert len(router.calls) == 0


@pytest.mark.asyncio
async def test_reset_wipes_bank(isolated_artifacts):
    skill = ExamPrepSkill(kb=_FakeKB([]), router=_FakeRouter([]))
    bank = load_bank("c1")
    bank["topics"] = [{"id": "x", "name": "X", "weight": 0.5, "questions": []}]
    save_bank("c1", bank)
    path = isolated_artifacts / "courses" / "c1" / "exam_bank.json"
    assert path.exists()

    res = await skill.execute({"action": "reset", "course_id": "c1"})
    assert res.success
    assert not path.exists()


@pytest.mark.asyncio
async def test_view_exposes_attempt_count_and_correct_rate(isolated_artifacts):
    """Topic cards looked frozen after one submit because the view only
    surfaced mastery counts (which need 3 consecutive correct to flip).
    Pin the per-topic + overall attempt/correct fields so UI gets immediate
    feedback after every quiz round."""
    skill = ExamPrepSkill(kb=_FakeKB([]), router=_FakeRouter([]))
    bank = load_bank("c1")
    bank["topics"] = [
        {"id": "t1", "name": "T1", "weight": 0.9, "questions": [
            {
                "id": "qa", "type": "short_answer", "answer": "alpha",
                "history": [
                    {"correct": True}, {"correct": False}, {"correct": True},
                ],
                "consecutive_correct": 1, "mastered": False, "archived": False,
            },
            {
                "id": "qb", "type": "short_answer", "answer": "beta",
                "history": [{"correct": False}],
                "consecutive_correct": 0, "mastered": False, "archived": False,
            },
        ]},
    ]
    save_bank("c1", bank)

    res = await skill.execute({"action": "view", "course_id": "c1"})
    v = res.data["view"]
    topic = v["topics"][0]
    assert topic["attempt_count"] == 4
    assert topic["correct_count"] == 2
    assert topic["correct_rate"] == pytest.approx(0.5)
    # Overall rollups
    assert v["total_attempts"] == 4
    assert v["total_correct"] == 2
    assert v["overall_correct_rate"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_view_returns_compute_view_shape(isolated_artifacts):
    skill = ExamPrepSkill(kb=_FakeKB([]), router=_FakeRouter([]))
    bank = load_bank("c1")
    bank["topics"] = [
        {"id": "t1", "name": "T1", "weight": 0.9, "questions": [
            {"id": "qa", "mastered": True, "history": [], "consecutive_correct": 3, "archived": False, "type": "short_answer", "answer": ""},
            {"id": "qb", "mastered": False, "history": [], "consecutive_correct": 0, "archived": False, "type": "short_answer", "answer": ""},
        ]},
        {"id": "t2", "name": "T2", "weight": 0.5, "questions": []},
    ]
    save_bank("c1", bank)

    res = await skill.execute({"action": "view", "course_id": "c1"})
    assert res.success
    v = res.data["view"]
    assert v["total_questions"] == 2
    assert v["total_mastered"] == 1
    assert v["topic_count"] == 2
    # Topics sorted by descending weight
    assert v["topics"][0]["id"] == "t1"


@pytest.mark.asyncio
async def test_unknown_action_returns_error(isolated_artifacts):
    skill = ExamPrepSkill(kb=_FakeKB([]), router=_FakeRouter([]))
    res = await skill.execute({"action": "bogus", "course_id": "c1"})
    assert not res.success
    assert "unknown action" in res.error


@pytest.mark.asyncio
async def test_user_lang_threaded_into_system_prompt(isolated_artifacts):
    """fix-all v1 H1: user_lang must reach the LLM system prompt via the
    USER_LANG_BINDING addendum. The OLD bug was that user_lang lived on
    `self._user_lang` and raced across concurrent requests on the singleton
    skill. The fix is to thread it as a parameter; this test pins that the
    parameter actually flows through to `complete_structured`'s system kwarg.
    """
    class _CapturingRouter:
        def __init__(self):
            self.calls = []
        async def complete_structured(self, prompt, *, system="", task_type="", **kw):
            self.calls.append({"system": system, "task_type": task_type})
            return {"topics": [{"name": "T1", "weight": 0.5, "source_chunks": ["c0"]}]}

    chunks = [_mk_chunk("c0", "content")]
    router = _CapturingRouter()
    skill = ExamPrepSkill(kb=_FakeKB(chunks), router=router)

    res = await skill.execute({"action": "plan", "course_id": "c1", "user_lang": "zh"})
    assert res.success
    # The system kwarg must include the USER_LANG_BINDING zh text. The
    # binding text starts with "User language preference: Reply ONLY in zh".
    assert any("Reply ONLY in zh" in c["system"] for c in router.calls), \
        f"zh binding missing from system prompts: {router.calls}"


@pytest.mark.asyncio
async def test_user_lang_does_not_leak_across_concurrent_requests(isolated_artifacts):
    """fix-all v1 H1 regression: pre-fix, two concurrent calls on the same
    singleton skill would race `self._user_lang`. We now pass it as a
    method parameter so the captured system prompt MUST track the request's
    own lang, regardless of interleaving.
    """
    class _SlowCapturingRouter:
        def __init__(self):
            self.calls = []
            self.first_started = asyncio.Event()
            self.allow_finish = asyncio.Event()
        async def complete_structured(self, prompt, *, system="", task_type="", **kw):
            self.calls.append({"system": system})
            self.first_started.set()
            await self.allow_finish.wait()
            return {"topics": [{"name": "T1", "weight": 0.5, "source_chunks": ["c0"]}]}

    chunks = [_mk_chunk("c0", "content")]
    router = _SlowCapturingRouter()
    skill = ExamPrepSkill(kb=_FakeKB(chunks), router=router)

    # Fire request A (zh) — it'll await inside complete_structured.
    task_a = asyncio.create_task(skill.execute({
        "action": "plan", "course_id": "course_a", "user_lang": "zh",
    }))
    await router.first_started.wait()
    # Now fire request B (en) on a different course so the lock doesn't
    # serialise it. This is exactly the race that pre-fix corrupted
    # self._user_lang.
    router.first_started.clear()
    task_b = asyncio.create_task(skill.execute({
        "action": "plan", "course_id": "course_b", "user_lang": "en",
    }))
    await router.first_started.wait()
    # Both stalled inside the LLM call. Let them complete.
    router.allow_finish.set()
    res_a, res_b = await asyncio.gather(task_a, task_b)
    assert res_a.success and res_b.success
    # The two captured systems must each carry their OWN binding.
    assert len(router.calls) == 2
    assert "Reply ONLY in zh" in router.calls[0]["system"]
    assert "Reply ONLY in en" in router.calls[1]["system"]


@pytest.mark.asyncio
async def test_concurrent_submits_serialize_via_per_course_lock(isolated_artifacts):
    """fix-all v1 H2: two concurrent submits on the same course MUST
    serialise so neither's grading history is lost to last-writer-wins.
    Pre-fix, both load_bank → mutate → save_bank with no lock → the later
    save_bank overwrote the earlier's grading.
    """
    class _SlowRouter:
        async def complete_structured(self, *a, **kw):
            await asyncio.sleep(0)
            return {"questions": []}

    chunks = [_mk_chunk("c0", "content")]
    skill = ExamPrepSkill(kb=_FakeKB(chunks), router=_SlowRouter())

    bank = load_bank("c1")
    bank["topics"] = [{
        "id": "topic_a", "name": "A", "weight": 0.5, "source_chunks": [],
        "questions": [
            {"id": "q1", "type": "short_answer", "prompt": "p1", "answer": "alpha",
             "history": [], "consecutive_correct": 0, "mastered": False, "archived": False},
            {"id": "q2", "type": "short_answer", "prompt": "p2", "answer": "beta",
             "history": [], "consecutive_correct": 0, "mastered": False, "archived": False},
        ],
    }]
    save_bank("c1", bank)

    # Two concurrent submits, each grading one different question.
    t1 = skill.execute({"action": "submit", "course_id": "c1", "answers": {"q1": "alpha"}})
    t2 = skill.execute({"action": "submit", "course_id": "c1", "answers": {"q2": "beta"}})
    res1, res2 = await asyncio.gather(t1, t2)
    assert res1.success and res2.success

    # Both histories MUST land in the bank — neither submit's grading was
    # overwritten by the other.
    final = load_bank("c1")
    qs = {q["id"]: q for q in final["topics"][0]["questions"]}
    assert len(qs["q1"]["history"]) == 1, f"q1 grading lost: {qs['q1']['history']}"
    assert len(qs["q2"]["history"]) == 1, f"q2 grading lost: {qs['q2']['history']}"


@pytest.mark.asyncio
async def test_re_extract_preserves_questions_by_normalized_name(isolated_artifacts):
    """fix-all v1 H4: a force=True re-extract used to drop all questions
    whose topic name shifted (even slightly). Now matching-by-normalized-name
    carries the question history forward."""
    chunks = [_mk_chunk("c0", "content")]
    router = _FakeRouter([
        {"topics": [{"name": "Backpropagation", "weight": 0.9, "source_chunks": ["c0"]}]},
        # Second plan emits a normalized-equivalent name with trailing dot.
        {"topics": [{"name": "Backpropagation.", "weight": 0.9, "source_chunks": ["c0"]}]},
    ])
    skill = ExamPrepSkill(kb=_FakeKB(chunks), router=router)

    await skill.execute({"action": "plan", "course_id": "c1"})
    bank = load_bank("c1")
    bank["topics"][0]["questions"] = [{
        "id": "q_preserved", "type": "short_answer", "prompt": "?", "answer": "x",
        "history": [{"correct": True}, {"correct": True}], "consecutive_correct": 2,
        "mastered": False, "archived": False,
    }]
    save_bank("c1", bank)

    res = await skill.execute({"action": "plan", "course_id": "c1", "force": True})
    assert res.success
    final = load_bank("c1")
    # Same topic count (no archive bucket added).
    assert all(not t.get("archived_topic") for t in final["topics"])
    # Questions carried forward into the new topic.
    new_topic = final["topics"][0]
    assert any(q["id"] == "q_preserved" for q in new_topic["questions"])
    assert res.data["migrated_topic_count"] == 1
    assert res.data["orphan_question_count"] == 0


@pytest.mark.asyncio
async def test_re_extract_archives_orphan_questions_when_name_drifts(isolated_artifacts):
    """fix-all v1 H4: a name drift that DOESN'T normalize-match (different
    word entirely) must archive the old questions into a `_archive_*` bucket
    instead of silently dropping them."""
    chunks = [_mk_chunk("c0", "content")]
    router = _FakeRouter([
        {"topics": [{"name": "OldName", "weight": 0.5, "source_chunks": ["c0"]}]},
        {"topics": [{"name": "EntirelyDifferent", "weight": 0.5, "source_chunks": ["c0"]}]},
    ])
    skill = ExamPrepSkill(kb=_FakeKB(chunks), router=router)

    await skill.execute({"action": "plan", "course_id": "c1"})
    bank = load_bank("c1")
    bank["topics"][0]["questions"] = [{
        "id": "q_orphan", "type": "short_answer", "prompt": "?", "answer": "x",
        "history": [{"correct": True}], "consecutive_correct": 1,
        "mastered": False, "archived": False,
    }]
    save_bank("c1", bank)

    res = await skill.execute({"action": "plan", "course_id": "c1", "force": True})
    assert res.success
    final = load_bank("c1")
    archives = [t for t in final["topics"] if t.get("archived_topic")]
    assert len(archives) == 1
    assert any(q["id"] == "q_orphan" for q in archives[0]["questions"])
    assert res.data["orphan_question_count"] == 1


@pytest.mark.asyncio
async def test_next_quiz_returns_reason_for_empty_result(isolated_artifacts):
    """fix-all v1 M6: pre-fix, an LLM-generation-fail and an all-mastered
    state both rendered as the same misleading "fully mastered" message.
    Now the skill ships an explicit `reason` field."""
    class _FailRouter:
        async def complete_structured(self, *a, **kw):
            raise RuntimeError("simulated LLM failure")

    chunks = [_mk_chunk("c0", "content")]
    skill = ExamPrepSkill(kb=_FakeKB(chunks), router=_FailRouter())
    bank = load_bank("c1")
    # One topic, NO questions yet, so next_quiz will try to seed (and fail).
    bank["topics"] = [{
        "id": "topic_a", "name": "A", "weight": 0.5,
        "source_chunks": [{"chunk_id": "c0"}], "questions": [],
    }]
    save_bank("c1", bank)

    res = await skill.execute({"action": "next_quiz", "course_id": "c1"})
    assert res.success
    assert res.data["questions"] == []
    assert res.data["reason"] == "generation_failed"


@pytest.mark.asyncio
async def test_next_quiz_include_mastered_when_explicit_topic_ids(isolated_artifacts):
    """fix-all v1 M7: explicit topic drill-down should re-include mastered
    questions for review. Default sampling continues to exclude them."""
    skill = ExamPrepSkill(kb=_FakeKB([]), router=_FakeRouter([]))
    bank = load_bank("c1")
    bank["topics"] = [{
        "id": "topic_a", "name": "A", "weight": 0.5, "source_chunks": [],
        "questions": [
            {"id": "q_mastered", "type": "short_answer", "prompt": "p1", "answer": "a",
             "history": [], "consecutive_correct": 3, "mastered": True, "archived": False},
            {"id": "q_fresh", "type": "short_answer", "prompt": "p2", "answer": "b",
             "history": [], "consecutive_correct": 0, "mastered": False, "archived": False},
        ],
    }]
    save_bank("c1", bank)

    default_res = await skill.execute({"action": "next_quiz", "course_id": "c1"})
    default_qids = [q["id"] for q in default_res.data["questions"]]
    assert "q_mastered" not in default_qids
    assert "q_fresh" in default_qids

    explicit_res = await skill.execute({
        "action": "next_quiz", "course_id": "c1", "topic_ids": ["topic_a"],
    })
    explicit_qids = [q["id"] for q in explicit_res.data["questions"]]
    assert "q_mastered" in explicit_qids


@pytest.mark.asyncio
async def test_submit_does_not_hang_when_variant_gen_stalls(isolated_artifacts, monkeypatch):
    """The original bug: a stuck codex call made `asyncio.gather` wait
    forever → frontend fetch had no default timeout → spinner ran
    indefinitely. Per-call `asyncio.wait_for(timeout=EXAM_PREP_LLM_TIMEOUT_S)`
    keeps total submit time bounded. Here we slash the timeout to 0.3 s and
    feed a router that sleeps 5 s — submit must complete in well under 5 s
    with `variants_added` empty (variant gen timed out → swallowed)."""
    import asyncio
    from nano_notebooklm.skills import exam_prep as ep

    monkeypatch.setattr(ep, "EXAM_PREP_LLM_TIMEOUT_S", 0.3)

    class _StallRouter:
        calls = 0
        async def complete_structured(self, *_a, **_k):
            type(self).calls += 1
            await asyncio.sleep(5.0)  # never returns in time
            return {"questions": []}

    chunks = [_mk_chunk("c0", "ml")]
    skill = ExamPrepSkill(kb=_FakeKB(chunks), router=_StallRouter())

    bank = load_bank("c1")
    bank["topics"] = [{
        "id": "topic_a", "name": "A", "weight": 0.5,
        "source_chunks": [{"chunk_id": "c0"}],
        "questions": [{
            "id": "q1", "type": "short_answer", "prompt": "?", "answer": "alpha",
            "history": [], "consecutive_correct": 0, "mastered": False, "archived": False,
        }],
    }]
    save_bank("c1", bank)

    start = asyncio.get_event_loop().time()
    res = await skill.execute({
        "action": "submit", "course_id": "c1",
        "answers": {"q1": "wrongthing"},
    })
    elapsed = asyncio.get_event_loop().time() - start

    assert res.success
    assert res.data["wrong_topic_count"] == 1
    assert res.data["variants_added"] == {}  # timeout swallowed all variant gens
    # Generous upper bound: timeout 0.3s + scheduling slack. Without the fix
    # this assertion would fire after 5+ seconds.
    assert elapsed < 2.0, f"submit took {elapsed:.2f}s — timeout not enforced"
