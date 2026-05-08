"""Persistent user memory — stores preferences, learning context, and session history.

Enables the AI to remember the user across sessions:
- What courses they're studying
- Their learning goals and weak areas
- Preferred explanation style
- Past interactions summary
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path

from nano_notebooklm import config

logger = logging.getLogger(__name__)

MEMORY_PATH = config.ARTIFACTS_DIR / "user_memory.json"

# review-swarm fix-all v3 #M4: serialise the read-modify-write sequence so
# concurrent /api/memory POST requests can't last-write-wins each other,
# and write atomically with fsync so a crash mid-write can't truncate the
# memory file.
_MEMORY_LOCK = threading.RLock()


def load_memory() -> dict:
    """Load user memory from disk."""
    with _MEMORY_LOCK:
        if MEMORY_PATH.exists():
            try:
                return json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                logger.warning("user_memory.json is corrupt; falling back to defaults")
                return _default_memory()
        return _default_memory()


def save_memory(memory: dict):
    """Atomic + durable write of user memory."""
    memory["last_updated"] = datetime.now().isoformat()
    MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(memory, ensure_ascii=False, indent=2).encode("utf-8")
    with _MEMORY_LOCK:
        tmp = MEMORY_PATH.with_suffix(".json.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, MEMORY_PATH)
        try:
            dir_fd = os.open(str(MEMORY_PATH.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass  # parent fsync unsupported on some FS


def update_memory(key: str, value):
    """Update a specific memory field — single critical section."""
    with _MEMORY_LOCK:
        mem = load_memory()
        mem[key] = value
        save_memory(mem)


def add_interaction(course_id: str, question: str, summary: str):
    """Record a user interaction for context continuity."""
    with _MEMORY_LOCK:
        mem = load_memory()
        interactions = mem.get("recent_interactions", [])
        interactions.append({
            "course": course_id,
            "question": question[:200],
            "summary": summary[:300],
            "timestamp": datetime.now().isoformat(),
        })
        mem["recent_interactions"] = interactions[-50:]
        active = mem.get("active_courses", [])
        if course_id and course_id not in active:
            active.append(course_id)
        mem["active_courses"] = active
        save_memory(mem)


def get_context_prompt(course_id: str | None = None) -> str:
    """Build a context string from memory to prepend to AI prompts."""
    mem = load_memory()
    parts = []

    # User profile
    name = mem.get("user_name", "")
    if name:
        parts.append(f"The student's name is {name}.")

    goals = mem.get("learning_goals", "")
    if goals:
        parts.append(f"Their learning goals: {goals}")

    style = mem.get("preferred_style", "")
    if style:
        parts.append(f"They prefer {style} explanations.")

    # Weak areas
    weak = mem.get("weak_areas", [])
    if weak:
        parts.append(f"Known weak areas: {', '.join(weak)}. Pay extra attention to these topics.")

    # Recent context
    interactions = mem.get("recent_interactions", [])
    if course_id:
        recent = [i for i in interactions if i.get("course") == course_id][-3:]
    else:
        recent = interactions[-3:]

    if recent:
        parts.append("Recent study context:")
        for i in recent:
            parts.append(f"  - Asked about: {i['question']}")

    return "\n".join(parts) if parts else ""


def _default_memory() -> dict:
    return {
        "user_name": "",
        "learning_goals": "",
        "preferred_style": "clear and concise with examples",
        "active_courses": [],
        "weak_areas": [],
        "recent_interactions": [],
        "preferences": {
            "language": "auto",  # auto-detect from source material
            "detail_level": "medium",
            "include_examples": True,
        },
        "created_at": datetime.now().isoformat(),
        "last_updated": datetime.now().isoformat(),
    }
