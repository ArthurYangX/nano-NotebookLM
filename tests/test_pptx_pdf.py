"""Tests for the LibreOffice-driven pptx → pdf sidecar converter.

The conversion itself requires the soffice binary, which the test host
may not have. We exercise:
  - find_soffice() honours NANO_NLM_SOFFICE_PATH env override
  - find_soffice() returns None when nothing is installed
  - convert_pptx_to_pdf() returns None gracefully when soffice missing
  - sidecar caching short-circuits a second call when the cache is fresh
  - sidecar invalidates when the source mtime advances
  - convert_directory() is a no-op when soffice missing (does not raise)
  - sidecar_path() preserves the full original filename (so .pptx and
    .pdf in the same dir do not collide on the sidecar)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nano_notebooklm.ingest import pptx_pdf


def test_find_soffice_returns_none_when_missing(monkeypatch):
    # Nuke PATH and override env so neither lookup path can find a binary.
    monkeypatch.delenv("NANO_NLM_SOFFICE_PATH", raising=False)
    monkeypatch.setenv("PATH", "/nonexistent")
    # Patch the fallback list to a guaranteed-missing path so the test
    # does not depend on whether the host has LibreOffice installed.
    monkeypatch.setattr(pptx_pdf, "_SOFFICE_FALLBACK_PATHS",
                        ("/nonexistent/soffice",))
    assert pptx_pdf.find_soffice() is None
    assert pptx_pdf.pptx_pdf_available() is False


def test_find_soffice_honours_env_override(tmp_path, monkeypatch):
    fake_bin = tmp_path / "soffice"
    fake_bin.write_text("#!/bin/sh\nexit 0\n")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("NANO_NLM_SOFFICE_PATH", str(fake_bin))
    assert pptx_pdf.find_soffice() == str(fake_bin)


def test_find_soffice_rejects_missing_override(tmp_path, monkeypatch):
    """Override pointing at a non-existent path must NOT win — we fall
    through to PATH lookup. Otherwise a typo silently disables the
    converter for everyone using the env var."""
    monkeypatch.setenv("NANO_NLM_SOFFICE_PATH", str(tmp_path / "absent"))
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setattr(pptx_pdf, "_SOFFICE_FALLBACK_PATHS",
                        ("/nonexistent/soffice",))
    assert pptx_pdf.find_soffice() is None


def test_sidecar_path_preserves_original_name(tmp_path):
    """A `lecture1.pptx` sidecar must be named `lecture1.pptx.pdf`, not
    `lecture1.pdf` — the latter would collide with a separately-uploaded
    `lecture1.pdf` in the same course."""
    sc = pptx_pdf.sidecar_path(tmp_path, "lecture1.pptx")
    assert sc == tmp_path / "lecture1.pptx.pdf"
    # Path-traversal: callers may pass an attacker-controlled source_file;
    # sidecar_path strips the directory component via Path.name.
    sc2 = pptx_pdf.sidecar_path(tmp_path, "../../etc/passwd.pptx")
    assert sc2 == tmp_path / "passwd.pptx.pdf"


def test_needs_conversion_missing_sidecar(tmp_path):
    src = tmp_path / "deck.pptx"
    src.write_bytes(b"PK\x03\x04")  # minimal zip magic
    sc = tmp_path / "deck.pptx.pdf"
    assert pptx_pdf.needs_conversion(src, sc) is True


def test_needs_conversion_stale_sidecar(tmp_path):
    src = tmp_path / "deck.pptx"
    sc = tmp_path / "deck.pptx.pdf"
    sc.write_bytes(b"%PDF-stale")
    src.write_bytes(b"PK\x03\x04")
    # Force src mtime to advance past sidecar's by 5s.
    import os, time
    past = sc.stat().st_mtime - 5
    os.utime(sc, (past, past))
    assert pptx_pdf.needs_conversion(src, sc) is True


def test_needs_conversion_fresh_sidecar(tmp_path):
    src = tmp_path / "deck.pptx"
    sc = tmp_path / "deck.pptx.pdf"
    src.write_bytes(b"PK\x03\x04")
    sc.write_bytes(b"%PDF-fresh")
    # sc mtime > src mtime → no conversion needed
    import os
    future = src.stat().st_mtime + 5
    os.utime(sc, (future, future))
    assert pptx_pdf.needs_conversion(src, sc) is False


def test_convert_pptx_to_pdf_returns_none_when_soffice_missing(tmp_path, monkeypatch):
    src = tmp_path / "deck.pptx"
    src.write_bytes(b"PK\x03\x04")
    preview = tmp_path / "previews"
    monkeypatch.setattr(pptx_pdf, "find_soffice", lambda: None)
    assert pptx_pdf.convert_pptx_to_pdf(src, preview) is None
    # And we did NOT eagerly create the preview dir when there is nothing
    # to write — keeps the artifacts tree tidy on hosts without soffice.
    assert not preview.exists()


def test_convert_pptx_to_pdf_returns_none_for_missing_source(tmp_path, monkeypatch):
    monkeypatch.setattr(pptx_pdf, "find_soffice", lambda: "/usr/bin/soffice")
    assert pptx_pdf.convert_pptx_to_pdf(
        tmp_path / "absent.pptx", tmp_path / "previews",
    ) is None


def test_convert_pptx_to_pdf_uses_cache_without_running_soffice(tmp_path, monkeypatch):
    """When a fresh sidecar exists we MUST short-circuit before invoking
    subprocess — otherwise repeated Reader visits cost a soffice cold-start
    each time. We assert the cache hit by stubbing find_soffice to a path
    that would crash if actually executed."""
    src = tmp_path / "deck.pptx"
    src.write_bytes(b"PK\x03\x04")
    preview = tmp_path / "previews"
    preview.mkdir()
    sc = preview / "deck.pptx.pdf"
    sc.write_bytes(b"%PDF-1.4 cached")
    import os
    future = src.stat().st_mtime + 5
    os.utime(sc, (future, future))

    def _explode(*a, **kw):
        raise AssertionError("subprocess.run must not run on cache hit")

    monkeypatch.setattr(pptx_pdf, "find_soffice", lambda: "/usr/bin/soffice")
    monkeypatch.setattr(pptx_pdf.subprocess, "run", _explode)
    result = pptx_pdf.convert_pptx_to_pdf(src, preview)
    assert result == sc
    assert sc.read_bytes() == b"%PDF-1.4 cached"  # untouched


def test_convert_pptx_to_pdf_handles_subprocess_failure(tmp_path, monkeypatch):
    """A non-zero soffice exit code must NOT raise — the upload pipeline
    treats sidecar gen as best-effort. We also pin that no half-written
    sidecar lands on disk on failure."""
    src = tmp_path / "deck.pptx"
    src.write_bytes(b"PK\x03\x04")
    preview = tmp_path / "previews"

    class _CompletedProcess:
        returncode = 1
        stderr = b"unable to load deck"
        stdout = b""

    monkeypatch.setattr(pptx_pdf, "find_soffice", lambda: "/usr/bin/soffice")
    monkeypatch.setattr(pptx_pdf.subprocess, "run", lambda *a, **kw: _CompletedProcess())
    assert pptx_pdf.convert_pptx_to_pdf(src, preview) is None
    sc = preview / "deck.pptx.pdf"
    assert not sc.exists()


def test_convert_directory_no_op_when_soffice_missing(tmp_path, monkeypatch):
    upload = tmp_path / "uploads"
    upload.mkdir()
    (upload / "a.pptx").write_bytes(b"PK\x03\x04")
    (upload / "b.pptx").write_bytes(b"PK\x03\x04")
    (upload / "ignore.pdf").write_bytes(b"%PDF-")
    monkeypatch.setattr(pptx_pdf, "find_soffice", lambda: None)
    out = pptx_pdf.convert_directory(upload, tmp_path / "previews")
    assert out == {}


def test_convert_directory_skips_non_pptx(tmp_path, monkeypatch):
    upload = tmp_path / "uploads"
    upload.mkdir()
    (upload / "deck.pptx").write_bytes(b"PK\x03\x04")
    (upload / "notes.pdf").write_bytes(b"%PDF-")
    (upload / "readme.md").write_text("# notes")
    preview = tmp_path / "previews"
    preview.mkdir()

    seen: list[str] = []

    def _fake_convert(src, prev_dir, *, force=False, soffice=None):
        seen.append(src.name)
        sc = pptx_pdf.sidecar_path(prev_dir, src.name)
        sc.write_bytes(b"%PDF-fake")
        return sc

    monkeypatch.setattr(pptx_pdf, "find_soffice", lambda: "/usr/bin/soffice")
    monkeypatch.setattr(pptx_pdf, "convert_pptx_to_pdf", _fake_convert)
    out = pptx_pdf.convert_directory(upload, preview)
    assert seen == ["deck.pptx"]  # PDF + MD ignored
    assert out["deck.pptx"] == preview / "deck.pptx.pdf"
