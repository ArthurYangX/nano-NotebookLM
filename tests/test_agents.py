"""Offline tests for subagent orchestration and formatting."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_subagent_web_research_happy(monkeypatch):
    from nano_notebooklm.agents import run_subagent

    async def fake_search(query: str, *, max_results: int = 5):
        assert query == "RAFT paper year"
        return [
            {
                "title": "RAFT: Adapting Language Model to Domain Specific RAG",
                "url": "https://example.test/raft",
                "snippet": "RAFT was introduced as retrieval augmented fine tuning.",
            }
        ]

    result = await run_subagent(
        "web_research",
        {"query": "RAFT paper year", "search_fn": fake_search},
    )

    assert result["status"] == "ok"
    assert "RAFT" in result["summary"]
    assert result["citations"][0]["url"] == "https://example.test/raft"
    assert "[Source:" in result["citation_block"]


@pytest.mark.asyncio
async def test_subagent_web_research_timeout(monkeypatch):
    from nano_notebooklm.agents import run_subagent

    async def failing_search(query: str, *, max_results: int = 5):
        raise TimeoutError("network unavailable")

    result = await run_subagent(
        "web_research",
        {"query": "supplement this answer", "search_fn": failing_search},
    )

    assert result["status"] == "fallback"
    assert result["summary"]
    assert "未补充" in result["summary"]


def test_subagent_formatter_happy():
    from nano_notebooklm.agents.formatter import format_response

    raw = "##Title\n- item\n```python\nprint(1)\n[Source: slides.pdf p.3]\n"
    formatted = format_response(raw)

    assert formatted.startswith("## Title")
    assert "```python\nprint(1)\n```" in formatted
    assert "[Source: slides.pdf p.3]" in formatted


def test_subagent_formatter_invalid():
    from nano_notebooklm.agents.formatter import format_response

    raw = "Here is math $E=mc^2 and a fence ```\nouter\n``` inner ```"
    formatted = format_response(raw)

    assert formatted.count("```") % 2 == 0
    assert formatted.count("$") % 2 == 0
    assert len(formatted) < 500
