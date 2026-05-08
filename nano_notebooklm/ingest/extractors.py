"""Document text extraction for PDF, PPTX, DOCX, and Markdown.

Adapted from NLPProject/scripts/chunk_with_metadata.py with Pydantic models.
"""

from __future__ import annotations

from pathlib import Path

from nano_notebooklm.types import FileType, PageInfo


def extract_pdf(filepath: str) -> list[PageInfo]:
    """Extract text page-by-page from a PDF.

    fix-all v3 #L2: doc.close() previously ran only after the page loop
    completed; a malformed PDF that raised inside `page.get_text()`
    leaked the open document handle. Wrap in try/finally so the handle
    is always released.
    """
    import fitz

    doc = fitz.open(filepath)
    pages = []
    try:
        for page_num in range(len(doc)):
            page = doc[page_num]
            text = page.get_text().strip()
            if text and len(text) > 30:
                pages.append(PageInfo(
                    text=text,
                    page=page_num + 1,
                    total_pages=len(doc),
                ))
    finally:
        doc.close()
    return pages


def extract_pptx(filepath: str) -> list[PageInfo]:
    """Extract text slide-by-slide from a PPTX."""
    from pptx import Presentation

    prs = Presentation(filepath)
    slides = []
    for slide_num, slide in enumerate(prs.slides, 1):
        parts = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    text = para.text.strip()
                    if text:
                        parts.append(text)
        text = "\n".join(parts)
        if text and len(text) > 30:
            slides.append(PageInfo(
                text=text,
                slide=slide_num,
                total_slides=len(prs.slides),
            ))
    return slides


def extract_docx(filepath: str) -> list[PageInfo]:
    """Extract text paragraph-by-paragraph from a DOCX."""
    from docx import Document as DocxDocument

    doc = DocxDocument(filepath)
    sections = []
    current_heading = "Introduction"
    current_text: list[str] = []
    para_start = 1

    for i, para in enumerate(doc.paragraphs, 1):
        if para.style and para.style.name and para.style.name.startswith("Heading"):
            # Save previous section
            text = "\n".join(current_text).strip()
            if text and len(text) > 30:
                sections.append(PageInfo(
                    text=text,
                    section=current_heading,
                    line_start=para_start,
                    line_end=i - 1,
                ))
            current_heading = para.text.strip() or current_heading
            current_text = []
            para_start = i
        else:
            if para.text.strip():
                current_text.append(para.text.strip())

    # Last section
    text = "\n".join(current_text).strip()
    if text and len(text) > 30:
        sections.append(PageInfo(
            text=text,
            section=current_heading,
            line_start=para_start,
            line_end=len(doc.paragraphs),
        ))
    return sections


def extract_markdown(filepath: str) -> list[PageInfo]:
    """Extract text section-by-section from a Markdown file."""
    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()

    sections = []
    current_heading = "Introduction"
    current_text: list[str] = []
    line_start = 1

    for i, line in enumerate(content.split("\n"), 1):
        if line.startswith("#"):
            text = "\n".join(current_text).strip()
            if text and len(text) > 30:
                sections.append(PageInfo(
                    text=text,
                    section=current_heading,
                    line_start=line_start,
                    line_end=i - 1,
                ))
            current_heading = line.lstrip("#").strip()
            current_text = []
            line_start = i
        else:
            current_text.append(line)

    # Last section
    text = "\n".join(current_text).strip()
    if text and len(text) > 30:
        sections.append(PageInfo(
            text=text,
            section=current_heading,
            line_start=line_start,
            line_end=i if 'i' in dir() else line_start,
        ))
    return sections


def extract_file(filepath: str | Path) -> tuple[list[PageInfo], FileType]:
    """Auto-detect file type and extract text."""
    filepath = Path(filepath)
    suffix = filepath.suffix.lower()

    extractors = {
        ".pdf": (extract_pdf, FileType.PDF),
        ".pptx": (extract_pptx, FileType.PPTX),
        ".ppt": (extract_pptx, FileType.PPTX),
        ".docx": (extract_docx, FileType.DOCX),
        ".md": (extract_markdown, FileType.MARKDOWN),
        ".markdown": (extract_markdown, FileType.MARKDOWN),
        ".txt": (extract_markdown, FileType.TXT),
    }

    if suffix not in extractors:
        raise ValueError(f"Unsupported file type: {suffix}")

    func, file_type = extractors[suffix]
    return func(str(filepath)), file_type


# Directories to skip during recursive scanning
SKIP_DIRS = {".venv", "__pycache__", "node_modules", ".git", "docs"}
SUPPORTED_EXTENSIONS = {".pdf", ".pptx", ".ppt", ".docx", ".md", ".markdown", ".txt"}


def collect_files(directory: str | Path) -> list[Path]:
    """Recursively collect all supported files from a directory."""
    directory = Path(directory)
    files = []
    for ext in SUPPORTED_EXTENSIONS:
        for f in directory.rglob(f"*{ext}"):
            if not any(skip in f.parts for skip in SKIP_DIRS):
                files.append(f)
    return sorted(files)
