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
  ├── /api/notes          Structured note generation
  ├── /api/quiz           Practice quiz generation
  ├── /api/exam-analysis  Exam pattern analysis (JSON body)
  ├── /api/report         Course report generation
  ├── /api/mindmap/{id}   Knowledge graph extraction
  ├── /api/upload/{id}    File upload + indexing (50MB cap, whitelisted suffixes)
  ├── /api/memory         User memory persistence
  ├── /api/courses        Course listing
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
  └── orchestrator/ Skill routing, parallel execution, memory
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

- API: input validation via Pydantic, 422 errors return
  `{error: "validation_error", request_id, detail}`. All responses include
  `x-request-id` and `x-response-time-ms` headers.
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
- Observability: `/api/status` includes backend, latency p50 samples, and cost
  fields; the frontend status bar displays degraded state if the backend is
  unavailable.
- Tests: run `pytest` — covers chunker, BM25/vector/hybrid search, FastAPI
  smoke, streaming endpoints, subagents, session log rotation, and frontend
  state helpers (no LLM keys or downloaded models required; uses deterministic
  hash-based fake embeddings and monkeypatched search/LLM paths).
- Still missing for production: auth / multi-tenant, request rate limits,
  background-task ingestion, OpenAPI client codegen, structured metrics
  (Prometheus). Mastery / KG editing are read-only.
