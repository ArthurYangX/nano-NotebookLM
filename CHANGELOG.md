# Changelog

All notable changes to nano-NOTEBOOKLM are documented here. This project follows
[Semantic Versioning](https://semver.org/).

## [0.2.0] — 2026-05-21

### Added

- **Providers Matrix** — `artifacts/providers.json` is now the runtime
  source of truth for LLM provider configuration. The Settings UI can
  add, edit, delete, test (5-token ping), and set-as-default any
  provider without restarting the server. `.env` is now just the
  first-boot bootstrap seed.
  - New endpoints: `GET / PUT / DELETE /api/providers`,
    `POST /api/providers/{id}/test`, `POST /api/providers/default`.
  - `api_key_ref` indirection: `env:VAR` (resolved at backend-build) or
    `literal:...` (inline, file mode 0o600, never echoed in responses).
  - `ModelRouter.reload()` swaps backends atomically; in-flight requests
    keep running against their resolved instance.
  - SSRF guard rejects RFC1918 / link-local / metadata-service hosts on
    `base_url` for both `openai_compat` and `openai_compat_local`.
  - 32-row cap on net-new providers (existing-row updates always pass).
- **Truthful Upload Stages** — upload pipeline now emits
  `extracting → chunking → embedding → kg_stage_a → kg_stage_b` with
  truthful 0/100 boundaries; server.py owns the final `KG_STAGE_B=100`
  after `kg.save`. New named constants `EXTRACTING`, `KG_STAGE_A`,
  `KG_STAGE_B` in `nano_notebooklm.kg.extractor`.
- **i18n** — central `frontend/i18n.js` STRINGS table powers all
  Settings / Topbar / Course-picker copy in both `zh` and `en`.
- **CHANGELOG.md** — this file.

### Changed

- `OpenAIBackend` / `ClaudeBackend` accept `http_timeout` kwarg so
  `/api/providers/{id}/test` can pin the SDK to 5s instead of the
  600s default.
- `ChatRequest.backend` relaxed from
  `Literal["openai" | "claude" | "local"]` to free-form `str` — accepts
  any provider id. Unknown values no longer 422; they fall back to the
  active default with a warn log (fixes a cold-load race).
- `agent_stream` / `explain_mindmap_node` use `router.get_openai_compat()`
  instead of a hardcoded `router.backends.get("openai")` lookup.
- `/api/status` payload gains a redacted `providers:` field;
  `default_backend` now mirrors the runtime active provider id (was
  the stale env value).
- `kg/extractor.py` batches Stage B embedding calls
  (`KG_STAGE_B_EMBED_BATCH=32`) and caps internal progress at 95.
- `ingest/extractors.py` pre-checks `.pptx` zip envelopes before
  letting python-pptx near the XML (defends against billion-laughs / XXE).
- Frontend topbar chip now cycles the dynamic provider list pulled
  from `/api/status:providers`; was hardcoded to openai/claude/local.

### Security

- `save_providers` writes via `os.open(O_CREAT | O_EXCL | O_NOFOLLOW,
  0o600)` to close the umask 0o644 window.
- `_validate_provider_url` blocks AWS IMDS (169.254.169.254),
  GCP/Alibaba metadata endpoints, RFC1918, and link-local addresses on
  user-supplied `base_url`. Loopback (127.0.0.1 / localhost / ::1) is
  still allowed for self-hosted LLMs.
- `_validate_provider_dict` rejects `literal:` api_key_ref bodies that
  are empty, whitespace-only, contain control characters, or contain
  non-ASCII (defends against Authorization-header injection + RTL /
  homograph attacks).
- `/api/providers/{id}/test` failure responses no longer include the
  upstream exception body (`detail` field dropped); only `error_type`,
  `latency_ms`, and a build-time `reason` are exposed.
- Log lines for unknown `api_key_ref` schemes only emit the scheme
  prefix (`env:` / `literal:`), never the body.

### Removed

- `frontend/app.jsx`'s legacy "three fixed radios" backend chip
  rendering. Replaced by `<ProvidersMatrix>` in Settings.
- Hardcoded `openai_model` / `claude_model` / `local_llm_model` reads
  from the frontend; everything goes through the providers payload.

### Migration

Existing deployments need no action. The first server start after the
upgrade synthesises `artifacts/providers.json` from your current `.env`
values; from that point the Settings UI is the source of truth.
Deleting `artifacts/providers.json` and restarting re-seeds from env.

## [0.1.0] — 2026-05-20

### Added

- Initial open-source release.
- MIT license.
- Self-hosted single-user FastAPI + React 18 study assistant.
- Three-preset embedding switch with per-preset FAISS namespace.
- Background-task upload pipeline.
- Knowledge-graph driven retrieval + RAG + intent routing.
- LaTeX note streaming with review pass + optional tectonic PDF.
- Self-evolving exam prep with topic plan + variant generation.
- Editable mind map with d3-force layout.
