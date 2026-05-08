"""list_courses — enumerate indexed courses with chunk counts."""

from __future__ import annotations

from nano_notebooklm.orchestrator.agent_tools import Tool

DESCRIPTION = """List every course indexed in the knowledge base.

Usage:
- Call this when the user asks about a course you don't know exists, or when deciding which course_id to pass to `search_kb`.
- Returns an array of {course_id, chunks}. Use `course_id` (not display name) when filtering search.
- Cheap — safe to call early in a session to ground yourself.
"""

PARAMETERS = {
    "type": "object",
    "properties": {},
}


def build_list_courses(orchestrator, kb) -> Tool:
    async def handler(args: dict):
        course_ids = orchestrator.list_courses()
        return [
            {
                "course_id": cid,
                "chunks": len(kb.get_chunks(cid)),
            }
            for cid in course_ids
        ]

    return Tool(
        name="list_courses",
        description=DESCRIPTION,
        parameters=PARAMETERS,
        handler=handler,
        is_read_only=True,
        concurrency_safe=True,
    )
