"""CLI entry point for nano-NOTEBOOKLM."""

import sys


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        import uvicorn
        host = "127.0.0.1"
        port = 8000
        for arg in sys.argv[2:]:
            if arg.startswith("--host="):
                host = arg.split("=", 1)[1]
            elif arg.startswith("--port="):
                port = int(arg.split("=", 1)[1])
        uvicorn.run("api.server:app", host=host, port=port)
    else:
        print("nano-NOTEBOOKLM")
        print()
        print("Usage:")
        print("  nano-nlm serve [--host=127.0.0.1] [--port=8000]   Start the API server")
        print("  python scripts/ingest_course.py                   Ingest a course directory")
        print("  python scripts/build_indices.py                   Build search indices")


if __name__ == "__main__":
    main()
