"""Built-in agent tools.

`build_default_registry` wires up the four MVP tools:
  search_kb     — hybrid retrieval (read-only, parallel-safe)
  read_chunk    — fetch one chunk's full text (read-only, parallel-safe)
  list_courses  — enumerate courses (read-only, parallel-safe)
  generate_note — write a structured note to disk (mutating, serial)
"""

from __future__ import annotations

from nano_notebooklm.orchestrator.agent_tools import ToolRegistry
from nano_notebooklm.orchestrator.tools.generate_note import build_generate_note
from nano_notebooklm.orchestrator.tools.list_courses import build_list_courses
from nano_notebooklm.orchestrator.tools.read_chunk import build_read_chunk
from nano_notebooklm.orchestrator.tools.search_kb import build_search_kb


def build_default_registry(kb, orchestrator, lock_course_id: str | None = None) -> ToolRegistry:
    """Build the four-tool MVP registry. Pass ``lock_course_id`` so that
    ``read_chunk`` refuses to leak chunks from sibling courses when a
    request is scoped to a specific course (fix-all v3 #H4).
    """
    reg = ToolRegistry()
    reg.register(build_search_kb(kb, orchestrator))
    reg.register(build_read_chunk(kb, lock_course_id=lock_course_id))
    reg.register(build_list_courses(orchestrator, kb))
    reg.register(build_generate_note(orchestrator))
    return reg


__all__ = ["build_default_registry"]
