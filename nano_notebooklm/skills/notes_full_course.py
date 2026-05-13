r"""Full-course note generation: per-file parallel LLM calls → programmatic
concat → single LLM review/polish pass.

Replaces the single-shot path (note_generator) for the "Generate full-course
notes" entry point. The single-shot skill remains the implementation for
topic-scoped notes (when `topic` is provided).

Design contract (agreed with user, 2026-05-11):
  - 1 LLM call per source_file (parallelism capped via asyncio.Semaphore=4)
  - Per-file outputs sanitized via latex_sanitizer.check() — failures
    surface as file_error events, do not abort the batch.
  - Programmatic merge: \section{<file>} wrap, idx-ordered, no LLM cost.
  - Single LLM review pass over the merged draft for terminology
    consistency + cross-references + duplicate-definition collapse.
  - Final body sanitized via check_unbounded() (forbidden-command scan
    without the 80KB cap — a 20-file course legitimately exceeds it).

The endpoint side (api/server.py) consumes these helpers and emits NDJSON
events progressively, so the first finished file shows in the UI while
the others are still running.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from typing import TYPE_CHECKING, Any

from nano_notebooklm.ai import prompt_templates as prompts
from nano_notebooklm.ai.base import TruncationSignal
from nano_notebooklm.skills.latex_sanitizer import LaTeXUnsafeError, check
from nano_notebooklm.types import Chunk

if TYPE_CHECKING:  # avoid runtime import cycles — only used for type hints
    from nano_notebooklm.ai.router import ModelRouter
    from nano_notebooklm.kb.store import KBStore

logger = logging.getLogger(__name__)

# Cache hardening v1 (2026-05-11):
#
# (a) Per-course cache lock. write_cache_entry does load → mutate → save,
#     which without a lock loses the other writer's entry under concurrent
#     fan-out. We keep one asyncio.Lock per course_id, lazy-initialised in
#     _get_course_cache_lock. Different courses don't contend.
#
# (b) Prompt version hash. If the team edits NOTE_FORMAT_LATEX /
#     NOTE_GENERATION_PROMPT / NOTE_MERGE_REVIEW_PROMPT, every cached
#     entry was produced by an outdated prompt and MUST regenerate.
#     _NOTE_PROMPT_VERSION is a stable sha1[:8] hash over the concatenation
#     of those three prompt strings, computed at import time. Each cache
#     entry now carries this field and plan_for_course treats a mismatch
#     as a miss.
#
# (c) Envelope schema. We wrap on-disk JSON in {"version", "entries",
#     "prompt_version"} so future shape changes can read-migrate. load_cache
#     accepts both v0 (bare-dict) legacy and v1 envelope. save_cache always
#     writes v1.
_NOTE_PROMPT_VERSION: str = hashlib.sha1(
    (
        prompts.NOTE_FORMAT_LATEX
        + prompts.NOTE_GENERATION_PROMPT
        + prompts.NOTE_MERGE_REVIEW_PROMPT
    ).encode("utf-8")
).hexdigest()[:8]

_CACHE_SCHEMA_VERSION: int = 1

# Keyed by (running-loop id, course_id) so locks captured under one loop
# don't accidentally serve a different loop (e.g. across asyncio.run()
# boundaries in test suites — production has a single loop, so the loop
# component is constant). asyncio.Lock binds to the first loop that
# acquires it; reusing it from another loop raises RuntimeError.
_COURSE_CACHE_LOCKS: dict[tuple[int, str], asyncio.Lock] = {}


def _get_course_cache_lock(course_id: str) -> asyncio.Lock:
    """Lazy-init per-course asyncio.Lock for the currently-running event
    loop. Two concurrent write_cache_entry calls for the same course
    serialise; different courses run in parallel.

    The dict grows by course_id forever — fine in practice (course_ids are
    bounded by user upload count) but if this ever ships in a multi-tenant
    SaaS context we'd want LRU eviction.
    """
    try:
        loop_id = id(asyncio.get_running_loop())
    except RuntimeError:
        # No running loop (synchronous context) — use 0 as a sentinel.
        # The caller will fail later anyway when `async with lock` runs,
        # but we avoid a confusing KeyError here.
        loop_id = 0
    key = (loop_id, course_id)
    lock = _COURSE_CACHE_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _COURSE_CACHE_LOCKS[key] = lock
    return lock

# Cap chunks fed into a single per-file prompt. Big PDF lectures can exceed
# 100 chunks; at ~300 tokens each that's 30K+ input tokens before the
# instructions, which starts to crowd codex GPT-5.4's context window. 60
# captures the bulk of a typical 90-minute lecture without truncating
# anything important — chunks are slide-order, so we keep the start of the
# lecture (where the definitions live) and drop only the trailing exercises.
MAX_CHUNKS_PER_FILE = 60

# 2026-05-13: raised 2 → 8 per user judgement that 2 was too slow on
# multi-file courses. The per-file retry in `generate_file_stream`
# (max_retries=1) tolerates the mid-stream resets the codex proxy
# occasionally issues under high concurrency; if 8 turns out to retry
# excessively in practice, dial back via NOTES_DEFAULT_CONCURRENCY env.
# Hard ceiling stays at 8 in ChatRequest.concurrency Pydantic validator
# (server.py) so a stray UI value can't go higher.
DEFAULT_CONCURRENCY = int(os.getenv("NOTES_DEFAULT_CONCURRENCY", "8"))
# Output-length caps for the two LLM passes. Both default well above the
# previous 8192 because (a) per-file notes for a dense 90-minute lecture
# can legitimately need ~10K tokens to cover every \begin{definition} /
# \begin{theorem} env, and (b) the review pass concatenates N files and
# needs proportionally more room — an 8192 cap was silently truncating
# the tail of long courses inside `\begin{...}` envs, producing notes
# that "stop without closing the last definition". Tunable via env so
# operators with stricter budget caps can dial them back.
PER_FILE_MAX_TOKENS = int(os.getenv("NOTES_PER_FILE_MAX_TOKENS", "12288"))
REVIEW_MAX_TOKENS = int(os.getenv("NOTES_REVIEW_MAX_TOKENS", "24576"))
PER_FILE_TEMPERATURE = 0.3
REVIEW_TEMPERATURE = 0.2


@dataclass(frozen=True)
class FilePlan:
    """Prepared inputs for one per-file LLM call.

    Incremental cache (2026-05-11): when ``cached_content`` is non-None,
    the per-file LLM call SHOULD be skipped — the endpoint emits a
    ``file_cached`` event and uses ``cached_content`` as the merge input.
    ``cache_key`` is the content hash of the file's chunks; the endpoint
    writes ``{cache_key, content}`` back to per_file_cache.json after a
    fresh successful generation.
    """
    idx: int
    source_file: str
    chunk_count: int
    prompt: str
    system: str
    task_type: str
    temperature: float
    max_tokens: int
    cache_key: str = ""
    cached_content: str | None = None


@dataclass(frozen=True)
class FileResult:
    """Outcome of one per-file LLM call. Exactly one of content/error
    is non-None.

    ``truncated`` is True when the upstream LLM stopped because it hit
    max_output_tokens / finish_reason='length' rather than completing
    naturally. The accumulated content is still kept (and the sanitizer
    still runs on it), but the caller MUST surface a visible "⚠️ this
    file's notes were truncated — consider raising NOTES_PER_FILE_MAX_TOKENS
    or splitting the source file" affordance to the user; otherwise a
    half-written ``\\begin{definition}`` env ships silently into the
    merge step.
    """
    idx: int
    source_file: str
    chunk_count: int
    content: str | None
    error: str | None
    truncated: bool = False


# ── Incremental per-file cache ─────────────────────────────────────
#
# Store: artifacts/courses/<course_id>/notes/per_file_cache.json
# Shape: {
#   "<source_file>": {
#     "chunk_hash": "<sha256 hex>",
#     "content": "<LaTeX body for this file>",
#     "generated_at": "<iso8601 UTC>",
#     "model": "<router model name>"
#   }
# }
#
# Invalidation: SHA256 over `chunk_id + "\n" + text` per chunk, joined with
# `|` separators. Catches both re-upload (chunk_ids re-issued) and content
# drift (text changes inside the same chunks).
#
# Concurrency: load_cache + save_cache are not internally locked. The
# /api/notes/full-course/stream endpoint serialises read/write because
# the same global semaphore that gates the LLM-heavy span also gates the
# cache mutation; two concurrent requests for the SAME course never write
# the cache at the same time. Different courses write different files.


def _cache_path(course_id: str) -> Path:
    """Resolve the per-file cache JSON path; refuses paths that escape
    ARTIFACTS_DIR/courses (defense in depth — course_id values like
    "../etc" otherwise let a future caller read arbitrary disk)."""
    from nano_notebooklm import config
    base = (config.ARTIFACTS_DIR / "courses" / course_id / "notes").resolve()
    allowed = (config.ARTIFACTS_DIR / "courses").resolve()
    if not base.is_relative_to(allowed):
        raise ValueError(f"course_id {course_id!r} resolves outside artifacts root")
    return base / "per_file_cache.json"


def load_cache(course_id: str) -> dict[str, dict]:
    """Read per_file_cache.json. Returns {} on missing / corrupt file —
    the caller treats a missing cache the same as an empty one (everything
    needs regen). Corrupt-file path logs at WARNING so an operator notices.

    Accepts both v0 (bare-dict `{source_file: entry, ...}`) legacy files
    and v1 envelope (`{"version": 1, "entries": {...}, ...}`). Returns the
    bare entries dict in both cases so callers don't need to know.
    """
    try:
        p = _cache_path(course_id)
    except ValueError:
        return {}
    if not p.exists():
        return {}
    try:
        with p.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        logger.warning("per_file_cache.json corrupt for %s — treating as empty",
                       course_id)
        return {}
    if not isinstance(data, dict):
        return {}
    # v1 envelope: {"version": 1, "entries": {...}, "prompt_version": "..."}
    if "version" in data and "entries" in data:
        entries = data.get("entries")
        if isinstance(entries, dict):
            return entries
        return {}
    # v0 legacy: bare dict mapping source_file → entry
    return data


def save_cache(course_id: str, cache: dict[str, dict]) -> None:
    """Atomic write of the entire cache dict — unique temp file + os.replace.

    The temp filename includes a uuid4 suffix so two concurrent workers
    writing the SAME cache file don't fight over a shared `.json.tmp`
    path (which would otherwise produce a race: W1 writes tmp, W2 writes
    tmp [clobbering W1], W1 os.replace ✓, W2 os.replace ✗ FileNotFound).
    The `os.replace` itself is atomic on POSIX, so whoever runs it last
    wins — write_cache_entry's read-modify-write protects single-entry
    updates from being lost (load → mutate → save_cache rewrites all).

    Caller passes the FULL desired post-state; we don't read-modify-write
    here. Use write_cache_entry / prune_stale_cache for incremental
    updates so two pieces of mutation logic don't drift.
    """
    try:
        p = _cache_path(course_id)
    except ValueError:
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    # uuid4 hex makes contention between concurrent workers benign — each
    # owns its own tmp file; only the final os.replace contends, and that
    # is atomic.
    import uuid as _uuid
    tmp = p.with_suffix(f".json.tmp.{_uuid.uuid4().hex[:8]}")
    envelope = {
        "version": _CACHE_SCHEMA_VERSION,
        "prompt_version": _NOTE_PROMPT_VERSION,
        "entries": cache,
    }
    payload = json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True)
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(payload)
    os.replace(tmp, p)


def chunk_hash(chunks: list[Chunk]) -> str:
    """Stable content hash for a file's chunks.

    Combines chunk_id (catches re-ingest where the text is identical but
    chunk boundaries shifted) AND text (catches content edits to the
    underlying source file). Separator bytes are non-text so a chunk text
    that happens to contain `|` cannot collide with another arrangement.
    """
    h = hashlib.sha256()
    for c in chunks:
        h.update(b"\x1f")  # ASCII unit separator
        h.update((c.chunk_id or "").encode("utf-8", "replace"))
        h.update(b"\x1e")  # ASCII record separator
        h.update((c.text or "").encode("utf-8", "replace"))
    return h.hexdigest()


def _write_cache_entry_unlocked(
    course_id: str,
    source_file: str,
    *,
    chunk_hash_value: str,
    content: str,
    model: str = "",
    prompt_version: str | None = None,
) -> None:
    """Synchronous read-modify-write — MUST be called under the per-course
    lock (see write_cache_entry). Exposed primarily for tests that want
    to exercise the I/O path without async plumbing.

    ``prompt_version`` defaults to the current module-level
    ``_NOTE_PROMPT_VERSION``; callers may pass an explicit value to write
    a stale entry (used by tests to simulate prompt-evolution invalidation).
    """
    cache = load_cache(course_id)
    cache[source_file] = {
        "chunk_hash": chunk_hash_value,
        "content": content,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": model,
        "prompt_version": (
            prompt_version if prompt_version is not None else _NOTE_PROMPT_VERSION
        ),
    }
    save_cache(course_id, cache)


async def write_cache_entry(
    course_id: str,
    source_file: str,
    *,
    chunk_hash_value: str,
    content: str,
    model: str = "",
    prompt_version: str | None = None,
) -> None:
    """Update one cache entry, atomically rewriting the whole cache file.

    Wraps the read-modify-write in a per-course asyncio.Lock so concurrent
    callers for the SAME course don't drop each other's entries. The
    actual disk I/O runs via asyncio.to_thread so blocking writes don't
    stall the event loop.

    Cheap for typical course sizes (10–30 entries × a few KB each).
    """
    lock = _get_course_cache_lock(course_id)
    async with lock:
        await asyncio.to_thread(
            _write_cache_entry_unlocked,
            course_id,
            source_file,
            chunk_hash_value=chunk_hash_value,
            content=content,
            model=model,
            prompt_version=prompt_version,
        )


def prune_stale_cache(course_id: str, active_source_files: set[str]) -> int:
    """Drop cache entries for source_files no longer present in the course
    (e.g. user deleted a file and re-ingested). Returns the number of
    entries removed. No-op when nothing changed."""
    cache = load_cache(course_id)
    stale = [k for k in cache if k not in active_source_files]
    if not stale:
        return 0
    for k in stale:
        del cache[k]
    save_cache(course_id, cache)
    return len(stale)


def _group_chunks_by_file(chunks: list[Chunk]) -> dict[str, list[Chunk]]:
    """Stable groupby on source_file. Order = first-occurrence order in
    the chunks list, which mirrors ingest order (chunk_id is monotonic
    within a document, and documents are appended in upload order)."""
    groups: dict[str, list[Chunk]] = {}
    for c in chunks:
        sf = c.source_file or "untitled"
        groups.setdefault(sf, []).append(c)
    return groups


def plan_for_course(
    kb: "KBStore | Any",
    course_id: str,
    user_lang: str | None = None,
    *,
    force_refresh: bool = False,
) -> list[FilePlan]:
    """Build one FilePlan per source_file in the course. Returns [] when
    the course has no chunks — caller surfaces this as an error event.

    Incremental cache: when ``force_refresh`` is False (default), each
    plan's ``cached_content`` is populated from per_file_cache.json if
    the file's current chunk_hash matches the cached one. The endpoint
    then short-circuits the LLM call. ``force_refresh=True`` ignores the
    cache entirely — used by the explicit "regenerate from scratch" UI.

    The hash is computed over the CAPPED chunk list (post-MAX_CHUNKS_PER_FILE
    truncation) so changing the cap invalidates every cache entry — which
    is the safe behavior, since the prompt that produced the cached body
    saw a different chunk set.
    """
    chunks = kb.get_chunks(course_id)
    if not chunks:
        return []
    groups = _group_chunks_by_file(chunks)
    cache = {} if force_refresh else load_cache(course_id)

    plans: list[FilePlan] = []
    for idx, (source_file, file_chunks) in enumerate(groups.items()):
        capped = file_chunks[:MAX_CHUNKS_PER_FILE]
        cache_key = chunk_hash(capped)
        cached_content: str | None = None
        entry = cache.get(source_file)
        if entry and isinstance(entry, dict):
            stored_hash = entry.get("chunk_hash")
            stored_body = entry.get("content")
            stored_prompt_version = entry.get("prompt_version")
            # All three of (chunk_hash, prompt_version, content) must match
            # current state. Stale prompt_version → entry was produced by
            # an older prompt; regen so the LLM uses the current rubric.
            if (
                stored_hash == cache_key
                and stored_prompt_version == _NOTE_PROMPT_VERSION
                and isinstance(stored_body, str)
                and stored_body.strip()
            ):
                # Defense in depth: re-run the sanitizer on the cached body.
                # A tampered cache file (or one written by a buggy build
                # that bypassed the per-file sanitizer) MUST NOT ship
                # malicious LaTeX to the client / tectonic.
                try:
                    cached_content = check(stored_body)
                except LaTeXUnsafeError as e:
                    logger.warning(
                        "cache entry rejected for course=%s file=%s — "
                        "unsafe LaTeX: %s",
                        course_id, source_file, e.reason,
                    )
                    cached_content = None

        # LaTeX-output fix-all v3 #1: prime LLM with `\cite{}` not `[Source:]`.
        # Same fix as note_generator.prepare_inputs — without it the LLM
        # mirrored the markdown-flavoured [Source:] marker straight into the
        # output, dragging the rest of the response into markdown shape.
        source_text = "\n\n---\n\n".join(
            f"\\cite{{{c.source_file}:{c.location}}}\n{c.text}"
            for c in capped
        )
        prompt = prompts.NOTE_GENERATION_PROMPT.format(
            course_name=course_id,
            topic=f"Detailed notes for {source_file}",
            source_text=source_text,
            format_instructions=prompts.NOTE_FORMAT_LATEX,
        )
        system = prompts.NOTE_GENERATION_SYSTEM
        binding = prompts.USER_LANG_BINDING(user_lang)
        if binding:
            system = f"{system}\n\n{binding}"
        plans.append(FilePlan(
            idx=idx,
            source_file=source_file,
            chunk_count=len(capped),
            prompt=prompt,
            system=system,
            task_type="note_generation",
            temperature=PER_FILE_TEMPERATURE,
            max_tokens=PER_FILE_MAX_TOKENS,
            cache_key=cache_key,
            cached_content=cached_content,
        ))
    return plans


async def generate_file_stream(
    router: "ModelRouter | Any",
    plan: FilePlan,
    max_retries: int = 1,
):
    """Streaming variant of generate_file. Async-generator that yields
    ``("delta", str)`` tuples as the LLM produces tokens, followed by
    exactly one terminal ``("result", FileResult)``.

    Why this exists: ``generate_file`` blocks until the full per-file
    body returns, which makes the UI freeze for the 5-30s a typical
    LLM call takes. The user perceives Notes generation as flickering
    between "stuck silence" (file phase) and "smooth streaming"
    (review phase). Streaming the file phase too smooths the experience.

    Sanitization happens once at the end on the accumulated content —
    the same ``check()`` gate the non-stream path uses. Caller is
    responsible for caching the terminal result.

    2026-05-13: ``max_retries`` (default 1 = total 2 attempts) covers
    the proxy-side mid-stream TCP reset that codex.ysaikeji.cn issues
    when its per-tenant SSE concurrency cap fires. The retry re-runs
    ``router.complete_stream`` from scratch with a backoff and re-yields
    fresh deltas; the frontend's ``file_done.content`` overwrite
    invariant means the visible duplicate accumulated text gets
    replaced by the sanitized final body, so UX is "looks weird for a
    second, then snaps to the correct content" instead of a hard
    file_error.
    """
    partial = ""
    truncated = False
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        partial = ""
        truncated = False
        try:
            async for item in router.complete_stream(
                plan.prompt,
                task_type=plan.task_type,
                system=plan.system,
                temperature=plan.temperature,
                max_tokens=plan.max_tokens,
            ):
                # Truncation sentinel: upstream hit max_output_tokens. Keep
                # any partial content (the sanitizer still runs on it) but
                # tag the FileResult so the endpoint can surface a warning.
                if isinstance(item, TruncationSignal):
                    truncated = True
                    logger.warning(
                        "per-file note streaming truncated for %s (reason=%s)",
                        plan.source_file, item.reason,
                    )
                    continue
                if item:
                    partial += item
                    yield ("delta", item)
            break  # success
        except Exception as e:  # noqa: BLE001
            last_exc = e
            logger.warning(
                "per-file streaming failed attempt %d/%d for %s: %s",
                attempt + 1, max_retries + 1, plan.source_file,
                type(e).__name__,
            )
            if attempt < max_retries:
                # Linear backoff: 1.5s then 3.0s. Codex proxy mid-stream
                # resets are bursty; a short cool-down lets the per-tenant
                # SSE cap drain before we re-fire.
                await asyncio.sleep(1.5 + attempt * 1.5)
                continue
            # Final attempt failed — surface error to caller.
            logger.exception("per-file note streaming exhausted retries for %s",
                             plan.source_file)
            yield ("result", FileResult(
                idx=plan.idx, source_file=plan.source_file,
                chunk_count=plan.chunk_count,
                content=None, error=type(e).__name__,
                truncated=truncated,
            ))
            return
    content = partial.strip()
    if not content:
        yield ("result", FileResult(
            idx=plan.idx, source_file=plan.source_file,
            chunk_count=plan.chunk_count,
            content=None, error="empty_llm_response",
            truncated=truncated,
        ))
        return
    try:
        safe = check(content)
    except LaTeXUnsafeError as e:
        logger.warning("per-file sanitizer rejected %s: %s",
                       plan.source_file, e.reason)
        yield ("result", FileResult(
            idx=plan.idx, source_file=plan.source_file,
            chunk_count=plan.chunk_count,
            content=None, error=f"latex_unsafe: {e.reason}",
            truncated=truncated,
        ))
        return
    yield ("result", FileResult(
        idx=plan.idx, source_file=plan.source_file,
        chunk_count=plan.chunk_count,
        content=safe, error=None,
        truncated=truncated,
    ))


async def generate_file(
    router: "ModelRouter | Any",
    plan: FilePlan,
    semaphore: asyncio.Semaphore | None = None,
) -> FileResult:
    """LLM-generate the per-file note and validate via the sanitizer.

    When ``semaphore`` is provided, the LLM call runs inside its acquire so
    the caller can throttle concurrency from outside. Endpoints that need
    finer event ordering (emit file_start the moment a slot opens) can
    manage the semaphore themselves and pass ``None`` here.

    Catches all exceptions so one bad file can't sink the batch — caller
    decides whether to emit a file_done or a file_error event from the
    returned FileResult.
    """
    if semaphore is not None:
        async with semaphore:
            return await _generate_file_inner(router, plan)
    return await _generate_file_inner(router, plan)


async def _generate_file_inner(
    router: "ModelRouter | Any",
    plan: FilePlan,
) -> FileResult:
    try:
        resp = await router.complete(
            plan.prompt,
            task_type=plan.task_type,
            system=plan.system,
            temperature=plan.temperature,
            max_tokens=plan.max_tokens,
        )
        content = (resp.content or "").strip()
        if not content:
            return FileResult(
                idx=plan.idx, source_file=plan.source_file,
                chunk_count=plan.chunk_count,
                content=None, error="empty_llm_response",
            )
        try:
            safe = check(content)
        except LaTeXUnsafeError as e:
            logger.warning("per-file sanitizer rejected %s: %s",
                           plan.source_file, e.reason)
            return FileResult(
                idx=plan.idx, source_file=plan.source_file,
                chunk_count=plan.chunk_count,
                content=None, error=f"latex_unsafe: {e.reason}",
            )
        return FileResult(
            idx=plan.idx, source_file=plan.source_file,
            chunk_count=plan.chunk_count,
            content=safe, error=None,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("per-file note generation failed for %s",
                         plan.source_file)
        return FileResult(
            idx=plan.idx, source_file=plan.source_file,
            chunk_count=plan.chunk_count,
            content=None, error=type(e).__name__,
        )


def concat_draft(file_results: list[FileResult]) -> str:
    """Programmatic merge — wrap each succeeded file in \\section{<file>}
    and join in idx order. Failed files are skipped silently (their
    file_error event tells the user). Returns the empty string when
    nothing succeeded.
    """
    pieces: list[str] = []
    for r in sorted(file_results, key=lambda x: x.idx):
        if not r.content:
            continue
        safe_title = _escape_latex_title(r.source_file)
        pieces.append(f"\\section{{{safe_title}}}\n{r.content.strip()}\n")
    return "\n".join(pieces)


def _escape_latex_title(name: str) -> str:
    """Make a filename safe to drop inside \\section{...}.

    Strips directory components (chunk source_file is a relative path
    like ``uploaded/lecture3.pdf`` — only the leaf is useful as a heading)
    and escapes LaTeX-special characters. Unicode passes through unchanged
    so a Chinese filename renders correctly under xeCJK.
    """
    base = name.rsplit("/", 1)[-1]
    out = []
    for ch in base:
        if ch in "&%$#_{}":
            out.append("\\" + ch)
        elif ch == "\\":
            out.append("\\textbackslash{}")
        elif ch == "~":
            out.append("\\textasciitilde{}")
        elif ch == "^":
            out.append("\\textasciicircum{}")
        else:
            out.append(ch)
    return "".join(out)


def prepare_review_inputs(
    course_id: str,
    draft: str,
    file_count: int,
    user_lang: str | None = None,
) -> dict:
    """Build inputs for the single LLM review/polish pass.

    Shape matches note_generator.prepare_inputs so the endpoint can pipe
    ``router.complete_stream`` deltas straight through to NDJSON
    review_chunk events.
    """
    prompt = prompts.NOTE_MERGE_REVIEW_PROMPT.format(
        course_name=course_id,
        file_count=file_count,
        draft=draft,
        format_instructions=prompts.NOTE_FORMAT_LATEX,
    )
    system = prompts.NOTE_MERGE_REVIEW_SYSTEM
    binding = prompts.USER_LANG_BINDING(user_lang)
    if binding:
        system = f"{system}\n\n{binding}"
    return {
        "prompt": prompt,
        "system": system,
        "task_type": "note_generation",
        "temperature": REVIEW_TEMPERATURE,
        "max_tokens": REVIEW_MAX_TOKENS,
    }
