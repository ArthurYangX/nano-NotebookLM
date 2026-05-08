"""Unit tests for the agent Tool registry + read-only batch executor."""

from __future__ import annotations

import asyncio
import json

import pytest

from nano_notebooklm.orchestrator.agent_tools import (
    Tool,
    ToolCall,
    ToolRegistry,
    run_tool_calls,
)


def _mk_tool(name: str, *, read_only: bool = True, concurrency_safe: bool = True,
             handler=None) -> Tool:
    async def default_handler(args):
        return {"called": name, "args": args}

    return Tool(
        name=name,
        description=f"{name} tool",
        parameters={"type": "object", "properties": {}},
        handler=handler or default_handler,
        is_read_only=read_only,
        concurrency_safe=concurrency_safe,
    )


def test_registry_register_and_get():
    reg = ToolRegistry()
    t = _mk_tool("alpha")
    reg.register(t)
    assert reg.get("alpha") is t
    assert reg.get("missing") is None
    assert reg.names() == ["alpha"]


def test_registry_rejects_duplicate():
    reg = ToolRegistry()
    reg.register(_mk_tool("alpha"))
    with pytest.raises(ValueError):
        reg.register(_mk_tool("alpha"))


def test_openai_schema_shape():
    reg = ToolRegistry()
    reg.register(_mk_tool("alpha"))
    schemas = reg.openai_schemas()
    assert len(schemas) == 1
    assert schemas[0]["type"] == "function"
    assert schemas[0]["function"]["name"] == "alpha"
    assert schemas[0]["function"]["parameters"]["type"] == "object"


def test_tool_call_arguments_parses_json():
    call = ToolCall(call_id="c1", name="x", arguments_raw='{"a": 1, "b": "ok"}')
    assert call.arguments == {"a": 1, "b": "ok"}


def test_tool_call_arguments_handles_garbage():
    assert ToolCall("c", "x", "not-json").arguments == {}
    assert ToolCall("c", "x", "").arguments == {}
    assert ToolCall("c", "x", "[]").arguments == {}  # array → empty dict


@pytest.mark.asyncio
async def test_run_tool_calls_batches_consecutive_readonly():
    """Three read-only tools in a row → one parallel batch (gather)."""
    barrier = asyncio.Event()
    seen = []

    async def slow_handler(args):
        seen.append(("enter", args["i"]))
        await barrier.wait()
        seen.append(("exit", args["i"]))
        return {"i": args["i"]}

    reg = ToolRegistry()
    reg.register(_mk_tool("ro_a", read_only=True, handler=slow_handler))
    reg.register(_mk_tool("ro_b", read_only=True, handler=slow_handler))
    reg.register(_mk_tool("ro_c", read_only=True, handler=slow_handler))

    calls = [
        ToolCall("c1", "ro_a", '{"i": 1}'),
        ToolCall("c2", "ro_b", '{"i": 2}'),
        ToolCall("c3", "ro_c", '{"i": 3}'),
    ]

    async def driver():
        # Let all three enter their handler before unblocking.
        await asyncio.sleep(0.02)
        # All three should be "enter"-ed before any "exit" — that proves parallel.
        assert [s for s in seen if s[0] == "enter"] == [
            ("enter", 1), ("enter", 2), ("enter", 3),
        ]
        barrier.set()

    drive_task = asyncio.create_task(driver())
    results = await run_tool_calls(calls, reg)
    await drive_task

    assert [c.call_id for c, _ in results] == ["c1", "c2", "c3"]
    payloads = [json.loads(s) for _, s in results]
    assert [p["i"] for p in payloads] == [1, 2, 3]


@pytest.mark.asyncio
async def test_run_tool_calls_serial_when_mutating():
    """A mutating tool between two read-only tools → no cross-batch parallelism."""
    timeline = []

    async def make_handler(name):
        async def h(args):
            timeline.append(("enter", name))
            await asyncio.sleep(0.01)
            timeline.append(("exit", name))
            return name
        return h

    reg = ToolRegistry()
    reg.register(_mk_tool("ro1", read_only=True, handler=await make_handler("ro1")))
    reg.register(_mk_tool("write", read_only=False, concurrency_safe=False,
                          handler=await make_handler("write")))
    reg.register(_mk_tool("ro2", read_only=True, handler=await make_handler("ro2")))

    calls = [
        ToolCall("c1", "ro1", "{}"),
        ToolCall("c2", "write", "{}"),
        ToolCall("c3", "ro2", "{}"),
    ]
    results = await run_tool_calls(calls, reg)

    # Each tool fully completes before the next starts (no interleaving).
    pairs = [(timeline[i], timeline[i + 1]) for i in range(0, 6, 2)]
    for enter, exit_ in pairs:
        assert enter[0] == "enter" and exit_[0] == "exit" and enter[1] == exit_[1]
    assert [r for _, r in results] == ["ro1", "write", "ro2"]


@pytest.mark.asyncio
async def test_run_tool_calls_unknown_tool_returns_error_string():
    reg = ToolRegistry()
    calls = [ToolCall("c1", "missing", "{}")]
    results = await run_tool_calls(calls, reg)
    assert "ERROR" in results[0][1] and "missing" in results[0][1]


@pytest.mark.asyncio
async def test_run_tool_calls_handler_exception_caught():
    async def boom(args):
        raise RuntimeError("kaboom")

    reg = ToolRegistry()
    reg.register(_mk_tool("bad", handler=boom))

    results = await run_tool_calls([ToolCall("c1", "bad", "{}")], reg)
    assert "ERROR" in results[0][1] and "kaboom" in results[0][1]
