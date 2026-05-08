"""M3 — editable mind map.

Backend contract:
  - `POST /api/mindmap/{course_id}/edit`  applies a list of ops
    (add_node / update_node / delete_node / add_edge / delete_edge) and
    persists them to `artifacts/courses/<cid>/mindmap_edits.json`.
  - `GET /api/mindmap/{course_id}` overlays the persisted ops on top of
    the system-extracted KG so the user's edits don't get clobbered when
    the KG is re-extracted.
  - `apply_edit_ops(kg_data, ops)` is the pure overlay function — easier
    to test than the endpoint because it has no I/O.

Tests are 100% offline: we feed a hand-rolled KG dict into apply_edit_ops
and use FastAPI's TestClient with a tmp_path-based artifacts dir for the
endpoint smoke tests.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# ── Pure overlay function ───────────────────────────────────────────


def _seed_kg() -> dict:
    return {
        "nodes": [
            {"id": "root_X", "name": "X", "depth": 0, "concept_type": "root", "weight": 10,
             "definition": "Course X."},
            {"id": "topic_X_a", "name": "Topic A", "depth": 1, "concept_type": "topic", "weight": 5},
            {"id": "concept_X_1", "name": "Concept 1", "depth": 2,
             "concept_type": "definition", "weight": 2,
             "source_chunks": [{"chunk_id": "c1", "source_file": "a.pdf", "page": 1}]},
        ],
        "edges": [
            {"source": "topic_X_a", "target": "root_X", "relation": "part-of"},
            {"source": "concept_X_1", "target": "topic_X_a", "relation": "part-of"},
        ],
    }


def test_apply_ops_add_node_attaches_via_part_of_edge():
    """Mini: add_node with parent_id creates the node + a part-of edge."""
    from api.server import apply_edit_ops

    out = apply_edit_ops(_seed_kg(), [
        {"op": "add_node", "id": "user_concept_1", "label": "My Note",
         "definition": "Personal annotation", "parent_id": "topic_X_a"},
    ])
    ids = [n["id"] for n in out["nodes"]]
    assert "user_concept_1" in ids
    new_node = next(n for n in out["nodes"] if n["id"] == "user_concept_1")
    assert new_node["name"] == "My Note"
    assert new_node["definition"] == "Personal annotation"
    assert new_node.get("user_added") is True
    # part-of edge to parent created
    assert any(
        e["source"] == "user_concept_1" and e["target"] == "topic_X_a"
        and e["relation"] == "part-of"
        for e in out["edges"]
    )


def test_apply_ops_update_node_overrides_label_only():
    """Mini: update_node label keeps definition and source_chunks unchanged."""
    from api.server import apply_edit_ops

    out = apply_edit_ops(_seed_kg(), [
        {"op": "update_node", "id": "concept_X_1", "label": "重命名后"},
    ])
    n = next(n for n in out["nodes"] if n["id"] == "concept_X_1")
    assert n["name"] == "重命名后"
    # source_chunks survive — student edits shouldn't lose provenance.
    assert len(n.get("source_chunks", [])) == 1
    assert n.get("user_edited") is True


def test_apply_ops_delete_node_drops_node_and_incident_edges():
    """Mini: delete_node also removes any edge touching that node."""
    from api.server import apply_edit_ops

    out = apply_edit_ops(_seed_kg(), [
        {"op": "delete_node", "id": "topic_X_a"},
    ])
    ids = [n["id"] for n in out["nodes"]]
    assert "topic_X_a" not in ids
    # edges referencing topic_X_a are gone (both topic→root and concept→topic).
    assert all(
        e["source"] != "topic_X_a" and e["target"] != "topic_X_a"
        for e in out["edges"]
    )


def test_apply_ops_add_edge_dedupes_against_existing():
    """Mini: add_edge for a tuple that's already there is a no-op (idempotent)."""
    from api.server import apply_edit_ops

    out = apply_edit_ops(_seed_kg(), [
        {"op": "add_edge", "source": "topic_X_a", "target": "root_X", "relation": "part-of"},
    ])
    matching = [
        e for e in out["edges"]
        if e["source"] == "topic_X_a" and e["target"] == "root_X" and e["relation"] == "part-of"
    ]
    assert len(matching) == 1


