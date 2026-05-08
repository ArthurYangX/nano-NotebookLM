"""更严格的 agent_loop 测试 — 关注 stream 异常路径与 cancel 安全。

覆盖现有 `test_agent_loop.py` 没盯的边界：
- assistant_message 中 ``content == None`` 的 done 输出
- assistant_message 缺失（stream 直接结束）
- llm_stream 自身抛 Exception → error 事件，不传播
- ``tool_call.arguments`` 解析失败时仍然送出 tool_result（call.arguments → {}）
- ``compose_system_prompt`` 在不同入参组合下的拼接顺序与 idempotency
- max_turns 边界（=1）
- 大量 text_delta 累积到 partial 字段
- 多 turn 中 messages list 增长且不重复加 system message
- run_agent 在没传 backend 也没传 llm_stream 时抛 ValueError
- _read_int_env 在非法值下回落（间接覆盖：cap 32）
"""

from __future__ import annotations

import os
from typing import AsyncIterator

import pytest

from nano_notebooklm.orchestrator import agent_loop
from nano_notebooklm.orchestrator.agent_loop import (
    DEFAULT_MAX_TURNS,
    compose_system_prompt,
    run_agent,
)
from nano_notebooklm.orchestrator.agent_tools import Tool, ToolRegistry


# ── helpers ───────────────────────────────────────────────────────────


def _mk_registry(*tools: Tool) -> ToolRegistry:
    reg = ToolRegistry()
    for t in tools:
        reg.register(t)
    return reg


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


def _scripted_stream(turns: list[list[dict]], record_calls: list | None = None):
    counter = {"i": 0}

    async def _stream(*, system, messages, tools, temperature, max_tokens) -> AsyncIterator[dict]:
        i = counter["i"]
        counter["i"] += 1
        if record_calls is not None:
            record_calls.append({
                "system": system,
                "messages": [m.copy() for m in messages],
                "n_tools": len(tools),
            })
        if i >= len(turns):
            raise AssertionError(f"agent loop asked for turn {i}, only {len(turns)} scripted")
        for evt in turns[i]:
            yield evt

    return _stream


# ── compose_system_prompt ────────────────────────────────────────────


def test_compose_system_prompt_no_course_only_base():
    p = compose_system_prompt(None, None)
    assert "study assistant" in p.lower()
    assert "Active course" not in p
    assert "Available courses" not in p


def test_compose_system_prompt_with_course_only():
    p = compose_system_prompt("CS231N", None)
    assert "Active course" in p
    assert "CS231N" in p
    assert "Available courses" not in p


def test_compose_system_prompt_with_course_and_names_orders_consistently():
    """The compose helper must produce a deterministic ordering: BASE → active
    → available. Tests pin the order so a future refactor that re-orders the
    sections can't silently break a system prompt that depends on it."""
    p = compose_system_prompt("CS231N", ["CS231N", "CS285", "机器人导论"])
    assert p.find("Active course") < p.find("Available courses")
    for c in ("CS231N", "CS285", "机器人导论"):
        assert f"`{c}`" in p


def test_compose_system_prompt_idempotent_for_same_inputs():
    a = compose_system_prompt("X", ["X", "Y"])
    b = compose_system_prompt("X", ["X", "Y"])
    assert a == b


def test_compose_system_prompt_no_course_but_with_names():
    """User hasn't picked a course but we still want the available list so the
    model can ground itself with `list_courses`-equivalent context."""
    p = compose_system_prompt(None, ["A", "B"])
    assert "Available courses" in p
    assert "Active course" not in p


# ── run_agent input validation ───────────────────────────────────────


@pytest.mark.asyncio
async def test_run_agent_requires_backend_or_stream():
    """No backend AND no llm_stream → must raise ValueError immediately, not
    silently return."""
    reg = _mk_registry(_ok_tool("search_kb", []))
    with pytest.raises(ValueError):
        async for _ in run_agent("hi", registry=reg):
            pass


# ── max_turns boundaries ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_max_turns_one_with_immediate_answer_succeeds():
    """max_turns=1 + an immediate text answer should succeed (turns==1, not
    max_turns_hit, and budget_hit reported as False)."""
    stream = _scripted_stream([
        [
            {"type": "text_delta", "delta": "ok"},
            {"type": "assistant_message",
             "message": {"role": "assistant", "content": "ok"}},
        ],
    ])
    reg = _mk_registry(_ok_tool("search_kb", []))
    events = [e async for e in run_agent("q", registry=reg, llm_stream=stream, max_turns=1)]
    assert events[-1]["type"] == "done"
    assert events[-1]["max_turns_hit"] is False
    assert events[-1]["budget_hit"] is False
    assert events[-1]["turns"] == 1


