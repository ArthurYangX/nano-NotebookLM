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


# ── review-swarm fix-all v1 #13: PDF endpoint coverage gaps ──────────


def test_oversized_latex_source_rejected_by_sanitizer(pdf_client):
    """Sanitizer's 80 KB cap fires before subprocess; verify endpoint
    surfaces it as 422 latex_unsafe with no tectonic spawn."""
    client, server_mod = pdf_client
    _set_tectonic(server_mod, available=True)
    huge = "x" * (90 * 1024) + r"\section{Hi}"
    with patch("api.server.subprocess.run") as run_mock:
        resp = client.post("/api/notes/export/pdf", json={
            "course_id": "testcourse",
            "latex_source": huge,
        })
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "latex_unsafe"
    assert "exceeds" in body["reason"].lower() or "byte" in body["reason"].lower()
    run_mock.assert_not_called()


def test_cjk_source_passes_sanitizer_and_compiles(pdf_client):
    """Pure-Chinese source body must traverse the sanitizer (no forbidden
    commands triggered) and reach tectonic. Real font availability is not
    tested here — that requires a real tectonic run. We assert the
    request flows end-to-end via the mocked subprocess.run."""
    client, server_mod = pdf_client
    _set_tectonic(server_mod, available=True)

    fake_pdf = b"%PDF-1.4 cjk test\n%%EOF\n"

    def fake_run(cmd, *args, **kwargs):
        outdir = Path(cmd[cmd.index("--outdir") + 1])
        (outdir / "note.pdf").write_bytes(fake_pdf)
        # Verify the document the sanitizer let through actually contains
        # the Chinese characters (i.e. xeCJK preamble + body wrote out).
        tex_content = (outdir / "note.tex").read_text(encoding="utf-8")
        assert "第一章" in tex_content
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout=b"", stderr=b"",
        )

    body = (
        r"\section{第一章 导论}" "\n"
        r"\begin{definition}[卷积神经网络]"
        r"卷积神经网络（CNN）使用 $k \times k$ 卷积核提取空间特征。"
        r"\end{definition}"
    )
    with patch("api.server.subprocess.run", side_effect=fake_run):
        resp = client.post("/api/notes/export/pdf", json={
            "course_id": "testcourse",
            "latex_source": body,
        })

    assert resp.status_code == 200
    assert resp.content == fake_pdf


def test_get_method_rejected(pdf_client):
    """The endpoint is POST-only — GET should return 405 (FastAPI default
    for an unallowed method on a defined path)."""
    client, server_mod = pdf_client
    _set_tectonic(server_mod, available=True)
    resp = client.get("/api/notes/export/pdf")
    assert resp.status_code == 405


def test_error_responses_carry_request_id(pdf_client):
    """review-swarm fix-all v1 #14: every PDF-export JSONResponse error
    body must include `request_id` so operators can cross-correlate
    with the access log."""
    client, server_mod = pdf_client

    # 503 tectonic missing
    _set_tectonic(server_mod, available=False)
    body = client.post("/api/notes/export/pdf", json={
        "course_id": "testcourse",
        "latex_source": r"\section{Hi}",
    }).json()
    assert "request_id" in body

    # 422 sanitizer reject
    _set_tectonic(server_mod, available=True)
    body = client.post("/api/notes/export/pdf", json={
        "course_id": "testcourse",
        "latex_source": r"\input{/etc/passwd}",
    }).json()
    assert "request_id" in body

    # 504 timeout
    with patch("api.server.subprocess.run",
               side_effect=subprocess.TimeoutExpired(cmd=["t"], timeout=60)):
        body = client.post("/api/notes/export/pdf", json={
            "course_id": "testcourse",
            "latex_source": r"\section{Hi}",
        }).json()
    assert "request_id" in body