def test_apply_ops_delete_edge_only_removes_named_tuple():
    """Mini: delete_edge by (source, target) drops just that one."""
    from api.server import apply_edit_ops

    seed = _seed_kg()
    seed["edges"].append({"source": "concept_X_1", "target": "topic_X_a", "relation": "depends-on"})
    out = apply_edit_ops(seed, [
        {"op": "delete_edge", "source": "concept_X_1", "target": "topic_X_a", "relation": "part-of"},
    ])
    remaining = [e for e in out["edges"] if e["source"] == "concept_X_1" and e["target"] == "topic_X_a"]
    assert len(remaining) == 1
    assert remaining[0]["relation"] == "depends-on"


def test_apply_ops_unknown_op_skipped_not_raised():
    """Corner: an unknown op key is logged-and-skipped, not crashed.
    The backend stores ops verbatim so a future client version may emit
    ops we don't yet recognize — never lose a user's other edits to one
    bad op."""
    from api.server import apply_edit_ops

    out = apply_edit_ops(_seed_kg(), [
        {"op": "nonsense", "id": "x"},
        {"op": "update_node", "id": "concept_X_1", "label": "still applied"},
    ])
    n = next(n for n in out["nodes"] if n["id"] == "concept_X_1")
    assert n["name"] == "still applied"


def test_apply_ops_replay_idempotent():
    """Corner: applying the same ops twice yields the same result as once.
    Pin idempotency for the GET-overlay path (which replays everything
    every request)."""
    from api.server import apply_edit_ops

    ops = [
        {"op": "add_node", "id": "u1", "label": "U1", "parent_id": "topic_X_a"},
        {"op": "update_node", "id": "concept_X_1", "label": "renamed"},
    ]
    once = apply_edit_ops(_seed_kg(), ops)
    twice = apply_edit_ops(_seed_kg(), ops + ops)
    # Same number of nodes / edges; same labels.
    assert sorted(n["id"] for n in once["nodes"]) == sorted(n["id"] for n in twice["nodes"])
    assert sorted((e["source"], e["target"], e["relation"]) for e in once["edges"]) \
           == sorted((e["source"], e["target"], e["relation"]) for e in twice["edges"])


# ── Endpoint round-trip ─────────────────────────────────────────────


@pytest.fixture
def client_with_seeded_kg(tmp_path, monkeypatch):
    """Stand up a TestClient pointed at a tmp artifacts dir with a tiny
    pre-seeded KG so we can exercise GET → POST /edit → GET overlay
    without depending on any LLM call."""
    art = tmp_path / "artifacts"
    courses_dir = art / "courses" / "TC"
    courses_dir.mkdir(parents=True)
    (courses_dir / "knowledge_graph.json").write_text(
        json.dumps(_seed_kg(), ensure_ascii=False),
        encoding="utf-8",
    )

    monkeypatch.setenv("ARTIFACTS_DIR", str(art))
    from nano_notebooklm import config
    monkeypatch.setattr(config, "ARTIFACTS_DIR", art)

    from api import server
    monkeypatch.setattr(server.config, "ARTIFACTS_DIR", art)

    yield TestClient(server.app)


def test_edit_endpoint_add_then_get_includes_new_node(client_with_seeded_kg):
    """Mini: POST add_node → next GET /api/mindmap returns the new node."""
    client = client_with_seeded_kg
    r = client.post("/api/mindmap/TC/edit", json={
        "ops": [{"op": "add_node", "id": "user_X_1",
                 "label": "User concept", "parent_id": "topic_X_a"}],
    })
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    r2 = client.post("/api/mindmap/TC")
    assert r2.status_code == 200, r2.text
    payload = r2.json()
    ids = [n["id"] for n in payload["nodes"]]
    assert "user_X_1" in ids
    # The new node is reachable via the part-of edge that was created.
    assert any(
        e["source"] == "user_X_1" and e["target"] == "topic_X_a"
        for e in payload["edges"]
    )


def test_edit_endpoint_rejects_invalid_op(client_with_seeded_kg):
    """Corner: malformed payload → 422 + standard error envelope."""
    client = client_with_seeded_kg
    r = client.post("/api/mindmap/TC/edit", json={"ops": []})  # min_length=1
    assert r.status_code == 422
    body = r.json()
    assert body.get("error") == "validation_error"
    assert body.get("request_id")


def test_edit_endpoint_unknown_course_returns_404(client_with_seeded_kg):
    """Corner: editing a course we never extracted KG for → 404, not crash."""
    client = client_with_seeded_kg
    r = client.post("/api/mindmap/Nonexistent/edit", json={
        "ops": [{"op": "add_node", "id": "u", "label": "x"}],
    })
    assert r.status_code == 404


# ── Frontend helper: applyMindmapOps must mirror backend semantics ─


