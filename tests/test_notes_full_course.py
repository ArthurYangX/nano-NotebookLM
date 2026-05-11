"""Tests for full-course note generation (per-file → merge → review).

Covers the skill helpers (plan / concat / prepare_review_inputs / generate_file)
and the streaming endpoint (event order, file_error surfacing, empty-course
guard, all-files-failed exit, terminal sanitization).

No live LLM — router.complete / complete_stream are monkeypatched.
"""

from __future__ import annotations

import asyncio
import importlib
import json

import pytest
from fastapi.testclient import TestClient

from nano_notebooklm.skills import notes_full_course
from nano_notebooklm.skills.notes_full_course import (
    FilePlan,
    FileResult,
    _escape_latex_title,
    _group_chunks_by_file,
    concat_draft,
    plan_for_course,
    prepare_review_inputs,
    generate_file,
)


# ── Unit tests: helpers ──────────────────────────────────────────────


def test_group_chunks_preserves_first_occurrence_order(sample_chunks):
    """sample_chunks order: ml.pdf, ml.pdf, rl.pdf, nlp.pdf, ir.pdf, zh.pdf.
    groupby keys should appear in that order — chunk_id is monotonic and
    later code relies on idx → source_file being stable across calls."""
    groups = _group_chunks_by_file(sample_chunks)
    assert list(groups.keys()) == ["ml.pdf", "rl.pdf", "nlp.pdf", "ir.pdf", "zh.pdf"]
    assert len(groups["ml.pdf"]) == 2
    assert len(groups["rl.pdf"]) == 1


def test_plan_for_course_builds_one_plan_per_file(sample_chunks, monkeypatch):
    class FakeKB:
        def get_chunks(self, course_id):
            return sample_chunks

    plans = plan_for_course(FakeKB(), "testcourse", user_lang="zh")
    assert len(plans) == 5
    assert [p.idx for p in plans] == [0, 1, 2, 3, 4]
    assert [p.source_file for p in plans] == [
        "ml.pdf", "rl.pdf", "nlp.pdf", "ir.pdf", "zh.pdf",
    ]
    # user_lang binding should be appended to the system prompt
    assert "中文" in plans[0].system or "zh" in plans[0].system
    # Per-file source_text should mention the file's chunks specifically
    assert "Backpropagation" in plans[0].prompt
    assert "Convolutional" in plans[0].prompt
    # rl.pdf plan should NOT contain ml.pdf content
    assert "Backpropagation" not in plans[1].prompt


def test_plan_for_course_empty_when_no_chunks():
    class EmptyKB:
        def get_chunks(self, course_id):
            return []

    assert plan_for_course(EmptyKB(), "testcourse") == []


def test_plan_for_course_caps_chunks_per_file(sample_chunks, monkeypatch):
    """A file with more than MAX_CHUNKS_PER_FILE chunks should be truncated
    to the cap — protects the per-file prompt from overflowing context."""
    big_chunks = []
    for i in range(notes_full_course.MAX_CHUNKS_PER_FILE + 10):
        c = sample_chunks[0].model_copy(update={
            "chunk_id": f"big-{i}",
            "text": f"chunk number {i}",
        })
        big_chunks.append(c)

    class BigKB:
        def get_chunks(self, course_id):
            return big_chunks

    plans = plan_for_course(BigKB(), "testcourse")
    assert len(plans) == 1
    assert plans[0].chunk_count == notes_full_course.MAX_CHUNKS_PER_FILE
    # The capped-off chunks should be absent from the prompt
    assert f"chunk number {notes_full_course.MAX_CHUNKS_PER_FILE + 5}" not in plans[0].prompt


def test_escape_latex_title_handles_specials_and_paths():
    assert _escape_latex_title("uploaded/lecture_3.pdf") == r"lecture\_3.pdf"
    assert _escape_latex_title("a&b.pdf") == r"a\&b.pdf"
    assert _escape_latex_title("c$d%e.pdf") == r"c\$d\%e.pdf"
    # Unicode (Chinese) passes through unchanged for xeCJK rendering
    assert _escape_latex_title("第一讲.pdf") == "第一讲.pdf"


def test_concat_draft_wraps_in_section_in_idx_order():
    results = [
        FileResult(idx=0, source_file="a.pdf", chunk_count=1,
                   content=r"\textbf{A body}", error=None),
        FileResult(idx=2, source_file="c.pdf", chunk_count=1,
                   content=r"\textbf{C body}", error=None),
        FileResult(idx=1, source_file="b.pdf", chunk_count=1,
                   content=r"\textbf{B body}", error=None),
    ]
    draft = concat_draft(results)
    # Must appear in idx order regardless of input order
    assert draft.index(r"\section{a.pdf}") < draft.index(r"\section{b.pdf}")
    assert draft.index(r"\section{b.pdf}") < draft.index(r"\section{c.pdf}")
    assert r"\textbf{A body}" in draft


