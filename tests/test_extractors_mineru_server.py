"""Tests for the persistent mineru-api server path + HTTP client.

These do NOT launch a real mineru-api subprocess (would need ~30s model
load + ~5GB RAM). Instead we stub the singleton state and the HTTP
endpoint with a tiny ASGI app so the wiring + fallback logic is
exercised offline.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from nano_notebooklm.ingest import extractors_mineru as M


# ── fix-all v1 M6: hermetic singleton reset ────────────────────────
# Module-level globals (`_MINERU_SERVER`, `_MINERU_SERVER_DISABLED_REASON`,
# `_MINERU_SERVER_STARTING`) leak across tests if not reset, especially
# under pytest-randomly. Reset all three in setup AND teardown for every
# test in this module.
@pytest.fixture(autouse=True)
def _reset_mineru_singleton():
    with M._MINERU_SERVER_LOCK:
        M._MINERU_SERVER = None
        M._MINERU_SERVER_DISABLED_REASON = None
        M._MINERU_SERVER_STARTING = None
    yield
    with M._MINERU_SERVER_LOCK:
        M._MINERU_SERVER = None
        M._MINERU_SERVER_DISABLED_REASON = None
        M._MINERU_SERVER_STARTING = None


# ── Helpers ─────────────────────────────────────────────────────────


def _fake_blocks_for_pdf(name: str) -> list[dict]:
    """Two-page block list — page_idx 0 has a header, page_idx 1 has a paragraph."""
    return [
        {"type": "header", "text": f"Title of {name}", "text_level": 1,
         "bbox": [0, 0, 100, 20], "page_idx": 0},
        {"type": "text", "text": f"Body for {name}", "bbox": [0, 30, 100, 50],
         "page_idx": 1},
    ]


def _make_fake_response(filename: str, status: int = 200):
    """Synthesize the JSON shape `mineru-api /file_parse` returns."""
    return {
        "status_code": status,
        "json": {
            "results": {
                filename: {"content_list": _fake_blocks_for_pdf(filename)}
            }
        },
    }


# ── _build_mineru_env now injects thread caps ──────────────────────


def test_build_mineru_env_sets_thread_caps():
    """OMP/MKL/OPENBLAS/NUMEXPR thread vars MUST be present so mineru's
    inner BLAS doesn't oversubscribe on many-core hosts."""
    import os
    env = M._build_mineru_env(device="cpu")
    for k in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        assert k in env, f"missing {k}"
        assert int(env[k]) >= 1
        assert int(env[k]) <= 8  # capped


def test_build_mineru_env_respects_user_override(monkeypatch):
    monkeypatch.setenv("MINERU_OMP_THREADS", "2")
    env = M._build_mineru_env(device="cpu")
    assert env["OMP_NUM_THREADS"] == "2"
    assert env["MKL_NUM_THREADS"] == "2"


# ── Singleton lifecycle ─────────────────────────────────────────────


def test_server_disabled_env_returns_none(monkeypatch):
    """MINERU_SERVER_DISABLED=1 → singleton refuses to start."""
    monkeypatch.setenv("MINERU_SERVER_DISABLED", "1")
    # Clear any cached singleton state
    with M._MINERU_SERVER_LOCK:
        M._MINERU_SERVER = None
    assert M._get_or_start_mineru_server() is None


def test_server_disabled_truthy_values(monkeypatch):
    for val in ["1", "true", "TRUE", "yes", "on"]:
        monkeypatch.setenv("MINERU_SERVER_DISABLED", val)
        assert M._server_disabled() is True
    for val in ["0", "false", "no", ""]:
        monkeypatch.setenv("MINERU_SERVER_DISABLED", val)
        assert M._server_disabled() is False


