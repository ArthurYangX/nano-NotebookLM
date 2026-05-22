// frontend/i18n.js — minimal i18n for nano-NOTEBOOKLM.
//
// Loaded as a plain JS file (no Babel) BEFORE any .jsx file in index.html,
// after the React UMD scripts. Exposes:
//   window.I18N.t(key, lang, vars?)  — pure function, safe to call anywhere
//   window.LangContext                — React.Context (default "en")
//   window.useT()                     — hook: returns (key, vars) => string
//
// Translation key convention follows react-i18next:
//   namespace.dotted.key  (semantic, not English-string-as-key)
// Missing key → falls back to en → falls back to key itself (never crashes).
(function () {
  "use strict";

  const STRINGS = {

    // ── common (cross-page reusable) ──
    "common.cancel":          { zh: "取消",       en: "Cancel" },
    "common.confirm":         { zh: "确认",       en: "Confirm" },
    "common.done":            { zh: "完成",       en: "Done" },
    "common.close":           { zh: "关闭",       en: "Close" },
    "common.delete":          { zh: "删除",       en: "Delete" },
    "common.save":            { zh: "保存",       en: "Save" },
    "common.loading":         { zh: "加载中…",    en: "Loading…" },
    "common.unknown_error":   { zh: "未知错误",   en: "Unknown error" },

    // ── language modal (first-run / re-pick) ──
    "lang_modal.title":       { zh: "选择回答语言",        en: "Choose your reply language" },
    "lang_modal.title_bi":    { zh: "选择回答语言 / Choose your reply language",
                                en: "选择回答语言 / Choose your reply language" },
    "lang_modal.hint":        {
      zh: "选定后，AI 在聊天、笔记、测验和报告中只会用这种语言回答。可随时通过顶栏切换。",
      en: "The assistant will reply ONLY in this language for chat, notes, quiz, and report generations. You can change this anytime via the topbar chip.",
    },
    "lang_modal.hint_bi":     {
      zh: "选定后 AI 仅以此语言回答聊天 / 笔记 / 测验 / 报告。可随时通过顶栏切换。\nThe assistant will reply ONLY in this language. You can change anytime via the topbar.",
      en: "选定后 AI 仅以此语言回答聊天 / 笔记 / 测验 / 报告。可随时通过顶栏切换。\nThe assistant will reply ONLY in this language. You can change anytime via the topbar.",
    },

    // ── topbar ──
    "topbar.manage_courses":         { zh: "管理",            en: "Manage" },
    "topbar.manage_courses_count":   { zh: "管理 · {n} 已隐藏", en: "Manage · {n} hidden" },
    "topbar.manage_tooltip":         {
      zh: "管理课程显示（仅前端隐藏，后端数据保留）",
      en: "Manage course visibility (frontend-only hide; backend data is preserved)",
    },
    "topbar.lang_chip_title":        { zh: "当前回答语言（点击切换）",  en: "Reply language preference (click to change)" },
    "topbar.lang_chip_title_unset":  { zh: "选择回答语言",              en: "Pick reply language" },
    "topbar.lang_chip_zh":           { zh: "中",  en: "中" },
    "topbar.lang_chip_en":           { zh: "EN",  en: "EN" },
    "topbar.backend_cycle":          { zh: "点击切换后端",              en: "Click to switch backend" },
    "topbar.backend_only":           { zh: "唯一已配置后端",            en: "Only configured backend" },
    "topbar.settings":               { zh: "设置（助手名 / 语言 / 后端 / 缓存）", en: "Settings (helper name, language, backend, cache)" },
    "topbar.notes_polishing":        { zh: "✨ 润色中",                  en: "✨ Polishing" },
    "topbar.notes_polishing_tip":    {
      zh: "第二轮：统一术语 / 加交叉引用 / 折叠重复定义。完成后会一次性替换为润色版。",
      en: "Pass 2: unify terminology, add cross-refs, collapse duplicate definitions. Replaces the draft once done.",
    },
    "topbar.notes_truncated":        { zh: "⚠️ {n} 截断",                en: "⚠️ {n} truncated" },
    "topbar.notes_truncated_lines": {
      zh: "以下文件因输出 token 上限被截断:",
      en: "The following files were truncated due to output token limit:",
    },
    "topbar.notes_truncated_review": {
      zh: "review 阶段也被截断 — 笔记结尾可能不完整",
      en: "Review pass was also truncated — notes may be incomplete near the end",
    },
    "topbar.notes_truncated_hint": {
      zh: "提示: 设置 NOTES_PER_FILE_MAX_TOKENS / NOTES_REVIEW_MAX_TOKENS 提高上限后重试",
      en: "Tip: raise NOTES_PER_FILE_MAX_TOKENS / NOTES_REVIEW_MAX_TOKENS to retry",
    },

    // ── empty workspace ──
    "empty.title":             { zh: "上传文档开始", en: "Upload documents to begin" },
    "empty.subtitle":          {
      zh: "上传一份 PDF / PPTX / DOCX / Markdown，系统会自动抽取章节、构建知识图谱，再驱动问答与笔记。",
      en: "Drop in a PDF / PPTX / DOCX / Markdown — sections are extracted, a knowledge graph is built, and chat + notes light up automatically.",
    },
    "empty.cta":               { zh: "上传第一个文档", en: "Upload your first document" },

    // ── course picker / upload modal ──
    "upload.modal_title":      { zh: "上传到哪个课程？",   en: "Upload to which course?" },
    "upload.close_aria":       { zh: "关闭",               en: "Close" },
    "upload.close_title":      { zh: "关闭 (Esc)",         en: "Close (Esc)" },
    "upload.existing_courses": { zh: "添加到已有课程",     en: "Add to existing course" },
    "upload.or_create":        { zh: "或新建课程",         en: "Or create a course" },
    "upload.create":           { zh: "新建课程",           en: "Create a course" },
    "upload.new_placeholder":  { zh: "输入新课程名称",     en: "Enter new course name" },
    "upload.dup_msg":          { zh: "已存在同名课程，请直接点上方按钮", en: "A course with this name exists — pick it above" },
    "upload.invalid_msg":      { zh: "名称含非法字符 — 仅支持字母 / 数字 / 中文 / 空格 / . - _",
                                 en: "Invalid characters — only letters / digits / Chinese / spaces / . - _" },
    "upload.create_btn":       { zh: "新建并上传",         en: "Create and upload" },
    "upload.dup_helper":       { zh: "已存在同名课程「{name}」 — 请直接点上方按钮，或换一个名字。",
                                 en: "Course \"{name}\" already exists — pick it above or use a different name." },
    "upload.naming_hint":      {
      zh: "名称仅支持字母 / 数字 / 中文 / 空格以及 . - _，且不能含 \"..\" 或以 \".\" 开头结尾。",
      en: "Names allow letters / digits / Chinese / spaces / . - _, and must not contain \"..\" or start / end with \".\".",
    },
    "upload.engine_label":     { zh: "PDF 提取引擎",       en: "PDF extraction engine" },
    "upload.engine_default":   { zh: "默认 · 毫秒级 · 不解析公式", en: "Default · ms · no formula parsing" },
    "upload.engine_mineru":    { zh: "高质量 · ~10s/页 · LaTeX + 表格", en: "High quality · ~10s/page · LaTeX + tables" },
    "upload.invalid_id_title": { zh: "课程 id 不规范: {cid}", en: "Invalid course id: {cid}" },
    "upload.refresh_lost":     {
      zh: "原始文件已不在内存中（页面已刷新）。请重新选择文件并上传。",
      en: "Original files are no longer in memory (page was refreshed). Please pick the files again.",
    },

    // ── course manager modal ──
    "course_mgr.title":            { zh: "课程显示管理",  en: "Course visibility" },
    "course_mgr.hint": {
      zh: "勾掉的课程会从顶栏下拉里隐藏 — 仅前端过滤，后端 artifacts/courses/ 下的数据完整保留。换浏览器或清 localStorage 后会重置。\n红色 🗑 删除 按钮则是彻底删除：移除磁盘文件 + 索引 + 浏览器缓存，不可撤销。",
      en: "Unchecked courses are hidden from the topbar dropdown — frontend-only filter; backend data in artifacts/courses/ is preserved. Resets if you switch browsers or clear localStorage.\nThe red 🗑 Delete button is a hard delete: it removes files + indices + browser cache. Cannot be undone.",
    },
    "course_mgr.empty":            { zh: "没有课程可管理。", en: "No courses to manage." },
    "course_mgr.delete_btn":       { zh: "🗑 删除",         en: "🗑 Delete" },
    "course_mgr.delete_tooltip":   {
      zh: "彻底删除课程 {cid}（磁盘 + 索引 + 浏览器缓存）",
      en: "Hard-delete course {cid} (disk + indices + browser cache)",
    },
    "course_mgr.show_all":         { zh: "全部显示 ({n})", en: "Show all ({n})" },

    // ── confirm dialogs (window.confirm / window.alert) ──
    "confirm.delete_course": {
      zh: "彻底删除课程 \"{cid}\"？\n\n这会移除：\n - 磁盘上 artifacts/courses/{cid}/ 整个目录\n - 该课程的 FAISS + BM25 索引\n - 浏览器中该课程的所有 localStorage 缓存\n\n该操作不可撤销。若是预置课程，删除后无法回滚（rollback hatch 失效）。",
      en: "Hard-delete course \"{cid}\"?\n\nThis removes:\n - artifacts/courses/{cid}/ on disk\n - FAISS + BM25 indices for this course\n - All browser localStorage cache for this course\n\nThis is irreversible. If this is a pre-bundled course, deleting it disables the rollback hatch.",
    },
    "confirm.delete_course_typeid": {
      zh: "再次确认：请输入完整课程 ID（区分大小写）以执行删除：\n{cid}",
      en: "Type the exact course ID (case-sensitive) to confirm deletion:\n{cid}",
    },
    "confirm.delete_course_mismatch": {
      zh: "输入不匹配：你输入了 \"{typed}\" 但期望 \"{cid}\"。已取消。",
      en: "Mismatch: you typed \"{typed}\" but \"{cid}\" was expected. Cancelled.",
    },
    "confirm.delete_course_done": {
      zh: "已删除课程 \"{cid}\"（{n} 个文件 / 目录）。",
      en: "Course \"{cid}\" deleted ({n} files / directories).",
    },
    "confirm.delete_course_gone": {
      zh: "课程 \"{cid}\" 已不存在（可能在另一标签页已删除）。",
      en: "Course \"{cid}\" no longer exists (maybe deleted in another tab).",
    },
    "confirm.delete_course_failed": {
      zh: "删除失败：{msg}",
      en: "Delete failed: {msg}",
    },

    // ── notes editor / latex compile ──
    "notes.compile_tectonic_missing": {
      zh: "Tectonic 不可用：服务器未安装 LaTeX 编译器。",
      en: "Tectonic unavailable: server has no LaTeX compiler installed.",
    },
    "notes.compile_blocked": {
      zh: "安全检查拦截：{reason}",
      en: "Security check blocked: {reason}",
    },
    "notes.compile_blocked_reason_default": {
      zh: "包含禁止的 LaTeX 命令",
      en: "Contains disallowed LaTeX commands",
    },
    "notes.compile_failed": {
      zh: "LaTeX 编译失败 (exit {exit})：\n{tail}",
      en: "LaTeX compile failed (exit {exit}):\n{tail}",
    },
    "notes.compile_failed_exit_unknown": { zh: "?", en: "?" },
    "notes.compile_timeout": {
      zh: "编译超时（>60s）。文档可能含死循环或复杂的图。",
      en: "Compile timeout (>60s). The document may have an infinite loop or heavy figures.",
    },
    "notes.compile_network_err": {
      zh: "网络错误：{msg}",
      en: "Network error: {msg}",
    },
    "notes.toolbar_locked_edit":    { zh: "Edit 模式下工具栏始终展开", en: "Toolbar is always open in Edit mode" },
    "notes.toolbar_expand":         { zh: "展开工具栏",       en: "Expand toolbar" },
    "notes.toolbar_collapse":       { zh: "隐藏工具栏",       en: "Collapse toolbar" },
    "notes.download_tex":           { zh: ".tex",            en: ".tex" },
    "notes.download_tex_tip":       { zh: "下载 .tex 源文件", en: "Download .tex source" },
    "notes.print_pdf":              { zh: "PDF (print)",     en: "PDF (print)" },
    "notes.print_pdf_tip":          { zh: "浏览器打印（快速预览）", en: "Print via browser (quick preview)" },
    "notes.tectonic_checking":      { zh: "检查 tectonic 状态中…",   en: "Checking tectonic status…" },
    "notes.tectonic_compile":       { zh: "服务端 LaTeX 编译（学术排版）", en: "Server-side LaTeX compile (academic typesetting)" },

    // ── reader / preview ──
    "reader.open_in_reader":       { zh: "在 Reader 中打开 ↗", en: "Open in Reader ↗" },
    "reader.open_in_reader_tip":   { zh: "切换到 Reader 标签页全屏查看", en: "Switch to Reader tab for full view" },
    "reader.preview_close_aria":   { zh: "关闭预览",           en: "Close preview" },
    "reader.preview_close_title":  { zh: "关闭 (Esc)",         en: "Close (Esc)" },
    "reader.source_missing":       {
      zh: "源文件不在磁盘 · 在 Reader 文本视图查看",
      en: "Source file missing on disk · view in Reader text mode",
    },

    // ── upload / processing status ──
    "processing.poll_failed":      { zh: "状态轮询连续失败，请稍后重试", en: "Status polling keeps failing — try again later" },
    "processing.unrecoverable":    { zh: "上传任务已不可恢复，请重试",   en: "Upload task unrecoverable — please retry" },

    // ── settings (large block) ──
    "settings.title":              { zh: "设置",                  en: "Settings" },
    "settings.section_general":    { zh: "通用",                  en: "General" },
    "settings.section_persona":    { zh: "助手身份",              en: "Assistant persona" },
    "settings.section_backend":    { zh: "LLM 后端",              en: "LLM backend" },
    "settings.section_lang":       { zh: "回答语言",              en: "Reply language" },
    "settings.section_embedding":  { zh: "Embedding 模型",        en: "Embedding model" },
    "settings.section_cache":      { zh: "前端缓存",              en: "Frontend cache" },
    "settings.lang_zh_chip":       { zh: "🇨🇳 中文",             en: "🇨🇳 中文" },
    "settings.lang_en_chip":       { zh: "🇺🇸 English",          en: "🇺🇸 English" },
    "settings.lang_current":       { zh: "当前：{label}",         en: "Current: {label}" },
    "settings.lang_label_zh":      { zh: "中文",                  en: "中文" },
    "settings.lang_label_en":      { zh: "English",               en: "English" },
    "settings.lang_unset":         {
      zh: "未设置 — 启动时会弹窗询问",
      en: "Not set — you'll be prompted on launch",
    },
    "settings.clear_cache":        { zh: "清空缓存",              en: "Clear cache" },
    "settings.clear_cache_confirm":{
      zh: "清空所有前端缓存（不包括偏好：language / backend / persona）？",
      en: "Clear all frontend cache (preferences language / backend / persona are kept)?",
    },
    "settings.clear_course_cache_confirm": {
      zh: "清空课程 {cid} 的全部前端缓存？\n（笔记草稿、KG 编辑视图状态、测验答题记录都会丢失，但服务端数据不受影响）",
      en: "Clear all frontend cache for course {cid}?\n(Note drafts, KG view state, and quiz answers will be lost. Server-side data is untouched.)",
    },
    "settings.reset_prefs_confirm": {
      zh: "重置所有偏好（语言、backend、persona、KG 视图、隐藏课程）？\n下次进入需重新选择语言。",
      en: "Reset all preferences (language, backend, persona, KG view, hidden courses)?\nYou'll be prompted to pick a language again on next launch.",
    },

    // ── library (source picker) ──
    "library.select_all":          { zh: "全选",   en: "All" },
    "library.select_none":         { zh: "全不选", en: "None" },
    "library.invert":              { zh: "反选",   en: "Invert" },
    "library.select_all_tip":      { zh: "勾选全部 sources",  en: "Select all sources" },
    "library.select_none_tip":     { zh: "清空所有勾选",      en: "Clear all selections" },
    "library.invert_tip":          { zh: "反选",              en: "Invert selection" },
    "library.shift_hint":          { zh: "Shift+Click 区间选", en: "Shift+Click for range select" },

    // ── exam-prep ──
    "exam.variant_brewing":        { zh: "变体生成中…", en: "Generating variants…" },
    "exam.variant_brewing_tip":    {
      zh: "新题目在后台生成（不阻塞当前页面）。下次开 quiz 时会出现。",
      en: "New questions are generated in the background. They'll appear next time you open a quiz.",
    },
    // ── Exam Prep page ──
    "exam.title":                       { zh: "题库练习", en: "Exam Prep" },
    "exam.empty_select_course":         { zh: "在侧边栏选择一个课程开始题库练习。", en: "Select a course from the sidebar to begin exam preparation." },
    "exam.empty_no_topics":             { zh: "本课程暂无题库。先从课件抽取主题以开始。", en: "No exam bank yet for this course. Extract topics from the course materials to begin." },
    "exam.action.extract_topics":       { zh: "抽取题库主题", en: "Extract Exam Topics" },
    "exam.action.re_extract":           { zh: "重新抽取主题", en: "Re-extract topics" },
    "exam.action.reset":                { zh: "重置题库", en: "Reset bank" },
    "exam.action.start_mixed":          { zh: "开始混合练习 · 所有未掌握主题", en: "Start Mixed Quiz · all non-mastered topics" },
    "exam.tooltip.re_extract":          { zh: "重新跑主题抽取。重命名主题的掌握记录会移到归档。", en: "Run topic extraction again. Mastery history for renamed topics moves to an archive." },
    "exam.confirm.re_extract":          {
      zh: "重新抽取题库主题？已有题目按标准化后的主题名匹配保留；改名了的主题，旧题目会移到归档桶（仍可查看）。继续？",
      en: "Re-extract exam topics? Existing questions are preserved for any topic whose name matches the new extraction (normalized). Topics whose names changed will have their questions moved to an archive bucket you can still see. Continue?",
    },
    "exam.confirm.reset":               { zh: "彻底清空题库？需要重新从头抽取主题。", en: "Wipe the entire exam bank? You'll need to re-extract topics from scratch." },
    "exam.stats.questions_mastered":    { zh: "{done} / {total} 题已掌握", en: "{done} / {total} questions mastered" },
    "exam.stats.topics_mastered":       { zh: "{done} / {total} 主题已全部掌握", en: "{done} / {total} topics fully mastered" },
    "exam.stats.attempts":              { zh: "答过 {n} 次", en: "{n} attempts" },
    "exam.stats.correct_pct":           { zh: "正确率 {pct}%", en: "{pct}% correct" },
    // Busy labels (shown while loading)
    "exam.busy.working":                { zh: "处理中…", en: "Working…" },
    "exam.busy.loading_bank":           { zh: "加载题库…", en: "Loading exam bank…" },
    "exam.busy.extracting":             { zh: "正在从课件抽取题库主题…", en: "Extracting exam topics from course materials…" },
    "exam.busy.sampling":               { zh: "正在从题库抽样题目…", en: "Sampling questions from the bank…" },
    "exam.busy.grading":                { zh: "评分中 · 同步为错题主题生成新变体…", en: "Grading + generating fresh variants for any wrong topics…" },
    "exam.busy.resetting":              { zh: "正在重置题库…", en: "Resetting bank…" },
    "exam.busy.elapsed":                { zh: "已用 {n} 秒", en: "{n}s elapsed" },
    "exam.busy.elapsed_long_hint":      { zh: " — 推理模型可能需要 ~120s 才超时", en: " — reasoning models can take up to 120s before timing out" },
    // Errors
    "exam.error.gen_failed":            { zh: "本主题的题目生成失败。重试一下 —— LLM 可能超时或返回了不合法 JSON。", en: "Question generation failed for this topic. Please retry — the LLM may have timed out or returned malformed JSON." },
    "exam.error.all_mastered":          { zh: "范围内题目全部已掌握。试试重新抽取主题或换一个主题。", en: "All questions in scope are already mastered. Try re-extracting topics or pick a different topic." },
    "exam.error.no_questions":          { zh: "暂无可用题目 —— 试试给该主题播种题目或检查课程 KB 有没有内容。", en: "No questions available — try seeding this topic or check the course KB has content." },
    "exam.error.answer_at_least_one":   { zh: "提交前至少回答一题。", en: "Please answer at least one question before submitting." },
    "exam.error.failed_load_bank":      { zh: "加载题库失败", en: "Failed to load exam bank" },
    "exam.error.failed_extract":        { zh: "主题抽取失败", en: "Topic extraction failed" },
    "exam.error.failed_start":          { zh: "启动练习失败", en: "Failed to start quiz" },
    "exam.error.failed_submit":         { zh: "提交失败", en: "Submit failed" },
    "exam.error.failed_reset":          { zh: "重置失败", en: "Reset failed" },
    "exam.info.re_extract_done":        {
      zh: "重新抽取完成 · {migrated} 个主题带题保留 · {orphans} 道孤立题归档（在 \"[archive] ...\" 主题里可查看）",
      en: "Re-extract complete · {migrated} topic(s) carried questions forward · {orphans} orphan questions archived (visible in a \"[archive] ...\" topic).",
    },
    // Topic card
    "exam.topic.weight":                { zh: "权重 · {pct}%", en: "weight · {pct}%" },
    "exam.topic.mastered_count":        { zh: "{done} / {total} 已掌握", en: "{done} / {total} mastered" },
    "exam.topic.attempts":              { zh: "答过 {n} 次", en: "{n} attempt(s)" },
    "exam.topic.correct_rate":          { zh: "正确率 {pct}%", en: "{pct}% correct" },
    "exam.topic.mastered_chip":         { zh: "✓ 已掌握", en: "✓ mastered" },
    "exam.topic.re_quiz_tip":           { zh: "再练一次本主题（已掌握）", en: "Re-quiz this topic (already mastered)" },
    "exam.topic.start_quiz_tip":        { zh: "开始练习 {name}", en: "Start quiz on {name}" },
    "exam.topic.review_btn":            { zh: "回顾已掌握 →", en: "Review mastered →" },
    "exam.topic.start_btn":             { zh: "练习本主题 →", en: "Quiz on this topic →" },
    "exam.archive.label":               { zh: "归档 · {n} 个旧主题桶（来自历次重抽取，不再被新练习抽中）。", en: "Archive · {n} bucket(s) of orphan questions from previous re-extracts (not sampled for new quizzes)." },
    // Quiz view
    "exam.quiz.title":                  { zh: "练习 · 共 {n} 题", en: "Quiz · {n} questions" },
    "exam.quiz.scoped_topic":           { zh: " · 限定到单个主题", en: " · scoped to topic" },
    "exam.quiz.placeholder":            { zh: "在这里写你的答案…", en: "Your answer…" },
    "exam.quiz.back":                   { zh: "返回", en: "Back" },
    "exam.quiz.submit":                 { zh: "提交 · 评分 {n} 题", en: "Submit · grade {n} answer(s)" },
    // Result view
    "exam.result.correct":              { zh: "答对", en: "correct" },
    "exam.result.wrong":                { zh: "答错", en: "wrong" },
    "exam.result.score":                { zh: "得分", en: "score" },
    "exam.result.fresh_variants":       { zh: "新变体题已生成", en: "fresh variants generated" },
    "exam.result.fresh_variants_tip":   { zh: "自演化：每个错题主题各生成 {n} 个新变体", en: "Self-evolution: {n} variants per wrong topic" },
    "exam.result.your_answer":          { zh: "你的答案：", en: "Your answer:" },
    "exam.result.empty_answer":         { zh: "（未作答）", en: "(empty)" },
    "exam.result.expected":             { zh: "参考答案：", en: "Expected:" },
    "exam.result.why":                  { zh: "解析：", en: "Why:" },
    "exam.result.back_topics":          { zh: "返回主题列表", en: "Back to Topics" },
    "exam.result.another_round":        { zh: "再来一轮", en: "Another Round" },

    // ── processing (upload progress) ──
    "processing.sec_suffix":       { zh: "秒",  en: "s" },
    "processing.min_suffix":       { zh: "分",  en: "m" },
    "processing.hour_suffix":      { zh: "小时", en: "h" },
    "processing.elapsed":          { zh: "已用",       en: "Elapsed" },
    "processing.remaining":        { zh: "剩余 ~{t}",  en: "~{t} remaining" },
    "processing.pages_total":      { zh: "共 {n} 页",  en: "{n} pages total" },
    "processing.estimate_about":   { zh: "估算约",     en: "Estimated" },
    "processing.pages_progress":   { zh: "{done} / {total} 页", en: "{done} / {total} pages" },
    "processing.with_pptx_render": { zh: "渲染 {n} 个 PPTX 预览", en: "rendering {n} pptx preview(s)" },
    "processing.failed_at":        { zh: "上传管道在 {stage} 阶段失败", en: "Upload pipeline failed at stage {stage}" },
    // Stage rows shown in the upload overlay (5 stages, lbl + sub each).
    "processing.stage.extracting.lbl":  { zh: "解析中",       en: "Extracting" },
    "processing.stage.extracting.sub":  { zh: "MinerU / PyMuPDF · 页 → 文本", en: "MinerU / PyMuPDF · pages → text" },
    "processing.stage.chunking.lbl":    { zh: "切片中",       en: "Chunking" },
    "processing.stage.chunking.sub":    { zh: "切成 1.5KB 段", en: "Segmenting into 1.5KB chunks" },
    "processing.stage.embedding.lbl":   { zh: "向量化",       en: "Embedding" },
    "processing.stage.embedding.sub":   { zh: "FAISS 向量 + BM25 索引", en: "FAISS vector + BM25 index" },
    "processing.stage.kg_stage_a.lbl":  { zh: "知识图 A 阶段",  en: "KG Stage A" },
    "processing.stage.kg_stage_a.sub":  { zh: "宏观主题 + 课程概览", en: "Macro topics + course overview" },
    "processing.stage.kg_stage_b.lbl":  { zh: "知识图 B 阶段",  en: "KG Stage B" },
    "processing.stage.kg_stage_b.sub":  { zh: "逐 chunk 概念 + 关系", en: "Per-chunk concepts + relations" },

    // ── reader (PDF outline toggle) ──
    "reader.show_outline":         { zh: "📑 显示索引", en: "📑 Show outline" },
    "reader.hide_outline":         { zh: "📑 隐藏索引", en: "📑 Hide outline" },
    "reader.show_outline_tip":     { zh: "显示 PDF 书签 / 缩略图侧栏", en: "Show PDF outline / thumbnails sidebar" },
    "reader.hide_outline_tip":     { zh: "隐藏 PDF 书签 / 缩略图侧栏", en: "Hide PDF outline / thumbnails sidebar" },

    // ── mindmap (knowledge graph) ──
    "mindmap.new_node":            { zh: "新节点",    en: "New node" },
    "mindmap.filtered_all":        { zh: "已过滤所有关系 · isolated nodes remain", en: "All relations filtered · isolated nodes remain" },
    "mindmap.show_legend":         { zh: "显示图例",  en: "Show legend" },
    "mindmap.hide_legend":         { zh: "隐藏图例",  en: "Hide legend" },

    // ── assistant (chat sidebar) ──
    "assistant.default_persona":      { zh: "学习助手",          en: "Study Assistant" },
    "assistant.persona_desc":         { zh: "学习助手 · 课堂材料问答", en: "Study assistant · course material Q&A" },
    "assistant.placeholder_normal":   { zh: "向{name}提问…",     en: "Ask {name} a question…" },
    "assistant.placeholder_thinking": { zh: "Esc 取消 · Shift+Enter 换行", en: "Esc to cancel · Shift+Enter for newline" },
    "assistant.send":                 { zh: "发送 (Enter)",       en: "Send (Enter)" },
    "assistant.cancel":               { zh: "取消 (Esc)",         en: "Cancel (Esc)" },
    "assistant.hide_suggestions":     { zh: "隐藏快捷提问（可再展开）", en: "Hide quick suggestions (toggleable later)" },
    "assistant.show_suggestions":     { zh: "显示快捷提问",        en: "Show quick suggestions" },
    "assistant.rewrite_tip":          {
      zh: "后台将后续问题改写成独立检索查询",
      en: "Backend rewrote your follow-up question into a self-contained retrieval query",
    },
    "assistant.error_connect":        { zh: "连接后端失败",        en: "Failed to connect to backend" },
    "assistant.error_prefix":         { zh: "错误: {msg}",         en: "Error: {msg}" },
    "assistant.step_searching":       { zh: "正在搜索知识库",      en: "Searching knowledge base" },

    // ── Reader "no source loaded" welcome / operation guide ──
    "reader.welcome.chapter":         { zh: "nano-NOTEBOOKLM", en: "nano-NOTEBOOKLM" },
    "reader.welcome.title":           { zh: "欢迎使用 nano-NOTEBOOKLM", en: "Welcome to nano-NOTEBOOKLM" },
    "reader.welcome.sub":             { zh: "上传课件或选择一个课程开始 · 下面是操作指南", en: "Upload course materials or select a course to begin · operation guide below" },
    "reader.welcome.intro":           {
      zh: "nano-NOTEBOOKLM 是一个跑在你自己机器上的学习助手。上传课件之后，它会自动建好知识图谱和向量索引，让你用带引用的对话、结构化笔记、自动出题的方式来学。下面是从零开始到进阶用法的完整流程。",
      en: "nano-NOTEBOOKLM is a self-hosted study assistant: upload course materials → automatic knowledge graph + vector index → ask questions with citations, generate structured notes, and practice with a self-evolving quiz bank. This guide walks through the whole flow, from first upload to advanced usage.",
    },

    // 1. Getting started
    "reader.welcome.h1":              { zh: "上手起步", en: "Getting Started" },
    "reader.welcome.s11_h":           { zh: "添加你的第一个课程", en: "Add your first course" },
    "reader.welcome.s11_p":           {
      zh: "在左侧\"课程库\"面板点 \"+\" 按钮，也可以直接把文件拖进去。弹出的窗口里要做三件事：起一个课程名、选解析引擎、选要上传的文件（PDF / PPTX / DOCX / Markdown 都支持）。\n\n解析引擎选哪个？PyMuPDF 是默认选项，几毫秒就能解析一页，适合纯文字课件。MinerU 慢得多——在 CPU 上每页要 10 秒左右——但它能完整保留 LaTeX 公式和表格。如果你的 PPT 里有数学公式或者复杂表格，选 MinerU。\n\n确认后后台会跑五个阶段：抽取文本 → 切片 → 嵌入向量 → 提取主题 → 提取概念。页面上的进度条会显示当前阶段和大致剩余时间。",
      en: "Click \"+\" in the left Library panel (or drag files onto it) → Course Picker opens: name a course → pick the extractor engine (PyMuPDF is fast at ~0.05s/page; MinerU is slow at ~10s/page but recovers LaTeX equations and HTML tables — strongly recommended for slide decks with formulas) → pick the PDF / PPTX / DOCX / MD files to upload → confirm. The background pipeline runs five stages: extracting → chunking → embedding → KG Stage A (topics) → Stage B (leaf concepts). Progress bar and ETA shown live.",
    },
    "reader.welcome.s12_h":           { zh: "配置 LLM provider", en: "Configure an LLM provider" },
    "reader.welcome.s12_p":           {
      zh: "点右上角的齿轮图标进入设置页，找到 \"AI Backend & Models\" 一节，再点 \"+ 添加 provider\" 按钮。\n\n弹出来的表单里有一个供应商下拉，预置了常见的几家：OpenAI、DeepSeek、Moonshot（Kimi）、智谱（GLM）、MiniMax、Groq、Together、Gemini、Anthropic Claude，以及本地跑的 Ollama / vLLM / LM Studio。选好之后，base URL 和默认模型名会自动填好。\n\nAPI key 建议选\"从环境变量读取\"——你只填变量名（比如 OPENAI_API_KEY），真正的密钥放在项目根目录的 .env 文件里，永远不会出现在 UI 或者落到磁盘配置里。如果实在不方便用环境变量，再选\"直接粘贴 key\"，密钥会以 0600 权限存到本地配置文件，只有当前用户能读。\n\n保存之后可以继续加第二个、第三个供应商。顶栏右上角的模型按钮（带 🤖 / 🧠 / 💻 图标的那个胶囊）显示当前默认 provider，点一下会循环切换到下一个。每一行右边都有一个\"测试\"按钮——5 秒内会告诉你这个 provider 通不通、key 对不对、返回的模型名是不是预期的。",
      en: "Top-right gear icon → Settings → AI Backend & Models. Click \"+ Add provider\" → pick a vendor from the preset dropdown (OpenAI / DeepSeek / Moonshot / Zhipu / MiniMax / Groq / Together / Gemini / Anthropic Claude / local Ollama / vLLM / LM Studio) → base URL and model auto-fill → API key: prefer \"From env var\" (the key stays in .env, never lands on disk) → save. You can configure multiple providers; the topbar chip cycles between them, and each row has a \"Test\" button for a 5-second connectivity probe.",
    },

    // 2. Core features
    "reader.welcome.h2":              { zh: "核心功能", en: "Core Features" },
    "reader.welcome.s21_h":           { zh: "Assistant — 带引用的提问", en: "Assistant — Q&A with citations" },
    "reader.welcome.s21_p":           {
      zh: "右侧的 Assistant 面板就是和课程内容对话的地方。提问可以针对当前选中的课程，也可以选\"全部课程\"做跨课程检索。\n\n回答里的方括号引用（比如 [s1]、[s2]）都是可点的链接，点一下会跳到 Reader 标签页里对应 PDF 的那一页并高亮相关段落。后台会根据你问的内容自动走不同的检索路径——可能是知识图谱、向量检索、跨课程汇总，或者直接走通用模型回答。你不用关心走的是哪条，回答下方会显示这次走的路径名。\n\n如果想换个模型回答这一次，比如普通问题用快的、复杂推理换成强的，直接点顶栏的模型按钮切换就行，不用回设置页。",
      en: "Right-side Assistant panel — ask anything about the active course or all courses. Citation chips in the reply (e.g. [s1] [s2]) are clickable → auto-jump to the Reader tab at the source page. Five retrieval paths: intent router → graphrag (KG-augmented retrieval) → RAG (BM25 + vector hybrid) → translate → cross-course → general. The topbar chip switches which LLM provider answers this turn.",
    },
    "reader.welcome.s22_h":           { zh: "Reader — 浏览原文", en: "Reader — browse source content" },
    "reader.welcome.s22_p":           {
      zh: "Reader 标签页就是原始课件的浏览器。PDF 直接用浏览器自带的 PDFium 渲染，翻页、全文搜索、缩放都和系统的 PDF 阅读器一样。PPTX 上传时会被 LibreOffice 转成 PDF 副本——这个副本只用于 Reader 里的预览，不影响后台抽出来的 chunks。\n\n从 Assistant 里点引用跳过来时，对应的内容块会自动高亮。左侧那个大纲按钮可以折叠或展开 PDF 自带的书签和缩略图栏。",
      en: "Switch to the Reader tab to browse the original course materials. PDFs use the browser's PDF.js / PDFium viewer (turn pages, full-text search). PPTX renders via a LibreOffice-generated PDF sidecar. Clicking a citation in the Assistant auto-highlights the corresponding chunk. The outline toggle on the left collapses/expands PDF bookmarks and thumbnails.",
    },
    "reader.welcome.s23_h":           { zh: "Notes — 结构化笔记生成", en: "Notes — structured note generation" },
    "reader.welcome.s23_p":           {
      zh: "Notes 标签页点\"生成笔记\"按钮，它会一个文件一个文件地流式生成结构化的 LaTeX 笔记，全部生成完后再过一遍 review，把不通顺、漏的、重复的地方修一遍。公式用浏览器里的 KaTeX 实时渲染，所见即所得。\n\n如果机器上装了 tectonic（一个轻量的 LaTeX 编译器），点导出按钮就能直接编译成 PDF。\n\n每一节都有独立缓存，下次再点生成只会处理新内容，不会重复跑已经生成过的。如果你想强制全部重新生成，点旁边的\"重新生成\"按钮可以绕过缓存。",
      en: "Notes tab → click \"Generate Notes\" → per-file streaming LaTeX note generation + a review pass for polish. KaTeX renders math in-browser. If `tectonic` is installed, a one-click compile to PDF is available. Each section is independently cached — re-generating only touches new content. The force-regenerate button bypasses the cache.",
    },
    "reader.welcome.s24_h":           { zh: "Knowledge Graph — 可编辑知识图谱", en: "Knowledge Graph — editable concept map" },
    "reader.welcome.s24_p":           {
      zh: "Mindmap 标签页显示从课件自动抽取的知识图谱。抽取分两步进行：先识别课程的几个主题（Stage A），再在每个主题下抽叶子概念（Stage B），最终是一个带层级的图。布局用 d3 的力导图，节点可以拖来拖去，连线会自动重新排布。\n\n手动操作有这几个：双击节点能编辑名字和定义；按住 Shift 把一个节点拖到另一个上可以加一条边；选中某个节点按 N 添加子节点；按 Del 删除。\n\n所有手动改动会以 overlay 的形式叠在自动抽取结果之上——也就是说重新抽取这门课程时，你手编的部分不会被清掉，会自动合并回来。",
      en: "Mindmap tab shows concepts + relations auto-extracted from the course materials (two-stage: topics → leaf concepts), laid out interactively with d3-force. Double-click to edit, shift-drag to add an edge, press N for a child node, Del to remove. Manual edits are stored as an overlay on top of automatic extraction — re-extraction never clobbers your handiwork.",
    },
    "reader.welcome.s25_h":           { zh: "Exam Prep — 自演化题库", en: "Exam Prep — self-evolving question bank" },
    "reader.welcome.s25_p":           {
      zh: "Exam Prep 标签页是带\"自演化\"机制的题库。第一次进入时它会先抽取课程主题，再给每个主题生成一组题（选择题和简答题混合）。答完点提交后 AI 评分，给出对错和解析。\n\n关键来了：**每答错一道题，系统会自动针对那个主题再生成几道新变体**塞回题库。所以你练得越多、错得越多，题库就越针对你的薄弱点，每次抽题都有新的。每个主题独立追踪掌握进度，掌握的主题会被打上勾，下次默认从未掌握的主题里抽。",
      en: "Exam Prep tab: auto-extracts course topics, generates a set of questions per topic (multiple choice + short answer), grades with AI. **For every wrong answer, the system auto-generates fresh variants targeting that topic** and adds them to the bank — the more you practice, the more focused the bank gets on your weak spots. Per-topic mastery is tracked.",
    },

    // 3. Advanced
    "reader.welcome.h3":              { zh: "进阶用法", en: "Advanced" },
    "reader.welcome.s31_h":           { zh: "跨课程检索", en: "Cross-course retrieval" },
    "reader.welcome.s31_p":           {
      zh: "左侧课程列表顶部有一个\"全部课程\"选项。选中之后，Assistant 会同时跨所有上传过的课程做检索。\n\n这个功能适合期末复习——比如把整学期的资料当成一个统一的知识库来问：\"机器学习和数据库里都讲过的索引，是同一个东西吗？\"系统会从两门课里都抽相关内容，对照回答。需要限定在某一门课时，点对应的课程标签即可。",
      en: "Click \"All Courses\" at the top of the sidebar (the default) to have the Assistant retrieve across every uploaded course at once. Useful for end-of-term review, treating the whole semester as one KB. Single-course scoping is one click away.",
    },
    "reader.welcome.s32_h":           { zh: "Embedding 模型切换", en: "Switch embedding model" },
    "reader.welcome.s32_p":           {
      zh: "设置页里有一节叫\"Embedding 模型\"，三档可选：\n\n• **本地 MiniLM**（默认）：多语言、零配置、384 维。在 CPU 上也跑得很快，但对学术中英跨语言检索一般。\n• **OpenAI text-embedding-3-large**：调 API，3072 维。中英跨语言最强，但每个文本块都要花一次 API 调用（按字数计费）。\n• **BGE-M3**：本地模型，1024 维。首次启动会下载约 2GB 的权重，之后纯本地跑，质量在 MiniLM 和 OpenAI 之间。\n\n切换是\"路由\"不是\"重建\"：每个预设在磁盘上有独立的索引目录，切回之前用过的预设是秒级的。切到一个新预设时，后台会重建索引（页面上有横幅提示进度），期间检索会临时退化到 BM25 关键词匹配，不影响使用。",
      en: "Settings → Embedding: three presets — local MiniLM (multilingual, zero-config, 384-dim), OpenAI text-embedding-3-large (API, 3072-dim, strongest for ZH-EN cross-lingual), BGE-M3 (local, strong multilingual, 1024-dim, ~2GB first download). Switching is a path-route, not a rebuild: each preset keeps its own FAISS namespace — switching back to a previous preset is instant.",
    },
    "reader.welcome.s33_h":           { zh: "命令行批处理", en: "CLI batch processing" },
    "reader.welcome.s33_p":           {
      zh: "项目根目录的 scripts/ 文件夹下放了几个命令行工具：\n\n• ingest_course.py：把本地某个文件夹的全部文件批量摄入成一门课程\n• build_indices.py：手动改了 chunks.json 之后，用它重建 FAISS 和 BM25 索引\n• reembed_all.py：用当前 embedding 预设给所有课程重新跑一遍嵌入\n\n适合从已有的资料库一次性把几十门课迁过来，或者升级 embedding 模型后做一次全局 re-embed。",
      en: "scripts/ directory: `ingest_course.py` batch-ingest a directory, `build_indices.py` rebuild FAISS/BM25 indices, `reembed_all.py` re-embed everything under the current preset. Useful for bulk-migrating an existing library.",
    },

    // 4. Troubleshooting
    "reader.welcome.h4":              { zh: "故障排查", en: "Troubleshooting" },
    "reader.welcome.s41_p":           {
      zh: "**上传进度条不动了？** 打开终端跑 `tail -f /tmp/nano-server.log` 看后台日志。如果一直在刷 mineru-api 相关的行，说明 MinerU 在慢慢解析（CPU 上每页 10 秒左右，属于正常）；如果完全没新日志、或者反复重启某个阶段，那是真挂了，刷新页面就好——后台任务有 1 小时 TTL，浏览器关掉再开都能续上。",
      en: "Upload stuck? Check server logs (`tail -f /tmp/nano-server.log`) to see whether mineru is actually grinding or genuinely hung. The frontend polls `/api/upload/status/<task_id>` every 1.5 s.",
    },
    "reader.welcome.s42_p":           {
      zh: "**回答和你想要的差很远？** 先看顶栏有没有亮起的\"当前文件\"按钮——它会把检索限定到某个文件，可能你之前点过某个文件忘记关掉。再不行，确认这门课的 chunks 数是否合理（在课程主页能看到，应该是几十到几百），数字异常小说明抽取没跑完。",
      en: "Wrong file in the answer's citation? The topbar's \"active source\" chip may be over-constraining; or re-extract the course (Settings → reindex). If that fails, check `/api/sources/<course_id>` — does the chunk count match what you expect?",
    },
    "reader.welcome.s43_p":           {
      zh: "**Reader 或 Notes 里的公式和表格丢了？** 大概率是那个文件当初用 PyMuPDF 抽的（默认引擎，速度优先），公式被当成乱码丢弃。解决办法：把当前课程删掉重新上传，在弹窗里选 MinerU 引擎；或者保留课程、重新上传同名文件并切换引擎，系统会检测到引擎变化自动重新抽取。",
      en: "Formulas or tables missing? That deck was extracted with PyMuPDF (the default), which drops math. Delete + re-upload the course and pick MinerU in the Course Picker; or change the engine which triggers a re-extract (the old chunks are dropped).",
    },
    "reader.welcome.s44_p":           {
      zh: "**提问没反应或者一直转圈？** 多半是 LLM provider 那一侧的问题。回到设置页找到对应那一行 provider，点\"测试\"按钮：返回 401 说明 key 错了或过期了；返回 timeout 说明网络不通 / VPN 没开 / base URL 拼错了；返回 connection refused 说明本地的 Ollama / vLLM 服务没起来。如果一切看起来都对但还是不通，再去看 `/tmp/nano-server.log` 里有没有报错。",
      en: "Provider API failing? In Settings, click the \"Test\" button on that provider's row (5-second timeout ping) to see whether it's 401 / timeout / network. Prefer `env:VAR` for `api_key_ref` so the key stays in .env; use `literal:` only when env vars aren't available.",
    },
    "assistant.step_retrieving":      { zh: "正在检索相关段落",    en: "Retrieving relevant passages" },
    "assistant.step_generating":      { zh: "正在生成回答",        en: "Generating answer" },
    "assistant.step_formatting":      { zh: "正在格式化响应",      en: "Formatting response" },
    "assistant.sug.summarize":        { zh: "总结这门课",          en: "Summarize this course" },
    "assistant.sug.key_concepts":     { zh: "有哪些关键概念？",    en: "What are the key concepts?" },
    "assistant.sug.list_defs":        { zh: "列出全部定义",        en: "List all definitions" },
    "assistant.sug.gen_notes":        { zh: "生成学习笔记",        en: "Generate study notes" },
    "assistant.sug.gen_quiz":         { zh: "生成测验题",          en: "Generate quiz" },
    "assistant.sug.build_kg":         { zh: "构建知识图谱",        en: "Build knowledge graph" },
    "assistant.sug.exam_analysis":    { zh: "考点分析",            en: "Exam analysis" },
    "assistant.sug.course_report":    { zh: "课程报告",            en: "Course report" },
    "assistant.sug.mastery":          { zh: "掌握度看板",          en: "Mastery dashboard" },
    "assistant.sug.rewrite_shorter":  { zh: "改写得更简洁",        en: "Rewrite shorter" },
    "assistant.sug.add_examples":     { zh: "添加例题",            en: "Add worked examples" },
    "assistant.sug.quiz_from_notes":  { zh: "从笔记生成测验",      en: "Generate quiz from notes" },
    "assistant.sug.what_concept":     { zh: "这个概念是什么？",    en: "What is this concept?" },
    "assistant.sug.find_prereqs":     { zh: "查找前置知识",        en: "Find prerequisites" },
    "assistant.sug.explain_rels":     { zh: "解释关系",            en: "Explain relationships" },
    "assistant.sug.new_quiz":         { zh: "生成新测验",          en: "Generate new quiz" },
    "assistant.sug.focus_weak":       { zh: "聚焦薄弱环节",        en: "Focus on weak areas" },
    "assistant.sug.make_harder":      { zh: "提高难度",            en: "Make it harder" },
    "assistant.sug.explain_answers":  { zh: "讲解答案",            en: "Explain the answers" },
    "assistant.action.building_kg":   { zh: "正在构建知识图谱…",   en: "Building knowledge graph…" },
    "assistant.action.gen_notes":     { zh: "正在生成学习笔记…",   en: "Generating study notes…" },
    "assistant.action.gen_quiz":      { zh: "正在生成练习题…",     en: "Generating practice quiz…" },

    // ── tweaks panel ──
    "tweaks.close":                   { zh: "关闭调节",            en: "Close tweaks" },

    // ── settings badges + section headers + body copy ──
    "settings.badge.configured":      { zh: "已配置",              en: "Configured" },
    "settings.badge.unconfigured":    { zh: "未配置",              en: "Not configured" },
    "settings.badge.loading":         { zh: "加载中…",             en: "Loading…" },
    "settings.head_sub":              {
      zh: "应用偏好集中页。API key 与模型 ID 由后端 .env 管理 — 此处仅显示状态，请直接编辑 .env 修改密钥与默认模型。",
      en: "Central preferences page. API keys + model IDs live in the server's .env — this page only shows status. Edit .env directly to change keys / default models.",
    },

    "settings.section.ai":            { zh: "AI 后端 / 模型", en: "AI Backend & Models" },
    "settings.section.ai_hint":       {
      zh: "可在此添加 / 编辑 / 删除 LLM provider · 改完无需重启 · API key 推荐用 env:VAR 形式（避免落盘）",
      en: "Add / edit / remove LLM providers here · no restart needed · prefer env:VAR refs over literal keys",
    },
    "settings.providers.col.label":   { zh: "名称",        en: "Name" },
    "settings.providers.col.kind":    { zh: "类型",        en: "Kind" },
    "settings.providers.col.model":   { zh: "模型",        en: "Model" },
    "settings.providers.col.base_url":{ zh: "Base URL",   en: "Base URL" },
    "settings.providers.col.status":  { zh: "状态",        en: "Status" },
    "settings.providers.col.actions": { zh: "操作",        en: "Actions" },
    "settings.providers.kind.openai_compat":        { zh: "OpenAI 兼容",       en: "OpenAI-compatible" },
    "settings.providers.kind.openai_compat_local":  { zh: "本地（OpenAI 兼容）", en: "Local (OpenAI-compatible)" },
    "settings.providers.kind.anthropic":            { zh: "Anthropic Claude",   en: "Anthropic Claude" },
    "settings.providers.badge.default":     { zh: "默认", en: "default" },
    "settings.providers.badge.key_ok":      { zh: "已配置 key", en: "key set" },
    "settings.providers.badge.key_missing": { zh: "未配置 key", en: "no key" },
    "settings.providers.badge.disabled":    { zh: "已停用", en: "disabled" },
    "settings.providers.action.test":       { zh: "测试", en: "Test" },
    "settings.providers.action.edit":       { zh: "编辑", en: "Edit" },
    "settings.providers.action.delete":     { zh: "删除", en: "Delete" },
    "settings.providers.action.set_default":{ zh: "设为默认", en: "Set default" },
    "settings.providers.action.cancel":     { zh: "取消", en: "Cancel" },
    "settings.providers.action.save":       { zh: "保存", en: "Save" },
    "settings.providers.add":               { zh: "+ 添加 provider", en: "+ Add provider" },
    "settings.providers.form.id":           { zh: "ID（小写字母数字 / -）", en: "ID (lowercase letters, digits, -)" },
    "settings.providers.form.label":        { zh: "显示名", en: "Display name" },
    "settings.providers.form.api_key_ref":  { zh: "API key ref（env:VAR 或 literal:...，推荐前者）", en: "API key ref (env:VAR or literal:...; prefer env)" },
    "settings.providers.form.base_url":     { zh: "Base URL（OpenAI 兼容必填）", en: "Base URL (required for OpenAI-compatible)" },
    "settings.providers.form.preset":       { zh: "供应商预设（选一个一键填好 base URL / 模型 / key ref）", en: "Vendor preset (one-click fills base URL / model / key ref)" },
    "settings.providers.preset.custom":     { zh: "自定义（手动填）", en: "Custom (manual)" },
    "settings.providers.api_key.label":     { zh: "API key", en: "API key" },
    "settings.providers.api_key.placeholder": { zh: "sk-... 直接粘贴你的 API key", en: "sk-... paste your API key" },
    "settings.providers.api_key.inherits_env": {
      zh: "留空即从环境变量 {var} 读取（在 .env 设置）。粘贴 key 在此可覆盖。",
      en: "Leave blank to read from env var {var} (set in .env). Paste a key here to override.",
    },
    "settings.providers.api_key.inherits_literal": {
      zh: "留空保留当前已存储的 key。重新粘贴可替换。",
      en: "Leave blank to keep the currently stored key. Paste here to replace.",
    },
    "settings.providers.api_key.literal_warn": {
      zh: "key 会明文存入 artifacts/providers.json（文件权限 0600，仅当前用户可读）。",
      en: "Stored inline in artifacts/providers.json (file mode 0600, owner-only).",
    },
    "settings.providers.test.running":      { zh: "测试中…", en: "Testing…" },
    "settings.providers.test.ok":           { zh: "✓ {ms}ms", en: "✓ {ms}ms" },
    "settings.providers.test.fail":         { zh: "✗ {err}", en: "✗ {err}" },
    "settings.providers.confirm_delete":    { zh: "确认删除 provider {id}？", en: "Delete provider {id}?" },
    "settings.providers.error":             { zh: "操作失败：{msg}", en: "Operation failed: {msg}" },
    "settings.providers.empty":             { zh: "尚未配置任何 provider · 用下面的表单添加一个", en: "No providers yet · add one with the form below" },
    "settings.providers.api_key_ref_disabled_hint": {
      zh: "（保留原值留空 = 不修改）",
      en: "(leave blank to keep current value)",
    },
    "settings.tag.main_path":         { zh: "主路径",              en: "Primary" },
    "settings.field.model":           { zh: "模型",                en: "Model" },
    "settings.field.base_url":        { zh: "base URL",            en: "base URL" },
    "settings.badge.unconfigured_claude":  { zh: "未配置 ANTHROPIC_API_KEY", en: "ANTHROPIC_API_KEY not set" },
    "settings.badge.unconfigured_local":   { zh: "未配置 LOCAL_LLM_BASE_URL", en: "LOCAL_LLM_BASE_URL not set" },
    "settings.local_tag":             { zh: "Ollama / vLLM / LM Studio", en: "Ollama / vLLM / LM Studio" },
    "settings.local_setup_hint": {
      zh: "在 .env 设置 LOCAL_LLM_BASE_URL + LOCAL_LLM_MODEL 启用本地模型",
      en: "Set LOCAL_LLM_BASE_URL + LOCAL_LLM_MODEL in .env to enable a local model",
    },
    "settings.local_endpoint":        { zh: "endpoint",            en: "endpoint" },

    "settings.section.embedding":     { zh: "Embedding 模型",       en: "Embedding Model" },
    "settings.section.embedding_hint": {
      zh: "切换会按需在后台重建索引 · 切回旧选项是秒切（每个预设保留独立索引）",
      en: "Switching triggers a background rebuild · Switching back is instant (each preset keeps its own index)",
    },
    "settings.rebuild.running_title": { zh: "正在重建索引",         en: "Rebuilding index" },
    "settings.rebuild.preset":        { zh: "预设",                en: "Preset" },
    "settings.rebuild.progress":      { zh: "进度",                en: "Progress" },
    "settings.rebuild.current_course":{ zh: "当前",                en: "Current" },
    "settings.rebuild.running_hint": {
      zh: "期间问答可用，但未重建课程的语义检索会临时退化为 BM25-only。",
      en: "Chat stays usable, but semantic search on un-rebuilt courses temporarily falls back to BM25-only.",
    },
    "settings.rebuild.done":          { zh: "✓ 索引已重建至 {preset}（{n} 门课程）", en: "✓ Index rebuilt to {preset} ({n} courses)" },
    "settings.rebuild.partial_title": { zh: "⚠ 部分重建失败",       en: "⚠ Partial rebuild failed" },
    "settings.rebuild.partial_count": { zh: "（{done}/{total} 完成）", en: "({done}/{total} completed)" },
    "settings.rebuild.failed_label":  { zh: "失败课程：",           en: "Failed courses:" },
    "settings.rebuild.partial_hint":  {
      zh: "可重新选这个预设触发重试，或检查服务端日志。",
      en: "Re-select this preset to retry, or check the server log.",
    },
    "settings.rebuild.error":         { zh: "重建失败：{msg}",      en: "Rebuild failed: {msg}" },
    "settings.embed.switch_failed":   { zh: "切换失败：{msg}",      en: "Switch failed: {msg}" },
    "settings.preset.tag_api":        { zh: "API",                 en: "API" },
    "settings.preset.tag_local":      { zh: "本地",                en: "Local" },
    "settings.preset.switching":      { zh: "切换中…",             en: "Switching…" },
    "settings.preset.unconfigured":   { zh: "未配置 EMBEDDING_API_KEY", en: "EMBEDDING_API_KEY not set" },
    "settings.preset.first_download": { zh: "首次下载约 {gb} GB",   en: "First download ~{gb} GB" },
    "settings.preset.custom_hint": {
      zh: "⚠ 当前 EMBEDDING_MODEL 是 env 自定义值（{model}），不属于任何预设。选一个预设后会持久化并覆盖 env 默认。",
      en: "⚠ Current EMBEDDING_MODEL is a custom env value ({model}), not a preset. Picking a preset persists the choice and overrides the env default.",
    },
    // Preset descriptions — keyed by preset_id so frontend can render the
    // right language. Backend config.py still ships a description string for
    // CLI / API consumers, but the UI overrides it with these.
    "settings.preset.desc.local_mini": {
      zh: "本地 sentence-transformers · 多语言 · 0 配置",
      en: "Local sentence-transformers · multilingual · zero config",
    },
    "settings.preset.desc.openai_large": {
      zh: "OpenAI 兼容 /v1/embeddings · text-embedding-3-large · 需要 API key",
      en: "OpenAI-compatible /v1/embeddings · text-embedding-3-large · API key required",
    },
    "settings.preset.desc.bge_m3": {
      zh: "BAAI/bge-m3 本地多语言强力模型 · 首次下载 ~2GB",
      en: "BAAI/bge-m3 strong local multilingual model · ~2 GB first download",
    },
    "settings.row.embed_warm":        { zh: "Embedding 预热",       en: "Embedding warm-up" },
    "settings.warm.warming":          { zh: "预热中…",             en: "warming…" },
    "settings.warm.ok":               { zh: "ok",                  en: "ok" },
    "settings.warm.failed":           { zh: "失败",                en: "failed" },
    "settings.row.tectonic":          { zh: "Tectonic (PDF 编译)",  en: "Tectonic (PDF compile)" },
    "settings.row.pptx_pdf":          { zh: "PPTX → PDF (LibreOffice)", en: "PPTX → PDF (LibreOffice)" },
    "settings.badge.available":       { zh: "可用",                en: "Available" },
    "settings.badge.unavailable":     { zh: "未安装",              en: "Not installed" },

    "settings.section.appearance":    { zh: "外观",                en: "Appearance" },
    "settings.section.appearance_hint":{ zh: "保存在本机浏览器",    en: "Stored in your browser" },
    "settings.theme_label":           { zh: "主题",                en: "Theme" },
    "settings.theme.paper":           { zh: "Paper",               en: "Paper" },
    "settings.theme.paper_hint":      { zh: "日间默认 · 暖白学术",  en: "Daytime default · warm academic white" },
    "settings.theme.dark":            { zh: "Dark",                en: "Dark" },
    "settings.theme.dark_hint":       { zh: "暖石墨 · 冷青蓝点缀",    en: "Warm graphite · cool cyan accent" },
    "settings.theme.auto":            { zh: "Auto",                en: "Auto" },
    "settings.theme.auto_hint":       { zh: "跟随系统",            en: "Follow system" },
    "settings.theme_current":         { zh: "当前：{theme}",        en: "Current: {theme}" },
    "settings.theme_auto_current":    {
      zh: "Auto · 现在 = {resolved}（跟随系统 prefers-color-scheme）",
      en: "Auto · now = {resolved} (follows system prefers-color-scheme)",
    },
    "settings.density_label":         { zh: "密度",                en: "Density" },
    "settings.density.compact":       { zh: "紧凑",                en: "Compact" },
    "settings.density.comfortable":   { zh: "舒适",                en: "Comfortable" },
    "settings.density.airy":          { zh: "宽松",                en: "Airy" },
    "settings.density_hint":          { zh: "控制行高 / 卡片内边距倍率", en: "Controls line height / card padding scale" },
    "settings.basesize_label":        { zh: "基础字号",            en: "Base font size" },

    "settings.section.user_prefs":    { zh: "用户偏好",            en: "User preferences" },
    "settings.lang_row_label":        { zh: "回答语言",            en: "Reply language" },
    "settings.persona_label":         { zh: "Persona（助手名）",   en: "Persona (assistant name)" },
    "settings.persona_count_hint":    { zh: "{n}/40 字符 · 会出现在系统提示词里", en: "{n}/40 chars · appears in the system prompt" },
    "settings.persona_privacy_warn":  {
      zh: "⚠ 这个名字会随每次提问发送到 LLM 后端 · 不要填真名 / 邮箱 / 手机号等隐私信息",
      en: "⚠ This name is sent to the LLM with every request · don't enter real name / email / phone or other private info",
    },
    "settings.persona_icon_label":    { zh: "助手图标",            en: "Assistant icon" },
    "settings.persona_icon_hint":     {
      zh: "粘贴一个 emoji（macOS：🌐/Fn + E，或 ⌃⌘空格）· 留空则用助手名首字",
      en: "Paste an emoji (macOS: 🌐/Fn + E, or ⌃⌘Space) · leave empty to use the first letter of the name",
    },
    "settings.persona_icon_clear":    { zh: "清空",                en: "Clear" },
    "settings.hidden_courses_label":  { zh: "隐藏的课程",          en: "Hidden courses" },
    "settings.hidden_courses_count":  { zh: "{n} 门课程已隐藏（仅前端隐藏；后端数据保留）", en: "{n} courses hidden (frontend-only; backend data is preserved)" },
    "settings.hidden_courses_unhide": { zh: "全部恢复显示",        en: "Show all" },
    "settings.hidden_courses_none":   { zh: "无",                  en: "none" },
    "settings.reset_row_label":       { zh: "重置所有偏好",        en: "Reset all preferences" },
    "settings.reset_btn":             { zh: "重置（含语言/backend/persona）", en: "Reset (language / backend / persona)" },
    "settings.reset_hint":            { zh: "页面会刷新；下次进入会重新询问语言", en: "Page reloads; you'll be asked for language again on next launch" },

    "settings.section.cache":         { zh: "本机缓存（localStorage）", en: "Local cache (localStorage)" },
    "settings.section.cache_hint":    { zh: "总占用 {bytes} · 浏览器上限约 5 MB", en: "{bytes} used · browser limit ~5 MB" },
    "settings.cache.global_keys":     { zh: "全局偏好键",          en: "Global pref keys" },
    "settings.cache.course_cache":    { zh: "课程缓存",            en: "Course cache" },
    "settings.cache.other_keys":      { zh: "其它键",              en: "Other keys" },
    "settings.cache.th_course":       { zh: "课程",                en: "Course" },
    "settings.cache.th_keys":         { zh: "键数",                en: "Keys" },
    "settings.cache.th_bytes":        { zh: "占用",                en: "Size" },
    "settings.cache.clear_one":       { zh: "清空",                en: "Clear" },
    "settings.cache.rescan":          { zh: "重新扫描",            en: "Rescan" },
    "settings.cache.clear_all":       { zh: "清空所有应用缓存（保留偏好）", en: "Clear all app cache (preferences kept)" },

    "settings.section.system":        { zh: "系统状态",            en: "System status" },
    "settings.row.courses":           { zh: "活跃课程数",          en: "Active courses" },
    "settings.row.chunks":            { zh: "索引 chunks 总数",    en: "Indexed chunks total" },
    "settings.row.tokens":            { zh: "累计 tokens",          en: "Cumulative tokens" },
    "settings.tokens_value":          { zh: "输入 {in_} · 输出 {out_}", en: "in {in_} · out {out_}" },

    // ── topbar tabs + course dropdown ──
    "tab.reader":                     { zh: "阅读器",        en: "Reader" },
    "tab.notes":                      { zh: "笔记",          en: "Notes" },
    "tab.mindmap":                    { zh: "知识图谱",      en: "Knowledge Graph" },
    "tab.exam_prep":                  { zh: "考试备考",      en: "Exam Prep" },
    "tab.history":                    { zh: "历史",          en: "History" },
    "topbar.all_courses":             { zh: "🌐 全部课程（{n} chunks）", en: "🌐 All Courses ({n} chunks)" },
    "topbar.course_option":           { zh: "{flag} {name}（{n} chunks）", en: "{flag} {name} ({n} chunks)" },
    "topbar.sources_btn":             { zh: "{n}/{total} 来源",    en: "{n}/{total} sources" },

    // ── status bar (bottom of app) ──
    "status.indexed":                 { zh: "已索引",        en: "Indexed" },
    "status.indexed_value":           { zh: "{n} 门课程 · {chunks} chunks", en: "{n} courses · {chunks} chunks" },
    "status.backend":                 { zh: "后端",          en: "Backend" },
    "status.backend_none":            { zh: "无",            en: "none" },
    "status.active":                  { zh: "当前课程",      en: "Active" },
    "status.context":                 { zh: "上下文",        en: "Context" },
    "status.context_value":           { zh: "{n} / {total} 来源", en: "{n} / {total} sources" },

    // ── library sidebar ──
    "library.sources":                { zh: "来源",          en: "Sources" },
    "library.in_context":             { zh: "{n} / {total} 在上下文中", en: "{n} / {total} in context" },
    "library.drop":                   { zh: "拖入文件或点击上传", en: "Drop files or click to upload" },
    "library.collections":            { zh: "课程集合",      en: "Collections" },
    "library.row_toggle_tip":         { zh: "点击切换勾选 · Shift+点击区间选", en: "Click to toggle · Shift+Click to range-select" },

    // ── assistant welcome / status ──
    "assistant.status.thinking":      { zh: "思考中",        en: "Thinking" },
    "assistant.status.drafting":      { zh: "起草中",        en: "Drafting" },
    "assistant.status.ready":         { zh: "就绪",          en: "Ready" },
    "assistant.context_label":        { zh: "上下文 ·",      en: "Context ·" },
    "assistant.who_welcome":          { zh: "{persona} · 欢迎", en: "{persona} · welcome" },
    "assistant.who_drafting":         { zh: "{persona} · 起草中…", en: "{persona} · drafting…" },
    "assistant.welcome_with_course":  { zh: "已就绪：{course}。提问或点下面的快捷建议。", en: "Ready to help with {course}. Ask questions or click a suggestion below." },
    "assistant.welcome_no_course":    { zh: "欢迎！请先从顶栏选一门课程，然后随便问。", en: "Welcome! Select a course from the top bar, then ask me anything." },
    "assistant.generating_cursor":    { zh: "生成中",        en: "Generating" },
    "assistant.action_check_tab":     { zh: "{label} 请在对应标签页查看结果。", en: "{label} Check the corresponding tab for results." },

    // ── alerts ──
    "alert.pick_course":              { zh: "请先选择一门具体课程（不能是「全部课程」）", en: "Please select a specific course first (not 'All Courses')" },
  };

  function t(key, lang, vars) {
    const entry = STRINGS[key];
    let s = entry && entry[lang];
    if (s == null) s = entry && entry.en;
    if (s == null) s = key;
    if (vars && typeof s === "string") {
      for (const k in vars) {
        s = s.replace(new RegExp("\\{" + k + "\\}", "g"), String(vars[k]));
      }
    }
    return s;
  }

  // Bind module-time React reference if available, otherwise late-bind on first
  // call. React UMD is loaded synchronously before this file, so the eager path
  // is the usual one; the lazy fallback exists only so unit tests can import
  // i18n.js without a React polyfill.
  const _LangContext = (typeof React !== "undefined" && React.createContext)
    ? React.createContext("en")
    : null;

  window.I18N = { STRINGS, t };
  window.LangContext = _LangContext;
  // Stable per-language closure. Without React.useMemo, callers that do
  // `React.useMemo(..., [t])` would see a fresh fn ref every render and
  // re-run the memo body unconditionally — the optimization at e.g.
  // reader.jsx's 25-paragraph welcome doc would silently regress.
  window.useT = function () {
    const ctxLang = (_LangContext && typeof React !== "undefined")
      ? React.useContext(_LangContext)
      : "en";
    const lang = ctxLang || "en";
    if (typeof React !== "undefined" && React.useMemo) {
      return React.useMemo(
        () => function (key, vars) { return t(key, lang, vars); },
        [lang],
      );
    }
    return function (key, vars) { return t(key, lang, vars); };
  };
})();
