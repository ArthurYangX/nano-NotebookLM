# nano-NOTEBOOKLM

AI-powered study assistant — knowledge extraction, study notes, exam prep, and course reports.

## Quick Start

```bash
cp .env.example .env  # Fill in API keys (codex proxy configured)
source .venv/bin/activate
pip install -e ".[test]"       # Include test deps
python scripts/ingest_all.py   # Ingest course data
python api/server.py           # Start server → http://localhost:8000
pytest                         # Run unit + API smoke tests
```

## Architecture

```
frontend/          React UI (Claude Design) — served as static files
  ├── app.jsx      Main app with course selection, tab routing
  ├── assistant.jsx  AI chat sidebar with real API calls
  ├── api.js       API bridge (fetch → FastAPI)
  └── *.jsx        Library, Reader, Notes, MindMap, Quiz, Processing

api/server.py      FastAPI backend — REST API + static file serving
  ├── /api/chat           RAG Q&A with source citations
  ├── /api/agent/stream   Multi-turn tool-calling agent (NDJSON event stream)
  ├── /api/notes          Structured note generation
  ├── /api/quiz           Practice quiz generation
  ├── /api/exam-analysis  Exam pattern analysis (JSON body)
  ├── /api/report         Course report generation
  ├── /api/mindmap/{id}        Two-stage KG extraction + user-edit overlay
  ├── /api/mindmap/{id}/edit   Apply student ops (add/update/delete/connect)
  ├── /api/upload/{id}    File upload + 4-stage NDJSON-streamed ingest (50MB cap, whitelisted suffixes)
  ├── /api/memory         User memory persistence
  ├── /api/courses        Course listing (mode=user|all; user hides preset courses by default)
  ├── /api/status         Backends + usage + embedding mode
  └── /api/health         Liveness probe
  Middleware: request-id, latency header, structured access log,
  global exception handler returning {error, request_id, detail}.

tests/             pytest suite — chunker, search/hybrid (RRF), API smoke
                   (request id headers, validation 422s, seeded KB).

nano_notebooklm/   Python backend modules
  ├── ai/           LLM abstraction (Claude + OpenAI/codex proxy)
  ├── ingest/       Document extraction (PDF/PPTX/DOCX/MD) + chunking
  ├── kb/           FAISS vector + BM25 keyword + RRF hybrid search
  ├── kg/           Knowledge graph (NetworkX + Mermaid)
  ├── skills/       QA, notes, quiz, exam analysis, mastery, reports
  └── orchestrator/ Skill routing, parallel execution, memory,
                    agent_loop (multi-turn tool calling) +
                    tools/ (search_kb, read_chunk, list_courses, generate_note)
```

## Key Conventions

- AI calls via codex proxy (`https://codex.ysaikeji.cn/v1`) using responses API
- Model: GPT-5.4 (configurable in .env)
- Embedding: `EMBEDDING_MODE=local` uses sentence-transformers (`all-MiniLM-L6-v2`).
  `EMBEDDING_MODE=api` calls an OpenAI-compatible `/embeddings` endpoint
  (defaults to `text-embedding-3-small` if model name is the local default).
- BM25 tokenizer: character + bigram for Chinese, word-level for English
- User memory persisted at `artifacts/user_memory.json`
- Config via `.env` (override=True to take precedence over system env vars)
- Frontend: CDN React 18 + Babel standalone (no build step). Generated
  notes / quiz / mindmap are cached per-course in `localStorage`
  under `nano-nlm:v1:<course>:<kind>` so refresh doesn't lose work.

## Course Data

8 courses ingested from `/Users/arthuryang/Desktop/大三学习/NLPProject/`:
- 15-213, CS182, CS231N, CS285, CSE 234, 机器人导论, 计算机组成原理, 模式识别
- ~15,400 chunks indexed (FAISS + BM25). Verify against
  `GET /api/status` (returns `total_chunks`).

## Maturity Notes

- API: input validation via Pydantic with strip-then-validate (whitespace-only
  → 422), 422 errors return `{error: "validation_error", request_id, detail}`.
  All responses include `x-request-id` and `x-response-time-ms` headers.
  `/api/chat` carries a `ChatResponse` model with `path: Literal["rag", "general",
  "translated", "cross-course", "graphrag"] | None` (forbidden extras) and
  optional `original_query` / `translated_query` / `general_reason` /
  `filter_empty` / `filter_low_quality` / `cross_course_origin` side fields.
