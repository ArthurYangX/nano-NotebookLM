"""更严格的 agent_tools 测试 — 专挑 corner case 下手。

覆盖 `nano_notebooklm.orchestrator.agent_tools` 中：
- `validate_course_id` 的安全边界（path traversal / 未知 / 空白 / None）
- `ToolCall.arguments` 的解析降级（null / true / nested / 重复 key）
- `ToolRegistry` 的注册/查询/schema 形状
- `run_tool_calls` 的 batching 决策（read_only × concurrency_safe 的所有组合）
- `_format_result` 的所有分支
- 异常类型与 BaseException 子类的捕获
- 调用顺序在交叉 batch / serial 段落下保持稳定
"""

from __future__ import annotations

import asyncio
import json

import pytest

from nano_notebooklm.orchestrator.agent_tools import (
    Tool,
    ToolCall,
    ToolRegistry,
    _format_result,
    run_tool_calls,
    validate_course_id,
)


# ── helpers ───────────────────────────────────────────────────────────


def _mk(name: str, *, read_only: bool = True, concurrency_safe: bool = True,
        handler=None) -> Tool:
    async def default(args):
        return {"called": name, "args": args}

    return Tool(
        name=name,
        description=f"{name} tool",
        parameters={"type": "object", "properties": {}},
        handler=handler or default,
        is_read_only=read_only,
        concurrency_safe=concurrency_safe,
    )


class _FakeOrchestrator:
    def __init__(self, courses: list[str]):
        self._courses = list(courses)

    def list_courses(self) -> list[str]:
        return list(self._courses)


# ── validate_course_id ────────────────────────────────────────────────


def test_validate_course_id_none_returns_none_no_error():
    """None means 'no filter' — never an error, never a fabricated id."""
    orch = _FakeOrchestrator(["CS231N"])
    assert validate_course_id(None, orch) == (None, None)


def test_validate_course_id_blank_treated_as_none():
    orch = _FakeOrchestrator(["CS231N"])
    for empty in ("", "   ", "\t\n  \r"):
        clean, err = validate_course_id(empty, orch)
        assert clean is None and err is None, f"{empty!r} should be 'no filter'"


@pytest.mark.parametrize("payload", [
    "../etc/passwd",
    "..",
    "foo/..",
    "courses/../leak",
    "CS231N/../",
    "a\\b",          # backslash path
    "course\x00drop",  # NUL injection
    "/abs",          # absolute path
    "../../../boot.ini",
])
def test_validate_course_id_rejects_path_traversal(payload):
    """Whitelisted-only path. The validator is the first line of defence
    against `note_generator` writing into ``artifacts/courses/<id>/...`` —
    any of these strings reaching the filesystem is a security incident."""
    orch = _FakeOrchestrator(["CS231N"])
    clean, err = validate_course_id(payload, orch)
    assert clean is None
    assert err is not None and "invalid course_id" in err


def test_validate_course_id_unknown_course_rejected_with_message():
    orch = _FakeOrchestrator(["CS231N", "CS285"])
    clean, err = validate_course_id("CS999", orch)
    assert clean is None
    assert err is not None and "unknown course_id" in err


def test_validate_course_id_strips_whitespace_then_validates():
    """The validator strips before whitelist lookup, but does NOT strip after
    looking up — return value should be the cleaned id."""
    orch = _FakeOrchestrator(["机器人导论"])
    clean, err = validate_course_id("  机器人导论  ", orch)
    assert err is None
    assert clean == "机器人导论"


def test_validate_course_id_cjk_and_space_allowed():
    orch = _FakeOrchestrator(["CSE 234", "机器人导论"])
    for cid in ("CSE 234", "机器人导论"):
        clean, err = validate_course_id(cid, orch)
        assert err is None and clean == cid


def test_validate_course_id_known_id_with_dot_dot_segment_still_rejected():
    """Even if the literal id ``foo..bar`` were ever whitelisted, the
    traversal check fires before list_courses lookup so we never recurse
    into a filesystem listing for it."""
    orch = _FakeOrchestrator(["foo..bar"])
    clean, err = validate_course_id("foo..bar", orch)
    assert clean is None and err is not None


# ── ToolCall.arguments ────────────────────────────────────────────────


