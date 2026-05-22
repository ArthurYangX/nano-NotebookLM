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
                                 ~5300 lines; one file deliberately. Search by route.

frontend/                        React 18, no bundler. Edit a .jsx and reload.
  app.jsx                        Top-level shell, course switching, topbar chips.
  assistant.jsx                  Chat sidebar.
  reader.jsx                     PDF/PPTX viewer + citation jump-to-page.
  notes.jsx                      LaTeX notes editor (CodeMirror) + tectonic PDF.
  mindmap.jsx                    d3-force KG with edit ops.
  exam-prep.jsx                  Self-evolving quiz.
  quiz.jsx                       Practice quiz (one-shot, non-bank).
  library.jsx                    Course library sidebar + course picker modal.
  processing.jsx                 Upload progress overlay (consumes NDJSON stage events
                                 from /api/upload + the ETA estimator from server.py).
  settings.jsx                   Providers matrix, embedding-preset radios, status badges.
  tweaks-panel.jsx               Per-course generation tweaks (note/quiz/exam knobs).
  i18n.js                        Central STRINGS table (zh + en). All user-facing copy
                                 goes here; components call `t("key", {placeholders})`.
                                 New strings: add to the dict + reference via t(); never
                                 inline a literal in JSX. Missing key falls back to the key.
  api.js                         Fetch wrappers; one place to add a new endpoint client-side.
  study-state.js                 Shared client state (active course / file / chat session).
  markdown.js                    Markdown renderer (assistant answers, notes preview).
  latex-to-html.js               LaTeX → HTML sanitizer for KaTeX rendering.
  styles.css                     All CSS lives here.

nano_notebooklm/
  ai/
    base.py                      LLMBackend ABC (carries name + kind attrs) + TruncationSignal.
    openai_backend.py            Generic OpenAI-compatible client (OpenAI, DeepSeek, Moonshot,
                                 Zhipu, MiniMax, Groq, Together, Gemini compat endpoint, …).
                                 Detects base_url to handle codex-proxy and DeepSeek quirks.
                                 Accepts http_timeout kwarg so /api/providers/{id}/test can
                                 cap the underlying httpx client.
    claude_backend.py            Anthropic native API (same http_timeout kwarg).
    local_backend.py             Legacy thin subclass; new code paths construct OpenAIBackend
                                 directly via _build_backend (kept for backward import compat).
    router.py                    ModelRouter — reads artifacts/providers.json, dispatches by
                                 task_type or explicit override, handles retries + fallback.
                                 Methods: reload() to swap backends without restart,
                                 get_openai_compat() for endpoints that need a chat-completions
                                 backend (agent, explain-node), diagnose_row() for UI badges.
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
  business logic — read `router.backends` keys (which are provider ids
  like `"openai-main"`, `"claude-main"`, or a user-added `"openai-alt"`,
  NOT class-family names). The frontend reads `available_backends` +
  `providers` from `/api/status` to render the topbar chip + Settings
  matrix. For endpoints that require an OpenAI-compatible backend
  specifically (agent loop, explain-node), call
  `router.get_openai_compat()` instead of dict-lookup by literal name.
- **Provider config is hot-swappable.** `artifacts/providers.json` is
  the source of truth; `.env` is the first-boot seed only. Mutations
  go through `PUT/DELETE /api/providers/...` + `router.reload()` under
  `_PROVIDERS_LOCK`. Never close an old backend instance's httpx client
  inside `reload()` — in-flight requests on the stack still hold the
  ref. Always go through `save_providers()` (atomic write 0o600) +
  `router.reload()`, never edit the file behind the router's back.
- **API key safety.** `api_key_ref` is the only on-disk representation;
  `env:VAR` resolves at backend-build via `os.getenv`, `literal:...`
  stores the key inline. Responses MUST go through
  `_redact_provider_row` so `literal:` values never leave the process.
- **All UI copy through i18n.** Every user-facing string in `frontend/*.jsx`
  goes through `t("key", {vars})` resolved against `frontend/i18n.js`.
  Don't hardcode `"Loading…"` / `"加载中…"` in JSX. Add a new entry to
  the STRINGS table with both `zh` and `en` bodies; placeholders use
  `{name}` and are substituted by `t()` at call time.

---

## Common tasks

### Add support for a new LLM provider

If the provider exposes an OpenAI-compatible `/v1/chat/completions`,
**no code changes are needed**. The user can either:

- Add it via the Settings UI (`PUT /api/providers/<id>` under the hood,
  no restart needed); or
- Seed it in `.env` (`OPENAI_BASE_URL` + `OPENAI_API_KEY` +
  `OPENAI_MODEL`) and let the first-boot bootstrap synthesise the row.

Either way the row lands in `artifacts/providers.json`. Update the
README table only if the endpoint is worth documenting as a built-in
shortcut.

If the provider has a native non-OpenAI API (like Anthropic): create
`nano_notebooklm/ai/<name>_backend.py` modeled on `claude_backend.py`,
add config keys to `nano_notebooklm/config.py`, add the kind to
`config.PROVIDER_KINDS` + `ProviderUpsertRequest.kind` Literal in
`api/server.py`, dispatch on it inside
`ModelRouter._build_backend`, and add an icon variant to
`frontend/app.jsx` (topbar chip `iconFor`) + a kind label in
`frontend/i18n.js`. `ChatRequest.backend` is `str | None` and does NOT
need updating — provider ids are dynamic.

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

### Background upload pipeline

`POST /api/upload/{course_id}` does NOT block on the heavy ingest; it
spawns an in-memory background task (`_run_upload_pipeline`) and returns
`task_id` immediately. The browser then polls `GET /api/upload/{task_id}`
for stage + percent + detail. Stages, in order:
`extracting → chunking → embedding → kg_stage_a → kg_stage_b`, each
emitting truthful 0..100 boundaries; server.py owns the final
`KG_STAGE_B=100` after `kg.save()`. Tasks live in process memory with a
1h TTL and die with the server (no Celery, by design).

The processing overlay also shows an ETA computed by
`_estimate_upload_duration_seconds` (constants near the top: per-page
extraction cost per engine, mineru cold-start surcharge, Stage A/B
concurrency + per-call seconds, final safety margin). Tweak these when
real-world wallclock drifts persistently more than ~30% off the bar.

### Hook MinerU into a non-PDF source

MinerU only natively eats PDFs, but `.pptx` can ride a sidecar: when
`engine=='mineru'` AND `previews_dir` is passed to `KBStore.ingest_course`,
each `.pptx` is matched to its soffice-rendered sidecar PDF (path lookup
via `ingest.pptx_pdf.sidecar_path`). The sidecar is added to the mineru
batch; results land back on the original `.pptx` filename so chunks
stamp the user-facing source. `.ppt` rides the same path. If no sidecar
exists, extraction silently falls back to python-pptx for that file.

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
curl http://localhost:8000/api/status | jq '.available_backends, .providers'
curl http://localhost:8000/api/providers | jq
# Pin chat to an explicit provider id
curl -X POST http://localhost:8000/api/chat -H 'Content-Type: application/json' \
  -d '{"question": "ping", "backend": "openai-main"}' | jq '.path, .model'
# One-click connectivity probe (5s timeout, no detail field on failure)
curl -X POST http://localhost:8000/api/providers/openai-main/test | jq
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
