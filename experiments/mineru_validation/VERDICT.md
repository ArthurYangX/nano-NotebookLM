# MinerU vs PyMuPDF — 验证报告

**日期**: 2026-05-16
**环境**: Mac M4 base + 16GB RAM + Python 3.12.7 + torch 2.12.0
**backend**: pipeline (mineru[pipeline] extras)
**device**: CPU (MPS 模式在 `DocAnalysis init` 卡死，CPU 模式正常)

---

## 性能数据（ch3_hmm.pdf 前 12 页）

| 阶段 | 时间 |
|---|---|
| 首次模型 init | 51 s |
| 后续 init（缓存预热）| 6 s |
| 12 页推理 | ~120 s（10 s/页） |
| **演示场景估算**（5 PDF × 70 页 = 350 页）| **~60 分钟一次性跑完** |

## 抽取质量（ch3 HMM 同一公式对比）

**PyMuPDF**:
```
)
,
,
|
(
2
1

k
t
i
t
j
t
s
qs
```

**MinerU**:
```latex
$$
P(q_t = s_j \mid q_{t-1} = s_i, q_{t-2} = s_k, \cdots)
$$
```

## 维度评分（ch3_hmm 12 页）

| 维度 | PyMuPDF | MinerU |
|---|---|---|
| 公式 → LaTeX | ❌ 砸成单字符竖排 | ✅ 完整 LaTeX |
| 表格 → markdown | ❌ 砸成线性文字 | ✅ HTML table 含 rowspan/colspan |
| 图片提取 | ❌ 完全丢失 | ✅ 自动抠图存 jpg + markdown 引用 |
| 章节层级 | ❌ 平铺 | ✅ `##` 标记 |
| 中文识别 | ✅ | ✅ |
| 速度 | ms 级 | 10 s/页 |

## MinerU 已知小毛病

1. 把表头中的特殊字符（"今天/昨天/晴朗"等堆叠图形）误识别为公式，产生 `\frac{A_{\widehat{A}}}{B}\mathbb{A}` 这种乱码。
2. 个别字符错认：`于` → `於`、`—` → `–`、`AndreiA.Markov` 缺空格。
3. 表格行内容偶尔串行（HMM 转移概率表的 `yesterday` 行错位）。

但这些瑕疵在"质量从 0 提升到 80 分"的语境下都是可接受的。chunker 后续可以在这一层做后处理修正。

---

## 判定: ✅ 接入

**建议路径**:
- ✅ **B. 双 pipeline**：默认 PyMuPDF（快，ms 级，适合首屏预览），可选 MinerU（慢，10 s/页，公式题/演示场景启用）
- ⬜ A. 全量替换：除非 chunker 后处理稳定，否则不建议（演示 OK，日常上传一份新 PDF 等 5-10 分钟体验差）
- ⬜ C. 不接入：放弃（公式题能力差距太大）
- ⬜ D. 只对学术 paper 启用：本课件全是 slide，证伪

**接入方案**:
1. 新增 `nano_notebooklm/ingest/extractors_mineru.py`：`extract_pdf_mineru(filepath, lang="ch")` 函数，包装 `mineru` Python API（避免每次 CLI 启动重新加载模型），返回 `list[PageInfo]`，把 markdown 按 `## ` 切 section、把 LaTeX 公式 block 标 `has_formula`。
2. `extract_file()` 加 `engine: Literal["pymupdf", "mineru"] = "pymupdf"` 参数。
3. `/api/upload/{id}` 加 `?engine=mineru` query 参数，前端 upload UI 加"高质量提取（慢）"复选框。
4. **首次加载模型放在 FastAPI startup**（lazy load），避免每次上传重启 fast_api 服务。

**风险点**:
- `transformers` 从 5.6.2 被降到 4.57.6 —— 要测 `sentence-transformers` 加载 MiniLM 是否还正常（embedding 链路）。
- 演示前要预下载模型（PDF-Extract-Kit 约 1GB）。
- MPS 模式卡死是个 mineru bug，本验证完全走 CPU，速度上限在这。如果未来 mineru 修了 MPS，速度可以快 3-5 倍。

## 验证产物

- `experiments/mineru_validation/output/ch3_hmm/mineru/ch3_hmm/auto/ch3_hmm.md` — MinerU 输出
- `experiments/mineru_validation/output/ch3_hmm/pymupdf.md` — PyMuPDF baseline
- `experiments/mineru_validation/output/ch3_hmm/compare.md` — 并排对比
- 其他三章（ch1/ch4/ch9）尚未跑 MinerU，留作下一步泛化验证。
