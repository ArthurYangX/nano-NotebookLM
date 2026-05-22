"""Tests for the MinerU PDF extractor.

The pure parser (`_blocks_to_pages` / `_render_block`) is exercised
with synthetic content_list.json shaped data — no MinerU install or
GPU/CPU inference required, so this runs in CI fast.

The end-to-end `extract_pdf_mineru` is skipped unless the demo PDF
`experiments/mineru_validation/samples/ch3_hmm.pdf` exists and the
`mineru` CLI is on PATH. Mark with `slow` so `pytest -m "not slow"`
skips it.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from nano_notebooklm.ingest.extractors_mineru import (
    MinerUExtractionError,
    MinerUNotFoundError,
    _blocks_to_pages,
    _render_block,
    extract_pdf_mineru,
)


# ── Pure unit tests on the block-renderer ──────────────────────────


def test_render_text_block():
    assert _render_block({"type": "text", "text": "hello"}) == "hello"


def test_render_header_block_uses_markdown_heading():
    out = _render_block({"type": "header", "text": "Section", "text_level": 2})
    assert out == "## Section"


def test_render_text_with_level_promotes_to_heading():
    out = _render_block({"type": "text", "text": "Title", "text_level": 1})
    assert out == "# Title"


def test_render_equation_wraps_in_dollar_block():
    # MinerU sometimes ships the wrapper already, sometimes not.
    bare = _render_block({"type": "equation", "text": "P(x|y) = z"})
    assert bare.startswith("$$") and bare.endswith("$$") and "P(x|y)" in bare
    wrapped = _render_block({"type": "equation", "text": "$$\nP = 1\n$$"})
    # Don't double-wrap.
    assert wrapped == "$$\nP = 1\n$$"


def test_render_table_passes_html_through():
    body = "<table><tr><td>a</td></tr></table>"
    out = _render_block({"type": "table", "table_body": body})
    assert out == body


def test_render_image_with_caption():
    out = _render_block({
        "type": "image",
        "img_path": "images/abc.jpg",
        "image_caption": ["Figure 1: HMM"],
    })
    assert out == "![Figure 1: HMM](images/abc.jpg)"


def test_render_image_no_caption_emits_empty_alt():
    out = _render_block({"type": "image", "img_path": "images/abc.jpg"})
    assert out == "![](images/abc.jpg)"


def test_render_chart_same_path_as_image():
    out = _render_block({
        "type": "chart",
        "img_path": "images/x.jpg",
        "chart_caption": ["Bar chart"],
    })
    assert out == "![Bar chart](images/x.jpg)"


def test_render_block_unknown_type_keeps_text():
    out = _render_block({"type": "weird", "text": "fallback"})
    assert out == "fallback"


def test_render_block_unknown_type_no_text_returns_empty():
    out = _render_block({"type": "weird"})
    assert out == ""


# ── Page assembly tests ────────────────────────────────────────────


def _block(type_, *, text="", page=0, y=0, x=0, **extra):
    """Compact constructor for synthetic blocks."""
    return {
        "type": type_,
        "text": text,
        "bbox": [x, y, x + 100, y + 50],
        "page_idx": page,
        **extra,
    }


def test_blocks_to_pages_groups_by_page_idx():
    blocks = [
        _block("text", text="page 1 first", page=0, y=10),
        _block("text", text="page 1 second", page=0, y=100),
        _block("text", text="page 2 only", page=1, y=10),
    ]
    pages = _blocks_to_pages(blocks)
    assert len(pages) == 2
    assert pages[0].page == 1
    assert pages[1].page == 2
    assert "page 1 first" in pages[0].text
    assert "page 1 second" in pages[0].text
    assert "page 2 only" in pages[1].text


def test_blocks_to_pages_sorts_by_y_then_x():
    blocks = [
        _block("text", text="bottom", page=0, y=500),
        _block("text", text="top-right", page=0, y=10, x=400),
        _block("text", text="top-left", page=0, y=10, x=10),
    ]
    pages = _blocks_to_pages(blocks)
    text = pages[0].text
    assert text.index("top-left") < text.index("top-right") < text.index("bottom")


def test_blocks_to_pages_drops_empty_pages():
    blocks = [
        _block("text", text="", page=0),
        _block("text", text="real content", page=1),
    ]
    pages = _blocks_to_pages(blocks)
    assert len(pages) == 1
    assert pages[0].page == 2  # 1-based


def test_blocks_to_pages_skips_blocks_missing_page_idx():
    # Defensive: a malformed block without page_idx should not crash.
    blocks = [
        _block("text", text="ok", page=0),
        {"type": "text", "text": "no page idx", "bbox": [0, 0, 0, 0]},
    ]
    pages = _blocks_to_pages(blocks)
    assert len(pages) == 1


def test_blocks_to_pages_assembles_mixed_block_types():
    blocks = [
        _block("header", text="HMM", page=0, y=10, text_level=2),
        _block("text", text="马尔科夫模型", page=0, y=80),
        _block(
            "equation",
            text="$$P(q_t = s_j) = a_{ij}$$",
            page=0,
            y=150,
        ),
        _block(
            "table",
            page=0,
            y=250,
            table_body="<table><tr><td>0.5</td></tr></table>",
        ),
        _block(
            "image",
            page=0,
            y=400,
            img_path="images/x.jpg",
            image_caption=["State transitions"],
        ),
    ]
    pages = _blocks_to_pages(blocks)
    assert len(pages) == 1
    text = pages[0].text
    assert "## HMM" in text
    assert "马尔科夫模型" in text
    assert "$$P(q_t = s_j) = a_{ij}$$" in text
    assert "<table>" in text
    assert "![State transitions](images/x.jpg)" in text


def test_blocks_to_pages_sets_total_pages_via_extract_path():
    # _blocks_to_pages doesn't set total_pages — extract_pdf_mineru does.
    # This pins the contract: helper leaves total_pages None, wrapper fills it.
    blocks = [
        _block("text", text="p1", page=0),
        _block("text", text="p2", page=1),
    ]
    pages = _blocks_to_pages(blocks)
    assert all(p.total_pages is None for p in pages)


# ── End-to-end smoke (skipped unless mineru + sample available) ─────


_DEMO_PDF = (
    Path(__file__).parent.parent
    / "experiments"
    / "mineru_validation"
    / "samples"
    / "ch3_hmm.pdf"
)


@pytest.mark.slow
@pytest.mark.skipif(
    shutil.which("mineru") is None and not Path(".venv/bin/mineru").exists(),
    reason="mineru CLI not installed",
)
@pytest.mark.skipif(not _DEMO_PDF.exists(), reason="demo PDF missing")
def test_extract_pdf_mineru_on_hmm_sample(tmp_path):
    pages = extract_pdf_mineru(
        _DEMO_PDF,
        lang="ch",
        output_dir=tmp_path,
        start_page=3,
        end_page=4,  # 2 pages, the high-formula range
        timeout_seconds=900,
    )
    assert len(pages) >= 1
    # At least one page should carry a $$ LaTeX block (HMM formulae live here).
    joined = "\n".join(p.text for p in pages)
    assert "$$" in joined or "\\mid" in joined, (
        "Expected at least one LaTeX equation in HMM extracted pages, got:\n"
        + joined[:500]
    )


def test_extract_pdf_mineru_raises_when_cli_missing(monkeypatch, tmp_path):
    fake_pdf = tmp_path / "x.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4\n%fake\n")
    # Patch BOTH resolvers + disable the singleton, otherwise the H1
    # server-first path swallows the missing-CLI signal by trying to
    # launch mineru-api (which would also be missing in CI but only by
    # coincidence). Without this, the test passes for the wrong reason.
    monkeypatch.setenv("MINERU_SERVER_DISABLED", "1")
    monkeypatch.setattr(
        "nano_notebooklm.ingest.extractors_mineru._resolve_mineru_cli",
        lambda: None,
    )
    monkeypatch.setattr(
        "nano_notebooklm.ingest.extractors_mineru._resolve_mineru_api_cli",
        lambda: None,
    )
    # Pin device so this test never reaches `import torch` — torch state
    # in a cross-suite run is order-dependent and not what we're testing.
    monkeypatch.setattr(
        "nano_notebooklm.ingest.extractors_mineru.mineru_auto_device",
        lambda: "cpu",
    )
    with pytest.raises(MinerUNotFoundError):
        extract_pdf_mineru(fake_pdf)


def test_extract_pdf_mineru_raises_when_pdf_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        extract_pdf_mineru(tmp_path / "does_not_exist.pdf")


# ── mineru_auto_device() — env override + cuda / cpu fallback ────────
#
# These tests patch `import torch` to fail so they are independent of
# whatever torch state earlier tests in the suite have left behind
# (some envs segfault on a second torch import). The function under
# test guarantees an `except Exception` around the torch path, so a
# raised ImportError must lead to the `cpu` branch.


def _disable_torch_import(monkeypatch):
    """Force `mineru_auto_device()` down the no-torch fallback so the
    test is independent of whatever torch state earlier tests left
    behind (a second torch import can segfault). The session-wide
    autouse fixture pins `MINERU_DEVICE_MODE=cpu`; tests in this
    module delenv it to exercise the env-override + auto branches."""
    import builtins

    from nano_notebooklm.ingest import extractors_mineru as M

    monkeypatch.delenv("MINERU_DEVICE_MODE", raising=False)
    M._reset_auto_device_cache()  # force re-probe under the patched import
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "torch" or name.startswith("torch."):
            raise ImportError("simulated: torch not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def test_mineru_auto_device_known_override(monkeypatch):
    from nano_notebooklm.ingest.extractors_mineru import mineru_auto_device

    # Env override path doesn't touch torch — no need to disable import.
    for value in ("cpu", "cuda", "mps"):
        monkeypatch.setenv("MINERU_DEVICE_MODE", value)
        assert mineru_auto_device() == value


def test_mineru_auto_device_unknown_override_falls_through(monkeypatch, caplog):
    """A typo like `guda` must NOT pass through — it would cause a
    180s health-check hang on the first upload before the singleton
    sticky-disables itself."""
    import logging

    from nano_notebooklm.ingest.extractors_mineru import mineru_auto_device

    _disable_torch_import(monkeypatch)
    monkeypatch.setenv("MINERU_DEVICE_MODE", "guda")
    with caplog.at_level(logging.WARNING, logger="nano_notebooklm.ingest.extractors_mineru"):
        result = mineru_auto_device()
    assert result == "cpu", f"with torch unimportable the fallback must be cpu, got {result!r}"
    assert any("guda" in rec.getMessage() for rec in caplog.records), (
        "expected a warning naming the rejected value, got: "
        + repr([r.getMessage() for r in caplog.records])
    )


def test_mineru_auto_device_no_torch_falls_to_cpu(monkeypatch):
    """If torch is unimportable (default install without [mineru]
    extras), auto-detect must return cpu, not raise."""
    from nano_notebooklm.ingest import extractors_mineru as M

    monkeypatch.delenv("MINERU_DEVICE_MODE", raising=False)
    _disable_torch_import(monkeypatch)
    assert M.mineru_auto_device() == "cpu"


def test_mineru_auto_device_never_auto_selects_mps(monkeypatch):
    """Even on Apple Silicon where `torch.backends.mps.is_available()`
    might be True, the auto path must NOT return `mps` — the pipeline
    backend hangs at DocAnalysis init under MPS. The auto path only
    consults `torch.cuda.is_available`, never MPS."""
    from nano_notebooklm.ingest.extractors_mineru import mineru_auto_device

    monkeypatch.delenv("MINERU_DEVICE_MODE", raising=False)
    _disable_torch_import(monkeypatch)
    assert mineru_auto_device() != "mps"



# ── review-swarm fix-all v1: H3 env scrub, H6 caption escape ────────


def test_render_image_caption_with_link_break_is_escaped():
    """H6 fix: an adversarial caption like `](javascript:...)` must not
    be able to close the markdown link and inject content. We don't
    drop the literal "javascript:" text (it's still part of the caption
    *content*), but the `]` and `[` MUST be escaped so a markdown parser
    sees a single link, not two."""
    from nano_notebooklm.ingest.extractors_mineru import _render_block
    out = _render_block({
        "type": "image",
        "img_path": "images/x.jpg",
        "image_caption": ["alt](javascript:alert(1))"],
    })
    # The closing `]` in the adversarial caption must be backslash-escaped.
    assert "\\]" in out, f"caption ']' not escaped: {out!r}"
    # No fully-formed second `](` link-syntax close (i.e. the link must
    # end at the real, unescaped `](`).
    real_link_closes = sum(
        1 for i in range(len(out) - 1)
        if out[i] == "]" and out[i + 1] == "(" and (i == 0 or out[i - 1] != "\\")
    )
    assert real_link_closes == 1, f"caption broke out of markdown link: {out!r}"


def test_render_image_dangerous_scheme_dropped():
    """H6 fix: img_path with javascript:/data:/vbscript:/file: schemes
    drops the link entirely, retaining only the caption text."""
    from nano_notebooklm.ingest.extractors_mineru import _render_block
    for scheme in ("javascript:alert(1)", "data:text/html,<script>", "vbscript:msg",
                   "file:///etc/passwd"):
        out = _render_block({
            "type": "image",
            "img_path": scheme,
            "image_caption": ["caption"],
        })
        assert scheme not in out, f"dangerous scheme {scheme!r} leaked: {out!r}"


def test_render_image_relative_path_preserved():
    """H6 fix: legitimate relative `images/<sha>.jpg` paths stay as-is."""
    from nano_notebooklm.ingest.extractors_mineru import _safe_markdown_image
    out = _safe_markdown_image("Figure 1", "images/abc.jpg")
    assert out == "![Figure 1](images/abc.jpg)"


def test_build_mineru_env_scrubs_credentials():
    """H3 fix: subprocess env must NOT include OPENAI/ANTHROPIC/AWS keys."""
    import os
    from nano_notebooklm.ingest.extractors_mineru import _build_mineru_env
    # Inject fake creds into our env
    os.environ["OPENAI_API_KEY"] = "sk-secret-12345"
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-secret"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "AWS-deadbeef"
    try:
        env = _build_mineru_env(device="cpu")
        assert "OPENAI_API_KEY" not in env
        assert "ANTHROPIC_API_KEY" not in env
        assert "AWS_SECRET_ACCESS_KEY" not in env
        # but functional env should be there
        assert "PATH" in env
        assert env["MINERU_DEVICE_MODE"] == "cpu"
    finally:
        for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AWS_SECRET_ACCESS_KEY"):
            os.environ.pop(k, None)


def test_build_mineru_env_keeps_proxy_and_hf_cache():
    """H3 fix: proxy + huggingface cache env vars are on the allowlist
    (mineru needs them to download models)."""
    import os
    from nano_notebooklm.ingest.extractors_mineru import _build_mineru_env
    os.environ["HTTPS_PROXY"] = "http://127.0.0.1:7890"
    os.environ["HF_HOME"] = "/tmp/hf"
    try:
        env = _build_mineru_env(device="cpu")
        assert env.get("HTTPS_PROXY") == "http://127.0.0.1:7890"
        assert env.get("HF_HOME") == "/tmp/hf"
    finally:
        for k in ("HTTPS_PROXY", "HF_HOME"):
            os.environ.pop(k, None)
