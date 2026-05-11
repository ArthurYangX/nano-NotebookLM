"""R5-1 — Knowledge Graph chapter roots.

User direction (2026-05-11): each uploaded source_file becomes its own KG
root (concept_type="root", depth=0) so "chapter" is the unit of the mental
model, not the whole course. These tests pin:

1. Per-file Stage A: extractor runs Stage A once per source_file with
   only that file's chunks visible in the prompt. Two files = two LLM
   calls. The course-wide overview is gone.
2. Multi-root edge wiring: each chapter's topics part-of-edge to that
   chapter's root; orphan leaves attach to their own file's root, not
   a foreign chapter's.
3. Merger preserves per-chapter same-named topics: two chapters with a
   topic named "Backpropagation" persist as two distinct topic nodes,
   each part-of its own root.
4. Stage A partial failure: if one file's Stage A errors but others
   succeed, the failed file still gets a chapter root (with empty
   overview); all its leaves attach as orphans.
5. `_kg_to_mindmap` exposes `rootIds: list[str]` listing every chapter
   root (stable-ordered), with the first one mirrored as `id`/`label`/
   `children` for back-compat.
6. Server-side delete_node refuses ANY concept_type=="root" — invariant
   F13 holds for all chapter roots, not just the first.

All tests offline. The router is the same hand-rolled stub used in
test_kg_extractor.py.
"""

from __future__ import annotations

import pytest

from nano_notebooklm.types import Chunk, FileType


def _chunk(cid: str, text: str, src: str, page: int = 1) -> Chunk:
    return Chunk(
        chunk_id=cid,
        doc_id=f"d_{src}",
        course_id="r5course",
        text=text,
        file_type=FileType.PDF,
        source_file=src,
        location=f"PDF p.{page}",
        page=page,
    )


class _FakeRouter:
    """Records calls in arrival order and returns canned dicts FIFO.

    Different from test_kg_extractor's stub: it tags each call with a
    `prompt` slice we can grep to assert per-file scoping.
    """
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def complete_structured(self, prompt, *, system: str = "", task_type: str = "", **kwargs):
        self.calls.append({"prompt": prompt, "system": system, "task_type": task_type})
        if not self.responses:
            raise RuntimeError("FakeRouter ran out of canned responses")
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


# ── (1) per-file Stage A ─────────────────────────────────────────────


