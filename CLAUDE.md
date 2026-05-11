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
  Global visual preferences (not per-course) use a flat `nano-nlm:v1:<kind>`
  key — currently `:backend` (codex / qwen_raft toggle), `:kg-legend-hidden`
  (Knowledge Graph bottom-right legend visibility). Any future global-pref
  key should follow the same flat shape and be listed here.

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
  route to Reader highlights, notes are editable with **LaTeX** + .tex / PDF
  export (Markdown export was removed in R4-6; the toolbar still has
  legacy `buildMarkdownExport`/`buildPdfPrintHtml` for backwards-compat
  but the Note path renders LaTeX via `frontend/latex-to-html.js`), quiz
  answers persist with stale detection and wrong-only review, mastery can
  launch targeted quiz practice, and session history is logged daily.
  **The mindmap tab is labelled "Knowledge Graph" in the UI** (R4-6
  rename) — internal endpoint `/api/mindmap/{id}` and the React component
  file `mindmap.jsx` keep their names.
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
- **Knowledge Graph trackpad gestures (2026-05-11):** wheel events on
  `.mindmap-wrap` are captured via a non-passive `addEventListener` (React's
  synthetic `onWheel` is passive by default — can't `preventDefault`).
  Capture is gated on `e.deltaMode === 0` (DOM_DELTA_PIXEL) so mouse-wheel
  + Windows ctrl+wheel page-zoom pass through unmodified; only macOS-style
  trackpad gestures hit the graph handler. `ctrlKey: true` (OS-synthesized
  pinch) → cursor-anchored zoom, range `[KG_ZOOM_MIN=0.3, KG_ZOOM_MAX=3]`
  (toolbar +/-/⟲ buttons share the same constants — no more "toolbar
  dead-zone" after pinch beyond 0.5-2). `ctrlKey: false` + non-zero delta
  → two-finger pan. Both branches `preventDefault()`. Wheel-active state
  via `isWheelingRef` + 150ms decay timer disables the 200ms transform
  transition so the cursor-anchor invariant stays visually stable during
  pinch. `closest('.mindmap-toolbar, .mindmap-legend, .mindmap-detail,
  .mindmap-legend-toggle, .mm-edge-picker')` early-return so wheel over
  the overlay UI doesn't accidentally zoom the canvas. NaN/Inf injection
  guarded via `Number.isFinite` on every computed zoom + pan.
- **KG legend hide-toggle (2026-05-11):** bottom-right legend has a `×`
  close button that collapses it to a `▤` pill at the same anchor;
  state persists in `localStorage["nano-nlm:v1:kg-legend-hidden"]`
  (global key, no course segment — same convention as the backend chip).
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
- Round 4 R4-5 Qwen-RAFT backend chip part 2 (2026-05-11): AutoDL
  Qwen2.5-7B-RAFT is now wired into /api/chat as a per-request optional
  second backend, alongside the codex GPT-5.4 main path. Part 1
  (commit d4e8b90) landed the `QwenRaftBackend(LLMBackend)` HTTP client
  + 19 unit tests; part 2 finishes the integration. Wiring:
  `ModelRouter._init_backends` now registers `qwen_raft` when
  `QWEN_RAFT_URL` is set; `_resolve_backend(task_type, backend_override)`
  maps the user-facing alias `"codex"` to the internal `"openai"`
  backend key. `ChatRequest` gains `backend: Literal["codex","qwen_raft"]
  | None = None`; `ChatResponse` gains `backend_fallback: bool | None =
  None` (extra=forbid preserved). `/api/chat` 422s when
  `backend="qwen_raft"` but `QWEN_RAFT_URL` is unset (a stale chip
  selection no longer silently degrades). qa_skill's new
  `_complete_with_backend_fallback` helper wraps `router.complete` in
  `asyncio.wait_for(timeout=QWEN_BACKEND_TIMEOUT_SECONDS=30s)` and
  silently degrades qwen→codex on timeout/exception, setting
  `data["backend_fallback"]=True` so the response can flag the
  degradation to the frontend. `/api/status` adds `qwen_raft_configured`
  + `qwen_raft_available` (health_check wrapped in 2s wait_for; broad
  except → status never 500s even when AutoDL host is flaky). Frontend
  topbar gets a `.backend-chip` next to the language chip — two
  variants (`.backend-codex` blue / `.backend-qwen` purple via oklch),
  disabled state ties to `/api/status` health, localStorage persists
  the selection across reloads. `Assistant` threads the `backend` prop
  into `API.chat(..., { userLang, backend })`. 9 new integration tests
  in tests/test_qwen_backend.py (28 total: 19 part 1 + 9 part 2) cover
  routing, 422 envelope, timeout fallback flag, status surface in both
  healthy and unavailable states, Literal rejection, schema contract,
  and the default-None routing case so the chip can't accidentally
  force every request through qwen. **Round 4 P0 (R4-1..R4-5) is now
  complete.**
- R4-5 part 2 review-swarm fix-all v1 (2026-05-11): first review-swarm
  on commit 6d2e590 surfaced 1 CRITICAL ("codex" alias hard-assumed
  OPENAI_API_KEY existed → claude-only / qwen-only deployments 500 on
  every chat) + 10 MEDIUM + 12 LOW. All 1+10 land plus 5 quick LOW; 21
  regression tests in tests/test_r4_5_part2_fix_all_v1.py. Key changes:
  (V1) `_complete_with_backend_fallback` treats `backend="codex"` as
  default task routing (same semantics as None) instead of pinning the
  internal "openai" backends key — the chip's "codex" label is the
  user-facing "use the configured main backend" not a hard openai pin.
  (V2) `/api/status` qwen health probe is now TTL-cached on
  `app.state.qwen_health_cache` (`QWEN_HEALTH_TTL_SECONDS` env, default
  15s) so the 10s frontend poll × N tabs doesn't trigger 6N req/min
  outbound to AutoDL; failure is cached too. (V3) `QWEN_RAFT_URL` is
  validated at config load — scheme must be http/https, host must not
  be a cloud-metadata endpoint (AWS / GCP / Aliyun IMDS); plaintext on
  non-loopback host warns. (V4) qwen path inside the helper uses
  `max_retries=1` (outer wait_for is the budget; router-level retries
  inside wait something for nothing), `except` narrows to
  `(QwenBackendError, RuntimeError, httpx.HTTPError)` (so a real
  programming bug surfaces as 500 instead of getting silently masked
  by `backend_fallback=True`), and log lines use
  `getattr(exc, "code", type(exc).__name__)` to avoid leaking prompt /
  URL via `str(exc)`. (V5) `QWEN_BACKEND_TIMEOUT_SECONDS` env override
  (default 30s) — operators can tune. (V6) frontend `useEffect` auto-
  rollbacks the chip selection to "codex" when /api/status reports
  qwen unavailable / unconfigured; status polling adds ±20% jitter so
  concurrent tabs don't pulse AutoDL in unison. (V7) `.env.example`
  documents 6 R4-5 env knobs. (V8) ChatRequest.backend and
  ChatResponse.backend_fallback get `Field(description=...)` so
  OpenAPI `/docs` surfaces the semantics; tests/test_r4_4_fix_all_v2's
  status_endpoint grep uses a sentinel slice instead of a magic char
  count (robust against future status_endpoint growth). Round 4 P0
  (R4-1 ~ R4-5 + 1 R4-5 fix-all v1) is now production-grade.
- **Round 4 R4-6 LaTeX Notes + incremental per-file cache (2026-05-11):**
  Notes are now LaTeX-only. The `/api/notes/full-course/stream` endpoint
  partitions per-source-file LLM calls (concurrency=4 default, capped at
  8; global `_FULL_COURSE_SEMAPHORE`=2 caps concurrent generations
  across the process) and emits NDJSON: `plan → file_start → (file_done
  | file_error | file_cached)* → merging → reviewing → review_chunk* →
  done | error`. `plan` carries `cached_count` / `fresh_count` / `force`
  + per-file `cached: bool`. `force: true` request body bypasses the
  cache entirely (UI: 🔄 button with `window.confirm` guard). The
  Reviewed pass always runs even when every file is cached, so new files
  fold into existing cross-refs.
  **Cache file** at `artifacts/courses/<id>/notes/per_file_cache.json`,
  shape `{"version": 1, "prompt_version": "<sha1[:8]>", "entries":
  {<source_file>: {chunk_hash, content, generated_at, model,
  prompt_version}}}`. Invalidation: SHA256 over `chunk_id + text` per
  capped chunk (catches re-upload + content drift) **and** sha1[:8] of
  `NOTE_FORMAT_LATEX + NOTE_GENERATION_PROMPT + NOTE_MERGE_REVIEW_PROMPT`
  concatenated (catches prompt-template edits — `_NOTE_PROMPT_VERSION`
  is computed at module import in `nano_notebooklm/skills/notes_full_course.py`).
  **Concurrency**: per-course `asyncio.Lock` keyed by `(loop_id,
  course_id)` around `write_cache_entry`'s load→mutate→save so two
  workers on the same course don't last-writer-wins each other's
  entries. **Security**: `plan_for_course` re-runs `latex_sanitizer.check()`
  on `entry["content"]` before serving — a tampered cache file can't
  ship `\write18`/`\input{}` to the client or tectonic. `load_cache`
  reads either v0 (bare dict) or v1 envelope; `save_cache` always
  writes v1.
  **Frontend rendering**: `frontend/latex-to-html.js` is a ~280-LOC
  whitelist shim covering exactly the macros `NOTE_FORMAT_LATEX`
  permits (`\section/\subsection/\textbf/\emph/\texttt/\cite` +
  `theorem/lemma/definition/example/remark/proof/itemize/enumerate/
  equation/align`). Stage 3 env stash uses **recursive descent** so
  nested envs (e.g. `\begin{proof}` inside `\begin{theorem}`) survive
  into envBuf with consistent ENV_n indices; Stage 8 loop expands until
  no placeholder remains. `renderInnerFragment` is inline-only (escape
  + inline macros + soft line breaks) — placeholders pass through to
  the outer Stage 9 / 10 / 11 sweep. `extractTOC` strips inline macros
  (`\texttt{X}` → `X`). Cite chip normalises `file:loc` → `file, loc`
  in the data-cite payload so `resolveCitationNavigation`'s split-on-
  `,` parser routes correctly.
  **Editor**: CodeMirror 6 via esm.sh ES modules with `stex` (LaTeX)
  syntax highlighting; gracefully falls back to `<textarea>` when CDN
  unreachable. Selection anchor preserved across remote-value updates
  unless editor has focus.
  **Export**: 3-way — `.tex` blob download, browser-print PDF (renders
  via KaTeX in a popped-out window), tectonic-compile PDF (`POST
  /api/notes/export/pdf` → `subprocess.run(["tectonic", ...])` inside
  `asyncio.to_thread` + global `Semaphore(2)` cap, configurable via
  `NANO_NLM_MAX_TECTONIC_CONCURRENCY`). PDF endpoint 503s when tectonic
  is absent; `/api/status` reports `tectonic_available: bool` so the
  frontend disables the button accordingly. PDF preamble uses
  `\providecommand{\cite}` (not `\renewcommand`) + `\IfFontExistsTF`
  fallback chain (PingFang SC → Noto Sans CJK SC → Source Han Sans SC)
  for cross-platform CJK compile. `latex_sanitizer.py` blacklists 19
  TeX primitives (`\input`/`\write18`/`\catcode`/`\def` etc) with 80KB
  cap; `check_unbounded()` variant for the merge/review pass body that
  legitimately exceeds 80KB.
  **localStorage key migration**: `notes:draft` → `notes-latex:draft`,
  legacy markdown drafts silently discarded on first read with a
  per-course one-time `console.info`. `nano-nlm:v1:backend` (no course
  segment) holds the global codex/qwen_raft backend chip preference.
- **Known LOW backlog (R4-6, NOT yet fixed):**
  - `concat_draft` + frontend `rebuildDraftFromFiles` both wrap each
    per-file body with `\section{<file>}` — if the LLM accidentally
    emits a leading `\section{}` of its own, the merged mid-stream
    draft has two stacked H2 lines. The review pass usually folds
    these out before `done`, so users rarely see it. Fix when polish
    needed: strip a leading `\section{...}` from per-file content
    before wrapping, or harden the per-file prompt.
  - `force=true` on `/api/notes/full-course/stream` is unauthenticated
    and burns ~$0.20-1.50 per call. Mitigated by `_FULL_COURSE_SEMAPHORE
    `=2 global cap + default CORS allow-list (`localhost` only) so
    externally-driven attacks are blocked, but local users + a future
    deployment surface need per-IP/per-user rate limit. Accepted in
    pre-user phase; add rate limit when auth lands.
- **Exam Prep — closed-loop self-evolving exam preparation (2026-05-11):**
  New skill `nano_notebooklm/skills/exam_prep.py` + 6 REST endpoints
  (`/api/exam-prep/plan|seed|quiz/next|quiz/submit` POST, `/api/exam-prep/
  {course_id}` GET+DELETE) + new frontend mode `exam-prep` mounted as
  `<ExamPrep>` in `frontend/exam-prep.jsx`. Replaces the disjoint
  Exam Analysis → Quiz → Mastery flow with a single state machine
  persisted at `artifacts/courses/<id>/exam_bank.json` (version=1
  envelope, atomic .json.tmp → rename writes).
  **Lifecycle (action dispatched on the skill via `params["action"]`):**
  `plan` runs a single LLM call (temperature=0.3) over up to 15 KB
  search hits → 5–8 exam-relevant topics with stable
  `topic_id = sha1(name.lower())[:10]`, weight ∈ [0,1], seed
  source_chunks. `seed` (or implicit on first `next_quiz` for an
  unseeded topic) fires per-topic LLM calls (concurrent via
  `asyncio.gather`) at temperature=0.6 to produce mixed-type questions
  (`multiple_choice` requires exactly 4 `["A. text"]` options + bare-
  letter `answer`; `short_answer` omits options). `next_quiz` samples
  non-mastered questions weighted by `topic.weight × (0.4 + 0.6 ×
  wrong_rate)`, with `max_per_topic = max(1, size/N + 1)` so a tiny
  topic-count quiz still has breadth. `submit` grades via
  `check_answer` (multi-choice = letter match using `_extract_letter`;
  short_answer = ≥3-char substring overlap either direction), pushes
  `{timestamp, user_answer, correct}` to each question's `history`,
  advances `consecutive_correct` (resets on wrong), flips
  `mastered=True` at `MASTERED_THRESHOLD=3` consecutive correct. Then
  the self-evolution pass: for each wrong-answered topic, one LLM call
  appends `variant_budget(wrong_topic_count)` fresh questions with
  `variant_of=<source_q_id>` provenance and alternating kind
  (multi-choice vs short-answer based on existing question count
  parity) so variants don't all share the same shape.
  **Variant budget** (`exam_prep.variant_budget`): `min(PER_TOPIC_CAP=5,
  max(1, TOTAL_VARIANT_CAP=20 // wrong_topic_count))`. So 1 wrong topic
  → 5 variants, 5 wrong → 4 each (20 total), 20+ wrong → 1 each. The
  PER_TOPIC_CAP prevents a single-topic miss from burning the entire
  20-call budget on near-identical variants.
  **Topic mastery** (`topic_mastery`): a topic is fully mastered when
  `total_questions ≥ TOPIC_MASTERY_MIN_QUESTIONS=3` AND
  `mastered_count / max(total, 3) ≥ TOPIC_MASTERY_RATIO=0.8`. Archived
  questions excluded from both numerator + denominator. The view payload
  exposed by `_compute_view` carries per-topic + overall ratios for the
  frontend progress page.
  **API surface:** GET / DELETE use `course_id: str` + manual
  `_validate_course_id_path()` so path-traversal returns the standard
  `{error, request_id, detail}` envelope at 400, not Pydantic's 422.
  POST endpoints use `ReqCourseId` body-field Annotated type. The
  submit endpoint caps `answers` at 50 entries and 2KB per value via a
  `field_validator` so a runaway client can't enqueue thousands of LLM
  calls in one submit. Session log records `kind=exam-prep-plan` and
  `kind=exam-prep-submit {wrong_topic_count, variants_added}`.
  **Frontend** (`frontend/exam-prep.jsx`): three internal views —
  `topics` (per-topic mastery progress card grid, "Start Mixed Quiz"
  + per-topic quiz CTA, mastered topics get a green chip + disabled
  CTA), `quiz` (mixed multi-choice + short-answer answer pane with a
  live answered-count bar), `result` (per-question green/red banner
  + variants-generated count badge so users see the bank growing).
  CSS lives in `frontend/styles.css` under `/* ── Exam Prep ── */`
  with oklch greens/reds consistent with the rest of the app. Wired
  into main nav via `tabs` array `{ id: "exam-prep", label: "Exam
  Prep", num: "★" }` and `{effectiveMode === "exam-prep" && <ExamPrep
  .../>}` in app.jsx; script tag registered before assistant.jsx in
  index.html.
  Tests: `tests/test_exam_prep.py` (26 — variant_budget math, mastery
  state transitions, check_answer letter+substring matching, bank
  load/save roundtrip + version mismatch recovery, plan reuses on
  second call but force regenerates, submit grades correctly + only
  wrong topics get variants + variant_of provenance recorded + 3
  consecutive correct flips mastered + wrong resets streak,
  reset wipes bank, unknown action surfaces error) +
  `tests/test_api_smoke.py` (4 new — empty-course view, traversal
  rejection, oversized-answers 422, no-topics 502).
  Known gaps: (a) `check_answer` for short_answer is substring-based
  not LLM-graded — fast and offline but accepts loose matches; an
  LLM-judge pass is a fix-later if false positives become a pattern;
  (b) variant generation is fire-and-forget within submit and adds
  ~3-8s latency proportional to wrong_topic_count — fine for
  N≤5 wrong but a 20-wrong-topic submit could feel slow (mitigated by
  the `min(5,…)` cap making max concurrent gen = 5); (c) `seed_questions`
  in `_generate_questions` falls back to `self.kb.search(topic.name)`
  when source_chunks are empty, so a topic with no chunk grounding can
  still produce questions but they may drift from course content;
  (d) the per-question-id `q_<uuid12>` IDs are random — stable enough
  for an exam bank lifetime, but if a future feature needs reproducible
  IDs across re-extracts we'd need to switch to content-hash IDs.
- Still missing for production: auth / multi-tenant, request rate limits,
  background-task ingestion, OpenAPI client codegen, structured metrics
  (Prometheus). Mastery is still read-only (KG editing landed in M3).
  The Exam Analysis / Mastery cards in the `Skills` tab are kept
  alongside Exam Prep as the older fire-once view — they may be
  retired once Exam Prep gains feature parity (per-topic drilldown,
  history visualisation).
