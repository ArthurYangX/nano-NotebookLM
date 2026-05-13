"""LLM-as-judge over benchmarks/results.jsonl using GPT-5.5.

For each (question, route_answer) pair, GPT-5.5 returns four 0-5 scores:
  - accuracy      : key facts / formulas / numerics correct
  - groundedness  : claims supported by sources (or absence-of-hallucination if bare)
  - completeness  : coverage of the reference answer's scoring points
  - conciseness   : free of CoT preamble / repetition / verbose padding

Streams to benchmarks/judged.jsonl (resume-safe).

Usage:
  python benchmarks/judge.py                # judge every (q_id, route) in results.jsonl
  python benchmarks/judge.py gpt_bare       # judge a single route subset
  JUDGE_CONCURRENCY=4 python benchmarks/judge.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from nano_notebooklm.ai.router import ModelRouter  # noqa: E402

QUESTIONS_PATH = ROOT / "benchmarks" / "questions_100.json"
RESULTS_PATH = ROOT / "benchmarks" / "results.jsonl"
JUDGED_PATH = ROOT / "benchmarks" / "judged.jsonl"

CONCURRENCY = int(os.getenv("JUDGE_CONCURRENCY", "8"))
REQUEST_TIMEOUT = float(os.getenv("JUDGE_TIMEOUT", "180"))

JUDGE_SYSTEM = """你是一位严谨的 NLP 课程评分员，正在评估学生（或大模型）对一道考试题的回答质量。
你将看到：题目、参考答案 / 评分点、被评估回答、被评估回答附带的引用列表（可能为空）。
请独立给出四个维度的 0-5 整数分（5=完美，0=完全错误），并写一句简短中文 comment。

**严格按以下 JSON 格式返回，不要任何额外文字或 markdown 包裹**：
{"accuracy": <0-5>, "groundedness": <0-5>, "completeness": <0-5>, "conciseness": <0-5>, "comment": "<≤80 字>"}

评分细则：

**accuracy（事实正确性）**：
- 5：所有关键事实 / 公式 / 数值与参考答案一致或等价
- 4：核心正确，次要细节有小偏差
- 3：核心方向对，但有 1-2 处实质错误
- 2：方向部分对，关键事实错
- 1：大部分错，但有零星正确
- 0：完全错或答非所问

**groundedness（基于课件 / 反幻觉）**：
- 5：cite 了真实存在的课件页码（[Source: ...]）且支撑论断；或明确说"基于通用知识"且无杜撰
- 4：cite 大部分支撑论断，少量未对齐
- 3：部分 cite 正确，存在 1 处过度推断
- 2：cite 与论断不符，或多处看起来杜撰但接近真相
- 1：明显幻觉但偶有正确
- 0：通篇杜撰 / 编造不存在的 source
- **对 sources 为空的回答**：评估"是否过度自信宣称引用不存在的内容"；若答案合理且不假装 cite → 给 4-5；若编造细节如"根据某某第 N 页"且不存在 → 扣分。

**completeness（评分点覆盖）**：
- 5：参考答案的关键点全部覆盖
- 4：覆盖大部分关键点，缺 1 个次要点
- 3：覆盖一半关键点
- 2：只覆盖少量
- 1：基本未覆盖
- 0：完全未涉及

**conciseness（简洁性 / 反 verbose）**：
- 5：直接给答案，无废话
- 4：偶有一句过渡，整体紧凑
- 3：有 CoT preamble 或一定冗余但答案完整
- 2：明显啰嗦，包含"先分析问题要点 / 引用原文关键内容 / 给出最终结论"式套话
- 1：大量套话且重复内容
- 0：套话 >> 实质内容

注意：comment 要简短指出最主要的扣分点或亮点，不要复述题目。"""


JUDGE_USER_TEMPLATE = """【题目（{chapter} / {type}）】
{question}

【参考答案 / 评分点】
{ref_answer}

【被评估回答】
{answer}

【被评估回答附带的引用源】
{sources_block}

请按系统指令以 JSON 返回评分。"""


def format_sources(sources: list) -> str:
    if not sources:
        return "（无引用）"
    lines = []
    for i, s in enumerate(sources[:8], 1):
        if isinstance(s, dict):
            sf = s.get("source_file", "?")
            loc = s.get("location", "?")
            score = s.get("score")
            lines.append(f"{i}. {sf} (loc={loc}, score={score})")
        else:
            lines.append(f"{i}. {s}")
    return "\n".join(lines)


def parse_judge_output(raw: str) -> dict:
    """Extract the JSON object from the model's response, robust to fence wrappers."""
    s = raw.strip()
    # try direct first
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # strip markdown fences
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # last resort: find first {...} block
    m = re.search(r"\{.*\}", s, re.S)
    if m:
        return json.loads(m.group(0))
    raise ValueError(f"could not parse judge output: {raw[:200]}")


