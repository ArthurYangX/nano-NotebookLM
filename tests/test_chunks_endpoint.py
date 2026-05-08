"""Round 2.2 #R5 — `/api/chunks/{chunk_id}` endpoint.

The Reader frontend used to render a hardcoded `READER_DOC` regardless of
which citation was clicked: the only signal the user got was the top-of-page
"Highlighted chunk <id>" tag — never the actual chunk text. This endpoint
backs a real fetch so the Reader can replace its body with the chunk + 1
neighbor each side and a banner like "《file》 · Page N".

Tests:
  - mini: seed a multi-chunk doc, ask for a middle chunk → prev/next/text
    all match expected order
  - corner: unknown chunk_id → 404, malformed/oversized chunk_id → 400
"""

from __future__ import annotations

import importlib
import json

import pytest
from fastapi.testclient import TestClient

from nano_notebooklm.types import Chunk, FileType


@pytest.fixture
def chunks_client(monkeypatch, tmp_path, fake_embed_fn):
    """Seed a single course with one document split across 5 chunks (pages 1-5)
    and one extra single-chunk document (so we exercise the doc_id filter)."""
    art = tmp_path / "artifacts"
    cdir = art / "courses" / "course_x"
    cdir.mkdir(parents=True)

    chapter = [
        Chunk(chunk_id=f"ch{i:02d}", doc_id="docA", course_id="course_x",
              text=f"page-{i+1} body about topic-{i+1}",
              file_type=FileType.PDF, source_file="textbook.pdf",
              location=f"Page {i+1}/5", page=i+1)
        for i in range(5)
    ]
    appendix = Chunk(chunk_id="ax01", doc_id="docB", course_id="course_x",
                    text="appendix content unrelated to chapter",
                    file_type=FileType.PDF, source_file="appendix.pdf",
                    location="Page 1/1", page=1)

    (cdir / "chunks.json").write_text(
        json.dumps([c.model_dump() for c in chapter + [appendix]], default=str)
    )
    (cdir / "course_meta.json").write_text(
        json.dumps({"course_id": "course_x", "name": "course_x",
                    "documents": ["docA", "docB"]})
    )

    monkeypatch.setenv("ARTIFACTS_DIR", str(art))
    from nano_notebooklm import config
    monkeypatch.setattr(config, "ARTIFACTS_DIR", art)
    from nano_notebooklm.kb import store as kb_store
    monkeypatch.setattr(kb_store, "_get_default_embed_fn", lambda: fake_embed_fn)

    import api.server as server_mod
    importlib.reload(server_mod)
    server_mod.kb.build_index("course_x")

    return TestClient(server_mod.app)


def test_chunks_endpoint_middle_returns_prev_and_next(chunks_client):
    """#R5 mini: ask for the middle chunk → prev/next match expected ordering;
    target chunk's text and source metadata travel back unchanged."""
    r = chunks_client.get("/api/chunks/ch02")
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["chunk"]["chunk_id"] == "ch02"
    assert body["chunk"]["text"] == "page-3 body about topic-3"
    assert body["source_file"] == "textbook.pdf"
    assert body["page"] == 3
    assert body["course_id"] == "course_x"
    assert body["doc_id"] == "docA"

    # Same-doc neighbors only — the unrelated appendix chunk must not appear
    assert body["prev"]["chunk_id"] == "ch01"
    assert body["next"]["chunk_id"] == "ch03"
    assert "appendix" not in body["prev"]["text"]
    assert "appendix" not in body["next"]["text"]


def test_chunks_endpoint_first_chunk_has_no_prev(chunks_client):
    """#R5 corner: edge of doc — first chunk has prev=None, only next set.
    `response_model_exclude_none=True` drops the field entirely; frontend uses
    `data.prev && ...` so absence == null behaviour."""
    r = chunks_client.get("/api/chunks/ch00")
    assert r.status_code == 200
    body = r.json()
    assert body["chunk"]["chunk_id"] == "ch00"
    assert body.get("prev") is None
    assert body["next"]["chunk_id"] == "ch01"


