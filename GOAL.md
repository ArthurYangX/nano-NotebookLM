# GOAL: 把 nano-NOTEBOOKLM 变成"我每天会真的打开来学习"的工具

> 这是一份 self-contained 的 goal prompt。任何一个新 Claude 会话拿到它都能直接接着往下干。
> 最后更新：2026-05-06。

## Definition of done（什么叫"可用"）

**单用户、个人学习场景**。我（一个大三学生，8 门课）能在它上面：

- 提问 → 给出带**可点击引用**的答案（点击直接跳到 Reader 对应页 + 高亮）
- 生成笔记 / 题目 → **可编辑、可导出**（Markdown / PDF）
- 看到自己**哪里薄弱**，并能一键生成针对性练习
- 上次生成的所有内容**重启后还在**
- 任何一次生成都**不会假死**（流式输出 + 失败可重试 + 部分结果保留）

**不是** SaaS、不是给其他人用的、不是研究 artifact。

## Current state（2026-05-06，已验证）

- API v0.2.0 / 23 routes / Pydantic 校验 / `x-request-id` + 延迟头 / 全局错误处理
- 8 门课 / 15,382 chunks / FAISS + BM25 + RRF
- 6 个 skill 后端实现：`qa` `note_generator` `quiz_generator` `exam_analyzer` `report_generator` `mastery_tracker`
- Frontend：Reader / Notes / MindMap / Quiz 4 tab + Assistant sidebar，生成结果走 localStorage 持久化
- 22 个 pytest 全过（offline，无需 LLM key）
- **当前阶段统一用 codex GPT-5.5 跑全流程**。AutoDL Qwen2.5-7B-RAFT 桥接脚本已就绪但**整条本地模型链路 deferred 到下一阶段**——先把功能跑齐、再做 backend 切换。

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

## Success criteria（11 条 checklist，全勾才算 done）

每条都附带需要补的测试。**没补 mini + corner 的，不许打勾。**

1. ☐ **6 个 skill 都有前端入口**（exam-analysis / report / mastery 当前没 UI）
   - mini：每个新 UI 入口的 fetch 调用打通后端，断言关键字段渲染
   - corner：后端 502 / 网络断开时 UI 不白屏
2. ☐ **引用可点击** → Reader 跳到该页 + 高亮目标 chunk
   - mini：点击后 active page 切换、目标 chunk 有高亮 className
   - corner：引用指向已删除的文件 / 不存在的页码 → 给出友好提示
3. ☐ **思维导图深化设计**（数据结构 + 视觉 + 交互一体）
   - **数据**：节点 = 概念，边 = 关系类型（is-a / part-of / depends-on / example-of / related），每节点带 `depth / weight / source_chunks[]`，由 `kg/extractor.py` 在抽取时打标
   - **视觉**：根据 `weight` 调字号 + 颜色饱和度；按 `depth` 分层；中心节点固定，子节点放射或树状（受 Tweaks 面板控制）；边的样式按关系类型区分（实线 / 虚线 / 箭头）
   - **交互**：点击节点 → 右侧弹出详情面板（概念解释 + 关联 chunks + "针对这个出 3 道题"按钮）；拖拽 / 缩放 / pan；高亮搜索；折叠子树
   - **联动**：节点 `source_chunks[]` 与 Reader 双向跳转（点节点定位文档，反过来也行）
   - mini：渲染一个 30 节点的 KG，断言每节点尺寸/颜色随 weight 变化、点击触发详情面板、source_chunks 链接到正确文件
   - corner：(a) 节点 ≥ 200 仍流畅（≥ 30fps）；(b) 空 KG / 单节点孤儿态有占位；(c) 节点重名时按 ID 区分不串扰
4. ☐ **Subagent 模块**（生成期补充信息 + 输出格式化）
   - **职责 A — 网络搜索增补**：当用户对生成结果不满（"展开"/"补充例子"/"这是哪年的论文"）或主 LLM 自己识别"知识缺口"时，subagent 接过 query → 调 web search API（Tavily / Bing / Serper，先选一个）→ 摘要回填
   - **职责 B — 输出格式化**：把主 LLM 的原始输出走一遍格式化器（Markdown 修复、LaTeX 公式补全 `$...$`、代码块语言识别、引用格式 `[Source: ...]` 规整、流式拼接 artifact 清理）
   - **架构**：新建 `nano_notebooklm/agents/`，至少有 `web_research.py` 和 `formatter.py` 两个 subagent；orchestrator 提供 `run_subagent(name, payload)`；前端 Assistant 多一个"展开调查"按钮
   - mini：(a) 触发 web_research 拿到非空结果，merge 进答案后引用块格式正确；(b) formatter 把畸形 Markdown 修复成可渲染版本
   - corner：(a) 搜索 API key 缺失或网络不通 → fallback 到主答案 + 标注"未补充"；(b) 搜索返回的内容包含 prompt injection 信号要被拒；(c) formatter 遇到嵌套代码块 / 不闭合 LaTeX 不能死循环