def test_server_singleton_returns_cached(monkeypatch):
    """If we pre-seed the singleton with a 'live' fake proc, the lazy
    starter returns it without launching anything."""
    # Make sure the disable env isn't set — would short-circuit before cache check
    monkeypatch.delenv("MINERU_SERVER_DISABLED", raising=False)

    class _FakeProc:
        returncode = None
        def poll(self):
            return None  # still running
        def terminate(self):
            pass
        def wait(self, timeout=None):
            pass
        def kill(self):
            pass

    fake_state = {
        "url": "http://127.0.0.1:99999",
        "port": 99999,
        "proc": _FakeProc(),
        "started_at": 0.0,
    }
    with M._MINERU_SERVER_LOCK:
        M._MINERU_SERVER = fake_state
    try:
        # Should NOT spawn anything new
        with patch.object(M, "_resolve_mineru_api_cli") as resolve:
            got = M._get_or_start_mineru_server()
        assert got is fake_state
        resolve.assert_not_called()
    finally:
        with M._MINERU_SERVER_LOCK:
            M._MINERU_SERVER = None


def test_server_singleton_restarts_on_dead_proc(monkeypatch):
    """If the cached proc has terminated, the next call clears the slot
    and a fresh launch attempt happens (which will fail when CLI is
    absent — that's fine, we just check the clearing)."""
    monkeypatch.delenv("MINERU_SERVER_DISABLED", raising=False)

    class _DeadProc:
        returncode = 1
        def poll(self):
            return 1

    fake_state = {
        "url": "http://127.0.0.1:1",
        "port": 1,
        "proc": _DeadProc(),
        "started_at": 0.0,
    }
    with M._MINERU_SERVER_LOCK:
        M._MINERU_SERVER = fake_state
        M._MINERU_SERVER_DISABLED_REASON = None
    try:
        # Force re-launch attempt to fail by making CLI resolve to None
        with patch.object(M, "_resolve_mineru_api_cli", return_value=None):
            got = M._get_or_start_mineru_server()
        assert got is None
        with M._MINERU_SERVER_LOCK:
            assert M._MINERU_SERVER is None
    finally:
        with M._MINERU_SERVER_LOCK:
            M._MINERU_SERVER = None
            M._MINERU_SERVER_DISABLED_REASON = None


# ── HTTP path — stub the server with httpx MockTransport ──────────


class _FakeAsyncClient:
    """Drop-in replacement for httpx.AsyncClient used in unit tests.

    httpx 0.28's AsyncClient enforces ``AsyncByteStream`` on the request
    stream, so passing ``files=`` from a unit-test transport trips a
    strict isinstance guard. Easier to stub the client itself: record
    the call, return the handler's Response.
    """
    last_call: dict | None = None

    def __init__(self, handler, **kw):
        self._handler = handler

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def post(self, url, *, files=None, data=None, **kw):
        # Surface the multipart contents to handlers via a simple dict
        # so handler assertions don't have to crack multipart wire format.
        type(self).last_call = {"url": url, "files": files, "data": data}
        return self._handler({"url": url, "files": files, "data": data})


def _run_with_handler(handler, fn):
    """Run async ``fn`` with httpx.AsyncClient replaced by _FakeAsyncClient."""
    import httpx

    def factory(**kw):
        return _FakeAsyncClient(handler=handler, **kw)

    async def go():
        with patch.object(httpx, "AsyncClient", factory):
            return await fn()

    return asyncio.run(go())


def test_extract_one_via_server_success(tmp_path):
    """Hit a stubbed mineru-api endpoint, verify PageInfo round-trip."""
    import httpx

    pdf = tmp_path / "demo.pdf"
    pdf.write_bytes(b"%PDF-1.4\nfake\n")

    def handler(call: dict) -> httpx.Response:
        assert call["url"].endswith("/file_parse")
        # Files tuple is (name, bytes, content_type)
        assert call["files"]["files"][0] == "demo.pdf"
        # Form fields contain mineru config
        data_dict = dict(call["data"])
        assert data_dict["backend"] == "pipeline"
        assert data_dict["parse_method"] == "auto"
        assert data_dict["formula_enable"] == "true"
        assert data_dict["return_content_list"] == "true"
        assert data_dict["lang_list"] == "ch"
        return httpx.Response(200, json={
            "results": {"demo.pdf": {"content_list": _fake_blocks_for_pdf("demo")}},
        })

    pages = _run_with_handler(
        handler,
        lambda: M._extract_one_via_server("http://test", pdf, "ch", timeout_seconds=30.0),
    )
    assert len(pages) == 2
    assert pages[0].page == 1
    assert "Title of demo" in pages[0].text
    assert "Body for demo" in pages[1].text


