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
from nano_notebooklm.skills.latex_sanitizer import LaTeXUnsafeError, check
from nano_notebooklm.types import Chunk

if TYPE_CHECKING:  # avoid runtime import cycles — only used for type hints
    from nano_notebooklm.ai.router import ModelRouter
    from nano_notebooklm.kb.store import KBStore

logger = logging.getLogger(__name__)

# Cap chunks fed into a single per-file prompt. Big PDF lectures can exceed
# 100 chunks; at ~300 tokens each that's 30K+ input tokens before the
# instructions, which starts to crowd codex GPT-5.4's context window. 60
# captures the bulk of a typical 90-minute lecture without truncating
# anything important — chunks are slide-order, so we keep the start of the
# lecture (where the definitions live) and drop only the trailing exercises.
MAX_CHUNKS_PER_FILE = 60

DEFAULT_CONCURRENCY = 4
PER_FILE_MAX_TOKENS = 8192
REVIEW_MAX_TOKENS = 8192
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
    is non-None."""
    idx: int
    source_file: str
    chunk_count: int
    content: str | None
    error: str | None


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
    needs regen). Corrupt-file path logs at WARNING so an operator notices."""
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
    payload = json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True)
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


def write_cache_entry(course_id: str, source_file: str, *,
                      chunk_hash_value: str, content: str,
                      model: str = "") -> None:
    """Update one entry, atomically rewriting the whole cache file. Cheap
    for typical course sizes (10–30 entries × a few KB each)."""
    cache = load_cache(course_id)
    cache[source_file] = {
        "chunk_hash": chunk_hash_value,
        "content": content,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": model,
    }
    save_cache(course_id, cache)


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
            if stored_hash == cache_key and isinstance(stored_body, str) and stored_body.strip():
                cached_content = stored_body

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