@pytest.mark.asyncio
async def test_max_turns_one_with_tool_call_hits_cap():
    """max_turns=1 + the model emits a tool call → loop hits the cap because
    the second turn (post-tool synthesis) never gets to run."""
    stream = _scripted_stream([
        [
            {"type": "assistant_message", "message": {
                "role": "assistant", "content": None,
                "tool_calls": [{"id": "c1", "type": "function",
                                "function": {"name": "search_kb", "arguments": "{}"}}],
            }},
        ],
    ])
    reg = _mk_registry(_ok_tool("search_kb", []))
    events = [e async for e in run_agent("q", registry=reg, llm_stream=stream, max_turns=1)]
    done = events[-1]
    assert done["type"] == "done"
    assert done["max_turns_hit"] is True
    assert done["budget_hit"] is False
    assert done["turns"] == 1


@pytest.mark.asyncio
async def test_tool_result_budget_terminates_run(monkeypatch):
    """A misbehaving model could ask for many huge tool_results. The loop
    must clamp via TOOL_RESULT_BUDGET_BYTES and emit done.budget_hit=True so
    the prompt size doesn't run away."""
    from nano_notebooklm.orchestrator import agent_loop as al

    # Force a tiny budget, then return a payload that overflows it on first
    # call. The done event should report budget_hit=True.
    monkeypatch.setattr(al, "TOOL_RESULT_BUDGET_BYTES", 64)
    big = "x" * 4096

    async def big_handler(args):
        return {"body": big}

    tool = Tool(
        name="search_kb", description="d",
        parameters={"type": "object"}, handler=big_handler,
        is_read_only=True, concurrency_safe=True,
    )
    reg = _mk_registry(tool)
    stream = _scripted_stream([
        [
            {"type": "assistant_message", "message": {
                "role": "assistant", "content": None,
                "tool_calls": [{"id": "c1", "type": "function",
                                "function": {"name": "search_kb", "arguments": "{}"}}],
            }},
        ],
        [
            # If the budget did NOT fire we'd reach here — make this turn assert.
            {"type": "text_delta", "delta": "should not reach"},
            {"type": "assistant_message",
             "message": {"role": "assistant", "content": "should not reach"}},
        ],
    ])
    events = [e async for e in run_agent("q", registry=reg, llm_stream=stream, max_turns=4)]
    done = events[-1]
    assert done["type"] == "done"
    assert done["budget_hit"] is True
    # Stops at turn 1, never asks for turn 2
    assert done["turns"] == 1


# ── stream malformations ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stream_ends_without_assistant_message_yields_error():
    """Provider hangs up mid-stream (no assistant_message frame) → error
    event with the partial text accumulated so far. Pin the stable `error`
    code (`no_assistant_message`) — the frontend uses it for retry classification."""
    stream = _scripted_stream([
        [
            {"type": "text_delta", "delta": "half-finished "},
            # no assistant_message ever
        ],
    ])
    reg = _mk_registry(_ok_tool("search_kb", []))
    events = [e async for e in run_agent("q", registry=reg, llm_stream=stream)]
    assert events[-1]["type"] == "error"
    assert events[-1]["error"] == "no_assistant_message"
    assert events[-1]["partial"] == "half-finished "


@pytest.mark.asyncio
async def test_stream_factory_raises_yields_error_event_not_propagation():
    """If `llm_stream` itself raises before producing any frames, the loop
    must convert it to a stable-code error event (``agent_error``) — never
    propagate the raw exception to the FastAPI StreamingResponse layer
    (which would 500 mid-stream and leak provider details)."""

    async def boom_stream(*, system, messages, tools, temperature, max_tokens):
        raise RuntimeError("openai 502")
        yield  # pragma: no cover (make it an async generator)

    reg = _mk_registry(_ok_tool("search_kb", []))
    events = [e async for e in run_agent("q", registry=reg, llm_stream=boom_stream)]
    assert events[-1]["type"] == "error"
    assert events[-1]["error"] == "agent_error"
    # Critically, the raw upstream message must NOT leak through to the user.
    assert "openai 502" not in events[-1].get("error", "")
    assert "openai 502" not in events[-1].get("partial", "")


