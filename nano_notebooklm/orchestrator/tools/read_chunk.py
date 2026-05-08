"""read_chunk — fetch a single chunk's full text by chunk_id."""

from __future__ import annotations

from nano_notebooklm.orchestrator.agent_tools import Tool

# Cap text returned per call to bound prompt growth in long agent loops.
# 8KB is enough for a normal slide / paragraph; rare oversized chunks get
# truncated with an explicit "(truncated, N more)" tail so the model knows.
MAX_CHUNK_TEXT_BYTES = 8 * 1024

DESCRIPTION = """Read the full text of a single chunk by its chunk_id.

Usage:
- Use this to expand context when the truncated text in a `search_kb` result isn't enough to answer.
- chunk_id values are stable across the session — copy them verbatim from a previous search result.
- Returns {chunk_id, course_id, source_file, location, text}. The text field is the chunk's full body (not truncated).
- If the chunk is not found, returns {error: "not_found", chunk_id}. Don't retry — the id is wrong or the index has been rebuilt.
- Cheap; safe to call several times in parallel (the loop batches read-only tools).
"""

PARAMETERS = {
    "type": "object",
    "properties": {
        "chunk_id": {
            "type": "string",
            "description": "The chunk_id from a previous search_kb result.",
        },
    },
    "required": ["chunk_id"],
}


def build_read_chunk(kb, lock_course_id: str | None = None) -> Tool:
    """When ``lock_course_id`` is set, reads from any other course return
    a `cross_course_denied` error rather than the chunk text. Without this
    a prompt-injected agent in course A can read chunks from course B by
    calling ``read_chunk`` on a chunk_id surfaced via an All-Courses
    search. fix-all v3 #H4.
    """
    async def handler(args: dict):
        chunk_id = (args.get("chunk_id") or "").strip()
        if not chunk_id:
            return {"error": "chunk_id is required"}
        chunk = kb.find_chunk(chunk_id)
        if chunk is None:
            return {"error": "not_found", "chunk_id": chunk_id}
        if lock_course_id and chunk.course_id != lock_course_id:
            return {
                "error": "cross_course_denied",
                "chunk_id": chunk_id,
                "active_course": lock_course_id,
                "actual_course": chunk.course_id,
            }
        text = chunk.text
        truncated = False
        if len(text) > MAX_CHUNK_TEXT_BYTES:
            text = text[:MAX_CHUNK_TEXT_BYTES] + f"\n\n(truncated, {len(chunk.text) - MAX_CHUNK_TEXT_BYTES} more chars)"
            truncated = True
        return {
            "chunk_id": chunk.chunk_id,
            "course_id": chunk.course_id,
            "source_file": chunk.source_file,
            "location": chunk.location,
            "text": text,
            "truncated": truncated,
        }

    return Tool(
        name="read_chunk",
        description=DESCRIPTION,
        parameters=PARAMETERS,
        handler=handler,
        is_read_only=True,
        concurrency_safe=True,
    )
