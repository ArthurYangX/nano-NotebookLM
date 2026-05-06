"""Offline tests for daily session logs."""

from __future__ import annotations

from datetime import datetime


def test_session_log_happy(tmp_path):
    from nano_notebooklm.orchestrator.session_log import SessionLog

    log = SessionLog(tmp_path, now_fn=lambda: datetime(2026, 5, 6, 9, 30))
    entry = log.append(
        course_id="CS182",
        kind="question",
        payload={"text": "What is backprop?"},
    )
    grouped = log.list_grouped()

    assert entry["date"] == "2026-05-06"
    assert grouped["2026-05-06"][0]["course_id"] == "CS182"
    assert grouped["2026-05-06"][0]["payload"]["text"] == "What is backprop?"


def test_session_log_large_rotate(tmp_path):
    from nano_notebooklm.orchestrator.session_log import SessionLog

    log = SessionLog(
        tmp_path,
        max_bytes=80,
        now_fn=lambda: datetime(2026, 5, 6, 9, 30),
    )
    for i in range(8):
        log.append("CS182", "generation", {"text": "x" * 40, "i": i})

    log_files = sorted((tmp_path / "sessions").glob("session-2026-05-06*.jsonl"))
    assert len(log_files) >= 2
    assert all(path.stat().st_size <= 800 for path in log_files)
    assert log.list_grouped()["2026-05-06"][-1]["payload"]["i"] == 7
