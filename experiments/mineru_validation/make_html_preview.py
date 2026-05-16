"""Render mineru output (markdown + LaTeX) into a self-contained HTML preview.

Reads `output/<sample>/mineru/<stem>/auto/<stem>.md` and writes
`output/<sample>/preview.html` — open in a browser to see formulae
rendered by KaTeX, tables as proper HTML tables, images inlined.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "output"


HTML_TEMPLATE = """<!doctype html>
<html><head>
<meta charset="utf-8">
<title>MinerU preview: {title}</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css">
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/contrib/auto-render.min.js"
  onload="renderMathInElement(document.body,{{delimiters:[{{left:'$$',right:'$$',display:true}},{{left:'$',right:'$',display:false}}]}});"></script>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
body{{font:15px/1.55 -apple-system,Segoe UI,Helvetica;max-width:900px;margin:24px auto;padding:0 16px;color:#222}}
h1,h2,h3{{border-bottom:1px solid #eee;padding-bottom:4px}}
img{{max-width:100%;border:1px solid #ddd;border-radius:4px}}
table{{border-collapse:collapse;margin:1em 0}} td,th{{border:1px solid #999;padding:4px 8px}}
pre{{background:#f6f8fa;padding:12px;border-radius:6px;overflow:auto}}
.banner{{background:#fffbe6;border:1px solid #ffe58f;padding:8px 12px;border-radius:6px;margin-bottom:18px}}
</style>
</head><body>
<div class="banner">MinerU preview · {title}<br>
<small>raw md: <code>{md_path}</code> · debug pdfs: <code>{layout_pdf}</code> · <code>{span_pdf}</code></small>
</div>
<div id="md"></div>
<script>
const raw = {md_json};
// marked + KaTeX: stash math blocks before markdown, restore after.
const stash = [];
const masked = raw
  .replace(/\\$\\$([\\s\\S]+?)\\$\\$/g, (m) => {{ stash.push(m); return `@@MATH${{stash.length-1}}@@`; }})
  .replace(/(?<!\\\\)\\$([^\\n$]+?)\\$/g, (m) => {{ stash.push(m); return `@@MATH${{stash.length-1}}@@`; }});
let html = marked.parse(masked);
html = html.replace(/@@MATH(\\d+)@@/g, (_, i) => stash[+i]);
document.getElementById('md').innerHTML = html;
// resolve image paths relative to the md file's dir
document.querySelectorAll('#md img').forEach(img => {{
  const src = img.getAttribute('src');
  if (src && !src.startsWith('http')) {{
    img.src = '{img_base}/' + src;
  }}
}});
// re-run KaTeX after we injected math back in
setTimeout(()=>renderMathInElement(document.body,{{delimiters:[{{left:'$$',right:'$$',display:true}},{{left:'$',right:'$',display:false}}]}}), 100);
</script>
</body></html>
"""


def main() -> None:
    import json

    rendered = 0
    for sample_dir in sorted(OUT.iterdir()):
        if not sample_dir.is_dir():
            continue
        mineru_root = sample_dir / "mineru"
        md = next(mineru_root.glob("*/auto/*.md"), None) if mineru_root.exists() else None
        if md is None:
            continue

        md_text = md.read_text(encoding="utf-8")
        layout_pdf = md.with_name(md.stem + "_layout.pdf")
        span_pdf = md.with_name(md.stem + "_span.pdf")

        html = HTML_TEMPLATE.format(
            title=sample_dir.name,
            md_path=str(md.relative_to(ROOT.parent)),
            layout_pdf=str(layout_pdf.relative_to(ROOT.parent)) if layout_pdf.exists() else "(missing)",
            span_pdf=str(span_pdf.relative_to(ROOT.parent)) if span_pdf.exists() else "(missing)",
            md_json=json.dumps(md_text),
            img_base=str(md.parent.relative_to(sample_dir)),
        )
        preview = sample_dir / "preview.html"
        preview.write_text(html, encoding="utf-8")
        print(f"✓ {preview}")
        rendered += 1
    if rendered == 0:
        print("No mineru outputs found yet — run mineru first.")


if __name__ == "__main__":
    main()
