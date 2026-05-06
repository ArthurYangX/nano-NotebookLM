"""Run the regression eval suite against a live nano-NOTEBOOKLM API.

Layer 2 (default): hits /api/search for each question — no LLM cost.
Layer 3 (--with-chat N): also hit /api/chat for N sampled questions.

Outputs:
  - artifacts/eval/report-<timestamp>.md  (human-readable)
  - artifacts/eval/results-<timestamp>.jsonl  (per-question raw)
  - exits non-zero if hit_rate < threshold (default 0.85) so this can gate CI

Run:
  python scripts/run_eval.py
  python scripts/run_eval.py --base-url http://localhost:8000 --with-chat 30
  python scripts/run_eval.py --hit-rate-threshold 0.9
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
EVAL_DIR = ROOT / "artifacts" / "eval"
NO_CONTENT_MARKER = "No relevant content found in the selected sources"


def load_questions(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def call_search(client: httpx.Client, base_url: str, q: dict) -> dict:
    """Hit /api/search for the question, return result + timing."""
    payload = {"query": q["question"], "top_k": 5}
    if q.get("course_id"):
        payload["course_id"] = q["course_id"]
    t0 = time.perf_counter()
    try:
        r = client.post(f"{base_url}/api/search", json=payload, timeout=30.0)
        elapsed = (time.perf_counter() - t0) * 1000
        return {
            "status_code": r.status_code,
            "results": r.json().get("results", []) if r.status_code == 200 else [],
            "latency_ms": elapsed,
            "error": None if r.status_code == 200 else r.text[:200],
        }
    except Exception as exc:
        return {
            "status_code": 0,
            "results": [],
            "latency_ms": (time.perf_counter() - t0) * 1000,
            "error": str(exc)[:200],
        }


def call_chat(client: httpx.Client, base_url: str, q: dict) -> dict:
    payload = {"question": q["question"], "top_k": 5}
    if q.get("course_id"):
        payload["course_id"] = q["course_id"]
    t0 = time.perf_counter()
    try:
        r = client.post(f"{base_url}/api/chat", json=payload, timeout=120.0)
        elapsed = (time.perf_counter() - t0) * 1000
        body = r.json() if r.status_code == 200 else {}
        return {
            "status_code": r.status_code,
            "answer": body.get("answer", ""),
            "sources": body.get("sources", []),
            "latency_ms": elapsed,
            "error": None if r.status_code == 200 else r.text[:200],
        }
    except Exception as exc:
        return {
            "status_code": 0,
            "answer": "",
            "sources": [],
            "latency_ms": (time.perf_counter() - t0) * 1000,
            "error": str(exc)[:200],
        }


def grade_search(q: dict, result: dict) -> tuple[str, str]:
    """Return (verdict, note). verdict in {hit, miss, error, skip-adversarial-422}."""
    if q["category"] == "adversarial":
        if q.get("expect") == "validation_error":
            if result["status_code"] == 422:
                return ("ok", "expected 422")
            return ("error", f"expected 422, got {result['status_code']}")
        # otherwise we expect either ok with results or ok with empty (graceful)
        if result["status_code"] in (200, 422):
            return ("ok", f"adversarial passed gracefully ({result['status_code']})")
        return ("error", f"adversarial crashed: {result['status_code']} {result['error']}")
    # Normal questions: must return at least one hit with score > 0
    if result["status_code"] != 200:
        return ("error", f"http {result['status_code']}: {result['error']}")
    if not result["results"]:
        return ("miss", "0 results")
    top_score = max(r.get("score", 0) for r in result["results"])
    if top_score <= 0:
        return ("miss", f"top score {top_score}")
    return ("hit", f"top score {top_score:.4f}")


def grade_chat(q: dict, result: dict) -> tuple[str, str]:
    if result["status_code"] != 200:
        return ("error", f"http {result['status_code']}: {result['error']}")
    answer = result.get("answer", "")
    if not answer:
        return ("error", "empty answer")
    if NO_CONTENT_MARKER in answer:
        return ("no-content", "boilerplate 'No relevant content found'")
    if not result.get("sources"):
        return ("no-sources", "answer present but sources empty")
    return ("ok", f"{len(result['sources'])} sources")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--questions", type=Path, default=EVAL_DIR / "questions.jsonl")
    ap.add_argument("--with-chat", type=int, default=0,
                    help="also run N sampled questions through /api/chat (LLM cost!)")
    ap.add_argument("--limit", type=int, default=0,
                    help="cap total search calls (0 = no cap)")
    ap.add_argument("--hit-rate-threshold", type=float, default=0.85,
                    help="exit non-zero if non-adversarial hit rate < this")
    ap.add_argument("--seed", type=int, default=17)
    args = ap.parse_args()

    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_path = EVAL_DIR / f"report-{ts}.md"
    results_path = EVAL_DIR / f"results-{ts}.jsonl"

    if not args.questions.exists():
        print(f"Question file missing: {args.questions}\nRun scripts/build_eval_questions.py first.")
        sys.exit(2)

    questions = load_questions(args.questions)
    if args.limit:
        questions = questions[:args.limit]

    random.seed(args.seed)

    # Verify server is up
    with httpx.Client() as client:
        try:
            h = client.get(f"{args.base_url}/api/health", timeout=5.0)
            assert h.status_code == 200
        except Exception as exc:
            print(f"API not reachable at {args.base_url}: {exc}")
            sys.exit(2)

        # ── Layer 2: search ──
        print(f"Running search eval on {len(questions)} questions ...")
        search_results: list[dict] = []
        for i, q in enumerate(questions, 1):
            r = call_search(client, args.base_url, q)
            verdict, note = grade_search(q, r)
            search_results.append({**q, "search": {**r, "verdict": verdict, "note": note}})
            if i % 50 == 0 or i == len(questions):
                hits = sum(1 for x in search_results if x["search"]["verdict"] == "hit")
                misses = sum(1 for x in search_results if x["search"]["verdict"] == "miss")
                errs = sum(1 for x in search_results if x["search"]["verdict"] == "error")
                print(f"  [{i}/{len(questions)}] hit={hits} miss={misses} err={errs}")

        # ── Layer 3: chat (sampled) ──
        chat_results: list[dict] = []
        if args.with_chat > 0:
            non_adv = [q for q in search_results if q["category"] != "adversarial"]
            sample = random.sample(non_adv, min(args.with_chat, len(non_adv)))
            print(f"Running chat eval on {len(sample)} sampled questions (LLM, slower) ...")
            for i, q in enumerate(sample, 1):
                r = call_chat(client, args.base_url, q)
                verdict, note = grade_chat(q, r)
                chat_results.append({"id": q["id"], "course_id": q.get("course_id"),
                                     "question": q["question"], "category": q["category"],
                                     "chat": {**r, "verdict": verdict, "note": note}})
                if i % 5 == 0 or i == len(sample):
                    print(f"  chat [{i}/{len(sample)}]")

    # ── Aggregate ──
    non_adv = [r for r in search_results if r["category"] != "adversarial"]
    adv = [r for r in search_results if r["category"] == "adversarial"]
    hit = sum(1 for r in non_adv if r["search"]["verdict"] == "hit")
    miss = sum(1 for r in non_adv if r["search"]["verdict"] == "miss")
    err = sum(1 for r in non_adv if r["search"]["verdict"] == "error")
    hit_rate = hit / len(non_adv) if non_adv else 0.0
    adv_ok = sum(1 for r in adv if r["search"]["verdict"] == "ok")

    latencies = [r["search"]["latency_ms"] for r in search_results
                 if r["search"]["latency_ms"] > 0 and r["search"]["status_code"] == 200]
    p50 = statistics.median(latencies) if latencies else 0
    p95 = statistics.quantiles(latencies, n=20)[18] if len(latencies) >= 20 else max(latencies, default=0)

    by_cat_miss = {}
    for r in non_adv:
        if r["search"]["verdict"] != "hit":
            by_cat_miss.setdefault(r["category"], []).append(r)
    by_course_hit = {}
    for r in non_adv:
        cid = r.get("course_id") or "_all_"
        by_course_hit.setdefault(cid, [0, 0])
        by_course_hit[cid][1] += 1
        if r["search"]["verdict"] == "hit":
            by_course_hit[cid][0] += 1

    chat_ok = sum(1 for r in chat_results if r["chat"]["verdict"] == "ok")
    chat_no_content = sum(1 for r in chat_results if r["chat"]["verdict"] == "no-content")
    chat_no_sources = sum(1 for r in chat_results if r["chat"]["verdict"] == "no-sources")
    chat_err = sum(1 for r in chat_results if r["chat"]["verdict"] == "error")

    # ── Write raw results ──
    with results_path.open("w", encoding="utf-8") as f:
        for r in search_results + chat_results:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")

    # ── Markdown report ──
    lines: list[str] = []
    lines.append(f"# nano-NOTEBOOKLM regression eval — {ts}")
    lines.append(f"\nbase_url: `{args.base_url}` · questions: `{args.questions.name}`\n")
    lines.append("## Search layer")
    lines.append(f"- total non-adversarial: **{len(non_adv)}**")
    lines.append(f"- hit: **{hit}** ({hit_rate*100:.1f}%)")
    lines.append(f"- miss (0 results): **{miss}**")
    lines.append(f"- error (HTTP / exception): **{err}**")
    lines.append(f"- adversarial passed gracefully: **{adv_ok}/{len(adv)}**")
    lines.append(f"- latency search p50 / p95: **{p50:.1f}ms / {p95:.1f}ms**")
    lines.append(f"\n### By course (hit / total)")
    for cid, (h, t) in sorted(by_course_hit.items()):
        lines.append(f"- `{cid}`: {h}/{t}  ({h/t*100:.1f}%)")
    if any(by_cat_miss.values()):
        lines.append("\n### Sample of misses & errors (up to 15)")
        flat = [r for cat_list in by_cat_miss.values() for r in cat_list]
        for r in flat[:15]:
            lines.append(
                f"- `{r['id']}` [{r['search']['verdict']}] "
                f"course={r.get('course_id')} q=\"{r['question'][:60]}\" "
                f"-> {r['search']['note']}"
            )
    if chat_results:
        lines.append("\n## Chat layer (LLM, sampled)")
        lines.append(f"- total: **{len(chat_results)}**")
        lines.append(f"- ok (answer + sources): **{chat_ok}**")
        lines.append(f"- 'No relevant content' boilerplate: **{chat_no_content}**")
        lines.append(f"- empty sources: **{chat_no_sources}**")
        lines.append(f"- error: **{chat_err}**")
        chat_lat = [r["chat"]["latency_ms"] for r in chat_results if r["chat"]["status_code"] == 200]
        if chat_lat:
            lines.append(f"- chat latency p50 / p95: **{statistics.median(chat_lat):.0f}ms / {(statistics.quantiles(chat_lat, n=20)[18] if len(chat_lat) >= 20 else max(chat_lat)):.0f}ms**")
        bad_chats = [r for r in chat_results if r["chat"]["verdict"] in ("no-content", "no-sources", "error")][:10]
        if bad_chats:
            lines.append("\n### Sample of failed chats (up to 10)")
            for r in bad_chats:
                lines.append(f"- `{r['id']}` [{r['chat']['verdict']}] q=\"{r['question'][:60]}\" -> {r['chat']['note']}")
    lines.append(f"\n---\nraw: `{results_path.name}`\n")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n=== Report ===")
    print(report_path.read_text(encoding="utf-8"))

    # Exit code
    if hit_rate < args.hit_rate_threshold:
        print(f"\nFAIL: hit rate {hit_rate*100:.1f}% < threshold {args.hit_rate_threshold*100:.0f}%")
        sys.exit(1)
    if chat_no_content > len(chat_results) * 0.1:
        print(f"\nFAIL: too many 'No relevant content' chat answers ({chat_no_content}/{len(chat_results)})")
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
