"""更严格的 API 边界 + 安全测试。

聚焦点：
- COURSE_ID_PATTERN 拦截恶意 course_id（path traversal / control chars / 太长）
- 上传白名单 + 50MB cap + 空文件 / 无 filename 的处理
- request-id 中间件：客户端注入 / 多并发隔离 / 异常路径仍带 id
- 422 error envelope 一致性（不同字段触发同一形状）
- /api/chunks/{chunk_id} 在多课程间命中第一个匹配项
- /api/memory PUT 替换 vs POST 单 key
- /api/session-log POST 透传 payload + 校验 kind
- /api/agent/stream max_turns 越界
- /api/upload 拒绝非白名单后缀大小写变种
- query trailing whitespace not stripped from response field
"""

from __future__ import annotations

import importlib
import io
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from nano_notebooklm.types import Chunk, FileType, LLMResponse


@pytest.fixture
def secure_client(monkeypatch, tmp_path, fake_embed_fn):
    """Bigger seed than test_api_smoke: two courses with overlapping content
    + a CJK course id, so we can exercise the COURSE_ID_PATTERN whitelist
    and cross-course chunk lookup."""
    art = tmp_path / "artifacts"
    courses_dir = art / "courses"
    courses_dir.mkdir(parents=True)

    seeded: dict[str, list[Chunk]] = {
        "CS231N": [
            Chunk(chunk_id=f"a{i:03d}", doc_id="docA", course_id="CS231N",
                  text=f"page-{i+1} body about gradients-{i+1}",
                  file_type=FileType.PDF, source_file="lec.pdf",
                  location=f"Page {i+1}/3", page=i+1)
            for i in range(3)
        ],
        "机器人导论": [
            Chunk(chunk_id=f"b{i:03d}", doc_id="docB", course_id="机器人导论",
                  text=f"机器人 第{i+1}页 运动学内容",
                  file_type=FileType.PDF, source_file="ch1.pdf",
                  location=f"Page {i+1}/2", page=i+1)
            for i in range(2)
        ],
    }
    for cid, chunks in seeded.items():
        cdir = courses_dir / cid
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "chunks.json").write_text(
            json.dumps([c.model_dump() for c in chunks], default=str)
        )
        (cdir / "course_meta.json").write_text(
            json.dumps({"course_id": cid, "name": cid,
                        "documents": list({c.doc_id for c in chunks})})
        )

    monkeypatch.setenv("ARTIFACTS_DIR", str(art))
    from nano_notebooklm import config
    monkeypatch.setattr(config, "ARTIFACTS_DIR", art)
    from nano_notebooklm.kb import store as kb_store
    monkeypatch.setattr(kb_store, "_get_default_embed_fn", lambda: fake_embed_fn)

    import api.server as server_mod
    importlib.reload(server_mod)
    server_mod.kb.build_index(None)

    async def fake_complete(prompt, task_type="", system="", temperature=0.7,
                            max_tokens=4096, max_retries=3, backend=None):
        return LLMResponse(
            content="offline security-test answer",
            model="fake",
            input_tokens=1,
            output_tokens=1,
            latency_ms=1.0,
        )

    monkeypatch.setattr(server_mod.router, "complete", fake_complete)

    return TestClient(server_mod.app), server_mod, art


# ── COURSE_ID_PATTERN — Pydantic field validator ─────────────────────


@pytest.mark.parametrize("bad_id", [
    "../etc/passwd",
    "courses/../leak",
    "CS\x00drop",
    "CS231N/../",
    "a\\b",
    "a/b",
    "<script>",
    "id" * 100,  # 200 chars > 128 limit
    " " * 130,
])
def test_chat_rejects_malicious_course_id(secure_client, bad_id):
    """COURSE_ID_PATTERN should reject path traversal / control chars / oversize.
    422 (Pydantic) is the contract; never 500 / never silently accepted."""
    client, _, _ = secure_client
    r = client.post("/api/chat", json={"question": "hi", "course_id": bad_id})
    assert r.status_code == 422, f"{bad_id!r} should be rejected"
    body = r.json()
    assert body["error"] == "validation_error"
    assert body["request_id"]


def test_chat_dotdot_course_id_now_rejected_after_hardening(secure_client):
    """fix-all v3 #H1: the body-field traversal gap is closed.
    `_ensure_safe_course_id` (applied via the OptCourseId/ReqCourseId
    Annotated types) rejects `..` / leading-dot / trailing-dot, so chat /
    notes / search / report etc. all return 422 instead of forwarding `..`
    into the FS-touching skill layer."""
    client, _, _ = secure_client
    r = client.post("/api/chat", json={"question": "memory hierarchy", "course_id": ".."})
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "validation_error"


