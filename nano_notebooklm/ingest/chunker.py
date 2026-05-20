"""Token-based text chunking with metadata preservation."""

from __future__ import annotations

import re

from nano_notebooklm import config
from nano_notebooklm.types import Chunk, FileType, PageInfo
from nano_notebooklm.utils.token_counter import get_encoder


# `has_formula` heuristic: any of these patterns in chunk text → True.
# We deliberately *don't* match plain `$X$` inline math — slides often
# contain currency strings like "$5 per month and $50 per year" which
# would otherwise false-positive. Block math `$$...$$` and explicit
# LaTeX commands `\frac`, `\sum`, ... are unambiguous; inline math
# without any command is rare enough in real PDFs that the false
# negative is acceptable.
_FORMULA_PATTERNS = (
    re.compile(r"\$\$"),                              # block math fence
    re.compile(r"\\(?:frac|sum|int|mid|alpha|beta|gamma|delta|sigma|mu|pi|theta|lambda|cdot|cdots|prod|partial|nabla|infty|leq|geq|neq|approx|in|notin|subset|cup|cap|forall|exists|mathbb|mathcal|begin|end|left|right|times|hat|bar|tilde|dot|ddot|vec)\b"),  # common LaTeX macros
)


def _text_has_formula(text: str) -> bool:
    return any(p.search(text) for p in _FORMULA_PATTERNS)


# ── Formula block atomicity (R5/MinerU step ②) ─────────────────────
# The token-based splitter has no concept of "$$...$$ is a unit". For
# any page where a `$$...$$` block straddles `chunk_size`, the splitter
# happily cuts the block in half — chunk N ends with "\\boldsymbol" and
# chunk N+1 starts with "_ { t } } = ..." (real evidence from
# HMMViterbiDemo). Neither half is searchable, neither half is parseable
# to the answer LLM, and BM25/embedding tokenisers compound the damage.
#
# Strategy: before token-splitting, replace every `$$...$$` (DOTALL,
# non-greedy) with a short placeholder `NN` — two Unicode
# Private Use Area characters that tiktoken encodes as a single byte-
# fallback token each, plus a 1-3 char index. Token total ~5-7,
# essentially never straddles a typical chunk boundary. After split +
# decode, swap placeholders back to original LaTeX. If a half placeholder
# does survive at a chunk edge (unmatched `` without ``),
# strip the dangling marker — better than emitting `<<F0...` garbage to
# the user.
#
# Choice of  / : BMP private-use chars, can't collide with
# real LaTeX content; encoded by cl100k_base as <byte-fallback> tokens
# so the placeholder is opaque to the splitter — it can't be
# accidentally "decoded back" mid-split.
_PLACEHOLDER_OPEN = ""
_PLACEHOLDER_CLOSE = ""
_BLOCK_FORMULA_RE = re.compile(r"\$\$.*?\$\$", re.DOTALL)
_PLACEHOLDER_FULL_RE = re.compile(
    re.escape(_PLACEHOLDER_OPEN) + r"(\d+)" + re.escape(_PLACEHOLDER_CLOSE)
)


def _stash_block_formulas(text: str) -> tuple[str, list[str]]:
    """Replace every `$$...$$` with a short opaque placeholder.

    Returns the modified text plus the ordered list of original block
    bodies. The Nth placeholder maps to ``formulas[N]``.

    **H2 fix (review-swarm fix-all v1)**: If ``text`` already contains
    ``_PLACEHOLDER_OPEN`` or ``_PLACEHOLDER_CLOSE``, return the text
    untouched with empty formulas. Those BMP Private Use Area chars are
    legitimately used by some CJK custom-glyph fonts, IPA, and Apple
    emoji private encoding. Without this guard, ``_restore_block_formulas``
    would silently delete the user's own PUA bytes via the dangling-marker
    scrub. Trade-off: a PDF mixing legitimate PUA with formulae loses
    formula atomicity (the splitter may slice ``$$...$$``) — but never
    deletes user content. Atomicity is best-effort; content preservation
    is hard.
    """
    if _PLACEHOLDER_OPEN in text or _PLACEHOLDER_CLOSE in text:
        return text, []

    formulas: list[str] = []

    def _capture(match: re.Match) -> str:
        idx = len(formulas)
        formulas.append(match.group(0))
        return f"{_PLACEHOLDER_OPEN}{idx}{_PLACEHOLDER_CLOSE}"

    stashed = _BLOCK_FORMULA_RE.sub(_capture, text)
    return stashed, formulas


