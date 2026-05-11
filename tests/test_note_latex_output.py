"""Tests for the LaTeX-only Note output contract.

The Note pipeline must:
  1. ship raw LaTeX through /api/notes and /api/notes/stream — no markdown
     repair (`format_response`) on the way out, because that helper
     mangles LaTeX (it expects markdown's `##`, `**`, fenced code, etc.).
  2. reject `format="markdown"` from old clients with 422, not 500.
"""

from __future__ import annotations

import importlib
import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from nano_notebooklm.types import SkillResult


@pytest.fixture
def latex_client(monkeypatch, tmp_path, sample_chunks, fake_embed_fn):
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


def _read_events(response):
    return [json.loads(line) for line in response.iter_lines() if line]


def test_note_request_rejects_markdown_format(latex_client):
    """Backwards-compat hatch: old client sending the previous
    ``"markdown"`` literal must get a clear 422 with a stable shape, not
    a 500 or a cryptic ``extra fields not permitted``."""
    client, _ = latex_client
    resp = client.post("/api/notes",
                       json={"course_id": "testcourse", "format": "markdown"})
    assert resp.status_code == 422
    body = resp.json()
    # global validation handler returns {error, request_id, detail}
    assert body.get("error") == "validation_error"


@pytest.mark.parametrize("legacy_format", ["text", "html", "MARKDOWN", "tex", ""])
def test_note_request_rejects_all_legacy_formats(latex_client, legacy_format):
    """review-swarm fix-all v1 #12: pin that the old enum values
    ``text`` / ``html`` (plus any other non-"latex" literal) also 422.
    The Pydantic Literal["latex"] does this implicitly, but a future
    schema rewrite could silently re-accept without breaking any test."""
    client, _ = latex_client
    resp = client.post("/api/notes",
                       json={"course_id": "testcourse", "format": legacy_format})
    assert resp.status_code == 422, (legacy_format, resp.text)


def test_note_request_accepts_latex_or_default(latex_client, monkeypatch):
    """``format`` field can be omitted (default "latex") or explicitly "latex"."""
    client, server_mod = latex_client

    async def fake_run_skill(name, params):
        return SkillResult(success=True, data={
            "content": r"\section{Intro}" "\n" r"\textbf{important}",
            "format": "latex",
        })
    monkeypatch.setattr(server_mod.orchestrator, "run_skill", fake_run_skill)

    # default
    r1 = client.post("/api/notes", json={"course_id": "testcourse"})
    assert r1.status_code == 200, r1.text
    # explicit latex
    r2 = client.post("/api/notes",
                     json={"course_id": "testcourse", "format": "latex"})
    assert r2.status_code == 200, r2.text


def test_notes_endpoint_does_not_call_format_response(latex_client, monkeypatch):
    """The /api/notes path must NOT route LaTeX through ``format_response``
    (markdown repair) — that would mangle `\\section` and `$...$`."""
    client, server_mod = latex_client

    async def fake_run_skill(name, params):
        return SkillResult(success=True, data={
            "content": r"\section{Intro}" "\n" r"\textbf{key}",
            "format": "latex",
        })
    monkeypatch.setattr(server_mod.orchestrator, "run_skill", fake_run_skill)

    with patch("api.server.format_response") as mock_fmt:
        mock_fmt.side_effect = AssertionError("format_response must not run on notes")
        resp = client.post("/api/notes", json={"course_id": "testcourse"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == r"\section{Intro}" "\n" r"\textbf{key}"
    # Stronger assertion: no markdown remnants
    assert "##" not in body["content"]
    assert "**" not in body["content"]


def test_stream_notes_done_carries_raw_latex(latex_client, monkeypatch):
    """``/api/notes/stream`` ``done.content`` must be the raw concatenated
    LaTeX, not a format-repaired version."""
    client, server_mod = latex_client

    async def fake_complete_stream(prompt, task_type="", system="",
                                   temperature=0.7, max_tokens=4096):
        yield r"\section{Backprop}" "\n"
        yield r"\begin{theorem}[chain rule]" "\n"
        yield r"$\partial L/\partial w = \sum_i \partial L/\partial z_i \cdot \partial z_i/\partial w$" "\n"
        yield r"\end{theorem}" "\n"

    monkeypatch.setattr(server_mod.router, "complete_stream", fake_complete_stream)

    resp = client.post("/api/notes/stream", json={"course_id": "testcourse"})
    assert resp.status_code == 200
    events = _read_events(resp)
    done = events[-1]
    assert done["type"] == "done"
    # Raw, untouched
    assert r"\section{Backprop}" in done["content"]
    assert r"\begin{theorem}" in done["content"]
    assert "##" not in done["content"]
    # The full join is what the chunks were
    expected = (
        r"\section{Backprop}" "\n"
        r"\begin{theorem}[chain rule]" "\n"
        r"$\partial L/\partial w = \sum_i \partial L/\partial z_i \cdot \partial z_i/\partial w$" "\n"
        r"\end{theorem}" "\n"
    )
    assert done["content"] == expected


def test_stream_notes_does_not_invoke_format_response(latex_client, monkeypatch):
    """Stronger check: the streaming path explicitly skips format_response
    when kind=="notes"; verify by failing the test if the mock fires."""
    client, server_mod = latex_client

    async def fake_complete_stream(*a, **kw):
        yield r"\section{Hi}"
    monkeypatch.setattr(server_mod.router, "complete_stream", fake_complete_stream)

    with patch("api.server.format_response") as mock_fmt:
        mock_fmt.side_effect = AssertionError(
            "format_response must not run on the notes streaming path")
        resp = client.post("/api/notes/stream", json={"course_id": "testcourse"})
    events = _read_events(resp)
    assert events[-1]["type"] == "done"
