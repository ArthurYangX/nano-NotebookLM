# GOAL: 把 nano-NOTEBOOKLM 变成"我每天会真的打开来学习"的工具

> 这是一份 self-contained 的 goal prompt。任何一个新 Claude 会话拿到它都能直接接着往下干。
> 最后更新：2026-05-07（Round 3 起步）。

## Definition of done（什么叫"可用"）

**单用户、个人学习场景**。我（一个大三学生，8 门课）能在它上面：

- 提问 → 给出带**可点击引用**的答案（点击直接跳到 Reader 对应页 + 高亮）
- 生成笔记 / 题目 → **可编辑、可导出**（Markdown / PDF）
- 看到自己**哪里薄弱**，并能一键生成针对性练习
- 上次生成的所有内容**重启后还在**
- 任何一次生成都**不会假死**（流式输出 + 失败可重试 + 部分结果保留）
- **中文问题 = 英文问题**：中文 query 在中文课、英文课、或跨课都不会被冷待
- 模型**不会拿垃圾片段强答**：低质量命中应触发降级（通用回答 / 翻译重试 / 跨课 fallback），而不是把 0.03 分的随机 chunk 灌给 GPT 输出迷惑答案

**不是** SaaS、不是给其他人用的、不是研究 artifact。

## Current state（2026-05-06，Round 2 起步前）

- API v0.2.0 / 25 routes（含 streaming + subagent + session-log）/ Pydantic 校验 / `x-request-id` + 延迟头 / 全局错误处理
- 8 门课 / 15,382 chunks / FAISS + BM25 + RRF
- 6 个 skill 后端 + 全部前端入口；Notes/Quiz/Mindmap/Skills/History 5 个 tab + Assistant
- Streaming 端点存在（NDJSON，但目前是"全量后切块"伪流式）
- 50 个 pytest 全过（含 search/chat smoke 守门 + 24 个新增覆盖 Round 1 的 mini+corner）
- 回归 eval harness 三层：Layer 1 pytest smoke / Layer 2 search 739 题（87% 命中 baseline）/ Layer 3 chat 抽样
- Round 1 已完成 11 条 success criteria（见 STATUS.md Done log）
- **当前阶段统一用 codex GPT-5.5 跑全流程**。AutoDL Qwen2.5-7B-RAFT 桥接脚本就绪但 deferred
- **Embedding 仍是 all-MiniLM-L6-v2**（中文语义弱，靠 BM25 字符 bigram 兜底）

## Test discipline（**第一性原则，不可绕过**）

每一个 P0/P1/P2 交付项都必须同时带：

- **mini-test**（happy path）—— 至少一个单元或集成测试，证明这个功能在正常输入下端到端工作。要快、要 deterministic、要在 `pytest` 里跑过。
- **corner-test**（边界 / 失败模式）—— 至少一个测试覆盖以下其中一类：
  - 空输入 / 缺字段 / 超长输入 / 非法格式（→ 期望 422 + request_id）
  - 上游失败（LLM 不可用、超时、返回畸形 JSON）→ 期望优雅降级，不崩
  - 并发 / 重复请求 / 中途取消 → 不污染状态
  - 数据缺失（课程不存在、chunks.json 损坏、index 未 build）→ 期望明确错误码
  - 大数据量（top_k 边界、长文档分块退化）

**Test 必须 offline**：沿用 `tests/conftest.py` 的 hash-based `fake_embed_fn` 模式，禁止真打 LLM API。需要 mock 的 LLM 调用就 monkeypatch `ModelRouter.complete`。

**测试命名约定**：`test_<feature>_<happy|empty|invalid|timeout|...>` —— 一眼能看出覆盖了什么。

**Coverage gate**：新增功能涉及的函数 / 端点必须至少一对（mini + corner）。改动旧代码导致测试退化 → 必须先修绿再合并。

**Regression eval gate**：大改动后必须跑 `python scripts/run_eval.py`（无 LLM 成本）。命中率不能跌破 baseline 85%；All Courses 100% 不能跌。详见 STATUS.md Done log #R2。

## Round 2 Success criteria（8 条 checklist，全勾才算完成本轮）

每条都附带需要补的测试。**没补 mini + corner 的，不许打勾。**