_router: ModelRouter | None = None


def get_router() -> ModelRouter:
    global _router
    if _router is None:
        _router = ModelRouter()
    return _router


def load_done() -> set[tuple[str, str]]:
    done: set[tuple[str, str]] = set()
    if not JUDGED_PATH.exists():
        return done
    for line in JUDGED_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("judge_error"):
            continue
        done.add((rec["q_id"], rec["route"]))
    return done


def load_questions() -> dict[str, dict]:
    payload = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
    return {q["id"]: q for q in payload["questions"]}


def load_results(route_filter: set[str] | None) -> list[dict]:
    """Return latest non-errored row per (q_id, route)."""
    latest: dict[tuple[str, str], dict] = {}
    for line in RESULTS_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("error"):
            continue
        if route_filter and r["route"] not in route_filter:
            continue
        latest[(r["q_id"], r["route"])] = r
    return list(latest.values())


async def judge_one(item: dict, q: dict, sem: asyncio.Semaphore) -> dict:
    async with sem:
        prompt = JUDGE_USER_TEMPLATE.format(
            chapter=q["chapter"],
            type=q["type"],
            question=q["q"],
            ref_answer=q["ref_answer"],
            answer=item["answer"] or "(空)",
            sources_block=format_sources(item.get("sources", [])),
        )
        t0 = time.monotonic()
        try:
            resp = await asyncio.wait_for(
                get_router().complete(prompt, system=JUDGE_SYSTEM, temperature=0.1, max_tokens=512),
                timeout=REQUEST_TIMEOUT,
            )
            raw = getattr(resp, "content", "") or ""
            parsed = parse_judge_output(raw)
            for k in ("accuracy", "groundedness", "completeness", "conciseness"):
                v = parsed.get(k)
                if not isinstance(v, int) or not (0 <= v <= 5):
                    raise ValueError(f"bad score for {k}: {v!r}")
            parsed["comment"] = str(parsed.get("comment", ""))[:160]
            err = None
        except Exception as exc:
            parsed = {
                "accuracy": None, "groundedness": None,
                "completeness": None, "conciseness": None,
                "comment": "",
            }
            err = f"{type(exc).__name__}: {exc}"
        return {
            "q_id": item["q_id"],
            "qid": item.get("qid"),
            "route": item["route"],
            "chapter": q["chapter"],
            "type": q["type"],
            **parsed,
            "judge_latency_ms": round((time.monotonic() - t0) * 1000),
            "judge_error": err,
            "ts": time.time(),
        }


async def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    route_filter: set[str] | None = set(args) if args else None

    questions = load_questions()
    results = load_results(route_filter)
    if not results:
        sys.exit("[judge] no non-errored rows in results.jsonl matching filter")

    done = load_done()
    todo = [r for r in results if (r["q_id"], r["route"]) not in done]
    print(
        f"[judge] route_filter={route_filter or 'all'} candidates={len(results)} "
        f"already_judged={len(results) - len(todo)} todo={len(todo)} concurrency={CONCURRENCY}"
    )
    if not todo:
        return

    sem = asyncio.Semaphore(CONCURRENCY)
    out_f = JUDGED_PATH.open("a", encoding="utf-8")
    try:
        tasks = [asyncio.create_task(judge_one(r, questions[r["q_id"]], sem)) for r in todo]
        completed = 0
        errs = 0
        for fut in asyncio.as_completed(tasks):
            rec = await fut
            out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out_f.flush()
            completed += 1
            if rec.get("judge_error"):
                errs += 1
            scores = "/".join(
                str(rec.get(k, "?")) for k in ("accuracy", "groundedness", "completeness", "conciseness")
            )
            print(
                f"[judge] {completed}/{len(todo)} q={rec['q_id']} route={rec['route']:<10} "
                f"{scores} latency={rec['judge_latency_ms']}ms "
                f"err={(rec.get('judge_error') or '')[:60]}"
            )
    finally:
        out_f.close()
    print(f"[judge] done. completed={completed} errors={errs} → {JUDGED_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
