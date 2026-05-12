"""Pin the pptx ↔ pdf dedup pass in `collect_files`.

User scenario (R5-2 fix-all v4 #1): student drags `lecture1.pptx` AND
`lecture1.pdf` (the exported handout) into the upload dialog. Pre-fix,
both files chunked → sources panel showed two rows for the same lecture
and FAISS double-counted the slide content. The dedup pass now drops
the pdf in favor of the semantically-richer pptx.
"""

from __future__ import annotations

from pathlib import Path

from nano_notebooklm.ingest.extractors import collect_files


def test_collect_files_drops_pdf_when_sibling_pptx_present(tmp_path):
    (tmp_path / "lecture1.pptx").write_bytes(b"fake")
    (tmp_path / "lecture1.pdf").write_bytes(b"fake")
    files = collect_files(tmp_path)
    suffixes = [f.suffix.lower() for f in files]
    assert ".pptx" in suffixes
    assert ".pdf" not in suffixes


def test_collect_files_keeps_pdf_when_no_sibling_pptx(tmp_path):
    """A standalone pdf with no pptx twin must still be indexed."""
    (tmp_path / "homework1.pdf").write_bytes(b"fake")
    (tmp_path / "lecture1.pptx").write_bytes(b"fake")  # different stem
    files = collect_files(tmp_path)
    names = {f.name for f in files}
    assert "homework1.pdf" in names
    assert "lecture1.pptx" in names


def test_collect_files_dedup_is_per_directory(tmp_path):
    """A pdf in dir A must not be dropped because of a pptx in dir B
    (different parent). Subdirs are scanned recursively but dedup
    matches on `(parent, stem)`, not stem alone."""
    sub_a = tmp_path / "module_a"
    sub_b = tmp_path / "module_b"
    sub_a.mkdir()
    sub_b.mkdir()
    (sub_a / "intro.pptx").write_bytes(b"fake")
    (sub_b / "intro.pdf").write_bytes(b"fake")  # different parent
    files = collect_files(tmp_path)
    names = {f.name for f in files}
    assert {"intro.pptx", "intro.pdf"}.issubset(names)


def test_collect_files_dedup_handles_ppt_legacy_suffix(tmp_path):
    """Legacy `.ppt` (PowerPoint 97-2003) also wins over a same-stem pdf."""
    (tmp_path / "old.ppt").write_bytes(b"fake")
    (tmp_path / "old.pdf").write_bytes(b"fake")
    files = collect_files(tmp_path)
    suffixes = {f.suffix.lower() for f in files}
    assert ".ppt" in suffixes
    assert ".pdf" not in suffixes


def test_collect_files_dedup_preserves_unrelated_pdfs(tmp_path):
    """All three of (a.pptx, b.pdf, c.pdf) live; only b.pdf has a sibling
    a.pptx if we lazily match — verify exact-stem-only matching."""
    (tmp_path / "a.pptx").write_bytes(b"fake")
    (tmp_path / "b.pdf").write_bytes(b"fake")
    (tmp_path / "c.pdf").write_bytes(b"fake")
    files = collect_files(tmp_path)
    names = {f.name for f in files}
    assert "a.pptx" in names
    assert "b.pdf" in names  # No b.pptx exists — keep b.pdf
    assert "c.pdf" in names  # Same: keep c.pdf
