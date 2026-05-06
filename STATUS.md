# STATUS — nano-NOTEBOOKLM execution board

> **Codex 是 implementer。Claude 是 reviewer。** 用户（人）协调任务分配。
> 一切合约定义在 `GOAL.md`，不要在这改 GOAL；这里只追踪执行状态。
> 最后更新：2026-05-06。

## How this works

1. **用户**看 STATUS.md 决定下一项让 codex 还是 claude 做（人是任务分配者）。
2. **Worker**（codex 或 claude）：
   - 把对应项 `status` 从 `[ ]` 改成 `[codex]` 或 `[claude]` —— 这就是 claim
   - 写 `claimed_at` 时间戳（YYYY-MM-DD HH:MM）
   - 实施：先写 mini-test + corner-test（红），再写实现（绿），最后跑 `pytest`
   - 完成后把 `status` 改成 `[review]`，填完 review block 的所有字段
3. **Reviewer**（claude）：
   - 跑 `pytest`、读 diff、检查 GOAL.md 该条 success criteria
   - 通过 → `status` → `[x]`，把 GOAL.md 对应 `☐` → `☑`，更新 CLAUDE.md Maturity Notes（如有需要）
   - 不通过 → `status` 退回 `[codex]` 或 `[claude]`，在 `review_notes` 写明哪几条不过

## Lock states

| 符号 | 含义 |
|---|---|
| `[ ]` | available，谁都可以 claim |
| `[codex]` | codex 在做（claude 不要碰这项的代码文件） |
| `[claude]` | claude 在做（codex 不要碰这项的代码文件） |
| `[review]` | 已提交，等 claude 审 |
| `[x]` | 通过，已打勾回 GOAL.md |
| `[BLOCKED]` | 卡住，详情写在 review_notes |

## Reviewer checklist（claude 每次审都要逐条过）

- [ ] `pytest` 全部 pass（不止新加的；旧测试不能挂）
- [ ] mini-test 存在且真覆盖 happy path
- [ ] corner-test 存在且至少覆盖 GOAL.md 列出 5 类之一
- [ ] 改动严格匹配 GOAL.md 该条 success criteria（不多不少）
- [ ] Constraints 没踩坑（无 build step / 不引 Celery / 测试 offline / monkeypatch LLM 等）
- [ ] 浏览器实测 happy + 1 个 corner（仅当涉及 UI）
- [ ] CLAUDE.md `Maturity Notes` 段已更新（仅当成熟度变化）
- [ ] 没有引入新的依赖（除非任务本身需要，且写明在 review block）

## Conflict / safety rules

- 任何时刻 **不要让 codex 和 claude 同时编辑同一文件**。一项任务 lock 期间，另一个 agent 不动该任务涉及的文件。
- 修 STATUS.md 自身要原子化：claim → 写完一次保存 → 让对方看到。不要长时间 hold 编辑。
- 紧急释放：用户在文件顶部加一段 `## OVERRIDE: <task#> -> [ ]` 强制解锁。
- 任务跨多个 GOAL items 时拒绝接 —— 拆成单项再做。
- LLM / 网络 / DB / 真实 backend 调用一律 monkeypatch 走假桩，否则 review 直接打回。

---

# Items

## P0 — must have

### #1 6 个 skill 都有前端入口

- **goal ref**: GOAL.md success criteria #1（exam-analysis / report / mastery 加 UI）
- **status**: [review]
- **owner**: codex
- **claimed_at**: 2026-05-06 10:29
- **files**: frontend/app.jsx (~335 lines changed), frontend/study-state.js (306 lines), frontend/api.js (~67 lines), frontend/styles.css (~149 lines)
- **mini-test**: tests/test_frontend_helpers.py::test_frontend_skill_entries_happy
- **corner-test**: tests/test_frontend_helpers.py::test_frontend_skill_entries_timeout（上游失败 / 网络断开降级）
- **pytest**: 46 passed (was 22) — `============================== 46 passed in 1.56s ==============================`
- **self-check**: ☑ GOAL #1 mini  ☑ corner  ☑ no regression
- **review_notes**: UI browser smoke could not bind port 8000 in sandbox; FastAPI TestClient and Node helper tests passed.

### #2 引用可点击 → Reader 跳页 + 高亮