5. ☐ **Notes 可编辑** + **Markdown / PDF 导出**
   - mini：编辑后内容写回 localStorage；导出文件名 / 内容正确
   - corner：超大笔记（> 100KB）不冻 UI；同时切课不丢草稿
6. ☐ **Quiz 答案跨会话保留** + "只看错题"复习模式
   - mini：答完刷新仍保留；切换 review 模式只显示错题
   - corner：题库变更后旧答案能识别为 stale 并提示
7. ☐ **Mastery 仪表盘**：弱点列表 + 一键"练这个"生成定向 quiz
   - mini：弱点点击调用 quiz API 带 topic 参数
   - corner：mastery.json 不存在 / 全分数 ≥ 0.5 时 UI 给空态
8. ☐ **生成走流式**（notes / quiz / report token-by-token）
   - mini：流式 chunk 累积渲染，最终内容 == 全量调用结果
   - corner：流中断 → 已收到的部分保留 + 显示 retry
9. ☐ **失败可恢复**：部分结果保留 + 重试按钮 + 不冻 UI
   - mini：retry 按钮触发新请求并替换内容
   - corner：连续 3 次失败给出错误详情而不是无限 spinner
10. ☐ **每日 session log**：当天 / 当门课问过什么、生成过什么，可回看
    - mini：每次生成 / 提问写 entry，按日期分组返回
    - corner：log 文件超过阈值自动轮转，不能让磁盘炸
11. ☐ **可观测**：状态栏显示当前 backend / latency / cost；p50 search < 200ms，p50 chat < 5s
    - mini：状态栏渲染 `/api/status` 数据
    - corner：backend 全挂时状态栏显示降级状态而非崩溃

## Priority

**P0 — 没这些就还是 demo**

- 引用可点击 → Reader 跳页
- 思维导图深化设计（数据/视觉/交互/联动）
- Subagent 模块（web_research + formatter）
- Notes 编辑 + Markdown 导出
- exam-analysis / report / mastery 三个前端入口
- 流式生成（notes / quiz）
- Quiz 答案持久化 + 错题复习

**P1 — 真正成为日常工具**

- Mastery → 定向练习闭环
- 每日 session 历史
- 生成进度 / 延迟 / cost 指示
- 全局搜索栏
- Subagent 扩展（更多领域 / 自适应触发策略）

**P2 — 锦上添花**

- Memory 编辑 UI（目前只 API）
- Quiz 打印版 PDF
- 移动端响应式

**Deferred — 暂时延后，等 P0/P1 跑齐再回来做**

- AutoDL Qwen2.5-7B-RAFT 本地模型链路打通（脚本备好但暂不用，先用 codex GPT-5.5）
- 自托管 embedding 切换实测

## Non-goals（明确不做）

- 多用户 / 鉴权 / 多租户
- 限流 / 配额
- Docker / K8s / 云部署
- 实时协作
- 移动 App
- OpenAPI 客户端代码生成
- 自训 embedding

## Constraints（不要踩的坑）

- 前端**无 build step**（CDN React 18 + Babel standalone）。**不要**引 webpack / vite，除非实在不行。
- 后端继续 FastAPI。**不要**引 Celery / Redis，除非后台 ingest 真成瓶颈。
- 测试必须保持 **offline**（无需 LLM key），沿用 `tests/test_api_smoke.py` 的 fake embed fn 模式。LLM 调用 + subagent web search 一律 monkeypatch。
- 学生数据全部留在 `artifacts/`，**不要污染** repo。
- 当前阶段 LLM 调用**统一走 codex GPT-5.5**。AutoDL 本地模型链路 deferred，`scripts/switch_backend.sh` 保留但暂不切。
- Subagent 设计原则：每个 subagent 是无状态函数 + 明确 input/output schema，主 orchestrator 决定何时调用，不让 subagent 自己再 spawn。

## How an agent should start working on this

1. 读 `CLAUDE.md` 拿到最新 architecture。
2. 从 P0 挑一项，用 TaskCreate 拆 3-5 步，第一步永远是"明确 mini-test + corner-test 的契约"。
3. **先写测试**（mini + corner）—— 红灯。再写实现 —— 绿灯。再 refactor。
4. 落地节奏：编辑代码 → `pytest` 全过 → `python api/server.py` 启起来 → 浏览器验证 happy path 和至少 1 个 corner case → 更新 `CLAUDE.md` 的 "Maturity Notes" 段。
5. **一次只 PR 一个 P0 item**，不要捆绑。每个 PR 至少包含 1 个新 mini-test 和 1 个新 corner-test。
6. 任何 LLM cost / 延迟 / 失败模式相关改动，要在状态栏或日志里留下可观测信号。

## Success metric（一句话）

> **某一周里，我连续 5 天打开它学习，没有一次因为 UI 问题、生成卡死、或者引用不可点而放弃**——那一周完成时，goal 算达成。
