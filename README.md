# nano-NOTEBOOKLM

A self-hosted, open-source study assistant. Upload course PDFs / PPTX /
DOCX / Markdown → automatic knowledge graph + vector index → chat with
citations, structured LaTeX notes, practice quizzes, exam prep with a
self-evolving question bank, and an editable mind map.

Bring your own model: works with **OpenAI / DeepSeek / Moonshot / Zhipu /
MiniMax / Groq / Together / Anthropic Claude / Gemini**, or any local
runner that speaks OpenAI's `/v1/chat/completions` (**Ollama / vLLM /
LM Studio / llama.cpp**).

> Looking for a quickstart? See [Quick Start](#quick-start). Looking for
> how things fit together? See [`CLAUDE.md`](CLAUDE.md).

---

## Features

- **Chat with citations** — RAG (BM25 + FAISS + RRF) + a knowledge-graph
  retriever (concept-cosine seed + BFS hop expansion). Every answer links
  back to the source page in the built-in PDF reader.
- **LaTeX notes** — per-source-file streaming generation with a global
  review pass. KaTeX in the browser; optional `tectonic` compile to PDF.
- **Practice quizzes + Exam Prep** — generates questions, grades them,
  and **auto-generates variants of the ones you got wrong** so the bank
  grows in the directions you actually need.
- **Editable knowledge graph** — d3-force layout with relation filters,
  double-click edit, shift-drag to connect, "N" to add child, "Del" to
  remove. Edits persist as an overlay so re-extraction never clobbers
  your work.
- **Reader** — built-in PDF / PPTX preview, click any citation chip in a
  chat answer or note to jump to the exact page.
- **Background upload pipeline** — close the tab and come back; the
  ingest job keeps running.

---

## Architecture

```
[ React 18 (CDN, no build) ]
            ↓
[ FastAPI on :8000 ]
   ├── /api/chat     ── intent router → graphrag → RAG → translate → cross-course → general
   ├── /api/notes    ── per-file streaming LaTeX + review pass + tectonic PDF
   ├── /api/quiz     ── practice quiz generation
   ├── /api/exam-prep── self-evolving topic bank
   ├── /api/mindmap  ── KG read/write with edit overlay
   ├── /api/upload   ── background-task pipeline (chunking → embedding → KG)
   └── /api/status   ── available backends, embedding mode, health
            ↓
[ LLM router ] ─── openai / claude / local (any combination)
[ Embeddings ] ─── sentence-transformers (offline) or OpenAI-compatible API
[ Storage    ] ─── FAISS + BM25 + NetworkX KG  (under ./artifacts/)
```

Single-process, single-machine, no auth, no DB — designed for one user
or a small team running it on their own laptop / workstation.

---

## Quick Start

```bash
# 1. clone + install
git clone <repo-url> nano-notebooklm && cd nano-notebooklm
python -m venv .venv && source .venv/bin/activate
pip install -e ".[test]"

# 2. configure at least one LLM backend
cp .env.example .env
$EDITOR .env                  # set OPENAI_API_KEY (or ANTHROPIC_API_KEY, or LOCAL_LLM_*)

# 3. run
python api/server.py          # → http://localhost:8000
```

Open the browser, click "上传第一个文档", drop in a PDF, and you're done.

### Test the install

```bash
pytest                         # unit + API smoke tests; no LLM keys required
```

---

## Configuring an LLM backend

You need **at least one** of: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or
`LOCAL_LLM_BASE_URL + LOCAL_LLM_MODEL`. All three can coexist — the
frontend chip in the topbar cycles between them.

### Cloud providers (OpenAI-compatible)

Just set `OPENAI_BASE_URL` + `OPENAI_MODEL` to the provider's endpoint:

| Provider  | `OPENAI_BASE_URL`                                              | Suggested `OPENAI_MODEL`                    |
|-----------|----------------------------------------------------------------|---------------------------------------------|
| OpenAI    | `https://api.openai.com/v1`                                    | `gpt-4o-mini`                               |
| DeepSeek  | `https://api.deepseek.com/v1`                                  | `deepseek-chat`                             |
| Moonshot  | `https://api.moonshot.cn/v1`                                   | `moonshot-v1-8k`                            |
| Zhipu GLM | `https://open.bigmodel.cn/api/paas/v4`                         | `glm-4-flash`                               |
| MiniMax   | `https://api.minimax.chat/v1`                                  | `abab6.5-chat`                              |
| Groq      | `https://api.groq.com/openai/v1`                               | `llama-3.3-70b-versatile`                   |
| Together  | `https://api.together.xyz/v1`                                  | `meta-llama/Llama-3.3-70B-Instruct-Turbo`   |
| Gemini    | `https://generativelanguage.googleapis.com/v1beta/openai/`     | `gemini-2.0-flash`                          |

### Anthropic Claude

```bash
ANTHROPIC_API_KEY=sk-ant-...
CLAUDE_MODEL=claude-sonnet-4-5
```

### Local model

Run any OpenAI-compatible server on localhost, then:

```bash
# Ollama
LOCAL_LLM_BASE_URL=http://localhost:11434/v1
LOCAL_LLM_MODEL=qwen2.5:7b

# vLLM
LOCAL_LLM_BASE_URL=http://localhost:8000/v1
LOCAL_LLM_MODEL=Qwen/Qwen2.5-7B-Instruct

# LM Studio
LOCAL_LLM_BASE_URL=http://localhost:1234/v1
LOCAL_LLM_MODEL=<your loaded model name>
```

---

## Embeddings

```bash
EMBEDDING_MODE=local                                           # default
EMBEDDING_MODEL=paraphrase-multilingual-MiniLM-L12-v2          # offline, 50+ languages, CJK-friendly
```

Or switch to API-mode embeddings for higher quality cross-lingual
retrieval (costs money):

```bash
EMBEDDING_MODE=api
EMBEDDING_MODEL=text-embedding-3-small
# Optional — route embeddings to a different provider than chat
# EMBEDDING_API_KEY=...
# EMBEDDING_API_BASE_URL=https://api.openai.com/v1
```

---

## Main API endpoints

| Endpoint                                    | Purpose                                                                     |
|---------------------------------------------|-----------------------------------------------------------------------------|
| `POST /api/chat`                            | RAG + KG retrieval chat with citations and intent routing.                  |
| `POST /api/agent/stream`                    | Multi-turn tool-calling agent (NDJSON stream).                              |
| `POST /api/notes/full-course/stream`        | Per-file LaTeX note generation with review pass; incremental cache.         |
| `POST /api/quiz`                            | Practice quiz generation.                                                   |
| `POST /api/exam-prep/*`                     | Topic planning, question seeding, quiz draw, submit + auto-variant.        |
| `GET/POST /api/mindmap/{course_id}`         | Knowledge graph read; student edit ops.                                     |
| `POST /api/upload/{course_id}`              | Upload files; returns `{task_id, course_id}` immediately.                  |
| `GET  /api/upload/status/{task_id}`         | Poll background ingest progress (resume on tab reopen).                     |
| `GET  /api/status`                          | Configured backends, embedding mode, version, latency p50.                  |

Example:

```bash
curl -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"question": "What is a receptive field?", "course_id": null, "backend": "openai"}'
```

`backend` is optional — set it to `"openai"`, `"claude"`, or `"local"` to
override the default routing for a single call. Omit it to use whichever
one is configured as `DEFAULT_BACKEND`.

---

## Project layout

```
api/server.py            FastAPI entry point
frontend/                React 18 (CDN, no build), served statically
nano_notebooklm/
  ├── ai/                LLM router + openai/claude/local backends
  ├── ingest/            PDF/PPTX/DOCX extractors + chunking
  ├── kb/                FAISS + BM25 + RRF hybrid + graph search
  ├── kg/                Two-stage knowledge graph extraction
  ├── skills/            QA, notes, quiz, exam-prep, report, mastery
  └── orchestrator/      Skill routing, multi-turn agent loop, memory
scripts/                 ingest + index + embedding helpers
tests/                   pytest suite — runs offline, no LLM keys needed
artifacts/               (gitignored) per-course chunks, indices, KG, notes
```

---

## Development

```bash
pip install -e ".[test]"
pytest                         # unit + API smoke
pytest tests/test_api_smoke.py # quick subset
```

The frontend has no build step — it's React via the CDN and Babel
standalone. Just edit a `.jsx` file and refresh the browser.

---

## Production notes

nano-NOTEBOOKLM is designed for **single-user / small-team self-hosting**.
There is no authentication, no rate limiting, no multi-tenant isolation,
and no persistent task queue. If you expose it on the public internet:

- Put it behind a reverse proxy with HTTP basic auth (or OAuth).
- Disable `force=true` regen endpoints externally — they call the LLM
  on demand without per-IP throttling.
- Move `artifacts/` to a persistent volume.

---

## License

[MIT](LICENSE) — do what you want, just keep the copyright notice.

## Acknowledgements

- Inspired by Google's NotebookLM.
- Knowledge graph layout: [d3-force](https://github.com/d3/d3-force).
- PDF rendering: [PDFium](https://pdfium.googlesource.com/pdfium/) via
  PDF.js fallback. LaTeX → PDF via [tectonic](https://tectonic-typesetting.github.io/).
- Embeddings: [sentence-transformers](https://www.sbert.net/),
  multilingual MiniLM-L12-v2 default.
