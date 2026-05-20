"""Embedding preset switching: per-preset FAISS namespace, preference
persistence, KG concept_embedding per-preset bucket, /api/settings/embedding
endpoint, and legacy-layout migration.

Pinned because the whole point of the preset namespacing is to keep
switches cheap and non-destructive — a regression that points all presets
at the same dir would let a switch silently corrupt the previous preset's
cached vectors.

Test isolation note: config.py runs `load_dotenv(override=True)` at import,
which re-reads the project's real .env (including ARTIFACTS_DIR). That
clobbers any `monkeypatch.setenv("ARTIFACTS_DIR", ...)` we do before
reload. Always patch the module attributes directly via
`monkeypatch.setattr(config, ...)` to keep tests confined to tmp_path.
"""

from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient


def _isolate_config(monkeypatch, tmp_path):
    """Re-point config's ARTIFACTS_DIR + EMBEDDING_PREFERENCE_FILE at the
    test's tmp_path. Returns the config module. Must be called before any
    `save_embedding_preference` / `_faiss_root` access so writes stay
    confined to tmp_path."""
    from nano_notebooklm import config
    monkeypatch.setattr(config, "ARTIFACTS_DIR", tmp_path)
    monkeypatch.setattr(config, "EMBEDDING_PREFERENCE_FILE", tmp_path / "embedding_preference.json")
    return config


# ── 1. Preference file round-trip ────────────────────────────────────


def test_save_and_load_embedding_preference(monkeypatch, tmp_path):
    config = _isolate_config(monkeypatch, tmp_path)

    config.save_embedding_preference("local_mini")
    assert config.EMBEDDING_PREFERENCE_FILE.exists()
    assert config.active_preset_id() == "local_mini"
    assert config.EMBEDDING_MODEL == "paraphrase-multilingual-MiniLM-L12-v2"

    config.save_embedding_preference("bge_m3")
    assert config.active_preset_id() == "bge_m3"
    assert config.EMBEDDING_MODEL == "BAAI/bge-m3"