@pytest.mark.asyncio
async def test_tool_execution_error_yields_stable_code():
    """If `run_tool_calls` itself raises (registry corruption, etc.), the
    loop must surface ``tool_execution_failed`` rather than crashing the
    StreamingResponse."""
    from nano_notebooklm.orchestrator import agent_loop as al

    stream = _scripted_stream([
        [
            {"type": "assistant_message", "message": {
                "role": "assistant", "content": None,
                "tool_calls": [{"id": "c1", "type": "function",
                                "function": {"name": "search_kb", "arguments": "{}"}}],
            }},
        ],
    ])
    reg = _mk_registry(_ok_tool("search_kb", []))

    async def broken_run_tool_calls(calls, registry):
        raise RuntimeError("registry exploded")

    import contextlib
    with contextlib.ExitStack() as stack:
        # patch the symbol the agent_loop module actually references
        original = al.run_tool_calls
        al.run_tool_calls = broken_run_tool_calls
        stack.callback(lambda: setattr(al, "run_tool_calls", original))
        events = [e async for e in run_agent("q", registry=reg, llm_stream=stream)]

    assert events[-1]["type"] == "error"
    assert events[-1]["error"] == "tool_execution_failed"


@pytest.mark.asyncio
async def test_text_deltas_with_empty_string_are_no_ops():
    """Some providers emit empty `delta` frames as keepalives. Those must NOT
    surface as separate user-facing text events (would chop the UI render)."""
    stream = _scripted_stream([
        [
            {"type": "text_delta", "delta": ""},
            {"type": "text_delta", "delta": "real"},
            {"type": "text_delta", "delta": ""},
            {"type": "assistant_message",
             "message": {"role": "assistant", "content": "real"}},
        ],
    ])
    reg = _mk_registry(_ok_tool("search_kb", []))
    events = [e async for e in run_agent("q", registry=reg, llm_stream=stream)]
    text_events = [e for e in events if e["type"] == "text"]
    assert [e["delta"] for e in text_events] == ["real"]


@pytest.mark.asyncio
async def test_unknown_event_types_ignored_gracefully():
    """Provider extends the protocol with a new event type → we MUST silently
    ignore it (forward-compat). A future provider could add ``audio_delta``
    etc. and we don't want to crash the loop on a type check."""
    stream = _scripted_stream([
        [
            {"type": "vendor_specific_x", "x": 1},
            {"type": "text_delta", "delta": "hi"},
            {"type": "assistant_message",
             "message": {"role": "assistant", "content": "hi"}},
        ],
    ])
    reg = _mk_registry(_ok_tool("search_kb", []))
    events = [e async for e in run_agent("q", registry=reg, llm_stream=stream)]
    assert events[-1]["type"] == "done"
    assert events[-1]["answer"] == "hi"


@pytest.mark.asyncio
async def test_tool_call_with_invalid_json_arguments_still_executes_handler():
    """Model emits malformed JSON for tool arguments → ToolCall.arguments
    returns {}, handler still runs with {}, the loop doesn't break.

    Without this safety the agent would deadlock the moment GPT-5.4 fumbled
    a comma in its tool argument JSON. The handler sees {}, can refuse or
    return an empty result, model recovers next turn."""
    seen_args = []

    async def capture(args):
        seen_args.append(args)
        return {"ok": True}

    tool = Tool(name="search_kb", description="d",
                parameters={"type": "object"}, handler=capture,
                is_read_only=True, concurrency_safe=True)
    reg = _mk_registry(tool)
    stream = _scripted_stream([
        [
            {"type": "assistant_message", "message": {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "c1", "type": "function",
                    "function": {"name": "search_kb",
                                 "arguments": '{"query":'}  # truncated JSON
                }],
            }},
        ],
        [
            {"type": "text_delta", "delta": "recovered"},
            {"type": "assistant_message",
             "message": {"role": "assistant", "content": "recovered"}},
        ],
    ])
    events = [e async for e in run_agent("q", registry=reg, llm_stream=stream)]

    # handler was called with {} (defensively-empty args)
    assert seen_args == [{}]
    # tool_call event surfaces the parsed-empty arguments to the user too
    tc = next(e for e in events if e["type"] == "tool_call")
    assert tc["arguments"] == {}
    assert events[-1]["type"] == "done"


# ── multi-turn message accumulation ──────────────────────────────────


