# AGENT.md — 给 AI 助手的安装指引

这份文档是给 AI 编程助手（Claude / Cursor / Codex / Copilot / 通义灵码 / etc.）读的。如果用户拿到这份代码压缩包后**直接把这个文件交给 AI**，AI 按下面步骤可以把同学的环境完整跑起来。

---

## 你（AI）的角色

你正在帮一位新同学把 `nano-NOTEBOOKLM` 跑起来。他们大概有：

- Python 3.10+
- 一些命令行基础
- 一个 OpenAI 兼容的 API key（或者想用国内 codex 代理）

**你的任务**：按下面顺序执行命令、读取 `/api/status` 输出、给同学解释成功/失败原因。**遇到下面"必须问用户"的项目时停下问用户**，不要替他猜。

---

## 安装与启动（5 步）

### 1. 确认在项目根

```bash
ls README.md CLAUDE.md api/server.py
```

三个文件都在 = 在项目根。否则 `cd` 进去。

### 2. 装依赖

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[test]"
```

### 3. 配置 `.env`（**必须问用户**）

```bash
cp .env.example .env
```

打开 `.env`，问用户以下三个问题再填：

| 字段 | 问题 |
|---|---|
| `OPENAI_API_KEY` | "你的 API key 是什么？没有可以去 `codex.ysaikeji.cn` 充值。" |
| `OPENAI_BASE_URL` | "你的 OpenAI 兼容服务地址？默认填 `https://codex.ysaikeji.cn/v1`。" |
| `OPENAI_MODEL` | "对应的模型名？codex 代理填 `gpt-5.5`；OpenAI 原生填 `gpt-4o-mini` 之类。" |

其他字段保持 `.env.example` 默认：

```
EMBEDDING_MODE=local
EMBEDDING_MODEL=all-MiniLM-L6-v2
DEFAULT_BACKEND=openai
```

`QWEN_*` 字段**不要填**（除非用户明确说"我已经联系作者拿到 Qwen 转发命令"——参考下面"Qwen 后端"段落）。

### 4. 启动 server

```bash
python api/server.py
```

首次启动会自动从 HuggingFace 下载 `all-MiniLM-L6-v2` 模型 (~80MB)，等 20-30s。

### 5. 验证

```bash
curl -s http://localhost:8000/api/status | python -m json.tool
```

期望看到：

```json
{
  "openai_api_key_configured": true,
  "embed_warm_ok": true,
  "qwen_raft_configured": false,
  "qwen_base_configured": false,
  ...
}
```

然后让用户浏览器打开 `http://localhost:8000`，应该看见前端。

---

## 故障决策树

### `openai_api_key_configured: false`

用户没填 `.env` 里 `OPENAI_API_KEY`。让他填上重启。

### `embed_warm_ok: false` 卡住超过 1 分钟

模型下载被墙。两个选择：
- 让用户配代理：`export HF_ENDPOINT=https://hf-mirror.com`
- 或者切 `EMBEDDING_MODE=api`（消耗 API 积分但不依赖 HuggingFace）

### Chat 返回 500 + log 含 `daily_points_exhausted`

Codex 当日积分耗尽（默认 12500/天）。让用户：
- 等明天 00:05 自动重置
- 或者换 `OPENAI_BASE_URL` 到别的 OpenAI 兼容服务

### Chat 召回不准（用 "HMM" 等英文缩写搜中文课件拿到错章节）

已知限制——`all-MiniLM-L6-v2` 是英文 only。修复办法：

```bash
# .env 改：
# EMBEDDING_MODE=api
# EMBEDDING_MODEL=text-embedding-3-small

python scripts/reembed_all.py   # 约 2 小时
```

或告诉用户："query 用全中文（隐马尔可夫）或全英文（Hidden Markov Model）能绕开"。

### `qwen_raft_available: false` 但用户切 chip 到 RAFT

正常——这要本地 SSH tunnel 转发到一台跑 Qwen 的 GPU 服务器。用户没接的话**这条路线就是不可用**，让他在 chip 选 🤖 Codex 即可，其他功能完全正常。

---

## Qwen-RAFT / Qwen-Base 后端

**默认不可用**。如果用户问"我想用 RAFT 微调版"：

告诉他：**联系 nano-NOTEBOOKLM 项目作者**。作者会给：
1. 一行 `sshpass + ssh -L` 双端口转发命令（密码 + 端口每个实例不同）
2. 一段 `.env` 片段（`QWEN_BASE_URL` / `QWEN_RAFT_URL` / 两个 timeout env）

具体内容**不在压缩包里**，因为依赖作者当前实例状态。

---

## DO NOT（不要做的事）

| 不要 | 原因 |
|---|---|
| 删 `artifacts/` 文件夹 | 含用户上传后已处理的课程数据 |
| 把 `.env` 提交到 git | 会泄漏 API key |
| 改 `CLAUDE.md` / `GOAL.md` | 内部文档，不要篡改 |
| 改 `DEFAULT_BACKEND` | 除非用户明确接了 Qwen，否则保持 `openai` |
| `git push` | 除非用户明确说"帮我推" |
| 跑 `scripts/reembed_all.py` 没问用户 | 烧 2 小时 + API 积分 |

---

## 关键文件指引

| 路径 | 内容 |
|---|---|
| `README.md` | 简短公开 README |
| `CLAUDE.md` | 完整内部架构（200+ 节，给重度修改用） |
| `GOAL.md` | 项目愿景 + roadmap |
| `benchmarks/report.md` | 4 路线 benchmark + 5 题并排样例 |
| `api/server.py` | FastAPI 主入口 |
| `frontend/app.jsx` | 主 React 组件 |
| `nano_notebooklm/skills/qa_skill.py` | RAG 聊天主逻辑 |

---

## 完成后

帮用户跑一个 sanity test：

```bash
curl -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"question": "hello", "user_lang": "en"}'
```

收到非空 JSON 答案 = 全链路通了。结束任务，让用户自己浏览前端探索。
