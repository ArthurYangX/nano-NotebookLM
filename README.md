<div align="center">

# nano-NotebookLM

**A self-hosted, open-source study assistant — chat with citations, structured LaTeX notes, exam-prep with a self-evolving question bank, and an editable knowledge graph.**

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Tests](https://github.com/ArthurYangX/nano-NotebookLM/actions/workflows/test.yml/badge.svg)](https://github.com/ArthurYangX/nano-NotebookLM/actions/workflows/test.yml)
[![Code style](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

English | [简体中文](README.zh-CN.md)

<sub>Not affiliated with Google. NotebookLM is a trademark of Google LLC.</sub>

https://github.com/user-attachments/assets/39469b3a-fddf-4341-8671-39aae9775176

</div>

---

## Overview

Upload course PDFs / PPTX / DOCX / Markdown → automatic knowledge graph
+ vector index → chat with **page-accurate citations**, structured
LaTeX notes, practice quizzes, exam prep with a **self-evolving question
bank**, and an editable mind map.

Bring your own model: **DeepSeek · OpenAI · Anthropic Claude · Gemini
· and 11+ more** named cloud providers, or any local runner that speaks
OpenAI's `/v1/chat/completions` (**Ollama / vLLM / LM Studio /
llama.cpp**). See the [Provider matrix](#provider-matrix) for the full
list.

---

## Quick Start

```bash
# 1. clone + install (uv is ~10× faster than pip; either works)
git clone https://github.com/ArthurYangX/nano-NotebookLM && cd nano-NotebookLM
uv venv && source .venv/bin/activate && uv pip install -e ".[test]"
# or: python -m venv .venv && source .venv/bin/activate && pip install -e ".[test]"

# 2. configure — copy the template and fill AT LEAST ONE LLM key
#    (OPENAI_API_KEY, or ANTHROPIC_API_KEY, or LOCAL_LLM_BASE_URL+LOCAL_LLM_MODEL)
cp .env.example .env

# 3. run (Ctrl-C to stop; or use ./dev.sh below to manage as a background process)
python api/server.py
```

### → Open [`http://localhost:8000`](http://localhost:8000) in your browser

Then click the **⚙ Settings** gear (top-right of the topbar) to manage
LLM providers — add or swap a provider, switch the active default, or
hit **Test** for a 5-second connectivity ping. All without restarting
the server. The `.env` values you just filled in seed the first row;
once Settings exists, the UI is the source of truth (writes go to
`artifacts/providers.json`).

---

Prefer a managed lifecycle? `./dev.sh` wraps the same commands:

```bash
./dev.sh install   # creates .venv + installs deps + seeds .env
./dev.sh up        # starts the server in background, waits for /api/health
./dev.sh status    # pid + /api/status snapshot
./dev.sh logs      # tail /tmp/nano-nlm.log
./dev.sh down      # stops the server
```

> Install `uv` for ~10× faster setup:
> `curl -LsSf https://astral.sh/uv/install.sh | sh` — full docs at
> <https://docs.astral.sh/uv/getting-started/installation/>.
> Plain `pip` works identically if you'd rather skip it.

---

### Docker (no Python install needed)

```bash
git clone https://github.com/ArthurYangX/nano-NotebookLM && cd nano-NotebookLM
cp .env.example .env   # fill at least one LLM key
docker compose up      # → http://localhost:8000
```

Course data, KGs, notes, and sessions persist in `./artifacts/` on the
host. The default image is **~2 GB** — most of which is torch (CPU
build) + `sentence-transformers` for the local embedding option;
PDF extraction uses `pymupdf`. To bake MinerU OCR into the image
(heavy: ~5-7 GB total), build with `--build-arg WITH_MINERU=1`:

```bash
docker compose build --build-arg WITH_MINERU=1
docker compose up
```

**Want to host a public demo?** See
[`huggingface_space/DEPLOY.md`](huggingface_space/DEPLOY.md) for a
one-command push to HuggingFace Spaces (free CPU tier works).

#### Optional extras (only install what you need)

| What | Install | Without it |
|---|---|---|
| **MinerU** — OCR / layout-aware PDF extractor for scanned slides | `uv pip install -e ".[mineru]"` (heavy: ~3-5GB w/ models) | Upload `engine=mineru` returns an error; `engine=pymupdf` (default) still works |
| **tectonic** — LaTeX → PDF compiler for Notes export | `brew install tectonic` / `apt install tectonic` | "Export PDF" button is hidden; in-browser KaTeX preview still works |
| **LibreOffice** (`soffice`) — `.pptx` → PDF sidecar for MinerU | `brew install --cask libreoffice` / `apt install libreoffice` | `.pptx` with `engine=mineru` silently falls back to python-pptx (no OCR) |

GPU: MinerU auto-detects CUDA on Linux (1–2 s/page); Apple Silicon
defaults to CPU (~12 s/page) — auto-detect never picks MPS because the
pipeline backend hangs there. Override with
`MINERU_DEVICE_MODE=cuda|cpu|mps` in `.env`. **Escape hatch**: if CUDA
is detected but breaks at runtime (stale driver / OOM), set
`MINERU_DEVICE_MODE=cpu` to force CPU.

> **Network requirement.** The frontend loads React / KaTeX / d3-force
> / CodeMirror / IBM Plex fonts from public CDNs (jsdelivr, unpkg,
> esm.sh, Google Fonts) — first page-load needs internet. Fully
> air-gapped deployments will need to vendor those assets locally.

> Looking for architecture and code map? See [`CLAUDE.md`](CLAUDE.md).
> Want to contribute? See [`CONTRIBUTING.md`](CONTRIBUTING.md).

---

## Why nano-NotebookLM

| Capability                                | nano-NotebookLM   | [open-notebook](https://github.com/lfnovo/open-notebook) | Google NotebookLM | ChatGPT (upload PDF) |
|-------------------------------------------|:-----------------:|:-----------------:|:-----------------:|:--------------------:|
| Fully self-hosted, your data never leaves | ✅                | ✅                | ❌                | ❌                   |
| Bring your own LLM (cloud or local)       | ✅ 20+ providers (any OpenAI-compatible endpoint) | ✅ 18+ providers  | ❌ Gemini only    | ❌ OpenAI only       |
| **Page-accurate citations**, click to jump | ✅               | ⚠️ basic refs     | ✅                | ⚠️ inline quote only |
| Editable knowledge graph                  | ✅                | ❌                | ❌                | ❌                   |
| LaTeX notes (KaTeX + tectonic PDF)        | ✅                | ❌                | ❌                | ❌                   |
| Exam prep with self-evolving question bank | ✅               | ❌                | ❌                | ❌                   |
| Background upload pipeline, resumable     | ✅                | ⚠️ async, no stages | ✅              | ⚠️ session-bound     |
| Cross-course retrieval                    | ✅                | ⚠️ within-notebook only | ❌          | ❌                   |
| Podcast / multi-speaker audio generation  | ❌                | ✅                | ✅ (2-speaker)    | ❌                   |
| Audio / video / web-page ingestion        | 🔜 future (today: PDF/PPTX/DOCX/MD) | ✅            | ✅                | ⚠️ varies            |
| Cost at scale                             | Local GPU / API   | Local GPU / API   | Free tier capped  | Subscription         |

> **Choosing between nano-NotebookLM and open-notebook:** **open-notebook** is strong at multi-modal ingestion (audio / video / web) and podcast generation. **nano-NotebookLM** is strong at the deep-reading loop — page-accurate citations into a built-in PDF reader, an editable knowledge graph, LaTeX notes (KaTeX + tectonic PDF), and an exam-prep mode that grows a question bank around the topics you got wrong. Provider coverage is comparable — both are OpenAI-compatible, so any `/v1/chat/completions` endpoint plugs into either.

---

## Features

- **Chat with page-accurate citations** — RAG (BM25 + FAISS + RRF) +
  knowledge-graph retriever (concept-cosine seed + BFS hop expansion).
  Every answer links back to the source page in the built-in PDF reader.
- **LaTeX notes** — per-source-file streaming generation with a global
  review pass. KaTeX in the browser; optional `tectonic` compile to PDF.
- **Practice quizzes + Exam Prep** — generates questions, grades them,
  and **auto-generates variants of the ones you got wrong** so the bank
  grows in the directions you actually need.
- **Editable knowledge graph** — d3-force layout with relation filters,
  double-click edit, shift-drag to connect, `N` to add child, `Del` to
  remove. Edits persist as an overlay so re-extraction never clobbers
  your work.
- **Reader** — built-in PDF / PPTX preview, click any citation chip in a
  chat answer or note to jump to the exact page.
- **Background upload pipeline** — close the tab and come back; the
  ingest job keeps running.

### See it in action

<table>
<tr>
<td width="50%" valign="top">
  <img src="docs/screenshots/notes.png" alt="LaTeX Notes">
  <br><b>LaTeX Notes</b> — per-file streaming generation, KaTeX preview, click any citation chip to jump to the source page.
</td>
<td width="50%" valign="top">
  <img src="docs/screenshots/mindmap.png" alt="Knowledge Graph">
  <br><b>Knowledge Graph</b> — d3-force layout with relation filters (part-of / depends-on / related / example-of). Edit overlay survives re-extraction.
</td>
</tr>
<tr>
<td width="50%" valign="top">
  <img src="docs/screenshots/exam-prep.png" alt="Exam Prep">
  <br><b>Exam Prep</b> — topics weighted by mastery; wrong answers auto-spawn variant questions so the bank grows where you need it.
</td>
<td width="50%" valign="top">
  <img src="docs/screenshots/chat-citations.png" alt="Chat with cited sources">
  <br><b>Chat with cited sources</b> — every answer ends with chips like <code>ch3.pdf, Page 32/51</code> pointing at the exact source file and page; click to jump there. Bilingual sectioned format (课件覆盖 / 补充背景) makes it explicit which part came from your slides vs general background.
</td>
</tr>
</table>

---

## Provider matrix

| Provider              | Type             | `OPENAI_BASE_URL`                                              | Suggested model                              |
|-----------------------|------------------|----------------------------------------------------------------|----------------------------------------------|
| **[DeepSeek](https://api-docs.deepseek.com/) ★ (used during development)** | Cloud, compat | `https://api.deepseek.com/v1`                                  | `deepseek-v4-pro`                            |
| [OpenAI](https://platform.openai.com/docs/api-reference)               | Cloud, native    | `https://api.openai.com/v1`                                    | `gpt-4o-mini`                                |
| [Anthropic Claude](https://docs.anthropic.com/en/api/getting-started)  | Cloud, native    | *(uses Anthropic SDK)*                                         | `claude-sonnet-4-5`                          |
| [Moonshot (Kimi)](https://platform.moonshot.cn/docs)                   | Cloud, compat    | `https://api.moonshot.cn/v1`                                   | `moonshot-v1-8k`                             |
| [Zhipu GLM](https://docs.bigmodel.cn/)                                 | Cloud, compat    | `https://open.bigmodel.cn/api/paas/v4`                         | `glm-4-flash`                                |
| [MiniMax](https://www.minimax.io/platform_overview)                    | Cloud, compat    | `https://api.minimax.chat/v1`                                  | `abab6.5-chat`                               |
| [Groq](https://console.groq.com/docs/quickstart)                       | Cloud, compat    | `https://api.groq.com/openai/v1`                               | `llama-3.3-70b-versatile`                    |
| [Together](https://docs.together.ai/docs/openai-api-compatibility)     | Cloud, compat    | `https://api.together.xyz/v1`                                  | `meta-llama/Llama-3.3-70B-Instruct-Turbo`    |
| [Gemini (OpenAI mode)](https://ai.google.dev/gemini-api/docs/openai)   | Cloud, compat    | `https://generativelanguage.googleapis.com/v1beta/openai/`     | `gemini-2.0-flash`                           |
| [xAI (Grok)](https://docs.x.ai/docs/overview)                          | Cloud, compat    | `https://api.x.ai/v1`                                          | `grok-4`                                     |
| [OpenRouter](https://openrouter.ai/docs/quickstart)                    | Cloud, compat    | `https://openrouter.ai/api/v1`                                 | `openai/gpt-4o-mini` *(any `vendor/model` id)* |
| [Perplexity](https://docs.perplexity.ai/getting-started/quickstart)    | Cloud, compat    | `https://api.perplexity.ai` *(no `/v1`)*                       | `sonar-pro`                                  |
| [Mistral](https://docs.mistral.ai/api/)                                | Cloud, compat    | `https://api.mistral.ai/v1`                                    | `mistral-large-latest`                       |
| [DashScope (Qwen)](https://www.alibabacloud.com/help/en/model-studio/compatibility-of-openai-with-dashscope) | Cloud, compat | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` | `qwen-plus`                                  |
| [Fireworks](https://docs.fireworks.ai/getting-started/quickstart)      | Cloud, compat    | `https://api.fireworks.ai/inference/v1`                        | `accounts/fireworks/models/deepseek-v3p1`    |
| [SiliconFlow](https://docs.siliconflow.cn/en/userguide/quickstart)     | Cloud, compat    | `https://api.siliconflow.cn/v1`                                | `Qwen/Qwen2.5-72B-Instruct`                  |
| [Cerebras](https://inference-docs.cerebras.ai/introduction)            | Cloud, compat    | `https://api.cerebras.ai/v1`                                   | `llama-3.3-70b`                              |
| **[Ollama](https://ollama.com/library) ★**                             | Local            | `http://localhost:11434/v1`                                    | `qwen3:14b` *(suggested as open source model)*     |
| **[vLLM](https://docs.vllm.ai/en/latest/)**                            | Local            | `http://localhost:8001/v1` *(pick any free port; **not** 8000 — that's the app server)* | `Qwen/Qwen2.5-7B-Instruct`                   |
| **[LM Studio](https://lmstudio.ai/docs/app/api)**                      | Local            | `http://localhost:1234/v1`                                     | *(model loaded in LM Studio)*                |
| **[llama.cpp server](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)** | Local | `http://localhost:8080/v1`                                     | *(GGUF model loaded)*                        |

You can mix freely — add a cloud OpenAI key for high-quality KG
extraction, point chat at a local 7B for privacy, and the Settings UI
swaps between them per-task without restart.

### Editing providers from the UI (no restart)

`.env` is **just the first-boot seed**. On first start the server
synthesises `artifacts/providers.json` from your env vars and from then
on the Settings page is the source of truth: add a second
OpenAI-compatible endpoint, swap a model, set the active default, or
one-click **Test → 5s ping** — all without restarting. Keys can stay in
`.env` (`api_key_ref: env:VAR`) or be stored inline if you prefer
(`api_key_ref: literal:sk-…`; written 0o600, never echoed in any
response).

| Endpoint                                    | Purpose                              |
|---------------------------------------------|--------------------------------------|
| `GET    /api/providers`                     | List configured providers (redacted) |
| `PUT    /api/providers/{id}`                | Create or update                     |
| `DELETE /api/providers/{id}`                | Remove (refuses default / last row)  |
| `POST   /api/providers/{id}/test`           | 5-token ping with 5s timeout         |
| `POST   /api/providers/default`             | Switch active default                |

---

## Embeddings

Three switchable presets, picked from the Settings UI (no restart, no
destructive rebuild — each preset gets its own FAISS namespace under
`indices/faiss/<preset>/`, so toggling is a path-route):

| Preset         | Model                                            | Notes                                       |
|----------------|--------------------------------------------------|---------------------------------------------|
| `local_mini`   | `paraphrase-multilingual-MiniLM-L12-v2`          | Offline, 50+ languages, CJK-friendly (default) |
| `openai_large` | `text-embedding-3-large`                         | Best cross-lingual quality, costs money     |
| `bge_m3`       | `BAAI/bge-m3`                                    | Strong CJK + EN, runs locally (heavier)     |

The first switch to a never-used preset kicks off a one-shot background
rebuild of every course's index; the banner in the topbar tracks
progress. Switching back to an already-built preset is instant.

The env vars in `.env.example` (`EMBEDDING_MODE` / `EMBEDDING_MODEL` /
`EMBEDDING_API_*`) only seed the first-run default — once the Settings
preset is set, it wins.

---

## Main API endpoints

| Endpoint                                    | Purpose                                                                     |
|---------------------------------------------|-----------------------------------------------------------------------------|
| `POST /api/chat`                            | RAG + KG retrieval chat with citations and intent routing.                  |
| `POST /api/agent/stream`                    | Multi-turn tool-calling agent (NDJSON stream).                              |
| `POST /api/notes/full-course/stream`        | Per-file LaTeX note generation with review pass; incremental cache.         |
| `POST /api/quiz`                            | Practice quiz generation.                                                   |
| `POST /api/exam-prep/*`                     | Topic planning, question seeding, quiz draw, submit + auto-variant.         |
| `GET/POST /api/mindmap/{course_id}`         | Knowledge graph read; student edit ops.                                     |
| `POST /api/upload/{course_id}`              | Upload files; returns `{task_id, course_id}` immediately.                   |
| `GET  /api/upload/status/{task_id}`         | Poll background ingest progress (resume on tab reopen).                     |
| `GET  /api/status`                          | Configured backends, embedding mode, version, latency p50.                  |

Example:

```bash
curl -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"question": "What is a receptive field?", "course_id": null, "backend": "openai-main"}'
```

`backend` is optional — set it to any provider id from
`GET /api/providers` (e.g. `"openai-main"`, `"claude-main"`, or a
user-added `"openai-alt"`) to override the default routing for a single
call. Unknown ids fall back to the active default with a server-side
warn log, so a stale localStorage chip value won't 422 mid-conversation.

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
docs/screenshots/        README assets
```

---

## Development

```bash
uv pip install -e ".[test]"    # or: pip install -e ".[test]"
pytest                         # unit + API smoke; no LLM keys required
pytest tests/test_api_smoke.py # quick subset
```

The frontend has no build step — it's React via the CDN and Babel
standalone. Just edit a `.jsx` file and refresh the browser.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the contributor checklist
and [`CLAUDE.md`](CLAUDE.md) for the code-map / conventions.

---

## Roadmap

Recently shipped:

- Background upload pipeline with NDJSON stage events
- UI-managed provider matrix (add / swap / test without restart)
- Three-tier embedding presets (local MiniLM / OpenAI 3-large / BGE-M3)
- MinerU OCR ingest for scanned PDFs
- Multi-turn chat with history-aware query rewriting
- Selection-driven notes generation (per-file checkbox, incremental cache)
- Central i18n table (`frontend/i18n.js`) — full zh + en UI parity

Planned (issues welcome):

- Web URL as a source type (fetch + readable-content extraction, then through the existing chunk / embed / KG pipeline)
- Vite build option (opt-in, CDN stays default)
- Mastery-driven exam-prep difficulty curve
- Cross-course graph linking

---

## Production notes

nano-NotebookLM is designed for **single-user / small-team self-hosting**.
There is no authentication, no rate limiting, no multi-tenant isolation,
and no persistent task queue. If you expose it on the public internet:

- Put it behind a reverse proxy with HTTP basic auth (or OAuth).
- Disable `force=true` regen endpoints externally — they call the LLM
  on demand without per-IP throttling.
- Move `artifacts/` to a persistent volume.

---

## License

Released under the [Apache License 2.0](LICENSE). Free to use, modify,
and redistribute — including in commercial products — provided the
original copyright notice, license text, and `NOTICE` file are retained,
and any modified files are marked as changed. See [`NOTICE`](NOTICE) for
attribution requirements.

## Acknowledgements

- Inspired by Google's [NotebookLM](https://notebooklm.google.com/).
  **Not affiliated with Google.** NotebookLM is a trademark of Google LLC.
- Naming convention follows [`nanoGPT`](https://github.com/karpathy/nanoGPT)
  and [`nano-vLLM`](https://github.com/GeeeekExplorer/nano-vllm) —
  small, self-hosted, single-file-friendly homages.
- Knowledge graph layout: [d3-force](https://github.com/d3/d3-force).
- PDF rendering: [PyMuPDF](https://pymupdf.readthedocs.io/) for
  extraction, [PDF.js](https://mozilla.github.io/pdf.js/) in the browser.
  LaTeX → PDF via [tectonic](https://tectonic-typesetting.github.io/).
- Embeddings: [sentence-transformers](https://www.sbert.net/),
  multilingual MiniLM-L12-v2 default.
- OCR for scanned PDFs: [MinerU](https://github.com/opendatalab/MinerU).