- Smart routing (Round 2 #1+#2+#3): chat input goes through
  `router_intent.classify_input` — short / greeting / pure-punctuation queries
  skip RAG and go to a general GPT path; RAG results pass a score gate
  (`top1≥τ AND hits≥min_hits` OR `top1≥2τ AND hits≥1`); on 0-hit + course/query
  language mismatch (and not mixed) the query is translated once (5s budget,
  no retry) and the search is retried; still failing → cross-course fallback
  (search All Courses, surface results from a sibling course with a "本课无相关
  内容" annotation); finally → general path. Path decision surfaces as a chip
  in the assistant UI; topbar dropdown shows 🇨🇳/🇺🇸/🌐 per course.
- Real streaming (Round 2 #5): `/api/notes/stream` and `/api/report/stream`
  pipe `router.complete_stream` deltas straight through to NDJSON `chunk`
  events (codex `responses.create(stream=True)` → asyncio.Queue bridge from
  the executor thread); `/api/quiz/stream` keeps pseudo-streaming because
  partial JSON is unparseable mid-flight. First-token UX dropped from ~13s to
  <1.5s on codex GPT-5.5.
- Tool-calling agent (`/api/agent/stream`): multi-turn ReAct loop using
  `chat.completions(stream=True, tools=[...])`. Four built-in tools —
  `search_kb`, `read_chunk`, `list_courses`, `generate_note`. NDJSON event
  vocabulary: `{type: "text", delta}` / `{type: "tool_call", name,
  arguments, call_id}` / `{type: "tool_result", name, call_id, result}` /
  `{type: "done", answer, turns, max_turns_hit, budget_hit}` /
  `{type: "error", error, partial}`. Read-only tool calls in a single turn
  run via `asyncio.gather` (`run_tool_calls` partitioning); generate_note
  serializes. Hardening: course_id whitelisted against
  `orchestrator.list_courses()` in every tool, dedicated 2-worker
  `_agent_executor` (so agent runs can't starve notes/report/qa pool),
  bounded `Queue(maxsize=256)` with thread-side backpressure, cancellation
  via `threading.Event` + `stream.close()`, per-tool `asyncio.wait_for`
  timeout (30s read-only / 60s generate_note), aggregate
  `tool_result` budget (200KB → done.budget_hit=True), max_turns guard
  (default 8). Frontend rendering contract: `tool_result.result` and
  `error.partial` are untrusted text — render as `<pre>` / `<code>`,
  never as HTML or markdown.
- CJK fallbacks (Round 2 #6): all global font stacks
  (`--serif`/`--sans`/`--mono`) end with PingFang SC / Microsoft YaHei /
  Hiragino Sans GB / Noto Sans SC; long Chinese filenames in citation chips
  ellipsis-clip with hover-to-expand title.
- P0/P1 learning UX: frontend now exposes all six skills, clickable citations
  route to Reader highlights, notes are editable with Markdown/PDF export,
  quiz answers persist with stale detection and wrong-only review, mastery can
  launch targeted quiz practice, and session history is logged daily.
- Generation reliability: notes / quiz / report have NDJSON streaming endpoints,
  partial output retention, retry state helpers, and formatter cleanup; web
  research and formatter subagents are stateless and offline-testable.
- Knowledge graph: KG payloads carry depth / weight / source_chunks plus typed
  relations; frontend mind map uses weight/depth styling, detail panels, source
  links, pan/zoom/drag, collapse, and empty-state handling.
- Mind map M1+M2+M3 (2026-05-06): KG extraction is now two-stage — Stage A
  produces `course_overview` + 5–9 macro topics (one LLM call, sanitized name
  ≤80 chars / definition ≤300 chars, 15s `asyncio.wait_for` ceiling); Stage B
  per-chunk extraction injects topics so each concept declares a `parent_topic`.
  Output graph has explicit `concept_type="root"` (depth=0) + topic nodes
  (depth=1) + leaf concepts (depth≥2) connected by `part-of` edges (child →
  parent). Merger dedups by `(concept_type, normalized_name)` so a Stage A
  topic and a same-named Stage B leaf don't collapse. Frontend layout
  (`prepareMindmap`) is parent-aware recursive radial: slice ∝ subtree leaf
  count; root rendered as a course card; per-topic HSL hue inherited by
  descendants. The map is editable: dblclick edit, N add child, Del delete
  (root protected), shift+drag to connect with relation popup. Edits go to
  `POST /api/mindmap/{id}/edit` (per-course `asyncio.Lock` + atomic temp-file
  rename); persisted to `artifacts/courses/<id>/mindmap_edits.json` and
  replayed on every GET, so re-extraction never clobbers student work.
  Endpoint validates that `add_node.parent_id` / `add_edge` source+target
  exist in the graph and returns `op_results: [{op, status, reason}]` so the
  client can surface skipped ops; the frontend shows `● N op skipped` /
  `● save failed` chips in the toolbar instead of console-warn-only.
- Observability: `/api/status` includes backend, latency p50 samples, and cost
  fields; the frontend status bar displays degraded state if the backend is
  unavailable.
- Tests: run `pytest` — covers chunker, BM25/vector/hybrid search, FastAPI
  smoke, streaming endpoints, subagents, session log rotation, and frontend
  state helpers (no LLM keys or downloaded models required; uses deterministic
  hash-based fake embeddings and monkeypatched search/LLM paths).
- Mind map R4-3 (2026-05-11): default render is now d3-force directed
  graph via `prepareMindmapForce` (returns `{nodes, links, edges,
  relationTypes, rootId, empty}` — `edges` is a back-compat alias for
  `links`). Legacy radial `prepareMindmap` preserved as alias for
  `prepareMindmapTree`. d3-dispatch/d3-quadtree/d3-timer/d3-force loaded
  from `cdn.jsdelivr.net` at exact-version pins (SRI debt across all
  CDN scripts is a separate cleanup). Sim tick → `setSimNodes` is
  rAF-coalesced so 60Hz d3 ticks produce at most one React render per
  frame; `childrenByParent` `Map` replaces O(N²) parent filter walks in
  `visibleIds`. Drag interaction: when sim is live, drag writes only
  `fx`/`fy` (sim's pin), NOT the legacy `offsets` dict, so mouseup
  doesn't double-count the delta. `alphaTarget(0.2).restart()` fires
  once at mousedown (not per mousemove). Marker IDs (`kg-arrow-*`)
  scoped per-instance via `React.useId` so multiple `<MindMap>` mounts
  don't collide. Relation filter chips: per-component state preserves
  user's disabled selections across KG re-extracts (newcomer relations
  default to enabled; previously-disabled ones stay disabled). Empty
  KG returns `{links: [], edges: [], relationTypes: []}` on every path.
  All R3-3 affordances (dblclick edit / N add child / Del delete /
  shift+drag connect / alt+click NodeDeepDivePanel / commitOps) intact.
- Round 4 R4-1 + R4-2 (2026-05-10): direction switched to upload-only +
  KG-driven retrieval. `/api/courses` accepts `mode: Literal["all","user"] |
  None` defaulting to `"user"`; user mode filters `config.PRESET_COURSE_IDS`
  (the 8 hardcoded preset course ids — Round 1 ingest is **physically kept
  on disk** as a rollback hatch, only hidden in UI). Frontend `app.jsx` reads
  `?show_preset=1` to opt back into the all-courses view. Empty courses
  list renders an `.empty-courses-cta` upload prompt instead of a blank
  workspace. `/api/upload/{id}` rewritten as `StreamingResponse` of
  `application/x-ndjson`: pre-stream HTTPException 4xx for invalid files
  (50MB cap, suffix whitelist, zip-bomb check); then one or more
  `{type:"stage", stage, progress, detail?}` events per stage in order
  `chunking → embedding → kg_stage_a → kg_stage_b`; terminal `{type:"done",
  course_id, files, chunks, documents, kg_nodes, duration_ms}` or
  `{type:"error", error:"upload_pipeline_failed", stage}`. Stages run via
  `asyncio.to_thread` so the event loop stays responsive (chunking +
  embedding) or via an in-loop async task with a `_progress` callback
  that pushes to a bounded `asyncio.Queue(maxsize=64)` (KG extraction).
  Per-course pipeline lock `_UPLOAD_LOCKS[course_id]` (capped at 512
  entries with opportunistic eviction) serialises concurrent same-course
  uploads. `extract_from_chunks` gains an optional `progress_callback`
  kwarg; callback exceptions are caught + logged so a misbehaving
  telemetry hook can't abort the pipeline. Stage names live in
  `nano_notebooklm/kg/extractor.py` as `UPLOAD_STAGES` constant +
  `UploadStage` Literal — single source of truth across server, extractor,
  and `frontend/processing.jsx`. `frontend/processing.jsx` rewritten to
  render 4 progress bars driven by NDJSON events; retry button re-invokes
  the original upload via a `retryRef` closure (not just dismiss-modal).
- Round 4 R4-4 GraphRAG retriever (2026-05-11): `/api/chat` adds a fifth
  path `"graphrag"` that fires *before* BM25/vector RRF when the course has
  a `knowledge_graph.json`. New module `nano_notebooklm/kb/graph_search.py`
  loads the KG, embeds the query, ranks concept nodes by cosine
  (using cached `concept_embedding: list[float]` on each non-root Concept;
  lazy recompute on missing/dim-mismatch), takes top-k seeds, BFS-expands
  along part-of/depends-on/prerequisite_of edges to hop_limit, then collects
  reachable nodes' `source_chunks`, dedups by chunk_id, sorts by
  `(-hop_distance, seed_score, node_weight)`, joins `chunks.json` for text,
  and caps at `max_chunks=30`. `extract_from_chunks` gains an optional
  `embed_fn` kwarg so the upload pipeline + mindmap GET both seed
  `concept_embedding` once; legacy KGs without the field fall through to
  lazy per-query embedding. Compute is folded into Stage B's 100% emit
  (no `kg_stage_c` — preserves R4-2's 4-stage NDJSON upload contract).
  qa_skill's `_maybe_graphrag` runs the disk read + cosine pass via
  `asyncio.to_thread`; skipped when `course_filter` is None (All Courses)
  or `checked_files` is set (graphrag hop expansion can't honour per-file
  filtering). <2 hits → silent fall-through to existing RAG → translation
  → cross-course → general chain. Frontend chip: green `🕸️ 图检索` via
  `.path-chip.path-graphrag` oklch styling.
- R4-4 review-swarm fix-all v1 (2026-05-11): 4-route review caught a
  CRITICAL data-path bug + two HIGH performance regressions in the R4-4
  patch. Fixes: (A1) `KnowledgeGraph.add_concepts` now persists
  `concept_embedding` through its add_node kwargs and merge branch —
  before, networkx silently dropped the field on save so every chat hit
  the lazy-embed path despite the caching design. (A2) `extract_from_chunks`
  awaits `embed_fn(texts)` via `asyncio.to_thread` so R4-2's NDJSON queue
  drain stays responsive during the post-Stage-B embedding pass. (A3)
  graphrag admission now uses `router_intent.passes_score_gate` with a
  graphrag-specific cosine floor (`GRAPHRAG_SCORE_GATE_TOP1`, default
  0.15) instead of a permissive `len >= 2` check — prevents low-relevance
  queries from pre-empting RAG. (B4) graph_search resolves cache-miss
  node embeddings in a single batched `embed_fn(list)` call. (B5)
  Mindmap update_node ops drop the cached `concept_embedding` when name
  or definition changes so the next graph_search lazy-recomputes. (B6)
  `GRAPHRAG_ENABLED` env kill-switch lets operators disable graphrag
  globally without deleting KG files. (B7) FastAPI `@app.on_event("startup")`
  warms `kb.embed_fn` via `asyncio.to_thread` so the first /api/mindmap,
  /api/upload, or graphrag chat doesn't pay a 5-30s sentence-transformer
  model load on the request hot path. Adds 12 regression tests in
  tests/test_r4_4_fix_all_v1.py.
- R4-4 review-swarm fix-all v2 (2026-05-11): second review pass on
  fix-all v1 (commit 764276d) — no critical/high blocker. 10 medium
  fix-soon items land plus 4 quick low: (V1) `_graphrag_score_floor`
  clamps to [0, 1] (negative env value previously bypassed admission);
  STATUS.md fix-all v1 gains explicit `status: [review]` field;
  `test_router_intent.py` ChatResponse.path accept-list adds "graphrag";
  `.env.example` documents `GRAPHRAG_ENABLED` /
  `GRAPHRAG_SCORE_GATE_TOP1` / `NANO_NLM_DISABLE_EMBED_WARMUP`.
  (V2) startup warm-up switches to `asyncio.create_task(_do_warmup())`
  fire-and-forget so FastAPI accepts liveness probes during the model
  load (K8s no longer CrashLoopBackOff on 5-30s sentence-transformer
  init); `EMBEDDING_MODE=api` path skips warm-up entirely (no local
  model to load); new `app.state.embed_warm_ok` flag surfaced via
  `/api/status` `embed_warm_ok` field (None=in-flight/True=ok/False=
  failed). (V3) graphrag admission now passes `min_hits=1` to
  `passes_score_gate` explicitly so single-strong-hit small-course
  uploads aren't rejected by the RAG default of `min_hits=2`. (V4)
  `_resolve_node_embeddings` batch failure falls back to per-node
  `embed_fn([t])` so a poison-text outlier only loses itself, not the
  whole cache-miss list. (V5) log PII scrub: 3 sites drop
  `exc_info=True` (openai-python tracebacks carry request body =
  user query in API mode); `_load_kg` / `_load_chunks_index` log only
  `course_id` not absolute filesystem path. (V6) `graph_search._load_kg`
  applies a minimal `mindmap_edits.json` overlay for `delete_node` /
  `delete_edge` ops so student-deleted nodes no longer seed
  retrieval. Adds 16 regression tests in
  tests/test_r4_4_fix_all_v2.py.
- R4-4 fix-all v3 (2026-05-11, LOW backlog clean + real-behavior tests):
  closes 12 LOW items from v1/v2 review-swarms and upgrades 3 grep pins
  to true behavioral assertions. (T1) `test_extract_from_chunks_yields
  _event_loop_during_embed` proves A2's `asyncio.to_thread(embed_fn,
  texts)` actually off-loads — a concurrent ticker coroutine ticks ≥ 5
  times during a 100ms embed sleep. (T2) `test_startup_hook_fire_and
  _forget_does_not_block_status` proves B7's fire-and-forget — TestClient
  reaches /api/status in < 350ms despite a 400ms slow_embed warmup.
  (T3) `test_per_node_fallback_only_loses_poisoned_node` proves V4's
  per-node fallback: a POISON_BOMB sentinel node only loses itself, the
  other four still rank. (L4) `_maybe_graphrag` now wraps graph_search
  in `asyncio.wait_for(timeout=GRAPHRAG_TIMEOUT_SECONDS=10s)` so a
  stalled embed_fn falls through to RAG instead of hanging the chat.
  (L5) `graph_search._concept_embed_text` builds a real `Concept`
  instance instead of a duck-typed _Shim — signature drift in the
  extractor helper now raises ValidationError at construction rather
  than silently dropping every cache-miss node into a broad except.
  (L6) `_normalize_kg_nodes` strip of `concept_embedding` is now pinned
  by a positive isolation test so a future spread-operator refactor
  breaks loudly rather than silently shipping ~300KB float arrays per
  /api/mindmap request. (L7) `KnowledgeGraph.add_concepts` merge branch
  overwrites concept_embedding on dimension mismatch (operator switched
  EMBEDDING_MODE local→api → re-extract no longer stuck on stale 384d
  cache). (L8) end-to-end test pins the graphrag-zero → RAG → cross-
  course fallback chain (course A's empty KG + course B's strong match
  → path="cross-course" + cross_course_origin="courseB"). (L9) end-to-
  end test pins user_lang × graphrag — `Reply ONLY in zh` addendum lands
  in the system prompt even when the graphrag branch fires. (L10)
  `_graphrag_enabled` semantics inverted to fail-safe: only an explicit
  enable token (`1/true/yes/on/enabled`) keeps graphrag on, anything
  else (typos, unknown spellings) disables. (L11) `server.py:_warm
  _embed_fn` carries an attribution comment back-referencing commit
  764276d (R4-4 fix-all v1) + abce190 (v2), so `git blame` doesn't
  silently misattribute the design intent to R4-6's e60bca3.
  Adds 11 regression tests in tests/test_r4_4_fix_all_v3.py.
- Still missing for production: auth / multi-tenant, request rate limits,
  background-task ingestion, OpenAPI client codegen, structured metrics
  (Prometheus). Mastery is still read-only (KG editing landed in M3).
  R4-5 (Qwen-RAFT backend chip) is the only outstanding Round 4 P0.
