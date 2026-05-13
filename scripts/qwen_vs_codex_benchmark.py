"""Codex (GPT-5.4) vs Qwen-RAFT side-by-side benchmark on test-slides course.

Pipeline:
    1. Load questions.json
    2. For each question, call /api/chat twice (backend=codex, backend=qwen_raft)
       sequentially per backend (avoid hot-path contention) but parallelism across
       questions is OFF — we want clean per-call timing.
    3. Persist raw runs to artifacts/benchmark/runs.json
    4. Have GPT-5.4 grade each (question, codex_answer, qwen_answer) tuple on
       accuracy + completeness; persist judgements.
    5. Write a markdown report.

Notes:
- All requests go through /api/chat which already does router_intent + RAG; we
  just toggle the backend kwarg per the R4-5 contract.
- We don't measure first-token latency (would need streaming) — total wall-clock
  is what matters for chat UX comparison.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from pathlib import Path

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("bench")

ROOT = Path(__file__).resolve().parent.parent
QUESTIONS = ROOT / "artifacts/benchmark/questions.json"
RUNS = ROOT / "artifacts/benchmark/runs.json"
JUDGEMENTS = ROOT / "artifacts/benchmark/judgements.json"
REPORT = ROOT / "artifacts/benchmark/REPORT.md"
# Also mirror the report to a committable, non-gitignored path so the
# permanent record lives in the repo (artifacts/ is gitignored).
REPORT_MIRROR = ROOT / "benchmarks/qwen_vs_codex_REPORT.md"
API = "http://127.0.0.1:8000"
CHAT_TIMEOUT = 180.0  # seconds per backend call (qwen on 4-bit can be slow)


# ── Step 1+2: ask both backends for every question ─────────────────


async def ask_once(client: httpx.AsyncClient, question: str, course: str,
                   backend: str) -> dict:
    """Single /api/chat call. Returns {answer, path, backend_fallback,
    duration_s, error}."""
    payload = {
        "question": question,
        "course_id": course,
        "backend": backend,
    }
    start = time.monotonic()
    try:
        r = await client.post(f"{API}/api/chat", json=payload,
                              timeout=CHAT_TIMEOUT)
        dur = time.monotonic() - start
        if r.status_code != 200:
            return {
                "backend": backend,
                "answer": "",
                "duration_s": dur,
                "error": f"http {r.status_code}: {r.text[:200]}",
            }
        body = r.json()
        return {
            "backend": backend,
            "answer": body.get("answer") or "",
            "path": body.get("path"),
            "backend_fallback": body.get("backend_fallback"),
            "sources": body.get("sources") or [],
            "duration_s": dur,
            "error": None,
        }
    except Exception as exc:
        return {
            "backend": backend,
            "answer": "",
            "duration_s": time.monotonic() - start,
            "error": f"{type(exc).__name__}: {exc}",
        }


async def run_benchmark(question_bank: dict) -> list[dict]:
    """Sequential per-question, both backends. Returns list of run records."""
    course = question_bank["course_id"]
    qs = question_bank["questions"]
    runs: list[dict] = []
    async with httpx.AsyncClient() as client:
        for i, q in enumerate(qs):
            qid, qtext = q["id"], q["q"]
            log.info("[%d/%d] %s: %s", i + 1, len(qs), qid, qtext[:60])
            # codex first
            codex = await ask_once(client, qtext, course, "codex")
            log.info("    codex  %.2fs  path=%s  fallback=%s  len=%d",
                     codex["duration_s"], codex.get("path"),
                     codex.get("backend_fallback"), len(codex["answer"]))
            # then qwen
            qwen = await ask_once(client, qtext, course, "qwen_raft")
            log.info("    qwen   %.2fs  path=%s  fallback=%s  len=%d",
                     qwen["duration_s"], qwen.get("path"),
                     qwen.get("backend_fallback"), len(qwen["answer"]))
            runs.append({
                "id": qid,
                "type": q.get("type"),
                "chapter": q.get("chapter"),
                "question": qtext,
                "codex": codex,
                "qwen": qwen,
            })
            # Persist incrementally so a mid-run crash doesn't lose data.
            RUNS.write_text(json.dumps(runs, ensure_ascii=False, indent=2))
    return runs


# ── Step 3: LLM-as-judge grading ───────────────────────────────────


JUDGE_PROMPT = """你是一个严格的 NLP 课程助教，正在批改两个 AI 助手对同一道题的回答。

