"""R3-3 — mind-map learning order + node deep-dive.

Pins the contract added in Round 3 #R3-3:

  (a) Stage A LLM emits ``prerequisite_of`` between topics; the extractor
      runs ``topo_sort_topics`` and stamps ``Concept.learning_order`` on
      each topic in study order. ``_kg_to_mindmap`` propagates the field
      to the payload so ``prepareMindmap`` can render a "1 / 2 / 3 ..."
      badge on each topic node.

  (b) ``POST /api/mindmap/{cid}/explain-node`` runs ``agent_loop.run_agent``
      with a strict 2-tool whitelist (``search_kb`` + ``read_chunk``) for
      at most 4 turns and streams NDJSON events identical in shape to
      ``/api/agent/stream`` so the frontend can reuse its renderer.

Tests stay offline (no LLM keys, no network): the router / agent stream
factory is monkeypatched, the FastAPI testclient drives the endpoint,
and front-end contract grep tests run against the static JSX/CSS files.
"""

from __future__ import annotations

import importlib
import json
import re
import subprocess
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nano_notebooklm.types import Chunk, FileType


# ── Fakes ───────────────────────────────────────────────────────────


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
    """Stand-in for ModelRouter.complete_structured (FIFO canned dicts)."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def complete_structured(self, prompt, *, system: str = "", task_type: str = "", **kwargs):
        self.calls.append({"prompt": prompt, "system": system, "task_type": task_type, **kwargs})
        if not self.responses:
            raise RuntimeError("FakeRouter ran out of canned responses")
        return self.responses.pop(0)


class _FakeBackend:
    name = "openai"
    model = "test-model"
    client = None


# ── (a-1) topo_sort_topics — graph helper ───────────────────────────


def test_topo_sort_topics_linear_chain():
    """Mini: 5 topics A→B→C→D→E linear precedence → order [A,B,C,D,E]
    stable. Each (a, b) edge means a must precede b."""
    from nano_notebooklm.kg.graph import topo_sort_topics

    topics = ["A", "B", "C", "D", "E"]
    edges = [("A", "B"), ("B", "C"), ("C", "D"), ("D", "E")]
    assert topo_sort_topics(topics, edges) == ["A", "B", "C", "D", "E"]


def test_topo_sort_breaks_cycle_with_weight_fallback():
    """Corner: A→B→C→A cycle never reaches indeg=0. Helper must NOT
    raise — degrade to weight-desc / input-order over the leftover ids
    so the caller can still assign learning_order numbers."""
    from nano_notebooklm.kg.graph import topo_sort_topics

    topics = ["A", "B", "C"]
    edges = [("A", "B"), ("B", "C"), ("C", "A")]
    weights = {"A": 9.0, "B": 5.0, "C": 7.0}
    out = topo_sort_topics(topics, edges, weights=weights)
    # All 3 ids must appear exactly once (not raise, not lose any).
    assert sorted(out) == ["A", "B", "C"]
    # Weight-desc on the cycle leftover: A (9) > C (7) > B (5).
    assert out == ["A", "C", "B"]


def test_topo_sort_topics_isolated_node_kept():
    """Sanity: a topic with no precedence edges still appears in the
    output. Its position is governed by weight tie-break, not absent."""
    from nano_notebooklm.kg.graph import topo_sort_topics

    topics = ["X", "Y", "Z"]
    edges = [("X", "Y")]
    weights = {"X": 1.0, "Y": 1.0, "Z": 5.0}
    out = topo_sort_topics(topics, edges, weights=weights)
    # Z's weight is highest among indeg=0 (Z, X) so it wins the first slot.
    assert out[0] == "Z"
    # And the X→Y precedence is still respected.
    assert out.index("X") < out.index("Y")


# ── (a-2) Stage A — prerequisite_of → topic learning_order ──────────


async def test_extract_macro_topics_emits_prerequisite_edges():
    """Mini: Stage A returns 5 topics + 4 prereq edges → extractor produces
    learning_order=1..5 stamped on the topics in topological order, plus
    ``depends-on`` edges between them so the mindmap can draw precedence."""
    from nano_notebooklm.kg.extractor import extract_from_chunks

    stage_a = {
        "course_overview": "Course about ML.",
        "topics": [
            {"name": "Foundations", "summary": "...", "weight": 9},
            {"name": "Linear Models", "summary": "...", "weight": 7},
            {"name": "Trees", "summary": "...", "weight": 6},
            {"name": "Neural Nets", "summary": "...", "weight": 8},
            {"name": "Generative", "summary": "...", "weight": 5},
        ],
        "prerequisite_of": [
            {"from": "Foundations", "to": "Linear Models"},
            {"from": "Linear Models", "to": "Trees"},
            {"from": "Trees", "to": "Neural Nets"},
            {"from": "Neural Nets", "to": "Generative"},
        ],
    }
    stage_b_each = {
        "concepts": [{
            "name": "Sample concept",
            "definition": "...",
            "type": "definition",
            "parent_topic": "Foundations",
        }],
        "relations": [],
    }
    chunks = [_chunk("c1", "...")]
    router = _FakeRouter([stage_a, stage_b_each])

    concepts, relations = await extract_from_chunks(
        chunks, course_name="testcourse", router=router, max_chunks=1,
    )

    topics = sorted(
        [c for c in concepts if c.concept_type == "topic"],
        key=lambda t: t.learning_order or 0,
    )
    assert [t.name for t in topics] == [
        "Foundations", "Linear Models", "Trees", "Neural Nets", "Generative",
    ]
    assert [t.learning_order for t in topics] == [1, 2, 3, 4, 5]

    # depends-on edges between topics surface for mindmap edge styling.
    topic_ids = {t.concept_id for t in topics}
    depends_on = [
        r for r in relations
        if r.relation_type == "depends-on"
        and r.source in topic_ids
        and r.target in topic_ids
    ]
    assert len(depends_on) == 4


async def test_extract_topics_no_prerequisite_field_omits_learning_order():
    """Compatibility: LLM returns Stage A WITHOUT ``prerequisite_of`` (old
    fixtures, fast-path replies). Topics get learning_order=None and the
    mindmap still renders. No depends-on edges are synthesized."""
    from nano_notebooklm.kg.extractor import extract_from_chunks

    stage_a = {
        "course_overview": "x",
        "topics": [
            {"name": f"T{i}", "summary": "s", "weight": 5} for i in range(5)
        ],
        # prerequisite_of omitted entirely
    }
    stage_b_each = {
        "concepts": [{"name": "K", "definition": "", "type": "definition",
                      "parent_topic": "T0"}],
        "relations": [],
    }
    chunks = [_chunk("c1", "...")]
    router = _FakeRouter([stage_a, stage_b_each])

    concepts, relations = await extract_from_chunks(
        chunks, course_name="c", router=router, max_chunks=1,
    )

    topics = [c for c in concepts if c.concept_type == "topic"]
    assert len(topics) == 5
    assert all(t.learning_order is None for t in topics)
    topic_ids = {t.concept_id for t in topics}
    assert not any(
        r.relation_type == "depends-on" and r.source in topic_ids and r.target in topic_ids
        for r in relations
    )


# ── (a-3) _kg_to_mindmap — propagate learning_order to payload ──────


def test_kg_to_mindmap_passes_learning_order_to_payload():
    """End-to-end: a topic node carrying learning_order=2 surfaces that
    field in the normalized ``nodes`` payload so the frontend renderer
    can paint a badge."""
    from api.server import _kg_to_mindmap

    kg_data = {
        "nodes": [
            {"id": "root_X", "name": "X", "depth": 0, "concept_type": "root"},
            {"id": "topic_a", "name": "A", "depth": 1, "concept_type": "topic",
             "weight": 5, "learning_order": 1},
            {"id": "topic_b", "name": "B", "depth": 1, "concept_type": "topic",
             "weight": 5, "learning_order": 2},
            {"id": "leaf_x", "name": "X concept", "depth": 2,
             "concept_type": "definition", "weight": 2},
        ],
        "edges": [
            {"source": "topic_a", "target": "root_X", "relation": "part-of"},
            {"source": "topic_b", "target": "root_X", "relation": "part-of"},
            {"source": "leaf_x", "target": "topic_a", "relation": "part-of"},
        ],
    }
    result = _kg_to_mindmap(kg_data, "X")
    nodes_by_id = {n["id"]: n for n in result["nodes"]}
    assert nodes_by_id["topic_a"]["learning_order"] == 1
    assert nodes_by_id["topic_b"]["learning_order"] == 2
    # Root + leaf carry None (or absent) — never a stale int from another node.
    assert nodes_by_id["root_X"]["learning_order"] is None
    assert nodes_by_id["leaf_x"]["learning_order"] is None


def test_kg_to_mindmap_coerces_invalid_learning_order_to_none():
    """Defensive: a hand-edited KG with a stringified value
    (``learning_order: "two"``) must NOT crash and must NOT round-trip a
    junk string to the frontend; coerce to None."""
    from api.server import _kg_to_mindmap

    kg_data = {
        "nodes": [
            {"id": "root", "name": "X", "depth": 0, "concept_type": "root"},
            {"id": "t", "name": "T", "depth": 1, "concept_type": "topic",
             "weight": 5, "learning_order": "two"},
        ],
        "edges": [{"source": "t", "target": "root", "relation": "part-of"}],
    }
    result = _kg_to_mindmap(kg_data, "X")
    nodes_by_id = {n["id"]: n for n in result["nodes"]}
    assert nodes_by_id["t"]["learning_order"] is None


# ── (b-1) /api/mindmap/{cid}/explain-node — agent stream endpoint ───


@pytest.fixture
def explain_client(monkeypatch, tmp_path, sample_chunks, fake_embed_fn):
    """TestClient with a seeded course + persisted KG that includes one
    topic node so we have a valid node_id target. Mirrors test_agent_api's
    fixture; KG file is the only extra wiring."""
    art = tmp_path / "artifacts"
    (art / "courses" / "testcourse").mkdir(parents=True)
    (art / "courses" / "testcourse" / "chunks.json").write_text(
        json.dumps([c.model_dump() for c in sample_chunks], default=str)
    )
    kg = {
        "nodes": [
            {"id": "root_testcourse", "name": "testcourse", "depth": 0,
             "concept_type": "root", "weight": 10, "definition": "overview"},
            {"id": "topic_a", "name": "Backprop", "depth": 1,
             "concept_type": "topic", "weight": 7,
             "definition": "Backprop summary", "learning_order": 1},
            {"id": "topic_b", "name": "Convolutions", "depth": 1,
             "concept_type": "topic", "weight": 6,
             "definition": "Conv summary", "learning_order": 2},
        ],
        "edges": [
            {"source": "topic_a", "target": "root_testcourse", "relation": "part-of"},
            {"source": "topic_b", "target": "root_testcourse", "relation": "part-of"},
            {"source": "topic_b", "target": "topic_a", "relation": "depends-on"},
        ],
    }
    (art / "courses" / "testcourse" / "knowledge_graph.json").write_text(
        json.dumps(kg, ensure_ascii=False)
    )

    monkeypatch.setenv("ARTIFACTS_DIR", str(art))
    from nano_notebooklm import config
    monkeypatch.setattr(config, "ARTIFACTS_DIR", art)
    from nano_notebooklm.kb import store as kb_store
    monkeypatch.setattr(kb_store, "_get_default_embed_fn", lambda: fake_embed_fn)

    import api.server as server_mod
    importlib.reload(server_mod)
    server_mod.kb.build_index("testcourse")
    monkeypatch.setitem(server_mod.router.backends, "openai", _FakeBackend())
    return TestClient(server_mod.app), server_mod


def _read_events(response):
    return [json.loads(line) for line in response.iter_lines() if line]


def test_explain_node_endpoint_streams_agent_events(explain_client, monkeypatch):
    """Mini: NDJSON event vocabulary identical to /api/agent/stream — at
    least one tool_call + tool_result event followed by a done event."""
    client, server_mod = explain_client

    state = {"calls": 0}

    def fake_factory(backend):
        async def fake_stream(*, system, messages, tools, temperature, max_tokens):
            state["calls"] += 1
            if state["calls"] == 1:
                # First turn: emit a search_kb tool call.
                yield {"type": "assistant_message", "message": {
                    "role": "assistant", "content": None,
                    "tool_calls": [{
                        "id": "c1", "type": "function",
                        "function": {"name": "search_kb",
                                     "arguments": '{"query": "backprop", "course_id": "testcourse"}'},
                    }],
                }}
            else:
                yield {"type": "text_delta", "delta": "Backprop computes "}
                yield {"type": "text_delta", "delta": "gradients via chain rule."}
                yield {"type": "assistant_message",
                       "message": {"role": "assistant",
                                   "content": "Backprop computes gradients via chain rule."}}
        return fake_stream

    monkeypatch.setattr(server_mod, "_agent_llm_stream_factory", fake_factory)

    resp = client.post(
        "/api/mindmap/testcourse/explain-node",
        json={"node_id": "topic_a"},
    )
    assert resp.status_code == 200
    events = _read_events(resp)
    types = [e["type"] for e in events]
    assert "tool_call" in types
    assert "tool_result" in types
    assert events[-1]["type"] == "done"
    assert "Backprop" in events[-1]["answer"]


def test_explain_node_unknown_node_id_returns_404(explain_client):
    """Data-missing corner: node_id not in KG → 404 + standard error envelope."""
    client, _ = explain_client
    resp = client.post(
        "/api/mindmap/testcourse/explain-node",
        json={"node_id": "this_node_does_not_exist"},
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]
    assert body["request_id"]


def test_explain_node_rejects_invalid_course_id(explain_client):
    """Path-traversal / malformed course_id reuses ``_validate_course_id_path``
    so the endpoint refuses to even look at the KG path."""
    client, _ = explain_client
    # path traversal — caught by COURSE_ID_PATTERN before reaching the body.
    resp = client.post(
        "/api/mindmap/..%2Fetc%2Fpasswd/explain-node",
        json={"node_id": "topic_a"},
    )
    # FastAPI may collapse the encoded slashes in routing; both 400 (our
    # validator) and 404 (route miss) are acceptable as long as we never
    # 200 / 500 / leak a stack trace.
    assert resp.status_code in (400, 404)


def test_explain_node_validation_blank_node_id(explain_client):
    """Pydantic min_length=1 enforces non-empty node_id."""
    client, _ = explain_client
    resp = client.post("/api/mindmap/testcourse/explain-node", json={"node_id": ""})
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "validation_error"


def test_explain_node_503_when_no_backend(explain_client):
    """Without an OpenAI-compatible backend the endpoint must short-
    circuit to 503 — never proceed into agent_loop with backend=None."""
    client, server_mod = explain_client
    # Drop the backend dict so the runtime check fails.
    server_mod.router.backends.pop("openai", None)
    resp = client.post(
        "/api/mindmap/testcourse/explain-node",
        json={"node_id": "topic_a"},
    )
    assert resp.status_code == 503


def test_explain_node_tools_strict_subset(explain_client, monkeypatch):
    """Tool whitelist contract: registry only exposes search_kb +
    read_chunk. If the LLM tries to call generate_note or list_courses
    (the two tools allowed in /api/agent/stream but NOT here), the
    registry must reject and surface an error tool_result instead of
    silently writing files / leaking other course names."""
    client, server_mod = explain_client

    # Build the explain-node registry directly and assert the whitelist.
    from nano_notebooklm.orchestrator.tools.search_kb import build_search_kb  # noqa: F401
    reg = server_mod._build_explain_node_registry()
    assert set(reg.names()) == {"search_kb", "read_chunk"}, (
        f"explain-node registry leaked: {reg.names()}"
    )
    # OpenAI tool schemas must match — confirm we don't accidentally ship
    # a third tool's schema while leaving its handler off the registry.
    schemas = reg.openai_schemas()
    schema_names = {s["function"]["name"] for s in schemas}
    assert schema_names == {"search_kb", "read_chunk"}

    # End-to-end: a model that tries `generate_note` gets an error
    # tool_result (no file written, no crash), and the loop terminates.
    def fake_factory(backend):
        async def fake_stream(*, system, messages, tools, temperature, max_tokens):
            yield {"type": "assistant_message", "message": {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "c1", "type": "function",
                    "function": {"name": "generate_note",
                                 "arguments": '{"course_id": "testcourse"}'},
                }],
            }}
        return fake_stream

    monkeypatch.setattr(server_mod, "_agent_llm_stream_factory", fake_factory)
    resp = client.post(
        "/api/mindmap/testcourse/explain-node",
        json={"node_id": "topic_a"},
    )
    assert resp.status_code == 200
    events = _read_events(resp)
    tool_results = [e for e in events if e["type"] == "tool_result"]
    assert tool_results, "expected at least one tool_result for the rejected call"
    # _format_result wraps the RuntimeError as "ERROR: RuntimeError: unknown tool: generate_note"
    assert "unknown tool" in tool_results[0]["result"]
    assert "generate_note" in tool_results[0]["result"]


# ── Frontend contract grep tests ────────────────────────────────────


def _read_repo_file(rel_path: str) -> str:
    return Path(__file__).resolve().parents[1].joinpath(rel_path).read_text()


def test_frontend_mindmap_jsx_wires_alt_click_to_deepdive():
    """Grep contract: mindmap.jsx must read e.altKey, call
    requestNodeDeepDive, declare NodeDeepDivePanel, and wire setDeepDivePanel
    into state. These are the four anchors STATUS.md pins for #R3-3."""
    src = _read_repo_file("frontend/mindmap.jsx")
    for anchor in ("e.altKey", "requestNodeDeepDive",
                   "function NodeDeepDivePanel", "setDeepDivePanel"):
        assert anchor in src, f"mindmap.jsx missing anchor {anchor!r}"


