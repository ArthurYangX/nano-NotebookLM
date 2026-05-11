"""Unit tests for nano_notebooklm.skills.latex_sanitizer."""

from __future__ import annotations

import pytest

from nano_notebooklm.skills.latex_sanitizer import (
    check,
    check_unbounded,
    LaTeXUnsafeError,
    MAX_LATEX_BYTES,
)


def test_happy_path_allowed_macros():
    body = (
        r"\section{Intro}" "\n"
        r"\subsection{Background}" "\n"
        r"\textbf{Key term}: a definition." "\n"
        r"\begin{theorem}[Pythagoras]" "\n"
        r"  $a^2 + b^2 = c^2$" "\n"
        r"\end{theorem}" "\n"
        r"\begin{proof}" "\n"
        r"Standard geometric argument." "\n"
        r"\end{proof}" "\n"
        r"\cite{ml.pdf:p.1}" "\n"
    )
    cleaned = check(body)
    # Sanitizer is a pass-through on safe input (only strips TeX magic comments).
    assert cleaned == body


def test_happy_path_chinese():
    body = (
        r"\section{第一章 导论}" "\n"
        r"\begin{definition}[卷积神经网络]" "\n"
        r"卷积神经网络（CNN）使用 $k \times k$ 卷积核提取空间特征。" "\n"
        r"\end{definition}" "\n"
    )
    cleaned = check(body)
    assert "卷积" in cleaned


@pytest.mark.parametrize("payload,fragment", [
    (r"\input{/etc/passwd}", r"\input"),
    (r"a \include{secrets} b", r"\include"),
    (r"\InputIfFileExists{/etc/passwd}{}{}", r"\InputIfFileExists"),
    (r"\verbatiminput{/etc/passwd}", r"\verbatiminput"),
    (r"\write18{rm -rf /}", r"\write18"),
    (r"\immediate something", r"\immediate"),
    (r"\openout5=foo", r"\openout"),
    (r"\write5{x}", r"\write"),
    (r"\catcode`@=11", r"\catcode"),
    (r"\def\foo{bar}", r"\def"),
    (r"\let\foo\bar", r"\let"),
    (r"\newcommand{\foo}{bar}", r"\newcommand"),
    (r"\renewcommand{\foo}{bar}", r"\renewcommand"),
    (r"\csname endlinechar\endcsname", r"\csname"),
    (r"\loop\iftrue\repeat", r"\loop"),
    (r"\openin1=foo", r"\openin"),
    (r"\read1 to \line", r"\read"),
    (r"\documentclass{article}", r"\documentclass"),
    (r"\usepackage{anything}", r"\usepackage"),
])
def test_rejects_forbidden_commands(payload, fragment):
    with pytest.raises(LaTeXUnsafeError) as exc_info:
        check(payload)
    # The reason should be deterministic and human-readable.
    assert exc_info.value.reason
    assert fragment.lstrip("\\") in exc_info.value.reason or fragment in exc_info.value.reason


def test_empty_input_rejected():
    with pytest.raises(LaTeXUnsafeError):
        check("")
    with pytest.raises(LaTeXUnsafeError):
        check("   \n\n  ")


def test_non_string_rejected():
    with pytest.raises(LaTeXUnsafeError):
        check(None)  # type: ignore[arg-type]
    with pytest.raises(LaTeXUnsafeError):
        check(123)  # type: ignore[arg-type]


def test_size_cap_byte_aware():
    # ASCII payload safely under cap → pass; clearly over → fail.
    just_under = "x" * (MAX_LATEX_BYTES - 100) + r"\section{Hi}"
    assert check(just_under)
    just_over = "x" * (MAX_LATEX_BYTES + 1)
    with pytest.raises(LaTeXUnsafeError) as exc_info:
        check(just_over)
    assert "exceeds" in exc_info.value.reason


