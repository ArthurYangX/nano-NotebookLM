"""Tests for the LaTeX PDF compile endpoint (/api/notes/export/pdf).

Covers:
  - tectonic missing → 503 short-circuit (no subprocess fired)
  - sanitizer rejection → 422 (no subprocess fired)
  - happy path: subprocess writes PDF → 200 with application/pdf bytes
  - subprocess non-zero exit → 422 with log tail + exit_code
  - subprocess timeout → 504
  - body shape rejected by Pydantic (missing/blank latex_source) → 422
"""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def pdf_client(monkeypatch, tmp_path, sample_chunks, fake_embed_fn):
    art = tmp_path / "artifacts"
    (art / "courses" / "testcourse").mkdir(parents=True)
    (art / "courses" / "testcourse" / "chunks.json").write_text(
        json.dumps([c.model_dump() for c in sample_chunks], default=str)
    )
    monkeypatch.setenv("ARTIFACTS_DIR", str(art))

    from nano_notebooklm import config
    monkeypatch.setattr(config, "ARTIFACTS_DIR", art)
    from nano_notebooklm.kb import store as kb_store
    monkeypatch.setattr(kb_store, "_get_default_embed_fn", lambda: fake_embed_fn)

    import api.server as server_mod
    importlib.reload(server_mod)
    server_mod.kb.build_index("testcourse")
    return TestClient(server_mod.app), server_mod


def _set_tectonic(server_mod, *, available: bool, path: str = "/usr/local/bin/tectonic"):
    server_mod.app.state.tectonic_available = available
    server_mod.app.state.tectonic_path = path if available else None


def test_returns_503_when_tectonic_missing(pdf_client):
    client, server_mod = pdf_client
    _set_tectonic(server_mod, available=False)
    with patch("api.server.subprocess.run") as run_mock:
        resp = client.post("/api/notes/export/pdf", json={
            "course_id": "testcourse",
            "latex_source": r"\section{Hi}",
        })
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"] == "tectonic_unavailable"
    run_mock.assert_not_called()


def test_sanitizer_blocks_input_command(pdf_client):
    client, server_mod = pdf_client
    _set_tectonic(server_mod, available=True)
    with patch("api.server.subprocess.run") as run_mock:
        resp = client.post("/api/notes/export/pdf", json={
            "course_id": "testcourse",
            "latex_source": r"\section{Hi} \input{/etc/passwd}",
        })
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "latex_unsafe"
    assert "input" in body["reason"].lower()
    # Critically: subprocess MUST NOT have fired
    run_mock.assert_not_called()


def test_sanitizer_blocks_write18(pdf_client):
    client, server_mod = pdf_client
    _set_tectonic(server_mod, available=True)
    with patch("api.server.subprocess.run") as run_mock:
        resp = client.post("/api/notes/export/pdf", json={
            "course_id": "testcourse",
            "latex_source": r"\section{Hi} \write18{rm -rf /}",
        })
    assert resp.status_code == 422
    assert resp.json()["error"] == "latex_unsafe"
    run_mock.assert_not_called()


def test_happy_path_returns_pdf_bytes(pdf_client):
    client, server_mod = pdf_client
    _set_tectonic(server_mod, available=True)

    fake_pdf = b"%PDF-1.4 fake pdf body for test\n%%EOF\n"

    def fake_run(cmd, *args, **kwargs):
        # cmd is [tectonic_path, "-X", "compile", "--outdir", outdir, "--keep-logs", tex_path]
        outdir = Path(cmd[cmd.index("--outdir") + 1])
        # write the fake PDF where the endpoint will read it
        (outdir / "note.pdf").write_bytes(fake_pdf)
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout=b"compile ok", stderr=b"",
        )

    with patch("api.server.subprocess.run", side_effect=fake_run):
        resp = client.post("/api/notes/export/pdf", json={
            "course_id": "testcourse",
            "latex_source": r"\section{Hi} \textbf{ok}",
        })

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content == fake_pdf
    assert "attachment" in resp.headers["content-disposition"]


def test_compile_failure_returns_422_with_log_tail(pdf_client):
    client, server_mod = pdf_client
    _set_tectonic(server_mod, available=True)

    long_log = (b"LaTeX error: ") + b"x" * 6000 + b"\n! Undefined control sequence."

    def fake_run(cmd, *args, **kwargs):
        return subprocess.CompletedProcess(
            args=cmd, returncode=1,
            stdout=b"", stderr=long_log,
        )

    with patch("api.server.subprocess.run", side_effect=fake_run):
        resp = client.post("/api/notes/export/pdf", json={
            "course_id": "testcourse",
            "latex_source": r"\section{Hi} \nonexistentmacro",
        })

    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "latex_compile_failed"
    assert body["exit_code"] == 1
    # log tail must be bounded
    assert len(body["log"].encode("utf-8")) <= 4000 + 100  # +slop for utf-8 boundary
    # the tail should retain the meaningful final error fragment
    assert "Undefined control sequence" in body["log"]


def test_compile_timeout_returns_504(pdf_client):
    client, server_mod = pdf_client
    _set_tectonic(server_mod, available=True)

    def fake_run(cmd, *args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 60))

    with patch("api.server.subprocess.run", side_effect=fake_run):
        resp = client.post("/api/notes/export/pdf", json={
            "course_id": "testcourse",
            "latex_source": r"\section{Hi} \begin{theorem} forever \end{theorem}",
        })

    assert resp.status_code == 504
    assert resp.json()["error"] == "latex_compile_timeout"


def test_subprocess_succeeds_but_no_pdf_emitted_returns_502(pdf_client):
    """Defensive path: tectonic returns 0 but didn't actually write note.pdf
    (unlikely but possible — e.g. compile aborted after exit handler)."""
    client, server_mod = pdf_client
    _set_tectonic(server_mod, available=True)

    def fake_run(cmd, *args, **kwargs):
        # do NOT write note.pdf
        return subprocess.CompletedProcess(args=cmd, returncode=0,
                                            stdout=b"", stderr=b"")

    with patch("api.server.subprocess.run", side_effect=fake_run):
        resp = client.post("/api/notes/export/pdf", json={
            "course_id": "testcourse",
            "latex_source": r"\section{Hi}",
        })
    assert resp.status_code == 502
    assert resp.json()["error"] == "latex_compile_failed"


def test_blank_latex_source_rejected_by_pydantic(pdf_client):
    client, server_mod = pdf_client
    _set_tectonic(server_mod, available=True)
    resp = client.post("/api/notes/export/pdf", json={
        "course_id": "testcourse",
        "latex_source": "",
    })
    # pydantic min_length=1 → 422 via global validation handler
    assert resp.status_code == 422


def test_status_endpoint_exposes_tectonic_flag(pdf_client):
    client, server_mod = pdf_client
    _set_tectonic(server_mod, available=True)
    body = client.get("/api/status").json()
    assert body["tectonic_available"] is True
    _set_tectonic(server_mod, available=False)
    body = client.get("/api/status").json()
    assert body["tectonic_available"] is False
