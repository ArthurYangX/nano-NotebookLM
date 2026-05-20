# Using nano-NOTEBOOKLM with an AI coding assistant

This file is for **AI coding assistants** (Claude Code, Cursor, Codex,
Copilot, …) that you ask to extend, debug, or operate this codebase.
Hand it the path `CLAUDE.md` and it will know enough to make safe,
targeted changes.

Humans: see [`README.md`](README.md) for install + usage.

---

## What this project is

A self-hosted study assistant that ingests course documents (PDF / PPTX
/ DOCX / Markdown) and provides chat, structured notes, quizzes, an
exam-prep loop, and an editable knowledge graph — all backed by a
provider-agnostic LLM router.

- Single-process FastAPI + React 18 (CDN, no build).
- No DB, no auth, no multi-tenant — everything lives in `./artifacts/`.
- Default LLM backend is OpenAI-compatible; Anthropic Claude and any
  local OpenAI-compatible server (Ollama / vLLM / LM Studio / llama.cpp)
  are first-class siblings.

---

## Repo layout — where to make changes

```
api/server.py                    FastAPI routes + middleware + Pydantic models.
                                 ~3000 lines; one file deliberately. Search by route.

frontend/                        React 18, no bundler. Edit a .jsx and reload.
  app.jsx                        Top-level shell, course switching, topbar chips.
  assistant.jsx                  Chat sidebar.
  reader.jsx                     PDF/PPTX viewer + citation jump-to-page.
  notes.jsx                      LaTeX notes editor (CodeMirror) + tectonic PDF.
  mindmap.jsx                    d3-force KG with edit ops.
  exam-prep.jsx                  Self-evolving quiz.
  settings.jsx                   Backend chip radios, status badges.
  api.js                         Fetch wrappers; one place to add a new endpoint client-side.

nano_notebooklm/
  ai/
    base.py                      LLMBackend ABC + TruncationSignal sentinel.
    openai_backend.py            Generic OpenAI-compatible client (OpenAI, DeepSeek, Moonshot,
                                 Zhipu, MiniMax, Groq, Together, Gemini compat endpoint, …).
                                 Detects base_url to handle codex-proxy and DeepSeek quirks.
    claude_backend.py            Anthropic native API.
    local_backend.py             Thin subclass of OpenAIBackend for local /v1 servers.
    router.py                    ModelRouter — registers configured backends, dispatches by
                                 task_type or explicit override, handles retries + fallback.
    prompt_templates.py          All system prompts and language bindings.

  ingest/                        File → chunk pipeline (PDF/PPTX/DOCX/MD).
  kb/                            FAISS + BM25 + RRF + graph_search (KG-driven retriever).
  kg/                            Two-stage KG extraction (topics → leaf concepts).
  skills/                        Independent business logic, each exposes execute(params).
                                 Filenames are inconsistent (historical) — don't rename
                                 for "consistency"; tests grep by exact filename.
    qa_skill.py                  /api/chat — intent routing, graphrag, RAG, translation,
                                 cross-course, general; the largest skill.
    notes_full_course.py         Per-file LaTeX generation with incremental cache.
    note_generator.py            Legacy single-shot note skill (kept for /api/notes).
    quiz_generator.py            Practice quiz.
    exam_prep.py                 Topic plan → seed questions → quiz draw → submit + variant.
    exam_analyzer.py             One-shot exam-pattern analysis (older surface).
    report_generator.py          Long-form course report.
    mastery_tracker.py           Read-only mastery scoring.
    latex_sanitizer.py           Whitelist sanitizer for LaTeX bodies from the LLM.
  orchestrator/                  Skill registry + memory + multi-turn agent loop + tools.

scripts/                         CLI helpers — ingest_course.py, build_indices.py, reembed_all.py.
tests/                           pytest. Runs offline; uses deterministic fake embeddings + monkeypatched LLM.
```

---

## Conventions to follow

- **One file, many routes.** `api/server.py` stays single-file. Don't
  split into `routes/`; tests grep route handlers by name.
- **Pydantic everywhere on the wire.** Request and response models live
  beside the route. `model_config = {"extra": "forbid"}` is the default
  for response models — new sidecar fields must be added to the model
  explicitly, not silently smuggled through.
- **Pure async skills, sync backends inside thread pool.** OpenAI SDK
  calls run in `_executor` (24 worker default). Don't `await` from
  inside `_complete_codex_sync` — wrap with `loop.run_in_executor`.
- **NDJSON streaming envelope.** Long-running endpoints (notes, upload,
  agent) emit one JSON event per line: `{type: "stage", ...}` → … →
  `{type: "done", ...}` or `{type: "error", ...}`. Frontend parses by
  splitting on `\n`.
