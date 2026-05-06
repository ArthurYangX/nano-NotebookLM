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