1. ☑ **智能查询路由：双路径 + 质量门槛**
   - **路径 A（RAG）**：`kb.search` 后用 score 门槛（top1 ≥ τ AND ≥2 hits）筛选；不达标即降级。τ 通过 eval 数据 tune（建议从 0.05 起）
   - **路径 B（通用 GPT）**：寒暄 / 短输入（strip 后字符 < 3 或纯标点）/ 路径 A 兜底失败 → 不带 RAG context，专用 system prompt：身份是学习助手，提供通用回答，明确告知"未基于课程材料"
   - **路由器**：`nano_notebooklm/orchestrator/router_intent.py` 新增；规则优先（长度 / 标点 / 寒暄关键词），必要时 LLM 一次 classify
   - **后端响应加 `path` 字段**：`"rag" | "general" | "translated" | "cross-course"` 给前端以不同 chip 样式显示
   - mini：3 个用例 — RAG-hit / 短输入走通用 / 阈值不达标降级到通用
   - corner：低分但单 hit、纯标点、空 query strip-after-empty

2. ☑ **0-hit 自动翻译重试**
   - 每门课 ingest 时计算"中文字符占比"指纹，存到 `course_meta.json`；query 端 unicode 区段统计判 zh / en / mixed
   - 当前课 0 hits + 语言不匹配 → 让 codex 翻译 query 一次（专用短 prompt，禁止解释）→ retry search 一次
   - 答案前缀注明 "原问 X，翻译为 Y 后检索"，回前端 `path: "translated"` + `original_query`/`translated_query`
   - **只翻一次**，避免循环（mixed 语言 query 直接走双语 search）
   - mini：中文 query 在 15-213（英文课）→ 触发翻译命中 → 答案带翻译注明
   - corner：(a) 翻译 LLM call 失败 → graceful 降级到跨课 fallback，不崩 (b) 翻译后还是 0 → 路径 B 兜底 (c) mixed 语言 query 不重复翻译

3. ☑ **跨课 fallback + 课程语言指示器**
   - 当前课 0 hits + 翻译也 0 hits → 自动用 All Courses 重搜，命中后给警示 "本课无相关内容，从《X 课》找到："
   - 顶栏课程下拉每条名称旁加语言标识（🇨🇳 / 🇺🇸 / 🌐 mixed）：根据 ingest 时的中文字符占比指纹决定
   - 用户能一眼看到"我现在在哪门课、它是什么语言"
   - mini：单课 0 hits → cross-course 命中 → 答案有 from-other-course 标注
   - corner：(a) cross-course 也 0 → 路径 B 兜底，message 友好 (b) 课程指纹缺失（旧 ingest）→ 文件名启发判语言 (c) 标识在密度紧凑模式下不挤压主标题

4. ☐ **Embedding 升级到 bge-base-zh-v1.5（双语）**
   - `.env`：`EMBEDDING_MODEL=BAAI/bge-base-zh-v1.5`，模型本地下载或走 API
   - 维度变化（768 vs 384）→ FAISS index 必须 rebuild；`build_index` 检测维度不匹配自动 rebuild
   - 跑 `scripts/run_eval.py` 重新 baseline，目标：中文课 hit rate ≥ 95%，all-courses meta 100%
   - 中文 query 在中文课的 vector top1 score 从 ~0.03 → ~0.5+
   - mini：search "什么是内存" 在 计算机组成原理 课 top1 score ≥ 0.3
   - corner：(a) 模型权重缺失 → 启动报清晰错误，不 silent fail (b) index 维度不匹配 → 自动 rebuild + 提示 (c) 离线测试用 hash-based fake，禁止真下载

5. ☑ **真流式生成（替代当前伪流式）**
   - `OpenAIBackend._complete_codex_sync` 已是 streaming，把 `response.output_text.delta` 直通 `_stream_response` 的 NDJSON 事件，**不要等全量再切块**
   - 前端体感首 token 时间 < 1.5s（codex GPT-5.5 实测），全程不再"等 30s 然后秒刷"
   - mini：服务端 NDJSON 事件粒度 ≤ 50 字符 / 事件，按时间间隔到达（不是一次性全到）
   - corner：流中断（client 断开 / 上游超时 30s）→ 已发的 partial 保留 + retryable=true；OpenAIBackend 改动不能破坏现有 chat（非流）路径