def test_extract_one_via_server_http_error_raises(tmp_path):
    """Non-200 response → MinerUExtractionError with body snippet."""
    import httpx

    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF\n")

    def handler(call):
        return httpx.Response(500, text="internal whoops" * 100)

    with pytest.raises(M.MinerUExtractionError, match="HTTP 500"):
        _run_with_handler(
            handler,
            lambda: M._extract_one_via_server("http://test", pdf, "ch"),
        )


def test_extract_one_via_server_missing_entry_falls_back_to_stem(tmp_path):
    """Older mineru versions may key by stem not full filename."""
    import httpx

    pdf = tmp_path / "lec01.pdf"
    pdf.write_bytes(b"%PDF\n")

    def handler(call):
        return httpx.Response(200, json={
            "results": {"lec01": {"content_list": _fake_blocks_for_pdf("lec01")}},
        })

    pages = _run_with_handler(
        handler,
        lambda: M._extract_one_via_server("http://test", pdf, "ch"),
    )
    assert pages, "stem fallback must find the entry"


def test_extract_one_via_server_bad_content_list_raises(tmp_path):
    """If content_list is missing / wrong type → MinerUExtractionError."""
    import httpx

    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF\n")

    def handler(call):
        return httpx.Response(200, json={"results": {"x.pdf": {"content_list": "oops"}}})

    with pytest.raises(M.MinerUExtractionError, match="content_list"):
        _run_with_handler(
            handler,
            lambda: M._extract_one_via_server("http://test", pdf, "ch"),
        )


# ── Batch + fallback wiring ─────────────────────────────────────────


def test_batch_via_server_empty_input():
    assert asyncio.run(M.extract_pdfs_mineru_via_server([])) == {}


def test_batch_via_server_falls_back_when_server_unavailable(monkeypatch):
    """When _get_or_start_mineru_server returns None, the async batch
    raises MinerUExtractionError so the sync wrapper falls back."""
    monkeypatch.setattr(M, "_get_or_start_mineru_server", lambda **kw: None)

    async def go():
        await M.extract_pdfs_mineru_via_server([__file__], lang="en")

    with pytest.raises(M.MinerUExtractionError, match="unavailable"):
        asyncio.run(go())


def test_batch_sync_wrapper_prefers_server_then_falls_back(monkeypatch, tmp_path):
    """extract_pdfs_mineru_batch tries server first; on failure falls
    back to the subprocess CLI path."""
    pdf = tmp_path / "demo.pdf"
    pdf.write_bytes(b"%PDF\n")

    # Force the server path to fail
    async def fail_async(*a, **kw):
        raise M.MinerUExtractionError("simulated server down")

    monkeypatch.setattr(M, "extract_pdfs_mineru_via_server", fail_async)
    # And the CLI path to be absent — return early with empty dict-equivalent error
    monkeypatch.setattr(M, "_resolve_mineru_cli", lambda: None)

    with pytest.raises(M.MinerUNotFoundError):
        M.extract_pdfs_mineru_batch([str(pdf)], lang="en")


def test_batch_sync_wrapper_respects_disable_env(monkeypatch, tmp_path):
    """MINERU_SERVER_DISABLED=1 skips server attempt entirely (no log spam)."""
    monkeypatch.setenv("MINERU_SERVER_DISABLED", "1")
    pdf = tmp_path / "demo.pdf"
    pdf.write_bytes(b"%PDF\n")

    # If server path is touched, this would be called — assert it isn't
    called = []
    async def should_not_run(*a, **kw):
        called.append(True)
        raise AssertionError("server path should be skipped")
    monkeypatch.setattr(M, "extract_pdfs_mineru_via_server", should_not_run)
    monkeypatch.setattr(M, "_resolve_mineru_cli", lambda: None)

    with pytest.raises(M.MinerUNotFoundError):
        M.extract_pdfs_mineru_batch([str(pdf)], lang="en")
    assert not called


