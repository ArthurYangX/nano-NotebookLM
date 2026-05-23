"""ModelRouter.reload() invariants for the providers-matrix migration.

Pinned:
- reload() is atomic: an exception mid-build leaves self.backends
  unchanged (no partial wipe).
- A row whose api_key_ref resolves to "" is skipped (no dead entry).
- `_active_default_id` falls back through providers.json →
  DEFAULT_BACKEND env → first available, and never returns an id that
  isn't in self.backends (callers depend on that to skip an extra
  membership check).
- `get_openai_compat()` ignores anthropic rows and prefers the active
  default when it's openai-compat.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def isolated_router(monkeypatch, tmp_path):
    from nano_notebooklm import config
    from nano_notebooklm.ai.router import ModelRouter

    monkeypatch.setattr(config, "PROVIDERS_FILE", tmp_path / "providers.json")
    monkeypatch.setenv("OPENAI_API_KEY", "test-sk-openai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-sk-anthropic")

    def _write(rows, default_id=None):
        config.PROVIDERS_FILE.write_text(json.dumps({
            "version": 1,
            "providers": rows,
            "default_backend_id": default_id,
        }))

    _write([
        {
            "id": "o1", "kind": "openai_compat", "label": "O1",
            "base_url": "https://api.openai.com/v1",
            "api_key_ref": "env:OPENAI_API_KEY",
            "model": "gpt-4o-mini", "enabled": True,
        },
        {
            "id": "c1", "kind": "anthropic", "label": "C1",
            "base_url": None,
            "api_key_ref": "env:ANTHROPIC_API_KEY",
            "model": "claude-sonnet-4-5", "enabled": True,
        },
    ], default_id="o1")
    r = ModelRouter()
    return r, _write


def test_reload_picks_up_new_row(isolated_router, monkeypatch):
    r, write = isolated_router
    assert set(r.backends.keys()) == {"o1", "c1"}
    write([
        {"id": "o1", "kind": "openai_compat", "label": "O1",
         "base_url": "https://api.openai.com/v1",
         "api_key_ref": "env:OPENAI_API_KEY",
         "model": "gpt-4o-mini", "enabled": True},
        {"id": "o2", "kind": "openai_compat", "label": "O2",
         "base_url": "https://api.openai.com/v1",
         "api_key_ref": "env:OPENAI_API_KEY",
         "model": "gpt-4o", "enabled": True},
    ], default_id="o1")
    r.reload()
    assert set(r.backends.keys()) == {"o1", "o2"}


def test_reload_skips_row_with_empty_api_key(isolated_router, monkeypatch):
    """A row whose env:VAR resolves to empty must be skipped, not
    registered with a dead key (which would surface as cryptic 401s
    downstream)."""
    r, write = isolated_router
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    write([
        {"id": "o1", "kind": "openai_compat", "label": "O1",
         "base_url": "https://api.openai.com/v1",
         "api_key_ref": "env:OPENAI_API_KEY",
         "model": "gpt-4o-mini", "enabled": True},
        {"id": "c1", "kind": "anthropic", "label": "C1",
         "base_url": None,
         "api_key_ref": "env:ANTHROPIC_API_KEY",
         "model": "claude-sonnet-4-5", "enabled": True},
    ], default_id="o1")
    r.reload()
    assert "o1" in r.backends
    assert "c1" not in r.backends


def test_resolve_backend_rejects_unknown_id(isolated_router):
    r, _ = isolated_router
    with pytest.raises(RuntimeError, match="not configured"):
        r._resolve_backend("", "no-such-id")


def test_active_default_id_falls_back_to_first_available(isolated_router):
    r, write = isolated_router
    # default_backend_id missing → first available
    write([
        {"id": "o1", "kind": "openai_compat", "label": "O1",
         "base_url": "https://api.openai.com/v1",
         "api_key_ref": "env:OPENAI_API_KEY",
         "model": "gpt-4o-mini", "enabled": True},
    ], default_id=None)
    r.reload()
    assert r._active_default_id() == "o1"


def test_active_default_id_drops_stale_pointer(isolated_router):
    """If default_backend_id points at a deleted row, the resolver must
    fall through to the env mapping / first available — never return an
    id that isn't in self.backends."""
    r, write = isolated_router
    write([
        {"id": "o1", "kind": "openai_compat", "label": "O1",
         "base_url": "https://api.openai.com/v1",
         "api_key_ref": "env:OPENAI_API_KEY",
         "model": "gpt-4o-mini", "enabled": True},
    ], default_id="deleted-id")
    r.reload()
    did = r._active_default_id()
    assert did in r.backends
    assert did != "deleted-id"


