"""qwen-only output-quality monitor.

Runs the 30 test-slides questions through /api/chat with backend=qwen_raft
and writes a markdown report flagging:

  - RAFT preamble residue (Analyze key points / Final conclusion / ##begin_quote##
    leaking through the strip)
  - Off-topic answers (heuristic: <2 keyword overlap between question and answer)
  - Low-confidence note prepended (`本次检索置信度较低`)
  - Path used (graphrag / rag / cross-course / general / translated)
  - Top retrieval source(s)

Output: benchmarks/qwen_monitor_<UTC>.md (committable) + same content into
artifacts/benchmark/qwen_monitor_latest.json (raw, gitignored).

Usage:
    python scripts/qwen_monitor.py             # 串行跑 (~15min)
    python scripts/qwen_monitor.py --concurrency 4   # 4 题并发
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("qwen-monitor")

ROOT = Path(__file__).resolve().parent.parent
QUESTIONS = ROOT / "benchmarks/qwen_vs_codex_questions.json"
API = "http://127.0.0.1:8000"
CHAT_TIMEOUT = 120.0

# Residue patterns that should NOT appear in qwen output after strip.
# If any of these match, the strip regex missed a variant.
RAFT_RESIDUE = re.compile(
    r"\b(?:Analyze\s+key\s+points|Key\s+points?\s+(?:to\s+analyze|analysis)|"
    r"Final\s+conclusion|Quote\s+evidence|Evidence\s+from\s+(?:the\s+)?"
    r"(?:text|document|passage)|##\s*(?:begin|end)_quote\s*##)\s*[:：]?",
    flags=re.IGNORECASE,
)


async def ask(client: httpx.AsyncClient, q: dict, course: str,
              backend: str = "qwen_raft") -> dict:
    """Call /api/chat once with the given backend. Default is qwen_raft for
    the historical RAFT-output monitoring use case; pass backend='codex' to
    monitor the primary production path instead."""
    start = time.monotonic()
    body = {
        "question": q["q"],
        "course_id": course,
        "top_k": 5,
    }
    # Only set backend when explicitly choosing qwen — leaving it absent
    # lets server-side default routing kick in (which is what real chat
    # traffic looks like for the codex path).
    if backend and backend != "default":
        body["backend"] = backend
    try:
        resp = await client.post(f"{API}/api/chat", json=body, timeout=CHAT_TIMEOUT)
        elapsed_ms = (time.monotonic() - start) * 1000
        if resp.status_code != 200:
            return {
                "id": q["id"], "q": q["q"], "chapter": q.get("chapter"),
                "type": q.get("type"), "elapsed_ms": elapsed_ms,
                "error": f"HTTP {resp.status_code}", "body": resp.text[:500],
            }
        data = resp.json()
        return {
            "id": q["id"], "q": q["q"], "chapter": q.get("chapter"),
            "type": q.get("type"), "elapsed_ms": elapsed_ms,
            "answer": data.get("answer", ""),
            "path": data.get("path"),
            "backend_fallback": data.get("backend_fallback", False),
            "sources": [
                {
                    "file": s.get("source_file"),
                    "loc": s.get("location"),
                    "score": s.get("score"),
                }
                for s in (data.get("sources") or [])[:3]
            ],
            "model": data.get("model"),
        }
    except Exception as e:  # noqa: BLE001
        return {
            "id": q["id"], "q": q["q"], "chapter": q.get("chapter"),
            "type": q.get("type"),
            "elapsed_ms": (time.monotonic() - start) * 1000,
            "error": type(e).__name__, "body": str(e)[:200],
        }


def analyze(run: dict) -> dict:
    """Score a single run with heuristic flags."""
    flags = []
    if "error" in run:
        flags.append(f"ERROR:{run['error']}")
        return {**run, "flags": flags}
    answer = run.get("answer", "") or ""
    if not answer.strip():
        flags.append("EMPTY")
    if RAFT_RESIDUE.search(answer):
        flags.append("RAFT_RESIDUE")
    if "##begin_quote##" in answer or "##end_quote##" in answer:
        flags.append("RAW_QUOTE_MARKERS")
    if "本次检索置信度较低" in answer or "Low retrieval confidence" in answer:
        flags.append("LOW_CONFIDENCE")
    if run.get("backend_fallback"):
        flags.append("BACKEND_FALLBACK_TO_CODEX")
    # Off-topic heuristic: split question into 2-grams (CJK) / words
    # (ASCII), check how many appear as substrings of answer. CJK
    # tokens get split into bigrams so "马尔科夫模型" → {"马尔","尔科",
    # "科夫","夫模","模型"} catches partial overlap even when the
    # answer's surrounding context segments differently.
    #
    # 2026-05-13: also collect ASCII tokens from CJK questions so a
    # cross-language answer (qwen sometimes answers a Chinese question
    # in English while preserving the technical term, e.g. "CNN", "RNN",
    # "Transformer") still counts as on-topic. Without this, q12-style
    # cases mis-flag.
    def _content_keys(s: str) -> set[str]:
        keys: set[str] = set()
        for run_match in re.finditer(r"[一-鿿]+", s):
            chunk = run_match.group(0)
            for i in range(len(chunk) - 1):
                keys.add(chunk[i:i + 2])
        # ASCII words >= 2 chars catches CS acronyms like "CNN", "RNN",
        # "ID3" while still skipping single letters that would add noise.
        for word in re.findall(r"[A-Za-z]{2,}", s):
            keys.add(word.lower())
        return keys

    q_keys = _content_keys(run["q"])
    a_keys = _content_keys(answer)
    overlap = len(q_keys & a_keys)
    if q_keys and overlap == 0:
        flags.append("OFF_TOPIC_NO_OVERLAP")
    elif q_keys and overlap < 2 and len(q_keys) >= 4:
        flags.append(f"WEAK_OVERLAP({overlap}/{len(q_keys)})")
    # Refused-to-answer detection (LOW_CONFIDENCE prompt working)
    refusal_markers = (
        "未直接覆盖", "未直接回答", "未能直接", "无法直接", "未在", "没有直接",
        "not directly", "doesn't directly", "cannot directly", "not covered",
        "not in the provided", "not shown",
    )
    lower = answer.lower()
    if any(m in answer or m.lower() in lower for m in refusal_markers):
        flags.append("REFUSED")
    return {**run, "flags": flags, "answer_len": len(answer)}


async def main_async(concurrency: int, output_dir: Path, backend: str):
    bank = json.loads(QUESTIONS.read_text(encoding="utf-8"))
    course = bank["course_id"]
    questions = bank["questions"]

    sem = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient() as client:
        async def run_one(q):
            async with sem:
                log.info("→ %s [%s] %s", q["id"], q.get("chapter"), q["q"][:50])
                r = await ask(client, q, course, backend=backend)
                a = analyze(r)
                flags = ",".join(a.get("flags") or [])
                log.info("← %s flags=[%s] %.1fs path=%s",
                         q["id"], flags, r.get("elapsed_ms", 0) / 1000,
                         a.get("path"))
                return a

        tasks = [run_one(q) for q in questions]
        results = await asyncio.gather(*tasks)

    # Sort by question id to keep report stable across concurrent runs.
    results.sort(key=lambda r: r["id"])

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    output_dir.mkdir(parents=True, exist_ok=True)
    tag = backend.replace("_", "-")
    report_md = output_dir / f"{tag}_monitor_{ts}.md"
    raw_json = output_dir / f"{tag}_monitor_{ts}.json"
    raw_json.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    write_report(results, report_md, course, backend=backend)
    log.info("done — wrote %s", report_md)


def write_report(results: list[dict], path: Path, course: str, backend: str = "qwen_raft") -> None:
    total = len(results)
    flag_counts: dict[str, int] = {}
    for r in results:
        for f in r.get("flags") or []:
            key = f.split("(", 1)[0]
            flag_counts[key] = flag_counts.get(key, 0) + 1

    ok = sum(1 for r in results if not r.get("flags"))
    lines: list[str] = []
    lines.append(f"# {backend} output monitor — {course}")
    lines.append("")
    lines.append(f"- run at: {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"- total: {total}")
    lines.append(f"- clean (no flags): {ok}")
    lines.append("")
    lines.append("## Flag breakdown")
    lines.append("")
    lines.append("| flag | count |")
    lines.append("|------|-------|")
    for f, c in sorted(flag_counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"| `{f}` | {c} |")
    lines.append("")
    lines.append("## Per-question detail")
    lines.append("")
    for r in results:
        flags = r.get("flags") or []
        status = "✅" if not flags else "⚠️"
        lines.append(f"### {status} `{r['id']}` · {r.get('chapter')} · {r.get('type')}")
        lines.append("")
        lines.append(f"**Q:** {r['q']}")
        lines.append("")
        if "error" in r:
            lines.append(f"**ERROR:** {r['error']} — {r.get('body','')}")
            lines.append("")
            continue
        if flags:
            lines.append("**Flags:** " + ", ".join(f"`{f}`" for f in flags))
            lines.append("")
        path_v = r.get("path") or "-"
        lines.append(f"- path: `{path_v}` · elapsed: {r.get('elapsed_ms',0)/1000:.1f}s · answer_len: {r.get('answer_len',0)}")
        sources = r.get("sources") or []
        if sources:
            src_str = " · ".join(
                f"{s.get('file')}@{s.get('loc')} (score={s.get('score',0):.3f})"
                for s in sources
            )
            lines.append(f"- top sources: {src_str}")
        lines.append("")
        lines.append("**Answer:**")
        lines.append("")
        lines.append("```")
        # Trim to 1500 chars to keep report readable
        answer = r.get("answer", "") or ""
        if len(answer) > 1500:
            lines.append(answer[:1500] + "\n... [truncated]")
        else:
            lines.append(answer)
        lines.append("```")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, default=2,
                    help="Concurrent calls (default 2)")
    ap.add_argument("--backend", type=str, default="qwen_raft",
                    help="Backend to test: 'qwen_raft' (default), 'codex', "
                         "or 'default' for server-side default routing")
    ap.add_argument("--output-dir", type=Path, default=ROOT / "benchmarks",
                    help="Directory to write the dated report into")
    args = ap.parse_args()
    asyncio.run(main_async(args.concurrency, args.output_dir, args.backend))


if __name__ == "__main__":
    sys.exit(main())
