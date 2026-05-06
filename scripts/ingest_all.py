"""Batch ingest all courses from NLPProject."""

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

from nano_notebooklm.kb.store import KBStore

NLPPROJECT = Path("/Users/arthuryang/Desktop/大三学习/NLPProject")

COURSES = [
    "15-213",
    "CS182",
    "CS231N",
    "CS285",
    "CSE 234",
    "机器人导论",
    "计算机组成原理",
    "模式识别",
]
# Skip CS336_Notebook (14GB, too large) and ECE408_FA23_UIUC-master (single file)

def main():
    kb = KBStore()
    total_chunks = 0
    start = time.time()

    for course_id in COURSES:
        course_dir = NLPPROJECT / course_id
        if not course_dir.exists():
            print(f"SKIP {course_id}: directory not found")
            continue

        print(f"\n{'='*60}")
        print(f"Ingesting: {course_id}")
        print(f"{'='*60}")

        try:
            course = kb.ingest_course(str(course_dir), course_id)
            chunks = kb.get_chunks(course_id)
            total_chunks += len(chunks)
            print(f"  -> {len(chunks)} chunks")
        except Exception as e:
            print(f"  -> FAILED: {e}")

    print(f"\n{'='*60}")
    print(f"All courses ingested: {total_chunks} total chunks")
    print(f"Time: {time.time() - start:.1f}s")
    print(f"{'='*60}")

    # Build global index
    print("\nBuilding global search index...")
    kb.build_index()
    print("Done!")


if __name__ == "__main__":
    main()