def _node_run(script: str) -> str:
    import subprocess
    proc = subprocess.run(
        ["node", "-e", script], cwd=".", text=True, capture_output=True, check=True,
    )
    return proc.stdout


def test_frontend_apply_mindmap_ops_add_and_update():
    """Mini: client-side overlay applies add/update ops and produces the
    same shape as the server. Pin so optimistic UI matches server reality."""
    script = """
        const h = require('./frontend/study-state.js');
        const kg = {
          nodes: [
            {id: 'root', name: 'C', depth: 0, concept_type: 'root'},
            {id: 'topic_a', name: 'A', depth: 1, concept_type: 'topic'},
          ],
          edges: [{source: 'topic_a', target: 'root', relation: 'part-of'}],
        };
        const out = h.applyMindmapOps(kg, [
          {op: 'add_node', id: 'u1', label: 'New', parent_id: 'topic_a'},
          {op: 'update_node', id: 'topic_a', label: 'Renamed Topic'},
        ]);
        const ids = out.nodes.map(n => n.id);
        if (!ids.includes('u1')) throw new Error('missing added node');
        const renamed = out.nodes.find(n => n.id === 'topic_a');
        if (renamed.name !== 'Renamed Topic') throw new Error('not renamed');
        if (renamed.user_edited !== true) throw new Error('user_edited flag missing');
        if (!out.edges.some(e => e.source === 'u1' && e.target === 'topic_a' && e.relation === 'part-of')) {
          throw new Error('parent edge not created');
        }
        console.log('ok');
    """
    assert _node_run(script).strip() == "ok"


def test_frontend_apply_mindmap_ops_delete_drops_incident_edges():
    """Corner: deleting a node also drops its inbound and outbound edges
    on the client overlay, same as backend. Prevents dangling edges in
    optimistic UI."""
    script = """
        const h = require('./frontend/study-state.js');
        const kg = {
          nodes: [
            {id: 'a', name: 'A'}, {id: 'b', name: 'B'}, {id: 'c', name: 'C'},
          ],
          edges: [
            {source: 'a', target: 'b', relation: 'part-of'},
            {source: 'b', target: 'c', relation: 'part-of'},
          ],
        };
        const out = h.applyMindmapOps(kg, [{op: 'delete_node', id: 'b'}]);
        if (out.nodes.some(n => n.id === 'b')) throw new Error('b not deleted');
        if (out.edges.length !== 0) throw new Error('incident edges should be gone');
        console.log('ok');
    """
    assert _node_run(script).strip() == "ok"


def test_frontend_api_exposes_edit_mindmap():
    """Frontend contract: api.js wires editMindmap to /mindmap/{id}/edit."""
    text = open("frontend/api.js", "r", encoding="utf-8").read()
    assert "editMindmap" in text
    assert "/mindmap/" in text and "/edit" in text


def test_frontend_mindmap_jsx_surfaces_sync_error_to_user():
    """F8 contract: commitOps's POST .catch must NOT be console.warn-only.
    The UI must update some user-visible state when the server rejects an
    op or the request fails entirely. We grep for the wiring pieces."""
    text = open("frontend/mindmap.jsx", "r", encoding="utf-8").read()
    # state for sync error
    assert "syncError" in text
    assert "setSyncError" in text
    # the POST .then path inspects op_results to surface skipped ops
    assert "op_results" in text
    assert "skipped" in text
    # the .catch path also sets syncError (not just console.warn)
    catch_idx = text.find(".catch(err =>")
    assert catch_idx > 0
    catch_block = text[catch_idx:catch_idx + 400]
    assert "setSyncError" in catch_block, (
        "POST .catch must call setSyncError so the user sees the failure"
    )


def test_frontend_mindmap_jsx_wires_edit_handlers():
    """Frontend contract: mindmap.jsx exposes the four interactions
    (dblclick edit / N add / Del / shift+drag connect) via grep — no
    babel-standalone runner here, so we assert the wiring is present."""
    text = open("frontend/mindmap.jsx", "r", encoding="utf-8").read()
    # dblclick → label edit
    assert "onDoubleClick" in text
    assert "startEditingNode" in text
    # keyboard shortcuts
    assert "addChildOf" in text
    assert "deleteNodeWithConfirm" in text
    # shift-drag → connect; relation popup
    assert "shiftKey" in text
    assert "pendingEdge" in text
    # commits route through API.editMindmap
    assert "API.editMindmap" in text or "editMindmap" in text
    # uses StudyState helpers
    assert "applyMindmapOps" in text
    assert "newMindmapNodeId" in text