6. ☑ **CJK 字体 fallback + 中英混排细节**
   - `frontend/styles.css` 三个 `--serif`/`--sans`/`--mono` 末尾追加 `"PingFang SC", "Microsoft YaHei", "Hiragino Sans GB", "Noto Sans SC"`
   - 引用 chip 长中文文件名 `text-overflow: ellipsis` + 鼠标悬停展开
   - inline `$...$` 公式前后中英文之间自动加 `0.15em` 细空格
   - mini：(无 build 测) 视觉脚本 `tests/test_styles_cjk.py` 检查 styles.css 里三个 font 栈都含至少一个 CJK 字体
   - corner：仅有英文系统的环境 fallback 到 sans-serif 不报错

7. ☑ **Pydantic 输入硬化（strip-then-validate）**
   - `SearchRequest` `ChatRequest` `NoteRequest` 等 query/question 字段加 `@field_validator` 在 strip 后再判 min_length
   - 单空格 / `\n\n\n` / 全角空格 / 仅 emoji / 仅零宽字符 → 422 + request_id
   - **不破坏 #R2 eval baseline**（adversarial 单空格的 14/15 → 应 15/15）
   - mini：strip-then-empty 各种形态 → 422
   - corner：(a) 看似空但含 unicode 控制字符 → 也判空 (b) 合法但全是空白 + 一个字符 "x " → 200（不要过度严格）

8. ☐ **eval 概念抽取改用 jieba（中文质量飞跃）**
   - `scripts/build_eval_questions.py` 中文部分 regex `[一-鿿]{2,4}` → `jieba.cut` + 词性过滤（n / vn / eng）
   - 问题集中文部分从 "部定位方"、"题的线性" 这类碎片 → 真术语 "局部性原理"、"运动学正解"
   - 重新生成 questions.jsonl，跑 Layer 2 eval，中文课命中率应进一步 ↑
   - mini：抽出的中文概念 ≥ 80% 是有意义术语（人工标注 50 条样本评估）
   - corner：jieba 模块缺失 → 降级到 regex + 警告（不 hard fail）

## Round 3 Success criteria（用户 2026-05-07 提出的 3 条 checklist，全勾才算 Round 3 完成）

每条都附带需要补的测试。**没补 mini + corner 的，不许打勾。**

R3-1. ☐ **Quiz / Skills / History tab 滚动修复**
   - **背景**：`RealQuizView` / `SkillsDashboard` / `SessionHistory` 都用 `<div className="reader-body">` 包裹，但 `frontend/styles.css` 没定义 `.reader-body`（只定义了 R6 时为 Notes 加的 `.notes-reader-body`）。父级 `.workspace` 是 `overflow: hidden`，所以这三个 tab 超出视口的内容被裁剪，用户上下滑不动。
   - **修法**：`styles.css` 加一条 `.reader-body { height:100%; overflow-y:auto; overflow-x:hidden; }`。
   - mini：grep styles.css 含 `.reader-body { ... overflow-y:auto ... }` 规则
   - corner：(a) 与 `.notes-reader-body` selector 物理独立，不互相覆盖（grep 顺序 + selector 唯一）；(b) 浏览器实测 Quiz 30 题滚到底部 + Skills 三卡片不被裁；(c) `.workspace` 仍 `overflow:hidden` 不被波及

R3-2. ☐ **用户语言偏好（中/英）首次选择 + 全链路注入**
   - **背景**：当前 `QA_SYSTEM` 仅一句"Match the user's language"是弱约束，LLM 可能因为 source 是英文就用英文回答中文 query，反之亦然。用户要的是 **显式偏好**——首次进入选中文/英文，之后所有 AI 输出强制按这个语言。
   - **前端**：首次进入弹轻量 modal（"请选择语言 / Choose your language: 中文 / English"），结果写到 `localStorage["nano-nlm:v1:user-lang"]`，顶栏右上加 chip 可改。
   - **API 透传**：`API.chat` / `API.streamNotes` / `API.streamReport` / `API.generateQuiz` / `/api/agent/stream` 全部带 `user_lang: "zh" | "en"`。
   - **后端注入**：`QA_SYSTEM` / `GENERAL_QA_SYSTEM` / `NOTE_GENERATION_SYSTEM` / `QUIZ_GENERATION_SYSTEM` / `REPORT_*` 末尾加强约束："The user has set their preferred language to {zh|en}. Reply ONLY in that language regardless of the source material's language."（覆盖原"match the user's language"弱约束）。`agent_loop.compose_system_prompt` 同样注入。
   - **范围**：只支持 zh / en 两种值。`null` / 未传 → 老行为（match query lang）。
   - mini：(a) POST /api/chat with user_lang="zh" → captured system prompt 含 zh-only 强约束；(b) frontend modal 在未设 lang 时渲染（grep app.jsx）
   - corner：(a) user_lang="fr" → 422 + standard error envelope；(b) user_lang 未传 → 兼容老行为；(c) localStorage 持久化跨刷新（grep study-state.js loadUserLang）；(d) `/api/agent/stream` compose_system_prompt 含 lang binding

