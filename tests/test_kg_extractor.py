"""M1 — Two-stage KG extraction tests.

Stage A (macro): one LLM call producing course overview + 5-9 macro topics.
Stage B (micro): per-chunk concept extraction with topics passed in,
each concept attached to a `parent_topic` (referencing topic concept_id).

All tests offline. The router is a hand-rolled stub that records calls
and returns canned structured-JSON responses, mirroring the discipline
used in tests/test_router_intent.py and tests/test_agents.py.
"""

from __future__ import annotations

import json

import pytest

from nano_notebooklm.types import Chunk, FileType


# ── Fakes ──────────────────────────────────────────────────────────


def _chunk(cid: str, text: str, src: str = "ml.pdf", page: int = 1) -> Chunk:
    return Chunk(
        chunk_id=cid,
        doc_id="d1",
        course_id="testcourse",
        text=text,
        file_type=FileType.PDF,
        source_file=src,
        location=f"PDF p.{page}",
        page=page,
    )


class _FakeRouter:
    """Stand-in for ModelRouter.complete_structured.

    Returns canned dicts in FIFO order. If `responses` runs out, the next
    call raises so tests catch under-stubbing instead of silently looping.
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def complete_structured(self, prompt, *, system: str = "", task_type: str = "", **kwargs):
        self.calls.append({"prompt": prompt, "system": system, "task_type": task_type, **kwargs})
        if not self.responses:
            raise RuntimeError("FakeRouter ran out of canned responses")
        return self.responses.pop(0)


class _RaisingRouter:
    def __init__(self, exc: Exception):
        self.exc = exc
        self.calls = 0

    async def complete_structured(self, *_a, **_k):
        self.calls += 1
        raise self.exc


# ── Stage A — macro topics ──────────────────────────────────────────


async def test_extract_macro_topics_happy():
    """Stage A returns course_overview + 5-9 topic concepts at depth=1."""
    from nano_notebooklm.kg.extractor import extract_course_overview_and_topics

    chunks = [
        _chunk(f"c{i}", f"Chunk {i} introduces concept {i} for deep learning frameworks.")
        for i in range(20)
    ]
    router = _FakeRouter([
        {
            "course_overview": "An introduction to deep learning frameworks.",
            "topics": [
                {"name": "Tensor operations", "summary": "Scalars, vectors, n-d arrays.", "weight": 8},
                {"name": "Automatic differentiation", "summary": "Backprop on the graph.", "weight": 7},
                {"name": "GPU acceleration", "summary": "BLAS / cuBLAS / cuDNN.", "weight": 6},
                {"name": "Computation graph", "summary": "Static vs dynamic.", "weight": 6},
                {"name": "Optimization", "summary": "SGD, Adam.", "weight": 5},
            ],
        }
    ])

    overview, topics, _prereq = await extract_course_overview_and_topics(
        course_id="testcourse",
        course_name="testcourse",
        source_files=["ml.pdf", "ml2.pdf"],
        sample_chunks=chunks,
        router=router,
    )

    assert "deep learning" in overview.lower()
    assert 5 <= len(topics) <= 9
    assert all(t.depth == 1 for t in topics)
    assert all(t.concept_type == "topic" for t in topics)
    # Topic ids deterministic, course-prefixed, distinct.
    ids = [t.concept_id for t in topics]
    assert all(i.startswith("topic_testcourse_") for i in ids)
    assert len(set(ids)) == len(ids)
    # Stage A must call the LLM exactly once with course_name in prompt.
    assert len(router.calls) == 1
    assert "testcourse" in router.calls[0]["prompt"]


async def test_extract_macro_topics_clamps_oversized_response():
    """Edge: LLM returns 15 topics → trim to 9 (upper bound)."""
    from nano_notebooklm.kg.extractor import extract_course_overview_and_topics

    router = _FakeRouter([{
        "course_overview": "x",
        "topics": [{"name": f"T{i}", "summary": "s", "weight": 1} for i in range(15)],
    }])
    _, topics, _ = await extract_course_overview_and_topics(
        course_id="c", course_name="c",
        source_files=["a.pdf"], sample_chunks=[_chunk("c1", "x")], router=router,
    )
    assert len(topics) <= 9


async def test_extract_macro_topics_falls_back_when_llm_fails():
    """Corner: Stage A LLM raises → ('', []) gracefully (no crash)."""
    from nano_notebooklm.kg.extractor import extract_course_overview_and_topics

    router = _RaisingRouter(RuntimeError("upstream 502"))
    overview, topics, prereq = await extract_course_overview_and_topics(
        course_id="c", course_name="c",
        source_files=["a.pdf"], sample_chunks=[_chunk("c1", "x")], router=router,
    )
    assert overview == ""
    assert topics == []
    assert prereq == []
    assert router.calls == 1


async def test_extract_macro_topics_empty_corpus():
    """Corner: zero chunks → no LLM call, returns ('', [])."""
    from nano_notebooklm.kg.extractor import extract_course_overview_and_topics

    router = _FakeRouter([])
    overview, topics, prereq = await extract_course_overview_and_topics(
        course_id="c", course_name="c",
        source_files=[], sample_chunks=[], router=router,
    )
    assert overview == ""
    assert topics == []
    assert prereq == []
    assert router.calls == []  # no LLM call at all


# ── Stage B — concept extraction with parent_topic ──────────────────


async def test_extract_chunk_attaches_parent_topic():
    """Chunk-level extraction receives topics, attaches each concept to a topic id."""
    from nano_notebooklm.kg.extractor import extract_concepts_from_chunk
    from nano_notebooklm.types import Concept

    topics = [
        Concept(
            concept_id="topic_testcourse_tensor_ops",
            name="Tensor operations",
            definition="...",
            concept_type="topic",
            course_ids=["testcourse"],
            depth=1,
            weight=8.0,
        ),
        Concept(
            concept_id="topic_testcourse_autograd",
            name="Automatic differentiation",
            definition="...",
            concept_type="topic",
            course_ids=["testcourse"],
            depth=1,
            weight=7.0,
        ),
    ]
    router = _FakeRouter([
        {
            "concepts": [
                {"name": "Backpropagation", "definition": "...", "type": "algorithm",
                 "parent_topic": "Automatic differentiation"},
                {"name": "Tensor", "definition": "...", "type": "definition",
                 "parent_topic": "Tensor operations"},
            ],
            "relations": [
                {"source": "Tensor", "target": "Backpropagation", "type": "related"},
            ],
        }
    ])
    chunk = _chunk("c1", "Backpropagation propagates gradients through tensor ops.")

    concepts, relations = await extract_concepts_from_chunk(
        chunk, "testcourse", router, topics=topics,
    )

    assert len(concepts) == 2
    # Every leaf concept carries parent_topic referencing a real topic id.
    for c in concepts:
        assert c.depth >= 2
        assert c.parent_topic in {t.concept_id for t in topics}, (
            f"{c.name} has parent_topic={c.parent_topic!r}, expected one of topic ids"
        )
    # The topics list itself was injected into the prompt so the LLM has scope.
    prompt = router.calls[0]["prompt"]
    assert "Automatic differentiation" in prompt
    assert "Tensor operations" in prompt


async def test_extract_chunk_unmatched_parent_topic_drops_to_none():
    """Corner: LLM hallucinates a topic name not in the provided list →
    concept is still kept but parent_topic is None (router will mount it
    as orphan under the course root, never under a wrong topic)."""
    from nano_notebooklm.kg.extractor import extract_concepts_from_chunk
    from nano_notebooklm.types import Concept

    topics = [
        Concept(concept_id="topic_c_a", name="Topic A", definition="", concept_type="topic",
                course_ids=["c"], depth=1, weight=1.0),
    ]
    router = _FakeRouter([{
        "concepts": [
            {"name": "Concept Z", "definition": "...", "type": "definition",
             "parent_topic": "Bogus Hallucinated Topic"},
        ],
        "relations": [],
    }])
    chunk = _chunk("c1", "x")

    concepts, _ = await extract_concepts_from_chunk(chunk, "c", router, topics=topics)

    assert len(concepts) == 1
    assert concepts[0].parent_topic is None


# ── End-to-end orchestration ────────────────────────────────────────


async def test_extract_from_chunks_two_stage_builds_root_and_topics():
    """End-to-end: Stage A → 2 topics + Stage B → 1 leaf each → returned
    Concept list contains course root (depth=0) + 2 topics (depth=1) + leaves
    (depth>=2), and Relation list contains part-of edges leaf→topic and
    topic→root."""
    from nano_notebooklm.kg.extractor import extract_from_chunks

    stage_a = {
        "course_overview": "Course about X.",
        "topics": [
            {"name": "Topic Alpha", "summary": "sumA", "weight": 5},
            {"name": "Topic Beta", "summary": "sumB", "weight": 5},
        ],
    }
    stage_b_each = {
        "concepts": [{
            "name": "Concept-{n}", "definition": "...", "type": "definition",
            "parent_topic": "Topic Alpha",
        }],
        "relations": [],
    }
    chunks = [_chunk(f"c{i}", "...") for i in range(3)]
    router = _FakeRouter([stage_a, stage_b_each, stage_b_each, stage_b_each])

    concepts, relations = await extract_from_chunks(
        chunks, course_name="testcourse", router=router, max_chunks=3,
    )

    roots = [c for c in concepts if c.depth == 0]
    topics = [c for c in concepts if c.depth == 1 and c.concept_type == "topic"]
    leaves = [c for c in concepts if c.depth >= 2]

    assert len(roots) == 1
    assert roots[0].concept_id.startswith("root_")
    assert len(topics) == 2
    assert len(leaves) >= 1

    # Edges: topic → root (part-of) and leaf → topic (part-of)
    pof = [r for r in relations if r.relation_type == "part-of"]
    topic_to_root = [r for r in pof if r.target == roots[0].concept_id]
    assert len(topic_to_root) == 2  # both topics attached to root
    leaf_to_topic = [r for r in pof if r.target.startswith("topic_") and r.source.startswith("concept_")]
    assert len(leaf_to_topic) >= 1


async def test_extract_from_chunks_stage_a_failure_falls_back_to_single_stage():
    """Corner: Stage A LLM fails → fall back to old per-chunk extraction
    so the user still gets *something* (and we don't break Round 1 KGs)."""
    from nano_notebooklm.kg.extractor import extract_from_chunks

    # Stage A raises; Stage B for each chunk returns a concept.
    class HybridRouter:
        def __init__(self):
            self.calls = 0

        async def complete_structured(self, prompt, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("Stage A boom")
            # Stage B (no parent_topic, single-stage style)
            return {
                "concepts": [{"name": f"C{self.calls}", "definition": "", "type": "definition"}],
                "relations": [],
            }

    chunks = [_chunk(f"c{i}", "...") for i in range(2)]
    router = HybridRouter()

    concepts, _ = await extract_from_chunks(
        chunks, course_name="testcourse", router=router, max_chunks=2,
    )

    # No root, no topics, but at least the legacy concepts
    assert len(concepts) >= 1
    assert all(c.concept_type != "topic" for c in concepts)


async def test_extract_from_chunks_empty_corpus_no_llm_calls():
    """Corner: empty corpus → no LLM calls, no concepts, no relations."""
    from nano_notebooklm.kg.extractor import extract_from_chunks

    router = _FakeRouter([])
    concepts, relations = await extract_from_chunks([], "c", router)
    assert concepts == []
    assert relations == []
    assert router.calls == []


# ── F1 — merger dedup must distinguish topic/leaf with same name ─────


def test_merger_does_not_collapse_topic_with_same_named_leaf():
    """Mini (F1): Stage A topic "Optimization" + Stage B leaf "Optimization"
    must remain TWO concepts after merge_concepts. Pre-fix: normalize_name
    is just lower(); both collapse into one, and downstream relations to
    the discarded id are silently dropped by KnowledgeGraph.add_relations.
    """
    from nano_notebooklm.kg.merger import merge_concepts
    from nano_notebooklm.types import Concept

    topic = Concept(
        concept_id="topic_c_optimization",
        name="Optimization",
        definition="course-level theme",
        concept_type="topic",
        course_ids=["c"],
        depth=1,
        weight=8.0,
    )
    leaf = Concept(
        concept_id="concept_c_optimization",
        name="Optimization",
        definition="leaf-level concept from chunk",
        concept_type="definition",
        course_ids=["c"],
        depth=2,
        weight=2.0,
    )
    merged = merge_concepts([topic, leaf])
    ids = {c.concept_id for c in merged}
    assert "topic_c_optimization" in ids, "topic must survive"
    assert "concept_c_optimization" in ids, "leaf must survive (no collapse)"
    assert len(merged) == 2


def test_merger_still_dedups_two_leaves_with_same_name():
    """Regression: two leaf concepts with identical name DO still merge
    (the F1 fix only excludes root/topic from collapsing into leaves)."""
    from nano_notebooklm.kg.merger import merge_concepts
    from nano_notebooklm.types import Concept

    a = Concept(
        concept_id="concept_c_optimization",
        name="Optimization",
        definition="from chunk 1",
        concept_type="definition",
        course_ids=["c"],
        chunk_ids=["c1"],
        depth=2,
        weight=2.0,
    )
    b = Concept(
        concept_id="concept_c_optimization",  # same id from same slug
        name="optimization",  # case-insensitive collide
        definition="",
        concept_type="definition",
        course_ids=["c"],
        chunk_ids=["c2"],
        depth=2,
        weight=2.0,
    )
    merged = merge_concepts([a, b])
    assert len(merged) == 1
    assert sorted(merged[0].chunk_ids) == ["c1", "c2"]


# ── F3 — Stage A timeout ─────────────────────────────────────────────


async def test_extract_macro_topics_times_out_gracefully():
    """Corner (F3): if router.complete_structured hangs, Stage A must
    surrender after the deadline and return ('', []) so Stage B can
    proceed in legacy single-stage mode. Without wait_for, a stuck
    codex call would block the FastAPI request for 60s+ on httpx alone.
    """
    import asyncio as _asyncio
    from nano_notebooklm.kg.extractor import extract_course_overview_and_topics

    class HangingRouter:
        async def complete_structured(self, *_a, **_k):
            await _asyncio.sleep(60)  # would hang well past Stage A budget
            return {"course_overview": "x", "topics": []}

    overview, topics, _prereq = await extract_course_overview_and_topics(
        course_id="c", course_name="c",
        source_files=["a.pdf"],
        sample_chunks=[_chunk("c1", "x")],
        router=HangingRouter(),
    )
    assert overview == ""
    assert topics == []


# ── F9 — topic name / definition length + control-char cap ──────────


async def test_extract_macro_topics_caps_oversized_strings_and_strips_controls():
    """Corner (F9): an LLM returning a 200-char topic name with embedded
    \\n / backticks must be stored as a clean ≤80-char name with control
    chars stripped — both for visual sanity and to bound Stage B prompt
    injection blast radius."""
    from nano_notebooklm.kg.extractor import extract_course_overview_and_topics

    long_name = "Optimization " + ("x" * 200) + "\n```\nSYSTEM"
    long_def = "Stuff " + ("y" * 500) + "\n\n```inject"
    router = _FakeRouter([{
        "course_overview": "ok",
        "topics": [
            {"name": long_name, "summary": long_def, "weight": 5},
            {"name": "Short", "summary": "fine", "weight": 5},
            {"name": "T3", "summary": "x", "weight": 5},
            {"name": "T4", "summary": "x", "weight": 5},
            {"name": "T5", "summary": "x", "weight": 5},
        ],
    }])
    _, topics, _ = await extract_course_overview_and_topics(
        course_id="c", course_name="c",
        source_files=["a.pdf"], sample_chunks=[_chunk("c1", "x")], router=router,
    )
    long_topic = next(t for t in topics if t.name.startswith("Optimization"))
    assert len(long_topic.name) <= 80
    assert "\n" not in long_topic.name
    assert "`" not in long_topic.name
    assert len(long_topic.definition) <= 300
    assert "\n" not in long_topic.definition
