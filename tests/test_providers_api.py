"""Providers matrix CRUD endpoints + secret redaction.

Pinned invariants:
- `literal:<value>` api_key_refs MUST never round-trip to the client.
  Any GET / POST response that includes provider rows is asserted to
  not contain the literal value.
- PUT / DELETE mutate providers.json + trigger `router.reload()` so
  `/api/status:available_backends` reflects the change without a
  restart.
- DELETE refuses to remove the default backend or the only enabled row
  (would zero out router.backends).

Test isolation: monkeypatches `config.PROVIDERS_FILE` to a tmp_path so
writes don't touch the operator's real artifacts/providers.json.
Restores live router.backends via a teardown reload.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def isolated_providers(monkeypatch, tmp_path, request):
    """Redirect providers.json at a tmp path, seed it with a synthetic
    two-provider config, reload the live router. Teardown restores the
    real providers.json by reloading once monkeypatch unwinds."""
    from nano_notebooklm import config
    from api.server import router

    pfile = tmp_path / "providers.json"
    monkeypatch.setattr(config, "PROVIDERS_FILE", pfile)
    # Synthetic two-provider seed. api_key_ref uses env: so we don't
    # store secrets even in tests; the actual env var resolves to "" but
    # `_validate_provider_dict` accepts it — `_build_backend` is the one
    # that skips rows whose key resolves to empty, so we monkeypatch the
    # env to a placeholder value so the backends actually register.
    monkeypatch.setenv("OPENAI_API_KEY", "test-sk-openai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-sk-anthropic")
    seed = {
        "version": 1,
        "providers": [
            {
                "id": "openai-main", "kind": "openai_compat", "label": "OpenAI",
                "base_url": "https://api.openai.com/v1",
                "api_key_ref": "env:OPENAI_API_KEY",
                "model": "gpt-4o-mini", "enabled": True,
            },
            {
                "id": "claude-main", "kind": "anthropic", "label": "Claude",
                "base_url": None,
                "api_key_ref": "env:ANTHROPIC_API_KEY",
                "model": "claude-sonnet-4-5", "enabled": True,
            },
        ],
        "default_backend_id": "openai-main",
    }
    pfile.write_text(json.dumps(seed))
    router.reload()

    def _teardown():
        # Restore the operator's real providers.json by undoing
        # monkeypatch first (handled by pytest), then reloading.
        # We can't access the un-monkeypatched config here, so the
        # request.addfinalizer ordering matters: monkeypatch undoes
        # after our addfinalizer runs. Trigger a reload in a deferred
        # callback that runs *after* monkeypatch reverts.
        pass

    request.addfinalizer(lambda: router.reload())
    return router


def test_get_providers_returns_seeded_list(isolated_providers):
    from api.server import app
    with TestClient(app) as c:
        r = c.get("/api/providers")
        assert r.status_code == 200
        body = r.json()
        ids = [p["id"] for p in body["providers"]]
        assert ids == ["openai-main", "claude-main"]
        assert body["default_backend_id"] == "openai-main"


def test_literal_api_key_never_leaks(isolated_providers, monkeypatch, tmp_path):
    """If an operator stores a key as `literal:<value>`, every API
    response must redact it. Lock this with a grep over the JSON body."""
    from nano_notebooklm import config
    from api.server import app, router

    data = config.load_providers()
    data["providers"].append({
        "id": "openai-literal", "kind": "openai_compat", "label": "Literal Key",
        "base_url": "https://api.openai.com/v1",
        "api_key_ref": "literal:sk-supersecret-DO-NOT-LEAK",
        "model": "gpt-4o", "enabled": True,
    })
    config.save_providers(data)
    router.reload()

    with TestClient(app) as c:
        for path in ("/api/providers", "/api/status"):
            r = c.get(path)
            assert r.status_code == 200
            text = r.text
            assert "DO-NOT-LEAK" not in text, f"{path} leaked the literal key"
            assert "supersecret" not in text, f"{path} leaked the literal key"
        # The redaction shape is "literal:***"
        body = c.get("/api/providers").json()
        row = next(p for p in body["providers"] if p["id"] == "openai-literal")
        assert row["api_key_ref"] == "literal:***"
        assert row["api_key_configured"] is True


def test_put_provider_registers_in_router(isolated_providers):
    from api.server import app, router
    with TestClient(app) as c:
        r = c.put("/api/providers/openai-alt", json={
            "kind": "openai_compat", "label": "Alt",
            "base_url": "https://api.openai.com/v1",
            "api_key_ref": "env:OPENAI_API_KEY",
            "model": "gpt-4o", "enabled": True,
        })
        assert r.status_code == 200, r.text
        assert r.json()["registered"] is True
        assert "openai-alt" in router.backends
        # /api/status surfaces it
        s = c.get("/api/status").json()
        assert "openai-alt" in s["available_backends"]


def test_put_rejects_bad_id_and_bad_api_key_ref(isolated_providers):
    from api.server import app
    with TestClient(app) as c:
        # Path containing space → 422
        r = c.put("/api/providers/has space", json={
            "kind": "openai_compat", "label": "x",
            "base_url": "https://x.example/v1",
            "api_key_ref": "env:X", "model": "m",
        })
        assert r.status_code == 422
        # api_key_ref without env:/literal: → 422
        r = c.put("/api/providers/bad-ref", json={
            "kind": "openai_compat", "label": "x",
            "base_url": "https://x.example/v1",
            "api_key_ref": "plain-secret", "model": "m",
        })
        assert r.status_code == 422


def test_delete_refuses_default_and_last(isolated_providers):
    from api.server import app, router
    with TestClient(app) as c:
        # openai-main is the default → 409
        r = c.delete("/api/providers/openai-main")
        assert r.status_code == 409
        # Move default, delete openai-main, then try to delete the last
        # remaining one (claude-main is default + only enabled) → 409
        c.post("/api/providers/default", json={"provider_id": "claude-main"})
        r = c.delete("/api/providers/openai-main")
        assert r.status_code == 200
        r = c.delete("/api/providers/claude-main")
        assert r.status_code == 409  # blocked: now the default again


def test_set_default_persists_and_reloads(isolated_providers):
    from nano_notebooklm import config
    from api.server import app, router
    with TestClient(app) as c:
        r = c.post("/api/providers/default", json={"provider_id": "claude-main"})
        assert r.status_code == 200
        # On-disk
        data = config.load_providers()
        assert data["default_backend_id"] == "claude-main"
        # Router resolves new default
        assert router._active_default_id() == "claude-main"


def test_test_endpoint_does_not_mutate_router(isolated_providers):
    """POST /api/providers/{id}/test builds a TRANSIENT backend; the
    router's live backends dict must not gain a phantom entry, and an
    already-registered provider's instance identity should be unchanged.
    """
    from api.server import app, router
    before = dict(router.backends)
    with TestClient(app) as c:
        r = c.post("/api/providers/openai-main/test")
        # We don't have network access in CI — the call will either
        # return ok=true (lucky) or ok=false with an error_type. Both
        # are valid behaviour. What matters is the side-effect contract.
        assert r.status_code == 200
        body = r.json()
        assert "ok" in body
    assert set(router.backends.keys()) == set(before.keys())
    # Identity preservation: transient build didn't replace the live
    # instance.
    for k, v in before.items():
        assert router.backends[k] is v


def test_chat_request_unknown_backend_falls_back(isolated_providers, caplog):
    """fix-all v1 M5: chat handler no longer 422s when `req.backend`
    is a provider id that's not in `router.backends`. Instead it
    drops the override and falls through to task-type routing, with a
    log warning. This avoids a cold-load race where the frontend chip
    carries a stale localStorage id (deleted via Settings in another
    tab) and the user types fast enough to hit chat() before the
    rollback effect fires.

    `raise_server_exceptions=False` so a downstream 401 from the (fake)
    LLM call doesn't propagate as a Python exception in the test — we
    care only that the handler-level 422 was not raised.
    """
    import logging
    from api.server import app
    caplog.set_level(logging.WARNING, logger="api.server")
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.post("/api/chat", json={
            "question": "ping", "backend": "no-such-provider",
        })
        # Must NOT be 422 anymore. (The downstream LLM call typically
        # 500s in the no-real-network test env when the fake api key
        # gets rejected upstream — that's fine; the contract under test
        # is specifically that we don't reject the request up front
        # with 422 because of an unknown chip id.)
        assert r.status_code != 422, (
            f"unknown backend should fall back, not 422; got {r.status_code}: {r.text[:200]}"
        )
    # Log warning must have fired so an operator can see the silent
    # downgrade in production.
    assert any(
        "unknown backend" in rec.message and "no-such-provider" in rec.message
        for rec in caplog.records
    ), "expected a warning naming the unknown backend"


def test_get_status_includes_providers_payload(isolated_providers):
    from api.server import app
    with TestClient(app) as c:
        body = c.get("/api/status").json()
        assert "providers" in body
        assert body["providers"]["default_backend_id"] == "openai-main"
        assert {p["id"] for p in body["providers"]["providers"]} == {"openai-main", "claude-main"}


def test_status_default_backend_reflects_runtime(isolated_providers):
    """fix-all v1 M2: /api/status:default_backend used to return the
    env-source value `config.DEFAULT_BACKEND`, ignoring runtime
    switches via /api/providers/default. Now it mirrors
    `router._active_default_id()`."""
    from api.server import app
    with TestClient(app) as c:
        body = c.get("/api/status").json()
        assert body["default_backend"] == "openai-main"
        c.post("/api/providers/default", json={"provider_id": "claude-main"})
        body = c.get("/api/status").json()
        assert body["default_backend"] == "claude-main"


def test_literal_redaction_across_all_endpoints(isolated_providers, monkeypatch):
    """fix-all v1 L8: pin the literal-leak invariant on EVERY endpoint
    that returns provider rows — not just GET providers + /api/status.
    A future refactor that adds an unredacted debug field on the PUT /
    DELETE / default response would otherwise escape coverage."""
    from nano_notebooklm import config
    from api.server import app, router

    LITERAL_SECRET = "ZZ-canary-DO-NOT-LEAK-9b1a"
    data = config.load_providers()
    data["providers"].append({
        "id": "leak-probe", "kind": "openai_compat", "label": "Leak Probe",
        "base_url": "https://api.openai.com/v1",
        "api_key_ref": f"literal:{LITERAL_SECRET}",
        "model": "gpt-4o", "enabled": True,
    })
    config.save_providers(data)
    router.reload()

    with TestClient(app) as c:
        # 1. GET /api/providers
        r = c.get("/api/providers")
        assert LITERAL_SECRET not in r.text
        # 2. /api/status
        r = c.get("/api/status")
        assert LITERAL_SECRET not in r.text
        # 3. PUT (upsert response carries providers payload)
        r = c.put("/api/providers/leak-probe", json={
            "kind": "openai_compat", "label": "Leak Probe 2",
            "base_url": "https://api.openai.com/v1",
            "api_key_ref": f"literal:{LITERAL_SECRET}",
            "model": "gpt-4o", "enabled": True,
        })
        assert r.status_code == 200, r.text
        assert LITERAL_SECRET not in r.text
        # 4. POST /api/providers/default (response carries providers payload)
        c.post("/api/providers/default", json={"provider_id": "leak-probe"})
        # reset to openai-main so cleanup DEL works
        r = c.post("/api/providers/default", json={"provider_id": "openai-main"})
        assert r.status_code == 200
        assert LITERAL_SECRET not in r.text
        # 5. DELETE (response carries providers payload)
        r = c.delete("/api/providers/leak-probe")
        assert r.status_code == 200, r.text
        assert LITERAL_SECRET not in r.text


def test_ssrf_guard_rejects_private_and_metadata_hosts(isolated_providers):
    """fix-all v1 H1: PUT must refuse base_url pointing at AWS IMDS,
    RFC1918, link-local. Used to slip through for kind=openai_compat
    because the legacy SSRF guard only ran for openai_compat_local."""
    from api.server import app
    with TestClient(app) as c:
        for bad in (
            "http://169.254.169.254/latest/meta-data/",  # AWS IMDS
            "http://10.0.0.5/v1",                         # RFC1918
            "http://192.168.1.1/v1",                      # RFC1918
            "http://172.16.0.1/v1",                       # RFC1918
            "http://metadata.google.internal/",           # GCP metadata
        ):
            r = c.put("/api/providers/ssrf-probe", json={
                "kind": "openai_compat", "label": "SSRF Probe",
                "base_url": bad,
                "api_key_ref": "env:OPENAI_API_KEY",
                "model": "gpt-4o", "enabled": True,
            })
            assert r.status_code == 422, f"{bad!r} should have been rejected, got {r.status_code}"


def test_ssrf_guard_allows_loopback_for_local_kind(isolated_providers):
    """Loopback (127.0.0.1 / localhost / ::1) is allowed for both
    kinds — self-hosted Ollama / vLLM workflows depend on it. Public
    HTTPS endpoints also allowed."""
    from api.server import app
    with TestClient(app) as c:
        for good_kind, good_url in (
            ("openai_compat_local", "http://127.0.0.1:8001/v1"),
            ("openai_compat_local", "http://localhost:11434/v1"),
            ("openai_compat", "https://api.openai.com/v1"),
        ):
            r = c.put("/api/providers/loop-probe", json={
                "kind": good_kind, "label": "Loop Probe",
                "base_url": good_url,
                "api_key_ref": "env:OPENAI_API_KEY",
                "model": "x", "enabled": True,
            })
            assert r.status_code == 200, f"{good_url!r} should have been accepted, got {r.status_code}: {r.text}"
        # Cleanup
        c.post("/api/providers/default", json={"provider_id": "openai-main"})
        c.delete("/api/providers/loop-probe")


def test_test_endpoint_no_detail_field_on_failure(isolated_providers):
    """fix-all v1 M1: failure responses must not include `detail`
    (which used to echo `str(exc)[:200]` — leaks SDK request body /
    upstream response body)."""
    from api.server import app
    from nano_notebooklm import config
    # Add a row whose env var is unset so _build_backend fails cleanly
    data = config.load_providers()
    data["providers"].append({
        "id": "dead-row", "kind": "openai_compat", "label": "Dead",
        "base_url": "https://api.openai.com/v1",
        "api_key_ref": "env:DEFINITELY_NOT_SET_VARIABLE_X9Z",
        "model": "gpt-4o", "enabled": True,
    })
    config.save_providers(data)
    from api.server import router
    router.reload()
    with TestClient(app) as c:
        r = c.post("/api/providers/dead-row/test")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is False
        assert body["error_type"] == "build_failed"
        # Reason is allowed (it's a build-time diagnosis, not an upstream
        # response body), but `detail` field must be absent.
        assert "detail" not in body
        assert body.get("reason"), "expected a `reason` for build_failed"


def test_literal_invalid_content_rejected(isolated_providers):
    """fix-all v1 M8: literal: api_key_ref body must be non-empty +
    printable ASCII. A `literal:` (empty) or `literal:foo\\n` used to
    slip through PUT and fail mysteriously later."""
    from api.server import app
    with TestClient(app) as c:
        for bad in ("literal:", "literal:   ", "literal:abc\ndef", "literal:中文key"):
            r = c.put("/api/providers/badlit", json={
                "kind": "openai_compat", "label": "x",
                "base_url": "https://api.openai.com/v1",
                "api_key_ref": bad,
                "model": "x", "enabled": True,
            })
            assert r.status_code == 422, f"{bad!r} should be rejected, got {r.status_code}"


def test_build_error_surfaces_in_providers_payload(isolated_providers, monkeypatch):
    """fix-all v1 M4: a row that passes JSON validation but fails at
    backend-construction time (e.g. env var unset) must surface a
    `build_error` string in /api/providers + /api/status, so the UI
    can show a red dot instead of silently going stale."""
    from nano_notebooklm import config
    from api.server import app, router
    data = config.load_providers()
    data["providers"].append({
        "id": "no-env", "kind": "openai_compat", "label": "No Env",
        "base_url": "https://api.openai.com/v1",
        "api_key_ref": "env:DEFINITELY_NOT_SET_VARIABLE_X9Z",
        "model": "x", "enabled": True,
    })
    config.save_providers(data)
    router.reload()
    with TestClient(app) as c:
        rows = c.get("/api/providers").json()["providers"]
        no_env = next(r for r in rows if r["id"] == "no-env")
        assert no_env["registered"] is False
        assert "api_key_ref" in (no_env.get("build_error") or "")
        # Healthy row reports build_error: None
        ok_row = next(r for r in rows if r["id"] == "openai-main")
        assert ok_row["registered"] is True
        assert ok_row["build_error"] is None


def test_provider_count_capped(isolated_providers):
    """fix-all v2 L6: with no auth, PUT must refuse adding new rows
    beyond _PROVIDER_MAX_ROWS so a hostile client can't bloat
    providers.json into a multi-MB file (loaded on every router.reload
    and re-redacted on every /api/status poll)."""
    from api.server import app, _PROVIDER_MAX_ROWS
    with TestClient(app) as c:
        # We start with 2 seeded rows (openai-main, claude-main); add
        # until we hit the cap.
        existing = len(c.get("/api/providers").json()["providers"])
        room = _PROVIDER_MAX_ROWS - existing
        for i in range(room):
            r = c.put(f"/api/providers/filler-{i}", json={
                "kind": "openai_compat", "label": f"F{i}",
                "base_url": "https://api.openai.com/v1",
                "api_key_ref": "env:OPENAI_API_KEY",
                "model": "gpt-4o", "enabled": True,
            })
            assert r.status_code == 200, f"filler {i} unexpectedly rejected: {r.text}"
        # One more should 409
        r = c.put("/api/providers/over-cap", json={
            "kind": "openai_compat", "label": "Over",
            "base_url": "https://api.openai.com/v1",
            "api_key_ref": "env:OPENAI_API_KEY",
            "model": "gpt-4o", "enabled": True,
        })
        assert r.status_code == 409, r.text
        # Updating an EXISTING row still works at the cap
        r = c.put("/api/providers/filler-0", json={
            "kind": "openai_compat", "label": "F0-updated",
            "base_url": "https://api.openai.com/v1",
            "api_key_ref": "env:OPENAI_API_KEY",
            "model": "gpt-4o", "enabled": True,
        })
        assert r.status_code == 200


def test_save_providers_writes_with_0600_perms(monkeypatch, tmp_path):
    """fix-all v2 L1: tmp file is created via os.open(..., 0o600) so
    the umask 0o644 window is closed. Inspect mode after save."""
    import stat
    from nano_notebooklm import config
    monkeypatch.setattr(config, "PROVIDERS_FILE", tmp_path / "providers.json")
    config.save_providers({"version": 1, "providers": [], "default_backend_id": None})
    mode = stat.S_IMODE(config.PROVIDERS_FILE.stat().st_mode)
    # Must NOT be group / world readable
    assert mode & 0o077 == 0, f"providers.json mode 0o{mode:o} leaks bits to group/world"


def test_save_providers_refuses_symlink(monkeypatch, tmp_path):
    """fix-all v2 L7: save_providers refuses to overwrite a symlink.
    The OS-level `os.replace` would silently swap the link target for
    a regular file; explicit OSError is safer."""
    from nano_notebooklm import config
    monkeypatch.setattr(config, "PROVIDERS_FILE", tmp_path / "providers.json")
    decoy = tmp_path / "decoy.json"
    decoy.write_text("{}")
    config.PROVIDERS_FILE.symlink_to(decoy)
    with pytest.raises(OSError, match="symlink"):
        config.save_providers({"version": 1, "providers": [], "default_backend_id": None})


def test_load_providers_refuses_symlink(monkeypatch, tmp_path, caplog):
    """fix-all v2 L7: load_providers refuses to follow a symlink at
    PROVIDERS_FILE and falls back to env defaults (with an error log)."""
    import logging
    from nano_notebooklm import config
    monkeypatch.setattr(config, "PROVIDERS_FILE", tmp_path / "providers.json")
    decoy = tmp_path / "decoy.json"
    decoy.write_text('{"version": 1, "providers": [{"id":"x","kind":"openai_compat","label":"X","base_url":"https://x.example/v1","api_key_ref":"env:OPENAI_API_KEY","model":"y","enabled":true}], "default_backend_id":"x"}')
    config.PROVIDERS_FILE.symlink_to(decoy)
    caplog.set_level(logging.ERROR, logger="nano_notebooklm.config")
    data = config.load_providers()
    # The decoy's "x" row must NOT appear; we fell back to env defaults
    assert all(p.get("id") != "x" for p in data.get("providers", []))
    assert any("symlink" in rec.message for rec in caplog.records)


def test_llm_backend_kind_default_exists(isolated_providers):
    """fix-all v2 L4: kind is now a class-level attribute on LLMBackend
    with default "" — `getattr(b, 'kind', '')` is no longer monkey-patch-
    dependent. Any future direct construction (tests, CLI) won't
    AttributeError when checked via get_openai_compat."""
    from nano_notebooklm.ai.base import LLMBackend
    assert hasattr(LLMBackend, "kind")
    assert LLMBackend.kind == ""
    # Existing backends still get kind populated by _build_backend
    from api.server import router
    for b in router.backends.values():
        assert b.kind in ("openai_compat", "openai_compat_local", "anthropic"), (
            f"backend {b.name!r} has unset kind {b.kind!r}"
        )


def test_status_endpoint_survives_malformed_row(isolated_providers, tmp_path):
    """fix-all v1 H4: a hand-edited providers.json with a row whose
    `api_key_ref` is null (instead of str) must not crash /api/status
    — `/api/status` is the frontend heartbeat; a 500 there bricks the
    chip + rollback effect simultaneously."""
    from api.server import app, router
    from nano_notebooklm import config
    # Inject a malformed row by writing directly (bypassing save_providers'
    # validation flow — exactly the hand-edit scenario H4 protects against).
    config.PROVIDERS_FILE.write_text(
        '{"version": 1, "providers": ['
        '{"id":"good","kind":"openai_compat","label":"Good","base_url":"https://api.openai.com/v1","api_key_ref":"env:OPENAI_API_KEY","model":"gpt-4o","enabled":true},'
        '{"id":"bad","api_key_ref":null,"kind":"openai_compat"}'
        '], "default_backend_id":"good"}'
    )
    router.reload()
    with TestClient(app) as c:
        r = c.get("/api/status")
        assert r.status_code == 200, f"malformed row crashed /api/status: {r.text[:300]}"
        body = r.json()
        # Both rows surface, the bad one carries a build_error.
        ids = {p["id"] for p in body["providers"]["providers"]}
        assert "good" in ids and "bad" in ids
        bad_row = next(p for p in body["providers"]["providers"] if p["id"] == "bad")
        assert bad_row.get("build_error") or bad_row.get("registered") is False
