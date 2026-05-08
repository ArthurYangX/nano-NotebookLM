"""Tests for the multi-turn agent loop.

Two layers covered:
1. Loop logic: mocks the LLM at the `llm_stream` boundary so we can script
   deterministic turn sequences without an API key.
2. chat.completions streaming bridge: mocks the OpenAI client itself so we
   exercise the delta-accumulation logic that converts SDK events into our
   `text_delta` / `assistant_message` / `error` shape.

System-prompt assembly is also exercised here.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import AsyncIterator

import pytest

from nano_notebooklm.orchestrator import agent_loop
from nano_notebooklm.orchestrator.agent_loop import compose_system_prompt, run_agent
from nano_notebooklm.orchestrator.agent_tools import Tool, ToolRegistry


def _mk_registry(*tools: Tool) -> ToolRegistry:
    reg = ToolRegistry()
    for t in tools:
        reg.register(t)
    return reg


def _scripted_stream(turns: list[list[dict]]):
    """Returns an llm_stream callable that yields one turn from `turns` per call."""
    counter = {"i": 0}

    async def _stream(*, system, messages, tools, temperature, max_tokens) -> AsyncIterator[dict]:
        i = counter["i"]
        counter["i"] += 1
        if i >= len(turns):
            raise AssertionError(f"agent loop asked for turn {i}, only {len(turns)} scripted")
        for evt in turns[i]:
            yield evt

    return _stream


def _ok_tool(name: str, payload):
    async def handler(args):
        return payload
    return Tool(
        name=name,
        description=f"{name}",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        is_read_only=True,
        concurrency_safe=True,
    )


@pytest.mark.asyncio
async def test_no_tool_call_emits_done():
    """Model answers in one shot → text deltas + done event, no tool calls."""
    stream = _scripted_stream([
        [
            {"type": "text_delta", "delta": "Backprop "},
            {"type": "text_delta", "delta": "is the chain rule."},
            {"type": "assistant_message",
             "message": {"role": "assistant", "content": "Backprop is the chain rule."}},
        ],
    ])
    reg = _mk_registry(_ok_tool("search_kb", []))

    events = []
    async for evt in run_agent("what is backprop?", registry=reg,
                               course_id=None, llm_stream=stream):
        events.append(evt)

    text_events = [e for e in events if e["type"] == "text"]
    assert "".join(e["delta"] for e in text_events) == "Backprop is the chain rule."
    assert events[-1] == {
        "type": "done",
        "answer": "Backprop is the chain rule.",
        "turns": 1,
        "max_turns_hit": False,
        "budget_hit": False,
    }
    assert not any(e["type"] == "tool_call" for e in events)


@pytest.mark.asyncio
async def test_single_tool_call_then_answer():
    """Turn 1: tool call. Turn 2: final answer using the tool result."""
    stream = _scripted_stream([
        [
            {"type": "assistant_message", "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "search_kb", "arguments": '{"query": "rrf"}'},
                }],
            }},
        ],
        [
            {"type": "text_delta", "delta": "RRF combines rankings."},
            {"type": "assistant_message",
             "message": {"role": "assistant", "content": "RRF combines rankings."}},
        ],
    ])
    reg = _mk_registry(_ok_tool("search_kb", [{"chunk_id": "c1", "text": "rrf body"}]))

    events = []
    async for evt in run_agent("explain rrf", registry=reg, llm_stream=stream):
        events.append(evt)

    types = [e["type"] for e in events]
    assert types == ["tool_call", "tool_result", "text", "done"]
    assert events[0]["name"] == "search_kb"
    assert events[0]["arguments"] == {"query": "rrf"}
    # tool_result carries the JSON-serialized handler return
    assert "rrf body" in events[1]["result"]
    assert events[-1]["answer"] == "RRF combines rankings."
    assert events[-1]["turns"] == 2


@pytest.mark.asyncio
async def test_parallel_tool_calls_one_turn():
    """Model emits two read-only calls in a single turn → both executed,
    both tool_result events surface in order."""
    stream = _scripted_stream([
        [
            {"type": "assistant_message", "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "c_a", "type": "function",
                     "function": {"name": "search_kb", "arguments": '{"query": "a"}'}},
                    {"id": "c_b", "type": "function",
                     "function": {"name": "search_kb", "arguments": '{"query": "b"}'}},
                ],
            }},
        ],
        [
            {"type": "text_delta", "delta": "ok"},
            {"type": "assistant_message",
             "message": {"role": "assistant", "content": "ok"}},
        ],
    ])
    reg = _mk_registry(_ok_tool("search_kb", [{"hit": True}]))

    events = []
    async for evt in run_agent("dual", registry=reg, llm_stream=stream):
        events.append(evt)

    tool_calls = [e for e in events if e["type"] == "tool_call"]
    tool_results = [e for e in events if e["type"] == "tool_result"]
    assert [c["call_id"] for c in tool_calls] == ["c_a", "c_b"]
    assert [r["call_id"] for r in tool_results] == ["c_a", "c_b"]
    assert events[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_stream_error_yields_error_event():
    stream = _scripted_stream([
        [
            {"type": "text_delta", "delta": "partial..."},
            {"type": "error", "error": "upstream 502"},
        ],
    ])
    reg = _mk_registry(_ok_tool("search_kb", []))

    events = []
    async for evt in run_agent("hi", registry=reg, llm_stream=stream):
        events.append(evt)

    assert events[-1]["type"] == "error"
    assert events[-1]["error"] == "upstream 502"
    assert events[-1]["partial"] == "partial..."


@pytest.mark.asyncio
async def test_max_turns_guard():
    """If the model keeps calling tools forever, we stop at max_turns."""
    def make_loop_turn(call_id: str):
        return [{
            "type": "assistant_message", "message": {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": call_id, "type": "function",
                    "function": {"name": "search_kb", "arguments": "{}"},
                }],
            },
        }]

    stream = _scripted_stream([make_loop_turn(f"c{i}") for i in range(10)])
    reg = _mk_registry(_ok_tool("search_kb", []))

    events = []
    async for evt in run_agent("loop", registry=reg, llm_stream=stream,
                               max_turns=3):
        events.append(evt)

    done = events[-1]
    assert done["type"] == "done"
    assert done["max_turns_hit"] is True
    assert done["turns"] == 3
    # 3 turns × 1 call each
    assert sum(1 for e in events if e["type"] == "tool_call") == 3


# ── compose_system_prompt ──────────────────────────────────────────────


def test_system_prompt_baseline_has_tools_section():
    p = compose_system_prompt(course_id=None)
    assert "search_kb" in p and "read_chunk" in p
    assert "Active course" not in p
    assert "Available courses" not in p


def test_system_prompt_with_active_course():
    p = compose_system_prompt(course_id="CS231N")
    assert "Active course" in p
    assert "`CS231N`" in p


def test_system_prompt_with_course_list():
    p = compose_system_prompt(course_id="CS231N", course_names=["CS231N", "CSE 234"])
    assert "Available courses" in p
    assert "`CS231N`" in p and "`CSE 234`" in p


# ── chat.completions streaming bridge ──────────────────────────────────


def _evt(content=None, tool_calls=None):
    """Build a fake `chat.completions.create` stream chunk."""
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


def _tc(index, *, id=None, name=None, args=None):
    """Build a fake tool_call delta."""
    if name is not None or args is not None:
        fn = SimpleNamespace(name=name, arguments=args)
    else:
        fn = None
    return SimpleNamespace(index=index, id=id, function=fn)


class _FakeStream:
    """Fake OpenAI streaming iterator with a recorded close()."""

    def __init__(self, events):
        self._events = iter(events)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._events)

    def close(self):
        self.closed = True


def _fake_backend(events_or_factory):
    if callable(events_or_factory):
        stream = _FakeStream(events_or_factory())
    else:
        stream = _FakeStream(events_or_factory)

    def create(**kw):
        return stream

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    backend = SimpleNamespace(client=client, model="test-model")
    backend._stream = stream  # stash for assertions
    return backend


@pytest.mark.asyncio
async def test_bridge_text_only_assembles_full_message():
    backend = _fake_backend([
        _evt(content="Hello "),
        _evt(content="world"),
        _evt(content="."),
    ])
    stream = agent_loop.make_chat_completions_stream(backend)

    events = []
    async for evt in stream(system="sys", messages=[{"role": "user", "content": "hi"}],
                            tools=[], temperature=0.0, max_tokens=64):
        events.append(evt)

    text_deltas = [e for e in events if e["type"] == "text_delta"]
    assert "".join(e["delta"] for e in text_deltas) == "Hello world."
    assert events[-1]["type"] == "assistant_message"
    assert events[-1]["message"]["content"] == "Hello world."
    assert "tool_calls" not in events[-1]["message"]
    assert backend._stream.closed is True


@pytest.mark.asyncio
async def test_bridge_single_tool_call_across_deltas():
    """SDK splits tool_call across N deltas; the bridge concatenates by index."""
    backend = _fake_backend([
        _evt(tool_calls=[_tc(0, id="call_1", name="search_kb", args="")]),
        _evt(tool_calls=[_tc(0, args='{"q":')]),
        _evt(tool_calls=[_tc(0, args='"rrf"}')]),
    ])
    stream = agent_loop.make_chat_completions_stream(backend)
    events = [e async for e in stream(system="", messages=[], tools=[],
                                       temperature=0.0, max_tokens=64)]

    msg = events[-1]["message"]
    assert msg["content"] is None
    assert len(msg["tool_calls"]) == 1
    assert msg["tool_calls"][0]["id"] == "call_1"
    assert msg["tool_calls"][0]["function"]["name"] == "search_kb"
    assert msg["tool_calls"][0]["function"]["arguments"] == '{"q":"rrf"}'


@pytest.mark.asyncio
async def test_bridge_parallel_tool_calls_distinct_indexes():
    backend = _fake_backend([
        _evt(tool_calls=[_tc(0, id="c1", name="a", args='{}')]),
        _evt(tool_calls=[_tc(1, id="c2", name="b", args='{}')]),
    ])
    stream = agent_loop.make_chat_completions_stream(backend)
    events = [e async for e in stream(system="", messages=[], tools=[],
                                       temperature=0.0, max_tokens=64)]

    tc_list = events[-1]["message"]["tool_calls"]
    assert [t["id"] for t in tc_list] == ["c1", "c2"]
    assert [t["function"]["name"] for t in tc_list] == ["a", "b"]


@pytest.mark.asyncio
async def test_bridge_exception_yields_stable_error_code():
    """Mid-stream exception → `error` event with stable code, not raw str(exc)."""
    def events_gen():
        yield _evt(content="partial ")
        raise RuntimeError("vendor secret leak: api-key-shape sk-...")

    backend = _fake_backend(events_gen)
    stream = agent_loop.make_chat_completions_stream(backend)
    events = [e async for e in stream(system="", messages=[], tools=[],
                                       temperature=0.0, max_tokens=64)]

    err = next(e for e in events if e["type"] == "error")
    assert err["error"] == "upstream_error"
    # The raw exception message must NOT be in the event payload.
    assert "vendor secret leak" not in str(err)
    assert "sk-" not in str(err)


@pytest.mark.asyncio
async def test_unknown_tool_call_surfaces_error_in_result():
    """Model hallucinates a tool name → tool_result carries an ERROR string,
    loop keeps going (the model can recover on next turn)."""
    stream = _scripted_stream([
        [
            {"type": "assistant_message", "message": {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "c1", "type": "function",
                    "function": {"name": "fictitious", "arguments": "{}"},
                }],
            }},
        ],
        [
            {"type": "text_delta", "delta": "I cannot use that tool."},
            {"type": "assistant_message",
             "message": {"role": "assistant", "content": "I cannot use that tool."}},
        ],
    ])
    reg = _mk_registry(_ok_tool("search_kb", []))

    events = []
    async for evt in run_agent("trick", registry=reg, llm_stream=stream):
        events.append(evt)

    tr = next(e for e in events if e["type"] == "tool_result")
    assert "ERROR" in tr["result"] and "fictitious" in tr["result"]
    assert events[-1]["type"] == "done"
