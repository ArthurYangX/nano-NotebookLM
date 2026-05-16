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
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from nano_notebooklm.types import PageInfo

logger = logging.getLogger(__name__)


# H3 fix (review-swarm fix-all v1): forwarding the whole parent env to the
# mineru subprocess leaks OPENAI_API_KEY / ANTHROPIC_API_KEY / AWS_* / etc.
# If mineru ever logs env on crash or loads a 3rd-party plugin those creds
# escape. Allowlist only the env that mineru *needs* (PATH so it can find
# tools; HOME for model cache lookup; HF_HOME / MINERU_* knobs; proxy vars
# so it can reach huggingface; locale for utf-8 output). Everything else
# stays in our process.
_MINERU_ENV_ALLOWLIST = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "TMPDIR",
    "LANG", "LC_ALL", "LC_CTYPE",
    "HF_HOME", "HF_HUB_CACHE", "HF_HUB_OFFLINE",
    "HUGGINGFACE_HUB_CACHE", "MODELSCOPE_CACHE",
    "TRANSFORMERS_CACHE", "TORCH_HOME", "XDG_CACHE_HOME",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy",
})


def _build_mineru_env(device: str) -> dict[str, str]:
    """Build a minimal env for the mineru subprocess (H3 fix)."""
    env = {k: v for k, v in os.environ.items() if k in _MINERU_ENV_ALLOWLIST}
    env["MINERU_DEVICE_MODE"] = device
    return env


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

        env = _build_mineru_env(device)

        logger.info(
            "mineru: starting %s lang=%s pages=%s..%s device=%s",
            filepath.name, lang,
            "0" if start_page is None else start_page,
            "end" if end_page is None else end_page,
            device,
        )
        t0 = time.monotonic()
        # M5 + M6 (review-swarm fix-all v1):
        #   - errors="replace" so mineru's non-utf-8 panic dumps don't
        #     crash the wrapper with UnicodeDecodeError;
        #   - capture_output buffers ALL stdout+stderr in memory which on
        #     a 100-page PDF can hit 10MB+. We keep stderr bounded by
        #     piping it through a background draining thread that retains
        #     only the last ~200 lines for the error message.
        from collections import deque
        from threading import Thread

        stderr_tail: deque[str] = deque(maxlen=200)

        def _drain(stream, sink: deque[str]) -> None:
            try:
                for line in stream:
                    sink.append(line.rstrip("\n"))
            except Exception:
                pass

        proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        drainer = Thread(target=_drain, args=(proc.stderr, stderr_tail), daemon=True)
        drainer.start()
        try:
            proc.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            elapsed = time.monotonic() - t0
            logger.warning("mineru: timeout after %.1fs on %s", elapsed, filepath.name)
            raise MinerUExtractionError(
                f"mineru timed out after {timeout_seconds}s on {filepath.name}\n"
                "stderr tail:\n" + "\n".join(list(stderr_tail)[-20:])
            )
        finally:
            drainer.join(timeout=1.0)

        elapsed = time.monotonic() - t0
        if proc.returncode != 0:
            logger.error(
                "mineru: failed (%s) in %.1fs on %s",
                proc.returncode, elapsed, filepath.name,
            )
            raise MinerUExtractionError(
                f"mineru exited {proc.returncode}\nstderr tail:\n"
                + "\n".join(list(stderr_tail)[-20:])
            )
        logger.info("mineru: completed %s in %.1fs", filepath.name, elapsed)

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


