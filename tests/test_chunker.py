"""Tests for the token-based chunker."""

from __future__ import annotations

from nano_notebooklm.ingest.chunker import chunk_pages
from nano_notebooklm.types import FileType, PageInfo


def _make_page(text: str, page_num: int = 1, total: int = 1) -> PageInfo:
    return PageInfo(text=text, page=page_num, total_pages=total)


def test_chunker_skips_too_short():
    pages = [_make_page("short")]
    chunks = chunk_pages(
        pages=pages,
        source_file="x.pdf",
        file_type=FileType.PDF,
        course_id="c",
        doc_id="d" * 16,
        chunk_size=100,
        overlap=10,
        min_tokens=50,
    )
    assert chunks == []


def test_chunker_keeps_single_chunk_when_under_size():
    text = ("token " * 80).strip()
    pages = [_make_page(text)]
    chunks = chunk_pages(
        pages=pages,
        source_file="x.pdf",
        file_type=FileType.PDF,
        course_id="c",
        doc_id="d" * 16,
        chunk_size=200,
        overlap=20,
        min_tokens=50,
    )
    assert len(chunks) == 1
    assert chunks[0].course_id == "c"
    assert chunks[0].file_type == FileType.PDF
    assert "Page 1" in chunks[0].location


def test_chunker_splits_long_pages_with_overlap():
    text = ("hello world this is a chunking test " * 60).strip()
    pages = [_make_page(text, page_num=1, total=1)]
    chunks = chunk_pages(
        pages=pages,
        source_file="x.pdf",
        file_type=FileType.PDF,
        course_id="c",
        doc_id="d" * 16,
        chunk_size=80,
        overlap=10,
        min_tokens=20,
    )
    assert len(chunks) >= 2
    # Each chunk has unique id
    ids = [c.chunk_id for c in chunks]
    assert len(set(ids)) == len(ids)
    # Locations are tagged with "(part N)" for split chunks
    assert any("part" in c.location for c in chunks)


def test_chunker_preserves_metadata():
    pages = [_make_page("alpha beta gamma " * 30, page_num=3, total=10)]
    chunks = chunk_pages(
        pages=pages,
        source_file="lec.pdf",
        file_type=FileType.PDF,
        course_id="cs231n",
        doc_id="abcdef0123456789",
        chunk_size=300,
        overlap=20,
        min_tokens=10,
    )
    c = chunks[0]
    assert c.source_file == "lec.pdf"
    assert c.page == 3
    assert "Page 3/10" in c.location
    assert c.chunk_id.startswith("chunk_cs231n_abcdef01_")


# ── has_formula heuristic (R5 chunker enrichment) ──────────────────


def _chunk_one(text: str):
    """Helper: chunk a single page of `text` and return the first chunk."""
    pages = [_make_page(text)]
    chunks = chunk_pages(
        pages=pages,
        source_file="x.pdf",
        file_type=FileType.PDF,
        course_id="c",
        doc_id="d" * 16,
        chunk_size=1000,
        overlap=0,
        min_tokens=5,
    )
    return chunks[0] if chunks else None


def test_has_formula_detects_block_math():
    c = _chunk_one("The forward algorithm is:\n$$\nP(O|\\lambda) = \\sum_t \\alpha_t(i)\n$$\nDone.")
    assert c is not None and c.has_formula is True


def test_has_formula_detects_latex_macros():
    c = _chunk_one("After dropout we compute \\frac{1}{N} \\sum_{i=1}^N x_i for the average. " * 3)
    assert c is not None and c.has_formula is True


def test_has_formula_detects_inline_math():
    c = _chunk_one("Let $x \\in \\mathbb{R}^d$ be the input vector and apply ReLU. " * 3)
    assert c is not None and c.has_formula is True


def test_has_formula_false_for_plain_prose():
    c = _chunk_one(
        "马尔科夫模型最早由 Markov 于 1913 年提出，主要用于语音处理与统计机器翻译领域。" * 4
    )
    assert c is not None and c.has_formula is False


def test_has_formula_false_for_currency_amount():
    # `$5` alone should NOT trigger inline-math (need ≥1 non-whitespace
    # char between the dollars — the heuristic uses [^\s$]).
    c = _chunk_one("The license costs $5 per month and the support plan is $50 per year. " * 4)
    assert c is not None and c.has_formula is False
