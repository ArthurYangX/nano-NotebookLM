---
title: nano-NotebookLM
emoji: 📚
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 8000
pinned: false
license: apache-2.0
short_description: Self-hosted study assistant — chat with citations, LaTeX notes, KG
---

# nano-NotebookLM

A self-hosted, open-source study assistant. Upload course PDFs / PPTX
/ DOCX / Markdown, get an automatic knowledge graph + vector index,
then chat with **page-accurate citations**, structured LaTeX notes,
practice quizzes, and an exam-prep mode with a **self-evolving question
bank**.

This Space runs the slim docker build — no MinerU OCR — so PDF
extraction is fast (`pymupdf`) but scanned slides fall back to
text-mode parsing.

- **Source code**: <https://github.com/ArthurYangX/nano-NotebookLM>
- **License**: Apache 2.0

## Configure your LLM key

Open the **⚙ Settings** gear in the top-right and add a provider. The
project supports OpenAI / Anthropic / DeepSeek / OpenRouter / xAI Grok
/ Perplexity / Mistral / any OpenAI-compatible endpoint, plus local
runners (Ollama / vLLM / LM Studio / llama.cpp).

Space owners can pre-seed a key by adding a **Repository secret** named
e.g. `OPENAI_API_KEY` + `OPENAI_BASE_URL` + `OPENAI_MODEL`. The first
boot picks those up; the Settings UI then becomes the source of truth.

## Persistence

Free Spaces have **ephemeral** storage — uploaded courses, KGs, and
notes are lost when the Space sleeps. For a persistent demo, upgrade
the Space to a paid tier with persistent storage and the `./artifacts`
directory will survive sleeps.
