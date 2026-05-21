"""MinerU-backed PDF extractor.

Use this when you need high-quality formula + table + layout recovery
(e.g., HMM / DL slides where PyMuPDF reduces equations to single-character
columns). About 4x slower than PyMuPDF per page steady-state (~3-4s/page
on M4 CPU after model load), so it's an opt-in pipeline, not the default.

Two execution paths:

  1. **Persistent server (preferred)** — On first call we launch one
     `mineru-api` subprocess bound to 127.0.0.1, then route every PDF to
     it via HTTP `POST /file_parse`. The ~30-45s model load is paid once
     per parent process; subsequent uploads start at steady-state.
     `_get_or_start_mineru_server()` is the lazy singleton; an atexit
     hook sends SIGTERM at shutdown. The FastAPI `_warm_mineru_server`
     startup hook kicks the singleton at boot so the first upload
     doesn't pay the load on the request hot path.

  2. **Subprocess CLI fallback** — When the server fails to start or
     `MINERU_SERVER_DISABLED=1` is set, we fall back to the legacy
     `mineru -p <dir>` batch path: stage PDFs in a temp dir, run one
     subprocess (paying full cold start), parse content_list.json from
     each per-PDF output dir.

Both paths converge on `_blocks_to_pages(blocks)` and yield
`list[PageInfo]` with 1-based page numbers, equations as `$$...$$`,
tables as raw HTML, and images as escaped markdown image links.

Concurrency: on macOS, mineru-api hard-pins `max_concurrent_requests=1`
(see `mineru.cli.fast_api:251` — `is_mac_environment()` branch), so
`asyncio.gather` over multiple PDFs serialises inside the server. On
Linux, operators raise it via `MINERU_API_MAX_CONCURRENT_REQUESTS`.

MPS is disabled (`MINERU_DEVICE_MODE=cpu`) — under MPS the pipeline
backend hangs at `DocAnalysis init` with 0% CPU. CPU mode is the only
supported device on Apple Silicon.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from nano_notebooklm.types import PageInfo

logger = logging.getLogger(__name__)


# H3 fix (review-swarm fix-all v1): forwarding the whole parent env to the
# mineru subprocess leaks OPENAI_API_KEY / ANTHROPIC_API_KEY / AWS_* / etc.
# If mineru ever logs env on crash or loads a 3rd-party plugin those creds
# escape. Allowlist only the env that mineru *needs* (PATH so it can find
# tools; HOME for model cache lookup; HF_HOME / MINERU_* knobs; proxy vars
# so it can reach huggingface; locale for utf-8 output). Everything else
# stays in our process.
_MINERU_ENV_ALLOWLIST = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "TMPDIR",
    "LANG", "LC_ALL", "LC_CTYPE",
    "HF_HOME", "HF_HUB_CACHE", "HF_HUB_OFFLINE",
    "HUGGINGFACE_HUB_CACHE", "MODELSCOPE_CACHE",
    "TRANSFORMERS_CACHE", "TORCH_HOME", "XDG_CACHE_HOME",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy",
    # fix-all v1 M3: include the mineru-api concurrency knob in the
    # allowlist (was previously injected manually in
    # _get_or_start_mineru_server). Single-source-of-truth — anything
    # not here doesn't reach the server subprocess.
    "MINERU_API_MAX_CONCURRENT_REQUESTS",
})


# fix-all v1 M2: clamp the mineru-api concurrency env to a sane range
# BEFORE it lands in the subprocess. Operator typo `=1000` would otherwise
# fork 1000 model-loading workers and OOM the host (each holds ~5GB).
_MINERU_MAX_CONCURRENT_HARD_CAP = 16


def _validated_max_concurrent_requests() -> str | None:
    """Read + validate ``MINERU_API_MAX_CONCURRENT_REQUESTS`` from env.

    Returns the value as a string (mineru-api expects env-format) when
    parseable AND in ``[1, _MINERU_MAX_CONCURRENT_HARD_CAP]``. Returns
    None when unset, malformed, or out of range — operator gets a single
    log line and we fall through to mineru-api's own default.
    """
    raw = os.environ.get("MINERU_API_MAX_CONCURRENT_REQUESTS")
    if raw is None or not raw.strip():
        return None
    try:
        n = int(raw.strip())
    except ValueError:
        logger.warning(
            "MINERU_API_MAX_CONCURRENT_REQUESTS=%r is not an integer; ignoring", raw,
        )
        return None
    if n < 1 or n > _MINERU_MAX_CONCURRENT_HARD_CAP:
        logger.warning(
            "MINERU_API_MAX_CONCURRENT_REQUESTS=%d is out of range [1, %d]; ignoring",
            n, _MINERU_MAX_CONCURRENT_HARD_CAP,
        )
        return None
    return str(n)


def _build_mineru_env(device: str) -> dict[str, str]:
    """Build a minimal env for the mineru subprocess (H3 fix).

    Also injects thread-count caps for the inner BLAS / OpenMP / MKL
    runtimes. Without these, mineru's CPU OCR + layout YOLO over-subscribe
    threads on a many-core box and trash each other via false sharing.
    Capped at the smaller of 8 or cpu_count so single-PDF throughput is
    maximised without starving the host. Caller can override via the
    ``MINERU_OMP_THREADS`` env on the parent process.
    """
    env = {k: v for k, v in os.environ.items() if k in _MINERU_ENV_ALLOWLIST}
    # fix-all v1 M2: validate MAX_CONCURRENT_REQUESTS before forwarding.
    # The allowlist copy above pulls in the raw value; we replace (or
    # drop) it with the clamped one.
    validated = _validated_max_concurrent_requests()
    if validated is None:
        env.pop("MINERU_API_MAX_CONCURRENT_REQUESTS", None)
    else:
        env["MINERU_API_MAX_CONCURRENT_REQUESTS"] = validated
    env["MINERU_DEVICE_MODE"] = device
    threads = os.environ.get("MINERU_OMP_THREADS")
    if not threads:
        cpu = os.cpu_count() or 4
        threads = str(min(8, max(1, cpu)))
    for k in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        env[k] = threads
    return env


def _resolve_mineru_cli() -> str | None:
    """Find the mineru CLI executable.

    Order:
      1. `<sys.executable_dir>/mineru` — the venv adjacent to the running
         Python. This is the common case when called from `.venv/bin/python
         scripts/...` because PATH may not include `.venv/bin`.
      2. `shutil.which("mineru")` — anywhere on PATH.

    Returns None if neither resolves.
    """
    venv_cli = Path(sys.executable).parent / "mineru"
    if venv_cli.exists() and os.access(venv_cli, os.X_OK):
        return str(venv_cli)
    return shutil.which("mineru")


class MinerUNotFoundError(RuntimeError):
    """Raised when the `mineru` CLI is missing from the active venv."""


class MinerUExtractionError(RuntimeError):
    """Raised when mineru exits non-zero or produces no content_list.json."""


class _LoopRunningError(RuntimeError):
    """Internal sentinel — async helper called from a thread that already
    has a running event loop. Callers should fall back to the sync path.
    fix-all v1 H3: previously detected via substring match on the
    ``asyncio.run() cannot be called`` RuntimeError message, which is
    Python-version fragile."""


def _run_async(coro):
    """Run ``coro`` to completion from a sync context.

    Raises ``_LoopRunningError`` (not RuntimeError) when the calling
    thread already has a running event loop, so callers can take the
    fallback path explicitly. Replaces the brittle substring-match
    catch around ``asyncio.run`` (fix-all v1 H3).
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No running loop on this thread — safe to use asyncio.run.
        return asyncio.run(coro)
    # Cancel the coroutine we won't run, then raise the typed sentinel.
    coro.close()
    raise _LoopRunningError("event loop already running on this thread")


