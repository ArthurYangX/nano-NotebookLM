"""generate_note — invoke the note_generator skill (writes a file)."""

from __future__ import annotations

from nano_notebooklm.orchestrator.agent_tools import Tool, validate_course_id

DESCRIPTION = """Generate a structured study note for a course/topic and save it to disk.

Usage:
- Call this only when the user explicitly asks for a "note", "study note", "summary doc", "笔记". For short answers, just answer in your text response after `search_kb` — do NOT call this speculatively.
- `course_id` is required.
- `topic` narrows the note to a single concept. Omit to generate a full-course overview note (slow; only when explicitly requested).
- Output is always LaTeX (.tex file under artifacts/courses/<course>/notes/). Returns {output_path, format, topic, sources_used}.
- Generation takes 5–15 s. After it returns, summarize for the user what was generated and where it was saved — don't paste the entire note body.
"""

# review-swarm fix-all v1 #6: dropped `format` parameter. The Note pipeline
# is LaTeX-only since the R4-6 refactor; advertising a `markdown` option
# made the agent loop request markdown but receive a .tex file labelled
# `format: "latex"` — opaque contract drift in tool result vs tool schema.
PARAMETERS = {
    "type": "object",
    "properties": {
        "course_id": {
            "type": "string",
            "description": "Course id (e.g. 'CS231N').",
        },
        "topic": {
            "type": "string",
            "description": "Optional focused topic. Omit for a full-course note.",
        },
    },
    "required": ["course_id"],
}


def build_generate_note(orchestrator, lock_course_id: str | None = None) -> Tool:
    """When ``lock_course_id`` is set, refuse generation for any other
    course — otherwise an agent locked to course A could write a note
    file into course B's folder (file-write side effect + content leak).
    fix-all v4 #A4.
    """
    async def handler(args: dict):
        clean_course, err = validate_course_id(args.get("course_id"), orchestrator)
        if err is not None:
            return {"error": err}
        if not clean_course:
            return {"error": "course_id is required"}
        if lock_course_id and clean_course != lock_course_id:
            return {
                "error": "cross_course_denied",
                "active_course": lock_course_id,
                "requested_course": clean_course,
            }
        params = {
            "course_id": clean_course,
            "topic": args.get("topic"),
            # LaTeX-only — `format` is no longer in the tool schema.
            "format": "latex",
        }
        result = await orchestrator.run_skill("note_generator", params)
        if not result.success:
            return {"error": result.error or "note generation failed"}
        return {
            "output_path": result.output_path,
            "format": result.data.get("format"),
            "topic": result.data.get("topic"),
            "sources_used": result.data.get("sources_used"),
        }

    return Tool(
        name="generate_note",
        description=DESCRIPTION,
        parameters=PARAMETERS,
        handler=handler,
        is_read_only=False,
        concurrency_safe=False,
        # Note generation does retrieval + LLM call + file write — the upper
        # bound is dominated by the LLM call (5–15s typical, occasionally
        # longer on first-token latency). 60s leaves headroom without
        # letting a stuck call wedge an entire agent turn.
        timeout_s=60.0,
    )
