"""R4-4 GraphRAG retriever — review-swarm fix-all v3 regression tests.

Closes the LOW backlog from v1/v2 review swarms + replaces 2 source-pin
greps with real behavioral tests:

  T1 (real-behavior): extract_from_chunks's slow embed_fn doesn't block
     the event loop — concurrent asyncio.sleep(0.01) tasks still tick
     during the embed pass (covers A2's asyncio.to_thread contract).
  T2 (real-behavior): FastAPI startup hook returns immediately when
     kb.embed_fn is slow — /api/status responds in << warmup duration
     (covers V2's fire-and-forget contract).
  T3 (real-behavior): poison-text outlier in a 5-node KG only loses
     itself; the other 4 nodes still rank under per-node fallback
     (covers V4's per-node fallback contract beyond the v2 spy test).

  L4 graph_search hangs are bounded by 10s timeout in qa_skill.
  L5 graph_search _concept_embed_text now wraps a real Concept instance
     (signature drift surfaces as ValidationError instead of silent
     AttributeError inside a broad except).
  L6 mindmap GET response strips concept_embedding (no 200×384-float
     blob shipped to the browser).
  L7 KnowledgeGraph.add_concepts merge branch overwrites embedding on
     dimension mismatch (operator switched EMBEDDING_MODE).
  L8 graphrag returns < min_hits → falls through to RAG → translation
     → cross-course chain (end-to-end via /api/chat).
  L9 user_lang × graphrag — graphrag path injects the zh/en addendum
     into the system prompt just like the rag path.
  L10 GRAPHRAG_ENABLED inverts to fail-safe: unrecognised values
      disable graphrag instead of silently leaving it on.
  L11 server.py:_warm_embed_fn carries an attribution comment pointing
      back to commit 764276d (R4-4 review-swarm fix-all v1).
"""

from __future__ import annotations

import asyncio
import importlib
import json
import re
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent


# ── helpers (kept compatible with v1/v2 fixtures) ────────────────────


_KEYWORDS = ["zebra", "quokka", "aardvark", "penguin", "kangaroo"]


