"""MinerU-backed PDF extractor.

Use this when you need high-quality formula + table + layout recovery
(e.g., HMM / DL slides where PyMuPDF reduces equations to single-character
columns). About 10x slower than PyMuPDF on M4 CPU (~10s/page first time,
~6s/page on warm cache), so it's an opt-in pipeline, not the default.

Pipeline:
  1. subprocess → `mineru -b pipeline -l <ch|en> -p <pdf> -o <tmp>`
  2. parse `<tmp>/<stem>/auto/<stem>_content_list.json` — one block per
     paragraph / equation / table / image, each tagged with `page_idx`
     and `bbox`.
  3. group by page_idx, sort by bbox.y_top, render every block back to
     text (equations → $$ LaTeX $$, tables → HTML, images → ![](path)).
  4. yield `list[PageInfo]` 1-based page numbers, matching the PyMuPDF
     extractor's contract.

The mineru CLI starts a local FastAPI server for each invocation and
re-loads the layout/OCR/formula models. On a cold cache that's ~50s; on
a warm cache (run twice in a row) it's ~6s. For batch demos, prefer one
mineru call per PDF and let the OS file cache absorb the cost.

MPS is currently disabled (`MINERU_DEVICE_MODE=cpu`) — under MPS the
pipeline backend hangs at `DocAnalysis init` with 0% CPU. CPU mode runs
cleanly at ~10s/page on M4.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from nano_notebooklm.types import PageInfo


def _resolve_mineru_cli() -> str | None:
    """Find the mineru CLI executable.

    Order:
      1. `<sys.executable_dir>/mineru` — the venv adjacent to the running
         Python. This is the common case when called from `.venv/bin/python
         scripts/...` because PATH may not include `.venv/bin`.
      2. `shutil.which("mineru")` — anywhere on PATH.

    Returns None if neither resolves.
    """
    venv_cli = Path(sys.executable).parent / "mineru"
    if venv_cli.exists() and os.access(venv_cli, os.X_OK):
        return str(venv_cli)
    return shutil.which("mineru")


class MinerUNotFoundError(RuntimeError):
    """Raised when the `mineru` CLI is missing from the active venv."""


class MinerUExtractionError(RuntimeError):
    """Raised when mineru exits non-zero or produces no content_list.json."""


def extract_pdf_mineru(
    filepath: str | Path,
    lang: str = "ch",
    output_dir: str | Path | None = None,
    *,
    start_page: int | None = None,
    end_page: int | None = None,
    timeout_seconds: int = 1800,
    device: str = "cpu",
) -> list[PageInfo]:
    """Extract PDF pages via MinerU pipeline backend.

    Args:
      filepath: absolute or relative path to the PDF.
      lang: `ch` for Chinese, `en` for English. Affects which OCR
        model is loaded; auto-detection is unreliable on slide decks.
      output_dir: directory MinerU writes to. When None a temp dir is
        used and deleted after parse. Pass a real dir to keep the
        intermediate markdown / image assets (useful for debugging).
      start_page / end_page: 0-indexed inclusive range. None = whole PDF.
      timeout_seconds: subprocess timeout. Default 1800s = 30 min, enough
        for ~100 pages on M4 CPU.
      device: `cpu` (only currently-supported on Apple Silicon) or `mps`
        (currently hangs — keep `cpu`).

    Returns: 1-based PageInfo list. Each page text is the natural reading
    order of blocks, with LaTeX equations as `$$...$$` blocks and tables
    as raw HTML. `has_formula` is *not* set here — the chunker decides
    that per chunk after concatenation / splitting.
    """
    filepath = Path(filepath).resolve()
    if not filepath.exists():
        raise FileNotFoundError(filepath)

    mineru_cli = _resolve_mineru_cli()
    if mineru_cli is None:
        raise MinerUNotFoundError(
            "mineru CLI not found. Install with: pip install 'mineru[pipeline]'"
        )

    cleanup = False
    if output_dir is None:
        output_dir = Path(tempfile.mkdtemp(prefix="mineru_extract_"))
        cleanup = True
    else:
        output_dir = Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

    try:
        cmd = [
            mineru_cli,
            "-p", str(filepath),
            "-o", str(output_dir),
            "-b", "pipeline",
            "-l", lang,
        ]
        if start_page is not None:
            cmd += ["-s", str(start_page)]
        if end_page is not None:
            cmd += ["-e", str(end_page)]

        env = os.environ.copy()
        env["MINERU_DEVICE_MODE"] = device

        result = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        if result.returncode != 0:
            raise MinerUExtractionError(
                f"mineru exited {result.returncode}\nstderr tail:\n"
                + "\n".join(result.stderr.splitlines()[-20:])
            )

        # mineru writes to <output_dir>/<stem>/auto/<stem>_content_list.json
        stem = filepath.stem
        content_list_path = output_dir / stem / "auto" / f"{stem}_content_list.json"
        if not content_list_path.exists():
            raise MinerUExtractionError(
                f"content_list.json not found at {content_list_path}\n"
                "mineru may have produced a different layout — check output dir."
            )

        with content_list_path.open(encoding="utf-8") as fh:
            blocks = json.load(fh)

        pages = _blocks_to_pages(blocks)
        # Annotate total_pages
        total = max((p.page or 0) for p in pages) if pages else 0
        for p in pages:
            p.total_pages = total
        return pages
    finally:
        if cleanup:
            shutil.rmtree(output_dir, ignore_errors=True)


def _blocks_to_pages(blocks: list[dict]) -> list[PageInfo]:
    """Group MinerU blocks by page_idx and render each page to text.

    Each block has keys: type (text|header|equation|image|chart|table),
    bbox ([x0,y0,x1,y1]), page_idx (0-based), plus type-specific payload.
    """
    by_page: dict[int, list[dict]] = {}
    for b in blocks:
        idx = b.get("page_idx")
        if idx is None:
            continue
        by_page.setdefault(idx, []).append(b)

    pages: list[PageInfo] = []
    for page_idx in sorted(by_page):
        page_blocks = sorted(
            by_page[page_idx],
            key=lambda b: (b.get("bbox", [0, 0, 0, 0])[1], b.get("bbox", [0, 0, 0, 0])[0]),
        )
        parts: list[str] = []
        for b in page_blocks:
            rendered = _render_block(b)
            if rendered:
                parts.append(rendered)
        text = "\n\n".join(parts).strip()
        if not text:
            continue
        pages.append(PageInfo(text=text, page=page_idx + 1))
    return pages


def _render_block(b: dict) -> str:
    """Render a single MinerU block to markdown/LaTeX text."""
    t = b.get("type")
    if t in ("text", "header"):
        text = (b.get("text") or "").strip()
        if not text:
            return ""
        # Headers get a markdown heading; text_level 1=#, 2=##, ...
        level = b.get("text_level")
        if t == "header" and isinstance(level, int) and level > 0:
            return f"{'#' * min(level, 6)} {text}"
        if t == "text" and isinstance(level, int) and level > 0:
            return f"{'#' * min(level, 6)} {text}"
        return text
    if t == "equation":
        latex = (b.get("text") or "").strip()
        if not latex:
            return ""
        # MinerU emits "$$\n...\n$$" already; preserve as-is so chunker
        # can detect block boundaries downstream.
        return latex if latex.startswith("$$") else f"$$\n{latex}\n$$"
    if t == "table":
        body = (b.get("table_body") or "").strip()
        return body
    if t in ("image", "chart"):
        path = b.get("img_path") or ""
        caption_field = "image_caption" if t == "image" else "chart_caption"
        captions = b.get(caption_field) or []
        caption = " ".join(c.strip() for c in captions if c.strip()) if isinstance(captions, list) else ""
        if path:
            return f"![{caption}]({path})" if caption else f"![]({path})"
        return ""
    # Unknown block type — keep its text if present, otherwise skip.
    return (b.get("text") or "").strip()