def extract_pdfs_mineru_batch(
    filepaths: list[str | Path],
    lang: str = "ch",
    output_dir: str | Path | None = None,
    *,
    timeout_seconds: int = 3600,
    device: str = "cpu",
) -> dict[str, list[PageInfo]]:
    """H5 fix (review-swarm fix-all v1): batch-extract many PDFs in a
    single mineru subprocess.

    The mineru CLI loads ~5GB of layout/OCR/formula/table models on
    startup. Calling it once per PDF (the path `extract_pdf_mineru`
    takes) pays 50s × N cold-start overhead. The CLI natively supports
    `-p <dir>` and processes every file under that directory in one
    process, so we symlink (or copy as fallback) all PDFs into a single
    temp dir, run one subprocess, then read each PDF's content_list.json
    back out.

    Returns: `{filepath_str: list[PageInfo]}` keyed by the **original**
    filepath, so callers can match each PageInfo list back to its
    source file. Files that mineru failed on are simply absent from the
    returned dict (callers should fall back per-file).

    Args mostly mirror `extract_pdf_mineru`. `timeout_seconds` defaults
    to 1 hour because a batch of 20 PDFs at 10s/page × 50 pages can
    easily take 30 minutes.
    """
    mineru_cli = _resolve_mineru_cli()
    if mineru_cli is None:
        raise MinerUNotFoundError(
            "mineru CLI not found. Install with: pip install 'mineru[pipeline]'"
        )

    paths = [Path(p).resolve() for p in filepaths]
    missing = [p for p in paths if not p.exists()]
    if missing:
        raise FileNotFoundError(f"missing inputs: {missing[:3]}")
    if not paths:
        return {}

    cleanup_input = False
    cleanup_output = False
    if output_dir is None:
        output_dir = Path(tempfile.mkdtemp(prefix="mineru_batch_out_"))
        cleanup_output = True
    else:
        output_dir = Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

    # Stage all PDFs in a single dir for mineru's directory mode. We
    # prefer symlinks for zero-copy; fall back to copy on filesystems
    # that don't support them (mostly the macOS sandboxed case).
    input_dir = Path(tempfile.mkdtemp(prefix="mineru_batch_in_"))
    cleanup_input = True
    stem_to_original: dict[str, Path] = {}
    try:
        for p in paths:
            staged = input_dir / p.name
            # If two inputs have the same basename we'd collide. Disambiguate
            # by prepending a short hash; the stem mineru produces will use
            # this filename, so we record the mapping.
            if staged.exists():
                import hashlib
                h = hashlib.sha1(str(p).encode()).hexdigest()[:8]
                staged = input_dir / f"{p.stem}_{h}{p.suffix}"
            try:
                staged.symlink_to(p)
            except OSError:
                shutil.copy2(p, staged)
            stem_to_original[staged.stem] = p

        cmd = [
            mineru_cli,
            "-p", str(input_dir),
            "-o", str(output_dir),
            "-b", "pipeline",
            "-l", lang,
        ]
        env = _build_mineru_env(device)
        logger.info(
            "mineru batch: %d PDFs lang=%s device=%s",
            len(paths), lang, device,
        )
        t0 = time.monotonic()

        from collections import deque
        from threading import Thread

        stderr_tail: deque[str] = deque(maxlen=200)

        def _drain(stream, sink):
            try:
                for line in stream:
                    sink.append(line.rstrip("\n"))
            except Exception:
                pass

        proc = subprocess.Popen(
            cmd, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
        drainer = Thread(target=_drain, args=(proc.stderr, stderr_tail), daemon=True)
        drainer.start()
        try:
            proc.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise MinerUExtractionError(
                f"mineru batch timed out after {timeout_seconds}s for {len(paths)} PDFs"
            )
        finally:
            drainer.join(timeout=1.0)

        elapsed = time.monotonic() - t0
        if proc.returncode != 0:
            logger.error("mineru batch failed (%s) in %.1fs", proc.returncode, elapsed)
            raise MinerUExtractionError(
                f"mineru batch exited {proc.returncode}\nstderr tail:\n"
                + "\n".join(list(stderr_tail)[-30:])
            )
        logger.info(
            "mineru batch: %d PDFs done in %.1fs (%.1fs/file avg)",
            len(paths), elapsed, elapsed / max(len(paths), 1),
        )

        # Read each PDF's content_list.json back out.
        results: dict[str, list[PageInfo]] = {}
        for stem, original in stem_to_original.items():
            content_list = output_dir / stem / "auto" / f"{stem}_content_list.json"
            if not content_list.exists():
                logger.warning(
                    "mineru batch: no output for %s (stem=%s)", original.name, stem
                )
                continue
            try:
                with content_list.open(encoding="utf-8") as fh:
                    blocks = json.load(fh)
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("mineru batch: bad output for %s: %s", original.name, exc)
                continue
            pages = _blocks_to_pages(blocks)
            total = max((p.page or 0) for p in pages) if pages else 0
            for p in pages:
                p.total_pages = total
            results[str(original)] = pages
        return results
    finally:
        if cleanup_input:
            shutil.rmtree(input_dir, ignore_errors=True)
        if cleanup_output:
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
        return _safe_markdown_image(caption, path)
    # Unknown block type — keep its text if present, otherwise skip.
    return (b.get("text") or "").strip()


# H6 fix (review-swarm fix-all v1): MinerU's image_caption is content
# from the user's PDF and can be adversarial. Naively interpolating it
# into `f"![{caption}]({path})"` lets a PDF caption like
# `](javascript:alert(1))` close the link and inject an arbitrary URL.
# That chunk text then flows to the chat answer + Notes preview, where
# the frontend renders markdown.
#
# Defense: (1) escape `]` and `)` and backslash in caption so the
# parser can't see a fake link close, and (2) reject any path whose
# scheme is dangerous (javascript:/data:/vbscript:); only relative
# paths or http(s) survive. Images mineru produces are always relative
# `images/<sha>.jpg`, so the scheme guard never blocks a legitimate
# block.
_DANGEROUS_URL_SCHEMES = ("javascript:", "data:", "vbscript:", "file:")


def _safe_markdown_image(caption: str, path: str) -> str:
    if not path:
        return ""
    lowered = path.strip().lower()
    for scheme in _DANGEROUS_URL_SCHEMES:
        if lowered.startswith(scheme):
            # Drop the link entirely — caption (if any) becomes plain text.
            return caption.strip()
    safe_caption = (
        caption.replace("\\", "\\\\")
               .replace("]", "\\]")
               .replace("[", "\\[")
    )
    safe_path = (
        path.replace("\\", "\\\\")
            .replace(")", "\\)")
            .replace("(", "\\(")
    )
    return f"![{safe_caption}]({safe_path})"