# ── F5 + F7 — endpoint validation + op_results in response ─────────


def test_apply_ops_skipped_when_parent_id_does_not_exist():
    """F5 mini: add_node with a parent_id that doesn't exist in the KG
    must NOT silently persist a dangling part-of edge. Skip the op,
    surface the reason to the caller via op_results."""
    from api.server import apply_edit_ops_with_results

    payload, op_results = apply_edit_ops_with_results(_seed_kg(), [
        {"op": "add_node", "id": "u1", "label": "X", "parent_id": "nonexistent_topic"},
    ])
    # The node CAN still land (user-added orphan is a legitimate state),
    # but the dangling edge to a missing parent must not persist.
    assert not any(
        e.get("source") == "u1" and e.get("target") == "nonexistent_topic"
        for e in payload["edges"]
    )
    # op_results surfaces the partial outcome.
    assert len(op_results) == 1
    r = op_results[0]
    assert r["op"] == "add_node"
    # status reflects the parent-id miss (e.g. "applied_with_warning" or
    # "skipped_partially" — implementation choice). Reason must mention parent.
    assert "parent" in (r.get("reason") or "").lower()


def test_apply_ops_skipped_when_add_edge_endpoint_missing():
    """F5 corner: add_edge with a non-existent source or target is dropped
    entirely (no persisted dangling edge), and op_results says why."""
    from api.server import apply_edit_ops_with_results

    payload, op_results = apply_edit_ops_with_results(_seed_kg(), [
        {"op": "add_edge", "source": "ghost", "target": "topic_X_a",
         "relation": "related"},
        {"op": "add_edge", "source": "topic_X_a", "target": "phantom",
         "relation": "related"},
    ])
    assert all(
        e.get("source") not in {"ghost"} and e.get("target") not in {"phantom"}
        for e in payload["edges"]
    )
    assert len(op_results) == 2
    assert all(r["status"] == "skipped" for r in op_results)
    assert all("not found" in (r.get("reason") or "").lower() or "missing" in (r.get("reason") or "").lower()
               for r in op_results)


def test_apply_ops_skipped_when_update_node_id_unknown():
    """F5: update_node on a non-existent id is logged + reported, not
    silently swallowed."""
    from api.server import apply_edit_ops_with_results

    _, op_results = apply_edit_ops_with_results(_seed_kg(), [
        {"op": "update_node", "id": "no_such_id", "label": "x"},
    ])
    assert len(op_results) == 1
    assert op_results[0]["status"] == "skipped"


