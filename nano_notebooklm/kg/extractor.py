"""LLM-based concept and relation extraction from text chunks.

R5-1 (2026-05-11): chapter roots. The course no longer has a single
`course_overview` root — each uploaded source file becomes its own root
(`concept_type="root"`, depth=0) so the student's mental model "one
chapter = one root" matches the visual graph. Stage A runs per file
(bounded by an `asyncio.Semaphore(3)` so a 20-file upload doesn't
fan out 20 simultaneous LLM calls); Stage B's chunk-level extraction
uses that file's topics as the parent_topic vocabulary.

  Stage A — `extract_course_overview_and_topics`
    One LLM call sees a single file's chunk heads and returns 3-5 macro
    topics + a one-line chapter overview. Topics become depth=1 Concept
    nodes (concept_type="topic"); topic_id encodes both the course slug
    and the file slug so two chapters with a same-named topic don't
    collide in the merger.

  Stage B — `extract_concepts_from_chunk`
    Per-chunk LLM call, fed the chunk's chapter topics. Each extracted
    concept declares which topic it belongs to via `parent_topic` (the
    topic's concept_id, resolved client-side from a per-file name map).
    Concepts that don't fit any topic get parent_topic=None and are
    later mounted under that file's chapter root.

  Orchestration — `extract_from_chunks`
    Group chunks by source_file → per-file Stage A (parallel, capped) →
    per-file Stage B (parallel batches) → synthesize one root per file
    with part-of edges leaf→topic→chapter_root. If a file's Stage A
    fails its chapter root still gets created with an empty overview;
    its leaves attach as orphans. If every file's Stage A fails the
    function falls back to legacy single-stage extraction (no roots,
    no topics) so the user still gets *something*.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
from typing import Any, Callable, Final, Literal


# fix-all v1 #A9: single source of truth for the R4-2 NDJSON `stage`
# field. Imported by api/server.py and grepped against by tests +
# frontend processing.jsx so a typo on either side breaks loudly.
UploadStage = Literal["extracting", "chunking", "embedding", "kg_stage_a", "kg_stage_b"]
# 2026-05-20: "extracting" split out of "chunking". Previously a single
# chunking stage covered both MinerU/PyMuPDF text extraction (slow:
# 4-10s/page on mineru CPU) AND the cheap pages → 1.5KB chunk
# segmentation (~ms/page). With the split, users see the slow part
# tick as its own stage and the second chunking bar move fast.
EXTRACTING: Final[str] = "extracting"
KG_STAGE_A: Final[str] = "kg_stage_a"
KG_STAGE_B: Final[str] = "kg_stage_b"
UPLOAD_STAGES: Final[tuple[str, ...]] = (
    "extracting", "chunking", "embedding", "kg_stage_a", "kg_stage_b",
)

from nano_notebooklm.ai import prompt_templates as prompts
from nano_notebooklm.ai.router import ModelRouter
from nano_notebooklm.types import Chunk, Concept, Relation

logger = logging.getLogger(__name__)


# ── Stage A ─────────────────────────────────────────────────────────


_MAX_HEADS = 30          # at most 30 chunk excerpts in the Stage A prompt
_HEAD_CHARS = 100        # truncate each excerpt to this many chars
# R5-1 fix-all v1 #F4: Stage A runs per chapter (not per course). A single
# lecture file rarely supports the M1 "5-9 topics for the whole course"
# expectation — the LLM pads or splits. The prompt now requests 3-5
# chapter-level topics; the clamp follows so under-counts don't trigger
# the legacy warning.
_TOPIC_MIN = 3
_TOPIC_MAX = 9
# R5-2 fix-all v6: Stage A timeout used to be a hard 15s ceiling, but
# codex cold-start + a 30-chunk-heads prompt routinely brushes past that
# (a user-reported 84-slide pptx upload landed with zero topic/root
# nodes because Stage A timed out → single-stage fallback). 30s default
# is still well below the "hung codex" threshold the original timeout
# was guarding against. Env-tunable for slower deployments.
_STAGE_A_TIMEOUT_SECONDS = float(os.getenv("KG_STAGE_A_TIMEOUT_SECONDS", "30"))
_STAGE_A_PARALLELISM = 3  # R5-1: per-file Stage A concurrency cap so a
                          # 20-file upload doesn't fan out 20 codex
                          # requests at once. Stage A is the only LLM
                          # call per chapter and is small (one prompt,
                          # ≤30 chunk heads), so 3-way concurrency keeps
                          # latency reasonable without flooding upstream.
# R5-2 fix-all v5: Stage B used to run `asyncio.gather(batch_size=5)` then
# barrier on the whole batch before starting the next 5 — so a slow
# straggler in batch i blocked the rest from kicking off. Flatten the
# pipeline by using a single semaphore + flat gather across all sampled
# chunks: every available worker grabs the next chunk the moment it's
# free, no batch barriers. 10 is the safe default (codex proxy comfortably
# handles ~10-15 rps); bump via env when running against a backend with
# higher throughput, or lower it on rate-limited deployments.
_STAGE_B_CONCURRENCY = int(os.getenv("KG_STAGE_B_CONCURRENCY", "10"))
_TOPIC_NAME_MAX = 80     # F9: cap topic name to bound prompt-injection
_TOPIC_DEF_MAX = 300     # F9: cap topic definition for the same reason
_TOPIC_BAD_CHARS = ("\n", "\r", "\t", "`")  # F9: strip control / fence chars


def _chapter_slug(filename: str) -> str:
    """R5-1 fix-all v1 #F3: produce a collision-resistant chapter-root id
    suffix from `filename`. Plain `_slug()` strips `.` and collapses
    whitespace, so `"lec 1.pdf"` / `"lec_1.pdf"` / `"LEC 1.PDF"` all hash
    to `lec_1pdf` and the merger's compound `(type, name, concept_id)` key
    silently fuses the two chapter roots into one. Appending an 8-char
    sha1 of the original filename disambiguates byte-distinct inputs that
    happen to alias under the lossy slug.
    """
    base = _slug(filename) or "chapter"
    digest = hashlib.sha1((filename or "").encode("utf-8")).hexdigest()[:8]
    return f"{base}_{digest}"


# R5-1 fix-all v1 #F11: user-uploaded filenames flow raw into the per-file
# Stage A prompt as `course "{course_name}"`, `- {source_file}` listing,
# and the `[source_file]` prefix of each chunk excerpt. _safe_upload_name
# upstream already strips C0 controls and bidi marks, but it permits
# backticks, quotes, braces, and newlines — all of which let a crafted
# filename break out of the prompt frame (`lec1.pdf"; SYSTEM: ignore prior
# instructions.pdf`). Cap length, strip control/fence chars before
# splicing. Mirrors _sanitize_topic_field's discipline for LLM *output*.
_FILENAME_PROMPT_BAD_CHARS = ("\n", "\r", "\t", "`", "{", "}", '"')
_FILENAME_PROMPT_MAX = 160  # bound length so a 4KB filename can't dominate the prompt


def _sanitize_filename_for_prompt(name: str) -> str:
    s = str(name or "").strip()
    for ch in _FILENAME_PROMPT_BAD_CHARS:
        s = s.replace(ch, " ")
    s = " ".join(s.split())
    if len(s) > _FILENAME_PROMPT_MAX:
        s = s[:_FILENAME_PROMPT_MAX].rstrip() + "…"
    return s


def _sanitize_topic_field(value: object, max_len: int) -> str:
    """F9: cap length, drop control/fence characters. Used on topic name
    and definition before the strings get re-injected into Stage B
    prompts. A poisoned PDF that emits a 200-char name with embedded
    `\\n``` ` could otherwise steer chunk-level extraction across the
    whole course."""
    s = str(value or "").strip()
    for ch in _TOPIC_BAD_CHARS:
        s = s.replace(ch, " ")
    s = " ".join(s.split())  # collapse whitespace
    if len(s) > max_len:
        s = s[:max_len].rstrip()
    return s


async def extract_course_overview_and_topics(
    course_id: str,
    course_name: str,
    source_files: list[str],
    sample_chunks: list[Chunk],
    router: ModelRouter,
) -> tuple[str, list[Concept], list[tuple[str, str]]]:
    """Stage A: ask the LLM for the course overview + 5-9 macro topics.

    Returns ``(overview, topics, prereq_edges)``. Topics are Concept
    objects with depth=1 and concept_type="topic", IDs prefixed
    `topic_{course_id}_`. ``prereq_edges`` carries pedagogical
    precedence as ``(earlier_topic_id, later_topic_id)`` pairs derived
    from the LLM's ``prerequisite_of`` field; pairs that reference
    unknown topic names are silently dropped. The caller assigns
    ``learning_order`` from these edges via ``topo_sort_topics``.

    Failure modes:
      - empty corpus (no chunks AND no files) → ('', [], []) without an LLM call
      - LLM raises → ('', [], []) so the caller can fall back to single-stage
      - LLM returns malformed JSON → ('', [], []) (caller falls back)
    """
    if not sample_chunks and not source_files:
        return "", [], []

    # Sample chunk heads. Shuffle so we don't bias toward early documents.
    sample = list(sample_chunks)
    if len(sample) > _MAX_HEADS:
        sample = random.sample(sample, _MAX_HEADS)
    # R5-1 fix-all v1 #F11: sanitize user-controlled filename + course
    # name before splicing into the LLM prompt. Strips backticks, braces,
    # quotes, newlines that would otherwise let a crafted upload break out
    # of the prompt frame and instruct the LLM directly.
    safe_course_name = _sanitize_filename_for_prompt(course_name)
    chunk_heads = "\n".join(
        f"- [{_sanitize_filename_for_prompt(c.source_file)}] "
        f"{(c.text or '').strip()[:_HEAD_CHARS]}"
        for c in sample
    )
    files_block = "\n".join(
        f"- {_sanitize_filename_for_prompt(f)}" for f in source_files[:50]
    ) or "(no files listed)"

    prompt = prompts.MACRO_TOPICS_PROMPT.format(
        course_name=safe_course_name,
        source_files=files_block,
        chunk_heads=chunk_heads or "(no chunk excerpts)",
    )

    try:
        data = await asyncio.wait_for(
            router.complete_structured(
                prompt,
                task_type="concept_extraction",
                system=prompts.MACRO_TOPICS_SYSTEM,
                temperature=0.2,
            ),
            timeout=_STAGE_A_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Stage A (macro topics) timed out for %s after %ss; "
            "falling back to single-stage extraction",
            course_id, _STAGE_A_TIMEOUT_SECONDS,
        )
        return "", [], []
    except Exception as exc:  # noqa: BLE001 — Stage A is allowed to fail soft
        # R5-1 fix-all v1 #F10: scrub exception body — openai-python errors
        # echo the request body which carries chunk excerpts (user content).
        logger.warning(
            "Stage A (macro topics) failed for %s: %s",
            course_id, getattr(exc, "code", type(exc).__name__),
        )
        return "", [], []

    if not isinstance(data, dict):
        return "", [], []

    overview = str(data.get("course_overview", "")).strip()
    raw_topics = data.get("topics", [])
    if not isinstance(raw_topics, list):
        return overview, [], []

    topics: list[Concept] = []
    seen_ids: set[str] = set()
    for raw in raw_topics[: _TOPIC_MAX * 2]:  # cap before dedup
        if not isinstance(raw, dict):
            continue
        # F9: sanitize name + definition before they re-enter Stage B prompts.
        name = _sanitize_topic_field(raw.get("name", ""), _TOPIC_NAME_MAX)
        if not name:
            continue
        topic_id = f"topic_{course_id}_{_slug(name)}"
        if topic_id in seen_ids:
            continue
        seen_ids.add(topic_id)
        try:
            weight = float(raw.get("weight", 5.0))
        except (TypeError, ValueError):
            weight = 5.0
        weight = max(1.0, min(10.0, weight))
        topics.append(Concept(
            concept_id=topic_id,
            name=name,
            definition=_sanitize_topic_field(
                raw.get("summary", raw.get("definition", "")), _TOPIC_DEF_MAX,
            ),
            concept_type="topic",
            course_ids=[course_id],
            chunk_ids=[],
            depth=1,
            weight=weight,
            source_chunks=[],
            parent_topic=None,
        ))

    if len(topics) > _TOPIC_MAX:
        topics = sorted(topics, key=lambda t: -t.weight)[:_TOPIC_MAX]
    if 0 < len(topics) < _TOPIC_MIN:
        # Fewer than expected — keep what we have; the orchestrator will
        # decide whether to fall back to single-stage based on emptiness.
        logger.info(
            "Stage A returned only %d topic(s) for %s (expected %d-%d)",
            len(topics), course_id, _TOPIC_MIN, _TOPIC_MAX,
        )

    # R3-3: parse prerequisite_of pairs into topic-id edges. We resolve by
    # exact topic name (post-sanitization) to whatever survived the trim
    # above. Pairs that name a dropped or unknown topic are silently
    # discarded — old fixtures without the field still produce [].
    name_to_id = {t.name: t.concept_id for t in topics}
    raw_prereq = data.get("prerequisite_of", [])
    prereq_edges: list[tuple[str, str]] = []
    if isinstance(raw_prereq, list):
        for raw in raw_prereq:
            if not isinstance(raw, dict):
                continue
            src_name = _sanitize_topic_field(raw.get("from", ""), _TOPIC_NAME_MAX)
            dst_name = _sanitize_topic_field(raw.get("to", ""), _TOPIC_NAME_MAX)
            src_id = name_to_id.get(src_name)
            dst_id = name_to_id.get(dst_name)
            if src_id and dst_id and src_id != dst_id:
                prereq_edges.append((src_id, dst_id))

    return overview, topics, prereq_edges


# ── Stage B — chunk-level extraction ────────────────────────────────


async def extract_concepts_from_chunk(
    chunk: Chunk,
    course_name: str,
    router: ModelRouter,
    topics: list[Concept] | None = None,
) -> tuple[list[Concept], list[Relation]]:
    """Extract concepts and relations from a single chunk via LLM.

    If `topics` is provided (Stage A succeeded), they're listed in the
    prompt and each extracted concept is expected to declare which topic
    it belongs to via `parent_topic` (matched by EXACT name). Unmatched
    parent_topic strings are dropped to None — the orchestrator mounts
    those leaves under the course root rather than under a wrong topic.
    """
    if topics:
        topics_listing = "\n".join(f"- {t.name}: {t.definition}" for t in topics)
        topics_block = prompts.CONCEPT_EXTRACTION_TOPICS_BLOCK.format(
            topics_listing=topics_listing,
        )
    else:
        topics_block = ""

    prompt = prompts.CONCEPT_EXTRACTION_PROMPT.format(
        course_name=course_name,
        chunk_text=chunk.text,
        topics_block=topics_block,
    )

    try:
        data = await router.complete_structured(
            prompt,
            task_type="concept_extraction",
            system=prompts.CONCEPT_EXTRACTION_SYSTEM,
            temperature=0.2,
        )
    except Exception as e:  # noqa: BLE001 — chunk-level failures are tolerated
        logger.warning(f"Concept extraction failed for {chunk.chunk_id}: {e}")
        return [], []

    if not isinstance(data, dict):
        return [], []

    name_to_topic_id = {t.name: t.concept_id for t in (topics or [])}

    concepts: list[Concept] = []
    raw_concepts = data.get("concepts", [])
    if isinstance(raw_concepts, list):
        for rc in raw_concepts:
            if not isinstance(rc, dict):
                continue
            name = str(rc.get("name", "")).strip()
            if not name:
                continue
            raw_parent = rc.get("parent_topic")
            parent_topic_id = name_to_topic_id.get(str(raw_parent).strip()) if raw_parent else None
            depth = _depth_for_type(rc.get("type", "definition"))
            # Leaf concepts always sit below depth=1 topics.
            if depth < 2:
                depth = 2
            concepts.append(Concept(
                concept_id=f"concept_{course_name}_{_slug(name)}",
                name=name,
                definition=str(rc.get("definition", "")).strip(),
                concept_type=str(rc.get("type", "definition")).lower(),
                course_ids=[chunk.course_id],
                chunk_ids=[chunk.chunk_id],
                depth=depth,
                weight=_weight_for_concept(rc, chunk),
                source_chunks=[{
                    "chunk_id": chunk.chunk_id,
                    "source_file": chunk.source_file,
                    "location": chunk.location,
                    # 2026-05-13: include both `page` (pdf/docx) and
                    # `slide` (pptx) so the frontend's PDF-preview deep
                    # link can fall back to slide-N when page is null.
                    # Previously pptx courses had every KG citation
                    # land on PDF page 1 because the serializer only
                    # carried `page`, and pptx chunks have page=None.
                    "page": chunk.page,
                    "slide": chunk.slide,
                }],
                parent_topic=parent_topic_id,
            ))

    relations: list[Relation] = []
    raw_relations = data.get("relations", [])
    if isinstance(raw_relations, list):
        for rr in raw_relations:
            if not isinstance(rr, dict):
                continue
            source = str(rr.get("source", "")).strip()
            target = str(rr.get("target", "")).strip()
            if source and target:
                relations.append(Relation(
                    source=f"concept_{course_name}_{_slug(source)}",
                    target=f"concept_{course_name}_{_slug(target)}",
                    relation_type=_normalize_relation(rr.get("type", "related")),
                ))

    return concepts, relations


# ── Orchestration ───────────────────────────────────────────────────


def _concept_embed_text(c: Concept, chunk_text_lookup: dict[str, str] | None = None) -> str:
    """Text fed to the embedding model for a concept node.

    2026-05-13 cross-lingual fix: pre-fix the text was just `name。definition`,
    both of which inherit the SOURCE document's language. A KG built from an
    English slide deck ends up with concept names like "Self-Attention" and
    English definitions — when a Chinese-speaking user queries "什么是
    attention机制", the all-MiniLM-L6-v2 embedding (English-leaning) ranks
    Chinese-named concepts from sibling chapters higher than the actual
    English match, so GraphRAG silently routes to the wrong file.

    The fix: when chunk text lookup is available (provided by extractor +
    graph_search lazy-recompute), append up to ~300 chars of the FIRST
    source chunk's text. This grounds the concept embedding in the real
    domain vocabulary that appears in the source — "Self-Attention" gets
    the surrounding "Transformer Positional Encoding Multi-Head Q K V
    softmax attention weights..." context, which materially raises the
    cosine for any query touching the same vocab, regardless of language.

    Truncate the whole package at 800 chars (was 600) — slight bump for
    the added context. Still cheap on batch-embed runtime.
    """
    name = (c.name or "").strip()
    definition = (c.definition or "").strip()
    text = f"{name}。{definition}" if definition else name
    if chunk_text_lookup:
        for sc in (c.source_chunks or [])[:2]:
            cid = sc.get("chunk_id") if isinstance(sc, dict) else None
            if not cid:
                continue
            chunk_text = chunk_text_lookup.get(cid)
            if not chunk_text:
                continue
            excerpt = " ".join(chunk_text.split())[:300]
            if excerpt:
                text = f"{text} | {excerpt}"
                break  # one excerpt is enough; more dilutes the concept signal
    return text[:800]


async def extract_from_chunks(
    chunks: list[Chunk],
    course_name: str,
    router: ModelRouter,
    max_chunks: int = 50,
    progress_callback=None,
    embed_fn: Callable[[list[str]], Any] | None = None,
) -> tuple[list[Concept], list[Relation]]:
    """Two-stage extraction, chapter-rooted (R5-1).

    Group chunks by source_file. For each file: Stage A (chapter overview
    + 3-5 macro topics, one LLM call, run in parallel across files with
    a small concurrency cap) → Stage B (per-chunk concept extraction
    using that file's topics, batched). Synthesize one root per file
    with concept_type="root" / depth=0, and wire part-of edges
    leaf→topic→chapter_root + cross-topic depends-on within each file.

    If every file's Stage A fails or yields no topics, falls back to
    legacy single-stage extraction (no roots, no topics, just per-chunk
    concepts) so we don't regress on a fully-degraded LLM. A partial
    failure (some files succeed, some fail) still produces chapter
    roots for the successful files; the failed files get a root with
    empty overview and all their leaves attach as orphans.

    R4-2: ``progress_callback`` (optional) is called as
    ``progress_callback(stage, percent)`` where ``stage`` is one of
    ``"kg_stage_a"``/``"kg_stage_b"`` and ``percent`` is 0–100. Server-side
    upload streaming uses this to drive a live progress bar; existing
    callers that omit it are unaffected.

    fix-all v1 #A10: callback exceptions are caught here so a misbehaving
    telemetry hook can't abort extraction — the pipeline is the
    consumer of truth, telemetry is best-effort.
    """
    def _emit(stage: str, pct: int) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback(stage, pct)
        except Exception:  # noqa: BLE001 — telemetry must not abort extraction
            logger.warning("progress_callback raised for stage=%s pct=%d; suppressed", stage, pct, exc_info=True)

    if not chunks:
        return [], []

    sampled = chunks if len(chunks) <= max_chunks else random.sample(chunks, max_chunks)

    # R5-1: group chunks by source_file. R5-1 fix-all v1 #F8: bucket key
    # is computed by `_chunk_bucket_key` and used both at bucketing time
    # AND at orphan-leaf routing time, so an empty/None source_file goes
    # consistently into the "(unknown)" bucket on both sides — pre-fix
    # the routing key was computed as `or None` and missed the bucket.
    def _chunk_bucket_key(source_file: object) -> str:
        s = str(source_file or "").strip()
        return s if s else "(unknown)"

    chunks_by_file: dict[str, list[Chunk]] = {}
    for c in sampled:
        chunks_by_file.setdefault(_chunk_bucket_key(c.source_file), []).append(c)
    files_ordered: list[str] = sorted(chunks_by_file.keys())

    # ── Stage A per file (parallel, semaphore-capped) ──────────────────
    # R5-1 fix-all v1 #F7: emit progress per file completion (not just 0
    # / 100) so a 20-file upload's progress bar advances during Stage A.
    # The semaphore caps concurrency; the counter is updated under a
    # tiny asyncio.Lock so the emitted percentage is monotonic.
    _emit(KG_STAGE_A, 0)
    sem = asyncio.Semaphore(_STAGE_A_PARALLELISM)
    stage_a_done = 0
    stage_a_total = max(1, len(files_ordered))
    stage_a_lock = asyncio.Lock()

    async def _stage_a_for_file(filename: str) -> tuple[str, list[Concept], list[tuple[str, str]]]:
        nonlocal stage_a_done
        # course_id encodes course slug + chapter-slug-with-hash so
        # `_slug` collisions on similar filenames (e.g. "lec 1.pdf"
        # / "lec_1.pdf") don't fuse into one root via the merger's
        # (type, name, concept_id) key. R5-1 fix-all v1 #F3.
        scoped_course_id = f"{course_name}__{_chapter_slug(filename)}"
        try:
            async with sem:
                result = await extract_course_overview_and_topics(
                    course_id=scoped_course_id,
                    course_name=filename,
                    source_files=[filename],
                    sample_chunks=chunks_by_file[filename],
                    router=router,
                )
        finally:
            # R5-2 review-swarm v2 follow-up F2: emit progress AFTER releasing
            # the semaphore slot so a slow / blocking progress_callback (e.g.
            # upload pipeline's NDJSON queue with backpressure) can't pin a
            # worker slot — defeats the per-file concurrency win.
            pct = None
            async with stage_a_lock:
                stage_a_done += 1
                pct = max(1, min(99, int(100 * stage_a_done / stage_a_total)))
            if pct is not None:
                _emit(KG_STAGE_A, pct)
        return result

    stage_a_results = await asyncio.gather(
        *[_stage_a_for_file(f) for f in files_ordered],
        return_exceptions=True,
    )
    # 2026-05-20: don't emit 100 here — Stage A's per-file accumulation +
    # topo_sort still runs below. Emit truthfully after that.

    per_file_overview: dict[str, str] = {}
    per_file_topics: dict[str, list[Concept]] = {}
    per_file_prereq: dict[str, list[tuple[str, str]]] = {}
    for filename, result in zip(files_ordered, stage_a_results):
        if isinstance(result, Exception):
            # R5-1 fix-all v1 #F10: scrub exception body before logging.
            # `str(exc)` on openai-python errors echoes the request which
            # includes the prompt (user-supplied chunk excerpts). Mirror
            # R4-4 fix-all v2 V5: log a structured code, not the body.
            logger.warning(
                "Stage A failed for chapter %r: %s",
                filename, getattr(result, "code", type(result).__name__),
            )
            per_file_overview[filename] = ""
            per_file_topics[filename] = []
            per_file_prereq[filename] = []
            continue
        overview, topics, prereq = result
        per_file_overview[filename] = overview
        per_file_topics[filename] = topics
        per_file_prereq[filename] = prereq

    # R3-3 carried over: assign learning_order per chapter from that
    # chapter's prereq edges. Empty prereq → all topics keep learning_order
    # =None so the frontend doesn't draw a badge.
    from nano_notebooklm.kg.graph import topo_sort_topics
    for filename in files_ordered:
        topics = per_file_topics[filename]
        prereq = per_file_prereq[filename]
        if topics and prereq:
            ordered = topo_sort_topics(
                [t.concept_id for t in topics],
                prereq,
                weights={t.concept_id: t.weight for t in topics},
            )
            position = {tid: i + 1 for i, tid in enumerate(ordered)}
            for t in topics:
                t.learning_order = position.get(t.concept_id)

    # 2026-05-20: Stage A truthful 100% — fire AFTER per-file accumulation
    # + topo_sort, not the moment `asyncio.gather` returns. The window is
    # small (~ms) but it's still a lie of the same shape as Stage B's.
    _emit(KG_STAGE_A, 100)

    # ── Stage B fully pipelined (R5-2 fix-all v5 A) ────────────────────
    # R5-1 fix-all v1 #F2 already flattened the outer per-file loop, but
    # the inner `batch_size=5` loop still imposed a barrier: 5 chunks
    # launch, await ALL 5, advance — a single straggler made the next 5
    # chunks wait. For a 30-chunk upload with one slow chunk per batch
    # that's ~6 stall windows.
    #
    # Now: one `asyncio.Semaphore(_STAGE_B_CONCURRENCY)` (default 10,
    # env-tunable via `KG_STAGE_B_CONCURRENCY`) plus a flat
    # `asyncio.gather` over every sampled chunk. Each worker grabs the
    # next chunk as soon as it finishes its current one — no barriers.
    # Progress is reported per-chunk-completion under a tiny lock so the
    # emitted % stays monotonic even though tasks complete out of order.
    _emit(KG_STAGE_B, 0)
    all_concepts: list[Concept] = []
    all_relations: list[Relation] = []
    stage_b_sem = asyncio.Semaphore(_STAGE_B_CONCURRENCY)
    stage_b_total = max(1, len(sampled))
    stage_b_done = 0
    stage_b_lock = asyncio.Lock()

    async def _stage_b_for_chunk(c: Chunk):
        nonlocal stage_b_done
        # R5-2 review-swarm v2 follow-up F2: emit progress AFTER releasing
        # the semaphore. Holding the slot during `_emit` (which may push
        # to the upload pipeline's bounded `asyncio.Queue(maxsize=64)`)
        # lets backpressure serialise the workers behind the lock —
        # partially defeats the flat-gather pipelining win.
        try:
            async with stage_b_sem:
                result = await extract_concepts_from_chunk(
                    c, course_name, router,
                    topics=per_file_topics.get(_chunk_bucket_key(c.source_file), []),
                )
        finally:
            pct = None
            async with stage_b_lock:
                stage_b_done += 1
                pct = max(1, min(99, int(100 * stage_b_done / stage_b_total)))
            if pct is not None:
                _emit(KG_STAGE_B, pct)
        return result

    tasks = [_stage_b_for_chunk(c) for c in sampled]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            # Same PII-scrub discipline as Stage A.
            logger.warning(
                "Batch extraction error: %s",
                getattr(result, "code", type(result).__name__),
            )
            continue
        concepts, relations = result
        all_concepts.extend(concepts)
        all_relations.extend(relations)
    # 2026-05-20: Stage B truthful 100% — DON'T emit 100 here. The
    # concept-embedding pass below + chapter-root synthesis still run for
    # 5-30s. We emit 80 to signal "gather done", then 95 after the
    # embedding pass. Server.py emits 100 only after merge + KG build +
    # disk save — i.e. when "done" really means done. Without this fix
    # the frontend modal hits Stage B 100% while server status is still
    # "running" for tens of seconds, leaving the UI visibly stuck until
    # a hard reload.
    _emit(KG_STAGE_B, 80)

    # R4-4: cache concept_embedding on every non-root concept so graph_search
    # can cosine-rank against the query without recomputing. Folded into
    # Stage B (no new NDJSON stage emitted) to preserve R4-2's 4-stage upload
    # contract — frontend processing.jsx sees Stage B hit 100% and the
    # embedding pass runs in the silence between Stage B 100% and the `done`
    # event. Lazy fallback in graph_search covers any concepts that miss this
    # pass (legacy KGs, embed_fn failures, dimension mismatches).
    #
    # fix-all v1 #A2 (R4-4 review-swarm): the embed_fn call is synchronous
    # (sentence-transformer forward or HTTP /embeddings) and was running on
    # the event loop, stalling R4-2's NDJSON queue-drain for 300–1000 ms.
    # Off-load to a worker thread so other concurrent requests keep moving.
    all_topics_flat: list[Concept] = [t for f in files_ordered for t in per_file_topics[f]]
    targets: list[Concept] = [*all_topics_flat, *all_concepts]
    if embed_fn is not None and targets:
        # 2026-05-13 cross-lingual fix: thread a chunk_id → text lookup so
        # `_concept_embed_text` can append a short excerpt of the source
        # chunk to the embedding text. Anchors concept embeddings in
        # real domain vocabulary so cross-language queries still hit.
        chunk_text_lookup = {c.chunk_id: c.text for c in chunks}
        try:
            texts = [_concept_embed_text(c, chunk_text_lookup) for c in targets]
            # fix-all v2 LOW F8: batch the embed_fn call and interpolate
            # Stage B progress between batches. The pass is one atomic
            # call covering 30-200 concepts (~10s/100 local, 5-15s/batch
            # over API); pre-fix the bar visibly stalled at 80% for the
            # whole duration. 32 is conservative — bump via env on
            # high-throughput local backends. Trailing `_emit(KG_STAGE_B,
            # 95)` outside the if-block still covers the no-embed-fn /
            # no-targets path.
            BATCH = int(os.getenv("KG_STAGE_B_EMBED_BATCH", "32"))
            BATCH = max(1, BATCH)
            n_batches = max(1, (len(texts) + BATCH - 1) // BATCH)
            all_embs: list = []
            for bi in range(n_batches):
                batch = texts[bi * BATCH : (bi + 1) * BATCH]
                batch_embs = await asyncio.to_thread(embed_fn, batch)
                all_embs.extend(batch_embs)
                # Interpolate Stage B 80 → 95 across the embedding pass
                # so the bar reflects real progress. Cap at 95 so the
                # caller's post-save `_set_stage(KG_STAGE_B, 100)`
                # remains the truth-of-done emitter.
                pct = 80 + int((95 - 80) * (bi + 1) / n_batches)
                _emit(KG_STAGE_B, min(95, pct))
            # embed_fn returns either np.ndarray (shape [n, d]) or list[list[float]].
            for c, emb in zip(targets, all_embs):
                c.concept_embedding = [float(x) for x in emb]
        except Exception:  # noqa: BLE001 — embedding failure is non-fatal
            logger.warning(
                "concept_embedding batch failed for %d concepts; graph_search "
                "will fall back to lazy per-query embedding",
                len(targets), exc_info=True,
            )

    if not all_topics_flat:
        # Fallback path — every file's Stage A produced nothing. Return
        # per-chunk concepts only, exactly like the pre-M1 implementation,
        # so the frontend still gets something. F20: bumped to warning so a
        # degraded mindmap is visible in operator triage.
        logger.warning(
            "Stage A empty across all %d files for %s; returning %d single-stage "
            "concepts (no chapter roots, no macro topics — investigate LLM output)",
            len(files_ordered), course_name, len(all_concepts),
        )
        # Truthful 95% emit on the early-return path too — server.py
        # finishes to 100 after disk save.
        _emit(KG_STAGE_B, 95)
        return all_concepts, all_relations

    # ── Synthesize one root per file + wire edges ──────────────────────
    roots: list[Concept] = []
    topic_to_root_edges: list[Relation] = []
    leaf_edges: list[Relation] = []
    orphan_edges: list[Relation] = []
    prereq_relations: list[Relation] = []

    # Map topic_id → its chapter root id (lookup for parent_topic resolution)
    topic_id_to_root: dict[str, str] = {}
    # Map source_file → root id (lookup for orphan attachment)
    filename_to_root: dict[str, str] = {}

    for filename in files_ordered:
        topics = per_file_topics[filename]
        # Even files where Stage A failed get a root — so their leaves
        # still have somewhere to land (and so the student sees all
        # their chapters in the graph even if topic extraction sputtered).
        if not topics and not chunks_by_file.get(filename):
            continue
        # R5-1 fix-all v1 #F3: chapter_slug appends a sha1[:8] of the raw
        # filename so two distinct files with collision-prone slugs
        # ("lec 1.pdf" / "lec_1.pdf") get distinct root ids.
        root_id = f"root_{course_name}__{_chapter_slug(filename)}"
        # Strip directory prefixes from the display label so it reads as
        # a chapter title; the underlying source_chunks still carry the
        # full path for citation routing.
        display_label = filename.rsplit("/", 1)[-1] or filename
        root = Concept(
            concept_id=root_id,
            name=display_label,
            definition=per_file_overview.get(filename, ""),
            concept_type="root",
            course_ids=[course_name],
            chunk_ids=[],
            depth=0,
            weight=10.0,
            source_chunks=[],
            parent_topic=None,
        )
        roots.append(root)
        filename_to_root[filename] = root_id

        for t in topics:
            topic_to_root_edges.append(Relation(
                source=t.concept_id, target=root_id, relation_type="part-of",
            ))
            topic_id_to_root[t.concept_id] = root_id

        for src_topic, dst_topic in per_file_prereq.get(filename, []):
            prereq_relations.append(Relation(
                source=dst_topic, target=src_topic, relation_type="depends-on",
            ))

    # Wire leaves: prefer the LLM-declared parent_topic (per-file scoped);
    # if it's missing/unknown, fall back to the leaf's own source_file
    # root. R5-1 fix-all v1 #F8+F9: route by the SAME `_chunk_bucket_key`
    # used at bucketing time so empty/None source_file ends up in the
    # "(unknown)" bucket on both sides. Pre-fix, mismatch caused such
    # leaves to silently graft onto `roots[0]` (the alphabetically-first
    # chapter), creating a cross-chapter attribution bug. If even the
    # "(unknown)" bucket has no root (shouldn't happen given the loop
    # above, but defensively), we now drop the leaf-edge entirely rather
    # than misattribute it to an unrelated chapter.
    for c in all_concepts:
        if c.parent_topic and c.parent_topic in topic_id_to_root:
            leaf_edges.append(Relation(
                source=c.concept_id, target=c.parent_topic, relation_type="part-of",
            ))
            continue
        leaf_file_raw = None
        if c.source_chunks:
            leaf_file_raw = c.source_chunks[0].get("source_file")
        target_root = filename_to_root.get(_chunk_bucket_key(leaf_file_raw))
        if target_root:
            orphan_edges.append(Relation(
                source=c.concept_id, target=target_root, relation_type="part-of",
            ))
        # else: no chapter root resolved (e.g. leaf has empty
        # source_chunks). Skip the edge rather than misattribute; the
        # leaf node still exists but renders unparented.

    logger.info(
        "Chapter-rooted extraction: %d roots + %d topics + %d concepts (%d orphans), "
        "%d edges from %d chunks across %d files",
        len(roots), len(all_topics_flat), len(all_concepts), len(orphan_edges),
        len(topic_to_root_edges) + len(leaf_edges) + len(orphan_edges)
            + len(all_relations) + len(prereq_relations),
        len(sampled), len(files_ordered),
    )

    # 2026-05-20: Stage B has finished the embedding pass + chapter-root
    # synthesis here; only merge / KG build / disk save remain in the
    # caller (server.py). Emit 95 so the bar reflects "most of Stage B
    # done, finalizing" — the caller emits 100 after the last byte hits
    # disk.
    _emit(KG_STAGE_B, 95)

    return (
        roots + all_topics_flat + all_concepts,
        topic_to_root_edges + leaf_edges + orphan_edges + prereq_relations + all_relations,
    )


# ── Helpers ─────────────────────────────────────────────────────────


def _slug(name: str) -> str:
    """Create a URL-safe slug from a concept name."""
    import re
    slug = re.sub(r"[^\w\s-]", "", name.lower())
    slug = re.sub(r"[\s_]+", "_", slug).strip("_")
    return slug[:50]


def _depth_for_type(concept_type: str) -> int:
    t = str(concept_type or "").lower()
    if t in {"course", "topic", "chapter"}:
        return 1
    if t in {"definition", "theorem", "algorithm"}:
        return 2
    return 2


def _weight_for_concept(raw: dict[str, Any], chunk: Chunk) -> float:
    score = raw.get("weight", raw.get("importance", 1.0))
    try:
        score = float(score)
    except (TypeError, ValueError):
        score = 1.0
    name_len = len(str(raw.get("name", "")).split())
    return max(1.0, min(10.0, score + min(name_len, 4) * 0.25))


def _normalize_relation(relation_type: str) -> str:
    value = str(relation_type or "related").lower().replace("_", "-")
    aliases = {
        "prerequisite": "depends-on",
        "prerequisite-of": "depends-on",
        "part_of": "part-of",
        "related-to": "related",
        "definition-of": "is-a",
        "type-of": "is-a",
    }
    value = aliases.get(value, value)
    return value if value in {"is-a", "part-of", "depends-on", "example-of", "related"} else "related"