def _restore_block_formulas(chunk_text: str, formulas: list[str]) -> str:
    """Swap placeholders back to original LaTeX.

    H2 fix (review-swarm fix-all v1): when ``formulas`` is empty (the
    stash refused because the page already contained PUA chars), this
    is an identity transform — we do NOT strip PUA bytes from the
    output, because they're the user's own content.

    Otherwise: scrub any dangling half-placeholder (an OPEN/CLOSE marker
    adjacent to a digit but unmatched — the signature of a placeholder
    cut at a chunk boundary that wasn't repaired by the post-split
    pass). We only strip those tightly-anchored fragments, never bare
    PUA characters on their own.
    """
    if not formulas:
        return chunk_text

    def _restore(match: re.Match) -> str:
        idx = int(match.group(1))
        return formulas[idx] if 0 <= idx < len(formulas) else ""

    out = _PLACEHOLDER_FULL_RE.sub(_restore, chunk_text)
    # Tightly-anchored half-placeholder scrub: only `<OPEN><digits>`
    # (close stripped) or `<digits><CLOSE>` (open stripped). Bare PUA
    # not adjacent to digits stays — that's user content (e.g. CJK
    # custom-glyph fonts), not our scaffolding.
    out = _PLACEHOLDER_OPEN_RE.sub("", out)
    out = re.sub(r"\d+" + re.escape(_PLACEHOLDER_CLOSE), "", out)
    return out


def _placeholder_indices_in(text: str) -> set[int]:
    """Return the set of fully-matched placeholder indices inside `text`.

    Used by the post-split repair pass to know which formulas a chunk
    captured intact.
    """
    return {int(m.group(1)) for m in _PLACEHOLDER_FULL_RE.finditer(text)}


# Match `<OPEN><digits>` even when `<CLOSE>` is missing — this is the
# signature of a placeholder whose CLOSE got sliced off into the next
# chunk. Used by `_unfinished_open_indices` to attribute each missing
# formula to the piece where its placeholder *started*.
_PLACEHOLDER_OPEN_RE = re.compile(re.escape(_PLACEHOLDER_OPEN) + r"(\d+)")


def _unfinished_open_indices(piece: str) -> list[int]:
    """Indices whose OPEN marker appears in this piece but CLOSE doesn't follow.

    Used by the post-split repair pass — these are the formulas whose
    `$$...$$` block started in this chunk but got cut at the boundary,
    so the original LaTeX needs to be re-injected here. Distinguishes
    "open + digits + close" (= fully captured, ignored here) from
    "open + digits + (anything else / end-of-string)" (= started here).
    """
    out: list[int] = []
    for m in _PLACEHOLDER_OPEN_RE.finditer(piece):
        idx = int(m.group(1))
        after = m.end()
        if after < len(piece) and piece[after] == _PLACEHOLDER_CLOSE:
            continue  # full placeholder, already captured
        out.append(idx)
    return out