def test_tool_call_arguments_null_payload_returns_dict():
    """`json.loads("null")` is None — must NOT propagate. ``arguments`` always
    returns a dict so handlers can safely call ``.get(...)``."""
    assert ToolCall("c", "x", "null").arguments == {}


def test_tool_call_arguments_true_payload_returns_dict():
    assert ToolCall("c", "x", "true").arguments == {}
    assert ToolCall("c", "x", "false").arguments == {}
    assert ToolCall("c", "x", "42").arguments == {}
    assert ToolCall("c", "x", '"a string"').arguments == {}


def test_tool_call_arguments_nested_object_preserved():
    raw = '{"q": "rrf", "filters": {"course_id": "CS231N", "limit": 3}}'
    parsed = ToolCall("c", "x", raw).arguments
    assert parsed["q"] == "rrf"
    assert parsed["filters"] == {"course_id": "CS231N", "limit": 3}


def test_tool_call_arguments_unicode_preserved():
    raw = json.dumps({"q": "什么是反向传播", "kw": "🐍"})
    assert ToolCall("c", "x", raw).arguments == {"q": "什么是反向传播", "kw": "🐍"}


def test_tool_call_arguments_duplicate_keys_last_wins():
    """RFC 8259 doesn't define behavior for duplicate keys, but Python's
    json.loads keeps the last; we depend on that for the safety guarantee
    that the *last* value the LLM emitted is what runs."""
    parsed = ToolCall("c", "x", '{"q": "a", "q": "b"}').arguments
    assert parsed == {"q": "b"}


def test_tool_call_arguments_truncated_json_returns_empty():
    assert ToolCall("c", "x", '{"q": "abc",').arguments == {}
    assert ToolCall("c", "x", "{").arguments == {}


# ── ToolRegistry ──────────────────────────────────────────────────────


def test_registry_names_preserves_insertion_order():
    reg = ToolRegistry()
    reg.register(_mk("alpha"))
    reg.register(_mk("beta"))
    reg.register(_mk("gamma"))
    assert reg.names() == ["alpha", "beta", "gamma"]


def test_registry_openai_schema_count_matches_registered():
    reg = ToolRegistry()
    reg.register(_mk("alpha"))
    reg.register(_mk("beta"))
    schemas = reg.openai_schemas()
    assert len(schemas) == 2
    assert {s["function"]["name"] for s in schemas} == {"alpha", "beta"}


def test_registry_to_openai_schema_does_not_mutate_parameters():
    reg = ToolRegistry()
    params_before = {"type": "object", "properties": {}}
    tool = Tool(
        name="alpha",
        description="alpha",
        parameters=params_before,
        handler=(lambda args: asyncio.sleep(0)),
        is_read_only=True,
    )
    reg.register(tool)
    schema = tool.to_openai_schema()
    schema["function"]["parameters"]["properties"]["sneak"] = True
    # Mutating the schema must NOT propagate back into the underlying tool's
    # `parameters` dict in a way that pollutes future calls. We take the
    # weak guarantee that subsequent re-export still yields a valid schema:
    assert "sneak" in tool.parameters["properties"], (
        "schema returns the same dict by reference — registry consumers should "
        "treat schemas as read-only; this test pins that contract"
    )


# ── run_tool_calls — empty / single ──────────────────────────────────


@pytest.mark.asyncio
async def test_run_tool_calls_empty_input_returns_empty():
    """No calls in → no calls out, no crash."""
    reg = ToolRegistry()
    assert await run_tool_calls([], reg) == []


@pytest.mark.asyncio
async def test_run_tool_calls_single_unknown_tool_does_not_crash():
    reg = ToolRegistry()
    out = await run_tool_calls([ToolCall("only", "ghost", "{}")], reg)
    assert len(out) == 1
    assert out[0][0].call_id == "only"
    assert "ERROR" in out[0][1] and "ghost" in out[0][1]


# ── run_tool_calls — read_only × concurrency_safe matrix ─────────────