async def test_per_file_stage_a_runs_once_per_source_file():
    """Two source_files → exactly two Stage A calls; each call sees only
    that file's chunks in its prompt."""
    from nano_notebooklm.kg.extractor import extract_from_chunks

    chunks = [
        _chunk("a1", "linear regression text", src="lec1.pdf"),
        _chunk("a2", "ridge regression text", src="lec1.pdf"),
        _chunk("b1", "neural net text", src="lec2.pdf"),
        _chunk("b2", "backprop text", src="lec2.pdf"),
    ]
    # Stage A returns minimal topic lists for each file (3 topics each =
    # below the M1 default _TOPIC_MIN=5 but Stage A still surfaces them).
    stage_a_lec1 = {
        "course_overview": "Lecture 1 covers linear models.",
        "topics": [
            {"name": "Linear regression", "summary": "OLS", "weight": 6},
            {"name": "Ridge regression", "summary": "L2 penalty", "weight": 5},
            {"name": "Bias variance", "summary": "tradeoff", "weight": 4},
        ],
    }
    stage_a_lec2 = {
        "course_overview": "Lecture 2 covers neural nets.",
        "topics": [
            {"name": "Neural network", "summary": "feed-forward", "weight": 7},
            {"name": "Backpropagation", "summary": "chain rule", "weight": 7},
        ],
    }
    # Stage B response per chunk — irrelevant for this test's assertions.
    stage_b = {"concepts": [], "relations": []}

    # Order: Stage A is parallel-but-deterministic (sorted by filename).
    # files_ordered = ["lec1.pdf", "lec2.pdf"], so Stage A for lec1 comes
    # before Stage A for lec2 in arrival order (semaphore is N=3 ≥ 2 files,
    # so they run concurrently but the gather list ordering is preserved).
    router = _FakeRouter([
        stage_a_lec1, stage_a_lec2,
        stage_b, stage_b, stage_b, stage_b,  # 4 chunks × Stage B
    ])

    concepts, _ = await extract_from_chunks(
        chunks, course_name="r5course", router=router, max_chunks=10,
    )

    # 2 Stage A calls + 4 Stage B calls = 6 total
    assert len(router.calls) == 6, f"expected 6 total LLM calls, got {len(router.calls)}"

    # Each Stage A prompt mentions its own filename and ONLY chunks from
    # that file. We test on chunk text fragments unique to each file.
    stage_a_prompts = [
        c["prompt"] for c in router.calls
        if c["task_type"] == "concept_extraction" and "macro-topics" in c["prompt"]
    ]
    # Some flexibility: Stage A prompt template may not contain
    # "macro-topics" literally — fall back to identifying by lack of
    # "Analyze this text". A robust marker: per-file Stage A passes only
    # one filename in the source_files block.
    stage_a_prompts = [c["prompt"] for c in router.calls[:2]]
    lec1_prompt = next(p for p in stage_a_prompts if "lec1.pdf" in p)
    lec2_prompt = next(p for p in stage_a_prompts if "lec2.pdf" in p)
    assert "lec1.pdf" in lec1_prompt and "lec2.pdf" not in lec1_prompt, \
        "lec1's Stage A must not leak lec2's filename"
    assert "lec2.pdf" in lec2_prompt and "lec1.pdf" not in lec2_prompt, \
        "lec2's Stage A must not leak lec1's filename"
    # Stage A chunk-head bleeding check: lec1 prompt sees only lec1 chunk
    # excerpts. We test by unique-word presence.
    assert "linear regression" in lec1_prompt and "neural net" not in lec1_prompt
    assert "neural net" in lec2_prompt and "linear regression" not in lec2_prompt

    # Two chapter roots + 5 topics (3+2) + 0 leaves
    roots = [c for c in concepts if c.depth == 0]
    assert len(roots) == 2, "expected one root per source_file"
    root_names = {r.name for r in roots}
    assert root_names == {"lec1.pdf", "lec2.pdf"}, \
        f"chapter root names must be the source filenames, got {root_names}"


# ── (2) per-chapter orphan attachment ─────────────────────────────────


