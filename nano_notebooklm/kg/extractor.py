"""LLM-based concept and relation extraction from text chunks."""

from __future__ import annotations

import logging
from typing import Any

from nano_notebooklm.ai import prompt_templates as prompts
from nano_notebooklm.ai.router import ModelRouter
from nano_notebooklm.types import Chunk, Concept, Relation

logger = logging.getLogger(__name__)


async def extract_concepts_from_chunk(
    chunk: Chunk,
    course_name: str,
    router: ModelRouter,
) -> tuple[list[Concept], list[Relation]]:
    """Extract concepts and relations from a single chunk via LLM."""
    prompt = prompts.CONCEPT_EXTRACTION_PROMPT.format(
        course_name=course_name,
        chunk_text=chunk.text,
    )

    try:
        data = await router.complete_structured(
            prompt,
            task_type="concept_extraction",
            system=prompts.CONCEPT_EXTRACTION_SYSTEM,
            temperature=0.2,
        )
    except Exception as e:
        logger.warning(f"Concept extraction failed for {chunk.chunk_id}: {e}")
        return [], []

    concepts = []
    raw_concepts = data.get("concepts", [])
    if isinstance(raw_concepts, list):
        for rc in raw_concepts:
            if not isinstance(rc, dict):
                continue
            name = rc.get("name", "").strip()
            if not name:
                continue
            concepts.append(Concept(
                concept_id=f"concept_{course_name}_{_slug(name)}",
                name=name,
                definition=rc.get("definition", ""),
                concept_type=rc.get("type", "definition"),
                course_ids=[chunk.course_id],
                chunk_ids=[chunk.chunk_id],
                depth=_depth_for_type(rc.get("type", "definition")),
                weight=_weight_for_concept(rc, chunk),
                source_chunks=[{
                    "chunk_id": chunk.chunk_id,
                    "source_file": chunk.source_file,
                    "location": chunk.location,
                    "page": chunk.page,
                }],
            ))

    relations = []
    raw_relations = data.get("relations", [])
    if isinstance(raw_relations, list):
        for rr in raw_relations:
            if not isinstance(rr, dict):
                continue
            source = rr.get("source", "").strip()
            target = rr.get("target", "").strip()
            if source and target:
                relations.append(Relation(
                    source=f"concept_{course_name}_{_slug(source)}",
                    target=f"concept_{course_name}_{_slug(target)}",
                    relation_type=_normalize_relation(rr.get("type", "related")),
                ))

    return concepts, relations


async def extract_from_chunks(
    chunks: list[Chunk],
    course_name: str,
    router: ModelRouter,
    max_chunks: int = 50,
) -> tuple[list[Concept], list[Relation]]:
    """Extract concepts from multiple chunks (samples if too many)."""
    import asyncio
    import random

    # Sample if too many chunks
    if len(chunks) > max_chunks:
        sampled = random.sample(chunks, max_chunks)
    else:
        sampled = chunks

    all_concepts: list[Concept] = []
    all_relations: list[Relation] = []

    # Process in batches of 5 for rate limiting
    batch_size = 5
    for i in range(0, len(sampled), batch_size):
        batch = sampled[i:i + batch_size]
        tasks = [extract_concepts_from_chunk(c, course_name, router) for c in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                logger.warning(f"Batch extraction error: {result}")
                continue
            concepts, relations = result
            all_concepts.extend(concepts)
            all_relations.extend(relations)

    logger.info(f"Extracted {len(all_concepts)} concepts, {len(all_relations)} relations from {len(sampled)} chunks")
    return all_concepts, all_relations


def _slug(name: str) -> str:
    """Create a URL-safe slug from a concept name."""
    import re
    slug = re.sub(r"[^\w\s-]", "", name.lower())
    slug = re.sub(r"[\s_]+", "_", slug).strip("_")
    return slug[:50]


def _depth_for_type(concept_type: str) -> int:
    t = str(concept_type or "").lower()
    if t in {"course", "topic", "chapter"}:
        return 0
    if t in {"definition", "theorem", "algorithm"}:
        return 1
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
