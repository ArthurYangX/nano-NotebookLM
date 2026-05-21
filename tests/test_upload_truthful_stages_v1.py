"""Pin contracts established by upload pipeline fix-all v2 (truthful stages).

Four regression tests:
  M5 — `_set_stage(state, "kg_stage_b", 100)` must land AFTER `kg.save`
  M6 — `extract_from_chunks` must NEVER emit Stage B 100% internally;
       Stage A 100% must come after `topo_sort_topics` is referenced.
  M7 — `kb.ingest_course`'s `on_extract_progress` callback contract
       (start tick (0, N), end tick (N, N), monotonic non-decreasing).
  M8 — `_EXTRACT_SECS_PER_PAGE` baselines sane + helper returns a value
       within a reasonable band for a small text file.
"""

from __future__ import annotations

from pathlib import Path

import pytest


# ── M5: server-side ordering ────────────────────────────────────────────


def test_kg_stage_b_100_emit_after_kg_save():
    """fix-all v2 M5: Stage B 100% must fire AFTER `kg.save` completes,
    not before. Without this invariant, the frontend hits 100% while
    the server is still mid-save, leaving the modal stuck until a
    hard reload — the original bug this fix addresses.
    """
    src = Path("api/server.py").read_text(encoding="utf-8")
    upload_idx = src.index("async def _run_upload_pipeline")
    upload = src[upload_idx:]

    # Server uses `_asyncio` alias (see imports at top of server.py); the
    # canonical kg.save call is `await _asyncio.to_thread(kg.save, ...)`.
    save_pos = upload.find("kg.save")
    final_emit_pos = upload.find('_set_stage(state, "kg_stage_b", 100)')
    if final_emit_pos < 0:
        final_emit_pos = upload.find("_set_stage(state, KG_STAGE_B, 100)")

    assert save_pos > 0, "couldn't find kg.save call in _run_upload_pipeline"
    assert final_emit_pos > 0, (
        "couldn't find final kg_stage_b 100 emit in _run_upload_pipeline"
    )
    assert final_emit_pos > save_pos, (
        f"Stage B 100 emit at offset {final_emit_pos} must come AFTER "
        f"kg.save at offset {save_pos} (relative to _run_upload_pipeline start)"
    )


# ── M6: extractor must not emit Stage B 100 ─────────────────────────────


def test_extract_from_chunks_never_emits_stage_b_100():
    """fix-all v2 M6: Stage B 100% must NOT be emitted inside
    extract_from_chunks — server.py owns the final 100 after kg.save.
    """
    src = Path("nano_notebooklm/kg/extractor.py").read_text(encoding="utf-8")
    body_start = src.index("async def extract_from_chunks")
    body = src[body_start : body_start + 30000]

    # Allow `_emit(KG_STAGE_B, 80)` and `_emit(KG_STAGE_B, 95)` but NOT 100.
    assert "_emit(KG_STAGE_B, 100)" not in body, (
        "extract_from_chunks must not emit kg_stage_b 100 — server.py "
        "owns the final 100 after kg.save"
    )

    # Stage A 100 emit should appear after the topo_sort_topics use so
    # the percentage reflects work actually done (per-file accumulation +
    # topo sort), not just the moment `asyncio.gather` returns.
    a100_pos = body.find("_emit(KG_STAGE_A, 100)")
    topo_pos = body.find("topo_sort_topics")
    assert a100_pos > 0, "couldn't find _emit(KG_STAGE_A, 100)"
    assert topo_pos > 0, "couldn't find topo_sort_topics reference"
    assert a100_pos > topo_pos, (
        f"Stage A 100 emit at {a100_pos} must come AFTER topo_sort_topics "
        f"use at {topo_pos}"
    )


# ── M7: ingest_course on_extract_progress callback contract ────────────


