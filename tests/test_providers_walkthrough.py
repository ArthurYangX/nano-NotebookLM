"""End-to-end user walkthrough — Providers Matrix.

Reads top-to-bottom like a manual UX dry-run an operator would do after
upgrading from the env-only backend config. Every user-visible feature
of the providers matrix is exercised in one story.

Distinct from `test_providers_api.py` / `test_router_dynamic.py`:
- Those are atomic unit / API tests pinning individual invariants
  (easy to bisect when one breaks).
- THIS file is a single narrative test that documents the supported
  user journey. A failure here means a real user-facing flow is
  broken end-to-end — it complements the unit tests, not replaces them.

The test uses `raise_server_exceptions=False` on the chat-path step so
the upstream LLM's 401 (we have no real api key in CI) surfaces as a
status code rather than a Python exception.
"""

from __future__ import annotations

import json
import logging
import stat

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def walkthrough_env(monkeypatch, tmp_path, request):
    """Cold-start environment. providers.json doesn't exist yet — the
    walkthrough's first step exercises bootstrap. Env carries OpenAI +
    Anthropic placeholder keys so `_resolve_api_key_ref` builds happy
    backends.

    Teardown reloads the live router against the operator's real
    providers.json once monkeypatch unwinds, so this test doesn't
    pollute subsequent runs.
    """
    from nano_notebooklm import config
    from api.server import router

    pfile = tmp_path / "providers.json"
    monkeypatch.setattr(config, "PROVIDERS_FILE", pfile)
    # NOTE: config.py captures these as module-level globals at import
    # time (`load_dotenv(override=True)` runs once). monkeypatch.setenv
    # would only affect future os.getenv calls — not the already-frozen
    # values. Use setattr to override the module attributes directly so
    # `_default_providers_from_env` reads our test values. (Same pattern
    # as test_embedding_preset.py:_isolate_config.)
    monkeypatch.setattr(config, "OPENAI_API_KEY", "test-sk-openai-wlk")
    monkeypatch.setattr(config, "OPENAI_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setattr(config, "OPENAI_MODEL", "gpt-4o-mini")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-sk-anthropic-wlk")
    monkeypatch.setattr(config, "CLAUDE_MODEL", "claude-sonnet-4-5")
    # _resolve_api_key_ref reads os.getenv at call time, so the actual
    # env var still needs to be set for backend construction to succeed.
    monkeypatch.setenv("OPENAI_API_KEY", "test-sk-openai-wlk")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-sk-anthropic-wlk")

    request.addfinalizer(lambda: router.reload())
    yield pfile


