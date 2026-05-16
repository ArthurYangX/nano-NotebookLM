"""Standalone demo: extract a PDF with MinerU and dump pages to JSON.

This is *not* wired into KBStore yet — it's a CLI smoke test to prove the
mineru extractor's output is what we expect before changing extract_file().

Usage:
    python scripts/ingest_with_mineru.py <pdf> [--lang ch|en] [--start N] [--end N]

Outputs:
    - <pdf-stem>_mineru_pages.json — list of {page, text, total_pages}
    - prints summary: pages, blocks-per-type, has-formula coverage

The mineru subprocess writes intermediate artifacts to a tmp dir which is
cleaned up unless --keep-tmp is passed (handy when you want to inspect
the raw markdown + extracted images).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nano_notebooklm.ingest.extractors_mineru import extract_pdf_mineru


_FORMULA_PATTERNS = (
    re.compile(r"\$\$"),                     # block math
    re.compile(r"(?<!\\)\$[^\n$]{2,}\$"),    # inline math (heuristic)
    re.compile(r"\\frac|\\sum|\\int|\\mid"), # LaTeX macros
)


def page_has_formula(text: str) -> bool:
    return any(p.search(text) for p in _FORMULA_PATTERNS)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--lang", default="ch", choices=["ch", "en"])
    parser.add_argument("--start", type=int, default=None, help="0-indexed start page")
    parser.add_argument("--end", type=int, default=None, help="0-indexed inclusive end page")
    parser.add_argument("--output", type=Path, default=None,
                        help="Output JSON path (default: <stem>_mineru_pages.json next to PDF)")
    parser.add_argument("--keep-tmp", type=Path, default=None,
                        help="Keep mineru intermediate dir here for inspection")
    args = parser.parse_args()

    if not args.pdf.exists():
        print(f"PDF not found: {args.pdf}", file=sys.stderr)
        return 1

    out = args.output or args.pdf.with_name(f"{args.pdf.stem}_mineru_pages.json")
    print(f"Extracting {args.pdf} (lang={args.lang}, range=[{args.start}, {args.end}]) ...")
    t0 = time.time()
    pages = extract_pdf_mineru(
        args.pdf,
        lang=args.lang,
        output_dir=args.keep_tmp,
        start_page=args.start,
        end_page=args.end,
    )
    elapsed = time.time() - t0
    n = len(pages)
    print(f"✓ {n} pages in {elapsed:.1f}s ({elapsed / max(n,1):.1f}s/page)")

    formula_pages = sum(1 for p in pages if page_has_formula(p.text))
    print(f"  pages with formulae: {formula_pages}/{n}")
    print(f"  total characters:    {sum(len(p.text) for p in pages):,}")

    out.write_text(
        json.dumps(
            [
                {"page": p.page, "total_pages": p.total_pages, "text": p.text}
                for p in pages
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"  written:             {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