- **goal ref**: GOAL.md success criteria #2
- **status**: [review]
- **owner**: codex
- **claimed_at**: 2026-05-06 10:29
- **files**: frontend/app.jsx (~335 lines changed), frontend/reader.jsx (~19 lines), frontend/assistant.jsx (~29 lines), frontend/study-state.js (306 lines), frontend/styles.css (~149 lines)
- **mini-test**: tests/test_frontend_helpers.py::test_citation_navigation_happy
- **corner-test**: tests/test_frontend_helpers.py::test_citation_navigation_invalid（数据缺失：引用文件不存在）
- **pytest**: 46 passed (was 22) — `============================== 46 passed in 1.56s ==============================`
- **self-check**: ☑ GOAL #2 mini  ☑ corner  ☑ no regression
- **review_notes**:

### #3 思维导图深化设计（数据 / 视觉 / 交互 / 联动）

- **goal ref**: GOAL.md success criteria #3
- **status**: [review]
- **owner**: codex
- **claimed_at**: 2026-05-06 10:29
- **files**: nano_notebooklm/kg/extractor.py (~43 lines), nano_notebooklm/kg/graph.py (~18 lines), nano_notebooklm/kg/merger.py (~15 lines), nano_notebooklm/types.py (~5 lines), api/server.py (~237 lines), frontend/mindmap.jsx (~40 lines), frontend/study-state.js (306 lines), frontend/styles.css (~149 lines)
- **mini-test**: tests/test_frontend_helpers.py::test_mindmap_layout_happy
- **corner-test**: tests/test_frontend_helpers.py::test_mindmap_layout_empty（空 KG 占位）
- **pytest**: 46 passed (was 22) — `============================== 46 passed in 1.56s ==============================`
- **self-check**: ☑ GOAL #3 mini  ☑ corner（200 节点 / 空 KG / 重名 至少一种）  ☑ no regression
- **review_notes**: Corner covers empty KG; 200-node FPS remains helper-level only, not browser-measured due sandbox port binding.

### #4 Subagent 模块（web_research + formatter）

- **goal ref**: GOAL.md success criteria #4
- **status**: [review]
- **owner**: codex
- **claimed_at**: 2026-05-06 10:29
- **files**: nano_notebooklm/agents/__init__.py (23 lines), nano_notebooklm/agents/web_research.py (74 lines), nano_notebooklm/agents/formatter.py (85 lines), api/server.py (~237 lines), frontend/assistant.jsx (~29 lines)
- **mini-test**: tests/test_agents.py::test_subagent_web_research_happy; tests/test_agents.py::test_subagent_formatter_happy
- **corner-test**: tests/test_agents.py::test_subagent_web_research_timeout（上游失败 / 网络不可用）; tests/test_agents.py::test_subagent_formatter_invalid（嵌套代码块 / 不闭合 LaTeX）
- **pytest**: 46 passed (was 22) — `============================== 46 passed in 1.56s ==============================`
- **self-check**: ☑ GOAL #4 mini  ☑ corner（API key 缺 / prompt injection / 嵌套代码块 至少一种）  ☑ web search monkeypatched  ☑ no regression
- **review_notes**: No real search provider is called in tests; search_fn is injected.

### #5 Notes 可编辑 + Markdown / PDF 导出

- **goal ref**: GOAL.md success criteria #5
- **status**: [review]
- **owner**: codex
- **claimed_at**: 2026-05-06 10:29
- **files**: frontend/app.jsx (~335 lines), frontend/study-state.js (306 lines), frontend/styles.css (~149 lines)
- **mini-test**: tests/test_frontend_helpers.py::test_notes_edit_export_happy
- **corner-test**: tests/test_frontend_helpers.py::test_notes_edit_large（大数据量：>100KB 笔记 + 切课不丢草稿）
- **pytest**: 46 passed (was 22) — `============================== 46 passed in 1.56s ==============================`
- **self-check**: ☑ GOAL #5 mini  ☑ corner（>100KB 不冻 / 切课不丢草稿 至少一种）  ☑ no regression
- **review_notes**: PDF export uses browser print-to-PDF path, no new dependency.

### #6 Quiz 答案跨会话保留 + 错题复习

- **goal ref**: GOAL.md success criteria #6
- **status**: [review]
- **owner**: codex
- **claimed_at**: 2026-05-06 10:29
- **files**: frontend/app.jsx (~335 lines), frontend/study-state.js (306 lines)
- **mini-test**: tests/test_frontend_helpers.py::test_quiz_persistence_happy
- **corner-test**: tests/test_frontend_helpers.py::test_quiz_persistence_invalid（数据变更：题库变更 stale 提示）
- **pytest**: 46 passed (was 22) — `============================== 46 passed in 1.56s ==============================`
- **self-check**: ☑ GOAL #6 mini  ☑ corner（题库变更 stale 提示）  ☑ no regression
- **review_notes**:

