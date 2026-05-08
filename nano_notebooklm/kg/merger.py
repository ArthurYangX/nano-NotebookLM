"""Cross-document concept deduplication and merging."""

from __future__ import annotations

import logging
import unicodedata
from collections import defaultdict

from nano_notebooklm.types import Concept, Relation

logger = logging.getLogger(__name__)


def merge_concepts(concepts: list[Concept]) -> list[Concept]:
    """Deduplicate concepts by (concept_type, normalized name) within a course.

    F1 (review-swarm): the dedup key is now compound. Pre-fix, a Stage A
    topic like "Optimization" and a Stage B leaf concept also called
    "Optimization" collapsed into one record — but their concept_ids differ
    (`topic_<course>_optimization` vs `concept_<course>_optimization`), so
    every part-of edge referencing the discarded id was silently dropped by
    `KnowledgeGraph.add_relations`. By keying on concept_type as well, root /
    topic / leaf with the same name now stay separate, and the structural
    edges survive.
    """
    merged: dict[tuple[str, str], Concept] = {}

    for c in concepts:
        key = (str(c.concept_type or "").lower(), _normalize_name(c.name))
        if key in merged:
            existing = merged[key]
            existing.chunk_ids = list(set(existing.chunk_ids + c.chunk_ids))
            existing.course_ids = list(set(existing.course_ids + c.course_ids))
            existing.source_chunks = _merge_sources(existing.source_chunks, c.source_chunks)
            existing.weight = max(existing.weight, c.weight)
            existing.depth = min(existing.depth, c.depth)
            if not existing.definition and c.definition:
                existing.definition = c.definition
            # Preserve the first-seen parent_topic; only override if missing.
            if not existing.parent_topic and c.parent_topic:
                existing.parent_topic = c.parent_topic
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
    """Normalize concept name for deduplication.

    fix-all v3 #L1: NFKC normalisation collapses full-width / half-width
    forms, Unicode compatibility variants, and most CJK punctuation
    differences so a Stage A topic emitted as `卷積神經網絡` and a Stage
    B leaf emitted as `卷积神经网络` (or "Ｃｏｎｖ" vs "Conv") aren't
    persisted as two records.
    """
    s = unicodedata.normalize("NFKC", str(name or "")).lower().strip()
    s = s.replace("-", " ").replace("_", " ")
    return " ".join(s.split())


def _merge_sources(left: list[dict], right: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for item in list(left or []) + list(right or []):
        key = (item.get("chunk_id"), item.get("source_file"), item.get("page"))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out
