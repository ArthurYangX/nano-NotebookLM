"""Run benchmark across up to 4 routes:
  - gpt_bare   : router.complete() with no RAG context (general knowledge baseline)
  - gpt_ragkg  : /api/chat backend=codex (graphrag → RAG → translation → cross-course chain)
  - qwen_base  : /api/chat backend=qwen_base
  - qwen_raft  : /api/chat backend=qwen_raft

Streams to results.jsonl (one line per (q_id, route), append-only, resume-safe).

Usage:
  python benchmarks/run_benchmark.py                          # all 4 routes
  python benchmarks/run_benchmark.py gpt_bare gpt_ragkg       # GPT only
  BENCH_CONCURRENCY=8 python benchmarks/run_benchmark.py ...  # override sem cap
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from nano_notebooklm.ai.router import ModelRouter  # noqa: E402

QUESTIONS_PATH = ROOT / "benchmarks" / "questions_100.json"
RESULTS_PATH = ROOT / "benchmarks" / "results.jsonl"
API_URL = os.getenv("BENCH_API_URL", "http://localhost:8000")
COURSE_ID = os.getenv("BENCH_COURSE_ID", "NLP")
CONCURRENCY = int(os.getenv("BENCH_CONCURRENCY", "16"))
REQUEST_TIMEOUT = float(os.getenv("BENCH_TIMEOUT", "300"))

ROUTES = ("gpt_bare", "gpt_ragkg", "qwen_base", "qwen_raft")

GPT_BARE_SYSTEM = (
    "你是一个 NLP 教学助手，正在回答一道课程考试题。根据你已有的通用知识作答，"
    "不需要也不应当声称引用任何外部资料。请用中文回答。\n"
    "- 概念题：直接给出定义/结论，必要时分点说明。\n"
    "- 公式题：写出完整公式（LaTeX 或纯文本均可），并解释符号含义。\n"
    "- 计算题：列出关键步骤再给最终数值结果。\n"
    "回答保持简洁，不要重复题目。"
)


def load_done() -> set[tuple[str, str]]:
    done: set[tuple[str, str]] = set()
    if not RESULTS_PATH.exists():
        return done
    for line in RESULTS_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("error"):
            continue
        done.add((rec["q_id"], rec["route"]))
    return done


_router_singleton: ModelRouter | None = None


def get_router() -> ModelRouter:
    global _router_singleton
    if _router_singleton is None:
        _router_singleton = ModelRouter()
    return _router_singleton


async def call_gpt_bare(q: dict) -> dict:
    router = get_router()
    t0 = time.monotonic()
    resp = await router.complete(
        q["q"],
        task_type="",
        system=GPT_BARE_SYSTEM,
        temperature=0.3,
        max_tokens=2048,
    )
    return {
        "answer": getattr(resp, "content", str(resp)) or "",
        "sources": [],
        "path": "bare",
        "latency_ms": round((time.monotonic() - t0) * 1000),
    }


async def call_chat(client: httpx.AsyncClient, q: dict, backend: str | None) -> dict:
    t0 = time.monotonic()
    payload: dict = {
        "question": q["q"],
        "course_id": COURSE_ID,
        "user_lang": "zh",
        "top_k": 8,
    }
    if backend is not None:
        payload["backend"] = backend
    r = await client.post(f"{API_URL}/api/chat", json=payload, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return {
        "answer": data.get("answer", ""),
        "sources": data.get("sources") or [],
        "path": data.get("path"),
        "backend_fallback": data.get("backend_fallback"),
        "latency_ms": round((time.monotonic() - t0) * 1000),
    }


async def run_one(client: httpx.AsyncClient, q: dict, route: str, sem: asyncio.Semaphore) -> dict:
    async with sem:
        try:
            if route == "gpt_bare":
                res = await call_gpt_bare(q)
            elif route == "gpt_ragkg":
                res = await call_chat(client, q, "codex")
            elif route == "qwen_base":
                res = await call_chat(client, q, "qwen_base")
            elif route == "qwen_raft":
                res = await call_chat(client, q, "qwen_raft")
            else:
                raise ValueError(f"unknown route {route}")
            res["error"] = None
        except Exception as exc:
            res = {
                "answer": "",
                "sources": [],
                "path": None,
                "latency_ms": 0,
                "error": f"{type(exc).__name__}: {exc}",
            }
        res.update({"q_id": q["id"], "qid": q["qid"], "route": route, "ts": time.time()})
        return res


async def main() -> None:
    payload = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
    questions = payload["questions"]
    limit = int(os.getenv("BENCH_LIMIT", "0"))
    if limit > 0:
        questions = questions[:limit]
        print(f"[bench] BENCH_LIMIT={limit} → running first {limit} questions only")

    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    routes = tuple(args) if args else ROUTES
    for r in routes:
        if r not in ROUTES:
            sys.exit(f"unknown route {r}; valid={ROUTES}")

    done = load_done()
    todo: list[tuple[dict, str]] = []
    for q in questions:
        for r in routes:
            if (q["id"], r) not in done:
                todo.append((q, r))
    total = len(questions) * len(routes)
    print(
        f"[bench] routes={list(routes)} q={len(questions)} total={total} "
        f"done_skipped={total - len(todo)} todo={len(todo)} concurrency={CONCURRENCY}"
    )
    if not todo:
        print("[bench] nothing to do")
        return

    sem = asyncio.Semaphore(CONCURRENCY)
    out_f = RESULTS_PATH.open("a", encoding="utf-8")
    try:
        async with httpx.AsyncClient() as client:
            tasks = [
                asyncio.create_task(run_one(client, q, r, sem))
                for q, r in todo
            ]
            completed = 0
            errors = 0
            for fut in asyncio.as_completed(tasks):
                rec = await fut
                out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                out_f.flush()
                completed += 1
                if rec.get("error"):
                    errors += 1
                err = rec.get("error")
                short_err = (err[:80] + "…") if err and len(err) > 80 else err
                print(
                    f"[bench] {completed}/{len(todo)} q={rec['q_id']} route={rec['route']:<10} "
                    f"latency={rec['latency_ms']:>6}ms path={rec.get('path')} err={short_err}"
                )
    finally:
        out_f.close()
    print(f"[bench] done. completed={completed} errors={errors} results={RESULTS_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
