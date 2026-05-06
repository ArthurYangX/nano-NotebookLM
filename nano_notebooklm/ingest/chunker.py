"""Token-based text chunking with metadata preservation.

Adapted from NLPProject/scripts/build_raft_dataset.py (token-based)
and chunk_with_metadata.py (metadata tracking).
"""

from __future__ import annotations

from nano_notebooklm import config
from nano_notebooklm.types import Chunk, FileType, PageInfo
from nano_notebooklm.utils.token_counter import get_encoder


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
    """
    enc = get_encoder()
    chunks: list[Chunk] = []
    seq = 0

    for page in pages:
        tokens = enc.encode(page.text)

        if len(tokens) < min_tokens:
            continue

        if len(tokens) <= chunk_size:
            # Entire page/slide fits in one chunk
            chunks.append(_make_chunk(
                text=page.text,
                page=page,
                source_file=source_file,
                file_type=file_type,
                course_id=course_id,
                doc_id=doc_id,
                seq=seq,
            ))
            seq += 1
        else:
            # Split into overlapping token-based chunks
            start = 0
            sub_idx = 0
            while start < len(tokens):
                end = min(start + chunk_size, len(tokens))
                chunk_tokens = tokens[start:end]
                if len(chunk_tokens) >= min_tokens:
                    chunk_text = enc.decode(chunk_tokens)
                    chunks.append(_make_chunk(
                        text=chunk_text,
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
                start += chunk_size - overlap

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