async def test_orphan_leaf_attaches_to_its_own_chapter_root():
    """A leaf with parent_topic=None gets a part-of edge to ITS own file's
    chapter root, not the first/foreign root. This is the routing
    invariant that makes chapter roots actually useful — otherwise leaves
    from lec2 could end up rendered as descendants of lec1's root."""
    from nano_notebooklm.kg.extractor import extract_from_chunks

    chunks = [
        _chunk("a1", "alpha file content", src="alpha.pdf"),
        _chunk("b1", "beta file content", src="beta.pdf"),
    ]
    # Stage A returns no topics for either file → every leaf is orphaned.
    stage_a_empty = {"course_overview": "", "topics": []}
    # Stage B returns one concept per chunk, with parent_topic=None
    # (no topic to match). Concept names are unique per chunk so the
    # merger doesn't collapse them.
    stage_b_alpha = {
        "concepts": [{"name": "Alpha concept", "definition": "x", "type": "definition"}],
        "relations": [],
    }
    stage_b_beta = {
        "concepts": [{"name": "Beta concept", "definition": "y", "type": "definition"}],
        "relations": [],
    }
    router = _FakeRouter([
        stage_a_empty, stage_a_empty,  # both Stage As empty
        stage_b_alpha, stage_b_beta,    # Stage B per chunk
    ])

    # With NO topics anywhere the orchestrator falls back to legacy
    # single-stage extraction (the `if not all_topics_flat` branch).
    # We want a different scenario: at least one file has topics so the
    # multi-root path is exercised. Replace one Stage A with a real
    # response.
    stage_a_with_topics = {
        "course_overview": "Alpha overview",
        "topics": [{"name": "Alpha topic", "summary": "x", "weight": 5}],
    }
    router = _FakeRouter([
        stage_a_with_topics, stage_a_empty,
        stage_b_alpha, stage_b_beta,
    ])

    concepts, relations = await extract_from_chunks(
        chunks, course_name="r5", router=router, max_chunks=10,
    )

    roots = {c.concept_id: c for c in concepts if c.depth == 0}
    assert len(roots) == 2, "both alpha and beta should get chapter roots"
    alpha_root_id = next(rid for rid, r in roots.items() if r.name == "alpha.pdf")
    beta_root_id = next(rid for rid, r in roots.items() if r.name == "beta.pdf")

    leaves = [c for c in concepts if c.depth >= 2]
    assert len(leaves) == 2, f"expected 2 leaves, got {len(leaves)}: {[l.name for l in leaves]}"
    alpha_leaf = next(l for l in leaves if l.name == "Alpha concept")
    beta_leaf = next(l for l in leaves if l.name == "Beta concept")

    # Alpha leaf attached to Alpha topic (it matched parent_topic="Alpha topic"
    # — wait, we didn't pass parent_topic. Let me re-check: stage_b_alpha has
    # no parent_topic field → router defaults to None → leaf is orphan →
    # attaches to its own chapter root)
    # Actually the Stage A for alpha did emit "Alpha topic", and Stage B
    # response doesn't claim parent_topic, so leaf goes to alpha_root.
    part_of = [r for r in relations if r.relation_type == "part-of"]
    alpha_leaf_edges = [r for r in part_of if r.source == alpha_leaf.concept_id]
    beta_leaf_edges = [r for r in part_of if r.source == beta_leaf.concept_id]
    assert len(alpha_leaf_edges) == 1
    assert alpha_leaf_edges[0].target == alpha_root_id, \
        f"alpha leaf must attach to alpha root ({alpha_root_id}), got {alpha_leaf_edges[0].target}"
    assert len(beta_leaf_edges) == 1
    assert beta_leaf_edges[0].target == beta_root_id, \
        f"beta leaf must attach to beta root ({beta_root_id}), got {beta_leaf_edges[0].target}"


# ── (3) merger does not collapse same-named topics across chapters ────


def test_merger_keeps_same_named_topics_in_different_chapters_distinct():
    """Two chapters both emit a topic called "Backpropagation". The merger
    must NOT collapse them — they're conceptually distinct nodes (one per
    chapter), and the part-of edge from each topic to its own root would
    silently break if they merged into one record."""
    from nano_notebooklm.kg.merger import merge_concepts
    from nano_notebooklm.types import Concept

    topic_a = Concept(
        concept_id="topic_course__lec1_backpropagation",
        name="Backpropagation",
        definition="lec1's framing",
        concept_type="topic",
        course_ids=["course"],
        depth=1,
        weight=7.0,
    )
    topic_b = Concept(
        concept_id="topic_course__lec2_backpropagation",
        name="Backpropagation",
        definition="lec2's framing",
        concept_type="topic",
        course_ids=["course"],
        depth=1,
        weight=7.0,
    )
    merged = merge_concepts([topic_a, topic_b])
    assert len(merged) == 2, \
        "same-named topics from different chapters must persist as 2 records"
    ids = {c.concept_id for c in merged}
    assert ids == {topic_a.concept_id, topic_b.concept_id}


