"""KG Stage B sampling ratio policy (2026-05-16).

Previously hardcoded `max_chunks=30`. Now `_kg_stage_b_sample_size(n)`
returns `clamp(int(n * RATIO), MIN, MAX)` with env-tunable knobs:

    KG_STAGE_B_SAMPLE_RATIO=0.3   (default 30% coverage)
    KG_STAGE_B_SAMPLE_MIN=30      (floor — tiny courses still get LLM extraction)
    KG_STAGE_B_SAMPLE_MAX=500     (ceiling — protects against runaway cost on huge corpora)
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def server_mod(monkeypatch, tmp_path):
    """Reload server with controlled env so the module-level constants
    bind to known values."""
    art = tmp_path / "artifacts"
    (art / "courses").mkdir(parents=True)
    (art / "uploads").mkdir(parents=True)
    monkeypatch.setattr("nano_notebooklm.config.ARTIFACTS_DIR", art)
    import api.server as sm
    importlib.reload(sm)
    return sm


def test_sample_size_scales_with_corpus(server_mod):
    """374-chunk corpus (NLP scenario) should sample ~30% = 112 chunks
    instead of the old hard 30."""
    assert server_mod._kg_stage_b_sample_size(374) == 112


def test_sample_size_floors_at_min_for_tiny_corpora(server_mod):
    """A 10-chunk single-doc course shouldn't get 3 LLM calls (would
    miss most concepts). Floor at MIN=30; the extractor itself uses all
    chunks when fewer than max_chunks exist, so this just disables the
    ratio's downside on tiny corpora."""
    assert server_mod._kg_stage_b_sample_size(10) == 30
    assert server_mod._kg_stage_b_sample_size(50) == 30  # 15 < 30 floor


def test_sample_size_ceiling_caps_huge_corpora(server_mod):
    """A 5000-chunk course at 30% would otherwise be 1500 calls × ~30s
    serial through codex proxy = hours. Cap at 500."""
    assert server_mod._kg_stage_b_sample_size(5000) == 500
    assert server_mod._kg_stage_b_sample_size(2000) == 500
    # Exactly at ceiling — 500/0.3 = 1666 chunks
    assert server_mod._kg_stage_b_sample_size(1666) == 499  # int(0.3 * 1666)
    assert server_mod._kg_stage_b_sample_size(1667) == 500


def test_sample_size_zero_or_negative_returns_min(server_mod):
    assert server_mod._kg_stage_b_sample_size(0) == 30
    assert server_mod._kg_stage_b_sample_size(-5) == 30


def test_sample_size_env_overrides_take_effect(monkeypatch, tmp_path):
    """Operators can tune the ratio / bounds via env."""
    monkeypatch.setenv("KG_STAGE_B_SAMPLE_RATIO", "0.5")
    monkeypatch.setenv("KG_STAGE_B_SAMPLE_MIN", "20")
    monkeypatch.setenv("KG_STAGE_B_SAMPLE_MAX", "200")
    art = tmp_path / "artifacts"
    (art / "courses").mkdir(parents=True)
    (art / "uploads").mkdir(parents=True)
    monkeypatch.setattr("nano_notebooklm.config.ARTIFACTS_DIR", art)
    import api.server as sm
    importlib.reload(sm)
    assert sm.KG_STAGE_B_SAMPLE_RATIO == 0.5
    assert sm.KG_STAGE_B_SAMPLE_MIN == 20
    assert sm.KG_STAGE_B_SAMPLE_MAX == 200
    # 374 chunks * 0.5 = 187 → within [20, 200] → 187
    assert sm._kg_stage_b_sample_size(374) == 187


def test_no_more_hardcoded_max_chunks_30():
    """Regression: ensure both upload-pipeline + explain-node call sites
    moved to `_kg_stage_b_sample_size(len(chunks))`. A future refactor
    that re-hardcodes 30 silently regresses KG coverage on large courses."""
    from pathlib import Path
    src = Path("api/server.py").read_text(encoding="utf-8")
    # Source mentions ``max_chunks=30`` only in the comment explaining
    # the historical default; no longer in actual call sites.
    call_site_matches = [
        line for line in src.splitlines()
        if "max_chunks=30" in line and not line.lstrip().startswith("#")
    ]
    assert not call_site_matches, (
        f"Found hardcoded max_chunks=30 call sites that should use "
        f"_kg_stage_b_sample_size: {call_site_matches}"
    )
    # Both call sites should use the new helper.
    assert src.count("_kg_stage_b_sample_size(len(chunks))") >= 2
