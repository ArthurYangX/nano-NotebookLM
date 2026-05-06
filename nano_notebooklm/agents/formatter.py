"""Output formatting subagent.

The formatter is deliberately deterministic and local: it repairs common
Markdown / LaTeX / citation artifacts without making model calls.
"""

from __future__ import annotations

import re


SOURCE_RE = re.compile(r"\[Source:\s*([^\]]+)\]")


def format_response(content: str) -> str:
    """Repair Markdown enough for stable rendering without looping."""
    text = str(content or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""

    text = _repair_headings(text)
    text = _repair_citations(text)
    text = _repair_fences(text)
    text = _repair_inline_math(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _repair_headings(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        hashes, title = match.group(1), match.group(2).strip()
        return f"{hashes} {title}"

    return re.sub(r"^(#{1,6})([^\s#].*)$", repl, text, flags=re.MULTILINE)


def _repair_citations(text: str) -> str:
    seen: set[str] = set()

    def repl(match: re.Match[str]) -> str:
        source = re.sub(r"\s+", " ", match.group(1)).strip()
        if not source:
            source = "unknown"
        seen.add(source)
        return f"[Source: {source}]"

    return SOURCE_RE.sub(repl, text)


def _repair_fences(text: str) -> str:
    text = text.replace(" ```", "\n```")
    text = re.sub(r"```([A-Za-z0-9_+.-]+)\n+", r"```\1\n", text)
    lines = text.split("\n")
    out: list[str] = []
    in_fence = False
    for line in lines:
        stripped = line.strip()
        if in_fence and SOURCE_RE.fullmatch(stripped):
            out.append("```")
            in_fence = False
            out.append(stripped)
            continue
        if stripped.startswith("```"):
            ticks = "```"
            lang = stripped[3:].strip()
            if not in_fence:
                lang = re.sub(r"[^A-Za-z0-9_+.-]", "", lang) or ""
                out.append(ticks + lang)
                in_fence = True
            else:
                out.append(ticks)
                in_fence = False
            continue
        out.append(line)
    if in_fence:
        out.append("```")
    return "\n".join(out)


def _repair_inline_math(text: str) -> str:
    # Only handle simple odd-dollar cases. Display math / code fences are left
    # intact; the intent is to avoid broken renderers, not parse TeX.
    if text.count("$") % 2 == 1:
        text += "$"
    return text
