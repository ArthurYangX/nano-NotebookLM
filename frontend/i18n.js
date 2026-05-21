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

    // ── processing (upload progress) ──
    "processing.sec_suffix":       { zh: "秒",  en: "s" },
    "processing.min_suffix":       { zh: "分",  en: "m" },
    "processing.hour_suffix":      { zh: "小时", en: "h" },
    "processing.elapsed":          { zh: "已用",       en: "Elapsed" },
    "processing.remaining":        { zh: "剩余 ~{t}",  en: "~{t} remaining" },
    "processing.pages_total":      { zh: "共 {n} 页",  en: "{n} pages total" },
    "processing.estimate_about":   { zh: "估算约",     en: "Estimated" },
    "processing.pages_progress":   { zh: "{done} / {total} 页", en: "{done} / {total} pages" },
    "processing.with_pptx_render": { zh: "渲染 PPTX 预览", en: "rendering PPTX preview" },
    "processing.failed_at":        { zh: "上传管道在 {stage} 阶段失败", en: "Upload pipeline failed at stage {stage}" },

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
    "settings.theme.paper_hint":      { zh: "默认浅色",            en: "Default light" },
    "settings.theme.sepia":           { zh: "Sepia",               en: "Sepia" },
    "settings.theme.sepia_hint":      { zh: "暖纸色",              en: "Warm paper" },
    "settings.theme.slate":           { zh: "Slate",               en: "Slate" },
    "settings.theme.slate_hint":      { zh: "石板灰",              en: "Slate gray" },
    "settings.theme.dark":            { zh: "Dark",                en: "Dark" },
    "settings.theme.dark_hint":       { zh: "深色",                en: "Dark" },
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
  window.useT = function () {
    const ctxLang = (_LangContext && typeof React !== "undefined")
      ? React.useContext(_LangContext)
      : "en";
    const lang = ctxLang || "en";
    return function (key, vars) { return t(key, lang, vars); };
  };
})();
