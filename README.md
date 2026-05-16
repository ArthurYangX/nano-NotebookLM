# nano-NOTEBOOKLM

AI 学习助手：上传 PDF/PPTX 课件 → 自动建知识图谱 + 向量索引 → 提供聊天问答、结构化笔记、练习题、考试模式、可编辑图谱。

详细内部架构见 `CLAUDE.md`，本 README 只讲怎么跑起来。

> 💡 **懒人路径**：把 `AGENT.md` 直接发给你常用的 AI 助手（Claude / Cursor / Codex / Copilot），让它带你完成下面所有步骤——它会自动跑命令、读 `/api/status`、按故障树诊断问题。

---

## 架构

```
[React 前端]
     ↓
[FastAPI :8000]
     ├──→ codex GPT-5.5 + text-embedding-3-small     (国内代理)
     ├──→ Qwen-Base / Qwen-RAFT  4-bit @ AutoDL      (可选，要联系作者)
     └──→ 本地 FAISS + BM25 + 知识图谱
```

前端 React CDN 无构建，FastAPI 直接 serve。3 种 LLM 后端，前端 chip 切：🤖 Codex / 🐧 Qwen-Base / 🎓 Qwen-RAFT。

---

## 启动

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[test]"
cp .env.example .env       # 改下面这几个字段
python api/server.py       # → http://localhost:8000
```

---

## `.env` 必填

```bash
OPENAI_API_KEY=sk-你自己的
OPENAI_BASE_URL=https://codex.ysaikeji.cn/v1
OPENAI_MODEL=gpt-5.5

EMBEDDING_MODE=local
EMBEDDING_MODEL=all-MiniLM-L6-v2
# 或者切 api 模式跨语言更准但烧积分：
# EMBEDDING_MODE=api  EMBEDDING_MODEL=text-embedding-3-small

DEFAULT_BACKEND=openai
```

codex 代理 `codex.ysaikeji.cn` OpenAI 协议兼容，自己充值积分。或换成任何 OpenAI 兼容 endpoint。

---

## Qwen-RAFT / Qwen-Base 后端

**要用 RAFT 微调版或 Base 量化版联系作者**——服务器开着的时候作者会给你 SSH 转发命令 + `.env` 片段。

不用 Qwen 也能完整 demo：chip 只显示 🤖 Codex，所有功能正常工作。

---

## 主要 API

| 端点 | 用途 |
|---|---|
| `POST /api/chat` | RAG + 图检索聊天 |
| `POST /api/notes/full-course/stream` | LaTeX 笔记 NDJSON 流 |
| `POST /api/quiz` | 练习题 |
| `POST /api/exam-prep/*` | 自演化考试题库（plan / seed / quiz/next / quiz/submit） |
| `GET/POST /api/mindmap/{course_id}` | 知识图谱读写 |
| `POST /api/upload/{course_id}` | 课件上传，立刻返回 `{task_id, course_id}`；后台跑 4 阶段 pipeline |
| `GET /api/upload/status/{task_id}` | 轮询上传进度快照（关 tab/刷新 → 恢复进度） |
| `GET /api/status` | 后端 + 模型 + Qwen 健康 |

请求示例：

```bash
curl -X POST http://localhost:8000/api/chat -H "Content-Type: application/json" -d '{
  "question": "什么是局部感受野",
  "course_id": "NLP",
  "user_lang": "zh",
  "backend": "qwen_raft"
}'
```

---

## 4 路线 Benchmark

100 题 × 4 路线对比（详见 `benchmarks/report.md` + 5 题并排样例）：

| 路线 | 平均字 | cite 率 | LaTeX | 延迟 |
|---|---:|---:|---:|---:|
| GPT-bare（无 RAG） | 473 | 0% | 75% | 49s |
| GPT-RAGKG（图检索）| 357 | 96% | 42% | 46s |
| Qwen-Base + RAG | 402 | 100% | 42% | 59s |
| Qwen-RAFT + RAG | 560 | 100% | 25% | 103s |

PPT 可直接引用：
- 接 RAG 的三条 cite 率 ≈ 100%，验证图检索召回链能用
- RAFT 答案最长但 LaTeX 最少 → 训练偏教学叙述，对公式不敏感
- GPT-bare 不接 RAG 反而 LaTeX 最积极（靠通用知识写公式）
- RAFT 延迟最高（强制生成 CoT 三段，前端被剥掉但 token 已生成）

---

## 项目结构

```
api/server.py              FastAPI 主入口
frontend/                  React CDN 前端
nano_notebooklm/
  ├── ai/                  LLM 抽象 + codex/Qwen backend
  ├── kb/                  FAISS + BM25 + 图检索
  ├── kg/                  知识图谱抽取
  ├── skills/              QA / notes / quiz / exam-prep / report
  └── orchestrator/        skill 路由 + multi-turn agent
benchmarks/                100 题题库 + 评测脚本 + report.md
artifacts/courses/<id>/    每门课的 chunks / faiss / KG / 笔记缓存
CLAUDE.md                  完整内部架构（200+ 节）
```