@pytest.mark.asyncio
async def test_messages_grow_each_turn_and_system_prompt_repeats_each_call():
    """Every llm_stream call must (a) re-supply the same system prompt
    (verbatim), and (b) hand over an ever-growing messages list — adding the
    new assistant + tool messages each turn. This pins the wire format the
    bridge contract depends on."""
    record = []
    stream = _scripted_stream([
        [
            {"type": "assistant_message", "message": {
                "role": "assistant", "content": None,
                "tool_calls": [{"id": "c1", "type": "function",
                                "function": {"name": "search_kb",
                                             "arguments": '{"q":"x"}'}}],
            }},
        ],
        [
            {"type": "text_delta", "delta": "done"},
            {"type": "assistant_message",
             "message": {"role": "assistant", "content": "done"}},
        ],
    ], record_calls=record)
    reg = _mk_registry(_ok_tool("search_kb", []))
    _ = [e async for e in run_agent("hi", registry=reg, llm_stream=stream,
                                    course_id="CS231N")]
    assert len(record) == 2
    # System prompt is identical across turns
    assert record[0]["system"] == record[1]["system"]
    assert "CS231N" in record[0]["system"]
    # First call sees just the user; second call sees user + assistant + tool
    assert len(record[0]["messages"]) == 1
    assert record[0]["messages"][0]["role"] == "user"
    roles_t2 = [m["role"] for m in record[1]["messages"]]
    assert roles_t2 == ["user", "assistant", "tool"]


@pytest.mark.asyncio
async def test_long_text_deltas_accumulate_into_done_answer():
    """Pin the contract: every text_delta is appended verbatim into the final
    `answer` field. UI relies on this for "what was streamed == what was
    saved to session log"."""
    deltas = ["chunk-{i:03d} ".format(i=i) for i in range(50)]
    full = "".join(deltas)
    stream = _scripted_stream([
        [*({"type": "text_delta", "delta": d} for d in deltas),
         {"type": "assistant_message",
          "message": {"role": "assistant", "content": full}}],
    ])
    reg = _mk_registry(_ok_tool("search_kb", []))
    events = [e async for e in run_agent("q", registry=reg, llm_stream=stream)]
    assert events[-1]["type"] == "done"
    assert events[-1]["answer"] == full


# ── env clamping ─────────────────────────────────────────────────────


def test_default_max_turns_within_clamp_bounds():
    """`_read_int_env` clamps to [1, 32]. Sanity check: the module-level
    default lands inside the bound regardless of host env."""
    assert 1 <= DEFAULT_MAX_TURNS <= 32


def test_read_int_env_clamps_negative_and_oversize(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_TURNS", "-5")
    assert agent_loop._read_int_env("AGENT_MAX_TURNS", 8, 1, 32) == 1
    monkeypatch.setenv("AGENT_MAX_TURNS", "9999")
    assert agent_loop._read_int_env("AGENT_MAX_TURNS", 8, 1, 32) == 32
    monkeypatch.setenv("AGENT_MAX_TURNS", "not-an-int")
    assert agent_loop._read_int_env("AGENT_MAX_TURNS", 8, 1, 32) == 8
    monkeypatch.delenv("AGENT_MAX_TURNS", raising=False)
    assert agent_loop._read_int_env("AGENT_MAX_TURNS", 8, 1, 32) == 8


# ── parallel tool calls preserve event ordering ──────────────────────


@pytest.mark.asyncio
async def test_event_order_tool_calls_then_results_then_text():
    """Ordering invariant the UI depends on:
    For each batch: ALL `tool_call` events first, then ALL `tool_result`
    events in the same order, then text starts."""
    stream = _scripted_stream([
        [
            {"type": "assistant_message", "message": {
                "role": "assistant", "content": None,
                "tool_calls": [
                    {"id": "a", "type": "function",
                     "function": {"name": "search_kb", "arguments": '{"q":"a"}'}},
                    {"id": "b", "type": "function",
                     "function": {"name": "search_kb", "arguments": '{"q":"b"}'}},
                    {"id": "c", "type": "function",
                     "function": {"name": "search_kb", "arguments": '{"q":"c"}'}},
                ],
            }},
        ],
        [
            {"type": "text_delta", "delta": "answer"},
            {"type": "assistant_message",
             "message": {"role": "assistant", "content": "answer"}},
        ],
    ])
    reg = _mk_registry(_ok_tool("search_kb", [{"hit": True}]))
    events = [e async for e in run_agent("q", registry=reg, llm_stream=stream)]
    types = [e["type"] for e in events]
    # 3 tool_call → 3 tool_result → text → done
    assert types[:3] == ["tool_call", "tool_call", "tool_call"]
    assert types[3:6] == ["tool_result", "tool_result", "tool_result"]
    assert types[-2:] == ["text", "done"]
    # call_id ordering preserved across both phases
    call_ids = [e["call_id"] for e in events if e["type"] == "tool_call"]
    result_ids = [e["call_id"] for e in events if e["type"] == "tool_result"]
    assert call_ids == ["a", "b", "c"]
    assert result_ids == ["a", "b", "c"]
