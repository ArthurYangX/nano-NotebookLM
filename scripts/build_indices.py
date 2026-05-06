"""CLI script to build/rebuild search indices."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nano_notebooklm.kb.store import KBStore


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(description="Build search indices")
    parser.add_argument("--course-id", type=str, help="Build index for specific course (default: all)")
    args = parser.parse_args()

    kb = KBStore()
    print(f"Building index for: {args.course_id or 'all courses'}")
    kb.build_index(args.course_id)
    print("Index built successfully!")


if __name__ == "__main__":
    main()
