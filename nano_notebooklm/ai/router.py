"""Model router: task-based routing, fallback, cost tracking."""

from __future__ import annotations

import asyncio
import logging

from nano_notebooklm import config
from nano_notebooklm.ai.base import LLMBackend
from nano_notebooklm.ai.claude_backend import ClaudeBackend
from nano_notebooklm.ai.openai_backend import OpenAIBackend
from nano_notebooklm.types import LLMResponse, TokenUsage

logger = logging.getLogger(__name__)


class ModelRouter:
    """Routes tasks to appropriate LLM backends with fallback and cost tracking.

    Backends are keyed by **provider id** (e.g. "openai-main", "claude-main",
    a user-added "openai-alt"), not by class family. The id space is
    defined by `artifacts/providers.json` (managed via the Settings UI);
    on first boot the file is synthesised from env so existing
    deployments keep working without operator action.
    """

    # fix-all v1 H5: legacy short-name → bootstrap-id alias. Used in two
    # places: `_active_default_id` (when env DEFAULT_BACKEND still says
    # "openai") and `get_backend` (when config.TASK_ROUTES values still
    # carry the old class-family names like "claude"). Pre-providers,
    # routing was by class family; after this rename, anyone with a
    # hand-edited TASK_ROUTES would silently get the wrong backend.
    _LEGACY_FAMILY_TO_ID = {
        "openai": "openai-main",
        "claude": "claude-main",
        "local": "local-main",
    }

    def __init__(self):
        self.backends: dict[str, LLMBackend] = {}
        self.usage = TokenUsage()
        # fix-all v1 H3: cache the resolved default_backend_id. Previously
        # `_active_default_id` (called per LLM dispatch + per /api/status)
        # re-read providers.json from disk each time. The cache invalidates
        # inside reload() (and external mutation endpoints invoke reload
        # under _PROVIDERS_LOCK, so hand-edits that bypass the API are
        # explicitly unsupported).
        self._default_id_cache: str | None = None
        self._init_backends()

    def _build_backend(self, row: dict, http_timeout: float | None = None) -> tuple[LLMBackend | None, str | None]:
        """Construct one backend instance from a providers.json row.

        Returns (instance, None) on success, (None, reason) on failure.
        The reason string is surfaced to operators via
        `_providers_payload.build_error` and `/api/providers/{id}/test`
        so a misconfigured row produces a visible diagnosis instead of
        a silent "this id was in the table but isn't in router.backends"
        dead-end.

        `http_timeout` (seconds) overrides the SDK / proxy default — set
        to a small value (e.g. 5s) by the /test endpoint so a stalled
        upstream can't pin executor workers for the full 600s ceiling.
        """
        ok, err = config._validate_provider_dict(row)
        if not ok:
            return None, f"row rejected: {err}"
        if not row.get("enabled", True):
            return None, "disabled"
        api_key = config._resolve_api_key_ref(row["api_key_ref"])
        if not api_key:
            return None, "api_key_ref resolves to empty (env var unset?)"
        kind = row["kind"]
        try:
            if kind == "anthropic":
                inst = ClaudeBackend(
                    api_key=api_key, model=row["model"], http_timeout=http_timeout,
                )
            elif kind in ("openai_compat", "openai_compat_local"):
                inst = OpenAIBackend(
                    api_key=api_key, base_url=row["base_url"], model=row["model"],
                    http_timeout=http_timeout,
                )
            else:
                return None, f"unknown kind {kind!r}"
        except Exception as exc:  # noqa: BLE001 — bad SDK init shouldn't poison the whole table
            return None, f"constructor raised {type(exc).__name__}"
        # Rename the instance so log lines + cross-review "alternate"
        # lookup track the provider id instead of the class family.
        inst.name = row["id"]
        # Stash the kind so callers (agent_stream, explain-node) can
        # filter for an openai-compat backend without inspecting class.
        inst.kind = kind
        return inst, None

    def diagnose_row(self, row: dict) -> str | None:
        """Return the reason a row would fail to register, or None if
        it would build successfully. Used by `_providers_payload` to
        surface `build_error` without actually constructing a backend
        (cheap — no httpx client allocation)."""
        ok, err = config._validate_provider_dict(row)
        if not ok:
            return f"row rejected: {err}"
        if not row.get("enabled", True):
            return "disabled"
        if not config._resolve_api_key_ref(row["api_key_ref"]):
            return "api_key_ref resolves to empty (env var unset?)"
        return None

    def _init_backends(self):
        # Bootstrap: first boot writes providers.json mirroring env so
        # subsequent edits go through the file. Idempotent — if the file
        # already exists, this is a no-op read.
        config.bootstrap_providers_from_env()
        data = config.load_providers()
        new: dict[str, LLMBackend] = {}
        for row in data.get("providers", []):
            rid = row.get("id")
            if not isinstance(rid, str) or not rid:
                continue
            inst, reason = self._build_backend(row)
            if inst is not None:
                new[rid] = inst
            else:
                logger.warning("provider %r not registered: %s", rid, reason)
        self.backends = new
        self._default_id_cache = None  # force recompute on next access
        if not self.backends:
            logger.warning(
                "No AI backends configured. Set at least one of "
                "OPENAI_API_KEY / ANTHROPIC_API_KEY / LOCAL_LLM_BASE_URL in .env, "
                "or add a provider via the Settings UI / POST /api/providers."
            )

    def reload(self) -> None:
        """Rebuild self.backends from the on-disk providers.json.

        Atomicity: builds the new dict in a local var first, then assigns
        in one statement. In-flight `complete()` / `complete_stream()`
        calls already hold their `backend_obj` from `_resolve_backend` on
        the stack — they keep running against the OLD instance (and its
        httpx client / executor / cancel-watcher resources), so a reload
        cannot tear down a request mid-flight. New requests after the
        assignment hit the new table.

        NOTE: explicitly do NOT close the old backend instances' httpx
        clients — the in-flight-request contract above depends on those
        connection pools staying alive until natural GC. Calling
        `.close()` here would break that contract.

        If `config.load_providers()` raises (it currently swallows all
        exceptions and falls back to env-derived defaults — but a future
        refactor might let it raise), the old `self.backends` is
        preserved because the failure propagates before the assignment.
        """
        data = config.load_providers()
        new: dict[str, LLMBackend] = {}
        for row in data.get("providers", []):
            rid = row.get("id")
            if not isinstance(rid, str) or not rid:
                continue
            inst, reason = self._build_backend(row)
            if inst is not None:
                new[rid] = inst
            else:
                logger.info("provider %r skipped on reload: %s", rid, reason)
        self.backends = new
        self._default_id_cache = None  # H3: drop cache so next call recomputes

    def _active_default_id(self) -> str:
        """Resolve the active default provider id.

        Order: (1) cached value (refreshed on reload());
        (2) providers.json:default_backend_id if still in self.backends;
        (3) legacy DEFAULT_BACKEND env mapped via _LEGACY_FAMILY_TO_ID;
        (4) first configured backend. Always returns an id present in
        self.backends (callers depend on this invariant).
        """
        if self._default_id_cache is not None and self._default_id_cache in self.backends:
            return self._default_id_cache
        data = config.load_providers()
        did = data.get("default_backend_id")
        if isinstance(did, str) and did in self.backends:
            self._default_id_cache = did
            return did
        mapped = self._LEGACY_FAMILY_TO_ID.get(config.DEFAULT_BACKEND)
        if mapped and mapped in self.backends:
            self._default_id_cache = mapped
            return mapped
        fallback = next(iter(self.backends), "")
        # Don't cache when no backends are configured — leaves room for
        # `reload()` to discover one without us serving a stale empty
        # string on the next call.
        if fallback:
            self._default_id_cache = fallback
        return fallback

    def get_openai_compat(self) -> LLMBackend | None:
        """Return an OpenAI-compatible backend for endpoints that hard-
        require chat-completions semantics (agent loop, explain-node).
        Prefers the active default if it's openai-compat; otherwise
        returns the first openai-compat entry. None if there isn't one.
        """
        did = self._active_default_id()
        if did and did in self.backends and getattr(self.backends[did], "kind", "") != "anthropic":
            return self.backends[did]
        for b in self.backends.values():
            if getattr(b, "kind", "") != "anthropic":
                return b
        return None

    def _resolve_backend(self, task_type: str, backend_override: str | None) -> LLMBackend:
        """Pick a backend for a single call.

        Explicit `backend_override` (e.g. ChatRequest.backend from the
        frontend chip) takes precedence over task-type routing. Unknown
        or unconfigured target → RuntimeError so the endpoint can
        translate to a 422.
        """
        if backend_override:
            if backend_override not in self.backends:
                raise RuntimeError(
                    f"backend {backend_override!r} not configured "
                    f"(available: {sorted(self.backends.keys())})"
                )
            return self.backends[backend_override]
        return self.get_backend(task_type)

    def get_backend(self, task_type: str = "") -> LLMBackend:
        """Get the appropriate backend for a task type."""
        default_id = self._active_default_id()
        target = config.TASK_ROUTES.get(task_type, default_id)
        # fix-all v1 H5: TASK_ROUTES values may still be legacy short
        # names ("claude", "local", "openai") from before the providers
        # rename. Translate to the bootstrap id so an old hand-edited
        # routing rule doesn't silently fall through to the wrong
        # backend via the next(iter(...)) fallback below.
        if target in self._LEGACY_FAMILY_TO_ID:
            mapped = self._LEGACY_FAMILY_TO_ID[target]
            if mapped in self.backends:
                target = mapped

        if target == "alternate":
            # fix-all v1 M3: cross-review "alternate" semantics. Pre-
            # providers, this branch returned the only non-default of the
            # 3 fixed class families — guaranteed a different vendor (e.g.
            # Claude when default = openai), which is the whole point of
            # cross-review. With dynamic providers, a naive "first id ≠
            # default" can pick another openai-compat row (same vendor,
            # different model) — defeats the prompt's purpose. Prefer a
            # different `kind` first, fall back to "any non-default" only
            # when there's no kind diversity.
            default_kind = getattr(self.backends.get(default_id), "kind", "") if default_id else ""
            for name, backend in self.backends.items():
                if name != default_id and getattr(backend, "kind", "") != default_kind:
                    return backend
            for name, backend in self.backends.items():
                if name != default_id:
                    return backend
            target = default_id

        if target in self.backends:
            return self.backends[target]

        # Fallback to whatever is available
        if self.backends:
            return next(iter(self.backends.values()))

        raise RuntimeError("No AI backend available. Configure API keys in .env")

    async def complete(
        self,
        prompt: str,
        task_type: str = "",
        system: str = "",
        temperature: float = 0.7,
        max_tokens: int = 4096,
        max_retries: int = 3,
        backend: str | None = None,
    ) -> LLMResponse:
        """Complete with automatic fallback and retry.

        When `backend` is explicitly pinned by the caller, cross-backend
        fallback is disabled — only same-backend retries happen. The
        caller is responsible for any upstream timeout/fallback chain.
        """
        backend_obj = self._resolve_backend(task_type, backend)
        last_error = None
        allow_fallback = backend is None

        for attempt in range(max_retries):
            try:
                resp = await backend_obj.complete(
                    prompt, system=system, temperature=temperature, max_tokens=max_tokens
                )
                self._track_usage(resp)
                return resp
            except Exception as e:
                last_error = e
                logger.warning(f"[{backend_obj.name}] attempt {attempt + 1} failed: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                    if allow_fallback and attempt == max_retries - 2:
                        fallback = self._get_fallback(backend_obj.name)
                        if fallback:
                            backend_obj = fallback
                            logger.info(f"Falling back to {backend_obj.name}")

        raise RuntimeError(f"All retries exhausted: {last_error}")

    async def complete_stream(
        self,
        prompt: str,
        task_type: str = "",
        system: str = "",
        temperature: float = 0.7,
        max_tokens: int = 4096,
        backend: str | None = None,
    ):
        """Stream content deltas. Routing matches `complete()`. No retry —
        once tokens have shipped, retrying would duplicate output. Backends
        without genuine streaming fall back to single-chunk yield via the
        default `LLMBackend.complete_stream` implementation.

        A trailing ``TruncationSignal`` may follow the last delta when
        the upstream stopped at max_output_tokens / finish_reason='length'.
        """
        backend_obj = self._resolve_backend(task_type, backend)
        async for item in backend_obj.complete_stream(
            prompt, system=system, temperature=temperature, max_tokens=max_tokens,
        ):
            yield item

    async def complete_structured(
        self,
        prompt: str,
        task_type: str = "",
        system: str = "",
        temperature: float = 0.3,
        max_tokens: int = 4096,
        backend: str | None = None,
    ) -> dict:
        backend_obj = self._resolve_backend(task_type, backend)
        result = await backend_obj.complete_structured(
            prompt, system=system, temperature=temperature, max_tokens=max_tokens
        )
        return result

    def _get_fallback(self, current_name: str) -> LLMBackend | None:
        for name, backend in self.backends.items():
            if name != current_name:
                return backend
        return None

    def _track_usage(self, resp: LLMResponse):
        self.usage.input_tokens += resp.input_tokens
        self.usage.output_tokens += resp.output_tokens

    def get_usage_summary(self) -> dict:
        return {
            "input_tokens": self.usage.input_tokens,
            "output_tokens": self.usage.output_tokens,
            "backends_available": list(self.backends.keys()),
        }