def _keyword_embed(texts: list[str]) -> np.ndarray:
    out = np.zeros((len(texts), len(_KEYWORDS)), dtype=np.float32)
    for i, t in enumerate(texts):
        low = (t or "").lower()
        for j, kw in enumerate(_KEYWORDS):
            if kw in low:
                out[i, j] = 1.0
    norms = np.linalg.norm(out, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return out / norms


def _make_node(nid, name, definition, chunk_id=None, concept_type="definition",
               depth=2, weight=1.0, concept_embedding=None):
    source_chunks = []
    if chunk_id is not None:
        source_chunks = [{"chunk_id": chunk_id, "source_file": "x.md",
                          "location": "p1", "page": 1}]
    node = {"id": nid, "name": name, "definition": definition,
            "concept_type": concept_type, "course_ids": [],
            "chunk_ids": [], "depth": depth, "weight": weight,
            "source_chunks": source_chunks, "parent_topic": None,
            "learning_order": None}
    if concept_embedding is not None:
        node["concept_embedding"] = concept_embedding
    return node


def _make_chunk(cid, text, course_id="cT"):
    return {"chunk_id": cid, "doc_id": "d1", "course_id": course_id,
            "text": text, "file_type": "pdf",
            "source_file": "x.md", "location": "p1", "page": 1}


def _seed(art, course_id, nodes, edges, chunks):
    cd = art / "courses" / course_id
    cd.mkdir(parents=True, exist_ok=True)
    (cd / "knowledge_graph.json").write_text(
        json.dumps({"nodes": nodes, "edges": edges, "course_name": course_id})
    )
    (cd / "chunks.json").write_text(
        json.dumps(chunks, default=str)
    )


# ── T1: A2 real behavior — embed_fn doesn't block the event loop ─────


async def test_extract_from_chunks_yields_event_loop_during_embed():
    """Slow synchronous embed_fn (100 ms sleep) must not block concurrent
    asyncio coroutines. With asyncio.to_thread we expect the parallel
    counter task to tick many times during the embed; if embed_fn ran
    on the event loop, the counter would barely move."""
    from nano_notebooklm.kg.extractor import extract_from_chunks
    from nano_notebooklm.types import Chunk, FileType

    chunks = [
        Chunk(chunk_id=f"c{i}", doc_id="d1", course_id="testcourse",
              text=f"chunk {i}", file_type=FileType.PDF,
              source_file="x.pdf", location=f"p{i}", page=i)
        for i in range(2)
    ]

    stage_a = {
        "course_overview": "About zebras.",
        "topics": [{"name": "Topic Alpha", "summary": "alpha zebra",
                    "weight": 5}],
    }
    stage_b = {
        "concepts": [{"name": "ConceptB", "definition": "beta zebra",
                      "type": "definition",
                      "parent_topic": "Topic Alpha"}],
        "relations": [],
    }

    class _FakeRouter:
        def __init__(self, responses):
            self.responses = list(responses)
        async def complete_structured(self, prompt, *, system="", task_type="", **kw):
            return self.responses.pop(0)

    router = _FakeRouter([stage_a, stage_b, stage_b])

    def slow_embed(texts):
        # Simulates a sentence-transformer forward pass.
        time.sleep(0.1)
        return _keyword_embed(texts)

    counter = {"ticks": 0}
    stop = asyncio.Event()

    async def ticker():
        while not stop.is_set():
            counter["ticks"] += 1
            await asyncio.sleep(0.005)

    ticker_task = asyncio.create_task(ticker())
    try:
        await extract_from_chunks(
            chunks, course_name="testcourse", router=router, max_chunks=2,
            embed_fn=slow_embed,
        )
    finally:
        stop.set()
        await ticker_task

    # If embed_fn blocked the event loop, ticker wouldn't run for 100ms +
    # so we'd see ~0-1 ticks. With to_thread, we expect >= 5 ticks easily
    # (100ms slot / 5ms cadence ≈ 20 ticks; use 5 as a generous floor).
    assert counter["ticks"] >= 5, \
        f"event loop blocked during embed pass: only {counter['ticks']} ticks"


# ── T2: B7 real behavior — startup doesn't block connections ─────────


def test_startup_hook_fire_and_forget_does_not_block_status(monkeypatch, tmp_path):
    """Set up a slow kb.embed_fn (200ms sleep). With fire-and-forget,
    /api/status must respond in well under 200ms (the warmup runs in
    background). If the hook awaited the warmup directly, the first
    response would be delayed by the embed_fn cost."""
    monkeypatch.delenv("NANO_NLM_DISABLE_EMBED_WARMUP", raising=False)
    monkeypatch.setenv("EMBEDDING_MODE", "local")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    (tmp_path / "artifacts" / "courses").mkdir(parents=True)
    from nano_notebooklm import config as cfg
    monkeypatch.setattr(cfg, "EMBEDDING_MODE", "local")
    monkeypatch.setattr(cfg, "ARTIFACTS_DIR", tmp_path / "artifacts")

    slow_calls = {"n": 0}
    started = {"at": None}
    finished = {"at": None}

    def slow_embed(texts):
        slow_calls["n"] += 1
        started["at"] = time.monotonic()
        time.sleep(0.4)
        finished["at"] = time.monotonic()
        return _keyword_embed(texts)

    from nano_notebooklm.kb import store as kb_store
    monkeypatch.setattr(kb_store, "_get_default_embed_fn", lambda: slow_embed)

    import api.server as server_mod
    importlib.reload(server_mod)

    t0 = time.monotonic()
    with TestClient(server_mod.app) as client:
        t1 = time.monotonic()
        r = client.get("/api/status")
        t2 = time.monotonic()

    boot_time = t1 - t0
    response_time = t2 - t1
    # boot_time includes the startup hook; should NOT include the 0.4s
    # warmup (fire-and-forget). Allow ample headroom for module import.
    assert boot_time < 0.35, \
        f"startup blocked: boot took {boot_time*1000:.0f}ms (warmup is 400ms)"
    assert r.status_code == 200
    body = r.json()
    # embed_warm_ok is None (in flight) or True (already finished) — but
    # NOT False because the slow_embed eventually succeeds.
    assert body.get("embed_warm_ok") in (None, True)


# ── T3: B4 real behavior — poison-text outlier ───────────────────────


def test_per_node_fallback_only_loses_poisoned_node(isolated_artifacts):
    """5-node KG. One node's text triggers an embed_fn exception even on
    per-node retry; the other four embed cleanly via the per-node
    fallback. graph_search should return the four good nodes' chunks
    instead of an empty result."""
    from nano_notebooklm.kb.graph_search import graph_search

    art = isolated_artifacts
    nodes = [
        _make_node("n_a", "Alpha", "zebra zebra", chunk_id="ca"),
        _make_node("n_b", "Beta", "zebra context", chunk_id="cb"),
        _make_node("n_c", "Gamma", "zebra topic", chunk_id="cc"),
        _make_node("n_d", "Delta", "zebra notes", chunk_id="cd"),
        # Poison node — its text contains a "BOMB" sentinel that the
        # embed_fn refuses to encode (simulates tokenizer reject).
        _make_node("n_poison", "Epsilon", "POISON_BOMB zebra", chunk_id="cp"),
    ]
    chunks = [_make_chunk(f"c{c}", f"text {c}") for c in "abcdp"]
    _seed(art, "cT", nodes, [], chunks)

    def picky_embed(texts):
        # First (batch) call: raise to force fallback.
        if len(texts) > 1:
            raise RuntimeError("batch refused")
        # Per-node calls: pass on clean text, raise on poison.
        if "POISON_BOMB" in texts[0]:
            raise RuntimeError("tokenizer rejects poison")
        return _keyword_embed(texts)

    results = graph_search("zebra", "cT", picky_embed,
                           artifacts_dir=art, top_k_concepts=5)
    cids = {r.chunk_id for r in results}
    # The 4 clean nodes' chunks should be ranked; the poison node's chunk
    # should be missing because its embedding couldn't be obtained.
    assert {"ca", "cb", "cc", "cd"} <= cids
    assert "cp" not in cids


# ── L4: graph_search timeout in qa_skill ─────────────────────────────


def test_qa_skill_graphrag_timeout_constant_exposed():
    """L4 pins a 10s budget on graph_search via asyncio.wait_for in
    _maybe_graphrag so a stalled embed_fn (e.g. API HTTP hang) doesn't
    block the chat path indefinitely."""
    from nano_notebooklm.skills import qa_skill
    assert hasattr(qa_skill, "GRAPHRAG_TIMEOUT_SECONDS")
    assert qa_skill.GRAPHRAG_TIMEOUT_SECONDS == 10.0

    src = (REPO_ROOT / "nano_notebooklm" / "skills" / "qa_skill.py").read_text(
        encoding="utf-8"
    )
    assert "asyncio.wait_for(" in src
    assert "GRAPHRAG_TIMEOUT_SECONDS" in src
    assert "asyncio.TimeoutError" in src


# ── L5: _concept_embed_text wraps real Concept ───────────────────────


def test_graph_search_concept_embed_uses_real_concept_instance():
    """Calling _concept_embed_text on a node dict must produce the same
    result as if we built a Concept and passed it to the extractor
    helper directly — i.e. the implementation must instantiate a real
    Concept rather than a duck-typed Shim."""
    from nano_notebooklm.kb.graph_search import _concept_embed_text
    from nano_notebooklm.kg.extractor import _concept_embed_text as extractor_helper
    from nano_notebooklm.types import Concept

    node = {"id": "x", "name": "Alpha", "definition": "zebra zebra"}
    out_via_graph = _concept_embed_text(node)
    out_via_extractor = extractor_helper(Concept(
        concept_id="x", name="Alpha", definition="zebra zebra"
    ))
    assert out_via_graph == out_via_extractor


# ── L6: mindmap GET strips concept_embedding ─────────────────────────


def test_normalize_kg_nodes_strips_concept_embedding():
    """L6 (Reviewer 4 #4): _normalize_kg_nodes uses an explicit field
    whitelist so concept_embedding never reaches the wire — even when
    the on-disk KG carries one. Pin this with a positive isolation test
    so a future spread-operator refactor breaks loudly instead of
    silently shipping ~300 KB of float arrays per /api/mindmap request."""
    from api.server import _normalize_kg_nodes

    out = _normalize_kg_nodes([
        {"id": "n_a", "name": "Alpha", "definition": "alpha",
         "concept_type": "topic", "depth": 1, "weight": 5.0,
         "source_chunks": [], "chunk_ids": [],
         "concept_embedding": [0.1] * 384},
    ])
    assert len(out) == 1
    assert "concept_embedding" not in out[0]


# ── L7: merge dim-mismatch overwrites ────────────────────────────────


def test_add_concepts_merge_overwrites_on_embedding_dim_mismatch():
    """If a re-extraction supplies an embedding with a different
    dimension (e.g. EMBEDDING_MODE switched local → api), the merge
    branch must overwrite rather than retain the stale 384d cache."""
    from nano_notebooklm.kg.graph import KnowledgeGraph
    from nano_notebooklm.types import Concept

    kg = KnowledgeGraph()
    kg.add_concepts([Concept(
        concept_id="x", name="X", definition="dx",
        concept_embedding=[0.0] * 384,  # stale local-mode dim
    )])
    kg.add_concepts([Concept(
        concept_id="x", name="X", definition="dx",
        concept_embedding=[0.5] * 1536,  # new api-mode dim
    )])
    persisted = kg.graph.nodes["x"]["concept_embedding"]
    assert len(persisted) == 1536
    assert persisted[0] == 0.5


# ── L8: graphrag-zero → cross-course chain ──────────────────────────


def test_graphrag_zero_falls_through_to_cross_course(monkeypatch, tmp_path):
    """When the current course has a KG that yields 0 graphrag hits
    AND the local RAG path returns nothing, the chain continues to
    cross-course; we should see path == "cross-course" with origin
    set to the sibling course."""
    art = tmp_path / "artifacts"
    courses = art / "courses"
    courses.mkdir(parents=True)

    # Course A has an empty KG (zero-hit graphrag, plus no chunks → no RAG).
    _seed(art, "courseA", [], [], [])
    # Course B has a strong matching chunk in its own kb.
    from nano_notebooklm.types import Chunk, FileType
    b_chunks = [
        Chunk(chunk_id="b1", doc_id="d", course_id="courseB",
              text="zebra zebra zebra unique fact",
              file_type=FileType.PDF, source_file="x.pdf",
              location="p1", page=1),
    ]
    (courses / "courseB").mkdir(parents=True)
    (courses / "courseB" / "chunks.json").write_text(
        json.dumps([c.model_dump() for c in b_chunks], default=str)
    )

    monkeypatch.setenv("ARTIFACTS_DIR", str(art))
    monkeypatch.setenv("NANO_NLM_DISABLE_EMBED_WARMUP", "1")
    monkeypatch.setenv("RAG_SCORE_GATE_TOP1", "0.0")
    from nano_notebooklm import config as cfg
    monkeypatch.setattr(cfg, "ARTIFACTS_DIR", art)
    from nano_notebooklm.kb import store as kb_store
    monkeypatch.setattr(kb_store, "_get_default_embed_fn", lambda: _keyword_embed)
    from nano_notebooklm.orchestrator import router_intent as ri
    ri._LANG_CACHE.clear()

    import api.server as server_mod
    importlib.reload(server_mod)
    server_mod.kb.build_index(None)

    from nano_notebooklm.types import LLMResponse

    async def stub(prompt, task_type="", system="", temperature=0.7,
                   max_tokens=4096, max_retries=3):
        return LLMResponse(content="cross-course-answer", model="fake",
                           input_tokens=1, output_tokens=1, latency_ms=1.0)

    monkeypatch.setattr(server_mod.router, "complete", stub)

    with TestClient(server_mod.app) as client:
        r = client.post("/api/chat", json={
            "question": "zebra zebra zebra unique fact",
            "course_id": "courseA",
        })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("path") == "cross-course", body
    assert body.get("cross_course_origin") == "courseB"


# ── L9: user_lang × graphrag — system prompt addendum injection ──────


def test_user_lang_zh_addendum_lands_in_graphrag_system_prompt(monkeypatch, tmp_path):
    """When chat takes the graphrag branch with user_lang="zh", the
    captured system prompt must still contain the zh-only binding
    addendum (graphrag uses _answer_rag which respects user_lang)."""
    art = tmp_path / "artifacts"
    (art / "courses").mkdir(parents=True)

    nodes = [
        _make_node("n_a", "Alpha", "zebra zebra zebra", chunk_id="ca",
                   concept_type="topic", depth=1, weight=5.0),
        _make_node("n_b", "Beta", "zebra context", chunk_id="cb"),
    ]
    chunks = [_make_chunk("ca", "alpha"), _make_chunk("cb", "beta")]
    _seed(art, "cT", nodes, [], chunks)

    monkeypatch.setenv("ARTIFACTS_DIR", str(art))
    monkeypatch.setenv("NANO_NLM_DISABLE_EMBED_WARMUP", "1")
    monkeypatch.setenv("RAG_SCORE_GATE_TOP1", "0.0")
    from nano_notebooklm import config as cfg
    monkeypatch.setattr(cfg, "ARTIFACTS_DIR", art)
    from nano_notebooklm.kb import store as kb_store
    monkeypatch.setattr(kb_store, "_get_default_embed_fn", lambda: _keyword_embed)
    from nano_notebooklm.orchestrator import router_intent as ri
    ri._LANG_CACHE.clear()

    import api.server as server_mod
    importlib.reload(server_mod)
    server_mod.kb.build_index(None)

    captured = {"systems": []}
    from nano_notebooklm.types import LLMResponse

    async def stub(prompt, task_type="", system="", temperature=0.7,
                   max_tokens=4096, max_retries=3):
        captured["systems"].append(system or "")
        return LLMResponse(content="ans", model="fake",
                           input_tokens=1, output_tokens=1, latency_ms=1.0)

    monkeypatch.setattr(server_mod.router, "complete", stub)

    with TestClient(server_mod.app) as client:
        r = client.post("/api/chat", json={
            "question": "zebra question",
            "course_id": "cT",
            "user_lang": "zh",
        })
    assert r.status_code == 200, r.text
    assert r.json().get("path") == "graphrag", r.json()
    qa_systems = [s for s in captured["systems"]
                  if "Dr. Marginalia" in s or "Reference documents" in s]
    assert qa_systems, "no qa system prompt captured"
    assert any("Reply ONLY in zh" in s for s in qa_systems), \
        f"zh-only binding addendum must reach graphrag system prompt; got:\n{qa_systems}"


# ── L10: GRAPHRAG_ENABLED inverts to fail-safe ───────────────────────


def test_graphrag_enabled_fail_safe_inversion(monkeypatch):
    """L10 (Reviewer 2 F5): unrecognised non-empty values disable
    graphrag (fail-safe). Empty / missing → default on. Only explicit
    enable tokens turn it back on."""
    from nano_notebooklm.skills import qa_skill

    monkeypatch.delenv("GRAPHRAG_ENABLED", raising=False)
    assert qa_skill._graphrag_enabled() is True  # default on

    for on in ("1", "true", "TRUE", "yes", "YES", "on", "enabled"):
        monkeypatch.setenv("GRAPHRAG_ENABLED", on)
        assert qa_skill._graphrag_enabled() is True, f"{on!r} should keep on"

    # Anything else (typos, deliberate disables) → off (fail-safe).
    # Whitespace-only values strip to empty and revert to default-on
    # (treated as "operator didn't intend anything").
    for off in ("0", "false", "no", "off", "disabled",
                "disablle", "stop", "FALCE", "anything-else"):
        monkeypatch.setenv("GRAPHRAG_ENABLED", off)
        assert qa_skill._graphrag_enabled() is False, f"{off!r} should disable"


# ── L11: attribution comment in server.py:_warm_embed_fn ─────────────


def test_warm_embed_fn_has_attribution_comment():
    """L11 (Reviewer 4 F6): the warm-up hook's design intent lives in
    R4-4 review-swarm fix-all v1 (commit 764276d) + v2 (commit
    abce190), even though the physical edit first landed in R4-6
    (e60bca3). A future archaeologist running `git blame` would
    otherwise (mis)attribute the design to R4-6."""
    src = (REPO_ROOT / "api" / "server.py").read_text(encoding="utf-8")
    hook_block = src[src.index("async def _warm_embed_fn") - 1500:
                     src.index("async def _warm_embed_fn")]
    assert "764276d" in hook_block, \
        "attribution comment must reference R4-4 fix-all v1 commit"