# ─── Persistent mineru-api server (avoid 30-45s cold start per upload) ────
#
# The mineru CLI starts a transient FastAPI server inside each invocation
# and tears it down at exit. Repeated uploads pay the full layout/OCR/
# formula/table model load every time (~30-45s on M4 CPU).
#
# We launch ONE mineru-api subprocess for the lifetime of the parent
# process and route every PDF to it via HTTP. Trade-offs:
#  - Cold start paid once at first call (lazy) or via warmup.
#  - On macOS, mineru-api hard-pins ``max_concurrent_requests=1`` (see
#    ``mineru.cli.fast_api:251``), so PDFs still serialise inside the
#    server even if the client uses asyncio.gather; on Linux operators
#    can raise it via ``MINERU_API_MAX_CONCURRENT_REQUESTS``.
#  - Server binds to 127.0.0.1 only — never exposed.
#  - At process atexit we send SIGTERM; the server's own lifespan handler
#    waits for in-flight tasks.

_MINERU_SERVER_LOCK = threading.Lock()
_MINERU_SERVER: dict | None = None  # singleton state
_MINERU_SERVER_DISABLED_REASON: str | None = None
# fix-all v1 M9/M10: in-flight launch coordination. When thread A is
# polling /health (potentially up to 180s), thread B should NOT block on
# the lock waiting for A's poll to finish — both should observe the
# launching state and wait on the same `threading.Event`. The lock is
# only held during cheap state-transition critical sections; the long
# health poll runs without it.
_MINERU_SERVER_STARTING: dict | None = None  # { "ready_event": Event(), "proc": Popen, "url": str, "port": int }


def _resolve_mineru_api_cli() -> str | None:
    venv_cli = Path(sys.executable).parent / "mineru-api"
    if venv_cli.exists() and os.access(venv_cli, os.X_OK):
        return str(venv_cli)
    return shutil.which("mineru-api")


