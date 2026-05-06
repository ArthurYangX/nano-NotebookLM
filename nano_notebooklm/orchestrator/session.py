"""Session state management."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Session:
    """Tracks state within a user session."""
    session_id: str = ""
    started_at: datetime = field(default_factory=datetime.now)
    active_course: str | None = None
    chat_history: list[dict] = field(default_factory=list)

    def add_message(self, role: str, content: str):
        self.chat_history.append({
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
        })

    def get_recent_context(self, n: int = 5) -> str:
        """Get last n messages as context string."""
        recent = self.chat_history[-n:]
        return "\n".join(f"{m['role']}: {m['content']}" for m in recent)
