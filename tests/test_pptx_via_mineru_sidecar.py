"""fix-all v3 (2026-05-22): coverage for the pptx-via-MinerU-sidecar
routing introduced in store.py + the new MinerU server-path helpers.

Three concerns get pinned here:

1. ``_extract_one_via_server`` key-match: when called with a sidecar
   path like ``foo.pptx.pdf``, the MinerU server returns results keyed
   by ``foo.pptx`` (it strips exactly one trailing ``.pdf``). The
   extractor's fallback (``filepath.with_suffix("").name``) must
   recover the right entry.

2. ``extract_pdfs_mineru_via_server`` ``on_file_done`` callback: emits
   monotonic ticks ``(done, total)`` as each PDF in the batch finishes,
   so the upload UI bar advances incrementally instead of jumping from
   0 to 100 at the very end.

3. ``content_list missing`` error path includes the upstream entry
   shape (``entry keys=[...]`` and an ``error=...`` preview field when
   present) so operators can diagnose why the server returned no
   blocks without poking the live service.

4. ``KBStore.ingest_course`` integration: when ``engine='mineru'`` AND
   ``previews_dir`` is passed AND a sidecar PDF exists for a ``.pptx``,
   the sidecar is included in the MinerU batch and resulting pages
   land on chunks stamped with the original ``.pptx`` source.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from nano_notebooklm.ingest import extractors_mineru as M


# Reuse the singleton-reset fixture pattern from test_extractors_mineru_server.py
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


def _fake_blocks(name: str) -> list[dict]:
    return [
        {"type": "header", "text": f"Title of {name}", "text_level": 1,
         "bbox": [0, 0, 100, 20], "page_idx": 0},
        {"type": "text", "text": f"Body for {name}",
         "bbox": [0, 30, 100, 50], "page_idx": 1},
    ]


class _FakeAsyncClient:
    """Drop-in replacement for httpx.AsyncClient (see test_extractors_mineru_server.py
    for the rationale — httpx 0.28 strict isinstance check on request bodies).

    Records the most recent call to ``last_call`` for assertion."""
    last_call: dict | None = None

    def __init__(self, handler, **kw):
        self._handler = handler

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def post(self, url, *, files=None, data=None, **kw):
        type(self).last_call = {"url": url, "files": files, "data": data}
        return self._handler({"url": url, "files": files, "data": data})


def _run_with_handler(handler, fn):
    import httpx

    def factory(**kw):
        return _FakeAsyncClient(handler=handler, **kw)

    async def go():
        with patch.object(httpx, "AsyncClient", factory):
            return await fn()

    return asyncio.run(go())


# ── 1. Sidecar key-match (foo.pptx.pdf → server keys by foo.pptx) ────


def test_extract_one_via_server_sidecar_key_match(tmp_path):
    """fix-all v3: when filepath is `lec.pptx.pdf` (a sidecar) the
    server's results dict is keyed by `lec.pptx` (one .pdf suffix
    stripped). The dedicated ``filepath.with_suffix("").name`` branch
    must resolve the entry."""
    import httpx

    sidecar = tmp_path / "lec.pptx.pdf"
    sidecar.write_bytes(b"%PDF\n")

    def handler(call):
        # MinerU strips exactly one trailing .pdf; result key has no .pdf.
        return httpx.Response(200, json={
            "results": {"lec.pptx": {"content_list": _fake_blocks("lec")}},
        })

    pages = _run_with_handler(
        handler,
        lambda: M._extract_one_via_server("http://test", sidecar, "ch"),
    )
    assert pages, "sidecar key-match must return pages, not fall through to error"
    assert "Title of lec" in pages[0].text


# ── 2. on_file_done callback fires monotonically with correct counts ──


def test_batch_via_server_on_file_done_ticks_monotonically(tmp_path, monkeypatch):
    """fix-all v3: on_file_done must fire once per completed file with
    (done, total) where total stays constant and done climbs monotonically
    from 1 to total, so the upload UI bar advances incrementally."""
    pdfs = []
    for i in range(3):
        p = tmp_path / f"f{i}.pdf"
        p.write_bytes(b"%PDF\n")
        pdfs.append(p)

    # Stub the server-start to return a valid url state.
    monkeypatch.setattr(
        M, "_get_or_start_mineru_server",
        lambda **kw: {"url": "http://stub", "proc": None, "port": 1, "device": "cpu"},
    )

    def handler(call):
        import httpx
        # Echo back blocks keyed by the filename in the call.
        name = call["files"]["files"][0]
        return httpx.Response(200, json={
            "results": {name: {"content_list": _fake_blocks(name)}},
        })

    ticks: list[tuple[int, int]] = []

    async def go():
        return await M.extract_pdfs_mineru_via_server(
            [str(p) for p in pdfs], lang="ch",
            on_file_done=lambda done, total: ticks.append((done, total)),
        )

    import httpx

    def factory(**kw):
        return _FakeAsyncClient(handler=handler, **kw)

    with patch.object(httpx, "AsyncClient", factory):
        results = asyncio.run(go())

    assert len(results) == 3, f"all 3 pdfs should succeed; got {results}"

    # Exactly 3 ticks, all with total=3, done climbing 1→2→3 monotonically.
    assert len(ticks) == 3, f"expected 3 ticks, got {len(ticks)}: {ticks}"
    assert all(total == 3 for _done, total in ticks), \
        f"total denominator must stay 3 across ticks: {ticks}"
    dones = [done for done, _total in ticks]
    assert dones == sorted(dones), f"done must be monotonically non-decreasing: {dones}"
    assert dones[-1] == 3, f"final done must equal total: {dones}"


def test_batch_via_server_on_file_done_callback_exception_suppressed(tmp_path, monkeypatch):
    """fix-all v3: if the on_file_done callback raises, the batch must
    NOT crash — the exception is logged and swallowed so the upload
    pipeline keeps progressing."""
    pdf = tmp_path / "f.pdf"
    pdf.write_bytes(b"%PDF\n")

    monkeypatch.setattr(
        M, "_get_or_start_mineru_server",
        lambda **kw: {"url": "http://stub", "proc": None, "port": 1, "device": "cpu"},
    )

    def handler(call):
        import httpx
        name = call["files"]["files"][0]
        return httpx.Response(200, json={
            "results": {name: {"content_list": _fake_blocks(name)}},
        })

    def bad_cb(done, total):
        raise RuntimeError("UI render glitch")

    async def go():
        return await M.extract_pdfs_mineru_via_server(
            [str(pdf)], lang="ch", on_file_done=bad_cb,
        )

    import httpx

    def factory(**kw):
        return _FakeAsyncClient(handler=handler, **kw)

    with patch.object(httpx, "AsyncClient", factory):
        results = asyncio.run(go())  # MUST NOT raise

    assert len(results) == 1, "extraction must still succeed when callback raises"


# ── 3. Enriched error message on content_list missing ────────────────


def test_extract_one_via_server_content_list_error_includes_entry_keys(tmp_path):
    """fix-all v3: when the server returns an entry with no content_list
    field, the error message must include the entry's key list AND
    surface a preview of any error/status/message/detail field, so
    operators can diagnose without re-poking the server."""
    import httpx

    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF\n")

    def handler(call):
        # MinerU returns an error-shaped entry instead of content_list.
        return httpx.Response(200, json={
            "results": {"x.pdf": {
                "error": "GPU out of memory while OCR-ing page 12",
                "status": "failed",
            }},
        })

    with pytest.raises(M.MinerUExtractionError) as exc_info:
        _run_with_handler(
            handler,
            lambda: M._extract_one_via_server("http://test", pdf, "ch"),
        )

    msg = str(exc_info.value)
    assert "entry keys=" in msg, f"error must include entry key list; got: {msg}"
    assert "error" in msg, f"error must surface entry['error'] preview; got: {msg}"
    assert "GPU out of memory" in msg, \
        f"error must include the upstream error body (truncated); got: {msg}"


def test_extract_one_via_server_content_list_error_truncates_preview(tmp_path):
    """fix-all v3: the entry preview is bounded to 120 chars so a
    runaway error string can't blow up the log line."""
    import httpx

    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF\n")
    long_err = "x" * 500

    def handler(call):
        return httpx.Response(200, json={
            "results": {"x.pdf": {"error": long_err}},
        })

    with pytest.raises(M.MinerUExtractionError) as exc_info:
        _run_with_handler(
            handler,
            lambda: M._extract_one_via_server("http://test", pdf, "ch"),
        )

    msg = str(exc_info.value)
    # Preview is sliced [:120], so the message can't contain all 500 x's.
    assert msg.count("x") < 200, \
        f"entry preview must be truncated to ~120 chars; got {msg.count('x')} x's"


