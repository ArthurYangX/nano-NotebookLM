"""CLI script to ingest course materials into the knowledge base."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nano_notebooklm.kb.store import KBStore


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(description="Ingest course materials")
    parser.add_argument("course_dir", type=str, help="Path to course directory")
    parser.add_argument("--course-id", type=str, help="Course identifier (default: directory name)")
    parser.add_argument("--engine", choices=["pymupdf", "mineru"], default="pymupdf",
                        help="PDF extractor (pymupdf=fast/lossy, mineru=slow/lossless)")
    parser.add_argument("--lang", choices=["ch", "en"], default="ch",
                        help="MinerU OCR language (only used when --engine=mineru)")
    parser.add_argument("--previews-dir", type=str, default=None,
                        help="Directory holding soffice-rendered .pptx → .pdf sidecars; "
                             "when set with --engine=mineru, .pptx files ride MinerU "
                             "through their sidecar PDF.")
    parser.add_argument("--build-index", action="store_true", help="Build search index after ingestion")
    args = parser.parse_args()

    kb = KBStore()
    course = kb.ingest_course(
        args.course_dir, args.course_id,
        engine=args.engine, lang=args.lang,
        previews_dir=Path(args.previews_dir) if args.previews_dir else None,
    )
    print(f"Ingested course: {course.course_id} (engine={args.engine})")

    if args.build_index:
        print("Building search index...")
        kb.build_index(course.course_id)
        print("Done!")


if __name__ == "__main__":
    main()