def test_size_cap_counts_utf8_bytes_not_chars():
    # Chinese characters are 3 bytes in UTF-8. A char-count cap would let
    # 3× the byte cap through; verify the byte cap rejects.
    chinese = "中" * (MAX_LATEX_BYTES // 3 + 100)
    with pytest.raises(LaTeXUnsafeError):
        check(chinese)


def test_strips_tex_magic_comments():
    body = (
        "%! TEX program = xelatex\n"
        "%!TEX shellescape = 1\n"
        r"\section{Hi}" "\n"
    )
    cleaned = check(body)
    assert "TEX" not in cleaned.split("\\section")[0]
    assert r"\section{Hi}" in cleaned


def test_command_boundary_is_respected():
    # `\inputfoo` is a different macro from `\input`. Sanitizer should
    # NOT reject — boundary check (`(?![a-zA-Z])`) is the whole point.
    body = r"\section{Test} \inputfoo{harmless}"
    # `\inputfoo` is not on the allowed list either, but the sanitizer's
    # job is to block dangerous primitives, not enforce the allow-list —
    # that's the prompt's job. So this should pass sanitize.
    assert check(body)


# review-swarm fix-all v1 #15: more boundary edge cases.


def test_forbidden_command_in_macro_argument_is_rejected():
    r"""Embedding `\write` inside another macro arg doesn't sanitise it —
    the sanitizer is a text scan, the regex still matches."""
    with pytest.raises(LaTeXUnsafeError) as exc:
        check(r"\section{Test} \textbf{innocent \write 18 bad}")
    assert "write" in exc.value.reason.lower()


def test_forbidden_command_inside_math_is_rejected():
    r"""TeX evaluates commands inside math mode too. `$\write18$` is
    still capable of shell escape in a real compile."""
    with pytest.raises(LaTeXUnsafeError):
        check(r"\section{Hi} $a + \write18{rm} = b$")


def test_command_after_newline_is_caught():
    """Forbidden commands can be preceded by newlines / tabs / arbitrary
    whitespace. The regex must not anchor on word boundary in a way
    that misses leading whitespace."""
    with pytest.raises(LaTeXUnsafeError):
        check("\\section{Hi}\n\n   \\input{/etc/passwd}")


def test_double_backslash_escaped_command_is_text_not_command():
    r"""In LaTeX source, `\\write18` is literal text `\write18` (the
    `\\` is a line break). Our regex still matches it, which is a
    false-positive — but it's the safer side of the trade-off (refuse
    rather than admit a borderline payload). Pin the behaviour."""
    with pytest.raises(LaTeXUnsafeError):
        check(r"some text \\write18 more text")


def test_command_in_comment_is_still_caught():
    r"""The sanitizer does NOT strip TeX comments (only the magic-TEX
    shellescape comments). So `% \write18` survives and matches.
    Trade-off: rare false positive (LLM rarely writes legitimate `%
    \write18` examples), but blocks the case where an attacker hides
    a command behind a comment that gets uncommented later by stream
    truncation. Pin behaviour either way."""
    with pytest.raises(LaTeXUnsafeError):
        check("\\section{Hi}\n% \\write18 example\nbody")


# ── check_unbounded — review-swarm fix-all v1 #15 ────────────────────


def test_check_unbounded_happy_path():
    """The same allowed-macro body that passes check() must also pass
    check_unbounded() — they share the forbidden-command list."""
    body = (
        r"\section{Intro}" "\n"
        r"\textbf{Key term}: a definition." "\n"
        r"\begin{theorem}$a^2+b^2=c^2$\end{theorem}" "\n"
    )
    assert check_unbounded(body) == body


def test_check_unbounded_rejects_empty():
    with pytest.raises(LaTeXUnsafeError):
        check_unbounded("")
    with pytest.raises(LaTeXUnsafeError):
        check_unbounded("   \n  \t")


def test_check_unbounded_rejects_non_string():
    with pytest.raises(LaTeXUnsafeError):
        check_unbounded(None)  # type: ignore[arg-type]
    with pytest.raises(LaTeXUnsafeError):
        check_unbounded(123)  # type: ignore[arg-type]


@pytest.mark.parametrize("payload,fragment", [
    (r"\input{/etc/passwd}", "\\input"),
    (r"\write18{rm -rf /}", "\\write18"),
    (r"\immediate\write{/tmp/x}{data}", "\\immediate"),
    (r"\openout5=/tmp/out", "\\openout"),
    (r"\def\evil{x}", "\\def"),
    (r"\let\foo=\bar", "\\let"),
    (r"\newcommand\foo{x}", "\\newcommand"),
    (r"\catcode`\\=12", "\\catcode"),
    (r"\csname evil\endcsname", "\\csname"),
    (r"\loop\foo\repeat", "\\loop"),
])
def test_check_unbounded_rejects_forbidden_commands(payload, fragment):
    body = r"\section{Hi}" + "\n" + payload + "\nbody"
    with pytest.raises(LaTeXUnsafeError) as exc:
        check_unbounded(body)
    # The same reason set as check() — the implementations share regex.
    assert exc.value.reason


def test_check_unbounded_bypasses_size_cap():
    """A body larger than MAX_LATEX_BYTES must pass check_unbounded
    (provided it contains no forbidden commands), but fail check()."""
    # Build a ~200 KB body of safe LaTeX. xeCJK chars are 3 bytes each so
    # ~70k characters is enough to clear 80 KB; we'll just repeat ASCII.
    big = r"\section{Big}" + "\n" + ("safe body text " * 15000)
    assert len(big.encode("utf-8")) > MAX_LATEX_BYTES

    # check_unbounded accepts the size
    out = check_unbounded(big)
    assert out.startswith(r"\section{Big}")
    # check() (with cap) rejects the same body
    with pytest.raises(LaTeXUnsafeError):
        check(big)


def test_check_unbounded_huge_body_with_forbidden_command_still_caught():
    """The cap-bypass must NOT bypass the forbidden-command scan — a 200KB
    body that hides a single \\input still has to be rejected."""
    head = r"\section{Big}" + "\n" + ("safe body text " * 8000)
    tail = " more body " * 8000
    payload = head + r"\input{/etc/passwd}" + tail
    assert len(payload.encode("utf-8")) > MAX_LATEX_BYTES
    with pytest.raises(LaTeXUnsafeError) as exc:
        check_unbounded(payload)
    assert "\\input" in exc.value.reason


def test_check_unbounded_strips_tex_magic_comments():
    """check_unbounded must apply the same _strip_shell_escape_comments
    pass as check()."""
    body = "%! TEX shellesc=1\n\\section{Hi}\nbody\n"
    out = check_unbounded(body)
    assert "%! TEX shellesc" not in out
    assert r"\section{Hi}" in out