def test_ingest_course_on_extract_progress_call_pattern(tmp_path):
    """fix-all v2 M7: pin the on_extract_progress callback contract.

    A future refactor that drops the `extracted_in_loop` flag would
    silently double-tick PDFs; one that drops the end-of-function
    force-100 would leave the bar at ~95% when some files yield no pages.
    """
    cd = tmp_path / "TestCourse"
    cd.mkdir()
    (cd / "a.txt").write_text("hello " * 100, encoding="utf-8")
    (cd / "b.md").write_text("# heading\n\npara " * 50, encoding="utf-8")

    ticks: list[tuple[int, int]] = []

    def cb(done: int, total: int) -> None:
        ticks.append((done, total))

    from nano_notebooklm.kb.store import KBStore

    store = KBStore(
        artifacts_dir=tmp_path / "artifacts",
        embed_fn=lambda texts: [[0.0] * 8 for _ in texts],
    )
    store.ingest_course(
        str(cd),
        course_id="TestCourse",
        engine="pymupdf",
        on_extract_progress=cb,
    )

    assert ticks, "callback must fire at least once"

    # Start tick: (0, N) — emitted before any file is processed.
    assert ticks[0][0] == 0, (
        f"first tick should have done=0; got {ticks[0]}"
    )
    assert ticks[0][1] >= 1, (
        f"first tick should have total>=1; got {ticks[0]}"
    )

    # End tick: (N, N) — the force-100 sweep at end of ingest_course.
    last_done, last_total = ticks[-1]
    assert last_done == last_total, (
        f"last tick should be (total, total); got {ticks[-1]} "
        f"(all ticks: {ticks})"
    )

    # Monotonic non-decreasing on done.
    prev_done = -1
    for done, _total in ticks:
        assert done >= prev_done, f"non-monotonic on done: {ticks}"
        prev_done = done


# ── M8: ETA baseline sanity ─────────────────────────────────────────────


def test_extract_secs_per_page_baselines_sane():
    """fix-all v2 M8: a typo on a baseline tune would silently 10x the
    ETA without any CI breakage. Pin the values within sane bands.
    """
    from api.server import _EXTRACT_SECS_PER_PAGE

    assert "pymupdf" in _EXTRACT_SECS_PER_PAGE
    assert "mineru" in _EXTRACT_SECS_PER_PAGE

    # pymupdf: vector text layer reads in ms — keep under 1 second/page.
    assert 0.01 <= _EXTRACT_SECS_PER_PAGE["pymupdf"] <= 1.0, (
        f"pymupdf baseline {_EXTRACT_SECS_PER_PAGE['pymupdf']} out of "
        f"sane range [0.01, 1.0]"
    )

    # mineru: CPU-OCR 3-10s/page on M4 — anything outside [1, 30]
    # signals a typo or unit confusion.
    assert 1.0 <= _EXTRACT_SECS_PER_PAGE["mineru"] <= 30.0, (
        f"mineru baseline {_EXTRACT_SECS_PER_PAGE['mineru']} out of "
        f"sane range [1.0, 30.0]"
    )


def test_estimate_upload_duration_seconds_returns_reasonable_value(tmp_path):
    """fix-all v2 M8: end-to-end sanity check that the helper produces a
    value within the documented sane band for a small text file.
    """
    from api.server import _estimate_upload_duration_seconds, _scan_file_pages

    f = tmp_path / "tiny.txt"
    f.write_text("hello", encoding="utf-8")

    total_pages, per_file = _scan_file_pages(tmp_path)
    eta = _estimate_upload_duration_seconds(
        tmp_path,
        "pymupdf",
        mineru_warm=True,
        total_pages=total_pages,
        per_file_pages=per_file,
    )

    # A single tiny text file: extraction + chunking + embedding + KG is
    # bounded — never less than the floor of 5 seconds, and not absurd.
    assert 5 <= eta <= 120, (
        f"tiny-file ETA {eta}s out of reasonable range [5, 120]"
    )