### #8 流式生成（notes / quiz / report）

- **goal ref**: GOAL.md success criteria #8
- **status**: [review]
- **owner**: codex
- **claimed_at**: 2026-05-06 10:29
- **files**: api/server.py (~237 lines), frontend/api.js (~67 lines), frontend/app.jsx (~335 lines), frontend/study-state.js (306 lines)
- **mini-test**: tests/test_streaming_api.py::test_stream_generation_happy
- **corner-test**: tests/test_streaming_api.py::test_stream_generation_timeout（上游失败 / 流中断保留 partial + retryable）
- **pytest**: 46 passed (was 22) — `============================== 46 passed in 1.56s ==============================`
- **self-check**: ☑ GOAL #8 mini  ☑ corner（流中断保留 + retry）  ☑ no regression
- **review_notes**:

## P1 — daily-use quality

### #7 Mastery 仪表盘 + 定向练习

- **goal ref**: GOAL.md success criteria #7
- **status**: [review]
- **owner**: codex
- **claimed_at**: 2026-05-06 10:29
- **files**: frontend/app.jsx (~335 lines), frontend/study-state.js (306 lines), frontend/styles.css (~149 lines), api/server.py (~237 lines)
- **mini-test**: tests/test_frontend_helpers.py::test_mastery_targeted_quiz_happy
- **corner-test**: tests/test_frontend_helpers.py::test_mastery_empty（数据缺失 / 全分 ≥0.5 空态）
- **pytest**: 46 passed (was 22) — `============================== 46 passed in 1.56s ==============================`
- **self-check**: ☑ GOAL #7 mini  ☑ corner（mastery.json 缺 / 全分 ≥0.5）  ☑ no regression
- **review_notes**:

### #9 失败可恢复 + 重试 UI

- **goal ref**: GOAL.md success criteria #9
- **status**: [review]
- **owner**: codex
- **claimed_at**: 2026-05-06 10:29
- **files**: frontend/app.jsx (~335 lines), frontend/study-state.js (306 lines)
- **mini-test**: tests/test_frontend_helpers.py::test_retry_generation_happy
- **corner-test**: tests/test_frontend_helpers.py::test_retry_generation_timeout（连续 3 次失败给出错误详情）
- **pytest**: 46 passed (was 22) — `============================== 46 passed in 1.56s ==============================`
- **self-check**: ☑ GOAL #9 mini  ☑ corner（连续 3 次失败错误详情）  ☑ no regression
- **review_notes**:

### #10 每日 session log

- **goal ref**: GOAL.md success criteria #10
- **status**: [review]
- **owner**: codex
- **claimed_at**: 2026-05-06 10:29
- **files**: nano_notebooklm/orchestrator/session_log.py (67 lines), api/server.py (~237 lines), frontend/app.jsx (~335 lines)
- **mini-test**: tests/test_session_log.py::test_session_log_happy
- **corner-test**: tests/test_session_log.py::test_session_log_large_rotate（大数据量：log 文件超过阈值轮转）
- **pytest**: 46 passed (was 22) — `============================== 46 passed in 1.56s ==============================`
- **self-check**: ☑ GOAL #10 mini  ☑ corner（log 大小阈值轮转）  ☑ no regression
- **review_notes**:

### #11 可观测：状态栏 backend / latency / cost

- **goal ref**: GOAL.md success criteria #11
- **status**: [review]
- **owner**: codex
- **claimed_at**: 2026-05-06 10:29
- **files**: api/server.py (~237 lines), frontend/app.jsx (~335 lines), frontend/study-state.js (306 lines), frontend/styles.css (~149 lines)
- **mini-test**: tests/test_frontend_helpers.py::test_observability_status_happy
- **corner-test**: tests/test_frontend_helpers.py::test_observability_status_timeout（上游失败：backend 全挂降级显示）
- **pytest**: 46 passed (was 22) — `============================== 46 passed in 1.56s ==============================`
- **self-check**: ☑ GOAL #11 mini  ☑ corner（backend 全挂降级显示）  ☑ no regression
- **review_notes**:

---

# Done log（按通过时间倒序）

> reviewer 通过后把 entry 从上面摘下来贴这里，保留主要 metadata 用于 audit。

_(空，等第一项通过)_

---

# OVERRIDE（紧急释放区，用户写）

_(空)_
