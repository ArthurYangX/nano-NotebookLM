"""LaTeX safety sanitizer for the Note module.

The Note pipeline accepts LaTeX from two untrusted sources:
  (1) the LLM (which we prompt-restrict to a fixed macro set in
      NOTE_FORMAT_LATEX, but the LLM can still hallucinate forbidden
      commands), and
  (2) the user (CodeMirror editor in the frontend, which can paste
      anything — including `\\input{/etc/passwd}` aiming at the tectonic
      PDF compile endpoint).

This module is a pure-text regex scan. It does NOT parse TeX; that would
be over-engineering for the threat model. The goal is to reject the small
set of commands that grant arbitrary file read / shell-out / output
redirection during a tectonic run, plus a few that let the document
override our preamble.

Failure mode is `LaTeXUnsafeError` (subclass of ValueError) carrying a
human-readable `reason`. Callers translate to a 422 with that reason.
"""

from __future__ import annotations

import re
from typing import Final

MAX_LATEX_BYTES: Final[int] = 80 * 1024  # 80 KB — a single study note never legitimately exceeds this

# Each pattern is anchored to a TeX command boundary (backslash + name,
# followed by a non-letter so `\inputfoo` is not matched by `\input`).
# Patterns are case-sensitive — TeX is case-sensitive.
_FORBIDDEN_PATTERNS: Final[tuple[tuple[str, str], ...]] = (
    (r"\\documentclass(?![a-zA-Z])",       "\\documentclass is reserved for the server preamble"),
    (r"\\usepackage(?![a-zA-Z])",          "\\usepackage is reserved for the server preamble"),
    (r"\\input(?![a-zA-Z])",               "\\input can read arbitrary files"),
    (r"\\include(?![a-zA-Z])",             "\\include can read arbitrary files"),
    (r"\\InputIfFileExists(?![a-zA-Z])",   "\\InputIfFileExists can read arbitrary files"),
    (r"\\verbatiminput(?![a-zA-Z])",       "\\verbatiminput can read arbitrary files"),
    (r"\\write18(?![a-zA-Z])",             "\\write18 enables shell escape"),
    (r"\\immediate\\write18(?![a-zA-Z])",  "\\immediate\\write18 enables shell escape"),
    (r"\\openout(?![a-zA-Z])",             "\\openout enables file writes"),
    (r"\\write(?![a-zA-Z])",               "\\write enables file writes"),
    (r"\\immediate(?![a-zA-Z])",           "\\immediate is used to bypass write deferral"),
    (r"\\catcode(?![a-zA-Z])",             "\\catcode redefinition can subvert sanitization"),
    (r"\\def(?![a-zA-Z])",                 "\\def macro redefinition is not allowed"),
    (r"\\let(?![a-zA-Z])",                 "\\let macro aliasing is not allowed"),
    (r"\\newcommand(?![a-zA-Z])",          "\\newcommand is not allowed (preamble owns this)"),
    (r"\\renewcommand(?![a-zA-Z])",        "\\renewcommand is not allowed (preamble owns this)"),
    (r"\\csname(?![a-zA-Z])",              "\\csname enables arbitrary control-sequence construction"),
    (r"\\loop(?![a-zA-Z])",                "\\loop can spin the compiler"),
    (r"\\openin(?![a-zA-Z])",              "\\openin can read arbitrary files"),
    (r"\\read(?![a-zA-Z])",                "\\read can pull data from arbitrary streams"),
)


class LaTeXUnsafeError(ValueError):
    """Raised when LaTeX body fails the safety scan."""

    def __init__(self, reason: str, *, snippet: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.snippet = snippet


def _strip_shell_escape_comments(source: str) -> str:
    """Strip `%! TEX shellesc=...` / `%! TEX program=...` magic comments.

    These don't grant capabilities in tectonic itself but some downstream
    LaTeX tooling honours them; we remove them defensively so a copy-pasted
    Overleaf preamble can't smuggle a directive past us.
    """
    return re.sub(r"^%\s*!\s*TEX(?![a-zA-Z])[^\n]*\n", "", source, flags=re.MULTILINE)


def check(source: str) -> str:
    """Validate ``source`` (LaTeX body only, no preamble expected).

    Returns the sanitised source (with TeX-magic comments stripped) when
    safe. Raises ``LaTeXUnsafeError`` with a stable ``reason`` string
    otherwise.
    """
    if not isinstance(source, str):
        raise LaTeXUnsafeError("latex source must be a string")
    if not source.strip():
        raise LaTeXUnsafeError("latex source is empty")
    # Compare byte size against the cap — UTF-8 Chinese can be 3× the
    # character count, so a char-count cap is a footgun.
    if len(source.encode("utf-8")) > MAX_LATEX_BYTES:
        raise LaTeXUnsafeError(
            f"latex source exceeds {MAX_LATEX_BYTES} bytes",
        )

    return _scan_forbidden(_strip_shell_escape_comments(source))


def check_unbounded(source: str) -> str:
    """Same forbidden-command scan as ``check`` but without the 80 KB cap.

    Used by full-course notes (per-file outputs are programmatically
    concatenated + LLM-polished into a single body that can legitimately
    exceed the single-topic cap). The security regex set is identical, so
    the tectonic threat model is preserved — only the size ceiling is
    relaxed.
    """
    if not isinstance(source, str):
        raise LaTeXUnsafeError("latex source must be a string")
    if not source.strip():
        raise LaTeXUnsafeError("latex source is empty")
    return _scan_forbidden(_strip_shell_escape_comments(source))


def _scan_forbidden(cleaned: str) -> str:
    for pattern, reason in _FORBIDDEN_PATTERNS:
        match = re.search(pattern, cleaned)
        if match:
            # Capture a short snippet around the match for the error log.
            start = max(0, match.start() - 20)
            end = min(len(cleaned), match.end() + 20)
            snippet = cleaned[start:end].replace("\n", " ")
            raise LaTeXUnsafeError(reason, snippet=snippet)
    return cleaned
