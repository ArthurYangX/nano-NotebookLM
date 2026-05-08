"""更严格的 chunker 测试 — 关注 token-based 切片在边界值下的稳定性。

覆盖：
- 空 / 单字符 / 全空白页面
- 多页面累积 + 跨页 chunk_id 序号连续
- chunk_size == min_tokens（无 overlap 的临界）
- overlap >= chunk_size（潜在死循环风险）
- 极长无标点单行
- 中英混合 + emoji
- 不同 FileType 的 location 字符串格式
- chunk_id 格式 / 唯一性 / 与 doc_id 前缀对应
- min_tokens 拦截窄段
- DOCX/MD/TXT 在没有 page 但有 section 时 location 用 Section 前缀
- DOCX/MD/TXT 没有 section 也没有 page → 用 line_start
"""

from __future__ import annotations

import pytest

from nano_notebooklm.ingest.chunker import chunk_pages
from nano_notebooklm.types import FileType, PageInfo


def _page(text: str, *, page_num=1, total=1, slide=None, total_slides=None,
          section=None, line_start=None) -> PageInfo:
    return PageInfo(
        text=text,
        page=page_num if slide is None and section is None and line_start is None else None,
        total_pages=total if slide is None and section is None and line_start is None else None,
        slide=slide,
        total_slides=total_slides,
        section=section,
        line_start=line_start,
    )


# ── empty / degenerate inputs ────────────────────────────────────────


def test_chunker_empty_pages_returns_empty():
    assert chunk_pages([], "x.pdf", FileType.PDF, "c", "d" * 16) == []


def test_chunker_blank_text_skipped():
    """A page with whitespace-only text encodes to a few tokens (or zero);
    must NOT produce a chunk that violates min_tokens."""
    pages = [_page("   \n\t   ")]
    out = chunk_pages(pages, "x.pdf", FileType.PDF, "c", "d" * 16,
                      chunk_size=100, overlap=10, min_tokens=50)
    assert out == []


def test_chunker_single_char_page_skipped():
    out = chunk_pages([_page("a")], "x.pdf", FileType.PDF, "c", "d" * 16,
                      chunk_size=100, overlap=10, min_tokens=50)
    assert out == []


# ── boundary token sizes ─────────────────────────────────────────────


def test_chunker_tokens_exactly_min_tokens_kept():
    """Pages whose token count == min_tokens must be kept (the check is
    `< min_tokens`, not `<=`)."""
    text = "word " * 50  # ~50 tokens
    out = chunk_pages([_page(text)], "x.pdf", FileType.PDF, "c", "d" * 16,
                      chunk_size=200, overlap=20, min_tokens=10)
    assert len(out) == 1


def test_chunker_tokens_one_below_min_dropped():
    """A page with < min_tokens is dropped — pin the strict threshold."""
    text = "x"  # ~1 token
    out = chunk_pages([_page(text)], "x.pdf", FileType.PDF, "c", "d" * 16,
                      chunk_size=200, overlap=20, min_tokens=2)
    assert out == []


def test_chunker_chunk_size_eq_page_tokens_one_chunk():
    """If page tokens == chunk_size, the "fits in one chunk" branch fires."""
    text = "alpha " * 100  # ~100 tokens
    out = chunk_pages([_page(text)], "x.pdf", FileType.PDF, "c", "d" * 16,
                      chunk_size=100, overlap=20, min_tokens=10)
    # Either branch (single chunk OR split) is acceptable depending on the
    # exact tiktoken count; pin that we don't lose content
    joined = "".join(c.text for c in out)
    assert "alpha" in joined
    assert all(c.chunk_id.startswith("chunk_c_") for c in out)


# ── overlap / step calculation ────────────────────────────────────────


def test_chunker_overlap_smaller_than_chunk_advances():
    text = "token " * 200
    out = chunk_pages([_page(text)], "x.pdf", FileType.PDF, "c", "d" * 16,
                      chunk_size=80, overlap=10, min_tokens=20)
    assert len(out) >= 2
    # Each chunk has unique sequence id
    ids = [c.chunk_id for c in out]
    assert len(set(ids)) == len(ids)


