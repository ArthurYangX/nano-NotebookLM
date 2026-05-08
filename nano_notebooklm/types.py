"""Shared data models for nano-NOTEBOOKLM."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


# ── Enums ────────────────────────────────────────────────────────────
class FileType(str, Enum):
    PDF = "pdf"
    PPTX = "pptx"
    DOCX = "docx"
    MARKDOWN = "md"
    TXT = "txt"


class NoteFormat(str, Enum):
    MARKDOWN = "markdown"
    LATEX = "latex"


# ── Document models ──────────────────────────────────────────────────
class PageInfo(BaseModel):
    """A single page/slide/section extracted from a document."""
    text: str
    page: int | None = None
    slide: int | None = None
    section: str | None = None
    total_pages: int | None = None
    total_slides: int | None = None
    line_start: int | None = None
    line_end: int | None = None


class Chunk(BaseModel):
    """A text chunk with full provenance metadata."""
    chunk_id: str
    doc_id: str
    course_id: str
    text: str
    file_type: FileType
    source_file: str
    location: str  # Human-readable location
    page: int | None = None
    slide: int | None = None
    section: str | None = None


class Document(BaseModel):
    """A processed document."""
    doc_id: str  # SHA256 of file content
    course_id: str
    filename: str
    file_type: FileType
    total_pages: int = 0
    chunk_ids: list[str] = Field(default_factory=list)
    ingested_at: datetime = Field(default_factory=datetime.now)


class Course(BaseModel):
    """A course with its documents."""
    course_id: str
    name: str
    documents: list[str] = Field(default_factory=list)  # doc_ids
    last_updated: datetime = Field(default_factory=datetime.now)


# ── AI models ────────────────────────────────────────────────────────
class LLMResponse(BaseModel):
    """Response from an LLM backend."""
    content: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0


class TokenUsage(BaseModel):
    """Cumulative token usage tracking."""
    input_tokens: int = 0
    output_tokens: int = 0
    total_cost_usd: float = 0.0


# ── Search models ────────────────────────────────────────────────────
class SearchResult(BaseModel):
    """A single search result."""
    chunk_id: str
    text: str
    source_file: str
    location: str
    score: float
    course_id: str = ""


# ── Knowledge graph models ───────────────────────────────────────────
class Concept(BaseModel):
    """An extracted concept from course materials.

    M1 expands `concept_type` to also mean course root / topic:
      - "root"  → depth=0, the course node (one per course)
      - "topic" → depth=1, a macro-topic from Stage A extraction
      - "definition" / "theorem" / "algorithm" / "example" → depth>=2 leaves
    `parent_topic` carries the concept_id of the depth=1 topic a leaf
    attaches to (None for roots, topics, or leaves the LLM couldn't place).
    """
    concept_id: str
    name: str
    definition: str
    concept_type: str = "definition"  # root, topic, definition, theorem, algorithm, example
    course_ids: list[str] = Field(default_factory=list)
    chunk_ids: list[str] = Field(default_factory=list)
    depth: int = 1
    weight: float = 1.0
    source_chunks: list[dict] = Field(default_factory=list)
    parent_topic: str | None = None
    # R3-3: 1-based topological position among Stage A topics
    # ("study Topic 1 before Topic 2"). None on roots, leaves, or
    # whenever the LLM didn't emit `prerequisite_of` for that batch.
    learning_order: int | None = None


class Relation(BaseModel):
    """A relationship between concepts."""
    source: str  # concept_id
    target: str  # concept_id
    relation_type: str  # is-a, part-of, depends-on, example-of, related


# ── Skill models ─────────────────────────────────────────────────────
class SkillResult(BaseModel):
    """Result from a skill execution."""
    success: bool
    data: dict = Field(default_factory=dict)
    output_path: str | None = None
    error: str | None = None


# ── Mastery models ───────────────────────────────────────────────────
class MasteryRecord(BaseModel):
    """Tracks a student's mastery of a concept."""
    concept_id: str
    score: float = 0.0  # 0.0 to 1.0
    attempts: int = 0
    last_tested: datetime | None = None
    wrong_answers: list[dict] = Field(default_factory=list)
