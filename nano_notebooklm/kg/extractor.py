"""LLM-based concept and relation extraction from text chunks.

M1 (2026-05-06): two-stage extraction.

  Stage A — `extract_course_overview_and_topics`
    One LLM call sees a corpus-wide sample (file list + chunk heads) and
    returns 5-9 macro topics + a one-line course overview. Topics become
    depth=1 Concept nodes (concept_type="topic").

  Stage B — `extract_concepts_from_chunk`
    Per-chunk LLM call, fed the Stage A topics. Each extracted concept
    declares which topic it belongs to via `parent_topic` (the topic's
    concept_id, resolved client-side). Concepts that don't fit any topic
    get parent_topic=None and are later mounted under the course root.

  Orchestration — `extract_from_chunks`
    Stage A → Stage B (parallel, batched) → synthesize an explicit course
    root Concept (depth=0) plus part-of relations leaf→topic and
    topic→root. If Stage A fails or yields no topics the function falls
    back to legacy single-stage extraction so the user still gets *some*
    KG instead of an empty payload.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Final, Literal


# fix-all v1 #A9: single source of truth for the R4-2 NDJSON `stage`
# field. Imported by api/server.py and grepped against by tests +
# frontend processing.jsx so a typo on either side breaks loudly.
UploadStage = Literal["chunking", "embedding", "kg_stage_a", "kg_stage_b"]
KG_STAGE_A: Final[str] = "kg_stage_a"
KG_STAGE_B: Final[str] = "kg_stage_b"
UPLOAD_STAGES: Final[tuple[str, ...]] = ("chunking", "embedding", "kg_stage_a", "kg_stage_b")

from nano_notebooklm.ai import prompt_templates as prompts
from nano_notebooklm.ai.router import ModelRouter
from nano_notebooklm.types import Chunk, Concept, Relation

logger = logging.getLogger(__name__)


# ── Stage A ─────────────────────────────────────────────────────────


_MAX_HEADS = 30          # at most 30 chunk excerpts in the Stage A prompt
_HEAD_CHARS = 100        # truncate each excerpt to this many chars
_TOPIC_MIN = 5
_TOPIC_MAX = 9
_STAGE_A_TIMEOUT_SECONDS = 15.0  # F3: hard ceiling so a hung codex call
                                  # can't block /api/mindmap indefinitely
_TOPIC_NAME_MAX = 80     # F9: cap topic name to bound prompt-injection
_TOPIC_DEF_MAX = 300     # F9: cap topic definition for the same reason
_TOPIC_BAD_CHARS = ("\n", "\r", "\t", "`")  # F9: strip control / fence chars


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
    chunk_heads = "\n".join(
        f"- [{c.source_file}] {(c.text or '').strip()[:_HEAD_CHARS]}"
        for c in sample
    )
    files_block = "\n".join(f"- {f}" for f in source_files[:50]) or "(no files listed)"

    prompt = prompts.MACRO_TOPICS_PROMPT.format(
        course_name=course_name,
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
        logger.warning("Stage A (macro topics) failed for %s: %s", course_id, exc)
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
                    "page": chunk.page,
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


async def extract_from_chunks(
    chunks: list[Chunk],
    course_name: str,
    router: ModelRouter,
    max_chunks: int = 50,
    progress_callback=None,
) -> tuple[list[Concept], list[Relation]]:
    """Two-stage extraction.

    Stage A: macro topics from a corpus-wide sample (one LLM call).
    Stage B: per-chunk concept extraction with the Stage A topics in
             context. Concepts attach to topics; topics attach to a
             synthesized course root (depth=0).

    If Stage A fails or yields no topics, falls back to legacy single-stage
    extraction (no root, no topics, just per-chunk concepts) so we don't
    regress Round 1 behavior on this code path.

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
    source_files = sorted({c.source_file for c in chunks if c.source_file})

    # Stage A
    _emit(KG_STAGE_A, 0)
    overview, topics, prereq_edges = await extract_course_overview_and_topics(
        course_id=course_name,
        course_name=course_name,
        source_files=source_files,
        sample_chunks=sampled,
        router=router,
    )
    _emit(KG_STAGE_A, 100)

    # R3-3: assign topological learning_order to topics. Empty prereq_edges
    # → leave learning_order=None on every topic so the frontend doesn't
    # draw badges; mirrors legacy fixtures and the no-prerequisite fallback.
    if topics and prereq_edges:
        from nano_notebooklm.kg.graph import topo_sort_topics
        ordered = topo_sort_topics(
            [t.concept_id for t in topics],
            prereq_edges,
            weights={t.concept_id: t.weight for t in topics},
        )
        position = {tid: i + 1 for i, tid in enumerate(ordered)}
        for t in topics:
            t.learning_order = position.get(t.concept_id)

    # Stage B in batches of 5 — same concurrency profile as before.
    _emit(KG_STAGE_B, 0)
    batch_size = 5
    all_concepts: list[Concept] = []
    all_relations: list[Relation] = []

    for i in range(0, len(sampled), batch_size):
        batch = sampled[i:i + batch_size]
        tasks = [
            extract_concepts_from_chunk(c, course_name, router, topics=topics)
            for c in batch
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logger.warning(f"Batch extraction error: {result}")
                continue
            concepts, relations = result
            all_concepts.extend(concepts)
            all_relations.extend(relations)
        done = min(i + batch_size, len(sampled))
        pct = max(1, min(99, int(100 * done / max(1, len(sampled)))))
        _emit(KG_STAGE_B, pct)
    _emit(KG_STAGE_B, 100)

    if not topics:
        # Fallback path — Stage A produced nothing. Return per-chunk
        # concepts only, exactly like the pre-M1 implementation, so the
        # frontend still gets something. F20: bumped to warning so a
        # degraded mindmap is visible in operator triage.
        logger.warning(
            "Stage A empty for %s; returning %d single-stage concepts "
            "(no course root, no macro topics — investigate LLM output)",
            course_name, len(all_concepts),
        )
        return all_concepts, all_relations

    # Stage A succeeded → synthesize root + topic-edges.
    root_id = f"root_{course_name}"
    doc_count = len(source_files)
    root_label = course_name if not doc_count else f"{course_name} · {doc_count} docs"
    root = Concept(
        concept_id=root_id,
        name=root_label,
        definition=overview,
        concept_type="root",
        course_ids=[course_name],
        chunk_ids=[],
        depth=0,
        weight=10.0,
        source_chunks=[],
        parent_topic=None,
    )

    # topic → root (part-of)
    topic_edges = [
        Relation(source=t.concept_id, target=root_id, relation_type="part-of")
        for t in topics
    ]

    # R3-3: pedagogical precedence as depends-on edges between topics
    # (later topic depends on earlier topic). The mindmap reuses
    # `depends-on` styling from M1 so no new relation type is needed.
    prereq_relations = [
        Relation(source=dst, target=src, relation_type="depends-on")
        for src, dst in prereq_edges
    ]

    # leaf → topic (part-of); skip leaves with parent_topic=None (they
    # become orphans under root via a synthesized edge instead)
    leaf_edges: list[Relation] = []
    orphan_edges: list[Relation] = []
    for c in all_concepts:
        if c.parent_topic:
            leaf_edges.append(Relation(
                source=c.concept_id, target=c.parent_topic, relation_type="part-of",
            ))
        else:
            orphan_edges.append(Relation(
                source=c.concept_id, target=root_id, relation_type="part-of",
            ))

    logger.info(
        "Two-stage extracted: 1 root + %d topics + %d concepts (%d orphans), "
        "%d edges from %d chunks",
        len(topics), len(all_concepts), len(orphan_edges),
        len(topic_edges) + len(leaf_edges) + len(orphan_edges) + len(all_relations),
        len(sampled),
    )

    return (
        [root] + topics + all_concepts,
        topic_edges + leaf_edges + orphan_edges + prereq_relations + all_relations,
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
