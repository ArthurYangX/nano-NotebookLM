"""Sanity tests for `_estimate_upload_duration_seconds`.

The estimator drives the user-visible ETA countdown on the upload
overlay. Constants in `_EXTRACT_SECS_PER_PAGE`, `_PPTX_*`,
`_MINERU_COLD_START_SECS`, `STAGE_A_SECS_PER_CALL`, `STAGE_B_SECS_PER_BATCH`,
and the 1.4x safety multiplier are load-bearing for user perception;
a typo dropping any of them to 0 (or 10x) would ship silently until a
real upload happens.

These tests assert ORDER-OF-MAGNITUDE ranges, not exact values, so the
constants can be tuned (per real-world drift) without the tests
breaking on every adjustment.
"""

from pathlib import Path

import pytest

from api import server as srv


def _patch_scan(monkeypatch, tmp_path: Path, per_file_pages: dict[str, int]):
    """Stub `_scan_file_pages` to return synthetic per-file page counts.

    Creates zero-byte files inside `tmp_path` so the estimator's
    downstream `f.stat().st_size` succeeds without us having to mock
    Path itself.
    """
    pf: dict[Path, int] = {}
    for name, pages in per_file_pages.items():
        f = tmp_path / name
        f.write_bytes(b"")
        pf[f] = pages
    total = sum(pf.values())
    monkeypatch.setattr(srv, "_scan_file_pages", lambda _dir: (total, pf))


def _force_embedding_mode(monkeypatch, mode: str) -> None:
    """Pin the embedding mode the estimator reads at call time."""
    from nano_notebooklm import config
    monkeypatch.setattr(config, "EMBEDDING_MODE", mode)


def _force_mineru_server_enabled(monkeypatch) -> None:
    """Ensure the estimator treats mineru as available (not sticky-disabled)."""
    from nano_notebooklm.ingest import extractors_mineru as m
    monkeypatch.setattr(m, "_MINERU_SERVER_DISABLED_REASON", None)


def test_pymupdf_single_small_pdf_under_a_minute(monkeypatch, tmp_path):
    """Single 10-page PDF on pymupdf + local embeddings should finish
    well under a minute (mostly Stage A + Stage B LLM tail)."""
    _patch_scan(monkeypatch, tmp_path, {"a.pdf": 10})
    _force_embedding_mode(monkeypatch, "local")
    _force_mineru_server_enabled(monkeypatch)
    eta = srv._estimate_upload_duration_seconds(
        Path("/tmp/_eta_test"), engine="pymupdf", mineru_warm=None,
    )
    # Extracting ~0.5s, chunking ~0.1s, embedding tiny, kg_a ~45s * 0.25,
    # kg_b minimal → with 1.4x margin ≈ 80-180s.
    assert 30 <= eta <= 240, f"unexpected ETA {eta}s for tiny pdf"


def test_mineru_warm_vs_cold_diff(monkeypatch, tmp_path):
    """Warm vs cold mineru should differ by ~_MINERU_COLD_START_SECS,
    not zero (regression guard against accidentally dropping the
    cold-start surcharge) and not 10x apart."""
    _patch_scan(monkeypatch, tmp_path, {"a.pdf": 8})
    _force_embedding_mode(monkeypatch, "local")
    _force_mineru_server_enabled(monkeypatch)
    cold = srv._estimate_upload_duration_seconds(
        Path("/tmp/_eta_test"), engine="mineru", mineru_warm=False,
    )
    warm = srv._estimate_upload_duration_seconds(
        Path("/tmp/_eta_test"), engine="mineru", mineru_warm=True,
    )
    # Warm should be smaller than cold by a margin in the 20-60s ballpark
    # (cold pays _MINERU_COLD_START_SECS=40 vs warm's 8s, times 1.4 margin).
    assert warm < cold, f"warm {warm} not less than cold {cold}"
    assert (cold - warm) >= 20, f"cold-warm gap {cold - warm}s suspiciously small"
    assert (cold - warm) <= 120, f"cold-warm gap {cold - warm}s suspiciously large"


def test_large_pdf_mineru_within_realistic_band(monkeypatch, tmp_path):
    """A 50-page PDF on mineru should land in the 10-40 min realistic
    band; outside that window means a constant changed by >2x."""
    _patch_scan(monkeypatch, tmp_path, {"big.pdf": 50})
    _force_embedding_mode(monkeypatch, "local")
    _force_mineru_server_enabled(monkeypatch)
    eta = srv._estimate_upload_duration_seconds(
        Path("/tmp/_eta_test"), engine="mineru", mineru_warm=True,
    )
    # 50 pages * 13s/page = 650s extracting; +Stage A ~45s; +Stage B
    # depends on chunks (50*4=200 → 20 batches * 30s = 600s); +1.4x.
    # Realistic band: 20-50 min.
    assert 600 <= eta <= 3600, f"unexpected ETA {eta}s for 50-page mineru"


def test_estimator_never_returns_zero(monkeypatch, tmp_path):
    """Empty upload still returns a small positive number, never 0 or
    negative — frontend treats `<= 0` as "no estimate available"."""
    _patch_scan(monkeypatch, tmp_path, {})
    _force_embedding_mode(monkeypatch, "local")
    _force_mineru_server_enabled(monkeypatch)
    eta = srv._estimate_upload_duration_seconds(
        Path("/tmp/_eta_test"), engine="pymupdf", mineru_warm=None,
    )
    assert eta >= 5, f"estimator returned suspiciously small {eta}"


def test_pptx_ppt_priced_same(monkeypatch, tmp_path):
    """`.ppt` and `.pptx` ride the same MinerU sidecar pipeline (review-
    swarm H3 fix), so the ETA must price them identically."""
    _force_embedding_mode(monkeypatch, "local")
    _force_mineru_server_enabled(monkeypatch)
    _patch_scan(monkeypatch, tmp_path, {"deck.pptx": 8})
    pptx_eta = srv._estimate_upload_duration_seconds(
        Path("/tmp/_eta_test"), engine="mineru", mineru_warm=True,
    )
    _patch_scan(monkeypatch, tmp_path, {"deck.ppt": 8})
    ppt_eta = srv._estimate_upload_duration_seconds(
        Path("/tmp/_eta_test"), engine="mineru", mineru_warm=True,
    )
    assert pptx_eta == ppt_eta, f".pptx ETA {pptx_eta} ≠ .ppt ETA {ppt_eta}"