def test_chunks_endpoint_last_chunk_has_no_next(chunks_client):
    """#R5 corner: edge of doc — last chunk has next=None (omitted from body)."""
    r = chunks_client.get("/api/chunks/ch04")
    assert r.status_code == 200
    body = r.json()
    assert body["prev"]["chunk_id"] == "ch03"
    assert body.get("next") is None


def test_chunks_endpoint_single_chunk_doc(chunks_client):
    """#R5 corner: single-chunk doc — both prev and next are None (omitted)."""
    r = chunks_client.get("/api/chunks/ax01")
    assert r.status_code == 200
    body = r.json()
    assert body["chunk"]["chunk_id"] == "ax01"
    assert body.get("prev") is None
    assert body.get("next") is None
    assert body["doc_id"] == "docB"


def test_chunks_endpoint_unknown_id_returns_404(chunks_client):
    """#R5 corner: missing chunk → 404 with structured error envelope, doesn't
    crash the server."""
    r = chunks_client.get("/api/chunks/does-not-exist")
    assert r.status_code == 404
    body = r.json()
    assert body["error"] == "chunk not found: does-not-exist"
    assert "request_id" in body


def test_chunks_endpoint_oversized_id_returns_400(chunks_client):
    """#R5 corner: malformed (oversized) chunk_id rejected at the boundary so
    we don't iterate every course for an obviously-bogus id."""
    r = chunks_client.get("/api/chunks/" + ("x" * 300))
    assert r.status_code == 400
    body = r.json()
    assert body["error"] == "invalid chunk_id"


# ── Frontend wiring contract: reader.jsx must call API.getChunk on highlight ──


def test_reader_jsx_calls_get_chunk_when_highlighted():
    """#R5 frontend contract: reader.jsx must (a) fetch via API.getChunk and
    (b) render the real chunk text when a chunk_id citation is active —
    rather than always rendering hardcoded `READER_DOC`. Round 1 #2 only
    asserted the citation resolver shape; this is the missing pin.

    fix-all v3 #2 (review-swarm contracts #5): tightened from substring `in`
    matches to regex patterns that require a function CALL (`API.getChunk(`)
    and a function/component DECLARATION (`function ChunkBlock`) so trivial
    deletion (e.g. leaving `// API.getChunk` as a comment) is caught."""
    from pathlib import Path
    import re as _re
    src = Path(__file__).resolve().parent.parent / "frontend" / "reader.jsx"
    text = src.read_text(encoding="utf-8")

    # API call must be wired up — require parens (a real call site, not a comment)
    assert _re.search(r"API\.getChunk\s*\(", text), \
        "reader.jsx must call API.getChunk(...) at a real call site"
    # ChunkBlock must be a real component, not a stale token
    assert _re.search(r"function\s+ChunkBlock\b", text), \
        "reader.jsx must declare `function ChunkBlock` (the real-chunk renderer)"
    # The chunk.text path is the actual body render
    assert "chunk.text" in text, \
        "reader.jsx must render fetched chunk.text, not only READER_DOC"
    # Banner must show source_file in a JSX template (not just a stray identifier)
    assert _re.search(r"source_file\s*[}\]\s.\)]", text), \
        "reader.jsx must reference chunkData.source_file in the banner"
    # Page banner — accept `page ?? "—"` or `Page ${...}` template literal
    assert _re.search(r"\.page\b|Page\s+", text), \
        "reader.jsx must surface a page label/value"


def test_api_js_exposes_get_chunk():
    """#R5 frontend contract: api.js must expose API.getChunk so reader.jsx
    can fetch — without this binding the wiring above is dead.
    fix-all v3 #2: regex-pin so commented-out remnants don't pass."""
    from pathlib import Path
    import re as _re
    src = Path(__file__).resolve().parent.parent / "frontend" / "api.js"
    text = src.read_text(encoding="utf-8")
    # Require `getChunk(` as a method declaration AND `/chunks/` in a real path
    assert _re.search(r"\bgetChunk\s*\(", text), \
        "api.js must declare getChunk(...) as a real method"
    assert _re.search(r"/chunks/\$\{|/chunks/`?\$\{|/chunks/\$\{encodeURIComponent", text) \
        or "/chunks/" in text, \
        "api.js must hit /chunks/<id> in a template literal"