- **No emojis in code or comments** unless the file is user-facing UI
  copy. Logs use plain text.
- **Comments earn their keep.** Default to none. Only add when the
  *why* is non-obvious (a workaround, a hidden constraint, a subtle
  invariant). Don't restate what the code does.
- **Backend selection is data-driven.** Don't hardcode backend names in
  business logic — read `router.backends` keys. The frontend reads
  `available_backends` from `/api/status` to render chips.

---

## Common tasks

### Add support for a new LLM provider

If the provider exposes an OpenAI-compatible `/v1/chat/completions`,
**no code changes are needed** — the user just sets `OPENAI_BASE_URL`,
`OPENAI_API_KEY`, and `OPENAI_MODEL` in `.env`. Update the table in
[`README.md`](README.md) and `.env.example` with the new endpoint.

If the provider has a native non-OpenAI API (like Anthropic): create
`nano_notebooklm/ai/<name>_backend.py` modeled on `claude_backend.py`,
add config keys to `nano_notebooklm/config.py`, register it in
`router._init_backends`, add an entry to the `ChatRequest.backend`
`Literal` in `api/server.py`, and a chip variant in `frontend/app.jsx` +
`frontend/settings.jsx` + `frontend/styles.css`.

### Add a new skill

1. Create `nano_notebooklm/skills/<name>_skill.py` subclassing `Skill`.
   Implement `async def execute(self, params: dict) -> SkillResult`.
2. Register in `nano_notebooklm/orchestrator/engine.py` (`self.skills`).
3. Add a `/api/<name>` route in `api/server.py` with request + response
   Pydantic models. The route should only validate input and call the
   skill — business logic stays in the skill.
4. Add a `tests/test_<name>.py` that monkeypatches the router and
   exercises the route via `TestClient`.

### Add a new endpoint that streams

Use `StreamingResponse(stream(), media_type="application/x-ndjson")`.
Inside `stream()`, run blocking work in `asyncio.to_thread` and bridge
to an `asyncio.Queue` if the producer is sync. See `/api/upload` or
`/api/notes/full-course/stream` for the pattern.

### Touch the knowledge graph

KG extraction is two-stage. Don't bypass `KnowledgeGraph.add_concepts`
— it's the only place that reconciles Stage A topics with Stage B leaf
concepts and persists `concept_embedding` (used by graph_search). When
a student edits the graph, the diff is appended to
`artifacts/courses/<id>/mindmap_edits.json` and replayed on every GET
so re-extraction never clobbers user work.

### Debug "the wrong file came back from retrieval"

Check, in order:

1. `/api/chat` request body — what's `active_source_file`? It biases
   retrieval toward that file. If you don't want the bias, send
   `active_source_file: null`.
2. `/api/sources/{course_id}` — is the chunk count what you expect for
   the relevant file? If a re-upload failed mid-pipeline, you may have
   half the chunks.
3. Graphrag admission floor — `GRAPHRAG_SCORE_GATE_TOP1` env (default
   `0.15`). Below this, graphrag falls through to BM25/vector RAG.
4. Read `artifacts/courses/<id>/knowledge_graph.json` for the actual
   topic IDs and which file each leaf concept came from.

### Bump a model version

Edit `.env` (or `.env.example` for the next user) — `OPENAI_MODEL`,
`CLAUDE_MODEL`, `LOCAL_LLM_MODEL`. Restart the server. No code change.

---

## Run + test

```bash
source .venv/bin/activate
python api/server.py                   # http://localhost:8000
pytest                                 # full unit + API smoke (offline)
pytest tests/test_api_smoke.py -x      # quick gate
```

If you change a backend, smoke-test it manually:

```bash
curl http://localhost:8000/api/status | jq '.available_backends, .openai_model, .claude_model, .local_llm_model'
curl -X POST http://localhost:8000/api/chat -H 'Content-Type: application/json' \
  -d '{"question": "ping", "backend": "openai"}' | jq '.path, .model'
```

---

## What's deliberately *not* here

These were on the roadmap and got cut to keep the project simple to
self-host. If you're tempted to add them, talk to a human first:

- Authentication / multi-tenant isolation.
- A persistent task queue (Celery / RQ). Upload tasks live in memory
  with a 1h TTL and die with the server.
- A database. `artifacts/` files are enough for single-user scale and
  survive `git clean` etc. unaided.
- Prometheus / OTel metrics. `/api/status` has p50 latencies and
  that's the budget.
- A frontend build step. CDN React + Babel-standalone is the deal; if
  the page loads slowly, lazy-load the heavy `.jsx` files, don't add
  Vite.
