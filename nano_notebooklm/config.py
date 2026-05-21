"""Central configuration for nano-NOTEBOOKLM."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS_DIR = Path(os.getenv("ARTIFACTS_DIR", str(PROJECT_ROOT / "artifacts")))

# ── AI backends ──────────────────────────────────────────────────────
# OpenAI-compatible provider. Works with OpenAI, DeepSeek, Moonshot,
# Together, Groq, Gemini (OpenAI-compat endpoint), any OneAPI-style
# proxy, etc. Just point OPENAI_BASE_URL at the provider's /v1.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# Anthropic Claude (native API).
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5")

# Local model via Ollama / vLLM / LM Studio / llama.cpp / etc. Any
# OpenAI-compatible /v1 endpoint works. LOCAL_LLM_API_KEY is sent as
# the Bearer token; most local servers ignore it but the OpenAI SDK
# requires a non-empty string.
import urllib.parse as _urllib_parse

_BLOCKED_HOSTNAMES = frozenset({
    # Cloud instance metadata services — supply-chain-tainted env
    # values like `LOCAL_LLM_BASE_URL=http://169.254.169.254/latest/...`
    # would otherwise let the openai SDK happily exfiltrate every
    # prompt to the metadata endpoint.
    "169.254.169.254",
    "metadata.google.internal",
    "metadata",
    "100.100.100.200",
})


def _validate_local_url(raw: str) -> str:
    """Sanity-check LOCAL_LLM_BASE_URL at config-load. Returns "" if
    invalid so router._init_backends skips LocalBackend registration.
    """
    raw = (raw or "").strip().rstrip("/")
    if not raw:
        return ""
    try:
        parsed = _urllib_parse.urlparse(raw)
    except Exception:
        logger.warning("LOCAL_LLM_BASE_URL=%r failed to parse; disabling local backend", raw)
        return ""
    if parsed.scheme not in ("http", "https"):
        logger.warning(
            "LOCAL_LLM_BASE_URL must be http or https (got %r); disabling local backend",
            parsed.scheme,
        )
        return ""
    host = (parsed.hostname or "").lower()
    if not host:
        logger.warning("LOCAL_LLM_BASE_URL=%r missing hostname; disabling local backend", raw)
        return ""
    if host in _BLOCKED_HOSTNAMES:
        logger.warning(
            "LOCAL_LLM_BASE_URL host %r is a cloud metadata endpoint; refusing", host,
        )
        return ""
    if parsed.scheme == "http" and host not in ("localhost", "127.0.0.1", "::1"):
        logger.warning(
            "LOCAL_LLM_BASE_URL uses http on non-loopback host %r; "
            "prompts will travel in plaintext", host,
        )
    return raw


import ipaddress as _ipaddress

_PROVIDER_URL_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _is_private_or_metadata_host(host: str) -> bool:
    """True if `host` is a known metadata endpoint or resolves (as a
    literal IP) to a loopback / link-local / RFC1918 range. DNS names
    are *not* resolved — DNS rebinding via providers.json is out of
    scope (single-user self-host threat model).
    """
    host = host.lower()
    if host in _BLOCKED_HOSTNAMES:
        return True
    try:
        ip = _ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_link_local or ip.is_private


def _validate_provider_url(raw: str) -> str:
    """SSRF guard for ANY user-supplied provider base_url (both
    `openai_compat` cloud and `openai_compat_local`). Returns the
    canonicalised URL (no trailing slash) or "" if rejected.

    Rules:
      - Scheme must be http or https.
      - Host must not be a known metadata endpoint (169.254.169.254,
        metadata.google.internal, …).
      - If the host parses as a literal private/link-local/loopback IP,
        only an explicit loopback alias (127.0.0.1 / ::1 / localhost)
        is allowed. RFC1918 ranges and link-local addresses are
        rejected — they would let `/api/providers/{id}/test` portscan
        the LAN.
      - HTTP on non-loopback emits a warning (plaintext prompts) but
        is permitted: some operators run a reverse proxy on an internal
        VLAN.

    This is the entry point used by `_validate_provider_dict`. The
    legacy `_validate_local_url` (used at env-load for
    LOCAL_LLM_BASE_URL) stays around for backward compatibility but
    new code paths should call this function.
    """
    raw = (raw or "").strip().rstrip("/")
    if not raw:
        return ""
    try:
        parsed = _urllib_parse.urlparse(raw)
    except Exception:
        logger.warning("provider base_url failed to parse")
        return ""
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        logger.warning("provider base_url scheme %r unsupported", scheme)
        return ""
    host = (parsed.hostname or "").lower()
    if not host:
        logger.warning("provider base_url missing hostname")
        return ""
    if host in _BLOCKED_HOSTNAMES:
        logger.warning(
            "provider base_url host %r is a cloud-metadata endpoint; rejected", host,
        )
        return ""
    # Literal-IP path: only loopback aliases pass. RFC1918 / link-local
    # are rejected to prevent the /test endpoint from being a LAN
    # portscanner. DNS names are not resolved (single-user threat model
    # accepts DNS rebinding risk).
    if host not in _PROVIDER_URL_LOOPBACK_HOSTS and _is_private_or_metadata_host(host):
        logger.warning(
            "provider base_url host %r is private/link-local; rejected "
            "(use 127.0.0.1 / localhost for self-hosted LLMs)", host,
        )
        return ""
    if scheme == "http" and host not in _PROVIDER_URL_LOOPBACK_HOSTS:
        logger.warning(
            "provider base_url uses http on non-loopback host %r; "
            "prompts will travel in plaintext", host,
        )
    return raw


LOCAL_LLM_BASE_URL = _validate_local_url(os.getenv("LOCAL_LLM_BASE_URL", ""))
LOCAL_LLM_MODEL = os.getenv("LOCAL_LLM_MODEL", "")
LOCAL_LLM_API_KEY = os.getenv("LOCAL_LLM_API_KEY", "local")

# Default backend when a task doesn't specify one: openai | claude | local
DEFAULT_BACKEND = os.getenv("DEFAULT_BACKEND", "openai")

# Embeddings can run on a separate OpenAI-compatible endpoint (e.g.
# main chat goes to a provider without /v1/embeddings, embeddings go
# to OpenAI). Falls back to the main OPENAI_* settings.
EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY", "") or OPENAI_API_KEY
EMBEDDING_API_BASE_URL = os.getenv("EMBEDDING_API_BASE_URL", "") or OPENAI_BASE_URL

# ── Embedding ────────────────────────────────────────────────────────
# - "local" → sentence-transformers (offline, downloads model on first
#   use). Default multilingual MiniLM handles 50+ languages incl. CJK.
# - "api"   → OpenAI-compatible /v1/embeddings endpoint.
#
# Three user-selectable presets are surfaced in the Settings UI. The active
# preset writes its (mode, model) pair into a small JSON preference file
# under ARTIFACTS_DIR so the choice survives restart without env edits.
# Indices are kept in per-preset namespaces (kb/store.py → indices/faiss/
# <preset_id>/...) so toggling between presets is a cheap path-route, not
# a destructive rebuild — switching back to a previously-used preset is
# instant.
EMBEDDING_PRESETS: dict[str, dict] = {
    "local_mini": {
        "label": "Local MiniLM",
        "description": "本地 sentence-transformers · 多语言 · 0 配置",
        "mode": "local",
        "model": "paraphrase-multilingual-MiniLM-L12-v2",
        "dim": 384,
        "requires_api_key": False,
        "download_size_mb": 470,
    },
    "openai_large": {
        "label": "OpenAI API",
        "description": "OpenAI 兼容 /v1/embeddings · text-embedding-3-large · 需要 API key",
        "mode": "api",
        "model": "text-embedding-3-large",
        "dim": 3072,
        "requires_api_key": True,
        "download_size_mb": 0,
    },
    "bge_m3": {
        "label": "BGE-M3",
        "description": "BAAI/bge-m3 本地多语言强力模型 · 首次下载 ~2GB",
        "mode": "local",
        "model": "BAAI/bge-m3",
        "dim": 1024,
        "requires_api_key": False,
        "download_size_mb": 2200,
    },
}

EMBEDDING_PREFERENCE_FILE = ARTIFACTS_DIR / "embedding_preference.json"


def _load_embedding_preference() -> str | None:
    """Return the preset_id persisted in the preference file, or None.

    Defensive: missing file / unreadable / unknown preset all return None
    so the caller falls back to env-derived defaults.
    """
    try:
        if not EMBEDDING_PREFERENCE_FILE.exists():
            return None
        data = json.loads(EMBEDDING_PREFERENCE_FILE.read_text())
        if not isinstance(data, dict):
            return None
        pid = data.get("preset_id")
        if pid in EMBEDDING_PRESETS:
            return pid
    except Exception as exc:  # noqa: BLE001 — preference file is best-effort
        logger.warning(
            "embedding preference file unreadable (%s); falling back to env defaults",
            type(exc).__name__,
        )
    return None


def save_embedding_preference(preset_id: str) -> None:
    """Persist preset choice AND mutate module-level EMBEDDING_MODE/MODEL
    so subsequent reads (kb.store, /api/status) see the new values without
    a process restart.
    """
    if preset_id not in EMBEDDING_PRESETS:
        raise ValueError(f"unknown embedding preset: {preset_id!r}")
    EMBEDDING_PREFERENCE_FILE.parent.mkdir(parents=True, exist_ok=True)
    EMBEDDING_PREFERENCE_FILE.write_text(
        json.dumps({"preset_id": preset_id}, ensure_ascii=False, indent=2)
    )
    preset = EMBEDDING_PRESETS[preset_id]
    global EMBEDDING_MODE, EMBEDDING_MODEL
    EMBEDDING_MODE = preset["mode"]
    EMBEDDING_MODEL = preset["model"]


def active_preset_id() -> str:
    """Return the preset_id whose (mode, model) matches current config.
    Returns "custom" when the operator overrode EMBEDDING_MODEL via env to
    a value that doesn't match any preset — the UI then shows the active
    radio as "未选择（自定义 env 配置）" and switches are still allowed.
    """
    for pid, p in EMBEDDING_PRESETS.items():
        if p["mode"] == EMBEDDING_MODE and p["model"] == EMBEDDING_MODEL:
            return pid
    return "custom"


_env_embedding_mode = os.getenv("EMBEDDING_MODE", "local")
_env_embedding_model = os.getenv("EMBEDDING_MODEL", "")
_pref_preset_id = _load_embedding_preference()
if _pref_preset_id is not None:
    _pref_preset = EMBEDDING_PRESETS[_pref_preset_id]
    EMBEDDING_MODE = _pref_preset["mode"]
    EMBEDDING_MODEL = _pref_preset["model"]
else:
    EMBEDDING_MODE = _env_embedding_mode
    EMBEDDING_MODEL = _env_embedding_model or (
        "text-embedding-3-small" if EMBEDDING_MODE == "api"
        else "paraphrase-multilingual-MiniLM-L12-v2"
    )

# ── Chunking defaults ────────────────────────────────────────────────
CHUNK_SIZE_TOKENS = 512
CHUNK_OVERLAP_TOKENS = 64
MIN_CHUNK_TOKENS = 50

# ── Search defaults ──────────────────────────────────────────────────
DEFAULT_TOP_K = 5
RRF_K = 60  # Reciprocal Rank Fusion constant

# ── Orchestrator ─────────────────────────────────────────────────────
MAX_PARALLEL_WORKERS = 4
CHECKPOINT_MODE = os.getenv("CHECKPOINT_MODE", "auto")


# ── Providers (UI-managed) ──────────────────────────────────────────
# Replaces the env-only "register OpenAI iff OPENAI_API_KEY else Claude
# iff ANTHROPIC_API_KEY else Local" path with a JSON config the frontend
# can edit at runtime. The file lives under ARTIFACTS_DIR so it survives
# restarts; on first boot we synthesise it from env so existing
# deployments keep working without operator action.
#
# Schema:
#   {
#     "version": 1,
#     "providers": [
#       {
#         "id": "openai-main",                 # unique, used as router key
#         "kind": "openai_compat",             # "openai_compat" | "openai_compat_local" | "anthropic"
#         "label": "OpenAI",
#         "base_url": "https://api.openai.com/v1",  # required for openai_compat*, ignored for anthropic
#         "api_key_ref": "env:OPENAI_API_KEY", # "env:VAR_NAME" or "literal:<value>"
#         "model": "gpt-4o-mini",
#         "enabled": true,
#       }
#     ],
#     "default_backend_id": "openai-main"
#   }
PROVIDERS_FILE = ARTIFACTS_DIR / "providers.json"
PROVIDERS_SCHEMA_VERSION = 1
PROVIDER_KINDS = ("openai_compat", "openai_compat_local", "anthropic")


def _resolve_api_key_ref(ref: str) -> str:
    """Resolve an `api_key_ref` to its concrete value.

    `env:VAR_NAME` reads os.environ at call time (so an operator can
    rotate the underlying secret via `.env` + restart without touching
    providers.json). `literal:...` is the inline value. Anything else is
    logged + treated as empty so the router skips the row instead of
    feeding a garbage key to the SDK.
    """
    if not ref:
        return ""
    if ref.startswith("env:"):
        return os.getenv(ref[4:], "")
    if ref.startswith("literal:"):
        return ref[len("literal:"):]
    # fix-all v1 H6: never log the ref body. The earlier version sliced
    # ref[:16] which on a `literal:sk-AB...` ref leaks the first ~8 bytes
    # of an api key into structured logging (Datadog/Loki/journalctl).
    # We log only the scheme prefix so an operator can still diagnose
    # "wrong scheme" misconfigurations.
    scheme = ref.split(":", 1)[0] if ":" in ref else "<no-colon>"
    logger.warning(
        "unknown api_key_ref scheme %s; provider row will be skipped "
        "(must start with 'env:' or 'literal:')", scheme,
    )
    return ""


def _validate_provider_dict(d: dict) -> tuple[bool, str]:
    """Schema-check a single provider entry. Returns (ok, error_message).

    For openai_compat_local entries, base_url is routed through the
    existing SSRF guard (_validate_local_url) so a user-added local
    provider can't be coerced into hitting cloud metadata endpoints.
    """
    if not isinstance(d, dict):
        return False, "entry must be an object"
    required_str = ("id", "kind", "label", "model", "api_key_ref")
    for k in required_str:
        if not isinstance(d.get(k), str) or not d[k]:
            return False, f"missing or empty string field: {k!r}"
    if d["kind"] not in PROVIDER_KINDS:
        return False, f"kind must be one of {PROVIDER_KINDS}"
    if d["kind"] in ("openai_compat", "openai_compat_local"):
        bu = d.get("base_url")
        if not isinstance(bu, str) or not bu:
            return False, "base_url required for openai_compat[_local]"
        # SSRF guard applies to BOTH kinds. fix-all v1 H1: previously
        # only `openai_compat_local` ran the guard, leaving
        # `kind=openai_compat` as an unauthenticated portscan + AWS-IMDS
        # exfil primitive via /api/providers/{id}/test. The unified
        # validator blocks metadata hosts + RFC1918 / link-local IPs
        # for both, while still allowing public DNS names and loopback.
        validated = _validate_provider_url(bu)
        if not validated:
            return False, f"base_url {bu!r} rejected by SSRF guard"
    if not isinstance(d.get("enabled", True), bool):
        return False, "enabled must be bool"
    ref = d["api_key_ref"]
    if not (ref.startswith("env:") or ref.startswith("literal:")):
        return False, "api_key_ref must start with 'env:' or 'literal:'"
    # fix-all v1 M8: validate literal: content. An inline secret like
    # `literal:sk-...\n` or `literal:` (empty) used to silently slip
    # through PUT and only fail at backend-build time. Reject control
    # chars (would inject into Authorization header / corrupt logs)
    # and empty-after-prefix at the validation layer so the operator
    # sees a 422 immediately.
    if ref.startswith("literal:"):
        body = ref[len("literal:"):]
        if not body or not body.strip():
            return False, "literal: api_key_ref body is empty"
        if any(ord(c) < 0x20 or ord(c) == 0x7F for c in body):
            return False, "literal: api_key_ref contains control characters"
        # Loose printable-ASCII gate. Real API keys are ASCII; rejecting
        # unicode here also stops a class of homograph / RTL-override
        # injection from a hostile localStorage value.
        if any(ord(c) > 0x7E for c in body):
            return False, "literal: api_key_ref must be printable ASCII"
    return True, ""


def _default_providers_from_env() -> dict:
    """Synthesise a providers config that mirrors what the legacy
    env-only `router._init_backends` would have built today. Used for
    first-boot bootstrap and as a fallback when providers.json is
    deleted or corrupt. api_key_ref always uses env:VAR so secrets stay
    out of the file even on first write.
    """
    providers: list[dict] = []
    if OPENAI_API_KEY:
        providers.append({
            "id": "openai-main",
            "kind": "openai_compat",
            "label": "OpenAI",
            "base_url": OPENAI_BASE_URL,
            "api_key_ref": "env:OPENAI_API_KEY",
            "model": OPENAI_MODEL,
            "enabled": True,
        })
    if ANTHROPIC_API_KEY:
        providers.append({
            "id": "claude-main",
            "kind": "anthropic",
            "label": "Anthropic Claude",
            "base_url": None,
            "api_key_ref": "env:ANTHROPIC_API_KEY",
            "model": CLAUDE_MODEL,
            "enabled": True,
        })
    if LOCAL_LLM_BASE_URL and LOCAL_LLM_MODEL:
        providers.append({
            "id": "local-main",
            "kind": "openai_compat_local",
            "label": "Local model",
            "base_url": LOCAL_LLM_BASE_URL,
            "api_key_ref": "env:LOCAL_LLM_API_KEY",
            "model": LOCAL_LLM_MODEL,
            "enabled": True,
        })
    # default_backend_id: prefer DEFAULT_BACKEND env mapping, else first row.
    env_map = {"openai": "openai-main", "claude": "claude-main", "local": "local-main"}
    desired = env_map.get(DEFAULT_BACKEND)
    default_id = desired if any(p["id"] == desired for p in providers) else (
        providers[0]["id"] if providers else None
    )
    return {
        "version": PROVIDERS_SCHEMA_VERSION,
        "providers": providers,
        "default_backend_id": default_id,
    }


def load_providers() -> dict:
    """Read providers.json. Returns the synthesised env-default config
    if the file is missing or unreadable — caller decides whether to
    persist that synthesised view back to disk (bootstrap).
    """
    # fix-all v2 L7: refuse to read through a symlink. Threat model is
    # single-user self-host, so this matters only if `artifacts/` is on
    # a writable shared volume — but the cost is one lstat and avoids
    # an "attacker writes providers.json → /Users/$USER/.ssh/id_ed25519"
    # disclosure via `/api/providers` (the file body would JSON-fail
    # immediately, but the read still happens).
    if not PROVIDERS_FILE.exists():
        return _default_providers_from_env()
    try:
        if PROVIDERS_FILE.is_symlink():
            logger.error(
                "providers.json at %s is a symlink; refusing to follow. "
                "Remove the link and re-bootstrap.", PROVIDERS_FILE,
            )
            return _default_providers_from_env()
    except OSError as exc:
        logger.warning(
            "providers.json lstat failed (%s); falling back to env defaults",
            type(exc).__name__,
        )
        return _default_providers_from_env()
    try:
        data = json.loads(PROVIDERS_FILE.read_text())
    except Exception as exc:  # noqa: BLE001 — corrupt file is best-effort
        logger.warning(
            "providers.json unreadable (%s); falling back to env-derived defaults",
            type(exc).__name__,
        )
        return _default_providers_from_env()
    if not isinstance(data, dict):
        return _default_providers_from_env()
    # fix-all v1 M6: version mismatch handling. A v1 server loading a
    # file written by a future v2+ server might misinterpret new fields
    # silently. We accept v1 (current) and treat anything higher as
    # "fall back to env defaults" — operator sees a warning instead of
    # subtle field stripping.
    file_version = data.get("version")
    if isinstance(file_version, int) and file_version > PROVIDERS_SCHEMA_VERSION:
        logger.warning(
            "providers.json version=%d is newer than this server's "
            "PROVIDERS_SCHEMA_VERSION=%d; falling back to env defaults to "
            "avoid silent field-shape drift. Roll forward the server or "
            "downgrade the file.", file_version, PROVIDERS_SCHEMA_VERSION,
        )
        return _default_providers_from_env()
    data.setdefault("version", PROVIDERS_SCHEMA_VERSION)
    data.setdefault("providers", [])
    data.setdefault("default_backend_id", None)
    if not isinstance(data["providers"], list):
        data["providers"] = []
    # fix-all v1 L5: dedupe by id. A hand-edited providers.json with
    # two rows sharing the same id used to silently last-write-wins
    # into `router.backends` while the UI showed BOTH — leading to
    # "I edited row A but Test green came from row B" confusion. Keep
    # the LAST occurrence (matches dict-last-wins semantics of the
    # downstream router build).
    seen_ids: set[str] = set()
    deduped: list[dict] = []
    for row in reversed(data["providers"]):
        rid = row.get("id") if isinstance(row, dict) else None
        if not isinstance(rid, str) or rid in seen_ids:
            if isinstance(rid, str) and rid in seen_ids:
                logger.warning(
                    "providers.json contains duplicate id %r; keeping the "
                    "last occurrence and discarding the earlier one(s)", rid,
                )
            continue
        seen_ids.add(rid)
        deduped.append(row)
    deduped.reverse()  # restore original ordering for the surviving rows
    data["providers"] = deduped
    return data


def save_providers(data: dict) -> None:
    """Persist providers.json atomically with 0o600 perms.

    Atomicity (tmp + os.replace) matters because a half-written file
    would survive across restarts and zero-out every backend — losing
    LLM access without an env fallback is worse than a stale config.

    fix-all v2 L1: open the tmp file with `os.open(...,
    O_CREAT | O_EXCL | O_WRONLY, 0o600)` so the file's perms are
    correct from inode birth. The previous `tmp.write_text(...)` +
    `os.chmod(...)` sequence honoured the process umask for the file's
    *initial* perms (typically 0o644), giving any concurrent reader a
    tiny window to slurp a `literal:` secret. O_EXCL also catches a
    stale .tmp from a crashed prior save.

    fix-all v2 L7: refuse to overwrite a symlink at PROVIDERS_FILE.
    `os.replace` renames the tmp file over the link target (replaces
    the link itself with a regular file), but checking explicitly
    surfaces the misconfiguration as a clear OSError instead of
    silently breaking whatever the link pointed at.
    """
    PROVIDERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if PROVIDERS_FILE.exists() and PROVIDERS_FILE.is_symlink():
        raise OSError(
            f"refusing to write through symlink at {PROVIDERS_FILE}; "
            "remove the link first"
        )
    tmp = PROVIDERS_FILE.with_suffix(".json.tmp")
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    # Clear any stale tmp file from a prior crashed save so O_EXCL passes.
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        # O_NOFOLLOW protects the open() syscall against a TOCTOU
        # symlink attack on the tmp filename itself. Not all platforms
        # define it; fall back to plain flags on Windows.
        flags |= os.O_NOFOLLOW
    except AttributeError:  # pragma: no cover — POSIX-only
        pass
    try:
        fd = os.open(tmp, flags, 0o600)
    except OSError:
        # Windows / FS without these flags — fall back to a plain
        # open + chmod sequence (the umask window remains, accepted on
        # platforms where 0o600 has no meaning anyway).
        tmp.write_bytes(payload)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    else:
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(payload)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    os.replace(tmp, PROVIDERS_FILE)
    # Post-replace chmod is belt-and-suspenders for FSes where
    # rename-into-place might drop the bits; harmless on POSIX since
    # the file was already 0o600 from O_CREAT.
    try:
        os.chmod(PROVIDERS_FILE, 0o600)
    except OSError:
        pass


def bootstrap_providers_from_env() -> dict:
    """Idempotent: if providers.json already exists, returns it
    untouched (operator's edits win). Otherwise synthesises from env,
    persists, and returns the new config. Safe to call on every router
    init / reload.
    """
    if PROVIDERS_FILE.exists():
        return load_providers()
    synthesised = _default_providers_from_env()
    if synthesised["providers"]:
        try:
            save_providers(synthesised)
        except OSError as exc:
            # fix-all v2 L2: bumped from warning to error and now
            # includes the path. Previous warning-level was easy to
            # miss in operators' log filters — symptom was "Settings UI
            # edits work in-memory but evaporate on restart", with no
            # actionable signal between boots.
            logger.error(
                "bootstrap_providers_from_env: failed to persist to %s (%s: %s); "
                "providers.json will be re-synthesised every boot until the "
                "underlying error (likely a permission / disk-full issue) is fixed",
                PROVIDERS_FILE, type(exc).__name__, exc,
            )
    return synthesised


# ── Task → backend routing ──────────────────────────────────────────
# Override per task type when you want a specific skill to go to a
# specific backend (e.g. send `report_writing` to Claude while everything
# else goes to a local model). Unknown / unmapped task types fall
# through to DEFAULT_BACKEND. Keep this map empty by default so a
# Claude-only or local-only deployment is honoured without surprise
# fallbacks. The sentinel "alternate" returns the first non-default
# backend, used by cross-review prompts when two backends are configured.
TASK_ROUTES: dict[str, str] = {
    "cross_review": "alternate",
}
