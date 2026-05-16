"""并排对比 PyMuPDF baseline vs MinerU 输出。

MinerU 输出是整本 markdown 不按页分（pipeline 模式会把所有页拼起来），
所以这里只能整体并排。生成 output/<sample>/compare.md。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "output"


def main() -> None:
    for sample_dir in sorted(OUT.iterdir()):
        if not sample_dir.is_dir():
            continue
        baseline = sample_dir / "pymupdf.md"
        mineru_dir = sample_dir / "mineru"
        # MinerU 的实际 md 文件路径：mineru/<stem>/auto/<stem>.md
        mineru_md = next(mineru_dir.glob("*/auto/*.md"), None) if mineru_dir.exists() else None

        if not baseline.exists():
            continue

        compare = sample_dir / "compare.md"
        parts = [f"# Compare: {sample_dir.name}", ""]
        parts.append("## PyMuPDF baseline")
        parts.append("")
        parts.append("```")
        parts.append(baseline.read_text(encoding="utf-8"))
        parts.append("```")
        parts.append("")
        parts.append("## MinerU output")
        parts.append("")
        if mineru_md and mineru_md.exists():
            parts.append("```")
            parts.append(mineru_md.read_text(encoding="utf-8"))
            parts.append("```")
        else:
            parts.append("_(MinerU 未生成 markdown)_")
        compare.write_text("\n".join(parts), encoding="utf-8")
        print(f"✓ {compare}")


if __name__ == "__main__":
    main()