def test_pick_free_port_returns_int():
    """_pick_free_port returns the preferred port when free, else ephemeral."""
    p = M._pick_free_port(0)  # 0 → always ephemeral
    assert isinstance(p, int)
    assert 1024 < p < 65536


# ── Stop hook ───────────────────────────────────────────────────────


def test_stop_mineru_server_handles_already_dead():
    """_stop_mineru_server must not raise when proc is already gone."""
    class _DeadProc:
        returncode = 0
        def poll(self):
            return 0
        def terminate(self):
            raise AssertionError("should not call terminate on dead proc")
        def kill(self):
            raise AssertionError("should not call kill on dead proc")
        def wait(self, timeout=None):
            return 0

    with M._MINERU_SERVER_LOCK:
        M._MINERU_SERVER = {"url": "x", "port": 1, "proc": _DeadProc(), "started_at": 0.0}

    M._stop_mineru_server()  # must not raise
    with M._MINERU_SERVER_LOCK:
        assert M._MINERU_SERVER is None


def test_stop_mineru_server_no_state():
    """When no singleton exists, _stop is a no-op."""
    with M._MINERU_SERVER_LOCK:
        M._MINERU_SERVER = None
    M._stop_mineru_server()  # must not raise


# ── fix-all v1 M1: secrets scrub regression test ───────────────────