R3-3. ☐ **思维导图：学习顺序角标 + 节点深探（agent stream）**
   - **背景**：M1+M2+M3 完成后 mindmap 是个静态地图——5-9 个 macro topics + leaves，但缺 (a) 学习路径感（先学哪个 topic）；(b) 深度入口（点节点只有右下角 detail 面板，没有真讲解）。
   - **学习顺序**：Stage A prompt 升级让 LLM 在 topics 间补 `prerequisite_of` 关系；`extractor.py` 解析后用稳定 Kahn 拓扑排序，给每个 topic Concept 打 `learning_order: int`（1 起编号）；`/api/mindmap/{cid}` payload 透传 learning_order；`mindmap.jsx` topic 节点（depth=1, learning_order != null）加角标 "1 / 2 / 3 ..."。
   - **节点深探**：alt+click 任意节点 → 调新端点 `/api/mindmap/{cid}/explain-node`（POST `{node_id}`），后端 wrap `agent_loop.run_agent` 限 turns=4 + 工具子集（search_kb + read_chunk only），NDJSON 流出"5 行精讲 + 3 道 mini quiz"到前端 `<NodeDeepDivePanel>`。
   - **工具子集**：deep-dive 不允许 `generate_note`（避免无意中写文件）/ `list_courses`（无意义）；只暴露 search_kb + read_chunk。
   - mini：(a) Stage A LLM stub 给 prerequisite_of → extractor 落成 depends-on edges + 拓扑序；(b) /api/mindmap payload 含 learning_order；(c) /api/mindmap/{cid}/explain-node 端点 NDJSON 输出 tool_call/tool_result/done
   - corner：(a) topics 成环 → 退化按 weight 降序（边界）；(b) LLM 无 prerequisite_of 字段 → learning_order=None，老 mindmap 仍渲（兼容）；(c) explain-node node_id 不存在 → 404（数据缺失）；(d) explain-node 路径穿越 course_id → 400（非法格式）；(e) 前端契约 grep：mindmap.jsx 含 `e.altKey` + `requestNodeDeepDive` + `<NodeDeepDivePanel>`

## Priority

**P0 — 没这些 Round 2 就不算完成**

- #1 智能查询路由 + 质量门槛（直接解决用户实测的 "?" → 垃圾答案 + 寒暄走 RAG 的 UX 灾难）
- #2 0-hit 自动翻译重试（直接解决"中文 query 在英文课 0 hits"的体验破口）
- #3 跨课 fallback + 课程语言指示
- #4 Embedding 升级到 bge-base-zh-v1.5（中文 vector recall 的根本问题）

**P1 — 真正成为日常工具**

- #5 真流式生成
- #6 CJK 字体 + 中英混排
- #7 Pydantic 输入硬化
- #8 eval jieba 概念抽取

**Round 3 P0 — 用户 2026-05-07 当下要的**

- R3-1 Quiz/Skills/History 滚动修复（5 分钟 quick win，单文件单测试）
- R3-2 用户语言偏好（中/英）首次选择 + 全链路注入
- R3-3 思维导图：学习顺序角标 + 节点深探（agent stream）

**P2 — 锦上添花（先放着）**

- Memory 编辑 UI（目前只 API）
- Quiz 打印版 PDF
- 移动端响应式
- Mindmap 200-node 浏览器实测 fps + virtualisation

**Deferred — 等 Round 2 P0/P1 跑齐再回来**