def test_get_openai_compat_skips_anthropic(isolated_router, monkeypatch):
    """Endpoints that hard-require OpenAI-compat (agent, explain-node)
    must NOT receive a Claude backend when only Claude has the active
    default — `get_openai_compat` falls back to any other openai-compat
    row, returning None only when nothing matches."""
    r, write = isolated_router
    write([
        {"id": "c1", "kind": "anthropic", "label": "C1",
         "base_url": None,
         "api_key_ref": "env:ANTHROPIC_API_KEY",
         "model": "claude-sonnet-4-5", "enabled": True},
        {"id": "o1", "kind": "openai_compat", "label": "O1",
         "base_url": "https://api.openai.com/v1",
         "api_key_ref": "env:OPENAI_API_KEY",
         "model": "gpt-4o-mini", "enabled": True},
    ], default_id="c1")
    r.reload()
    chosen = r.get_openai_compat()
    assert chosen is not None
    assert chosen.kind == "openai_compat"
    assert chosen.name == "o1"

    # Only anthropic? returns None
    write([
        {"id": "c1", "kind": "anthropic", "label": "C1",
         "base_url": None,
         "api_key_ref": "env:ANTHROPIC_API_KEY",
         "model": "claude-sonnet-4-5", "enabled": True},
    ], default_id="c1")
    r.reload()
    assert r.get_openai_compat() is None


def test_get_backend_alternate_returns_non_default(isolated_router):
    """The legacy `alternate` task-route sentinel must still return a
    backend whose id ≠ default — used by cross-review prompts."""
    from nano_notebooklm import config
    r, _ = isolated_router
    monkeypatch_routes = dict(config.TASK_ROUTES)
    monkeypatch_routes["cross_review"] = "alternate"
    config.TASK_ROUTES.update(monkeypatch_routes)
    try:
        chosen = r.get_backend("cross_review")
        # default is "o1" — alternate should pick c1
        assert chosen.name == "c1"
    finally:
        config.TASK_ROUTES.pop("cross_review", None)
        config.TASK_ROUTES["cross_review"] = "alternate"  # restore default


def test_build_backend_renames_instance_to_provider_id(isolated_router):
    """`backend.name` is used in log lines + alternate lookup. With
    multiple openai-compat rows the class-level `name = "openai"` would
    collide, so _build_backend overrides the instance attribute with the
    provider id."""
    r, _ = isolated_router
    for pid, inst in r.backends.items():
        assert inst.name == pid


def test_bootstrap_idempotent(monkeypatch, tmp_path):
    """`bootstrap_providers_from_env` must be a no-op when providers.json
    already exists — operator edits win over env."""
    from nano_notebooklm import config

    monkeypatch.setattr(config, "PROVIDERS_FILE", tmp_path / "providers.json")
    # `_default_providers_from_env` reads the module-level
    # `OPENAI_API_KEY` constant captured at import (line 24 of config.py),
    # NOT `os.getenv` at call time. `monkeypatch.setenv` would not
    # propagate — see the project memory note about config defaults.
    monkeypatch.setattr(config, "OPENAI_API_KEY", "k1")

    config.bootstrap_providers_from_env()
    first = json.loads(config.PROVIDERS_FILE.read_text())
    # Now mutate the file as if the operator hand-edited
    first["providers"][0]["label"] = "OPERATOR EDITED"
    config.PROVIDERS_FILE.write_text(json.dumps(first))
    # Re-bootstrap; must not overwrite
    config.bootstrap_providers_from_env()
    second = json.loads(config.PROVIDERS_FILE.read_text())
    assert second["providers"][0]["label"] == "OPERATOR EDITED"


