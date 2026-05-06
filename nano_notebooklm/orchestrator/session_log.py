"""Daily session log with size-based rotation."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Callable, Any


class SessionLog:
    """Append-only JSONL session log grouped by local day."""

    def __init__(
        self,
        artifacts_dir: Path,
        max_bytes: int = 1_000_000,
        now_fn: Callable[[], datetime] | None = None,
    ):
        self.artifacts_dir = Path(artifacts_dir)
        self.max_bytes = max_bytes
        self.now_fn = now_fn or datetime.now
        self.log_dir = self.artifacts_dir / "sessions"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._seq = 0

    def append(self, course_id: str | None, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        now = self.now_fn()
        self._seq += 1
        entry = {
            "id": f"{now.strftime('%Y%m%d%H%M%S%f')}-{self._seq:06d}",
            "date": now.strftime("%Y-%m-%d"),
            "timestamp": now.isoformat(timespec="seconds"),
            "course_id": course_id,
            "kind": kind,
            "payload": payload,
        }
        path = self._active_path(entry["date"])
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def list_grouped(self, limit: int = 500) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        entries: list[dict[str, Any]] = []
        for path in sorted(self.log_dir.glob("session-*.jsonl")):
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        entries = sorted(entries, key=lambda e: e.get("id", e.get("timestamp", "")))[-limit:]
        for entry in entries:
            grouped.setdefault(entry.get("date", "unknown"), []).append(entry)
        return grouped

    def _active_path(self, date: str) -> Path:
        base = self.log_dir / f"session-{date}.jsonl"
        if not base.exists() or base.stat().st_size < self.max_bytes:
            return base
        idx = 1
        while True:
            candidate = self.log_dir / f"session-{date}-{idx}.jsonl"
            if not candidate.exists() or candidate.stat().st_size < self.max_bytes:
                return candidate
            idx += 1
