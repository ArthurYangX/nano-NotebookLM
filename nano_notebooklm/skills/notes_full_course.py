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
import logging
from dataclasses import dataclass

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
    """Prepared inputs for one per-file LLM call."""
    idx: int
    source_file: str
    chunk_count: int
    prompt: str
    system: str
    task_type: str
    temperature: float
    max_tokens: int


@dataclass(frozen=True)
class FileResult:
    """Outcome of one per-file LLM call. Exactly one of content/error
    is non-None."""
    idx: int
    source_file: str
    chunk_count: int
    content: str | None
    error: str | None


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
) -> list[FilePlan]:
    """Build one FilePlan per source_file in the course. Returns [] when
    the course has no chunks — caller surfaces this as an error event."""
    chunks = kb.get_chunks(course_id)
    if not chunks:
        return []
    groups = _group_chunks_by_file(chunks)

    plans: list[FilePlan] = []
    for idx, (source_file, file_chunks) in enumerate(groups.items()):
        capped = file_chunks[:MAX_CHUNKS_PER_FILE]
        source_text = "\n\n---\n\n".join(
            f"[Source: {c.source_file}, {c.location}]\n{c.text}"
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