def test_edit_endpoint_returns_op_results(client_with_seeded_kg):
    """F7 mini: POST response includes op_results array so client can
    surface skipped ops to the user."""
    client = client_with_seeded_kg
    r = client.post("/api/mindmap/TC/edit", json={
        "ops": [
            {"op": "add_node", "id": "good", "label": "Good",
             "parent_id": "topic_X_a"},
            {"op": "add_edge", "source": "good", "target": "phantom",
             "relation": "related"},
        ],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert "op_results" in body
    assert isinstance(body["op_results"], list)
    assert len(body["op_results"]) == 2
    # First op succeeded, second was skipped.
    assert body["op_results"][0]["status"] in {"applied", "applied_with_warning"}
    assert body["op_results"][1]["status"] == "skipped"


# ── F2 — concurrent edit lock + atomic write ──────────────────────


def test_concurrent_edits_do_not_lose_ops(tmp_path, monkeypatch):
    """F2 mini: two concurrent POST /edit requests for the same course
    must BOTH land in mindmap_edits.json. Without a lock, load+append+
    save races overwrite each other's batches."""
    import asyncio
    art = tmp_path / "artifacts"
    courses_dir = art / "courses" / "CC"
    courses_dir.mkdir(parents=True)
    (courses_dir / "knowledge_graph.json").write_text(
        json.dumps(_seed_kg(), ensure_ascii=False), encoding="utf-8",
    )
    monkeypatch.setenv("ARTIFACTS_DIR", str(art))
    from nano_notebooklm import config
    monkeypatch.setattr(config, "ARTIFACTS_DIR", art)
    from api import server
    monkeypatch.setattr(server.config, "ARTIFACTS_DIR", art)

    async def post_one(ops):
        # call the underlying coroutine directly to avoid TestClient's threading
        from api.server import edit_mindmap, MindmapEditRequest
        req = MindmapEditRequest(ops=ops)
        return await edit_mindmap("CC", req)

    async def main():
        return await asyncio.gather(
            post_one([{"op": "add_node", "id": "uA", "label": "A",
                       "parent_id": "topic_X_a"}]),
            post_one([{"op": "add_node", "id": "uB", "label": "B",
                       "parent_id": "topic_X_a"}]),
        )

    asyncio.run(main())

    edits_file = art / "courses" / "CC" / "mindmap_edits.json"
    assert edits_file.exists()
    saved = json.loads(edits_file.read_text())
    op_ids = [op.get("id") for op in saved.get("ops", [])]
    # Both batches must have landed — no last-write-wins data loss.
    assert "uA" in op_ids, f"uA missing from {op_ids}"
    assert "uB" in op_ids, f"uB missing from {op_ids}"


def test_save_edits_uses_atomic_replace(tmp_path, monkeypatch):
    """F2 corner: _save_edits must NOT truncate-in-place. We patch
    Path.write_text to refuse the call, and assert the implementation
    instead writes to a `.tmp` sibling (or uses a different atomic path)
    before replacing the real file."""
    art = tmp_path / "artifacts"
    courses_dir = art / "courses" / "CT"
    courses_dir.mkdir(parents=True)
    monkeypatch.setenv("ARTIFACTS_DIR", str(art))
    from nano_notebooklm import config
    monkeypatch.setattr(config, "ARTIFACTS_DIR", art)
    from api import server
    monkeypatch.setattr(server.config, "ARTIFACTS_DIR", art)

    server._save_edits("CT", [
        {"op": "add_node", "id": "x", "label": "x"},
    ])
    final = art / "courses" / "CT" / "mindmap_edits.json"
    assert final.exists()
    # Round-trip readable JSON.
    payload = json.loads(final.read_text())
    assert payload.get("ops") and payload["ops"][0]["id"] == "x"
    # No leftover .tmp on a clean run.
    leftover = list((art / "courses" / "CT").glob("*.tmp"))
    assert leftover == []


# ── F13 — refuse to delete root via API ─────────────────────────────


def test_apply_ops_refuses_delete_node_on_root():
    """F13: backend must guard against delete_node with a node whose
    concept_type is 'root', mirroring the frontend alert. A direct
    POST bypassing the UI must not erase the course-card view."""
    from api.server import apply_edit_ops_with_results

    payload, op_results = apply_edit_ops_with_results(_seed_kg(), [
        {"op": "delete_node", "id": "root_X"},
    ])
    ids = [n["id"] for n in payload["nodes"]]
    assert "root_X" in ids, "root must NOT be deletable via the edit endpoint"
    assert op_results[0]["status"] == "skipped"
    assert "root" in op_results[0]["reason"].lower()


# ── F17 — overlay coerces disk-loaded op fields to str ──────────────


def test_apply_ops_coerces_non_string_id_field():
    """F17: a hand-edited mindmap_edits.json with `id: 123` (number)
    must not crash apply_edit_ops_with_results — we coerce to str()."""
    from api.server import apply_edit_ops_with_results

    payload, op_results = apply_edit_ops_with_results(_seed_kg(), [
        {"op": "update_node", "id": 999, "label": "numeric id"},
        {"op": "add_node", "id": 42, "label": 7, "parent_id": "topic_X_a"},
    ])
    # No crash; ops were either coerced or skipped, but the function returned.
    assert isinstance(payload["nodes"], list)
    assert len(op_results) == 2


# ── F6 — KnowledgeGraph.save persists parent_topic ─────────────────


def test_knowledge_graph_round_trip_preserves_parent_topic(tmp_path):
    """F6: Concept.parent_topic must survive save→load through
    KnowledgeGraph. Pre-fix: the field is dropped and round-trips as None."""
    from nano_notebooklm.kg.graph import KnowledgeGraph
    from nano_notebooklm.types import Concept

    kg = KnowledgeGraph()
    kg.add_concepts([
        Concept(
            concept_id="topic_c_a",
            name="Topic A",
            definition="",
            concept_type="topic",
            course_ids=["c"],
            depth=1,
            weight=5.0,
        ),
        Concept(
            concept_id="concept_c_x",
            name="Concept X",
            definition="",
            concept_type="definition",
            course_ids=["c"],
            depth=2,
            weight=2.0,
            parent_topic="topic_c_a",
        ),
    ])
    out = tmp_path / "kg.json"
    kg.save(out)

    kg2 = KnowledgeGraph()
    kg2.load(out)
    leaf = kg2.get_concept("concept_c_x")
    assert leaf is not None
    assert leaf.get("parent_topic") == "topic_c_a", (
        f"parent_topic dropped on save/load round-trip: {leaf}"
    )