def _pick_free_port(preferred: int) -> int:
    """Try ``preferred`` first; fall back to an ephemeral port.

    ``preferred=0`` skips the preferred-port attempt and goes straight
    to the OS-assigned ephemeral path (useful in tests).
    """
    if preferred > 0:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", preferred))
                return preferred
            except OSError:
                pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _server_disabled() -> bool:
    return os.environ.get("MINERU_SERVER_DISABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def _get_or_start_mineru_server(
    *,
    device: str = "cpu",
    startup_timeout: float = 180.0,
) -> dict | None:
    """Return the singleton server state dict, lazily starting it.

    Returns None if the server is disabled, the CLI is missing, or
    startup fails — callers should fall back to the subprocess path.
    State dict keys: ``url``, ``port``, ``proc``, ``started_at``.

    fix-all v1 M9/M10: lock is held only during state transitions
    (check cache, register pending launch, install final state). The
    long health-poll (up to ``startup_timeout`` seconds) runs WITHOUT
    the lock. Concurrent callers observe the pending-launch sentinel
    and wait on the same ``threading.Event``.
    """
    global _MINERU_SERVER, _MINERU_SERVER_DISABLED_REASON, _MINERU_SERVER_STARTING

    if _server_disabled():
        return None

    # ─── Phase 1: cheap check under lock ─────────────────────────────
    own_launch = False
    pending = None
    with _MINERU_SERVER_LOCK:
        if _MINERU_SERVER is not None:
            proc = _MINERU_SERVER.get("proc")
            if proc is not None and proc.poll() is None:
                return _MINERU_SERVER
            # Process died — clear so we can restart.
            logger.warning(
                "mineru-api server died (rc=%s); attempting restart now",
                proc.returncode if proc else None,
            )
            _MINERU_SERVER = None

        if _MINERU_SERVER_DISABLED_REASON:
            return None

        if _MINERU_SERVER_STARTING is not None:
            # Someone else is launching — we'll wait outside the lock.
            pending = _MINERU_SERVER_STARTING
        else:
            # We own this launch attempt. Register a pending sentinel so
            # other callers can join the wait.
            cli = _resolve_mineru_api_cli()
            if cli is None:
                _MINERU_SERVER_DISABLED_REASON = "mineru-api CLI not found"
                logger.info("mineru-api CLI missing; staying on subprocess path")
                return None

            try:
                preferred_port = int(os.environ.get("MINERU_API_PORT", "47865"))
                if preferred_port < 1024 or preferred_port > 65535:
                    logger.warning(
                        "MINERU_API_PORT=%d out of [1024,65535]; using ephemeral",
                        preferred_port,
                    )
                    preferred_port = 0
            except ValueError:
                logger.warning(
                    "MINERU_API_PORT=%r not an integer; using ephemeral",
                    os.environ.get("MINERU_API_PORT"),
                )
                preferred_port = 0
            port = _pick_free_port(preferred_port)
            url = f"http://127.0.0.1:{port}"

            env = _build_mineru_env(device)
            cmd = [cli, "--host", "127.0.0.1", "--port", str(port)]
            logger.info("mineru-api: launching on %s (device=%s)", url, device)
            try:
                proc = subprocess.Popen(
                    cmd, env=env,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError as exc:
                # M1: scrub exc body — OSError.__str__ embeds absolute paths /
                # usernames, surfaced via /api/status (unauthenticated). Keep the
                # exception type + errno; drop the message body.
                errno_str = f" errno={exc.errno}" if exc.errno else ""
                _MINERU_SERVER_DISABLED_REASON = f"launch failed: {type(exc).__name__}{errno_str}"
                logger.warning("mineru-api launch failed: %s%s", type(exc).__name__, errno_str)
                return None

            ready_event = threading.Event()
            pending = {"ready_event": ready_event, "proc": proc, "url": url, "port": port}
            _MINERU_SERVER_STARTING = pending
            own_launch = True

    # ─── Phase 2: health-poll WITHOUT the lock ───────────────────────
    if own_launch:
        return _poll_until_ready(pending, startup_timeout)

    # Joining an existing launch — wait on its event, then read the
    # outcome from the global state.
    pending["ready_event"].wait(timeout=startup_timeout + 5.0)
    with _MINERU_SERVER_LOCK:
        return _MINERU_SERVER  # may be None if the launch failed


def _poll_until_ready(pending: dict, startup_timeout: float) -> dict | None:
    """Poll /health (no lock held) until 200 or deadline.

    On success: install state and signal the event.
    On failure: set sticky disabled reason, kill proc, signal the event.
    """
    global _MINERU_SERVER, _MINERU_SERVER_DISABLED_REASON, _MINERU_SERVER_STARTING

    proc = pending["proc"]
    url = pending["url"]
    port = pending["port"]
    ready_event = pending["ready_event"]

    # stdlib urllib avoids importing httpx on the boot hot path.
    from urllib.request import urlopen
    from urllib.error import URLError

    t0 = time.monotonic()
    deadline = t0 + startup_timeout
    ready = False
    fail_reason: str | None = None

    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                fail_reason = f"server exited rc={proc.returncode}"
                logger.error("mineru-api exited during startup (rc=%s)", proc.returncode)
                break
            try:
                with urlopen(f"{url}/health", timeout=2.0) as resp:
                    if resp.status == 200:
                        ready = True
                        break
            except (URLError, OSError, TimeoutError):
                pass
            time.sleep(1.0)
        else:
            fail_reason = "health check timed out"

        if not ready:
            logger.warning("mineru-api did not become healthy: %s; killing", fail_reason)
            try:
                proc.terminate()
                proc.wait(timeout=5.0)
            except Exception:  # noqa: BLE001 — best-effort cleanup
                try:
                    proc.kill()
                except OSError:
                    pass
            with _MINERU_SERVER_LOCK:
                _MINERU_SERVER_DISABLED_REASON = fail_reason
                if _MINERU_SERVER_STARTING is pending:
                    _MINERU_SERVER_STARTING = None
            return None

        elapsed = time.monotonic() - t0
        logger.info("mineru-api: ready on %s after %.1fs", url, elapsed)

        state = {"url": url, "port": port, "proc": proc, "started_at": time.monotonic()}
        with _MINERU_SERVER_LOCK:
            _MINERU_SERVER = state
            if _MINERU_SERVER_STARTING is pending:
                _MINERU_SERVER_STARTING = None
        atexit.register(_stop_mineru_server)
        return state
    finally:
        # Signal any joiners regardless of outcome so they don't deadlock.
        ready_event.set()


def _stop_mineru_server() -> None:
    """atexit hook — send SIGTERM, then SIGKILL after 10s grace."""
    global _MINERU_SERVER
    with _MINERU_SERVER_LOCK:
        state = _MINERU_SERVER
        _MINERU_SERVER = None
    if not state:
        return
    proc = state.get("proc")
    if proc is None or proc.poll() is not None:
        return
    try:
        logger.info("mineru-api: stopping pid=%s", proc.pid)
        proc.terminate()
        try:
            proc.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5.0)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("mineru-api shutdown error: %s", exc)


