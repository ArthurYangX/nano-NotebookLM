"""PyMuPDF baseline: 抽取 3 个样本前 5 页（pattern_recognition_midterm 只有 1 页）。

输出落 output/<sample>/pymupdf.md，供与 MinerU 输出并排对比。
"""

from __future__ import annotations

import fitz
from pathlib import Path

ROOT = Path(__file__).parent
SAMPLES = ROOT / "samples"
OUT = ROOT / "output"

PAGE_LIMITS = {
    "ch1_intro": 8,    # 第一章绪论（基线，公式少）
    "ch3_hmm": 12,     # 第二章 HMM（公式重镇）
    "ch4_dl": 12,      # 第四章 II 深度学习（矩阵公式 + 网络图）
    "ch9_lm": 10,      # 第六章 语言模型（softmax / PPL）
}


def main() -> None:
    for stem, max_pages in PAGE_LIMITS.items():
        src = SAMPLES / f"{stem}.pdf"
        dst_dir = OUT / stem
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / "pymupdf.md"

        doc = fitz.open(str(src))
        n = len(doc) if max_pages is None else min(len(doc), max_pages)
        parts: list[str] = [f"# PyMuPDF baseline: {stem}", f"_pages: 1..{n} of {len(doc)}_", ""]
        for i in range(n):
            page = doc[i]
            text = page.get_text().strip()
            parts.append(f"## Page {i+1}")
            parts.append("")
            parts.append(text if text else "_(empty)_")
            parts.append("")
        doc.close()

        dst.write_text("\n".join(parts), encoding="utf-8")
        print(f"✓ {dst} ({n} pages)")


if __name__ == "__main__":
    main()