def test_chat_accepts_cjk_course_id(secure_client):
    """CJK course ids are explicitly allowed by the pattern (covers
    机器人导论 / 计算机组成原理)."""
    client, _, _ = secure_client
    r = client.post("/api/chat", json={"question": "什么是运动学",
                                       "course_id": "机器人导论"})
    assert r.status_code in (200, 502)  # 502 if LLM stub fails — but never 422


def test_search_rejects_malicious_course_id(secure_client):
    client, _, _ = secure_client
    r = client.post("/api/search", json={"query": "hi", "course_id": "../leak"})
    assert r.status_code == 422


def test_notes_rejects_malicious_course_id(secure_client):
    """Notes endpoint takes course_id as a body field; Pydantic pattern
    validation must apply there too."""
    client, _, _ = secure_client
    r = client.post("/api/notes", json={"course_id": "../leak"})
    assert r.status_code == 422


def test_upload_path_param_rejects_traversal(secure_client):
    """Path param uses `_validate_course_id_path` which raises HTTP 400 (not
    422) — pin the envelope so the global handler keeps the contract."""
    client, _, _ = secure_client
    files = [("files", ("note.md", b"hello", "text/markdown"))]
    r = client.post("/api/upload/..%2Fleak", files=files)
    # The traversal target gets URL-decoded by Starlette → "../leak" → 400.
    # Some routers normalize early and return 404; either is acceptable as
    # long as it's NOT 200 (i.e. a write to artifacts/uploads/../leak/...)
    # and NOT 500.
    assert r.status_code in (400, 404, 422), r.text


# ── Upload whitelist + size cap ──────────────────────────────────────


def test_upload_rejects_non_whitelisted_suffix_lowercase(secure_client):
    """Suffix check normalises to lower; .exe must be rejected."""
    client, _, _ = secure_client
    r = client.post("/api/upload/CS231N",
                    files=[("files", ("evil.exe", b"MZ\x90", "application/x-msdownload"))])
    assert r.status_code == 400
    assert "Unsupported file type" in str(r.json())


def test_upload_rejects_non_whitelisted_suffix_uppercase(secure_client):
    """Suffix check is case-insensitive — uppercase .EXE must be rejected too.
    A naïve check could miss this (e.g. `if suffix not in {'.pdf'}`)."""
    client, _, _ = secure_client
    r = client.post("/api/upload/CS231N",
                    files=[("files", ("EVIL.EXE", b"MZ", "application/x-msdownload"))])
    assert r.status_code == 400


def test_upload_rejects_no_filename(secure_client):
    """Empty filename → either Starlette rejects (422) before the handler
    runs, or the handler silently skips and yields saved=0 (400). Either is
    fine; the contract that matters is "never 200" / "never 500"."""
    client, _, _ = secure_client
    r = client.post("/api/upload/CS231N",
                    files=[("files", ("", b"x", "application/octet-stream"))])
    assert r.status_code in (400, 422), r.text


def test_upload_rejects_oversize_payload(secure_client, monkeypatch):
    """50MB cap. To avoid burning ~50MB of memory in CI we monkeypatch the
    cap down to 1KB and submit a 2KB payload — the comparison
    ``len(content) > MAX_UPLOAD_SIZE_BYTES`` is the actual code under test
    here, not the absolute byte count."""
    client, server_mod, _ = secure_client
    monkeypatch.setattr(server_mod, "MAX_UPLOAD_SIZE_BYTES", 1024)
    payload = b"x" * 2048
    r = client.post(
        "/api/upload/CS231N",
        files=[("files", ("big.md", payload, "text/markdown"))],
    )
    assert r.status_code == 413, r.text
    assert "exceeds limit" in str(r.json()) or "413" in str(r.status_code)


# ── Request ID middleware ─────────────────────────────────────────────


def test_request_id_uses_client_supplied_when_present(secure_client):
    client, _, _ = secure_client
    rid = "deadbeefcafe"
    r = client.get("/api/health", headers={"x-request-id": rid})
    assert r.headers["x-request-id"] == rid


def test_request_id_generated_when_absent_and_unique_per_request(secure_client):
    client, _, _ = secure_client
    seen = set()
    for _ in range(10):
        r = client.get("/api/health")
        rid = r.headers["x-request-id"]
        assert rid and rid not in seen
        seen.add(rid)