# fix-all v1 M7: per-PDF HTTP read timeout cap. Previously the read
# timeout was set to the whole-batch budget (default 1800s), masking a
# hung mineru server for 30 minutes per PDF. Cap reads at 300s — enough
# for a 100-page PDF on CPU at ~4s/page steady state with a 100% safety
# margin — and let the OUTER batch budget guard total wall clock.
# Operator override via ``MINERU_PER_PDF_TIMEOUT_SECONDS``.
def _per_pdf_timeout() -> float:
    raw = os.environ.get("MINERU_PER_PDF_TIMEOUT_SECONDS")
    if raw:
        try:
            v = float(raw)
            if 30.0 <= v <= 3600.0:
                return v
            logger.warning(
                "MINERU_PER_PDF_TIMEOUT_SECONDS=%s out of [30,3600]; using default",
                raw,
            )
        except ValueError:
            logger.warning(
                "MINERU_PER_PDF_TIMEOUT_SECONDS=%r not a number; using default", raw,
            )
    return 300.0


async def _extract_one_via_server(
    server_url: str,
    filepath: Path,
    lang: str,
    *,
    timeout_seconds: float | None = None,
) -> list[PageInfo]:
    """POST a single PDF to the mineru-api server and parse the response.

    Returns a list of PageInfo with the same contract as
    ``extract_pdf_mineru``. Raises ``MinerUExtractionError`` on any
    non-200 response, transport error, or malformed payload.

    ``timeout_seconds`` is the per-PDF HTTP read timeout. When None,
    defaults to ``_per_pdf_timeout()`` (300s or operator override).
    """
    import httpx

    if timeout_seconds is None:
        timeout_seconds = _per_pdf_timeout()
    else:
        # Even if caller passes the whole-batch budget, cap at per-PDF.
        timeout_seconds = min(float(timeout_seconds), _per_pdf_timeout())

    # Open the file in a thread to avoid blocking the loop on slow disks.
    data = await asyncio.to_thread(filepath.read_bytes)
    files = {"files": (filepath.name, data, "application/pdf")}
    # ``lang_list`` accepts multi-value form fields. Pass one per file.
    form = [
        ("lang_list", lang),
        ("backend", "pipeline"),
        ("parse_method", "auto"),
        ("formula_enable", "true"),
        ("table_enable", "true"),
        ("return_md", "false"),
        ("return_middle_json", "false"),
        ("return_model_output", "false"),
        ("return_content_list", "true"),
        ("return_images", "false"),
        ("response_format_zip", "false"),
        ("return_original_file", "false"),
    ]

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds, connect=10.0)) as client:
            resp = await client.post(f"{server_url}/file_parse", files=files, data=form)
    except httpx.HTTPError as exc:
        # fix-all v1 (review-swarm S3 L1): the singleton may be stale
        # (proc died between poll and send). Clear it so the next call
        # restarts cleanly instead of hitting the dead address again.
        _clear_server_state_after_transport_failure()
        raise MinerUExtractionError(f"mineru-api transport error on {filepath.name}: {exc}") from exc

    if resp.status_code != 200:
        # fix-all v1 M11: match the subprocess-path "stderr tail:" format
        # so any log scraper / monitoring keyed on that prefix sees a
        # parallel "body tail:" prefix for server-path failures.
        body = resp.text[:500]
        raise MinerUExtractionError(
            f"mineru-api {filepath.name}: HTTP {resp.status_code}\nbody tail:\n{body}"
        )

    try:
        payload = resp.json()
    except ValueError as exc:
        raise MinerUExtractionError(f"mineru-api {filepath.name}: bad JSON: {exc}") from exc

    results = payload.get("results") or {}
    # The server keys by the original filename (with extension).
    entry = results.get(filepath.name)
    if entry is None:
        # Fall back to stem match — older versions strip the suffix.
        for k, v in results.items():
            if Path(k).stem == filepath.stem:
                entry = v
                break
    if entry is None:
        raise MinerUExtractionError(
            f"mineru-api {filepath.name}: no entry in results (keys={list(results)[:3]})"
        )

    blocks = entry.get("content_list")
    if not isinstance(blocks, list):
        raise MinerUExtractionError(
            f"mineru-api {filepath.name}: content_list missing or wrong type"
        )

    pages = _blocks_to_pages(blocks)
    total = max((p.page or 0) for p in pages) if pages else 0
    for p in pages:
        p.total_pages = total
    return pages


