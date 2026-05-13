"""Parse test-slides/nlp_benchmark_100_questions.md -> benchmarks/questions_100.json.

Patches question 31 to include the two missing emission probabilities
(b_晴(潮湿)=0.15, b_阴(潮湿)=0.25) needed for the delta_2 computation.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "test-slides" / "nlp_benchmark_100_questions.md"
OUT = ROOT / "benchmarks" / "questions_100.json"

CHAPTER_RANGES = [
    ("ch1", 1, 15, "intro_paradigm"),
    ("ch3", 16, 35, "markov_hmm"),
    ("ch4", 36, 60, "classical_ml"),
    ("ch4(2)", 61, 85, "deep_learning"),
    ("ch9", 86, 100, "language_model_llm"),
]

TYPE_MAP = {"概念": "concept", "公式": "formula", "计算": "calculation"}


def chapter_for(qid: int) -> tuple[str, str]:
    for ch, lo, hi, section in CHAPTER_RANGES:
        if lo <= qid <= hi:
            return ch, section
    raise ValueError(f"qid {qid} out of range")


def parse() -> list[dict]:
    md = SRC.read_text(encoding="utf-8")
    parts = md.split("## 参考答案 / 评分点")
    assert len(parts) == 2, "markdown must have a single 参考答案 section"
    q_block, a_block = parts

    q_pat = re.compile(r"^(\d+)\. \【(概念|公式|计算)\】(.+?)(?=^\d+\. \【|^### |^## |\Z)", re.M | re.S)
    a_pat = re.compile(r"^(\d+)\. (.+?)(?=^\d+\. |\Z)", re.M | re.S)

    questions = {int(m.group(1)): (m.group(2), m.group(3).strip()) for m in q_pat.finditer(q_block)}
    answers = {int(m.group(1)): m.group(2).strip() for m in a_pat.finditer(a_block)}

    assert set(questions.keys()) == set(range(1, 101)), f"missing q: {set(range(1,101)) - set(questions)}"
    assert set(answers.keys()) == set(range(1, 101)), f"missing a: {set(range(1,101)) - set(answers)}"

    out = []
    for qid in range(1, 101):
        type_zh, q_text = questions[qid]
        ref_answer = answers[qid]
        chapter, section = chapter_for(qid)
        if qid == 31:
            q_text = q_text.rstrip(".。") + "（补充发射概率：`b_晴(潮湿)=0.15`、`b_阴(潮湿)=0.25`、`b_雨(潮湿)=0.35`。）"
        out.append({
            "id": f"q{qid:03d}",
            "qid": qid,
            "chapter": chapter,
            "section": section,
            "type": TYPE_MAP[type_zh],
            "q": q_text,
            "ref_answer": ref_answer,
        })
    return out


def main() -> None:
    items = parse()
    payload = {
        "version": 1,
        "course_id": "test-slides",
        "count": len(items),
        "type_distribution": {t: sum(1 for x in items if x["type"] == t) for t in ("concept", "formula", "calculation")},
        "chapter_distribution": {ch: sum(1 for x in items if x["chapter"] == ch) for ch, *_ in CHAPTER_RANGES},
        "questions": items,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {OUT}  ({payload['count']} questions, types={payload['type_distribution']}, chapters={payload['chapter_distribution']})")


if __name__ == "__main__":
    main()