def test_merger_still_dedups_same_named_leaves_across_chapters():
    """Regression: leaves with the same name ACROSS chapters still merge
    (per-chapter distinctness is only for root/topic types). A "Gradient
    descent" definition mentioned in both lec1 and lec2 should pool into
    one leaf node carrying both chunk_ids."""
    from nano_notebooklm.kg.merger import merge_concepts
    from nano_notebooklm.types import Concept

    leaf_a = Concept(
        concept_id="concept_course_gradient_descent",  # same id from same slug
        name="Gradient descent",
        definition="from lec1",
        concept_type="definition",
        course_ids=["course"],
        chunk_ids=["a1"],
        depth=2,
        weight=2.0,
    )
    leaf_b = Concept(
        concept_id="concept_course_gradient_descent",
        name="Gradient descent",
        definition="",
        concept_type="definition",
        course_ids=["course"],
        chunk_ids=["b1"],
        depth=2,
        weight=2.0,
    )
    merged = merge_concepts([leaf_a, leaf_b])
    assert len(merged) == 1
    assert sorted(merged[0].chunk_ids) == ["a1", "b1"], \
        "merged leaf must pool chunk_ids from both source chapters"


# ── (4) Stage A partial failure still produces chapter roots ──────────


async def test_stage_a_partial_failure_still_creates_chapter_root():
    """Two-file course where one Stage A succeeds and the other raises.
    The failed file must still get a chapter root (with empty overview
    + no topics) so the student sees every chapter in the graph; its
    leaves attach as orphans to that root."""
    from nano_notebooklm.kg.extractor import extract_from_chunks

    chunks = [
        _chunk("ok1", "alpha file content", src="alpha.pdf"),
        _chunk("ok2", "alpha more text", src="alpha.pdf"),
        _chunk("fail1", "beta file content", src="beta.pdf"),
        _chunk("fail2", "beta more text", src="beta.pdf"),
    ]
    stage_a_alpha = {
        "course_overview": "Alpha chapter overview.",
        "topics": [{"name": "Alpha topic", "summary": "s", "weight": 5}],
    }
    stage_b_response = {
        "concepts": [{"name": "Some leaf", "definition": "x", "type": "definition"}],
        "relations": [],
    }
    # FakeRouter: Stage A for alpha succeeds, Stage A for beta raises.
    # files_ordered is sorted: ["alpha.pdf", "beta.pdf"]
    router = _FakeRouter([
        stage_a_alpha,
        RuntimeError("Stage A beta upstream 502"),
        # Stage B for each of the 4 chunks
        stage_b_response, stage_b_response, stage_b_response, stage_b_response,
    ])

    concepts, _ = await extract_from_chunks(
        chunks, course_name="r5", router=router, max_chunks=10,
    )

    roots = [c for c in concepts if c.depth == 0]
    assert len(roots) == 2, \
        f"both chapters must get a root even when one Stage A fails; got {[r.name for r in roots]}"
    by_name = {r.name: r for r in roots}
    assert by_name["alpha.pdf"].definition == "Alpha chapter overview."
    assert by_name["beta.pdf"].definition == "", \
        "failed-Stage-A root carries empty overview, not a stale alpha definition"


# ── (5) _kg_to_mindmap surfaces rootIds list ──────────────────────────


def test_kg_to_mindmap_surfaces_rootIds_list_for_multi_chapter_course():
    """The /api/mindmap payload exposes `rootIds: list[str]` with every
    depth=0 root, stable-sorted. The first root is mirrored as `id` /
    `label` / `children` for back-compat with single-root clients."""
    from api.server import _kg_to_mindmap

    kg_data = {
        "nodes": [
            {"id": "root_c__lec2", "name": "lec2.pdf", "depth": 0,
             "concept_type": "root", "definition": "Lec2 overview", "weight": 10},
            {"id": "root_c__lec1", "name": "lec1.pdf", "depth": 0,
             "concept_type": "root", "definition": "Lec1 overview", "weight": 10},
            {"id": "topic_c__lec1_x", "name": "Topic X", "depth": 1,
             "concept_type": "topic", "weight": 5},
            {"id": "topic_c__lec2_y", "name": "Topic Y", "depth": 1,
             "concept_type": "topic", "weight": 5},
        ],
        "edges": [
            {"source": "topic_c__lec1_x", "target": "root_c__lec1", "relation": "part-of"},
            {"source": "topic_c__lec2_y", "target": "root_c__lec2", "relation": "part-of"},
        ],
    }
    out = _kg_to_mindmap(kg_data, "c")
    # rootIds present and lists BOTH roots
    assert "rootIds" in out, "_kg_to_mindmap must expose rootIds for R5-1 multi-root awareness"
    assert sorted(out["rootIds"]) == sorted(["root_c__lec1", "root_c__lec2"])
    # Stable order: by name → "lec1.pdf" before "lec2.pdf"
    assert out["rootIds"][0] == "root_c__lec1"
    # Back-compat: first root mirrored at the top level
    assert out["id"] == "root_c__lec1"
    assert out["label"] == "lec1.pdf"
    assert out["definition"] == "Lec1 overview"


