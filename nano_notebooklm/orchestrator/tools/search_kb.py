"""search_kb — hybrid (FAISS + BM25 + RRF) retrieval over indexed chunks."""

from __future__ import annotations

from nano_notebooklm.orchestrator.agent_tools import Tool, validate_course_id

DESCRIPTION = """Search the indexed course knowledge base using hybrid (FAISS vector + BM25 + RRF) retrieval.

Usage:
- Always call this for any factual question that depends on the user's course materials. Do NOT answer from prior knowledge when course context is needed — search first.
- `query` should be a short, content-bearing phrase (concept names, equations, terms). Long natural-language sentences work but waste tokens.
- `course_id` filters to a single course; omit it to search across all courses. Pass the user's active course unless they explicitly asked about another one.
- `top_k` defaults to 5; raise to 10 for breadth, drop to 3 for only the strongest hits. Hard cap is 20.
- Returns a JSON array of {chunk_id, source_file, location, score, course_id, text}. Text is truncated to 1200 chars — pass the chunk_id to `read_chunk` if you need the full body.
- A score below ~0.02 is usually noise. If every score is low, try a different query phrasing before answering — don't guess.
"""

PARAMETERS = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "Content-bearing search query (concept, term, equation name).",
        },
        "course_id": {
            "type": "string",
            "description": "Optional course id to restrict the search to. Omit to search all courses.",
        },
        "top_k": {
            "type": "integer",
            "description": "Number of results to return.",
            "default": 5,
            "minimum": 1,
            "maximum": 10,
        },
    },
    "required": ["query"],
}


def build_search_kb(kb, orchestrator, lock_course_id: str | None = None) -> Tool:
    """When ``lock_course_id`` is set, this tool refuses queries that try to
    target a different course. Without this guard a prompt-injected agent
    locked to course A could call ``search_kb(course_id="B")`` and harvest
    1200-char snippets from course B (read_chunk is locked but the search
    summary itself is enough exfil). fix-all v4 #A4.
    """
    async def handler(args: dict):
        query = (args.get("query") or "").strip()
        if not query:
            return {"error": "query is required"}
        clean_course, err = validate_course_id(args.get("course_id"), orchestrator)
        if err is not None:
            return {"error": err}
        if lock_course_id:
            if clean_course is None:
                # Force the active course rather than letting the agent
                # silently search across all courses.
                clean_course = lock_course_id
            elif clean_course != lock_course_id:
                return {
                    "error": "cross_course_denied",
                    "active_course": lock_course_id,
                    "requested_course": clean_course,
                }
        try:
            top_k = int(args.get("top_k", 5))
        except (TypeError, ValueError):
            top_k = 5
        top_k = max(1, min(top_k, 10))

        results = kb.search(query=query, top_k=top_k, course_id=clean_course)
        return [
            {
                "chunk_id": r.chunk_id,
                "source_file": r.source_file,
                "location": r.location,
                "score": round(float(r.score), 4),
                "course_id": r.course_id,
                "text": r.text[:1200],
            }
            for r in results
        ]

    return Tool(
        name="search_kb",
        description=DESCRIPTION,
        parameters=PARAMETERS,
        handler=handler,
        is_read_only=True,
        concurrency_safe=True,
    )