def _clear_server_state_after_transport_failure() -> None:
    """fix-all v1 (S3 L1): drop the singleton when an HTTP call to it
    fails at transport level. Next caller will re-launch."""
    global _MINERU_SERVER
    with _MINERU_SERVER_LOCK:
        if _MINERU_SERVER is None:
            return
        proc = _MINERU_SERVER.get("proc")
        # Only clear if proc actually died — a transient network blip
        # during normal operation shouldn't tear down a healthy server.
        if proc is not None and proc.poll() is not None:
            logger.warning(
                "mineru-api transport failure + dead proc (rc=%s); clearing singleton",
                proc.returncode,
            )
            _MINERU_SERVER = None


async def extract_pdfs_mineru_via_server(
    filepaths: list[str | Path],
    lang: str = "ch",
    *,
    timeout_seconds: float = 1800.0,
    device: str = "cpu",
) -> dict[str, list[PageInfo]]:
    """Server-backed batch: one HTTP request per PDF, ``asyncio.gather``.

    On Mac the server is pinned to concurrency=1 so the gather is in
    effect serial; on Linux + ``MINERU_API_MAX_CONCURRENT_REQUESTS>1``
    multiple PDFs run in true parallel. Either way the model load cost
    is paid once for the parent process, not once per upload batch.

    Returns ``{absolute_filepath: list[PageInfo]}``. Files that fail are
    omitted from the dict (caller falls back per-file).

    Raises ``MinerUExtractionError`` ONLY when the server is unreachable
    or every PDF failed; per-file failures are logged and swallowed so a
    single corrupt PDF doesn't poison the batch.
    """
    paths = [Path(p).resolve() for p in filepaths]
    if not paths:
        return {}
    missing = [p for p in paths if not p.exists()]
    if missing:
        raise FileNotFoundError(f"missing inputs: {missing[:3]}")

    # ``_get_or_start_mineru_server`` holds a threading lock during startup;
    # don't run it on the event loop thread directly.
    state = await asyncio.to_thread(_get_or_start_mineru_server, device=device)
    if state is None:
        raise MinerUExtractionError("mineru-api server unavailable; caller should fall back")
    url = state["url"]

    logger.info("mineru-api: batch start — %d PDFs", len(paths))
    t0 = time.monotonic()

    async def _one(p: Path) -> tuple[Path, list[PageInfo] | None, str | None]:
        try:
            pages = await _extract_one_via_server(url, p, lang, timeout_seconds=timeout_seconds)
            return (p, pages, None)
        except Exception as exc:  # noqa: BLE001 — per-file fault isolation
            return (p, None, f"{type(exc).__name__}: {exc}")

    tasks = [asyncio.create_task(_one(p)) for p in paths]
    results: dict[str, list[PageInfo]] = {}
    errors: list[str] = []
    for fut in asyncio.as_completed(tasks):
        p, pages, err = await fut
        if pages is not None:
            results[str(p)] = pages
        else:
            errors.append(f"{p.name}: {err}")
            logger.warning("mineru-api: failed %s — %s", p.name, err)

    elapsed = time.monotonic() - t0
    logger.info(
        "mineru-api: batch done — %d ok / %d failed in %.1fs (%.1fs/file avg)",
        len(results), len(errors), elapsed, elapsed / max(len(paths), 1),
    )

    if not results and errors:
        raise MinerUExtractionError(
            f"mineru-api: all {len(paths)} PDFs failed\nfirst error: {errors[0]}"
        )
    return results


