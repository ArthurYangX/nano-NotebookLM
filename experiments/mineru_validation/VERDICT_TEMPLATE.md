# MinerU vs PyMuPDF — 提取质量评测

**日期**: 2026-05-16
**环境**: Mac M4 base + 16GB RAM + Python 3.12.7 + torch 2.11.0
**backend**: pipeline (mineru[pipeline] extras)

---

## 样本对比

### 样本 1: 模式识别期中.pdf (1 页, 中文思维导图 + Unicode 公式)

| 维度 | PyMuPDF | MinerU |
|---|---|---|
| 文字完整性 | ⬜ | ⬜ |
| 中文识别 | ⬜ | ⬜ |
| 公式识别（→ LaTeX） | ⬜ | ⬜ |
| 阅读顺序 | ⬜ | ⬜ |
| 速度 | ms 级 | ⬜ s |

**判定**: ⬜⬜⬜

### 样本 2: CS231N lecture_7.pdf (前 10 页, 英文 slides + 公式图像)

| 维度 | PyMuPDF | MinerU |
|---|---|---|
| 文字完整性 | 部分（slide 模板文字重复多） | ⬜ |
| 公式识别 | ❌ 完全抽不到公式图像 | ⬜ |
| 图表说明 | ❌ | ⬜ |
| 阅读顺序 | 乱 | ⬜ |
| 速度 | ms 级 | ⬜ s |

**判定**: ⬜⬜⬜

### 样本 3: 计算机组成原理 - 存储器系统.pdf (前 10 页, 中文 + 多列布局)

| 维度 | PyMuPDF | MinerU |
|---|---|---|
| 文字完整性 | ✅ | ⬜ |
| 块状布局还原 | ❌ 砸成线性 | ⬜ |
| 中文识别 | ✅ | ⬜ |
| 速度 | ms 级 | ⬜ s |

**判定**: ⬜⬜⬜

---

## 总结

**值不值得接入？** ⬜ / ⬜ / ⬜

**建议路径**:
- ⬜ A. 接入主 ingest pipeline，所有上传都走 MinerU
- ⬜ B. 双 pipeline：默认 PyMuPDF（快），可选 MinerU（慢但好）
- ⬜ C. 不接入，PyMuPDF 已足够，重点修 chunker mojibake + 行级切分即可
- ⬜ D. MinerU 只对学术 paper 类型有效，对幻灯片 / 思维导图 ROI 低

**速度判断**: M4 上 ⬜ s/页 → 单文件 30 页 ≈ ⬜ s，可接受 / 不可接受

**主要发现**:
1.
2.
3.
