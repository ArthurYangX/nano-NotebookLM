"""Token-based text chunking with metadata preservation.

Adapted from NLPProject/scripts/build_raft_dataset.py (token-based)
and chunk_with_metadata.py (metadata tracking).
"""

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
    """
    formulas: list[str] = []

    def _capture(match: re.Match) -> str:
        idx = len(formulas)
        formulas.append(match.group(0))
        return f"{_PLACEHOLDER_OPEN}{idx}{_PLACEHOLDER_CLOSE}"

    stashed = _BLOCK_FORMULA_RE.sub(_capture, text)
    return stashed, formulas


def _restore_block_formulas(chunk_text: str, formulas: list[str]) -> str:
    """Swap placeholders back to original LaTeX.

    Also scrubs any unmatched / half-placeholder bytes that survived a
    chunk-edge cut, so the user never sees `\\ue0001\\ue0` garbage. In
    the (rare) case a placeholder is split across two chunks, the caller
    is responsible for ensuring the formula appears intact in at least
    one chunk — see `chunk_pages` for the post-split repair pass.
    """
    def _restore(match: re.Match) -> str:
        idx = int(match.group(1))
        return formulas[idx] if 0 <= idx < len(formulas) else ""

    out = _PLACEHOLDER_FULL_RE.sub(_restore, chunk_text)
    # Strip any dangling half-placeholder (open without close, or vice
    # versa, or open with non-numeric tail). Without this the user sees
    # raw  bytes — visually invisible but tokenizes weirdly.
    out = out.replace(_PLACEHOLDER_OPEN, "").replace(_PLACEHOLDER_CLOSE, "")
    return out


def _placeholder_indices_in(text: str) -> set[int]:
    """Return the set of fully-matched placeholder indices inside `text`.

    Used by the post-split repair pass to know which formulas a chunk
    captured intact.
    """
    return {int(m.group(1)) for m in _PLACEHOLDER_FULL_RE.finditer(text)}


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

    Uses token-based splitting (superior to character-based) from NLPProject's
    build_raft_dataset.py, combined with metadata tracking from chunk_with_metadata.py.

    R5/MinerU step ②: `$$...$$` LaTeX blocks are stashed to short
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
        stashed_text, formulas = _stash_block_formulas(page.text)
        tokens = enc.encode(stashed_text)

        if len(tokens) < min_tokens:
            continue

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

        # Post-split repair: any formula that disappeared (no chunk
        # captured it intact) is appended in full to the chunk where its
        # placeholder *started*. We detect "started here" by looking for
        # a dangling `` without its closing `` near the end
        # of a chunk (or a closing without opening near the start of the
        # next chunk).
        all_captured: set[int] = set().union(*(c for _, c in raw_pieces)) if raw_pieces else set()
        missing = [i for i in range(len(formulas)) if i not in all_captured]
        for missing_idx in missing:
            # Find first chunk that contains the open marker without a
            # full match — that's where the formula started.
            for j, (piece, _) in enumerate(raw_pieces):
                if _PLACEHOLDER_OPEN in piece:
                    # Append the original formula to this chunk's text
                    # so the answer LLM still has it. Newline before so
                    # it visually separates from the truncated context.
                    raw_pieces[j] = (piece + "\n\n" + formulas[missing_idx], raw_pieces[j][1])
                    break

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