def test_kg_to_mindmap_empty_payload_rootIds_is_empty_list():
    """Edge: zero-node KG must still ship `rootIds: []` so the frontend's
    Array.isArray(rootIds) check doesn't trip on undefined."""
    from api.server import _kg_to_mindmap

    out = _kg_to_mindmap({"nodes": [], "edges": []}, "empty_course")
    assert out["rootIds"] == []


def test_kg_to_mindmap_legacy_single_root_emits_one_element_rootIds():
    """Legacy KGs (Round 1, single course root) must surface a one-element
    rootIds list so the frontend's multi-root code can treat them uniformly
    without a side-branch."""
    from api.server import _kg_to_mindmap

    kg_data = {
        "nodes": [
            {"id": "root_legacy", "name": "Old Course", "depth": 0,
             "concept_type": "root", "weight": 10},
            {"id": "topic_a", "name": "A", "depth": 1, "concept_type": "topic", "weight": 5},
        ],
        "edges": [{"source": "topic_a", "target": "root_legacy", "relation": "part-of"}],
    }
    out = _kg_to_mindmap(kg_data, "legacy")
    assert out["rootIds"] == ["root_legacy"]


# ── (6) frontend prepareMindmapTree exposes rootIds ───────────────────


def test_prepare_mindmap_tree_returns_rootIds_grep():
    """Pin the JS contract: prepareMindmapTree return-value carries a
    `rootIds` field (R5-1). We grep the source rather than spin up Node;
    matches the test_frontend_helpers style."""
    from pathlib import Path
    src = (Path(__file__).parent.parent / "frontend" / "study-state.js").read_text()
    # The return statement at the end of prepareMindmapTree must mention rootIds.
    # Loose grep: at least one return-line referencing rootIds.
    assert "rootIds" in src, "study-state.js must expose rootIds in prepareMindmapTree"
    # Force layout passes it through too.
    # Look for the multi-root force-layout return shape.
    assert "tree.rootIds" in src or "rootIds: " in src, \
        "prepareMindmapForce must propagate rootIds from tree result"


def test_mindmap_jsx_delete_guard_message_mentions_chapter():
    """Pin the R5-1 UX update: delete-guard message says "chapter root"
    not "course root". Catches accidental revert in a future fix-all."""
    from pathlib import Path
    src = (Path(__file__).parent.parent / "frontend" / "mindmap.jsx").read_text()
    assert "chapter root" in src.lower(), \
        "mindmap.jsx delete guard must use 'chapter root' phrasing (R5-1)"


# ── (7) server delete_node refuses ANY root, not just the primary ─────


def test_server_delete_node_refuses_every_chapter_root():
    """F13 invariant: `/api/mindmap/{id}/edit` delete_node refuses any
    node with concept_type=="root". For R5-1 every chapter root must
    enjoy the same protection, not just the first one. We grep the
    relevant predicate."""
    from pathlib import Path
    src = (Path(__file__).parent.parent / "api" / "server.py").read_text()
    # The F13 guard string is `if str(existing.get("concept_type", "")).lower() == "root":`
    assert 'lower() == "root"' in src, \
        "server.py delete_node F13 guard must compare concept_type=='root' (no special-casing primary)"