题目：
{question}

回答 A（codex GPT-5.4）：
{answer_a}

回答 B（Qwen2.5-7B-RAFT 微调）：
{answer_b}

请基于 NLP/机器学习/深度学习的事实正确性，分别给两个回答打分：

- accuracy（事实正确性）：0-5 分。5=完全正确无误，4=主要正确小瑕疵，3=部分正确，2=主要错误，1=几乎全错，0=完全错误或拒答
- completeness（完整度）：0-5 分。5=覆盖所有关键点，4=覆盖大部分，3=覆盖一半，2=覆盖少许，1=几乎没覆盖，0=空白或离题

输出**严格的 JSON**（不要任何额外解释、markdown 代码块、前后文字），格式：
{{
  "a": {{"accuracy": <int>, "completeness": <int>, "notes": "<一句话评语>"}},
  "b": {{"accuracy": <int>, "completeness": <int>, "notes": "<一句话评语>"}},
  "winner": "a" | "b" | "tie",
  "case_note": "<一句话总结这道题的差异>"
}}
"""


def judge_one(router, q: dict) -> dict:
    """Call GPT-5.4 directly (bypass /api/chat) to grade."""
    prompt = JUDGE_PROMPT.format(
        question=q["question"],
        answer_a=q["codex"]["answer"] or "(空)",
        answer_b=q["qwen"]["answer"] or "(空)",
    )
    try:
        out = asyncio.run(router.complete_structured(
            prompt,
            task_type="qa_general",
            temperature=0.0,
            max_tokens=512,
        ))
        if "error" in out and "raw" in out:
            # Best-effort: try to extract embedded JSON.
            return {"_raw_error": out["error"], "_raw": out["raw"][:500]}
        return out
    except Exception as exc:
        return {"_judge_error": f"{type(exc).__name__}: {exc}"}


def grade_all(runs: list[dict]) -> list[dict]:
    """Build a router instance and grade every run."""
    # Lazy import so the bench script can be partially used without LLM deps.
    import sys
    sys.path.insert(0, str(ROOT))
    from nano_notebooklm.ai.router import ModelRouter
    router = ModelRouter()
    if "openai" not in router.backends:
        raise RuntimeError("OpenAI/codex backend not configured — cannot run judge")
    out: list[dict] = []
    for i, run in enumerate(runs):
        log.info("[judge %d/%d] %s", i + 1, len(runs), run["id"])
        j = judge_one(router, run)
        out.append({"id": run["id"], "judgement": j})
        JUDGEMENTS.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    return out


# ── Step 4: markdown report ────────────────────────────────────────


def _safe(d: dict, *keys, default=None):
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k, default)
    return d


def write_report(runs: list[dict], judgements: list[dict]) -> None:
    j_by_id = {j["id"]: j["judgement"] for j in judgements}

    # ── Aggregates ──
    n = len(runs)
    codex_times = [r["codex"]["duration_s"] for r in runs]
    qwen_times = [r["qwen"]["duration_s"] for r in runs]
    codex_lens = [len(r["codex"]["answer"]) for r in runs]
    qwen_lens = [len(r["qwen"]["answer"]) for r in runs]

    qwen_fallback_count = sum(1 for r in runs if r["qwen"].get("backend_fallback"))
    codex_errors = sum(1 for r in runs if r["codex"].get("error"))
    qwen_errors = sum(1 for r in runs if r["qwen"].get("error"))

    def avg(xs): return sum(xs) / len(xs) if xs else 0.0
    def med(xs):
        if not xs: return 0.0
        s = sorted(xs); m = len(s) // 2
        return s[m] if len(s) % 2 else (s[m-1] + s[m]) / 2

    # Accuracy / completeness from judge
    codex_acc, qwen_acc, codex_comp, qwen_comp, winners = [], [], [], [], []
    for r in runs:
        j = j_by_id.get(r["id"]) or {}
        ca = _safe(j, "a", "accuracy")
        qa = _safe(j, "b", "accuracy")
        cc = _safe(j, "a", "completeness")
        qc = _safe(j, "b", "completeness")
        if isinstance(ca, int): codex_acc.append(ca)
        if isinstance(qa, int): qwen_acc.append(qa)
        if isinstance(cc, int): codex_comp.append(cc)
        if isinstance(qc, int): qwen_comp.append(qc)
        w = j.get("winner")
        if w in ("a", "b", "tie"): winners.append(w)

    win_a = winners.count("a"); win_b = winners.count("b"); win_tie = winners.count("tie")

    lines: list[str] = []
    lines.append("# Codex (GPT-5.4) vs Qwen2.5-7B-RAFT 对比报告\n")
    lines.append(f"**题库**: 30 题，覆盖 ch1(intro) / ch3(HMM) / ch4(classical ML) / ch4-2(neural) / ch9(LM)\n")
    lines.append(f"**课程**: `test-slides`（326 chunks, 220 KG nodes）\n")
    lines.append(f"**Qwen 后端**: AutoDL RTX 5090 + serve_openai.py + SSH tunnel\n")
    lines.append(f"**Codex 后端**: codex.ysaikeji.cn/v1 (GPT-5.4)\n")
    lines.append(f"**评分**: GPT-5.4 当 LLM-as-judge，每题给两个回答打 accuracy(0-5) + completeness(0-5)\n\n")

    lines.append("## 总览\n")
    lines.append("| 指标 | codex (GPT-5.4) | Qwen2.5-7B-RAFT |\n")
    lines.append("|---|---|---|\n")
    lines.append(f"| 平均响应时长 | {avg(codex_times):.2f}s | {avg(qwen_times):.2f}s |\n")
    lines.append(f"| 中位响应时长 | {med(codex_times):.2f}s | {med(qwen_times):.2f}s |\n")
    lines.append(f"| 最长响应时长 | {max(codex_times):.2f}s | {max(qwen_times):.2f}s |\n")
    lines.append(f"| 最短响应时长 | {min(codex_times):.2f}s | {min(qwen_times):.2f}s |\n")
    lines.append(f"| 平均答案字符数 | {avg(codex_lens):.0f} | {avg(qwen_lens):.0f} |\n")
    lines.append(f"| 失败/错误次数 | {codex_errors} | {qwen_errors} |\n")
    lines.append(f"| backend_fallback 触发数 | — | {qwen_fallback_count} (qwen→codex 降级) |\n")
    lines.append(f"| 平均 accuracy 分 (0-5) | {avg(codex_acc):.2f} | {avg(qwen_acc):.2f} |\n")
    lines.append(f"| 平均 completeness 分 (0-5) | {avg(codex_comp):.2f} | {avg(qwen_comp):.2f} |\n\n")

    lines.append("## Head-to-head 判决\n")
    if winners:
        lines.append(f"- **codex 胜**: {win_a} 题（{100*win_a/len(winners):.0f}%）\n")
        lines.append(f"- **qwen 胜**: {win_b} 题（{100*win_b/len(winners):.0f}%）\n")
        lines.append(f"- **平局**: {win_tie} 题（{100*win_tie/len(winners):.0f}%）\n\n")
    else:
        lines.append("- 评判数据缺失\n\n")

    # Per-question table
    lines.append("## 逐题打分表\n")
    lines.append("| ID | 类型 | 章节 | codex(s) | qwen(s) | codex acc | qwen acc | codex comp | qwen comp | winner |\n")
    lines.append("|---|---|---|---|---|---|---|---|---|---|\n")
    for r in runs:
        j = j_by_id.get(r["id"]) or {}
        ca = _safe(j, "a", "accuracy", default="?")
        qa = _safe(j, "b", "accuracy", default="?")
        cc = _safe(j, "a", "completeness", default="?")
        qc = _safe(j, "b", "completeness", default="?")
        w = j.get("winner", "?")
        lines.append(
            f"| {r['id']} | {r.get('type','?')} | {r.get('chapter','?')} | "
            f"{r['codex']['duration_s']:.2f} | {r['qwen']['duration_s']:.2f} | "
            f"{ca} | {qa} | {cc} | {qc} | {w} |\n"
        )
    lines.append("\n")

    # Selected case details: pick 6 — best codex win, best qwen win, biggest tie,
    # plus 3 most interesting differences.
    def case_score(r):
        j = j_by_id.get(r["id"]) or {}
        ca = _safe(j, "a", "accuracy", default=0) or 0
        qa = _safe(j, "b", "accuracy", default=0) or 0
        return abs(ca - qa)

    sorted_runs = sorted(runs, key=case_score, reverse=True)
    selected = sorted_runs[:6]

    lines.append("## 详细案例对比（6 个最有代表性的题）\n\n")
    for r in selected:
        j = j_by_id.get(r["id"]) or {}
        ca = _safe(j, "a", "accuracy", default="?")
        qa = _safe(j, "b", "accuracy", default="?")
        cc = _safe(j, "a", "completeness", default="?")
        qc = _safe(j, "b", "completeness", default="?")
        w = j.get("winner", "?")
        case_note = j.get("case_note") or ""
        lines.append(f"### {r['id']} · {r.get('type','?')} · {r.get('chapter','?')}\n")
        lines.append(f"**题目**: {r['question']}\n\n")
        lines.append(f"**裁判**: winner={w}, accuracy(codex/qwen)={ca}/{qa}, completeness(codex/qwen)={cc}/{qc}\n")
        if case_note:
            lines.append(f"**评语**: {case_note}\n")
        lines.append("\n")
        lines.append(f"<details><summary>codex 回答 ({r['codex']['duration_s']:.1f}s, {len(r['codex']['answer'])} chars)</summary>\n\n")
        lines.append("```\n")
        lines.append(r["codex"]["answer"] or "(空)")
        lines.append("\n```\n\n</details>\n\n")
        lines.append(f"<details><summary>qwen 回答 ({r['qwen']['duration_s']:.1f}s, {len(r['qwen']['answer'])} chars)</summary>\n\n")
        lines.append("```\n")
        lines.append(r["qwen"]["answer"] or "(空)")
        lines.append("\n```\n\n</details>\n\n")
        ja_notes = _safe(j, "a", "notes") or ""
        jb_notes = _safe(j, "b", "notes") or ""
        if ja_notes or jb_notes:
            lines.append(f"**judge 对 codex**: {ja_notes}\n\n")
            lines.append(f"**judge 对 qwen**: {jb_notes}\n\n")
        lines.append("---\n\n")

    body = "".join(lines)
    REPORT.write_text(body)
    REPORT_MIRROR.parent.mkdir(parents=True, exist_ok=True)
    REPORT_MIRROR.write_text(body)
    log.info("Report written to %s (mirrored to %s)", REPORT, REPORT_MIRROR)


# ── Entry ──────────────────────────────────────────────────────────


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-run", action="store_true",
                    help="Skip step 1+2 (use existing runs.json)")
    ap.add_argument("--skip-judge", action="store_true",
                    help="Skip step 3 (use existing judgements.json)")
    args = ap.parse_args()

    question_bank = json.loads(QUESTIONS.read_text(encoding="utf-8"))

    if args.skip_run and RUNS.exists():
        runs = json.loads(RUNS.read_text(encoding="utf-8"))
        log.info("Loaded %d cached runs", len(runs))
    else:
        runs = asyncio.run(run_benchmark(question_bank))

    if args.skip_judge and JUDGEMENTS.exists():
        judgements = json.loads(JUDGEMENTS.read_text(encoding="utf-8"))
        log.info("Loaded %d cached judgements", len(judgements))
    else:
        judgements = grade_all(runs)

    write_report(runs, judgements)
    print(f"\nDONE. Report: {REPORT}")


if __name__ == "__main__":
    main()