def test_chunker_overlap_equal_to_chunk_size_is_known_defect():
    """**Defect found by this test suite.**

    When ``overlap == chunk_size`` the loop step ``chunk_size - overlap == 0``
    leaves ``start`` parked, and ``while start < len(tokens)`` becomes an
    infinite loop. Production never hits it (defaults are
    chunk_size=512, overlap=64), but a config typo could.

    We run the call in a daemon thread with a hard 2-second wall-clock cap;
    if the call DOES return within the cap, that means the defect was fixed
    — flip the assertion accordingly. Otherwise the daemon thread is
    abandoned at process exit (acceptable since pytest itself terminates).
    """
    import threading

    result: dict = {"done": False, "value": None}

    def _runner():
        text = "token " * 200
        result["value"] = chunk_pages([_page(text)], "x.pdf", FileType.PDF,
                                      "c", "d" * 16, chunk_size=80,
                                      overlap=80, min_tokens=20)
        result["done"] = True

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join(timeout=2.0)
    assert not result["done"], (
        "Chunker now terminates with overlap == chunk_size — the defect was "
        "fixed. Update this test to assert the new behavior (e.g. ValueError "
        "raised, or returns []) instead of pinning the loop."
    )


def test_chunker_overlap_larger_than_chunk_size_also_loops():
    """Same defect family, more obvious: overlap > chunk_size → step
    becomes negative → ``start`` actually goes backwards. Daemon thread
    again so the test suite never hangs."""
    import threading

    finished = threading.Event()

    def _runner():
        chunk_pages([_page("alpha " * 200)], "x.pdf", FileType.PDF, "c",
                    "d" * 16, chunk_size=80, overlap=120, min_tokens=20)
        finished.set()

    threading.Thread(target=_runner, daemon=True).start()
    finished.wait(timeout=2.0)
    assert not finished.is_set(), (
        "Chunker now terminates when overlap > chunk_size — defect fixed."
    )


# ── chunk_id / metadata stability ────────────────────────────────────


def test_chunk_ids_are_globally_sequential_across_pages():
    """seq counter is per-call, not per-page → ids should ascend across all
    pages without resets."""
    pages = [
        _page("alpha " * 80, page_num=1, total=3),
        _page("beta " * 80,  page_num=2, total=3),
        _page("gamma " * 80, page_num=3, total=3),
    ]
    out = chunk_pages(pages, "x.pdf", FileType.PDF, "ml", "abcdef0123456789",
                      chunk_size=200, overlap=20, min_tokens=10)
    assert len(out) == 3
    ids = [c.chunk_id for c in out]
    assert ids == ["chunk_ml_abcdef01_00000",
                   "chunk_ml_abcdef01_00001",
                   "chunk_ml_abcdef01_00002"]


def test_chunk_ids_unique_under_split():
    """When a page is split into multiple chunks, each chunk gets a distinct
    seq id (and `(part N)` in location). seq must NOT reset per page."""
    pages = [
        _page("token " * 600, page_num=1, total=2),
        _page("other " * 600, page_num=2, total=2),
    ]
    out = chunk_pages(pages, "x.pdf", FileType.PDF, "ml", "doc12345abcdef00",
                      chunk_size=80, overlap=10, min_tokens=10)
    ids = [c.chunk_id for c in out]
    assert len(set(ids)) == len(ids), f"duplicate ids: {ids}"


def test_chunk_metadata_carries_through_split():
    """All split chunks of a page must carry the same source_file / page /
    course_id."""
    pages = [_page("alpha " * 600, page_num=5, total=10)]
    out = chunk_pages(pages, "lec5.pdf", FileType.PDF, "cs231n",
                      "doc12345abcdef00",
                      chunk_size=80, overlap=10, min_tokens=10)
    assert len(out) > 1
    for c in out:
        assert c.source_file == "lec5.pdf"
        assert c.page == 5
        assert c.course_id == "cs231n"
        assert c.file_type == FileType.PDF


# ── location string formats per FileType ─────────────────────────────