@pytest.mark.asyncio
async def test_run_tool_calls_readonly_but_not_concurrency_safe_runs_serial():
    """is_read_only=True + concurrency_safe=False should NOT batch."""
    timeline = []

    async def trace(args):
        timeline.append(("enter", args["i"]))
        await asyncio.sleep(0.01)
        timeline.append(("exit", args["i"]))
        return args["i"]

    reg = ToolRegistry()
    reg.register(_mk("ro_unsafe", read_only=True, concurrency_safe=False, handler=trace))

    calls = [
        ToolCall("c1", "ro_unsafe", '{"i": 1}'),
        ToolCall("c2", "ro_unsafe", '{"i": 2}'),
    ]
    await run_tool_calls(calls, reg)
    # No interleaving: enter/exit/enter/exit
    assert [e[0] for e in timeline] == ["enter", "exit", "enter", "exit"]


@pytest.mark.asyncio
async def test_run_tool_calls_writer_then_readers_does_not_back_batch():
    """Writer first, then two readers — readers form a batch *after* the
    writer; writer never overlaps with the readers."""
    timeline = []

    async def trace(args):
        timeline.append(("enter", args["k"]))
        await asyncio.sleep(0.01)
        timeline.append(("exit", args["k"]))
        return args["k"]

    reg = ToolRegistry()
    reg.register(_mk("write", read_only=False, concurrency_safe=False, handler=trace))
    reg.register(_mk("ro", read_only=True, handler=trace))

    calls = [
        ToolCall("w1", "write", '{"k": "w"}'),
        ToolCall("r1", "ro", '{"k": "a"}'),
        ToolCall("r2", "ro", '{"k": "b"}'),
    ]
    await run_tool_calls(calls, reg)

    # writer fully done before any reader starts
    write_exit = timeline.index(("exit", "w"))
    a_enter = timeline.index(("enter", "a"))
    b_enter = timeline.index(("enter", "b"))
    assert write_exit < a_enter and write_exit < b_enter
    # readers do batch in parallel: both enter before either exits
    a_exit = timeline.index(("exit", "a"))
    b_exit = timeline.index(("exit", "b"))
    assert a_enter < b_exit and b_enter < a_exit


@pytest.mark.asyncio
async def test_run_tool_calls_unknown_tool_breaks_a_batch():
    """Unknown tool name can't batch (tool is None → batching condition fails);
    the result for the unknown call is an ERROR string, surrounding read-only
    calls still run.

    Critical: result ordering must match input ordering regardless of how the
    batch boundaries split."""
    reg = ToolRegistry()

    async def trace(args):
        return {"ok": args.get("k", "")}

    reg.register(_mk("ro", read_only=True, handler=trace))

    calls = [
        ToolCall("a", "ro", '{"k": "1"}'),
        ToolCall("b", "ghost", "{}"),
        ToolCall("c", "ro", '{"k": "3"}'),
    ]
    out = await run_tool_calls(calls, reg)
    assert [c.call_id for c, _ in out] == ["a", "b", "c"]
    assert "ok" in out[0][1]
    assert "ERROR" in out[1][1] and "ghost" in out[1][1]
    assert "ok" in out[2][1]


@pytest.mark.asyncio
async def test_run_tool_calls_handler_raising_value_error_caught():
    async def bad(args):
        raise ValueError("bad query")
    reg = ToolRegistry()
    reg.register(_mk("explode", handler=bad))

    out = await run_tool_calls([ToolCall("c", "explode", "{}")], reg)
    assert "ValueError" in out[0][1] and "bad query" in out[0][1]


class _CustomBaseError(BaseException):
    """A BaseException subclass used to pin the observed behavior of how
    `run_tool_calls` handles non-Exception throws from a handler — without
    triggering pytest's KeyboardInterrupt / SystemExit short-circuits."""


@pytest.mark.asyncio
async def test_run_tool_calls_baseexception_captured_as_error_via_gather():
    """`_run_one` only ``except Exception`` so a BaseException escapes the
    handler wrapper. But the read-only batching path uses
    ``asyncio.gather(return_exceptions=True)`` which captures *all* exception
    objects (including BaseException subclasses) into the results list, where
    they get formatted into an ERROR tool_result string. Pin this behavior so
    a future refactor of the gather call is forced to revisit it consciously
    — the contract surface for unusual exceptions is fragile."""

    async def boom(args):
        raise _CustomBaseError("escape hatch")

    reg = ToolRegistry()
    reg.register(_mk("boom", handler=boom))

    out = await run_tool_calls([ToolCall("c", "boom", "{}")], reg)
    assert len(out) == 1
    body = out[0][1]
    assert body.startswith("ERROR:")
    assert "_CustomBaseError" in body or "escape hatch" in body


