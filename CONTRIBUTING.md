# Contributing to nano-NotebookLM

Thanks for considering a contribution. nano-NotebookLM is a small,
self-hosted project — the bar is "does this make the single-user
study-assistant experience better without adding operational burden."

## Quick links

- Bugs / feature ideas: open an [issue](https://github.com/ArthurYangX/nano-NotebookLM/issues).
- Architecture & code map: [`CLAUDE.md`](CLAUDE.md) is the canonical
  guide. Read it before touching `api/server.py` or
  `nano_notebooklm/`.

## Dev setup

```bash
git clone https://github.com/ArthurYangX/nano-NotebookLM
cd nano-NotebookLM
python -m venv .venv && source .venv/bin/activate
pip install -e ".[test]"
cp .env.example .env       # at least one LLM key
python api/server.py       # http://localhost:8000
```

## Running tests

The full suite runs **offline** — no LLM keys required, embeddings are
faked deterministically, the router is monkeypatched.

```bash
pytest                              # full suite (~1000 tests, < 60s)
pytest tests/test_api_smoke.py -x   # quick API gate
pytest -k retrieval                 # subset by keyword
```

PRs that break `pytest` will be asked to fix the test, not delete it.

## What we welcome

- **Bug fixes** with a regression test in `tests/`.
- **New LLM providers** that speak OpenAI-compatible `/v1/chat/completions`
  — no code needed, just open an issue if you want it documented in the
  README provider table.
- **New skills** (quiz variants, study mode, …) following the pattern
  in `nano_notebooklm/skills/` (see `CLAUDE.md → Add a new skill`).
- **Frontend polish** — `.jsx` files, no build step, edit + refresh.
- **Docs / typos / translations** of any size, no need to ask first.

## What's out of scope (please ask before starting)

These got cut deliberately to keep self-hosting simple:

- Authentication / multi-tenant isolation.
- Persistent task queues (Celery / RQ).
- A real database (replacing `./artifacts/`).
- A frontend build step (Vite / webpack).
- Prometheus / OTel metrics beyond `/api/status`.

If you have a strong case for one of these, open an issue first so we
can talk about scope before you write code.

## Code style

- Follow what's already there. The codebase is intentionally low-magic
  and avoids premature abstraction.
- **No emojis** in code, comments, or logs (UI copy is the exception).
- **Comments earn their keep** — only when the *why* is non-obvious.
- **One-file-per-route ban**: `api/server.py` stays a single file.
- See [`CLAUDE.md → Conventions to follow`](CLAUDE.md) for the full
  rules.

## PR checklist

- [ ] `pytest` passes locally.
- [ ] New behavior has at least one test.
- [ ] No new dependencies unless absolutely needed (justify in the PR).
- [ ] No secrets / API keys in commits (`.env` is gitignored — keep it
      that way).
- [ ] README / CHANGELOG updated if user-visible behavior changed.

## License

By contributing, you agree your contributions are licensed under
[MIT](LICENSE), same as the rest of the project.
