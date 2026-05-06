"""Cross-document concept deduplication and merging."""

from __future__ import annotations

import logging
from collections import defaultdict

from nano_notebooklm.types import Concept, Relation

logger = logging.getLogger(__name__)


def merge_concepts(concepts: list[Concept]) -> list[Concept]:
    """Deduplicate concepts by normalized name within the same course."""
    merged: dict[str, Concept] = {}

    for c in concepts:
        key = _normalize_name(c.name)
        if key in merged:
            existing = merged[key]
            existing.chunk_ids = list(set(existing.chunk_ids + c.chunk_ids))
            existing.course_ids = list(set(existing.course_ids + c.course_ids))
            if not existing.definition and c.definition:
                existing.definition = c.definition
        else:
            merged[key] = c.model_copy()

    logger.info(f"Merged {len(concepts)} → {len(merged)} unique concepts")
    return list(merged.values())


def merge_relations(
    relations: list[Relation],
    concept_id_map: dict[str, str] | None = None,
) -> list[Relation]:
    """Deduplicate relations, optionally remapping concept IDs."""
    seen = set()
    merged = []

    for r in relations:
        source = concept_id_map.get(r.source, r.source) if concept_id_map else r.source
        target = concept_id_map.get(r.target, r.target) if concept_id_map else r.target
        key = (source, target, r.relation_type)
        if key not in seen:
            seen.add(key)
            merged.append(Relation(source=source, target=target, relation_type=r.relation_type))

    return merged


def _normalize_name(name: str) -> str:
    """Normalize concept name for deduplication."""
    return name.lower().strip().replace("-", " ").replace("_", " ")