def test_concat_draft_skips_failed_results():
    results = [
        FileResult(idx=0, source_file="a.pdf", chunk_count=1,
                   content=r"\textbf{A}", error=None),
        FileResult(idx=1, source_file="b.pdf", chunk_count=1,
                   content=None, error="latex_unsafe: \\input"),
        FileResult(idx=2, source_file="c.pdf", chunk_count=1,
                   content=r"\textbf{C}", error=None),
    ]
    draft = concat_draft(results)
    assert r"\section{a.pdf}" in draft
    assert r"\section{c.pdf}" in draft
    assert r"\section{b.pdf}" not in draft


def test_prepare_review_inputs_shape():
    out = prepare_review_inputs(
        course_id="cs231n", draft=r"\section{a}\textbf{x}",
        file_count=3, user_lang="en",
    )
    assert set(out.keys()) >= {"prompt", "system", "task_type",
                               "temperature", "max_tokens"}
    assert "cs231n" in out["prompt"]
    assert "3" in out["prompt"]
    # user_lang binding appended
    assert "English" in out["system"] or "en" in out["system"]
    assert out["task_type"] == "note_generation"


# ── Unit tests: generate_file (mocked router) ────────────────────────


class _FakeResponse:
    def __init__(self, content):
        self.content = content
        self.model = "fake"
        self.input_tokens = 100
        self.output_tokens = 50


class _FakeRouter:
    def __init__(self, responder):
        # responder: callable(prompt) -> str OR Exception to raise
        self.responder = responder
        self.calls = []
        self.in_flight = 0
        self.max_in_flight = 0
        self._lock = asyncio.Lock()

    async def complete(self, prompt, task_type="", system="",
                       temperature=0.7, max_tokens=4096):
        async with self._lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0.01)  # let other concurrent calls race
            self.calls.append({"prompt": prompt, "task_type": task_type})
            result = self.responder(prompt)
            if isinstance(result, Exception):
                raise result
            return _FakeResponse(result)
        finally:
            async with self._lock:
                self.in_flight -= 1


def _make_plan(idx=0, source_file="a.pdf"):
    return FilePlan(
        idx=idx, source_file=source_file, chunk_count=1,
        prompt="prompt", system="sys", task_type="note_generation",
        temperature=0.3, max_tokens=8192,
    )


def test_generate_file_happy_path():
    router = _FakeRouter(lambda p: r"\section{a}\textbf{ok}")
    result = asyncio.run(generate_file(router, _make_plan()))
    assert result.error is None
    assert r"\textbf{ok}" in result.content


def test_generate_file_catches_llm_exception():
    router = _FakeRouter(lambda p: RuntimeError("backend down"))
    result = asyncio.run(generate_file(router, _make_plan()))
    assert result.content is None
    assert result.error == "RuntimeError"


def test_generate_file_rejects_unsafe_latex():
    # \input is on the sanitizer blacklist
    router = _FakeRouter(lambda p: r"\input{/etc/passwd}")
    result = asyncio.run(generate_file(router, _make_plan()))
    assert result.content is None
    assert result.error and result.error.startswith("latex_unsafe:")


def test_generate_file_returns_empty_marker_on_blank_response():
    router = _FakeRouter(lambda p: "   \n  ")
    result = asyncio.run(generate_file(router, _make_plan()))
    assert result.content is None
    assert result.error == "empty_llm_response"


def test_generate_file_respects_semaphore_concurrency():
    """With Semaphore(2) and 6 parallel calls, max in-flight must never exceed 2."""
    router = _FakeRouter(lambda p: r"\textbf{ok}")
    sem = asyncio.Semaphore(2)
    plans = [_make_plan(idx=i, source_file=f"f{i}.pdf") for i in range(6)]

    async def run_all():
        return await asyncio.gather(*[generate_file(router, p, sem) for p in plans])

    results = asyncio.run(run_all())
    assert all(r.error is None for r in results)
    assert router.max_in_flight <= 2
    assert router.max_in_flight >= 1  # something actually ran in parallel


# ── Endpoint tests (TestClient + monkeypatched router) ─────────────────


@pytest.fixture
def fc_client(monkeypatch, tmp_path, sample_chunks, fake_embed_fn):
    """Same setup pattern as test_streaming_api.streaming_client."""
    art = tmp_path / "artifacts"
    (art / "courses" / "testcourse").mkdir(parents=True)
    (art / "courses" / "testcourse" / "chunks.json").write_text(
        json.dumps([c.model_dump() for c in sample_chunks], default=str)
    )
    monkeypatch.setenv("ARTIFACTS_DIR", str(art))

    from nano_notebooklm import config
    monkeypatch.setattr(config, "ARTIFACTS_DIR", art)
    from nano_notebooklm.kb import store as kb_store
    monkeypatch.setattr(kb_store, "_get_default_embed_fn", lambda: fake_embed_fn)

    import api.server as server_mod
    importlib.reload(server_mod)
    server_mod.kb.build_index("testcourse")
    return TestClient(server_mod.app), server_mod


def _read_events(response):
    out = []
    for line in response.iter_lines():
        if line:
            out.append(json.loads(line))
    return out


