"""Agent tool registry and read-only batch executor.

Reference (design only — no code copied):
- previous-agent/src/Tool.ts:362 → tool surface five-tuple (name, schema,
  is_read_only, concurrency_safe, destructive).
- previous-agent/src/services/tools/toolOrchestration.ts:91 partitionToolCalls
  → consecutive read-only tools run in one parallel batch; mutating tools
  serial.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


@dataclass
class Tool:
    """A single agent-callable tool.

    `description` is the *only* documentation the model sees — it must read
    like a usage guide (multi-line, prescriptive, with reasonable defaults
    and gotchas) rather than a one-line docstring.
    """

    name: str
    description: str
    parameters: dict
    handler: Callable[[dict], Awaitable[Any]]
    is_read_only: bool = True
    concurrency_safe: bool = True
    # Per-tool timeout (seconds). Slow tools (generate_note ≈ 5–15s) override
    # this; read-only tools should respect the default 30s ceiling.
    timeout_s: float = 30.0

    def to_openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolCall:
    call_id: str
    name: str
    arguments_raw: str

    @property
    def arguments(self) -> dict:
        if not self.arguments_raw:
            return {}
        try:
            parsed = json.loads(self.arguments_raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} already registered")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def openai_schemas(self) -> list[dict]:
        return [t.to_openai_schema() for t in self._tools.values()]


def validate_course_id(course_id, orchestrator) -> tuple[str | None, str | None]:
    """Whitelist a course_id against `orchestrator.list_courses()`.

    Returns ``(clean_id, error_msg)``; exactly one is ``None``. Used by every
    tool that takes a course_id so a prompt-injected LLM tool call cannot
    drive arbitrary path interpolation downstream (note_generator builds
    `ARTIFACTS_DIR / "courses" / course_id / ...` and writes files).
    """
    if course_id is None:
        return None, None
    cleaned = (course_id or "").strip()
    if not cleaned:
        return None, None
    # Path-traversal guard before hitting list_courses (a filesystem scan).
    if any(c in cleaned for c in ("/", "\\", "\x00")) or ".." in cleaned:
        return None, f"invalid course_id: {cleaned!r}"
    if cleaned not in orchestrator.list_courses():
        return None, f"unknown course_id: {cleaned!r}"
    return cleaned, None


async def run_tool_calls(
    calls: list[ToolCall],
    registry: ToolRegistry,
) -> list[tuple[ToolCall, str]]:
    """Execute tool calls. Consecutive read-only + concurrency-safe calls run
    in one `asyncio.gather` batch; anything mutating runs serially. Order of
    the returned list matches the input order regardless of batching.

    fix-all v3 #M6: results are accumulated as a list of (call, result)
    tuples — the previous dict-by-call_id approach silently overwrote
    rows when an LLM emitted duplicate call_ids, leaving the caller with
    two copies of the second result.
    """
    out: list[tuple[ToolCall, str]] = []
    i = 0
    while i < len(calls):
        call = calls[i]
        tool = registry.get(call.name)
        if tool is not None and tool.is_read_only and tool.concurrency_safe:
            j = i + 1
            batch = [call]
            while j < len(calls):
                nxt = registry.get(calls[j].name)
                if nxt is not None and nxt.is_read_only and nxt.concurrency_safe:
                    batch.append(calls[j])
                    j += 1
                else:
                    break
            results = await asyncio.gather(
                *(_run_one(c, registry) for c in batch),
                return_exceptions=True,
            )
            for c, r in zip(batch, results):
                out.append((c, _format_result(r)))
            i = j
        else:
            out.append((call, _format_result(await _run_one(call, registry))))
            i += 1
    return out


async def _run_one(call: ToolCall, registry: ToolRegistry) -> Any:
    tool = registry.get(call.name)
    if tool is None:
        return RuntimeError(f"unknown tool: {call.name}")
    args = call.arguments
    try:
        return await asyncio.wait_for(tool.handler(args), timeout=tool.timeout_s)
    except asyncio.TimeoutError:
        logger.warning("tool %s timed out after %.1fs", call.name, tool.timeout_s)
        return TimeoutError(f"tool {call.name} timed out after {tool.timeout_s}s")
    except Exception as exc:
        logger.warning("tool %s failed: %s", call.name, exc, exc_info=True)
        return exc


_LEAK_PATTERNS = [
    (re.compile(r"https?://\S+"), "[url]"),
    (re.compile(r"/[A-Za-z0-9_./-]{4,}"), "[path]"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "[apikey]"),
    (re.compile(r"\bBearer\s+\S+", re.IGNORECASE), "Bearer [redacted]"),
]


def _scrub(text: str) -> str:
    out = text
    for pat, repl in _LEAK_PATTERNS:
        out = pat.sub(repl, out)
    return out


def _format_result(value: Any) -> str:
    if isinstance(value, BaseException):
        # fix-all v3 #M5: keep the exception type so the LLM can react,
        # but scrub message body — provider error strings routinely
        # contain URLs / paths / sometimes API-key shaped tokens.
        msg = _scrub(str(value))[:120]
        return f"ERROR: {type(value).__name__}: {msg}"
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)
    return str(value)