# ── 4. KBStore.ingest_course routes .pptx through mineru via sidecar ──


def test_ingest_course_routes_pptx_through_mineru_sidecar(tmp_path, monkeypatch):
    """fix-all v3 integration: with engine='mineru' + previews_dir
    pointing at a directory containing the soffice-rendered sidecar
    PDF, the .pptx file rides MinerU through its sidecar. Chunks land
    with file_type=PPTX and the original .pptx as source_file."""
    from nano_notebooklm.kb.store import KBStore
    from nano_notebooklm.types import PageInfo

    cd = tmp_path / "Course"
    cd.mkdir()
    pptx = cd / "lec01.pptx"
    pptx.write_bytes(b"PK\x03\x04" + b"\0" * 100)  # bogus zip header; we mock extraction

    previews = tmp_path / "previews"
    previews.mkdir()
    # Sidecar lives at `<previews>/<leaf>.pdf` per pptx_pdf.sidecar_path.
    sidecar = previews / "lec01.pptx.pdf"
    sidecar.write_bytes(b"%PDF-1.4\nfake\n")

    # Stub the batch extractor to return pages keyed by the sidecar's
    # resolved absolute path (matches what store.py looks up).
    captured_batch_inputs: list[str] = []

    # Long-enough page text so chunk_pages doesn't drop the chunk below
    # MIN_CHUNK_TOKENS (50). The chunker's `x ` repetition pattern is a
    # known-good shape from the debug session — 200 tokens, comfortably
    # above the floor.
    long_text = "x " * 200

    def fake_batch(filepaths, *, lang="ch", timeout_seconds=3600, device="cpu", on_file_done=None):
        captured_batch_inputs.extend(filepaths)
        out = {}
        for fp in filepaths:
            out[str(Path(fp).resolve())] = [
                PageInfo(page=1, text=long_text, total_pages=2),
                PageInfo(page=2, text=long_text, total_pages=2),
            ]
        if on_file_done is not None:
            on_file_done(len(filepaths), len(filepaths))
        return out

    monkeypatch.setattr(
        "nano_notebooklm.ingest.extractors_mineru.extract_pdfs_mineru_batch",
        fake_batch,
    )

    store = KBStore(
        artifacts_dir=tmp_path / "artifacts",
        embed_fn=lambda texts: [[0.0] * 8 for _ in texts],
    )
    course = store.ingest_course(
        str(cd),
        course_id="Course",
        engine="mineru",
        previews_dir=previews,
    )

    # The sidecar PDF (not the .pptx itself) must appear in the mineru batch.
    assert captured_batch_inputs, (
        "extract_pdfs_mineru_batch was NOT called — patching missed the import path"
    )
    assert any("lec01.pptx.pdf" in p for p in captured_batch_inputs), (
        f"sidecar must be sent to mineru; batch_inputs={captured_batch_inputs}"
    )
    assert not any(p.endswith(".pptx") for p in captured_batch_inputs), (
        f"the raw .pptx must NOT be in the mineru batch (only its sidecar); "
        f"batch_inputs={captured_batch_inputs}"
    )

    # Chunks land with PPTX file_type + source_file pointing at the .pptx.
    import json
    chunks_path = tmp_path / "artifacts" / "courses" / "Course" / "chunks.json"
    assert chunks_path.exists(), "chunks.json must be written after ingest"
    chunks = json.loads(chunks_path.read_text(encoding="utf-8"))
    assert chunks, "ingest must produce at least one chunk"
    pptx_chunks = [c for c in chunks if c["source_file"] == "lec01.pptx"]
    assert pptx_chunks, (
        f"chunks must be stamped with the original .pptx source; "
        f"got sources={set(c['source_file'] for c in chunks)}"
    )
    assert all(c["file_type"] == "pptx" for c in pptx_chunks), (
        f"file_type must be PPTX (not PDF); "
        f"got {set(c['file_type'] for c in pptx_chunks)}"
    )


