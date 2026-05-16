"""Generate a side-by-side visual comparison: PyMuPDF (raw, unrendered) vs MinerU (KaTeX rendered).

Output: `output/<sample>/compare_visual.html` — open in a browser.
Left pane keeps the original chaotic linearization so you see the
"before" pain. Right pane renders MinerU's LaTeX + tables + images
through KaTeX and marked.js so you see the "after" recovery.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "output"

HTML = r"""<!doctype html>
<html lang="zh"><head>
<meta charset="utf-8">
<title>PyMuPDF vs MinerU · __TITLE__</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css">
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/contrib/auto-render.min.js"
  onload="renderMathInElement(document.body,{delimiters:[{left:'$$',right:'$$',display:true},{left:'$',right:'$',display:false}],throwOnError:false});"></script>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
:root{
  --bg:#fafbfc; --border:#d9dee4; --left-tint:#fff5f5; --right-tint:#f0fff4;
  --left-accent:#c0392b; --right-accent:#27ae60;
}
*{box-sizing:border-box}
body{margin:0;font:14px/1.6 -apple-system,Segoe UI,Helvetica,"PingFang SC","Microsoft YaHei";background:var(--bg);color:#222}
header{padding:20px 24px;border-bottom:1px solid var(--border);background:#fff;position:sticky;top:0;z-index:10}
header h1{margin:0 0 4px;font-size:20px}
header p{margin:0;color:#666;font-size:13px}
.meta{display:flex;gap:24px;margin-top:8px;font-size:12px;color:#555}
.meta b{color:#222}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--border);min-height:calc(100vh - 100px)}
.col{background:#fff;overflow:auto;max-height:calc(100vh - 100px)}
.col-header{padding:10px 16px;font-weight:600;border-bottom:1px solid var(--border);position:sticky;top:0;background:#fff;z-index:5}
.col.left .col-header{background:var(--left-tint);border-left:4px solid var(--left-accent)}
.col.right .col-header{background:var(--right-tint);border-left:4px solid var(--right-accent)}
.col-header small{display:block;font-weight:400;color:#666;font-size:11px;margin-top:2px}
.content{padding:16px 20px}
.left .content pre{
  background:#fff; border:1px dashed #e5b8b8; padding:14px;
  white-space:pre-wrap; word-break:break-all; font-family:"SF Mono","Menlo",monospace;
  font-size:12.5px; color:#7a3d3d; margin:0;
}
.right .content{font-size:14px}
.right .content h1,.right .content h2,.right .content h3{
  border-bottom:1px solid #e1e8ed; padding-bottom:4px; margin-top:1.6em;
}
.right .content img{max-width:100%;border:1px solid #ddd;border-radius:4px}
.right .content table{border-collapse:collapse;margin:1em 0;font-size:13px}
.right .content td,.right .content th{border:1px solid #999;padding:5px 10px}
.right .content code{background:#f6f8fa;padding:1px 4px;border-radius:3px;font-size:12.5px}
.right .content .katex-display{margin:0.8em 0}
.banner{padding:8px 16px;background:#fff3cd;border:1px solid #ffeaa7;border-radius:4px;margin-bottom:16px;font-size:12.5px;color:#856404}
.diff-stat{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;margin-left:6px}
.diff-stat.bad{background:#fce4e4;color:#c0392b}
.diff-stat.good{background:#e0f5e9;color:#27ae60}
footer{padding:16px 24px;text-align:center;color:#888;font-size:12px;border-top:1px solid var(--border);background:#fff}
</style>
</head><body>
<header>
<h1>PyMuPDF vs MinerU · 提取效果对比</h1>
<p>样本：<b>__TITLE__</b>  · 同一份 PDF 同样页范围 · 左：现行 PyMuPDF 抽取（RAG 真正吃到的文本）· 右：MinerU pipeline 抽取</p>
<div class="meta">
<span>PyMuPDF: <span class="diff-stat bad">公式打散成单字符竖排、表格丢失、阅读顺序乱 → RAG 召回到的就是这样的乱码</span></span>
<span>MinerU:  <span class="diff-stat good">LaTeX 公式还原、HTML 表格、图自动抠出</span></span>
<span>速度：PyMuPDF ms 级 · MinerU 10s/页 (M4 CPU)</span>
</div>
</header>
<div class="cols">
  <div class="col left">
    <div class="col-header">PyMuPDF <small>fitz.get_text() 原样输出 · 这就是抽出来的真实文本，没有做任何额外处理</small></div>
    <div class="content"><pre id="left"></pre></div>
  </div>
  <div class="col right">
    <div class="col-header">MinerU <small>pipeline backend 输出 · marked + KaTeX 渲染</small></div>
    <div class="content" id="right"></div>
  </div>
</div>
<footer>方案对比 · 2026-05-16 · 同一份 PDF (ch3_hmm 前 12 页)</footer>

<script>
const left_text  = __LEFT_JSON__;
const right_md   = __RIGHT_JSON__;
const img_base   = __IMG_BASE_JSON__;

document.getElementById('left').textContent = left_text;

// marked + KaTeX: stash math, render markdown, restore math, then trigger KaTeX
const stash = [];
const masked = right_md
  .replace(/\$\$([\s\S]+?)\$\$/g, (m) => { stash.push(m); return `@@MATH${stash.length-1}@@`; })
  .replace(/(?<!\\)\$([^\n$]+?)\$/g, (m) => { stash.push(m); return `@@MATH${stash.length-1}@@`; });
let html = marked.parse(masked);
html = html.replace(/@@MATH(\d+)@@/g, (_, i) => stash[+i]);
document.getElementById('right').innerHTML = html;
// resolve relative image paths
document.querySelectorAll('#right img').forEach(img => {
  const src = img.getAttribute('src');
  if (src && !src.startsWith('http')) img.src = img_base + '/' + src;
});
// re-run KaTeX
setTimeout(() => renderMathInElement(document.body, {
  delimiters: [{left:'$$',right:'$$',display:true},{left:'$',right:'$',display:false}],
  throwOnError: false,
}), 150);
</script>
</body></html>
"""


INDEX_HTML = """<!doctype html>
<html lang="zh"><head>
<meta charset="utf-8">
<title>PyMuPDF vs MinerU · 对比页索引</title>
<style>
body{font:15px/1.55 -apple-system,Segoe UI,Helvetica,"PingFang SC";max-width:780px;margin:40px auto;padding:0 20px;color:#222}
h1{margin:0 0 4px;font-size:22px}
.sub{color:#666;margin:0 0 24px;font-size:13px}
ul{list-style:none;padding:0}
li{margin:8px 0;padding:14px 18px;border:1px solid #d9dee4;border-radius:8px;background:#fff;display:flex;justify-content:space-between;align-items:center}
li:hover{border-color:#27ae60;box-shadow:0 1px 4px rgba(0,0,0,0.05)}
li a{font-weight:600;color:#2c5aa0;text-decoration:none;font-size:16px}
li a:hover{color:#27ae60}
li small{color:#666;font-size:12px}
.note{background:#fffbe6;border:1px solid #ffe58f;padding:10px 14px;border-radius:6px;margin-top:24px;font-size:13px;color:#5a4f1a}
code{background:#f6f8fa;padding:1px 5px;border-radius:3px;font-size:12.5px}
</style>
</head><body>
<h1>PyMuPDF vs MinerU · 提取效果对比</h1>
<p class="sub">同一份 PDF 同样页范围 · 左：现行抽取（RAG 实际吃到的文本）· 右：MinerU pipeline 抽取</p>
<ul>__ITEMS__</ul>
<div class="note">想给其它 PDF 生成对比页：先把样本 PDF 拷到 <code>samples/</code>，跑 <code>python experiments/mineru_validation/run_pymupdf_baseline.py</code> 和 <code>python -m mineru -p &lt;pdf&gt; -o output/&lt;name&gt;/mineru -b pipeline -l ch</code>，最后 <code>python experiments/mineru_validation/make_compare_visual.py</code>，本页会自动更新。</div>
</body></html>
"""


def main() -> None:
    rendered: list[tuple[str, str, int, int]] = []
    for sample_dir in sorted(OUT.iterdir()):
        if not sample_dir.is_dir():
            continue
        pymupdf_md = sample_dir / "pymupdf.md"
        mineru_root = sample_dir / "mineru"
        mineru_md = next(mineru_root.glob("*/auto/*.md"), None) if mineru_root.exists() else None

        if not pymupdf_md.exists() or mineru_md is None:
            continue

        left_text = pymupdf_md.read_text(encoding="utf-8")
        right_md = mineru_md.read_text(encoding="utf-8")
        img_base = str(mineru_md.parent.relative_to(sample_dir))

        html = (
            HTML.replace("__TITLE__", sample_dir.name)
                .replace("__LEFT_JSON__", json.dumps(left_text))
                .replace("__RIGHT_JSON__", json.dumps(right_md))
                .replace("__IMG_BASE_JSON__", json.dumps(img_base))
        )
        target = sample_dir / "compare_visual.html"
        target.write_text(html, encoding="utf-8")
        print(f"✓ {target}")
        rendered.append((sample_dir.name, f"output/{sample_dir.name}/compare_visual.html",
                         len(left_text), len(right_md)))

    if not rendered:
        print("Nothing to compare — need both pymupdf.md and mineru/<stem>/auto/<stem>.md")
        return

    # write root-level index linking to every compare page
    items = "\n".join(
        f'  <li><a href="{href}">{name}</a>'
        f'<small>{left_n:,} chars PyMuPDF · {right_n:,} chars MinerU</small></li>'
        for name, href, left_n, right_n in rendered
    )
    index = ROOT / "index.html"
    index.write_text(INDEX_HTML.replace("__ITEMS__", items), encoding="utf-8")
    print(f"✓ {index}")


if __name__ == "__main__":
    main()