def extract_pdf_mineru(
    filepath: str | Path,
    lang: str = "ch",
    output_dir: str | Path | None = None,
    *,
    start_page: int | None = None,
    end_page: int | None = None,
    timeout_seconds: int = 1800,
    device: str = "cpu",
) -> list[PageInfo]:
    """Extract PDF pages via MinerU pipeline backend.

    Args:
      filepath: absolute or relative path to the PDF.
      lang: `ch` for Chinese, `en` for English. Affects which OCR
        model is loaded; auto-detection is unreliable on slide decks.
      output_dir: directory MinerU writes to. When None a temp dir is
        used and deleted after parse. Pass a real dir to keep the
        intermediate markdown / image assets (useful for debugging).
      start_page / end_page: 0-indexed inclusive range. None = whole PDF.
      timeout_seconds: subprocess timeout. Default 1800s = 30 min, enough
        for ~100 pages on M4 CPU.
      device: `cpu` (only currently-supported on Apple Silicon) or `mps`
        (currently hangs — keep `cpu`).

    Returns: 1-based PageInfo list. Each page text is the natural reading
    order of blocks, with LaTeX equations as `$$...$$` blocks and tables
    as raw HTML. `has_formula` is *not* set here — the chunker decides
    that per chunk after concatenation / splitting.
    """
    filepath = Path(filepath).resolve()
    if not filepath.exists():
        raise FileNotFoundError(filepath)

    # fix-all v1 H1: route through the persistent mineru-api server when
    # available so a single-file call (e.g. kb/store.py per-file fallback
    # after a batch entry is missing) also skips the 30-45s cold start.
    # start_page / end_page can't be honoured by the server path (its
    # API doesn't expose -s/-e), so fall through to subprocess when set.
    if start_page is None and end_page is None and not _server_disabled():
        try:
            results = _run_async(
                extract_pdfs_mineru_via_server(
                    [str(filepath)], lang=lang,
                    timeout_seconds=float(timeout_seconds), device=device,
                )
            )
            pages = results.get(str(filepath))
            if pages is not None:
                return pages
        except MinerUExtractionError as exc:
            logger.warning(
                "mineru-api single-file failed (%s); falling back to subprocess CLI",
                exc,
            )
        except _LoopRunningError:
            logger.warning(
                "mineru-api single-file skipped (event loop already running); "
                "falling back to subprocess CLI",
            )

    mineru_cli = _resolve_mineru_cli()
    if mineru_cli is None:
        raise MinerUNotFoundError(
            "mineru CLI not found. Install with: pip install 'mineru[pipeline]'"
        )

    cleanup = False
    if output_dir is None:
        output_dir = Path(tempfile.mkdtemp(prefix="mineru_extract_"))
        cleanup = True
    else:
        output_dir = Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

    try:
        cmd = [
            mineru_cli,
            "-p", str(filepath),
            "-o", str(output_dir),
            "-b", "pipeline",
            "-l", lang,
        ]
        if start_page is not None:
            cmd += ["-s", str(start_page)]
        if end_page is not None:
            cmd += ["-e", str(end_page)]

        env = _build_mineru_env(device)

        logger.info(
            "mineru: starting %s lang=%s pages=%s..%s device=%s",
            filepath.name, lang,
            "0" if start_page is None else start_page,
            "end" if end_page is None else end_page,
            device,
        )
        t0 = time.monotonic()
        # M5 + M6 (review-swarm fix-all v1):
        #   - errors="replace" so mineru's non-utf-8 panic dumps don't
        #     crash the wrapper with UnicodeDecodeError;
        #   - capture_output buffers ALL stdout+stderr in memory which on
        #     a 100-page PDF can hit 10MB+. We keep stderr bounded by
        #     piping it through a background draining thread that retains
        #     only the last ~200 lines for the error message.
        from collections import deque
        from threading import Thread

        stderr_tail: deque[str] = deque(maxlen=200)

        def _drain(stream, sink: deque[str]) -> None:
            try:
                for line in stream:
                    sink.append(line.rstrip("\n"))
            except Exception:
                pass

        proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        drainer = Thread(target=_drain, args=(proc.stderr, stderr_tail), daemon=True)
        drainer.start()
        try:
            proc.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            elapsed = time.monotonic() - t0
            logger.warning("mineru: timeout after %.1fs on %s", elapsed, filepath.name)
            raise MinerUExtractionError(
                f"mineru timed out after {timeout_seconds}s on {filepath.name}\n"
                "stderr tail:\n" + "\n".join(list(stderr_tail)[-20:])
            )
        finally:
            drainer.join(timeout=1.0)

        elapsed = time.monotonic() - t0
        if proc.returncode != 0:
            logger.error(
                "mineru: failed (%s) in %.1fs on %s",
                proc.returncode, elapsed, filepath.name,
            )
            raise MinerUExtractionError(
                f"mineru exited {proc.returncode}\nstderr tail:\n"
                + "\n".join(list(stderr_tail)[-20:])
            )
        logger.info("mineru: completed %s in %.1fs", filepath.name, elapsed)

        # mineru writes to <output_dir>/<stem>/auto/<stem>_content_list.json
        stem = filepath.stem
        content_list_path = output_dir / stem / "auto" / f"{stem}_content_list.json"
        if not content_list_path.exists():
            raise MinerUExtractionError(
                f"content_list.json not found at {content_list_path}\n"
                "mineru may have produced a different layout — check output dir."
            )

        with content_list_path.open(encoding="utf-8") as fh:
            blocks = json.load(fh)

        pages = _blocks_to_pages(blocks)
        # Annotate total_pages
        total = max((p.page or 0) for p in pages) if pages else 0
        for p in pages:
            p.total_pages = total
        return pages
    finally:
        if cleanup:
            shutil.rmtree(output_dir, ignore_errors=True)