def test_bootstrap_idempotent_when_env_removed(monkeypatch, tmp_path):
    """Reviewer #4 finding 1: after first-boot bootstrap synthesises
    providers.json with `api_key_ref: "env:OPENAI_API_KEY"`, removing
    OPENAI_API_KEY from env on the next boot must NOT cause bootstrap
    to overwrite the file with an empty provider list."""
    from nano_notebooklm import config

    monkeypatch.setattr(config, "PROVIDERS_FILE", tmp_path / "providers.json")
    # See test_bootstrap_idempotent above for why setattr (not setenv).
    monkeypatch.setattr(config, "OPENAI_API_KEY", "k1")
    config.bootstrap_providers_from_env()
    first_bytes = config.PROVIDERS_FILE.read_bytes()
    assert b"openai-main" in first_bytes

    # Simulate "operator removed the key from env between boots".
    monkeypatch.setattr(config, "OPENAI_API_KEY", "")
    config.bootstrap_providers_from_env()
    second_bytes = config.PROVIDERS_FILE.read_bytes()
    assert first_bytes == second_bytes, (
        "removing the env var after bootstrap should NOT rewrite providers.json"
    )


def test_alternate_prefers_different_kind(isolated_router):
    """fix-all v1 M3: `cross_review`'s `alternate` sentinel used to
    return the first id ≠ default — with two openai_compat rows, that's
    same vendor / no real cross-vendor critique. Now prefers a different
    kind, falling back to "any non-default" only when there's no kind
    diversity."""
    from nano_notebooklm import config
    r, write = isolated_router
    # Three rows: openai-main (default), openai-alt (same kind), claude-main (different kind).
    write([
        {"id": "openai-main", "kind": "openai_compat", "label": "M",
         "base_url": "https://api.openai.com/v1",
         "api_key_ref": "env:OPENAI_API_KEY",
         "model": "gpt-4o-mini", "enabled": True},
        {"id": "openai-alt", "kind": "openai_compat", "label": "A",
         "base_url": "https://api.openai.com/v1",
         "api_key_ref": "env:OPENAI_API_KEY",
         "model": "gpt-4o", "enabled": True},
        {"id": "claude-main", "kind": "anthropic", "label": "C",
         "base_url": None,
         "api_key_ref": "env:ANTHROPIC_API_KEY",
         "model": "claude-sonnet-4-5", "enabled": True},
    ], default_id="openai-main")
    r.reload()
    try:
        # Inject the alternate route under cross_review
        config.TASK_ROUTES["cross_review"] = "alternate"
        chosen = r.get_backend("cross_review")
        # Must pick the anthropic row, not openai-alt
        assert chosen.kind == "anthropic", (
            f"alternate should prefer different kind, got {chosen.kind}/{chosen.name}"
        )
    finally:
        config.TASK_ROUTES["cross_review"] = "alternate"

    # And when there's no kind diversity, fall back to any non-default
    write([
        {"id": "openai-main", "kind": "openai_compat", "label": "M",
         "base_url": "https://api.openai.com/v1",
         "api_key_ref": "env:OPENAI_API_KEY",
         "model": "gpt-4o-mini", "enabled": True},
        {"id": "openai-alt", "kind": "openai_compat", "label": "A",
         "base_url": "https://api.openai.com/v1",
         "api_key_ref": "env:OPENAI_API_KEY",
         "model": "gpt-4o", "enabled": True},
    ], default_id="openai-main")
    r.reload()
    try:
        config.TASK_ROUTES["cross_review"] = "alternate"
        chosen = r.get_backend("cross_review")
        assert chosen.name == "openai-alt"
    finally:
        config.TASK_ROUTES["cross_review"] = "alternate"


def test_task_routes_legacy_short_names_aliased(isolated_router):
    """fix-all v1 H5: TASK_ROUTES values may carry pre-providers short
    names ("claude", "openai", "local"). The router must translate
    these to the bootstrap id so an operator's old hand-edited rule
    doesn't silently fall through to the wrong backend via
    `next(iter(...))`."""
    from nano_notebooklm import config
    r, write = isolated_router
    write([
        {"id": "openai-main", "kind": "openai_compat", "label": "M",
         "base_url": "https://api.openai.com/v1",
         "api_key_ref": "env:OPENAI_API_KEY",
         "model": "gpt-4o-mini", "enabled": True},
        {"id": "claude-main", "kind": "anthropic", "label": "C",
         "base_url": None,
         "api_key_ref": "env:ANTHROPIC_API_KEY",
         "model": "claude-sonnet-4-5", "enabled": True},
    ], default_id="openai-main")
    r.reload()
    try:
        config.TASK_ROUTES["report_writing"] = "claude"  # legacy short name
        chosen = r.get_backend("report_writing")
        assert chosen.name == "claude-main", (
            f"legacy 'claude' should alias to claude-main, got {chosen.name}"
        )
    finally:
        config.TASK_ROUTES.pop("report_writing", None)


