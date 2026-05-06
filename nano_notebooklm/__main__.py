"""CLI entry point for nano-NOTEBOOKLM."""

import sys


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        from ui.app import main as serve_main
        serve_main()
    else:
        print("nano-NOTEBOOKLM v0.1.0")
        print()
        print("Usage:")
        print("  python -m nano_notebooklm serve     Start the Gradio web UI")
        print("  python scripts/ingest_course.py      Ingest course materials")
        print("  python scripts/build_indices.py       Build search indices")


if __name__ == "__main__":
    main()