def extract_pdfs_mineru_batch(
    filepaths: list[str | Path],
    lang: str = "ch",
    output_dir: str | Path | None = None,
    *,
    timeout_seconds: int = 3600,
    device: str = "cpu",
) -> dict[str, list[PageInfo]]:
    """Batch-extract many PDFs through MinerU.

    Default path is the persistent ``mineru-api`` server (one model load
    per parent process). When the server is disabled or unreachable, we
    fall back to the H5 single-subprocess batch CLI path: stage all PDFs
    in one temp dir, run ``mineru -p <dir>``, parse outputs.

    Returns: ``{filepath_str: list[PageInfo]}`` keyed by the **original**
    filepath. Files that fail are simply absent (callers should fall
    back per-file).

    Args mostly mirror ``extract_pdf_mineru``. ``timeout_seconds``
    defaults to 1 hour because a batch of 20 PDFs × 50 pages can easily
    take 30 minutes on CPU.
    """
    # Server-first path. Run the async helper to completion via
    # _run_async (fix-all v1 H3) so callers in a sync thread (kb.ingest_course
    # is offloaded via to_thread from the upload pipeline) don't need to
    # know about the event loop. Falls back to subprocess CLI on any
    # server-level failure or when a running loop is detected.
    if not _server_disabled():
        try:
            return _run_async(
                extract_pdfs_mineru_via_server(
                    filepaths, lang=lang,
                    timeout_seconds=float(timeout_seconds), device=device,
                )
            )
        except MinerUExtractionError as exc:
            logger.warning(
                "mineru-api batch failed (%s); falling back to subprocess CLI",
                exc,
            )
        except _LoopRunningError:
            logger.warning(
                "mineru-api batch skipped (event loop already running); "
                "falling back to subprocess CLI",
            )

    mineru_cli = _resolve_mineru_cli()
    if mineru_cli is None:
        raise MinerUNotFoundError(
            "mineru CLI not found. Install with: pip install 'mineru[pipeline]'"
        )

    paths = [Path(p).resolve() for p in filepaths]
    missing = [p for p in paths if not p.exists()]
    if missing:
        raise FileNotFoundError(f"missing inputs: {missing[:3]}")
    if not paths:
        return {}

    cleanup_input = False
    cleanup_output = False
    if output_dir is None:
        output_dir = Path(tempfile.mkdtemp(prefix="mineru_batch_out_"))
        cleanup_output = True
    else:
        output_dir = Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

    # Stage all PDFs in a single dir for mineru's directory mode. We
    # prefer symlinks for zero-copy; fall back to copy on filesystems
    # that don't support them (mostly the macOS sandboxed case).
    input_dir = Path(tempfile.mkdtemp(prefix="mineru_batch_in_"))
    cleanup_input = True
    stem_to_original: dict[str, Path] = {}
    try:
        for p in paths:
            staged = input_dir / p.name
            # If two inputs have the same basename we'd collide. Disambiguate
            # by prepending a short hash; the stem mineru produces will use
            # this filename, so we record the mapping.
            if staged.exists():
                import hashlib
                h = hashlib.sha1(str(p).encode()).hexdigest()[:8]
                staged = input_dir / f"{p.stem}_{h}{p.suffix}"
            try:
                staged.symlink_to(p)
            except OSError:
                shutil.copy2(p, staged)
            stem_to_original[staged.stem] = p

        cmd = [
            mineru_cli,
            "-p", str(input_dir),
            "-o", str(output_dir),
            "-b", "pipeline",
            "-l", lang,
        ]
        env = _build_mineru_env(device)
        logger.info(
            "mineru batch: %d PDFs lang=%s device=%s",
            len(paths), lang, device,
        )
        t0 = time.monotonic()

        from collections import deque
        from threading import Thread

        stderr_tail: deque[str] = deque(maxlen=200)

        def _drain(stream, sink):
            try:
                for line in stream:
                    sink.append(line.rstrip("\n"))
            except Exception:
                pass

        proc = subprocess.Popen(
            cmd, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
        drainer = Thread(target=_drain, args=(proc.stderr, stderr_tail), daemon=True)
        drainer.start()
        try:
            proc.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise MinerUExtractionError(
                f"mineru batch timed out after {timeout_seconds}s for {len(paths)} PDFs"
            )
        finally:
            drainer.join(timeout=1.0)

        elapsed = time.monotonic() - t0
        if proc.returncode != 0:
            logger.error("mineru batch failed (%s) in %.1fs", proc.returncode, elapsed)
            raise MinerUExtractionError(
                f"mineru batch exited {proc.returncode}\nstderr tail:\n"
                + "\n".join(list(stderr_tail)[-30:])
            )
        logger.info(
            "mineru batch: %d PDFs done in %.1fs (%.1fs/file avg)",
            len(paths), elapsed, elapsed / max(len(paths), 1),
        )

        # Read each PDF's content_list.json back out.
        results: dict[str, list[PageInfo]] = {}
        for stem, original in stem_to_original.items():
            content_list = output_dir / stem / "auto" / f"{stem}_content_list.json"
            if not content_list.exists():
                logger.warning(
                    "mineru batch: no output for %s (stem=%s)", original.name, stem
                )
                continue
            try:
                with content_list.open(encoding="utf-8") as fh:
                    blocks = json.load(fh)
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("mineru batch: bad output for %s: %s", original.name, exc)
                continue
            pages = _blocks_to_pages(blocks)
            total = max((p.page or 0) for p in pages) if pages else 0
            for p in pages:
                p.total_pages = total
            results[str(original)] = pages
        return results
    finally:
        if cleanup_input:
            shutil.rmtree(input_dir, ignore_errors=True)
        if cleanup_output:
            shutil.rmtree(output_dir, ignore_errors=True)


def _blocks_to_pages(blocks: list[dict]) -> list[PageInfo]:
    """Group MinerU blocks by page_idx and render each page to text.

    Each block has keys: type (text|header|equation|image|chart|table),
    bbox ([x0,y0,x1,y1]), page_idx (0-based), plus type-specific payload.
    """
    by_page: dict[int, list[dict]] = {}
    for b in blocks:
        idx = b.get("page_idx")
        if idx is None:
            continue
        by_page.setdefault(idx, []).append(b)

    pages: list[PageInfo] = []
    for page_idx in sorted(by_page):
        page_blocks = sorted(
            by_page[page_idx],
            key=lambda b: (b.get("bbox", [0, 0, 0, 0])[1], b.get("bbox", [0, 0, 0, 0])[0]),
        )
        parts: list[str] = []
        for b in page_blocks:
            rendered = _render_block(b)
            if rendered:
                parts.append(rendered)
        text = "\n\n".join(parts).strip()
        if not text:
            continue
        pages.append(PageInfo(text=text, page=page_idx + 1))
    return pages


def _render_block(b: dict) -> str:
    """Render a single MinerU block to markdown/LaTeX text."""
    t = b.get("type")
    if t in ("text", "header"):
        text = (b.get("text") or "").strip()
        if not text:
            return ""
        # Headers get a markdown heading; text_level 1=#, 2=##, ...
        level = b.get("text_level")
        if t == "header" and isinstance(level, int) and level > 0:
            return f"{'#' * min(level, 6)} {text}"
        if t == "text" and isinstance(level, int) and level > 0:
            return f"{'#' * min(level, 6)} {text}"
        return text
    if t == "equation":
        latex = (b.get("text") or "").strip()
        if not latex:
            return ""
        # MinerU emits "$$\n...\n$$" already; preserve as-is so chunker
        # can detect block boundaries downstream.
        return latex if latex.startswith("$$") else f"$$\n{latex}\n$$"
    if t == "table":
        body = (b.get("table_body") or "").strip()
        return body
    if t in ("image", "chart"):
        path = b.get("img_path") or ""
        caption_field = "image_caption" if t == "image" else "chart_caption"
        captions = b.get(caption_field) or []
        caption = " ".join(c.strip() for c in captions if c.strip()) if isinstance(captions, list) else ""
        return _safe_markdown_image(caption, path)
    # Unknown block type — keep its text if present, otherwise skip.
    return (b.get("text") or "").strip()


# H6 fix (review-swarm fix-all v1): MinerU's image_caption is content
# from the user's PDF and can be adversarial. Naively interpolating it
# into `f"![{caption}]({path})"` lets a PDF caption like
# `](javascript:alert(1))` close the link and inject an arbitrary URL.
# That chunk text then flows to the chat answer + Notes preview, where
# the frontend renders markdown.
#
# Defense: (1) escape `]` and `)` and backslash in caption so the
# parser can't see a fake link close, and (2) reject any path whose
# scheme is dangerous (javascript:/data:/vbscript:); only relative
# paths or http(s) survive. Images mineru produces are always relative
# `images/<sha>.jpg`, so the scheme guard never blocks a legitimate
# block.
_DANGEROUS_URL_SCHEMES = ("javascript:", "data:", "vbscript:", "file:")


def _safe_markdown_image(caption: str, path: str) -> str:
    if not path:
        return ""
    lowered = path.strip().lower()
    for scheme in _DANGEROUS_URL_SCHEMES:
        if lowered.startswith(scheme):
            # Drop the link entirely — caption (if any) becomes plain text.
            return caption.strip()
    safe_caption = (
        caption.replace("\\", "\\\\")
               .replace("]", "\\]")
               .replace("[", "\\[")
    )
    safe_path = (
        path.replace("\\", "\\\\")
            .replace(")", "\\)")
            .replace("(", "\\(")
    )
    return f"![{safe_caption}]({safe_path})"