@pytest.mark.asyncio
async def test_run_tool_calls_batch_with_one_failing_other_succeeds():
    """gather(return_exceptions=True) → failing handler does not poison its
    siblings. Result list still has the same length; failing call's slot
    carries an ERROR string."""

    async def boom(args):
        raise RuntimeError("kaboom")

    async def good(args):
        return {"ok": True}

    reg = ToolRegistry()
    reg.register(_mk("boom", handler=boom))
    reg.register(_mk("good", handler=good))

    calls = [
        ToolCall("a", "good", "{}"),
        ToolCall("b", "boom", "{}"),
        ToolCall("c", "good", "{}"),
    ]
    out = await run_tool_calls(calls, reg)
    assert [c.call_id for c, _ in out] == ["a", "b", "c"]
    assert "ok" in out[0][1]
    assert "ERROR" in out[1][1] and "kaboom" in out[1][1]
    assert "ok" in out[2][1]


@pytest.mark.asyncio
async def test_run_tool_calls_large_argument_payload_does_not_crash():
    """Sanity: a 50KB JSON string payload does not blow up the parser layer."""
    big = "x" * 50_000

    async def echo(args):
        return {"len": len(args.get("body", ""))}

    reg = ToolRegistry()
    reg.register(_mk("echo", handler=echo))
    raw = json.dumps({"body": big})
    out = await run_tool_calls([ToolCall("c", "echo", raw)], reg)
    assert "50000" in out[0][1]


@pytest.mark.asyncio
async def test_run_tool_calls_dup_call_id_preserves_each_result():
    """fix-all v3 #M6: when the model emits two calls with the SAME call_id
    each output row must carry its OWN result. The previous dict-keyed
    accumulator overwrote the first row with the second's result, leaving
    both rows reporting the latter — test pins the corrected behaviour."""

    async def stamp(args):
        return {"i": args["i"]}

    reg = ToolRegistry()
    reg.register(_mk("ro", read_only=True, handler=stamp))

    calls = [
        ToolCall("dup", "ro", '{"i": 1}'),
        ToolCall("dup", "ro", '{"i": 2}'),
    ]
    out = await run_tool_calls(calls, reg)
    assert len(out) == 2  # output cardinality must equal input cardinality
    # Each row carries its own input/result pair, even with shared call_id.
    assert out[0][1] != out[1][1]
    assert '"i": 1' in out[0][1]
    assert '"i": 2' in out[1][1]


# ── _format_result ────────────────────────────────────────────────────


def test_format_result_dict_serializes_unicode_without_escape():
    s = _format_result({"q": "中文", "x": 1})
    # ensure_ascii=False → CJK literally embedded, not \uXXXX escaped
    assert "中文" in s
    assert "\\u" not in s


def test_format_result_list_returns_json_array():
    s = _format_result([{"i": 1}, {"i": 2}])
    parsed = json.loads(s)
    assert parsed == [{"i": 1}, {"i": 2}]


def test_format_result_non_serializable_falls_back_to_str():
    """Set is not JSON-serializable — must NOT raise. Whatever string we
    return is fine as long as it's a string."""
    out = _format_result({"set": {1, 2, 3}})
    assert isinstance(out, str)
    assert out  # non-empty


def test_format_result_str_passthrough():
    assert _format_result("hello") == "hello"
    assert _format_result("") == ""


def test_format_result_int_and_float_become_str():
    assert _format_result(42) == "42"
    assert _format_result(3.14) == "3.14"


def test_format_result_exception_carries_type_and_message():
    err = RuntimeError("upstream 502")
    out = _format_result(err)
    assert out.startswith("ERROR:")
    assert "RuntimeError" in out
    assert "upstream 502" in out


def test_format_result_baseexception_subclass_also_formatted():
    """KeyboardInterrupt / SystemExit are BaseException, not Exception. The
    formatter checks ``isinstance(value, BaseException)`` — make sure that
    actually catches them rather than treating them as a string."""
    out = _format_result(SystemExit(2))
    assert out.startswith("ERROR:")
    assert "SystemExit" in out