def test_frontend_topic_badge_renders_when_learning_order_set():
    """Grep contract: mindmap.jsx must read learning_order and render a
    .mm-order-badge node so the topic node carries a visible step number."""
    src = _read_repo_file("frontend/mindmap.jsx")
    assert "learning_order" in src
    assert "mm-order-badge" in src


def test_frontend_study_state_passes_learning_order_through():
    """Grep contract: prepareMindmap must read node.learning_order and
    forward it on each layout node; otherwise the badge anchor in
    mindmap.jsx silently no-ops on real KG payloads."""
    src = _read_repo_file("frontend/study-state.js")
    assert "learning_order" in src
    assert "requestNodeDeepDive" in src


def test_styles_css_defines_deepdive_panel_and_badge():
    """Grep contract: styles.css must define both the badge and the
    deep-dive panel rules; without these the alt+click overlay falls
    back to unstyled defaults and looks broken."""
    src = _read_repo_file("frontend/styles.css")
    assert ".mm-order-badge" in src
    assert ".mm-deepdive-panel" in src


# ── Live-render check via Node so the renderer + layout stay in sync ─


def _run_node(script: str) -> str:
    proc = subprocess.run(
        ["node", "-e", script], cwd=".", text=True,
        capture_output=True, check=True,
    )
    return proc.stdout