def test_reload_caches_default_id_in_memory(isolated_router, monkeypatch):
    """fix-all v1 H3: `_active_default_id` used to hit disk on every
    LLM dispatch. Now caches on the router instance, invalidated by
    reload(). Verify by counting disk reads via monkeypatch."""
    from nano_notebooklm import config
    r, _ = isolated_router
    load_calls = []
    real_load = config.load_providers

    def counting_load():
        load_calls.append(1)
        return real_load()

    monkeypatch.setattr(config, "load_providers", counting_load)
    # First call populates cache (one read)
    r._default_id_cache = None  # force recompute
    n_before = len(load_calls)
    r._active_default_id()
    n_after_first = len(load_calls)
    assert n_after_first - n_before == 1
    # Subsequent calls hit cache (zero additional reads)
    for _ in range(10):
        r._active_default_id()
    assert len(load_calls) == n_after_first, "cache should suppress repeat disk reads"


def test_reload_preserves_old_backends_on_outer_exception(monkeypatch, isolated_router):
    """fix-all v1 M7: the reload() docstring claims old `self.backends`
    is preserved when the rebuild step raises. The internal try/except
    in `_build_backend` catches per-row errors, but a failure in
    `load_providers` itself should not zero out router state.
    """
    from nano_notebooklm import config
    r, _ = isolated_router
    snapshot = dict(r.backends)

    def boom():
        raise RuntimeError("simulated disk failure")

    monkeypatch.setattr(config, "load_providers", boom)
    with pytest.raises(RuntimeError, match="simulated disk failure"):
        r.reload()
    # Backends dict unchanged
    assert dict(r.backends) == snapshot


def test_build_backend_returns_reason(isolated_router):
    """fix-all v1 M4: _build_backend now returns (inst, reason) so the
    /test endpoint and _providers_payload can surface diagnosis."""
    r, _ = isolated_router
    # Disabled row → reason "disabled"
    inst, reason = r._build_backend({
        "id": "x", "kind": "openai_compat", "label": "X",
        "base_url": "https://api.openai.com/v1",
        "api_key_ref": "env:OPENAI_API_KEY",
        "model": "m", "enabled": False,
    })
    assert inst is None and reason == "disabled"
    # Missing env → reason includes "api_key_ref"
    inst, reason = r._build_backend({
        "id": "x", "kind": "openai_compat", "label": "X",
        "base_url": "https://api.openai.com/v1",
        "api_key_ref": "env:DEFINITELY_NOT_SET_XYZ",
        "model": "m", "enabled": True,
    })
    assert inst is None and "api_key_ref" in reason
    # Healthy row → reason None
    inst, reason = r._build_backend({
        "id": "x", "kind": "openai_compat", "label": "X",
        "base_url": "https://api.openai.com/v1",
        "api_key_ref": "env:OPENAI_API_KEY",
        "model": "m", "enabled": True,
    })
    assert inst is not None and reason is None


def test_provider_url_ssrf_guard(monkeypatch, tmp_path):
    """fix-all v1 H1: _validate_provider_url blocks metadata + RFC1918
    + link-local hosts for ALL kinds (not just openai_compat_local)."""
    from nano_notebooklm import config
    for bad in (
        "http://169.254.169.254/v1",       # AWS IMDS / link-local
        "http://10.0.0.5/v1",              # RFC1918
        "http://192.168.0.1/v1",
        "http://172.16.5.5/v1",
        "http://metadata.google.internal/",
        "http://100.100.100.200/",         # Alibaba metadata (blocklisted name)
    ):
        assert config._validate_provider_url(bad) == "", f"{bad!r} should be blocked"
    # Loopback + public should pass
    for good in (
        "http://127.0.0.1:8001/v1",
        "http://localhost:11434/v1",
        "https://api.openai.com/v1",
        "https://api.deepseek.com",
    ):
        assert config._validate_provider_url(good) == good.rstrip("/")