- AutoDL Qwen2.5-7B-RAFT 本地模型链路打通（脚本备好但暂不用，先用 codex GPT-5.5）
- citation 引用页码越界单独 corner（数据缺失文件已有，页码超界没补）
- **Round 2 #4 embedding 升级 bge-base-zh-v1.5**：需要下载 ~440MB 模型 + FAISS index 重建（15k chunks，~10 分钟）+ 真实 LLM cost 验证。当前 #1+#2+#3 三条联动已经把"中文问题被冷待"的核心 UX 漏洞补住（gate 降级 + 翻译重试 + 跨课 fallback），#4 是把 vector recall 从 ~0.03 提到 ~0.5+，质量飞跃但不是阻断 5-day-streak 的关键。等用户决定何时下载模型再开。
- **Round 2 #8 jieba 概念抽取**：纯 eval 数据集质量改进，不影响日常使用 UX。现 87% baseline 是用 regex `[一-鿿]{2,4}` 抽出的概念问出的；换 jieba 后命中率会升但不改变线上行为。

## Non-goals（明确不做）

- 多用户 / 鉴权 / 多租户
- 限流 / 配额
- Docker / K8s / 云部署
- 实时协作
- 移动 App
- OpenAPI 客户端代码生成
- 自训 embedding

## Constraints（不要踩的坑）

- 前端**无 build step**（CDN React 18 + Babel standalone）。**不要**引 webpack / vite。
- 后端继续 FastAPI。**不要**引 Celery / Redis。
- 测试必须保持 **offline**。LLM 调用 + 翻译 + subagent web search 一律 monkeypatch。Embedding 真模型也禁止在测试里下载——用 `fake_embed_fn`。
- 学生数据全部留在 `artifacts/`，不要污染 repo。
- 当前阶段 LLM 调用**统一走 codex GPT-5.5**。AutoDL 仍 deferred。
- Subagent 设计原则：无状态函数 + 明确 input/output schema，orchestrator 决定何时调用，不让 subagent 自己 spawn。
- **路由不许重命名 / 删除现有 API**：所有 Round 1 端点行为契约保持兼容，新行为通过新字段（如 `path`）扩展。
- **新加的 `path` 字段语义不可乱用**：只有 `rag` / `general` / `translated` / `cross-course` 四个值，前端按这个 union 渲染样式。
- **eval baseline 不许跌**：每条 P0 完成后跑 `scripts/run_eval.py`，命中率退化 > 2% 就打回。

## How an agent should start working on this

1. 读 `CLAUDE.md` + `STATUS.md`（看协议 + Done log 哪些已完成）拿到最新 architecture。
2. 从 P0 挑一项，在 `STATUS.md` 加一个 entry 并 claim（status → `[codex]` 或 `[claude]`）。
3. **先写测试**（mini + corner）—— 红灯。再写实现 —— 绿灯。再 refactor。
4. 落地节奏：编辑代码 → `pytest` 全过 → 跑 `scripts/run_eval.py`（如果改了 RAG / search / qa）→ `python api/server.py` 启起来 → 浏览器验证 happy + 1 个 corner → 更新 `CLAUDE.md` 的 "Maturity Notes" 段。
5. **一次只 PR 一个 P0 item**，不要捆绑（Round 1 已经因为捆绑提交付出过审计代价）。每个 PR 至少包含 1 个新 mini-test 和 1 个新 corner-test。
6. 任何 LLM cost / 延迟 / 失败模式相关改动，要在状态栏或日志里留下可观测信号。
7. 完成后把 STATUS.md 该 entry 改 `[review]`，等 reviewer 审。reviewer 不通过 → 改回 `[codex]/[claude]` 按 review_notes 修。

## Success metric（一句话）

> **某一周里，我连续 5 天打开它学习，没有一次因为 UI 问题、生成卡死、引用不可点、或者中文问题被冷待而放弃**——那一周完成时，goal 算达成。

## Round 1 已完成（archive）

11 条 success criteria 全部 ☑（2026-05-06 commit 73e40cb + #R1 修复 + #R2 eval harness）：

1. ☑ 6 个 skill 都有前端入口
2. ☑ 引用可点击 → Reader 跳页 + 高亮
3. ☑ 思维导图深化设计（数据 / 视觉 / 交互 / 联动）
4. ☑ Subagent 模块（web_research + formatter）
5. ☑ Notes 编辑 + Markdown / PDF 导出
6. ☑ Quiz 答案跨会话保留 + 错题复习
7. ☑ Mastery 仪表盘 + 定向练习
8. ☑ 生成走流式（伪流式版，Round 2 #5 升级为真流式）
9. ☑ 失败可恢复 + 重试 UI
10. ☑ 每日 session log
11. ☑ 可观测：状态栏 backend / latency / cost

详细 review block 见 `STATUS.md` Done log。