def test_invalid_preset_id_raises(monkeypatch, tmp_path):
    config = _isolate_config(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        config.save_embedding_preference("not-a-real-preset")


# ── 2. KBStore FAISS path namespacing ────────────────────────────────


def test_faiss_root_includes_active_preset(monkeypatch, tmp_path):
    """`_faiss_root()` must include the active preset id so switching
    routes reads/writes to a different on-disk dir."""
    config = _isolate_config(monkeypatch, tmp_path)
    from nano_notebooklm.kb.store import KBStore

    config.save_embedding_preference("local_mini")
    kb = KBStore(artifacts_dir=tmp_path)
    assert kb._faiss_root().name == "local_mini"

    config.save_embedding_preference("bge_m3")
    assert kb._faiss_root().name == "bge_m3"


def test_reset_embed_fn_invalidates_caches(monkeypatch, tmp_path):
    """`reset_embed_fn()` drops cached embed_fn + index references so the
    next access lazy-loads against the new preset."""
    _isolate_config(monkeypatch, tmp_path)
    from nano_notebooklm.kb.store import KBStore

    fake_embed = lambda texts: np.zeros((len(texts), 8), dtype=np.float32)
    kb = KBStore(artifacts_dir=tmp_path, embed_fn=fake_embed)
    # Caller-injected embed_fn is pinned — reset must not zero it.
    kb.reset_embed_fn()
    assert kb._embed_fn is fake_embed

    # Default (unpinned) → reset clears.
    kb2 = KBStore(artifacts_dir=tmp_path)
    kb2._embed_fn = lambda t: np.zeros((len(t), 4))  # simulate cached default
    kb2._vector_index = object()
    kb2._hybrid = object()
    kb2.reset_embed_fn()
    assert kb2._embed_fn is None
    assert kb2._vector_index is None
    assert kb2._hybrid is None


# ── 3. Legacy layout migration ───────────────────────────────────────


def test_migrate_legacy_faiss_layout_moves_bare_dirs(monkeypatch, tmp_path):
    """Pre-namespacing layout: `indices/faiss/{global,courseA}/...`.
    After migration these should sit under `indices/faiss/<active>/`.
    """
    config = _isolate_config(monkeypatch, tmp_path)
    from nano_notebooklm.kb.store import migrate_legacy_faiss_layout

    config.save_embedding_preference("local_mini")

    legacy_root = tmp_path / "indices" / "faiss"
    (legacy_root / "global").mkdir(parents=True)
    (legacy_root / "global" / "faiss.index").write_bytes(b"fake")
    (legacy_root / "courseA").mkdir()
    (legacy_root / "courseA" / "faiss.index").write_bytes(b"fake")

    result = migrate_legacy_faiss_layout(tmp_path)
    assert set(result["moved"]) == {"global", "courseA"}
    assert (legacy_root / "local_mini" / "global" / "faiss.index").exists()
    assert (legacy_root / "local_mini" / "courseA" / "faiss.index").exists()
    assert not (legacy_root / "global").exists()
    assert not (legacy_root / "courseA").exists()

    # Idempotent: running again is a no-op.
    result2 = migrate_legacy_faiss_layout(tmp_path)
    assert result2["moved"] == []


def test_migrate_skips_under_custom_preset(monkeypatch, tmp_path):
    """When active preset is 'custom' (env-overridden to a non-preset
    model), we can't safely guess where legacy dirs belong — leave them."""
    config = _isolate_config(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "EMBEDDING_MODE", "api")
    monkeypatch.setattr(config, "EMBEDDING_MODEL", "some-weird-model-not-in-presets")
    assert config.active_preset_id() == "custom"

    from nano_notebooklm.kb.store import migrate_legacy_faiss_layout
    (tmp_path / "indices" / "faiss" / "global").mkdir(parents=True)
    result = migrate_legacy_faiss_layout(tmp_path)
    assert result["moved"] == []
    assert "custom" in result["skipped"]


# ── 4. KG concept_embedding per-preset bucket ────────────────────────


def test_kg_add_concepts_writes_per_preset_bucket(monkeypatch, tmp_path):
    config = _isolate_config(monkeypatch, tmp_path)
    from nano_notebooklm.kg.graph import KnowledgeGraph
    from nano_notebooklm.types import Concept

    config.save_embedding_preference("local_mini")
    kg = KnowledgeGraph()
    kg.add_concepts([Concept(
        concept_id="c1", name="C1", definition="d",
        concept_embedding=[0.1] * 384,
    )])
    node = kg.graph.nodes["c1"]
    assert "concept_embeddings" in node
    assert "local_mini" in node["concept_embeddings"]
    assert len(node["concept_embeddings"]["local_mini"]) == 384
    # Legacy field still mirrors the active-preset value for old readers.
    assert len(node["concept_embedding"]) == 384

    # Switch preset → next add_concepts writes into the new bucket; old
    # bucket entry is preserved (cheap "switch back" remains instant).
    config.save_embedding_preference("bge_m3")
    kg.add_concepts([Concept(
        concept_id="c1", name="C1", definition="d",
        concept_embedding=[0.7] * 1024,
    )])
    node = kg.graph.nodes["c1"]
    assert set(node["concept_embeddings"].keys()) == {"local_mini", "bge_m3"}
    assert len(node["concept_embeddings"]["local_mini"]) == 384
    assert len(node["concept_embeddings"]["bge_m3"]) == 1024


# ── 5. /api/settings/embedding endpoint ──────────────────────────────


def _server_with_isolated_artifacts(monkeypatch, tmp_path):
    """Bring up a fresh server.app whose config + KBStore point at tmp_path.

    We patch the already-loaded config module and the already-loaded server
    module's `kb` / `_EMBEDDING_REBUILD_STATE` so existing references stay
    consistent. No importlib.reload — that would re-trigger load_dotenv
    and clobber the patches.
    """
    monkeypatch.setenv("NANO_NLM_DISABLE_EMBED_WARMUP", "1")
    monkeypatch.setenv("NANO_NLM_DISABLE_MINERU_WARMUP", "1")
    config = _isolate_config(monkeypatch, tmp_path)
    import api.server as server
    from nano_notebooklm.kb.store import KBStore
    # Inject a fake embed_fn so the post-switch _rewarm hook (which calls
    # kb.embed_fn(["__warmup__"]) for local-mode presets) doesn't download
    # a real sentence-transformers model and hang the test process for
    # minutes on first run. The fake also "pins" the embed_fn so
    # reset_embed_fn is a no-op (per KBStore.reset_embed_fn contract).
    fake_embed = lambda texts: np.zeros((len(texts), 8), dtype=np.float32)
    monkeypatch.setattr(server, "kb", KBStore(artifacts_dir=tmp_path, embed_fn=fake_embed))
    # Reset rebuild state so each test starts from `idle`. Mirrors the
    # module-level initial shape including the H5 `failed_courses` list.
    fresh_state = {
        "task_id": None, "preset_id": None, "status": "idle",
        "total_courses": 0, "done_courses": 0, "failed_courses": [],
        "current_course": None,
        "error": None, "started_at": None, "ended_at": None,
    }
    server._EMBEDDING_REBUILD_STATE.clear()
    server._EMBEDDING_REBUILD_STATE.update(fresh_state)
    return server, config


def test_switch_embedding_to_local_mini(monkeypatch, tmp_path):
    server, config = _server_with_isolated_artifacts(monkeypatch, tmp_path)
    with TestClient(server.app) as client:
        resp = client.post("/api/settings/embedding", json={"preset_id": "local_mini"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["preset_id"] == "local_mini"
        assert body["embedding_model"] == "paraphrase-multilingual-MiniLM-L12-v2"
        assert body["rebuild_task_id"].startswith("embed-rebuild-")

        status = client.get("/api/status").json()
        assert status["active_preset_id"] == "local_mini"
        presets = status["embedding_presets"]
        assert {p["id"] for p in presets} == {"local_mini", "openai_large", "bge_m3"}
        for p in presets:
            assert {"id", "label", "description", "mode", "model", "dim",
                    "requires_api_key", "download_size_mb"} <= set(p.keys())


def test_switch_embedding_rejects_unknown_preset(monkeypatch, tmp_path):
    server, _ = _server_with_isolated_artifacts(monkeypatch, tmp_path)
    with TestClient(server.app) as client:
        resp = client.post("/api/settings/embedding", json={"preset_id": "bogus"})
        assert resp.status_code == 400


def test_switch_embedding_to_openai_requires_key(monkeypatch, tmp_path):
    """The API preset needs EMBEDDING_API_KEY (or OPENAI_API_KEY) — without
    either, the endpoint returns 400 instead of silently picking a preset
    that won't work."""
    server, config = _server_with_isolated_artifacts(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "EMBEDDING_API_KEY", "")
    with TestClient(server.app) as client:
        resp = client.post("/api/settings/embedding", json={"preset_id": "openai_large"})
        assert resp.status_code == 400
        assert "EMBEDDING_API_KEY" in resp.json()["detail"]


# ── 6. review-swarm fix-all coverage ─────────────────────────────────


def test_switch_embedding_409_when_rebuild_running(monkeypatch, tmp_path):
    """H1: a second switch while a rebuild is in flight returns 409
    instead of spawning a racing second loop that would clobber state +
    write vectors into a half-mutated preset namespace."""
    server, _ = _server_with_isolated_artifacts(monkeypatch, tmp_path)
    server._EMBEDDING_REBUILD_STATE.update({
        "status": "running", "preset_id": "local_mini",
        "started_at": 1.0, "ended_at": None,
    })
    with TestClient(server.app) as client:
        resp = client.post("/api/settings/embedding", json={"preset_id": "bge_m3"})
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert "rebuild already in progress" in detail
        assert "local_mini" in detail


def test_partial_rebuild_status_when_course_fails(monkeypatch, tmp_path):
    """H5: per-course exceptions don't crash the loop and don't masquerade
    as a clean 'done' — terminal status becomes 'partial' with the failing
    course id surfaced in failed_courses[]."""
    server, _ = _server_with_isolated_artifacts(monkeypatch, tmp_path)
    monkeypatch.setattr(server.orchestrator, "list_courses",
                        lambda: ["courseA", "courseB"])

    def fake_build_index(course_id=None, on_embed_progress=None, **kwargs):
        if course_id == "courseB":
            raise RuntimeError("simulated embed failure")
    monkeypatch.setattr(server.kb, "build_index", fake_build_index)

    with TestClient(server.app) as client:
        resp = client.post("/api/settings/embedding", json={"preset_id": "local_mini"})
        assert resp.status_code == 200, resp.text
        # Drive the loop to terminal by polling /api/status (each call
        # awaits in the same loop, giving the background task time slices).
        import time as _time
        deadline = _time.time() + 3.0
        while _time.time() < deadline:
            state = client.get("/api/status").json()["embedding_rebuild"]
            if state["status"] in ("partial", "done", "error"):
                break
        assert state["status"] == "partial", state
        assert "courseB" in state["failed_courses"]
        assert "courseA" not in state["failed_courses"]


def test_rebuild_loop_passes_preset_id_to_build_index(monkeypatch, tmp_path):
    """H1: the rebuild loop captures preset at task-spawn time and passes
    it explicitly so an intervening config mutation can't redirect writes.
    Also: per-course calls skip global + BM25; one final global pass at
    the end (M3)."""
    server, _ = _server_with_isolated_artifacts(monkeypatch, tmp_path)
    monkeypatch.setattr(server.orchestrator, "list_courses", lambda: ["courseA"])
    captured: list[dict] = []

    def fake_build_index(course_id=None, on_embed_progress=None, *,
                         preset_id=None, skip_bm25=False, skip_global=False):
        captured.append({
            "course_id": course_id, "preset_id": preset_id,
            "skip_bm25": skip_bm25, "skip_global": skip_global,
        })
    monkeypatch.setattr(server.kb, "build_index", fake_build_index)

    with TestClient(server.app) as client:
        client.post("/api/settings/embedding", json={"preset_id": "local_mini"})
        import time as _time
        deadline = _time.time() + 3.0
        while _time.time() < deadline:
            if server._EMBEDDING_REBUILD_STATE["status"] in ("done", "partial", "error"):
                break
            client.get("/api/status")

    assert len(captured) >= 2
    assert all(c["preset_id"] == "local_mini" for c in captured)
    per_course = [c for c in captured if c["course_id"] == "courseA"]
    assert per_course and per_course[0]["skip_bm25"] is True
    assert per_course[0]["skip_global"] is True
    global_call = [c for c in captured if c["course_id"] is None]
    assert global_call and global_call[0]["skip_global"] is False
    assert global_call[0]["skip_bm25"] is True


def test_done_state_translated_to_idle_after_ttl(monkeypatch, tmp_path):
    """M1: a terminal 'done' state older than _EMBEDDING_REBUILD_VIEW_TTL_S
    surfaces as idle on /api/status so the frontend banner doesn't stick
    across page loads."""
    server, _ = _server_with_isolated_artifacts(monkeypatch, tmp_path)
    server._EMBEDDING_REBUILD_STATE.update({
        "status": "done", "preset_id": "local_mini",
        "total_courses": 1, "done_courses": 1,
        "started_at": 0.0, "ended_at": 0.0,  # ancient
    })
    with TestClient(server.app) as client:
        view = client.get("/api/status").json()["embedding_rebuild"]
        assert view["status"] == "idle"
        # Underlying state must NOT be mutated — next switch consumes it.
        assert server._EMBEDDING_REBUILD_STATE["status"] == "done"

        import time as _time
        server._EMBEDDING_REBUILD_STATE["ended_at"] = _time.time()
        view2 = client.get("/api/status").json()["embedding_rebuild"]
        assert view2["status"] == "done"


def test_kg_legacy_concept_embedding_still_readable(monkeypatch, tmp_path):
    """Back-compat: a KG node persisted before per-preset bucketing has
    only `concept_embedding`. graph_search reads it as long as the dim
    matches the active preset's expected dim."""
    config = _isolate_config(monkeypatch, tmp_path)
    config.save_embedding_preference("local_mini")
    from nano_notebooklm.kb.graph_search import _resolve_node_embeddings

    nodes = [{
        "id": "n1", "name": "x", "definition": "d",
        "chunk_ids": [], "source_chunks": [],
        "concept_embedding": [0.1] * 384,
    }]
    fake_embed = lambda texts: np.zeros((len(texts), 384), dtype=np.float32)
    cache = _resolve_node_embeddings(nodes, fake_embed,
                                     expected_dim=384, chunk_text_lookup={})
    assert "n1" in cache
    assert cache["n1"].shape == (384,)


def test_mindmap_edit_clears_per_preset_bucket():
    """H3: renaming a concept must drop both the legacy field AND the
    per-preset bucket entries — otherwise graph_search keeps ranking the
    renamed node against its old text via the stale bucket cache."""
    node = {
        "id": "x", "name": "old", "definition": "d",
        "concept_embedding": [0.1] * 384,
        "concept_embeddings": {"local_mini": [0.1] * 384, "bge_m3": [0.2] * 1024},
    }
    patch = {"name": "renamed"}
    merged = {**node, **patch, "user_edited": True}
    # Mirror api/server.py update_node invalidation
    if "name" in patch or "definition" in patch:
        if merged.get("concept_embedding") is not None:
            merged["concept_embedding"] = None
        if merged.get("concept_embeddings"):
            merged["concept_embeddings"] = {}
    assert merged["concept_embedding"] is None
    assert merged["concept_embeddings"] == {}


def test_migrate_skips_symlinks(monkeypatch, tmp_path):
    """M2/M4: migration must not follow symlinks (a symlink under
    indices/faiss/ could let shutil.move redirect writes outside the
    artifacts tree)."""
    config = _isolate_config(monkeypatch, tmp_path)
    config.save_embedding_preference("local_mini")
    from nano_notebooklm.kb.store import migrate_legacy_faiss_layout

    root = tmp_path / "indices" / "faiss"
    root.mkdir(parents=True)
    (root / "courseA").mkdir()
    (root / "courseA" / "faiss.index").write_bytes(b"x")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / "courseB_symlink").symlink_to(elsewhere, target_is_directory=True)

    result = migrate_legacy_faiss_layout(tmp_path)
    assert "courseA" in result["moved"]
    assert "courseB_symlink" not in result["moved"]
    assert (root / "courseB_symlink").is_symlink()
    assert elsewhere.exists()
    assert (root / "local_mini" / "courseA" / "faiss.index").exists()


def test_switch_preset_id_min_length(monkeypatch, tmp_path):
    """L6: empty preset_id rejected at validation (422) instead of the
    handler's generic 400."""
    server, _ = _server_with_isolated_artifacts(monkeypatch, tmp_path)
    with TestClient(server.app) as client:
        resp = client.post("/api/settings/embedding", json={"preset_id": ""})
        assert resp.status_code == 422
