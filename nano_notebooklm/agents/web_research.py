"""Web research subagent.

Production callers may inject a search function backed by Tavily/Bing/Serper.
Tests inject a fake function. Without a key/search function this subagent
returns a graceful fallback and never attempts network access.
"""

from __future__ import annotations

import inspect
import os
import re
from typing import Any, Awaitable, Callable


SearchFn = Callable[..., Awaitable[list[dict[str, Any]]] | list[dict[str, Any]]]

INJECTION_PATTERNS = re.compile(
    r"(ignore\s+previous|system\s+prompt|developer\s+message|"
    r"reveal\s+secrets|exfiltrate|prompt\s+injection)",
    re.IGNORECASE,
)


async def run_web_research(payload: dict[str, Any]) -> dict[str, Any]:
    query = str(payload.get("query", "")).strip()
    if not query:
        return _fallback("empty query")

    search_fn = payload.get("search_fn")
    api_key = payload.get("api_key") or os.getenv("NANO_WEB_SEARCH_API_KEY")
    if search_fn is None and not api_key:
        return _fallback("missing search API key")

    try:
        if search_fn is None:
            raise RuntimeError("search provider is not configured")
        raw = search_fn(query, max_results=int(payload.get("max_results", 5)))
        results = await raw if inspect.isawaitable(raw) else raw
    except Exception as exc:  # network unavailable, timeout, provider error
        return _fallback(str(exc))

    safe_results: list[dict[str, str]] = []
    for item in results or []:
        title = str(item.get("title", "")).strip()
        url = str(item.get("url", "")).strip()
        snippet = str(item.get("snippet", item.get("content", ""))).strip()
        combined = f"{title}\n{url}\n{snippet}"
        if INJECTION_PATTERNS.search(combined):
            continue
        if title and url:
            safe_results.append({"title": title, "url": url, "snippet": snippet})

    if not safe_results:
        return _fallback("no safe search results")

    bullets = [f"- {r['title']}: {r['snippet']}" for r in safe_results[:3]]
    citation_lines = [f"[Source: {r['title']} — {r['url']}]" for r in safe_results[:3]]
    return {
        "status": "ok",
        "summary": "\n".join(bullets),
        "citations": safe_results[:3],
        "citation_block": "\n".join(citation_lines),
    }


def _fallback(reason: str) -> dict[str, Any]:
    return {
        "status": "fallback",
        "summary": f"未补充：web research unavailable ({reason}).",
        "citations": [],
        "citation_block": "",
        "error": reason,
    }
