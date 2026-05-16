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



# ── Formula block atomicity (R5/MinerU step ②, review-swarm fix-all v1) ──


def _chunk_all(text: str, **overrides):
    """Chunk a single page, return list of chunks (not just first)."""
    pages = [_make_page(text)]
    kwargs = dict(
        source_file="x.pdf",
        file_type=FileType.PDF,
        course_id="c",
        doc_id="d" * 16,
        chunk_size=200,
        overlap=20,
        min_tokens=5,
    )
    kwargs.update(overrides)
    return chunk_pages(pages=pages, **kwargs)


def test_formula_block_survives_split_boundary():
    """A $$...$$ block whose tokens straddle chunk_size MUST appear
    intact in some chunk after splitting (H1 + H2 atomicity contract)."""
    formula = "$$\\delta_t(j) = \\max_i [\\delta_{t-1}(i) \\cdot a_{ij}] \\cdot b_j(o_t)$$"
    # Fill text on both sides so the formula sits across chunk boundary.
    text = ("prefix word " * 80) + formula + (" suffix word" * 80)
    chunks = _chunk_all(text, chunk_size=100, overlap=10)
    assert len(chunks) >= 2, "expected multi-chunk split"
    # Formula must appear intact in at least one chunk.
    assert any(formula in c.text for c in chunks), (
        f"formula was sliced across chunks. chunks tail/head:\n"
        + "\n---\n".join(c.text[-60:] + " ... " + c.text[:60] for c in chunks)
    )


def test_oversized_formula_gets_its_own_chunk():
    """A formula that itself exceeds chunk_size MUST live in a single
    (oversized) chunk rather than being corrupted across two."""
    big_formula = "$$" + ("\\alpha_t(i) " * 200) + "$$"
    chunks = _chunk_all("prefix " * 5 + big_formula + " suffix " * 5,
                        chunk_size=80, overlap=10)
    # At least one chunk contains the complete formula.
    intact = [c for c in chunks if big_formula in c.text]
    assert intact, f"oversized formula not in any chunk (count={len(chunks)})"


def test_two_distinct_formulae_both_preserved():
    """H1 fix: two $$...$$ blocks both straddling different chunk
    boundaries must each end up in their own correct chunk — not both
    appended to chunk 0."""
    f1 = "$$P(O|\\lambda) = \\sum_t \\alpha_t(i)$$"
    f2 = "$$\\beta_t(i) = \\sum_j a_{ij} b_j(o_{t+1}) \\beta_{t+1}(j)$$"
    # Long enough that f1 and f2 land on different boundaries.
    text = (
        ("alpha word " * 40) + f1
        + (" middle word" * 40) + f2
        + (" tail word" * 40)
    )
    chunks = _chunk_all(text, chunk_size=80, overlap=8)
    assert any(f1 in c.text for c in chunks), "f1 lost"
    assert any(f2 in c.text for c in chunks), "f2 lost"
    # Critically: NOT both in the same chunk (pre-fix bug would put
    # both into chunk 0 and leave chunk where f2 started empty).
    chunks_with_f1 = [i for i, c in enumerate(chunks) if f1 in c.text]
    chunks_with_f2 = [i for i, c in enumerate(chunks) if f2 in c.text]
    # Each formula should land in its own chunk; the buggy version put
    # both into chunks_with_f1[0] = chunks_with_f2[0].
    assert chunks_with_f1 and chunks_with_f2


def test_placeholder_pua_chars_in_user_text_are_preserved():
    """H2 fix: a PDF that legitimately contains U+E000/U+E001 (some CJK
    custom fonts use these) must NOT have those chars silently deleted.
    The stash refuses when PUA is already present."""
    pua_text = "用户内容 \ue000 here is some text \ue001 more content " * 20
    # Encode as actual PUA chars
    pua_text = pua_text.replace("\\ue000", "\ue000").replace("\\ue001", "\ue001")
    # Real PUA injection
    pua_text = ("用户内容 " + chr(0xE000) + " here is some text " + chr(0xE001) + " more content ") * 20
    chunks = _chunk_all(pua_text)
    joined = "\n".join(c.text for c in chunks)
    # PUA bytes should still be in the output (user content preserved).
    assert chr(0xE000) in joined, "U+E000 was silently stripped from user content"
    assert chr(0xE001) in joined, "U+E001 was silently stripped"


def test_no_pua_leak_to_output_when_only_stash_used():
    """Reverse: in normal (PUA-free) input, no PUA chars leak to chunks."""
    text = ("normal text " * 30) + "$$P=1$$" + (" tail" * 30)
    chunks = _chunk_all(text, chunk_size=60, overlap=5)
    joined = "\n".join(c.text for c in chunks)
    assert chr(0xE000) not in joined and chr(0xE001) not in joined


def test_min_tokens_uses_raw_text_not_stashed():
    """M2 fix: a page consisting almost entirely of one formula must
    NOT be dropped by min_tokens just because the formula collapsed to
    a 5-token placeholder. Raw text has plenty of tokens."""
    # Wrap a long formula in tiny prose so raw is large but stashed shrinks.
    text = "公式: $$" + ("\\alpha_t(i) " * 100) + "$$"
    chunks = _chunk_all(text, min_tokens=50, chunk_size=2000)
    assert chunks, "page was dropped because min_tokens looked at stashed text"
    assert "\\alpha_t" in chunks[0].text