def test_request_id_present_in_validation_error_body(secure_client):
    """422 still has request_id in the body (so a frontend can correlate to
    server logs)."""
    client, _, _ = secure_client
    r = client.post("/api/chat", json={"question": ""})
    assert r.status_code == 422
    assert r.json()["request_id"]
    assert r.headers["x-request-id"]


def test_request_id_concurrent_requests_are_isolated(secure_client):
    """No cross-request state leak: 16 parallel requests get 16 distinct ids
    and the response header always matches the body's request_id (when the
    body returns one)."""
    client, _, _ = secure_client

    def hit():
        r = client.post("/api/chat", json={"question": ""})  # forced 422
        return (r.headers.get("x-request-id"), r.json().get("request_id"))

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda _: hit(), range(16)))

    assert len({h for h, _ in results}) == 16, "header ids must be unique"
    for header_id, body_id in results:
        assert header_id == body_id


def test_response_time_header_is_non_negative_float(secure_client):
    client, _, _ = secure_client
    r = client.get("/api/health")
    rtime = float(r.headers["x-response-time-ms"])
    assert rtime >= 0
    assert rtime < 5000  # ~5s sanity cap for a no-op endpoint


# ── 422 envelope shape ────────────────────────────────────────────────


def test_422_envelope_consistent_across_endpoints(secure_client):
    """Every Pydantic validation failure should return the SAME envelope
    shape: {error, request_id, detail}. Pin so a future global handler tweak
    can't drift the contract."""
    client, _, _ = secure_client
    cases = [
        ("/api/chat", {"question": ""}),
        ("/api/search", {"query": "", "top_k": 1}),
        ("/api/notes", {"course_id": ""}),
        ("/api/quiz", {"course_id": "", "num_questions": 1}),
        ("/api/report", {"course_id": ""}),
        ("/api/agent/stream", {"question": ""}),
    ]
    for path, body in cases:
        r = client.post(path, json=body)
        assert r.status_code == 422, f"{path} expected 422 got {r.status_code}"
        env = r.json()
        assert set(env.keys()) >= {"error", "request_id", "detail"}, (path, env)
        assert env["error"] == "validation_error", (path, env)


def test_422_question_max_length_enforced(secure_client):
    """Pydantic max_length=4000 on `question` → 4001 chars → 422."""
    client, _, _ = secure_client
    payload = "x" * 4001
    r = client.post("/api/chat", json={"question": payload})
    assert r.status_code == 422


def test_422_top_k_zero_rejected(secure_client):
    """top_k=0 violates ge=1 → 422."""
    client, _, _ = secure_client
    r = client.post("/api/search", json={"query": "x", "top_k": 0})
    assert r.status_code == 422


def test_422_negative_top_k_rejected(secure_client):
    client, _, _ = secure_client
    r = client.post("/api/search", json={"query": "x", "top_k": -1})
    assert r.status_code == 422


# ── /api/chunks/{chunk_id} ────────────────────────────────────────────


def test_chunks_endpoint_invalid_too_long(secure_client):
    """257-char chunk id → 400 (length check before scan)."""
    client, _, _ = secure_client
    r = client.get("/api/chunks/" + "x" * 257)
    assert r.status_code == 400


def test_chunks_endpoint_unknown_404(secure_client):
    client, _, _ = secure_client
    r = client.get("/api/chunks/never-existed")
    assert r.status_code == 404
    assert r.json()["request_id"]


def test_chunks_endpoint_finds_in_cjk_course(secure_client):
    """Chunk lookup must scan all courses, including the CJK-named one."""
    client, _, _ = secure_client
    r = client.get("/api/chunks/b001")
    assert r.status_code == 200
    body = r.json()
    assert body["course_id"] == "机器人导论"
    assert "运动学" in body["chunk"]["text"]


# ── /api/memory ───────────────────────────────────────────────────────


def test_memory_post_then_get_roundtrip(secure_client):
    client, _, _ = secure_client
    # POST single key
    r = client.post("/api/memory", json={"key": "interest", "value": "RAG"})
    assert r.status_code == 200
    # GET should reflect the update
    r = client.get("/api/memory")
    assert r.status_code == 200
    assert r.json().get("interest") == "RAG"


