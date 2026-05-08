"""Round 2 #6 — pin CJK font fallbacks in frontend/styles.css.

Doesn't render CSS; just greps the three font-stack tokens. Cheap and
deterministic — guards against a future "cleanup" PR that silently drops
the CJK fallbacks and breaks Chinese course names in the topbar dropdown.
"""

from __future__ import annotations

from pathlib import Path

STYLES = Path(__file__).resolve().parent.parent / "frontend" / "styles.css"

# At least one CJK family from each major platform should appear in every
# global font stack (--serif / --sans / --mono).
_REQUIRED = ("PingFang SC", "Microsoft YaHei", "Noto Sans")


def test_cjk_fallback_present_in_all_global_font_stacks():
    text = STYLES.read_text(encoding="utf-8")
    # Pull each `--xxx: ...;` global declaration's value
    for token in ("--serif", "--sans", "--mono"):
        # Find the line. Stacks span multiple lines after our Round 2 #6
        # rewrite, so capture from the token to its terminating `;`.
        idx = text.find(token + ":")
        assert idx != -1, f"{token} declaration not found in styles.css"
        end = text.find(";", idx)
        stack = text[idx:end]
        assert any(f in stack for f in _REQUIRED), (
            f"{token} stack missing CJK fallback (one of {_REQUIRED}); "
            f"got: {stack!r}"
        )


def test_long_filename_chip_has_overflow_guard():
    """Long Chinese PDF names like 深入理解计算机系统(中文版).pdf used to wrap
    and break chat-bubble layout. The .ref-chip rule must clip + ellipsis."""
    text = STYLES.read_text(encoding="utf-8")
    chip_block_start = text.index(".msg .refs .ref-chip")
    chip_block_end = text.index("}", chip_block_start)
    block = text[chip_block_start:chip_block_end]
    for prop in ("max-width", "text-overflow: ellipsis", "white-space: nowrap"):
        assert prop in block, f".ref-chip missing {prop!r}: {block!r}"