def test_ingest_course_pptx_falls_back_when_sidecar_missing(tmp_path, monkeypatch):
    """fix-all v3 integration: when previews_dir is provided BUT no
    sidecar exists for a given .pptx, that file must fall back to
    python-pptx extraction silently (no exception, mineru just doesn't
    see it in its batch)."""
    from nano_notebooklm.kb.store import KBStore
    from nano_notebooklm.types import PageInfo

    cd = tmp_path / "Course"
    cd.mkdir()
    pptx = cd / "no_sidecar.pptx"
    pptx.write_bytes(b"PK\x03\x04" + b"\0" * 100)

    previews = tmp_path / "previews"
    previews.mkdir()
    # Note: no sidecar PDF created → store.py must skip mineru routing.

    captured_batch_inputs: list[str] = []

    def fake_batch(filepaths, *, lang="ch", timeout_seconds=3600, device="cpu", on_file_done=None):
        captured_batch_inputs.extend(filepaths)
        return {}  # No mineru work to do

    monkeypatch.setattr(
        "nano_notebooklm.ingest.extractors_mineru.extract_pdfs_mineru_batch",
        fake_batch,
    )

    # Stub the per-file pptx fallback so we don't need python-pptx wired up.
    fake_pages = [PageInfo(page=1, text="fallback slide", total_pages=1)]
    from nano_notebooklm.types import FileType

    def fake_extract_file(path, *, engine, lang):
        return fake_pages, FileType.PPTX

    monkeypatch.setattr("nano_notebooklm.kb.store.extract_file", fake_extract_file)

    store = KBStore(
        artifacts_dir=tmp_path / "artifacts",
        embed_fn=lambda texts: [[0.0] * 8 for _ in texts],
    )
    store.ingest_course(
        str(cd),
        course_id="Course",
        engine="mineru",
        previews_dir=previews,
    )

    # The .pptx without a sidecar must NOT be sent through mineru.
    assert not captured_batch_inputs, (
        f"with no sidecar present, mineru batch must be empty; "
        f"got {captured_batch_inputs}"
    )


