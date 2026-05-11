"""Cross-document concept deduplication and merging."""

from __future__ import annotations

import logging
import unicodedata
from collections import defaultdict

from nano_notebooklm.types import Concept, Relation

logger = logging.getLogger(__name__)


def merge_concepts(concepts: list[Concept]) -> list[Concept]:
    """Deduplicate concepts within a course.

    F1 (review-swarm): the dedup key distinguishes root / topic / leaf by
    `concept_type` so a Stage A topic "Optimization" and a Stage B leaf
    "Optimization" don't collapse into one record (their concept_ids
    differ, so structural part-of edges referencing the discarded id were
    silently dropped by `KnowledgeGraph.add_relations`).

    R5-1: for `root` and `topic` types the dedup key also includes the
    `concept_id` (which encodes the chapter slug — see
    `kg/extractor.py:extract_from_chunks`). Two chapters with a same-named
    macro topic ("Backpropagation" in both Lecture 3 and Lecture 7) now
    persist as distinct nodes, each part-of its own chapter root, instead
    of merging into a single floating topic. Leaves continue to dedup by
    `(type, name)` because a leaf-level concept like "Gradient descent"
    referenced from two chapters IS the same concept (the merger pools
    their source_chunks so the student can jump to either citation).
    """
    merged: dict[tuple, Concept] = {}

    for c in concepts:
        c_type = str(c.concept_type or "").lower()
        if c_type in {"root", "topic"}:
            # R5-1: concept_id distinguishes per-chapter copies.
            key: tuple = (c_type, _normalize_name(c.name), c.concept_id)
        else:
            key = (c_type, _normalize_name(c.name))
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