def chunk_pages(
    pages: list[PageInfo],
    source_file: str,
    file_type: FileType,
    course_id: str,
    doc_id: str,
    chunk_size: int = config.CHUNK_SIZE_TOKENS,
    overlap: int = config.CHUNK_OVERLAP_TOKENS,
    min_tokens: int = config.MIN_CHUNK_TOKENS,
) -> list[Chunk]:
    """Split pages/slides into token-based chunks with full metadata.

    `$$...$$` LaTeX blocks are stashed to short
    placeholders before splitting, then restored. This guarantees no
    block ever ends up split across two chunks (a block that's itself
    larger than ``chunk_size`` lives in its own oversized chunk —
    breaking the size cap is strictly better than corrupting the
    formula).
    """
    enc = get_encoder()
    chunks: list[Chunk] = []
    seq = 0

    for page in pages:
        # M2 fix (review-swarm fix-all v1): the min_tokens gate measures
        # the *raw* text, not the stashed text. A slide that is nothing
        # but one big formula collapses to a ~5-token placeholder after
        # stashing and would have been dropped here pre-fix.
        if len(enc.encode(page.text)) < min_tokens:
            continue

        stashed_text, formulas = _stash_block_formulas(page.text)
        tokens = enc.encode(stashed_text)

        if len(tokens) <= chunk_size:
            # Whole page fits — single chunk, restore formulas in place.
            chunks.append(_make_chunk(
                text=_restore_block_formulas(stashed_text, formulas),
                page=page,
                source_file=source_file,
                file_type=file_type,
                course_id=course_id,
                doc_id=doc_id,
                seq=seq,
            ))
            seq += 1
            continue

        # Multi-chunk path. Track which formulas landed intact in which
        # chunk so we can detect placeholders that got split across the
        # boundary and re-inject the missing formula text.
        raw_pieces: list[tuple[str, set[int]]] = []  # (stashed_text, captured_idx_set)
        start = 0
        while start < len(tokens):
            end = min(start + chunk_size, len(tokens))
            chunk_tokens = tokens[start:end]
            if len(chunk_tokens) >= min_tokens:
                piece = enc.decode(chunk_tokens)
                captured = _placeholder_indices_in(piece)
                raw_pieces.append((piece, captured))
            start += chunk_size - overlap

        # Post-split repair (H1 fix, review-swarm fix-all v1): any
        # formula that disappeared (no chunk captured it intact) is
        # appended in full to the chunk where its placeholder *started*.
        # Pre-fix the repair picked the first chunk whose text contained
        # any OPEN marker; with two cross-boundary formulae A and B, both
        # got appended to chunk 0 and the chunk where B actually started
        # silently lost the formula. Post-fix we attribute by index via
        # `_unfinished_open_indices` so each missing formula lands in the
        # chunk where its OPEN was actually cut.
        all_captured: set[int] = set().union(*(c for _, c in raw_pieces)) if raw_pieces else set()
        missing = [i for i in range(len(formulas)) if i not in all_captured]
        for missing_idx in missing:
            placed = False
            for j, (piece, captured) in enumerate(raw_pieces):
                if missing_idx in _unfinished_open_indices(piece):
                    raw_pieces[j] = (piece + "\n\n" + formulas[missing_idx], captured)
                    placed = True
                    break
            if not placed and raw_pieces:
                # Fallback: placeholder somehow not in any piece. Append
                # to the first chunk so the formula at least lives
                # somewhere (better than silently dropping).
                p0, c0 = raw_pieces[0]
                raw_pieces[0] = (p0 + "\n\n" + formulas[missing_idx], c0)

        sub_idx = 0
        for piece, _ in raw_pieces:
            restored = _restore_block_formulas(piece, formulas)
            chunks.append(_make_chunk(
                text=restored,
                page=page,
                source_file=source_file,
                file_type=file_type,
                course_id=course_id,
                doc_id=doc_id,
                seq=seq,
                sub_idx=sub_idx,
            ))
            seq += 1
            sub_idx += 1

    return chunks


def _make_chunk(
    text: str,
    page: PageInfo,
    source_file: str,
    file_type: FileType,
    course_id: str,
    doc_id: str,
    seq: int,
    sub_idx: int | None = None,
) -> Chunk:
    """Create a Chunk with proper metadata based on file type."""
    location = _format_location(page, file_type, sub_idx)
    chunk_id = f"chunk_{course_id}_{doc_id[:8]}_{seq:05d}"

    return Chunk(
        chunk_id=chunk_id,
        doc_id=doc_id,
        course_id=course_id,
        text=text,
        file_type=file_type,
        source_file=source_file,
        location=location,
        page=page.page,
        slide=page.slide,
        section=page.section,
        has_formula=_text_has_formula(text),
    )


def _format_location(page: PageInfo, file_type: FileType, sub_idx: int | None = None) -> str:
    """Format a human-readable location string."""
    suffix = f" (part {sub_idx + 1})" if sub_idx is not None else ""

    if file_type == FileType.PDF and page.page is not None:
        return f"Page {page.page}/{page.total_pages}{suffix}"
    elif file_type == FileType.PPTX and page.slide is not None:
        return f"Slide {page.slide}/{page.total_slides}{suffix}"
    elif file_type in (FileType.MARKDOWN, FileType.TXT, FileType.DOCX) and page.section:
        return f"Section \"{page.section}\"{suffix}"
    return f"Position {page.line_start or 0}{suffix}"