def test_prepare_mindmap_node_carries_learning_order():
    """Mini: prepareMindmap must propagate learning_order onto each layout
    node so MindMap can `n.learning_order != null` and render the badge."""
    script = textwrap.dedent(
        """
        const h = require('./frontend/study-state.js');
        const kg = {nodes: [], edges: []};
        kg.nodes.push({id: 'root', name: 'C', depth: 0, concept_type: 'root', weight: 10});
        kg.nodes.push({id: 't1', name: 'T1', depth: 1, concept_type: 'topic', weight: 5, learning_order: 1});
        kg.nodes.push({id: 't2', name: 'T2', depth: 1, concept_type: 'topic', weight: 5, learning_order: 2});
        kg.edges.push({source: 't1', target: 'root', relation: 'part-of'});
        kg.edges.push({source: 't2', target: 'root', relation: 'part-of'});
        const layout = h.prepareMindmap(kg, {layout: 'radial'});
        const t1 = layout.nodes.find(n => n.id === 't1');
        const t2 = layout.nodes.find(n => n.id === 't2');
        if (t1.learning_order !== 1 || t2.learning_order !== 2) {
          throw new Error('order not propagated: ' + JSON.stringify({t1: t1.learning_order, t2: t2.learning_order}));
        }
        // Root + nodes without the field stay null so MindMap.jsx can
        // null-check before painting a badge.
        const root = layout.nodes.find(n => n.id === 'root');
        if (root.learning_order !== null) throw new Error('root must be null, got ' + root.learning_order);
        console.log('ok');
        """
    )
    assert _run_node(script).strip() == "ok"