def test_endpoint_happy_path_emits_full_pipeline(fc_client, monkeypatch):
    client, server_mod = fc_client

    async def fake_complete(prompt, task_type="", system="",
                            temperature=0.7, max_tokens=4096):
        # Return a deterministic-but-unique body per file so we can spot
        # them in the concat draft.
        body = r"\textbf{body}"
        if "ml.pdf" in prompt:
            body = r"\textbf{ml body}"
        elif "rl.pdf" in prompt:
            body = r"\textbf{rl body}"
        return _FakeResponse(body)

    async def fake_complete_stream(prompt, task_type="", system="",
                                   temperature=0.7, max_tokens=4096):
        # Pretend the review pass polished the draft — emit a couple of
        # deltas containing the merged \section headers so the test can
        # assert pass-through.
        yield r"\section{ml.pdf}"
        yield "\n"
        yield r"\textbf{polished ml body}"

    monkeypatch.setattr(server_mod.router, "complete", fake_complete)
    monkeypatch.setattr(server_mod.router, "complete_stream", fake_complete_stream)

    response = client.post("/api/notes/full-course/stream",
                           json={"course_id": "testcourse", "concurrency": 2})
    assert response.status_code == 200
    events = _read_events(response)
    types = [e["type"] for e in events]

    # plan is always first; done is always last
    assert types[0] == "plan"
    assert types[-1] == "done"
    # All 5 files should have produced a file_start + file_done pair
    starts = [e for e in events if e["type"] == "file_start"]
    dones = [e for e in events if e["type"] == "file_done"]
    assert len(starts) == 5
    assert len(dones) == 5
    # merging + reviewing markers appear once each, after all file_done events
    assert types.count("merging") == 1
    assert types.count("reviewing") == 1
    # review_chunk events stream the polish pass
    review = [e for e in events if e["type"] == "review_chunk"]
    assert len(review) >= 2
    # final done content contains the reviewed body
    final = events[-1]
    assert "polished ml body" in final["content"]
    assert final["files_succeeded"] == 5
    assert final["files_failed"] == 0


def test_endpoint_emits_file_error_for_unsafe_latex(fc_client, monkeypatch):
    client, server_mod = fc_client

    async def fake_complete(prompt, task_type="", system="",
                            temperature=0.7, max_tokens=4096):
        # Inject a forbidden command for the rl.pdf file only
        if "rl.pdf" in prompt:
            return _FakeResponse(r"\input{/etc/passwd}")
        return _FakeResponse(r"\textbf{ok}")

    async def fake_complete_stream(prompt, task_type="", system="",
                                   temperature=0.7, max_tokens=4096):
        yield r"\textbf{reviewed}"

    monkeypatch.setattr(server_mod.router, "complete", fake_complete)
    monkeypatch.setattr(server_mod.router, "complete_stream", fake_complete_stream)

    response = client.post("/api/notes/full-course/stream",
                           json={"course_id": "testcourse"})
    events = _read_events(response)
    errors = [e for e in events if e["type"] == "file_error"]
    assert len(errors) == 1
    assert errors[0]["source_file"] == "rl.pdf"
    assert errors[0]["error"].startswith("latex_unsafe:")
    # The other 4 files should still succeed and feed into the review pass
    final = events[-1]
    assert final["type"] == "done"
    assert final["files_succeeded"] == 4
    assert final["files_failed"] == 1


def test_endpoint_rejects_empty_course(fc_client, monkeypatch):
    client, server_mod = fc_client
    # Empty out the course — replace get_chunks with a stub
    monkeypatch.setattr(server_mod.kb, "get_chunks", lambda course_id=None: [])

    response = client.post("/api/notes/full-course/stream",
                           json={"course_id": "testcourse"})
    events = _read_events(response)
    assert len(events) == 1
    assert events[0]["type"] == "error"
    assert events[0]["error"] == "no_chunks"


def test_endpoint_handles_all_files_failed(fc_client, monkeypatch):
    client, server_mod = fc_client

    async def fake_complete(prompt, task_type="", system="",
                            temperature=0.7, max_tokens=4096):
        raise RuntimeError("backend down")

    monkeypatch.setattr(server_mod.router, "complete", fake_complete)

    response = client.post("/api/notes/full-course/stream",
                           json={"course_id": "testcourse"})
    events = _read_events(response)
    types = [e["type"] for e in events]
    # All five files should error, then a global error event closes the stream
    assert types.count("file_error") == 5
    # No reviewing/merging events when there's nothing left to merge
    assert "merging" not in types
    assert "reviewing" not in types
    assert events[-1]["type"] == "error"
    assert events[-1]["error"] == "all_files_failed"


def test_endpoint_rejects_invalid_concurrency(fc_client):
    client, _ = fc_client
    # concurrency=0 is below the ge=1 floor → 422 from Pydantic
    response = client.post("/api/notes/full-course/stream",
                           json={"course_id": "testcourse", "concurrency": 0})
    assert response.status_code == 422
    response = client.post("/api/notes/full-course/stream",
                           json={"course_id": "testcourse", "concurrency": 99})
    assert response.status_code == 422


def test_endpoint_rejects_unknown_field(fc_client):
    client, _ = fc_client
    response = client.post("/api/notes/full-course/stream",
                           json={"course_id": "testcourse", "topic": "x"})
    assert response.status_code == 422