# ── 5. fix-all v4 M5: no fuzzy stem collision ────────────────────────


def test_extract_one_via_server_unrelated_key_does_not_match(tmp_path):
    """fix-all v4 M5: with the fuzzy `Path(k).stem == filepath.stem`
    fallback removed, a response keyed on an unrelated filename must NOT
    silently match. Before the fix, an upload of `foo.pdf` paired with a
    server response keyed `bar.pdf` could mis-route bar's content onto
    foo's chunks because both `Path("foo.pdf").stem` and
    `Path("foo.pdf").stem` are themselves stems — meaning ANY single
    server-side result would be matched if it was the only entry. Pin
    the strict behaviour: mismatch → MinerUExtractionError."""
    import httpx

    pdf = tmp_path / "foo.pdf"
    pdf.write_bytes(b"%PDF\n")

    def handler(_call):
        return httpx.Response(200, json={
            "results": {"bar.pdf": {"content_list": _fake_blocks("bar")}},
        })

    # Pin the full diagnostic shape: operators rely on the `keys=` list
    # in the error message to debug server / client name skew. A future
    # refactor that drops the key-list dump should fail this test.
    with pytest.raises(M.MinerUExtractionError, match=r"no entry in results \(keys="):
        _run_with_handler(
            handler,
            lambda: M._extract_one_via_server("http://test", pdf, "ch"),
        )


# ── 6. fix-all v4 H2: extracting bar never overshoots total_files ────


def test_ingest_course_extracting_never_overshoots_on_sidecar_miss(tmp_path, monkeypatch):
    """fix-all v4 H2: when the mineru batch fires (truthy result dict)
    but the lookup for a specific sidecar misses, the pptx falls back
    through python-pptx via extract_file. The per-file tick MUST NOT
    re-count that file (its slot was already paid by the post-batch
    tick), or the extracting bar would briefly show e.g. 6/5 before
    the end-of-loop clamp."""
    from nano_notebooklm.kb.store import KBStore
    from nano_notebooklm.types import PageInfo, FileType

    cd = tmp_path / "Course"
    cd.mkdir()
    pptx = cd / "deck.pptx"
    pptx.write_bytes(b"PK\x03\x04" + b"\0" * 100)

    previews = tmp_path / "previews"
    previews.mkdir()
    sidecar = previews / "deck.pptx.pdf"
    sidecar.write_bytes(b"%PDF\n")

    # Batch returns a non-empty dict (so post-batch tick fires) but
    # under a key the per-file lookup will NOT find — forcing the
    # python-pptx fallback to run.
    def fake_batch(filepaths, *, lang="ch", timeout_seconds=3600,
                   device="cpu", on_file_done=None):
        if on_file_done is not None:
            on_file_done(len(filepaths), len(filepaths))
        return {"unrelated-key": []}

    monkeypatch.setattr(
        "nano_notebooklm.ingest.extractors_mineru.extract_pdfs_mineru_batch",
        fake_batch,
    )
    # Stub python-pptx extract path so we don't need a real deck on disk.
    monkeypatch.setattr(
        "nano_notebooklm.kb.store.extract_file",
        lambda path, *, engine, lang: (
            [PageInfo(page=1, text="fallback", total_pages=1)],
            FileType.PPTX,
        ),
    )

    ticks: list[tuple[int, int]] = []

    store = KBStore(
        artifacts_dir=tmp_path / "artifacts",
        embed_fn=lambda texts: [[0.0] * 8 for _ in texts],
    )
    store.ingest_course(
        str(cd),
        course_id="Course",
        engine="mineru",
        on_extract_progress=lambda d, t: ticks.append((d, t)),
        previews_dir=previews,
    )

    assert ticks, "callback must fire at least once"
    for done, total in ticks:
        assert done <= total, (
            f"H2 overshoot {done}/{total} in tick sequence {ticks}"
        )
    assert ticks[-1] == (ticks[-1][1], ticks[-1][1]), (
        f"final tick must reach (total, total), got {ticks[-1]}"
    )