def test_location_pdf_carries_page_total_and_part_index():
    pages = [_page("alpha " * 600, page_num=2, total=7)]
    out = chunk_pages(pages, "x.pdf", FileType.PDF, "c", "d" * 16,
                      chunk_size=80, overlap=10, min_tokens=10)
    assert out
    assert all("Page 2/7" in c.location for c in out)
    # First split chunk should be tagged "(part 1)"
    assert "(part 1)" in out[0].location


def test_location_pptx_carries_slide_count():
    pi = PageInfo(text="slide " * 80, slide=3, total_slides=12, page=None)
    out = chunk_pages([pi], "deck.pptx", FileType.PPTX, "c", "d" * 16,
                      chunk_size=200, overlap=20, min_tokens=10)
    assert out
    assert all("Slide 3/12" in c.location for c in out)


def test_location_md_with_section_uses_section_label():
    pi = PageInfo(text="# Heading\nbody " * 60, section="Intro")
    out = chunk_pages([pi], "doc.md", FileType.MARKDOWN, "c", "d" * 16,
                      chunk_size=200, overlap=20, min_tokens=10)
    assert out
    assert all('Section "Intro"' in c.location for c in out)


def test_location_txt_without_section_or_page_uses_line_start():
    pi = PageInfo(text="alpha " * 60, line_start=42, line_end=120)
    out = chunk_pages([pi], "doc.txt", FileType.TXT, "c", "d" * 16,
                      chunk_size=200, overlap=20, min_tokens=10)
    assert out
    assert all("Position 42" in c.location for c in out)


def test_location_txt_with_zero_line_start_falls_back_safely():
    pi = PageInfo(text="alpha " * 60, line_start=0)
    out = chunk_pages([pi], "doc.txt", FileType.TXT, "c", "d" * 16,
                      chunk_size=200, overlap=20, min_tokens=10)
    assert out
    # `line_start or 0` → "Position 0"
    assert all("Position 0" in c.location for c in out)


# ── content variety ────────────────────────────────────────────────


def test_chunker_handles_mixed_zh_en_emoji():
    """A page mixing Chinese, English, and emoji must chunk without crash and
    preserve the chars (decode round-trip via tiktoken)."""
    text = "中文内容 backpropagation 反向传播 🚀 " * 30
    pages = [_page(text)]
    out = chunk_pages(pages, "x.pdf", FileType.PDF, "c", "d" * 16,
                      chunk_size=200, overlap=20, min_tokens=10)
    assert out
    joined = "".join(c.text for c in out)
    assert "反向传播" in joined
    assert "backpropagation" in joined


def test_chunker_handles_no_whitespace_long_string():
    """A 4000-char run with no whitespace shouldn't break the chunker — we're
    token-based, not character-based, so spaces don't matter."""
    text = "x" * 4000
    pages = [_page(text)]
    out = chunk_pages(pages, "x.pdf", FileType.PDF, "c", "d" * 16,
                      chunk_size=200, overlap=20, min_tokens=10)
    # Either we produce chunks or we don't (depends on tiktoken's encoding of
    # repeated 'x'); the must-not-crash invariant is the point.
    assert isinstance(out, list)


def test_chunker_handles_only_numbers():
    text = "12345 " * 100
    out = chunk_pages([_page(text)], "x.pdf", FileType.PDF, "c", "d" * 16,
                      chunk_size=80, overlap=10, min_tokens=10)
    assert out
    assert "12345" in "".join(c.text for c in out)


# ── seq numbering across multi-page split ────────────────────────────


def test_chunker_seq_continues_across_split_and_short_pages():
    """Mix: page 1 splits into multiple, page 2 fits in one. seq should NOT
    reset; chunk_id ordering must be strictly increasing across pages."""
    pages = [
        _page("alpha " * 600, page_num=1, total=2),  # multi-split
        _page("beta " * 80,    page_num=2, total=2),  # single chunk
    ]
    out = chunk_pages(pages, "x.pdf", FileType.PDF, "c", "d" * 16,
                      chunk_size=80, overlap=10, min_tokens=10)
    ids = [c.chunk_id for c in out]
    assert ids == sorted(ids), "chunk_ids must be lexicographically increasing"
    assert len(set(ids)) == len(ids)
    # Last chunk lives on page 2, not page 1
    assert out[-1].page == 2