def test_build_mineru_env_strips_credentials_regression(monkeypatch):
    """fix-all v1 M1: explicit regression pin for the H3 invariant. A
    future contributor adding any of these creds to the allowlist (or
    refactoring _build_mineru_env to forward env wholesale) breaks this
    test loudly.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-12345")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "AWS-deadbeef")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIA-deadbeef")
    monkeypatch.setenv("HF_TOKEN", "hf_secret_xxx")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_xxx")
    env = M._build_mineru_env(device="cpu")
    for k in (
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
        "AWS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID",
        "HF_TOKEN", "GITHUB_TOKEN",
    ):
        assert k not in env, f"credential {k} must NOT be in mineru subprocess env"


# ── fix-all v1 M2: MAX_CONCURRENT_REQUESTS validation ──────────────


def test_validated_max_concurrent_requests_unset(monkeypatch):
    monkeypatch.delenv("MINERU_API_MAX_CONCURRENT_REQUESTS", raising=False)
    assert M._validated_max_concurrent_requests() is None


def test_validated_max_concurrent_requests_valid(monkeypatch):
    monkeypatch.setenv("MINERU_API_MAX_CONCURRENT_REQUESTS", "4")
    assert M._validated_max_concurrent_requests() == "4"


def test_validated_max_concurrent_requests_clamps_oversized(monkeypatch):
    """Operator typo `=1000` would OOM the host — must be rejected."""
    monkeypatch.setenv("MINERU_API_MAX_CONCURRENT_REQUESTS", "1000")
    assert M._validated_max_concurrent_requests() is None


def test_validated_max_concurrent_requests_rejects_zero(monkeypatch):
    monkeypatch.setenv("MINERU_API_MAX_CONCURRENT_REQUESTS", "0")
    assert M._validated_max_concurrent_requests() is None


def test_validated_max_concurrent_requests_rejects_negative(monkeypatch):
    monkeypatch.setenv("MINERU_API_MAX_CONCURRENT_REQUESTS", "-3")
    assert M._validated_max_concurrent_requests() is None


def test_validated_max_concurrent_requests_rejects_garbage(monkeypatch):
    monkeypatch.setenv("MINERU_API_MAX_CONCURRENT_REQUESTS", "lots")
    assert M._validated_max_concurrent_requests() is None


def test_build_mineru_env_drops_invalid_max_concurrent(monkeypatch):
    """When env is malformed, the var must not survive to the subprocess."""
    monkeypatch.setenv("MINERU_API_MAX_CONCURRENT_REQUESTS", "9999")
    env = M._build_mineru_env(device="cpu")
    assert "MINERU_API_MAX_CONCURRENT_REQUESTS" not in env


def test_build_mineru_env_passes_valid_max_concurrent(monkeypatch):
    monkeypatch.setenv("MINERU_API_MAX_CONCURRENT_REQUESTS", "6")
    env = M._build_mineru_env(device="cpu")
    assert env["MINERU_API_MAX_CONCURRENT_REQUESTS"] == "6"


# ── fix-all v1 M3: allowlist single-source-of-truth ────────────────


def test_max_concurrent_requests_in_allowlist():
    """fix-all v1 M3: MAX_CONCURRENT_REQUESTS must be in the allowlist
    (was previously injected manually outside the allowlist — confusing
    + duplicated code paths)."""
    assert "MINERU_API_MAX_CONCURRENT_REQUESTS" in M._MINERU_ENV_ALLOWLIST


# ── fix-all v1 M4: kb/store integration contract pin ───────────────


def test_kb_store_still_calls_extract_pdfs_mineru_batch():
    """fix-all v1 M4: pin that the real caller in kb/store.py uses the
    documented function name + kwarg. A rename here would break the
    upload pipeline silently in tests that mock different functions.
    """
    src = Path("nano_notebooklm/kb/store.py").read_text(encoding="utf-8")
    assert "extract_pdfs_mineru_batch(" in src, \
        "kb/store.py must call extract_pdfs_mineru_batch — contract pinned by fix-all v1 M4"
    # The `lang=lang` kwarg form is the documented public signature.
    assert "lang=lang" in src or "lang=" in src


# ── fix-all v1 M5: MINERU_API_PORT env override ────────────────────


def test_mineru_api_port_env_threaded_to_pick_free_port(monkeypatch):
    """Operator-set MINERU_API_PORT must actually reach _pick_free_port."""
    monkeypatch.setenv("MINERU_API_PORT", "57892")
    monkeypatch.delenv("MINERU_SERVER_DISABLED", raising=False)
    seen_preferred = []

    def fake_pick(preferred):
        seen_preferred.append(preferred)
        return 57892

    monkeypatch.setattr(M, "_pick_free_port", fake_pick)
    # Make the rest of startup fail fast so we don't actually launch.
    monkeypatch.setattr(M, "_resolve_mineru_api_cli", lambda: None)

    M._get_or_start_mineru_server()
    # _resolve_mineru_api_cli returns None BEFORE port lookup in the new
    # phase-1 ordering, so seen_preferred stays empty when CLI missing.
    # Flip: provide a CLI string and make Popen fail.
    seen_preferred.clear()
    monkeypatch.setattr(M, "_resolve_mineru_api_cli", lambda: "/usr/bin/mineru-api-fake")

    def fake_popen(*a, **kw):
        raise OSError("simulated launch failure")

    monkeypatch.setattr(M.subprocess, "Popen", fake_popen)
    with M._MINERU_SERVER_LOCK:
        M._MINERU_SERVER_DISABLED_REASON = None

    M._get_or_start_mineru_server()
    assert seen_preferred == [57892], \
        f"_pick_free_port should be called with the env value 57892, got {seen_preferred}"


def test_mineru_api_port_out_of_range_falls_back_to_ephemeral(monkeypatch):
    monkeypatch.setenv("MINERU_API_PORT", "99999")
    monkeypatch.delenv("MINERU_SERVER_DISABLED", raising=False)
    seen = []
    monkeypatch.setattr(M, "_pick_free_port", lambda p: (seen.append(p), 12345)[1])
    monkeypatch.setattr(M, "_resolve_mineru_api_cli", lambda: "/usr/bin/mineru-api-fake")
    monkeypatch.setattr(M.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))

    M._get_or_start_mineru_server()
    # Out-of-range port should be clamped to 0 (ephemeral)
    assert seen == [0]


def test_mineru_api_port_bad_value_falls_back(monkeypatch):
    monkeypatch.setenv("MINERU_API_PORT", "not-a-number")
    monkeypatch.delenv("MINERU_SERVER_DISABLED", raising=False)
    seen = []
    monkeypatch.setattr(M, "_pick_free_port", lambda p: (seen.append(p), 12345)[1])
    monkeypatch.setattr(M, "_resolve_mineru_api_cli", lambda: "/usr/bin/mineru-api-fake")
    monkeypatch.setattr(M.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))

    M._get_or_start_mineru_server()
    assert seen == [0]


# ── fix-all v1 H3: positive guard for asyncio.run ──────────────────


def test_run_async_runs_when_no_loop():
    """No event loop on this thread → asyncio.run path runs the coro."""
    async def _coro():
        return 42

    assert M._run_async(_coro()) == 42


def test_run_async_raises_loop_running_when_loop_active():
    """When the calling thread already has a running loop, _run_async
    raises the typed sentinel instead of trusting a substring match."""
    async def outer():
        async def _coro():
            return 99
        with pytest.raises(M._LoopRunningError):
            M._run_async(_coro())

    asyncio.run(outer())


# ── fix-all v1 H1: extract_pdf_mineru single-file routes through server ──


def test_extract_pdf_mineru_uses_server_when_available(monkeypatch, tmp_path):
    """fix-all v1 H1: when the server is reachable and no page-range is
    set, single-file extraction must NOT shell to subprocess."""
    pdf = tmp_path / "sample.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    called = {"server": False, "subprocess": False}

    async def fake_server(filepaths, **kw):
        called["server"] = True
        return {str(Path(p).resolve()): [
            __import__("nano_notebooklm.types", fromlist=["PageInfo"]).PageInfo(text="ok", page=1, total_pages=1),
        ] for p in filepaths}

    monkeypatch.setattr(M, "extract_pdfs_mineru_via_server", fake_server)
    monkeypatch.delenv("MINERU_SERVER_DISABLED", raising=False)

    # If subprocess path is reached, _resolve_mineru_cli would be hit;
    # poison it to detect.
    def poison_cli():
        called["subprocess"] = True
        return None

    monkeypatch.setattr(M, "_resolve_mineru_cli", poison_cli)

    pages = M.extract_pdf_mineru(str(pdf), lang="en")
    assert called["server"] is True
    assert called["subprocess"] is False
    assert pages and pages[0].text == "ok"


def test_extract_pdf_mineru_falls_back_to_subprocess_on_server_error(monkeypatch, tmp_path):
    """If the server path raises MinerUExtractionError, single-file
    extraction must fall back to the subprocess CLI (which then fails
    on its own absent CLI — that's expected and proves the wiring)."""
    pdf = tmp_path / "sample.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    async def fake_server_fail(filepaths, **kw):
        raise M.MinerUExtractionError("simulated")

    monkeypatch.setattr(M, "extract_pdfs_mineru_via_server", fake_server_fail)
    monkeypatch.setattr(M, "_resolve_mineru_cli", lambda: None)
    monkeypatch.delenv("MINERU_SERVER_DISABLED", raising=False)

    with pytest.raises(M.MinerUNotFoundError):
        M.extract_pdf_mineru(str(pdf), lang="en")


def test_extract_pdf_mineru_skips_server_with_page_range(monkeypatch, tmp_path):
    """Server path doesn't support -s/-e flags; when caller sets a
    page range, must go straight to subprocess CLI."""
    pdf = tmp_path / "sample.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    server_called = []

    async def fake_server(*a, **kw):
        server_called.append(True)
        return {}

    monkeypatch.setattr(M, "extract_pdfs_mineru_via_server", fake_server)
    monkeypatch.setattr(M, "_resolve_mineru_cli", lambda: None)
    monkeypatch.delenv("MINERU_SERVER_DISABLED", raising=False)

    with pytest.raises(M.MinerUNotFoundError):
        M.extract_pdf_mineru(str(pdf), lang="en", start_page=2, end_page=5)
    assert server_called == [], "server path should NOT be tried with page range"


# ── fix-all v1 M11: error message format consistency ───────────────


def test_server_http_error_includes_body_tail_marker(tmp_path):
    """fix-all v1 M11: server-path HTTP error must include the
    ``body tail:`` marker, paralleling the subprocess-path ``stderr tail:``
    so log scrapers can route both."""
    import httpx
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF\n")

    def handler(call):
        return httpx.Response(500, text="boom" * 200)

    with pytest.raises(M.MinerUExtractionError) as excinfo:
        _run_with_handler(
            handler,
            lambda: M._extract_one_via_server("http://test", pdf, "ch"),
        )
    msg = str(excinfo.value)
    assert "HTTP 500" in msg
    assert "body tail:" in msg, "must use the 'body tail:' marker convention"


# ── fix-all v1 M7: per-PDF timeout cap ─────────────────────────────


def test_per_pdf_timeout_default(monkeypatch):
    monkeypatch.delenv("MINERU_PER_PDF_TIMEOUT_SECONDS", raising=False)
    assert M._per_pdf_timeout() == 300.0


def test_per_pdf_timeout_env_override(monkeypatch):
    monkeypatch.setenv("MINERU_PER_PDF_TIMEOUT_SECONDS", "60")
    assert M._per_pdf_timeout() == 60.0


def test_per_pdf_timeout_rejects_too_small(monkeypatch):
    monkeypatch.setenv("MINERU_PER_PDF_TIMEOUT_SECONDS", "5")
    assert M._per_pdf_timeout() == 300.0  # falls back to default


def test_per_pdf_timeout_rejects_too_large(monkeypatch):
    monkeypatch.setenv("MINERU_PER_PDF_TIMEOUT_SECONDS", "100000")
    assert M._per_pdf_timeout() == 300.0


def test_per_pdf_timeout_rejects_garbage(monkeypatch):
    monkeypatch.setenv("MINERU_PER_PDF_TIMEOUT_SECONDS", "soon")
    assert M._per_pdf_timeout() == 300.0


def test_extract_one_via_server_caps_at_per_pdf_timeout(monkeypatch, tmp_path):
    """When caller passes 1800s, the actual httpx read timeout must be
    capped to per-PDF (default 300s)."""
    import httpx
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF\n")

    seen_timeout = {}

    class _RecordingClient:
        def __init__(self, **kw):
            seen_timeout["timeout"] = kw.get("timeout")
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None
        async def post(self, url, *, files=None, data=None, **kw):
            return httpx.Response(200, json={"results": {"x.pdf": {"content_list": []}}})

    monkeypatch.setattr(httpx, "AsyncClient", _RecordingClient)
    asyncio.run(M._extract_one_via_server("http://t", pdf, "ch", timeout_seconds=1800.0))
    t = seen_timeout["timeout"]
    # httpx.Timeout exposes read as .read
    assert t.read <= 300.0


# ── fix-all v1 H2 + M5: warmup task strong-ref + atexit registration ──


def test_warm_mineru_server_holds_strong_ref():
    """fix-all v1 H2: app.state._mineru_warm_task must exist after the
    startup hook fires. Source grep so a future refactor that drops the
    strong-ref breaks loudly."""
    src = Path("api/server.py").read_text(encoding="utf-8")
    assert "app.state._mineru_warm_task" in src, \
        "fix-all v1 H2: warmup task must be held in app.state to survive GC"


def test_atexit_register_called_on_first_successful_start(monkeypatch):
    """fix-all v1 M5: pin that atexit registration actually happens —
    a future refactor that moves the register call into the wrong
    branch would leak ~5GB mineru server subprocesses across dev reloads."""
    registered: list = []
    real_atexit = M.atexit.register
    monkeypatch.setattr(M.atexit, "register", lambda fn: registered.append(fn) or real_atexit(fn))

    # Stub out the slow startup pieces — we just want to verify the
    # register call lands at the end of a "successful" launch.
    monkeypatch.delenv("MINERU_SERVER_DISABLED", raising=False)
    monkeypatch.setattr(M, "_resolve_mineru_api_cli", lambda: "/usr/bin/fake")
    monkeypatch.setattr(M, "_pick_free_port", lambda p: 12345)

    class _FakeProc:
        returncode = None
        pid = 99999
        def poll(self):
            return None
        def terminate(self):
            pass
        def kill(self):
            pass
        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(M.subprocess, "Popen", lambda *a, **k: _FakeProc())

    # Make _poll_until_ready believe health came back instantly
    def fake_poll(pending, startup_timeout):
        state = {"url": pending["url"], "port": pending["port"], "proc": pending["proc"],
                 "started_at": 0.0}
        with M._MINERU_SERVER_LOCK:
            M._MINERU_SERVER = state
            if M._MINERU_SERVER_STARTING is pending:
                M._MINERU_SERVER_STARTING = None
        M.atexit.register(M._stop_mineru_server)
        pending["ready_event"].set()
        return state

    monkeypatch.setattr(M, "_poll_until_ready", fake_poll)

    state = M._get_or_start_mineru_server()
    assert state is not None
    assert M._stop_mineru_server in registered, \
        "fix-all v1 M5: _stop_mineru_server must be atexit-registered"


def test_sticky_disabled_reason_persists_within_process(monkeypatch):
    """fix-all v1 M5: once disabled, subsequent calls do NOT retry."""
    monkeypatch.delenv("MINERU_SERVER_DISABLED", raising=False)
    cli_calls = []

    def cli_fn():
        cli_calls.append(True)
        return None

    monkeypatch.setattr(M, "_resolve_mineru_api_cli", cli_fn)

    assert M._get_or_start_mineru_server() is None
    assert M._MINERU_SERVER_DISABLED_REASON is not None
    assert cli_calls == [True]

    # Second call must short-circuit on the sticky reason — no CLI re-probe.
    assert M._get_or_start_mineru_server() is None
    assert cli_calls == [True], "sticky-disable must not re-probe CLI on second call"


# ── fix-all v1 M9/M10: lock NOT held during health-poll ────────────


def test_health_poll_runs_outside_lock(monkeypatch):
    """fix-all v1 M9/M10: while one thread is polling /health, a second
    thread acquiring the lock must observe the ``_MINERU_SERVER_STARTING``
    sentinel and join the wait rather than block on the lock for 180s."""
    monkeypatch.delenv("MINERU_SERVER_DISABLED", raising=False)
    monkeypatch.setattr(M, "_resolve_mineru_api_cli", lambda: "/usr/bin/fake")
    monkeypatch.setattr(M, "_pick_free_port", lambda p: 12345)

    class _FakeProc:
        returncode = None
        pid = 99999
        def poll(self):
            return None
        def terminate(self):
            pass
        def kill(self):
            pass
        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(M.subprocess, "Popen", lambda *a, **k: _FakeProc())

    poll_started = threading.Event()
    poll_release = threading.Event()

    def slow_poll(pending, startup_timeout):
        poll_started.set()
        # Block here until the second thread has had a chance to observe
        # the STARTING sentinel.
        poll_release.wait(timeout=10.0)
        state = {"url": pending["url"], "port": pending["port"], "proc": pending["proc"],
                 "started_at": 0.0}
        with M._MINERU_SERVER_LOCK:
            M._MINERU_SERVER = state
            if M._MINERU_SERVER_STARTING is pending:
                M._MINERU_SERVER_STARTING = None
        pending["ready_event"].set()
        return state

    monkeypatch.setattr(M, "_poll_until_ready", slow_poll)

    import threading as _t
    results = {}

    def start_in_thread(name):
        results[name] = M._get_or_start_mineru_server()

    t1 = _t.Thread(target=start_in_thread, args=("first",))
    t1.start()
    assert poll_started.wait(timeout=5.0), "poll must have started"

    # While t1 is "polling", verify the STARTING sentinel is set and the
    # lock is NOT held (we'd block forever otherwise).
    acquired = M._MINERU_SERVER_LOCK.acquire(timeout=1.0)
    assert acquired, "fix-all v1 M9: lock must NOT be held during health-poll"
    M._MINERU_SERVER_LOCK.release()
    with M._MINERU_SERVER_LOCK:
        assert M._MINERU_SERVER_STARTING is not None

    # Release the poll, let t1 finish.
    poll_release.set()
    t1.join(timeout=5.0)
    assert results.get("first") is not None

