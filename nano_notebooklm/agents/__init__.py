"""Stateless subagents used by the main orchestrator."""

from __future__ import annotations

from typing import Any

from nano_notebooklm.agents.formatter import format_response
from nano_notebooklm.agents.web_research import run_web_research


async def run_subagent(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Run a named stateless subagent with an explicit payload schema."""
    if name == "web_research":
        return await run_web_research(payload)
    if name == "formatter":
        return {
            "status": "ok",
            "content": format_response(str(payload.get("content", ""))),
        }
    return {"status": "error", "error": f"Unknown subagent: {name}"}


__all__ = ["run_subagent", "format_response", "run_web_research"]