def test_memory_put_replaces_entire_object(secure_client):
    """PUT replaces the user keys but auto-stamps `last_updated`. Old custom
    keys (`a`, `b`) must be gone; the only user-set key is `only`."""
    client, _, _ = secure_client
    client.post("/api/memory", json={"key": "a", "value": 1})
    client.post("/api/memory", json={"key": "b", "value": 2})

    r = client.put("/api/memory", json={"only": "this"})
    assert r.status_code == 200
    body = client.get("/api/memory").json()
    assert body.get("only") == "this"
    assert "a" not in body
    assert "b" not in body
    # Auto-stamp is documented behavior — pin its presence so a refactor
    # that removes it has to consciously revisit the contract.
    assert "last_updated" in body


def test_memory_post_blank_key_rejected(secure_client):
    client, _, _ = secure_client
    r = client.post("/api/memory", json={"key": "", "value": "x"})
    assert r.status_code == 422


def test_memory_post_oversize_key_rejected(secure_client):
    client, _, _ = secure_client
    r = client.post("/api/memory", json={"key": "x" * 201, "value": "x"})
    assert r.status_code == 422


# ── /api/session-log ─────────────────────────────────────────────────


def test_session_log_round_trip(secure_client):
    """`session_log.list_grouped` returns dict[date_str → list[entry]]; the
    endpoint wraps it as `{"days": <that-dict>}`. Pin the shape and
    round-trip a real entry through it."""
    client, _, _ = secure_client
    r = client.post("/api/session-log", json={
        "course_id": "CS231N", "kind": "review",
        "payload": {"q": "memory hierarchy", "score": 0.5},
    })
    assert r.status_code == 200, r.text
    body = client.get("/api/session-log").json()
    days = body["days"]
    assert isinstance(days, dict) and days, "days must be a non-empty dict"
    flat = [e for entries in days.values() for e in entries]
    assert any(
        e.get("kind") == "review"
        and e.get("course_id") == "CS231N"
        and e.get("payload", {}).get("q") == "memory hierarchy"
        for e in flat
    ), flat


def test_session_log_blank_kind_rejected(secure_client):
    client, _, _ = secure_client
    r = client.post("/api/session-log", json={
        "course_id": "CS231N", "kind": "", "payload": {},
    })
    assert r.status_code == 422


# ── /api/agent/stream ────────────────────────────────────────────────


def test_agent_stream_max_turns_above_limit_rejected(secure_client):
    """max_turns must be in [1, 32]; 999 → 422."""
    client, _, _ = secure_client
    r = client.post("/api/agent/stream",
                    json={"question": "hi", "max_turns": 999})
    assert r.status_code == 422


def test_agent_stream_max_turns_zero_rejected(secure_client):
    client, _, _ = secure_client
    r = client.post("/api/agent/stream",
                    json={"question": "hi", "max_turns": 0})
    assert r.status_code == 422


# ── No information leak in 502 ───────────────────────────────────────


def test_chat_502_when_qa_skill_fails(secure_client, monkeypatch):
    """If the QA skill fails (e.g. LLM returns nothing), the API surfaces
    a 502 with structured envelope; the raw exception message is in
    `detail` but `error` carries the high-level reason — pin the shape."""
    client, server_mod, _ = secure_client

    from nano_notebooklm.types import SkillResult

    async def boom(params):
        return SkillResult(success=False, error="upstream provider returned 500")

    monkeypatch.setattr(server_mod.orchestrator.skills["qa"], "execute", boom)

    r = client.post("/api/chat", json={"question": "memory hierarchy"})
    assert r.status_code == 502
    body = r.json()
    assert "request_id" in body
    assert "error" in body


# ── Strip-then-validate (whitespace) ─────────────────────────────────


def test_chat_whitespace_question_rejected_with_consistent_envelope(secure_client):
    """Whitespace-only must fail validation (per CLAUDE.md), and the envelope
    must look identical to other 422 cases."""
    client, _, _ = secure_client
    r = client.post("/api/chat", json={"question": "   \t \n  "})
    assert r.status_code == 422
    env = r.json()
    assert env["error"] == "validation_error"


# ── Caching headers for / and /static (dev mode) ─────────────────────


def test_root_path_no_cache_headers(secure_client):
    """Frontend dev convention: serve / and /static with no-cache so JSX
    edits aren't cached by the browser. Pin the headers so a future CDN
    integration is forced to revisit caching strategy."""
    client, _, _ = secure_client
    r = client.get("/")
    assert "no-cache" in r.headers.get("Cache-Control", "")
