"""Layer 1 — fast offline regression smoke for the search layer.

Loads a curated 30-question subset and runs them via FastAPI TestClient (no
network, no LLM). Asserts a global hit-rate floor and per-course presence so
the kind of bug that triggered "No relevant content found in the selected
sources" never lands silently again.

The full Layer 2 eval (~750 questions, live server) lives in
`scripts/run_eval.py`. This file is the CI-fast guard.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# A curated subset chosen to exercise:
# - All 8 courses
# - High-frequency true concepts (should always hit)
# - Cross-course meta questions ("这是什么课") in both single-course and All Courses mode
# - Bilingual queries
SMOKE_QUESTIONS: list[dict] = [
    # 15-213 (CSAPP)
    {"course_id": "15-213", "query": "cache", "min_score": 0.0},
    {"course_id": "15-213", "query": "memory hierarchy", "min_score": 0.0},
    {"course_id": "15-213", "query": "stack", "min_score": 0.0},
    # CS182 (deep learning)
    {"course_id": "CS182", "query": "gradient", "min_score": 0.0},
    {"course_id": "CS182", "query": "backpropagation", "min_score": 0.0},
    # CS231N (vision)
    {"course_id": "CS231N", "query": "convolution", "min_score": 0.0},
    {"course_id": "CS231N", "query": "neural network", "min_score": 0.0},
    # CS285 (RL)
    {"course_id": "CS285", "query": "policy", "min_score": 0.0},
    {"course_id": "CS285", "query": "reward", "min_score": 0.0},
    # CSE 234
    {"course_id": "CSE 234", "query": "transformer", "min_score": 0.0},
    {"course_id": "CSE 234", "query": "training", "min_score": 0.0},
    # 机器人导论
    {"course_id": "机器人导论", "query": "传感器", "min_score": 0.0},
    {"course_id": "机器人导论", "query": "运动学", "min_score": 0.0},
    {"course_id": "机器人导论", "query": "路径规划", "min_score": 0.0},
    # 计算机组成原理
    {"course_id": "计算机组成原理", "query": "内存", "min_score": 0.0},
    {"course_id": "计算机组成原理", "query": "存储器", "min_score": 0.0},
    {"course_id": "计算机组成原理", "query": "寄存器", "min_score": 0.0},
    # 模式识别
    {"course_id": "模式识别", "query": "特征", "min_score": 0.0},
    {"course_id": "模式识别", "query": "分类器", "min_score": 0.0},
    {"course_id": "模式识别", "query": "聚类", "min_score": 0.0},
    # All-Courses meta (course_id=None) — these are the ones that triggered
    # the "No relevant content found" bug
    {"course_id": None, "query": "这是什么课", "min_score": 0.0},
    {"course_id": None, "query": "内存是什么", "min_score": 0.0},
    {"course_id": None, "query": "什么是反向传播", "min_score": 0.0},
    {"course_id": None, "query": "what is backpropagation", "min_score": 0.0},
    {"course_id": None, "query": "传感器", "min_score": 0.0},
    # Single-course meta
    {"course_id": "计算机组成原理", "query": "这是什么课", "min_score": 0.0},
    {"course_id": "机器人导论", "query": "这门课讲什么", "min_score": 0.0},
    {"course_id": "CS182", "query": "what is this course about", "min_score": 0.0},
    # Adversarial — must not crash
    {"course_id": None, "query": "?", "min_score": -1.0},  # may legitimately return nothing
    {"course_id": None, "query": "💀", "min_score": -1.0},
]


@pytest.fixture
def smoke_client(monkeypatch, tmp_path, fake_embed_fn):
    """Build a TestClient seeded with one chunk per smoke question's relevant
    course so search has *something* to find without requiring the real 15k-
    chunk index."""
    art = tmp_path / "artifacts"
    courses_dir = art / "courses"
    courses_dir.mkdir(parents=True)

    # One synthetic chunk per (course_id, query) that contains the query text.
    # This makes the smoke truly offline — no need for the real downloaded
    # corpus — while still proving the routing / filter / fusion pipeline.
    from nano_notebooklm.types import Chunk, FileType

    seed_chunks: dict[str, list[Chunk]] = {}
    seen = set()
    for i, q in enumerate(SMOKE_QUESTIONS):
        cid = q["course_id"] or "_globalmeta"
        if (cid, q["query"]) in seen:
            continue
        seen.add((cid, q["query"]))
        seed_chunks.setdefault(cid, []).append(Chunk(
            chunk_id=f"smoke-{i:03d}",
            doc_id=f"docsmoke{i:03d}",
            course_id=cid,
            text=f"{q['query']} 这是这门课关于 {q['query']} 的章节内容。"
                 f"course about {q['query']} discussion and overview.",
            file_type=FileType.PDF,
            source_file=f"smoke-{i}.pdf",
            location=f"Page {i+1}/30",
            page=i + 1,
        ))

    # Persist
    for cid, chunks in seed_chunks.items():
        cdir = courses_dir / cid
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "chunks.json").write_text(
            json.dumps([c.model_dump() for c in chunks], default=str)
        )
        (cdir / "course_meta.json").write_text(json.dumps(
            {"course_id": cid, "name": cid, "documents": [c.doc_id for c in chunks]}
        ))

    monkeypatch.setenv("ARTIFACTS_DIR", str(art))
    # Disable the score gate floor for smoke — the hash-based fake_embed_fn
    # produces RRF scores in the ~0.016-0.033 range which are below the
    # production-tuned 0.020 default; in real corpora top hits comfortably
    # exceed it. This matches the chat_client fixture in test_router_intent.
    monkeypatch.setenv("RAG_SCORE_GATE_TOP1", "0.0")
    from nano_notebooklm import config
    monkeypatch.setattr(config, "ARTIFACTS_DIR", art)
    from nano_notebooklm.kb import store as kb_store
    monkeypatch.setattr(kb_store, "_get_default_embed_fn", lambda: fake_embed_fn)

    import api.server as server_mod
    importlib.reload(server_mod)

    # Build a global index spanning all seeded courses (search() loads global
    # by default, then filters by course_id at query time)
    server_mod.kb.build_index(None)

    return TestClient(server_mod.app)


def test_smoke_search_hit_rate(smoke_client):
    """Every non-adversarial smoke query must return at least one result."""
    misses: list[tuple[str, str | None, str]] = []
    for q in SMOKE_QUESTIONS:
        if q["min_score"] < 0:  # adversarial — only assert non-crash
            r = smoke_client.post(
                "/api/search",
                json={"query": q["query"], "top_k": 5,
                      **({"course_id": q["course_id"]} if q["course_id"] else {})},
            )
            assert r.status_code in (200, 422), f"crashed on adversarial {q['query']!r}: {r.status_code}"
            continue

        r = smoke_client.post(
            "/api/search",
            json={"query": q["query"], "top_k": 5,
                  **({"course_id": q["course_id"]} if q["course_id"] else {})},
        )
        assert r.status_code == 200, f"{q['query']!r} → HTTP {r.status_code}"
        results = r.json()["results"]
        if not results:
            misses.append((q["query"], q["course_id"], "0 results"))

    assert not misses, (
        f"{len(misses)} smoke queries returned 0 results — RAG pipeline regressed:\n"
        + "\n".join(f"  - {q!r} (course={c}): {note}" for q, c, note in misses)
    )


def test_smoke_chat_no_boilerplate_with_default_files(smoke_client, monkeypatch):
    """Reproduces the All-Courses prefix bug: when frontend sends bracketed
    titles as checked_files, the backend filter must still hit (or, fixed
    correctly, the frontend should never send bracketed titles, but we still
    test that the qa_skill is robust to checked_files matching the chunks'
    raw source_file). This is the test that would have caught #R1."""
    from nano_notebooklm.types import LLMResponse

    # Stub the LLM so this test is offline + deterministic
    async def fake_complete(prompt, task_type="", system="", temperature=0.7, max_tokens=4096, max_retries=3):
        return LLMResponse(content="stubbed answer", model="fake", input_tokens=1, output_tokens=1, latency_ms=1.0)

    import api.server as server_mod
    monkeypatch.setattr(server_mod.router, "complete", fake_complete)

    # Find a real seeded chunk for 计算机组成原理 to use as a positive case
    target_idx = next(i for i, q in enumerate(SMOKE_QUESTIONS)
                      if q["course_id"] == "计算机组成原理" and q["query"] == "内存")
    matching_file = f"smoke-{target_idx}.pdf"

    # 1) Pass raw source_file → must return non-boilerplate answer
    r = smoke_client.post(
        "/api/chat",
        json={
            "question": "内存",
            "course_id": "计算机组成原理",
            "checked_files": [matching_file],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert "No relevant content found" not in body["answer"], (
        f"qa_skill returned boilerplate even though checked_files matched a real chunk:\n"
        f"file expected to match: {matching_file}\n"
        f"answer: {body['answer'][:200]}"
    )

    # 2) Pass non-matching checked_files → boilerplate is the correct response
    r2 = smoke_client.post(
        "/api/chat",
        json={
            "question": "内存",
            "course_id": "计算机组成原理",
            "checked_files": ["nope-this-file-doesnt-exist.pdf"],
        },
    )
    assert r2.status_code == 200
    assert "No relevant content found" in r2.json()["answer"], (
        "expected boilerplate when checked_files doesn't match anything"
    )