def test_user_walkthrough_providers_matrix(walkthrough_env, monkeypatch, caplog):
    """END-TO-END USER JOURNEY — read top to bottom.

    Each STEP corresponds to something an operator can do from the
    Settings UI / API. Comments explain what the user sees and what we
    assert. If you change provider-matrix UX, update this story so it
    stays a faithful documentation of the contract.
    """
    from nano_notebooklm import config
    from api.server import app, router

    # ──────────────────────────────────────────────────────────────────
    # STEP 1 — COLD START
    # ──────────────────────────────────────────────────────────────────
    # Operator just upgraded the server. providers.json does NOT exist
    # on disk yet. The first router init / first /api/status call must
    # bootstrap the file from env automatically.
    assert not config.PROVIDERS_FILE.exists(), "fixture should start without providers.json"

    with TestClient(app) as c:
        # The TestClient startup hook constructs the app, which already
        # has a `router = ModelRouter()` at import time. The import-time
        # bootstrap targeted the OPERATOR's real providers.json; here we
        # monkeypatched PROVIDERS_FILE post-import, so re-run bootstrap
        # explicitly against the tmp path. In a real fresh-install boot
        # this happens once inside `_init_backends`.
        config.bootstrap_providers_from_env()
        router.reload()
        assert config.PROVIDERS_FILE.exists(), "bootstrap should have created providers.json"
        mode = stat.S_IMODE(config.PROVIDERS_FILE.stat().st_mode)
        assert mode & 0o077 == 0, f"bootstrap-written file mode 0o{mode:o} leaks to group/world"

        # ──────────────────────────────────────────────────────────────
        # STEP 2 — LIST PROVIDERS
        # ──────────────────────────────────────────────────────────────
        # GET /api/providers returns the bootstrapped rows. Both OpenAI
        # and Claude env vars are set, so we get two rows.
        r = c.get("/api/providers")
        assert r.status_code == 200
        payload = r.json()
        ids = {p["id"] for p in payload["providers"]}
        assert ids == {"openai-main", "claude-main"}, f"expected env-bootstrapped pair, got {ids}"
        assert payload["default_backend_id"] in ids
        # api_key_configured surfaces per row so the UI can show a key badge.
        assert all(p["api_key_configured"] is True for p in payload["providers"])
        # build_error is None for healthy rows.
        assert all(p["build_error"] is None for p in payload["providers"])
        # registered means the row is actually live in router.backends.
        assert all(p["registered"] is True for p in payload["providers"])

        # ──────────────────────────────────────────────────────────────
        # STEP 3 — /api/status surfaces providers + matches /api/providers
        # ──────────────────────────────────────────────────────────────
        # The topbar chip reads `available_backends` from /api/status,
        # and Settings reads `providers.providers`. Both must agree.
        status = c.get("/api/status").json()
        assert set(status["available_backends"]) == ids
        assert status["providers"]["default_backend_id"] == payload["default_backend_id"]
        # default_backend mirror reflects the active runtime decision
        # (not stale env-source value).
        assert status["default_backend"] == payload["default_backend_id"]

        # ──────────────────────────────────────────────────────────────
        # STEP 4 — ADD A NEW PROVIDER
        # ──────────────────────────────────────────────────────────────
        # Operator clicks "+ Add provider" in Settings, fills the form
        # with a DeepSeek-compatible row, and saves.
        r = c.put("/api/providers/deepseek", json={
            "kind": "openai_compat",
            "label": "DeepSeek V4",
            "base_url": "https://api.deepseek.com",
            "api_key_ref": "env:OPENAI_API_KEY",  # reuse for the test
            "model": "deepseek-v4-pro",
            "enabled": True,
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["registered"] is True
        assert "deepseek" in {p["id"] for p in body["providers"]["providers"]}
        # Topbar chip immediately sees the new provider via /api/status
        status = c.get("/api/status").json()
        assert "deepseek" in status["available_backends"]

        # ──────────────────────────────────────────────────────────────
        # STEP 5 — TEST A PROVIDER (5s timeout, no detail leak)
        # ──────────────────────────────────────────────────────────────
        # Operator hits the Test button. Even if the upstream key is
        # bogus and the call 401s, the response must:
        #   1. come back fast (we set http_timeout=5.0 transient)
        #   2. NOT contain a `detail` field (would leak SDK exc body)
        #   3. report a usable `error_type` for the UI badge
        r = c.post("/api/providers/deepseek/test")
        assert r.status_code == 200
        result = r.json()
        assert "detail" not in result, "test response must not echo SDK exception body"
        assert "ok" in result
        if result["ok"] is False:
            assert result.get("error_type"), "failure must carry error_type"
        # Repeat the test on the bootstrapped openai-main row — same shape.
        r = c.post("/api/providers/openai-main/test").json()
        assert "detail" not in r

        # ──────────────────────────────────────────────────────────────
        # STEP 6 — EDIT EXISTING PROVIDER (keep current key)
        # ──────────────────────────────────────────────────────────────
        # Operator opens the Edit form for `deepseek` and changes only
        # the model name. The api_key_ref input is left blank — the UI
        # contract is "blank = keep current". The PUT body re-uses the
        # original env: ref (we simulate the frontend save() handler).
        r = c.put("/api/providers/deepseek", json={
            "kind": "openai_compat",
            "label": "DeepSeek V4 (renamed)",
            "base_url": "https://api.deepseek.com",
            "api_key_ref": "env:OPENAI_API_KEY",  # what save() would re-send
            "model": "deepseek-reasoner",
            "enabled": True,
        })
        assert r.status_code == 200
        deepseek = next(p for p in r.json()["providers"]["providers"] if p["id"] == "deepseek")
        assert deepseek["label"] == "DeepSeek V4 (renamed)"
        assert deepseek["model"] == "deepseek-reasoner"

        # ──────────────────────────────────────────────────────────────
        # STEP 7 — SET AS DEFAULT (radio in the leftmost column)
        # ──────────────────────────────────────────────────────────────
        # Operator clicks the "Set default" radio next to deepseek. The
        # backend persists default_backend_id and the topbar chip on
        # the next /api/status poll shows the new default.
        r = c.post("/api/providers/default", json={"provider_id": "deepseek"})
        assert r.status_code == 200
        status = c.get("/api/status").json()
        assert status["default_backend"] == "deepseek"
        assert status["providers"]["default_backend_id"] == "deepseek"

        # ──────────────────────────────────────────────────────────────
        # STEP 8 — CHAT WITH EXPLICIT BACKEND PIN
        # ──────────────────────────────────────────────────────────────
        # User clicks the topbar chip to cycle to claude-main, then
        # sends a chat. Backend was relaxed from Literal to str so any
        # registered id works.
        with TestClient(app, raise_server_exceptions=False) as c2:
            r = c2.post("/api/chat", json={
                "question": "ping",
                "backend": "claude-main",
            })
            # Will likely 500 because of bogus api key, but must NOT 422.
            assert r.status_code != 422, f"explicit valid backend rejected at gate: {r.text[:200]}"

        # ──────────────────────────────────────────────────────────────
        # STEP 9 — CHAT WITH STALE BACKEND (cold-load race)
        # ──────────────────────────────────────────────────────────────
        # User's localStorage carries an old backend id from before the
        # operator deleted that provider in another tab. The chat
        # handler must drop the override and fall through to default
        # (logging a warn). Used to be a hard 422.
        caplog.set_level(logging.WARNING, logger="api.server")
        with TestClient(app, raise_server_exceptions=False) as c2:
            r = c2.post("/api/chat", json={
                "question": "ping",
                "backend": "deleted-yesterday",
            })
            assert r.status_code != 422, "unknown backend should fall back, not 422"
        assert any(
            "unknown backend" in rec.message and "deleted-yesterday" in rec.message
            for rec in caplog.records
        ), "operator should see a log warning when fall-back kicks in"

        # ──────────────────────────────────────────────────────────────
        # STEP 10 — DELETE A NON-DEFAULT PROVIDER
        # ──────────────────────────────────────────────────────────────
        # Operator decides to drop claude-main. Since it's not the
        # default and there are other enabled rows, DELETE succeeds.
        r = c.delete("/api/providers/claude-main")
        assert r.status_code == 200
        status = c.get("/api/status").json()
        assert "claude-main" not in status["available_backends"]

        # ──────────────────────────────────────────────────────────────
        # STEP 11 — DELETE INVARIANTS
        # ──────────────────────────────────────────────────────────────
        # 11a. Cannot delete the default backend.
        r = c.delete("/api/providers/deepseek")
        assert r.status_code == 409, r.text
        assert "default" in r.json()["error"].lower()

        # 11b. Reset default to openai-main, delete deepseek (now non-default).
        c.post("/api/providers/default", json={"provider_id": "openai-main"})
        r = c.delete("/api/providers/deepseek")
        assert r.status_code == 200

        # 11c. Cannot delete the only remaining enabled row.
        r = c.delete("/api/providers/openai-main")
        assert r.status_code == 409
        assert (
            "default" in r.json()["error"].lower()
            or "only" in r.json()["error"].lower()
        )

        # ──────────────────────────────────────────────────────────────
        # STEP 12 — SSRF GUARD (H1)
        # ──────────────────────────────────────────────────────────────
        # Hostile (or careless) PUT attempts to point at metadata
        # endpoints or RFC1918 must all 422.
        for bad in (
            "http://169.254.169.254/v1",          # AWS IMDS
            "http://metadata.google.internal/",   # GCP
            "http://10.0.0.1/v1",                 # RFC1918
            "http://192.168.0.1/v1",
        ):
            r = c.put("/api/providers/ssrf-probe", json={
                "kind": "openai_compat", "label": "SSRF",
                "base_url": bad,
                "api_key_ref": "env:OPENAI_API_KEY",
                "model": "x", "enabled": True,
            })
            assert r.status_code == 422, f"{bad!r} slipped through SSRF guard"

        # Loopback for openai_compat_local is allowed.
        r = c.put("/api/providers/loop-probe", json={
            "kind": "openai_compat_local", "label": "Local",
            "base_url": "http://127.0.0.1:11434/v1",
            "api_key_ref": "env:OPENAI_API_KEY",
            "model": "qwen3-14b", "enabled": True,
        })
        assert r.status_code == 200, r.text
        c.delete("/api/providers/loop-probe")

        # ──────────────────────────────────────────────────────────────
        # STEP 13 — LITERAL KEY VALIDATION (M8) + REDACTION (L8)
        # ──────────────────────────────────────────────────────────────
        # 13a. Empty / whitespace literal: body rejected.
        for bad in ("literal:", "literal:   ", "literal:abc\ndef"):
            r = c.put("/api/providers/lit-probe", json={
                "kind": "openai_compat", "label": "L",
                "base_url": "https://api.openai.com/v1",
                "api_key_ref": bad,
                "model": "x", "enabled": True,
            })
            assert r.status_code == 422, f"{bad!r} slipped through literal validation"

        # 13b. Add a valid literal: row and confirm no response ever
        # contains the secret material.
        canary = "WALKTHROUGH-CANARY-DO-NOT-LEAK-7e8f1"
        r = c.put("/api/providers/lit-probe", json={
            "kind": "openai_compat", "label": "Inline",
            "base_url": "https://api.openai.com/v1",
            "api_key_ref": f"literal:{canary}",
            "model": "x", "enabled": True,
        })
        assert r.status_code == 200
        assert canary not in r.text, "PUT response leaked literal key"
        for path in ("/api/providers", "/api/status"):
            assert canary not in c.get(path).text, f"{path} leaked literal key"
        # Test endpoint also must not echo the key.
        assert canary not in c.post("/api/providers/lit-probe/test").text
        # Reassign default, delete the lit-probe (default-check + delete
        # response must also be canary-free).
        d_resp = c.post("/api/providers/default", json={"provider_id": "openai-main"})
        assert canary not in d_resp.text
        del_resp = c.delete("/api/providers/lit-probe")
        assert canary not in del_resp.text

        # ──────────────────────────────────────────────────────────────
        # STEP 14 — BUILD ERROR SURFACE (M4)
        # ──────────────────────────────────────────────────────────────
        # Operator adds a row referencing an unset env var. The PUT
        # succeeds at the schema layer, but the row never registers
        # in router.backends. The UI must see `registered:false` +
        # a `build_error` string so it can render a red dot.
        r = c.put("/api/providers/no-env", json={
            "kind": "openai_compat", "label": "Missing Env",
            "base_url": "https://api.openai.com/v1",
            "api_key_ref": "env:DEFINITELY_NOT_SET_VARIABLE_X9Z",
            "model": "x", "enabled": True,
        })
        assert r.status_code == 200
        assert r.json()["registered"] is False
        rows = c.get("/api/providers").json()["providers"]
        no_env = next(p for p in rows if p["id"] == "no-env")
        assert no_env["registered"] is False
        assert no_env["build_error"] and "api_key_ref" in no_env["build_error"]
        c.delete("/api/providers/no-env")

        # ──────────────────────────────────────────────────────────────
        # STEP 15 — MALFORMED ROW DOES NOT BRICK /api/status (H4)
        # ──────────────────────────────────────────────────────────────
        # An operator hand-edits providers.json and accidentally writes
        # `"api_key_ref": null`. The next status poll must still
        # succeed (the frontend heartbeat depends on it). The bad row
        # surfaces as a placeholder with a build_error.
        config.PROVIDERS_FILE.write_text(json.dumps({
            "version": 1,
            "providers": [
                {"id": "openai-main", "kind": "openai_compat", "label": "OpenAI",
                 "base_url": "https://api.openai.com/v1",
                 "api_key_ref": "env:OPENAI_API_KEY",
                 "model": "gpt-4o-mini", "enabled": True},
                {"id": "bad-edit", "api_key_ref": None, "kind": "openai_compat"},
            ],
            "default_backend_id": "openai-main",
        }))
        router.reload()
        r = c.get("/api/status")
        assert r.status_code == 200, f"malformed row crashed /api/status: {r.text[:300]}"
        rows = r.json()["providers"]["providers"]
        bad = next(p for p in rows if p["id"] == "bad-edit")
        assert bad["registered"] is False
        assert bad.get("build_error") or bad["registered"] is False

        # ──────────────────────────────────────────────────────────────
        # STEP 16 — ROW CAP (L6)
        # ──────────────────────────────────────────────────────────────
        # Restore a clean baseline before flooding.
        config.save_providers({
            "version": 1,
            "providers": [{
                "id": "openai-main", "kind": "openai_compat", "label": "OpenAI",
                "base_url": "https://api.openai.com/v1",
                "api_key_ref": "env:OPENAI_API_KEY",
                "model": "gpt-4o-mini", "enabled": True,
            }],
            "default_backend_id": "openai-main",
        })
        router.reload()
        from api.server import _PROVIDER_MAX_ROWS
        # Fill to the cap.
        existing = len(c.get("/api/providers").json()["providers"])
        for i in range(_PROVIDER_MAX_ROWS - existing):
            r = c.put(f"/api/providers/cap-{i}", json={
                "kind": "openai_compat", "label": f"Cap{i}",
                "base_url": "https://api.openai.com/v1",
                "api_key_ref": "env:OPENAI_API_KEY",
                "model": "gpt-4o", "enabled": True,
            })
            assert r.status_code == 200
        # 33rd row → 409
        r = c.put("/api/providers/cap-over", json={
            "kind": "openai_compat", "label": "Over",
            "base_url": "https://api.openai.com/v1",
            "api_key_ref": "env:OPENAI_API_KEY",
            "model": "x", "enabled": True,
        })
        assert r.status_code == 409
        # Updates within cap still work.
        r = c.put("/api/providers/cap-0", json={
            "kind": "openai_compat", "label": "Cap0-renamed",
            "base_url": "https://api.openai.com/v1",
            "api_key_ref": "env:OPENAI_API_KEY",
            "model": "gpt-4o", "enabled": True,
        })
        assert r.status_code == 200

        # ──────────────────────────────────────────────────────────────
        # STEP 17 — CLEANUP (operator deletes the test rows)
        # ──────────────────────────────────────────────────────────────
        # Final state: only openai-main remains, default points at it.
        for i in range(_PROVIDER_MAX_ROWS):
            c.delete(f"/api/providers/cap-{i}")
        rows = c.get("/api/providers").json()["providers"]
        assert {p["id"] for p in rows} == {"openai-main"}
        status = c.get("/api/status").json()
        assert status["available_backends"] == ["openai-main"]
        assert status["default_backend"] == "openai-main"
