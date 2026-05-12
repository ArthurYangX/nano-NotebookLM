"""LibreOffice-driven PPTX → PDF sidecar converter.

The browser's native PDF viewer (Chrome PDFium / Safari Preview) gives
the Reader a far better experience than the text-mode chunk dump we fall
back to for PPTX. This module produces a sidecar PDF rendering of an
uploaded .pptx so `/api/source/.../file` can serve a PDF and the Reader's
`<DocumentPdfFrame>` can render it natively (search / zoom / `#page=N`).

Design notes:
  - Sidecars live under `artifacts/courses/<course_id>/previews/` so the
    ingest scan over `artifacts/uploads/<course_id>/` does not pick them
    up as second-class PDFs and double-index the content.
  - Cached by source mtime: we re-run conversion only when the .pptx is
    newer than the sidecar (or the sidecar is missing).
  - Best-effort: if soffice is not on PATH this module is a no-op
    returning `None` from every call. Callers MUST NOT depend on a
    successful conversion — the Reader text-mode path stays as the
    fallback when no sidecar exists.
  - Conversion runs in `subprocess.run` with a hard timeout so a
    pathological deck cannot pin the upload pipeline.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# Hard ceiling on a single soffice invocation. LibreOffice cold-start adds
# ~3-6s on macOS; a 100-slide deck typically converts in <20s. 90s is
# generous for outliers without letting a hung soffice block uploads.
SOFFICE_TIMEOUT_SECONDS = float(os.environ.get("NANO_NLM_SOFFICE_TIMEOUT", "90"))

# Filesystem locations LibreOffice may install to. PATH lookup wins; these
# are macOS / common-Linux fallbacks for installs that did not symlink.
_SOFFICE_FALLBACK_PATHS = (
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    "/usr/bin/soffice",
    "/usr/bin/libreoffice",
    "/usr/local/bin/soffice",
    "/opt/homebrew/bin/soffice",
)


def find_soffice() -> str | None:
    """Locate the LibreOffice CLI binary, or return None.

    Resolution order:
      1. `NANO_NLM_SOFFICE_PATH` env override (operators on locked-down
         systems can pin an exact binary).
      2. `shutil.which("soffice")` / `shutil.which("libreoffice")`.
      3. Hard-coded fallback paths (macOS .app bundle, Homebrew, distro).
    """
    override = os.environ.get("NANO_NLM_SOFFICE_PATH")
    if override and Path(override).is_file() and os.access(override, os.X_OK):
        return override
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in _SOFFICE_FALLBACK_PATHS:
        if Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def pptx_pdf_available() -> bool:
    """Cheap probe — true when a sidecar conversion will succeed in
    principle (binary present, not just installed somewhere unreachable).
    """
    return find_soffice() is not None


def sidecar_path(preview_dir: Path, source_file: str) -> Path:
    """Resolve `<preview_dir>/<source_file>.pdf` (with the original suffix
    preserved so `lecture1.pptx` → `lecture1.pptx.pdf`).

    Keeping the full original name + .pdf avoids collision with a
    separately-uploaded `lecture1.pdf` and makes the sidecar's provenance
    obvious from `ls`.
    """
    leaf = Path(source_file).name
    return preview_dir / f"{leaf}.pdf"


def needs_conversion(pptx_path: Path, sidecar: Path) -> bool:
    """True when the sidecar is missing or older than the source pptx."""
    if not sidecar.exists():
        return True
    try:
        return pptx_path.stat().st_mtime > sidecar.stat().st_mtime
    except OSError:
        return True


def convert_pptx_to_pdf(
    pptx_path: Path,
    preview_dir: Path,
    *,
    force: bool = False,
    soffice: str | None = None,
) -> Path | None:
    """Render a .pptx to a sidecar PDF inside `preview_dir`.

    Returns the sidecar path on success (cache hit included), None when
    soffice is unavailable, the source is missing, or the conversion
    failed. Never raises.

    `force=True` re-runs conversion even when an up-to-date sidecar
    exists — useful from a future "regenerate previews" admin endpoint.
    """
    if not pptx_path.is_file():
        return None
    binary = soffice or find_soffice()
    if binary is None:
        return None
    preview_dir.mkdir(parents=True, exist_ok=True)
    sidecar = sidecar_path(preview_dir, pptx_path.name)
    if not force and not needs_conversion(pptx_path, sidecar):
        return sidecar
    # LibreOffice writes `<basename>.pdf` (replacing the .pptx suffix)
    # next to --outdir. We want `<basename>.pptx.pdf` so the sidecar name
    # is unambiguous; rename after the call. Use a per-invocation temp
    # subdir so concurrent conversions of differently-named files in the
    # same preview_dir cannot race on the soffice intermediate output.
    import tempfile
    with tempfile.TemporaryDirectory(prefix="soffice-out-", dir=str(preview_dir)) as tmpd:
        tmp_outdir = Path(tmpd)
        cmd = [
            binary,
            "--headless",
            "--norestore",
            "--nologo",
            "--nofirststartwizard",
            "--convert-to", "pdf",
            "--outdir", str(tmp_outdir),
            str(pptx_path),
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=SOFFICE_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "pptx_pdf.timeout source=%s timeout=%.0fs",
                pptx_path.name, SOFFICE_TIMEOUT_SECONDS,
            )
            return None
        except OSError as exc:
            logger.warning("pptx_pdf.spawn_failed source=%s err=%s",
                           pptx_path.name, exc.__class__.__name__)
            return None
        if result.returncode != 0:
            # stderr can leak temp paths; log only the exit code + stderr
            # tail (last 200 chars) so we keep the diagnostic without
            # bloating logs on a deck that produces megabytes of warnings.
            tail = (result.stderr or b"").decode("utf-8", "replace")[-200:]
            logger.warning(
                "pptx_pdf.convert_failed source=%s rc=%d tail=%r",
                pptx_path.name, result.returncode, tail,
            )
            return None
        produced = tmp_outdir / f"{pptx_path.stem}.pdf"
        if not produced.is_file():
            logger.warning("pptx_pdf.no_output source=%s", pptx_path.name)
            return None
        try:
            # os.replace is atomic on POSIX — readers either see the old
            # sidecar or the new one, never a half-written file.
            os.replace(produced, sidecar)
        except OSError as exc:
            logger.warning("pptx_pdf.rename_failed source=%s err=%s",
                           pptx_path.name, exc.__class__.__name__)
            return None
    logger.info("pptx_pdf.ok source=%s sidecar=%s bytes=%d",
                pptx_path.name, sidecar.name, sidecar.stat().st_size)
    return sidecar


def convert_directory(
    upload_dir: Path,
    preview_dir: Path,
    *,
    force: bool = False,
) -> dict[str, Path | None]:
    """Convert every .pptx in `upload_dir` (non-recursive); returns a map
    of source filename → sidecar path (or None on failure / skip).

    Caller logs the aggregate result; this helper never raises so a single
    bad deck cannot abort a multi-file upload.
    """
    binary = find_soffice()
    out: dict[str, Path | None] = {}
    if binary is None:
        return out
    if not upload_dir.is_dir():
        return out
    for entry in sorted(upload_dir.iterdir()):
        if entry.suffix.lower() != ".pptx" or not entry.is_file():
            continue
        out[entry.name] = convert_pptx_to_pdf(
            entry, preview_dir, force=force, soffice=binary,
        )
    return out
