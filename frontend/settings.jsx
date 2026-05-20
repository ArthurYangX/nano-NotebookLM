/* global React, StudyState */
// Settings — central preferences + read-only system status.
//
// A-tier (2026-05-12): no API-key editing on the client. Keys live in the
// server-side .env; this page shows "已配置 / 未配置" badges only. All
// editable state is hoisted from <App> via props — we never read or write
// localStorage directly except for the cache-management block at the
// bottom, which is purely a "view + clear" surface over the existing
// `nano-nlm:v1:*` keys.

const { useState: useS, useEffect: useSEffect, useMemo: useSMemo } = React;

function _formatBytes(n) {
  if (!n) return "0 B";
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / (1024 * 1024)).toFixed(2) + " MB";
}

function _scanLocalStorage() {
  // Returns [{ prefix, count, bytes, keys }]. Buckets:
  //   global: nano-nlm:v1:<flat_key>   (e.g. :backend, :persona, :user-lang, :kg-legend-hidden)
  //   course: nano-nlm:v1:<course_id>:<kind>
  //   other:  anything not starting with nano-nlm:v1
  //
  // SECURITY (review-swarm L2): `bucket.other` 只保留 {key, bytes} —— 它的
  // value 内容必须永远不渲染。同源下其他页（或浏览器扩展）写入的 localStorage
  // 可能包含敏感数据；当前 UI 仅显示 other 桶的计数（settings-cache-summary 第三格），
  // 如果未来添加 other 详情表，必须仍只渲染 key 名，绝不渲染 value。
  const buckets = { global: [], course: {}, other: [] };
  try {
    for (let i = 0; i < window.localStorage.length; i++) {
      const k = window.localStorage.key(i);
      if (!k) continue;
      const v = window.localStorage.getItem(k) || "";
      const bytes = k.length + v.length;
      if (k.startsWith("nano-nlm:v1:")) {
        const rest = k.slice("nano-nlm:v1:".length);
        const idx = rest.indexOf(":");
        if (idx === -1) {
          buckets.global.push({ key: k, bytes });
        } else {
          const courseId = rest.slice(0, idx);
          if (!buckets.course[courseId]) buckets.course[courseId] = [];
          buckets.course[courseId].push({ key: k, bytes });
        }
      } else {
        buckets.other.push({ key: k, bytes });
      }
    }
  } catch (e) { /* Safari private mode 等会抛 — 只读路径，跳过即可 */ }
  return buckets;
}

function Section({ title, hint, children }) {
  return (
    <section className="settings-section">
      <header className="settings-section-head">
        <h3>{title}</h3>
        {hint && <span className="settings-hint">{hint}</span>}
      </header>
      <div className="settings-section-body">{children}</div>
    </section>
  );
}

function Row({ label, value, mono }) {
  return (
    <div className="settings-row">
      <span className="settings-row-label">{label}</span>
      <span className={"settings-row-value" + (mono ? " mono" : "")}>{value}</span>
    </div>
  );
}

function Badge({ ok, labelOk, labelBad }) {
  return (
    <span className={"settings-badge " + (ok ? "ok" : "bad")}>
      {ok ? (labelOk || "已配置") : (labelBad || "未配置")}
    </span>
  );
}

// review-swarm M1: 三态 badge — backendStatus 尚未返回时（首屏 ~200ms）所有
// API key 都会 bool-coerce 成 false，badge 闪红"未配置"误导用户。loading
// 时改用 warn 态"加载中"，与 embed_warm 三态约定一致。
function LoadingBadge({ ready, ok, labelOk, labelBad }) {
  if (!ready) {
    return <span className="settings-badge warn">加载中…</span>;
  }
  return <Badge ok={ok} labelOk={labelOk} labelBad={labelBad} />;
}

function Settings({
  backendStatus,
  backend, onCommitBackend,
  userLang, onPickLang,
  persona, onCommitPersona,
  hiddenCourseIds, onUnhideAll,
  courses,
  theme, onCommitTheme, autoResolved,
  density, onCommitDensity,
  baseSize, onCommitBaseSize,
  onStatusRefresh,
}) {
  const [storageScan, setStorageScan] = useS(() => _scanLocalStorage());
  const [personaDraft, setPersonaDraft] = useS(persona || "");
  // Embedding preset switch UX: while the POST is in flight we set
  // `embedSwitching` to the target preset_id so the radio shows a loading
  // hint and other radios disable. Once the server acks, we clear it; the
  // backend's `embedding_rebuild` state then drives the banner via polling.
  const [embedSwitching, setEmbedSwitching] = useS(null);
  const [embedSwitchError, setEmbedSwitchError] = useS(null);
  useSEffect(() => { setPersonaDraft(persona || ""); }, [persona]);

  // While a background rebuild is running, tick /api/status faster so the
  // banner progresses smoothly. Cleans up on idle / unmount.
  const rebuildState = (backendStatus && backendStatus.embedding_rebuild) || null;
  const rebuildRunning = rebuildState && rebuildState.status === "running";
  useSEffect(() => {
    if (!rebuildRunning || !onStatusRefresh) return undefined;
    const t = setInterval(() => { onStatusRefresh(); }, 1500);
    return () => clearInterval(t);
  }, [rebuildRunning, onStatusRefresh]);

  function rescan() { setStorageScan(_scanLocalStorage()); }

  const totalCacheBytes = useSMemo(() => {
    let sum = 0;
    for (const x of storageScan.global) sum += x.bytes;
    for (const arr of Object.values(storageScan.course)) for (const x of arr) sum += x.bytes;
    for (const x of storageScan.other) sum += x.bytes;
    return sum;
  }, [storageScan]);

  function clearCourseCache(courseId) {
    if (!courseId) return;
    if (!window.confirm(`清空课程 ${courseId} 的全部前端缓存？\n（笔记草稿、KG 编辑视图状态、测验答题记录都会丢失，但服务端数据不受影响）`)) return;
    try {
      const toDel = [];
      for (let i = 0; i < window.localStorage.length; i++) {
        const k = window.localStorage.key(i);
        if (k && k.startsWith(`nano-nlm:v1:${courseId}:`)) toDel.push(k);
      }
      toDel.forEach(k => window.localStorage.removeItem(k));
    } catch (e) {
      // review-swarm L3: 配额耗尽 / 存储损坏时也要可观测，便于排查。
      console.warn("[settings] clearCourseCache failed:", e);
    }
    rescan();
  }

  function clearAllAppCache() {
    if (!window.confirm("清空所有前端缓存（不包括偏好：language / backend / persona）？")) return;
    try {
      const toDel = [];
      for (let i = 0; i < window.localStorage.length; i++) {
        const k = window.localStorage.key(i);
        if (!k || !k.startsWith("nano-nlm:v1:")) continue;
        // 保留偏好类全局 key
        // 与 CLAUDE.md 的全局偏好键清单保持同步（flat `nano-nlm:v1:<kind>`）。
        const preserved = new Set([
          "nano-nlm:v1:backend",
          "nano-nlm:v1:persona",
          "nano-nlm:v1:user-lang",
          "nano-nlm:v1:kg-legend-hidden",
          "nano-nlm:v1:hidden-courses",
          "nano-nlm:v1:notes-toc-hidden",
          "nano-nlm:v1:pdf-outline-hidden",
          "nano-nlm:v1:notes-toolbar-collapsed",
          "nano-nlm:v1:theme",
          "nano-nlm:v1:density",
          "nano-nlm:v1:base-size",
        ]);
        if (preserved.has(k)) continue;
        toDel.push(k);
      }
      toDel.forEach(k => window.localStorage.removeItem(k));
    } catch (e) {
      console.warn("[settings] clearAllAppCache failed:", e);
    }
    rescan();
  }

  function resetAllPrefs() {
    if (!window.confirm("重置所有偏好（语言、backend、persona、KG 视图、隐藏课程）？\n下次进入需重新选择语言。")) return;
    [
      "nano-nlm:v1:backend",
      "nano-nlm:v1:persona",
      "nano-nlm:v1:user-lang",
      "nano-nlm:v1:kg-legend-hidden",
      "nano-nlm:v1:hidden-courses",
      "nano-nlm:v1:notes-toc-hidden",
      "nano-nlm:v1:pdf-outline-hidden",
      "nano-nlm:v1:notes-toolbar-collapsed",
      "nano-nlm:v1:theme",
      "nano-nlm:v1:density",
      "nano-nlm:v1:base-size",
    ].forEach(k => {
      try { window.localStorage.removeItem(k); }
      catch (e) { console.warn("[settings] resetAllPrefs: removeItem(" + k + ") failed:", e); }
    });
    rescan();
    window.location.reload();
  }

  // ── 后端可用性 ──
  // review-swarm M1: 区分 "backendStatus 还没拉到" 与 "字段确实是 false/null"。
  // statusReady 用于驱动加载态 badge / 占位符，避免首屏闪红误导。
  const s = backendStatus || {};
  const statusReady = !!backendStatus;
  const available = new Set(s.available_backends || s.backends || []);
  const claudeAvailable = available.has("claude");
  const localConfigured = !!s.local_llm_configured;
  const localAvailable = available.has("local");
  const embedWarm = s.embed_warm_ok;
  const embedWarmLabel = embedWarm == null ? "warming…" : (embedWarm ? "ok" : "failed");
  const loadingDash = statusReady ? "—" : "加载中…";

  // ── Embedding presets ──
  const presets = Array.isArray(s.embedding_presets) ? s.embedding_presets : [];
  const activePresetId = s.active_preset_id || null;
  const embedApiConfigured = !!s.embedding_api_configured;

  async function pickEmbedding(presetId) {
    if (!presetId || presetId === activePresetId || embedSwitching) return;
    setEmbedSwitchError(null);
    setEmbedSwitching(presetId);
    try {
      await window.API.setEmbeddingPreset(presetId);
      // Refresh /api/status so the radio + banner reflect the new active
      // preset and the rebuild state appears immediately.
      if (onStatusRefresh) onStatusRefresh();
    } catch (e) {
      console.warn("[settings] setEmbeddingPreset failed:", e);
      setEmbedSwitchError(e && e.message ? e.message : String(e));
    } finally {
      setEmbedSwitching(null);
    }
  }

  // ── 课程信息（用于课程缓存表） ──
  const courseLookup = useSMemo(() => {
    const m = {};
    for (const c of (courses || [])) m[c.id] = c;
    return m;
  }, [courses]);

  return (
    <div className="settings-page">
      <header className="settings-head">
        <h2>Settings</h2>
        <p className="settings-sub">
          应用偏好集中页。API key 与模型 ID 由后端 <code>.env</code> 管理 — 此处仅显示状态，请直接编辑 <code>.env</code> 修改密钥与默认模型。
        </p>
      </header>

      <div className="settings-grid">

        {/* ───────── AI Backend & Models ───────── */}
        <Section title="AI Backend & Models" hint="只读 — 改 .env 后重启服务">
          <div className="settings-radio-group">
            {(() => {
              const openaiLabel = (() => {
                const url = (s.openai_base_url || "").toLowerCase();
                if (url.includes("deepseek")) return "DeepSeek";
                if (url.includes("moonshot")) return "Moonshot (Kimi)";
                if (url.includes("zhipu") || url.includes("bigmodel")) return "Zhipu (GLM)";
                if (url.includes("minimax")) return "MiniMax";
                if (url.includes("groq")) return "Groq";
                if (url.includes("together")) return "Together";
                if (url.includes("googleapis") || url.includes("generativelanguage")) return "Gemini";
                if (url.includes("openai.com")) return "OpenAI";
                return "OpenAI-compatible";
              })();
              return (
                <label className={"settings-radio" + (backend === "openai" ? " active" : "")}>
                  <input
                    type="radio"
                    name="backend"
                    value="openai"
                    checked={backend === "openai"}
                    disabled={!available.has("openai")}
                    onChange={() => onCommitBackend && onCommitBackend("openai")}
                  />
                  <div>
                    <div className="settings-radio-title">🤖 {openaiLabel} {s.openai_model || "—"} <span className="settings-tag">主路径</span></div>
                    <div className="settings-radio-desc">
                      模型: <code>{s.openai_model || loadingDash}</code>，base URL: <code>{s.openai_base_url || loadingDash}</code>
                    </div>
                  </div>
                </label>
              );
            })()}

            <label className={"settings-radio" + (backend === "claude" ? " active" : "") + (!claudeAvailable ? " disabled" : "")}>
              <input
                type="radio"
                name="backend"
                value="claude"
                checked={backend === "claude"}
                disabled={!claudeAvailable}
                onChange={() => onCommitBackend && onCommitBackend("claude")}
              />
              <div>
                <div className="settings-radio-title">
                  🧠 Anthropic Claude
                  {statusReady && !claudeAvailable && <Badge ok={false} labelBad="未配置 ANTHROPIC_API_KEY" />}
                </div>
                <div className="settings-radio-desc">
                  模型: <code>{s.claude_model || loadingDash}</code>
                </div>
              </div>
            </label>

            <label className={"settings-radio" + (backend === "local" ? " active" : "") + (!localAvailable ? " disabled" : "")}>
              <input
                type="radio"
                name="backend"
                value="local"
                checked={backend === "local"}
                disabled={!localAvailable}
                onChange={() => onCommitBackend && onCommitBackend("local")}
              />
              <div>
                <div className="settings-radio-title">
                  💻 Local model <span className="settings-tag">Ollama / vLLM / LM Studio</span>
                  {statusReady && !localConfigured && <Badge ok={false} labelBad="未配置 LOCAL_LLM_BASE_URL" />}
                </div>
                <div className="settings-radio-desc">
                  {localConfigured
                    ? <>模型: <code>{s.local_llm_model || "—"}</code> · endpoint: <code>{s.local_llm_base_url || "—"}</code></>
                    : <>在 <code>.env</code> 设置 <code>LOCAL_LLM_BASE_URL</code> + <code>LOCAL_LLM_MODEL</code> 启用本地模型</>}
                </div>
              </div>
            </label>
          </div>

          <hr className="settings-divider" />

          <Row label="Default backend (.env)" value={<code>{s.default_backend || loadingDash}</code>} />
          <Row label="Main API model" value={<code>{s.openai_model || loadingDash}</code>} />
          <Row label="Main API base URL" value={<code>{s.openai_base_url || loadingDash}</code>} />
          <Row label="Main API key" value={<LoadingBadge ready={statusReady} ok={!!s.openai_api_key_configured} />} />
          <Row label="Active backends" value={<code>{statusReady ? ((s.backends || []).join(", ") || "—") : "加载中…"}</code>} />
        </Section>

        {/* ───────── Embedding Model ───────── */}
        <Section title="Embedding Model" hint="切换会按需在后台重建索引 · 切回旧选项是秒切（每个预设保留独立索引）">
          {/* Rebuild progress banner — surfaces after a switch while
              kb.build_index runs across all courses for the new preset. */}
          {rebuildState && rebuildState.status === "running" && (
            <div className="settings-banner warn" style={{ marginBottom: 10 }}>
              <strong>正在重建索引</strong> · 预设 <code>{rebuildState.preset_id}</code> · 进度{" "}
              {rebuildState.done_courses}/{rebuildState.total_courses}
              {rebuildState.current_course ? <> · 当前 <code>{rebuildState.current_course}</code></> : null}
              <div className="settings-pref-hint">期间问答可用，但未重建课程的语义检索会临时退化为 BM25-only。</div>
            </div>
          )}
          {rebuildState && rebuildState.status === "done" && rebuildState.preset_id && (
            <div className="settings-banner ok" style={{ marginBottom: 10 }}>
              ✓ 索引已重建至 <code>{rebuildState.preset_id}</code>（{rebuildState.done_courses} 门课程）
            </div>
          )}
          {/* H5: some courses failed mid-build. Status is "partial" — render
              the failed list so the user knows which courses' retrieval is
              broken instead of seeing a misleading green "done" banner. */}
          {rebuildState && rebuildState.status === "partial" && (
            <div className="settings-banner bad" style={{ marginBottom: 10 }}>
              ⚠ 部分重建失败 · 预设 <code>{rebuildState.preset_id}</code>
              （{rebuildState.done_courses}/{rebuildState.total_courses} 完成）
              {Array.isArray(rebuildState.failed_courses) && rebuildState.failed_courses.length > 0 && (
                <div className="settings-pref-hint">
                  失败课程：{rebuildState.failed_courses.map(c => <code key={c} style={{ marginRight: 6 }}>{c}</code>)}
                  <br />可重新选这个预设触发重试，或检查服务端日志。
                </div>
              )}
            </div>
          )}
          {rebuildState && rebuildState.status === "error" && (
            <div className="settings-banner bad" style={{ marginBottom: 10 }}>
              重建失败：<code>{rebuildState.error || "unknown"}</code>
            </div>
          )}
          {embedSwitchError && (
            <div className="settings-banner bad" style={{ marginBottom: 10 }}>
              切换失败：{embedSwitchError}
            </div>
          )}

          <div className="settings-radio-group">
            {presets.map(p => {
              const disabled = (p.requires_api_key && !embedApiConfigured) || (embedSwitching && embedSwitching !== p.id);
              const isActive = activePresetId === p.id;
              const isSwitching = embedSwitching === p.id;
              const blockedByKey = p.requires_api_key && !embedApiConfigured;
              return (
                <label
                  key={p.id}
                  className={"settings-radio" + (isActive ? " active" : "") + (disabled ? " disabled" : "")}
                >
                  <input
                    type="radio"
                    name="embedding-preset"
                    value={p.id}
                    checked={isActive}
                    disabled={!!disabled}
                    onChange={() => pickEmbedding(p.id)}
                  />
                  <div>
                    <div className="settings-radio-title">
                      {p.label}
                      {" "}<span className="settings-tag">{p.mode === "api" ? "API" : "本地"} · {p.dim}d</span>
                      {isSwitching && <span className="settings-badge warn" style={{ marginLeft: 8 }}>切换中…</span>}
                      {statusReady && blockedByKey && <Badge ok={false} labelBad="未配置 EMBEDDING_API_KEY" />}
                    </div>
                    <div className="settings-radio-desc">
                      {p.description}
                      {p.download_size_mb > 0 && (
                        <> · 首次下载约 {(p.download_size_mb / 1024).toFixed(1)} GB</>
                      )}
                      <br />
                      <span style={{ color: "var(--ink-3, #888)" }}>
                        模型: <code>{p.model}</code>
                      </span>
                    </div>
                  </div>
                </label>
              );
            })}
            {activePresetId === "custom" && (
              <div className="settings-pref-hint" style={{ marginTop: 6 }}>
                ⚠ 当前 <code>EMBEDDING_MODEL</code> 是 env 自定义值（<code>{s.embedding_model || "—"}</code>），不属于任何预设。选一个预设后会持久化并覆盖 env 默认。
              </div>
            )}
          </div>

          <hr className="settings-divider" />

          <Row label="Embedding mode" value={<code>{s.embedding_mode || loadingDash}</code>} />
          <Row label="Embedding model" value={<code>{s.embedding_model || loadingDash}</code>} />
          <Row label="Active preset" value={<code>{activePresetId || loadingDash}</code>} />
          <Row label="Embed warm-up" value={
            <span className={"settings-badge " + (embedWarm === true ? "ok" : embedWarm === false ? "bad" : "warn")}>
              {embedWarmLabel}
            </span>
          } />
          <Row label="Tectonic (PDF 编译)" value={<LoadingBadge ready={statusReady} ok={!!s.tectonic_available} labelOk="可用" labelBad="未安装" />} />
          <Row label="PPTX → PDF (LibreOffice)" value={<LoadingBadge ready={statusReady} ok={!!s.pptx_pdf_available} labelOk="可用" labelBad="未安装" />} />
        </Section>

        {/* ───────── 外观 ───────── */}
        <Section title="外观" hint="保存在本机浏览器">
          <div className="settings-pref-row">
            <div className="settings-pref-label">主题</div>
            <div className="settings-pref-ctrl">
              {[
                { v: "paper",  label: "Paper",  hint: "默认浅色" },
                { v: "sepia",  label: "Sepia",  hint: "暖纸色" },
                { v: "slate",  label: "Slate",  hint: "石板灰" },
                { v: "dark",   label: "Dark",   hint: "深色" },
                { v: "auto",   label: "Auto",   hint: "跟随系统" },
              ].map(o => (
                <button
                  key={o.v}
                  className={"settings-chip" + (theme === o.v ? " active" : "")}
                  title={o.hint}
                  onClick={() => onCommitTheme && onCommitTheme(o.v)}
                >{o.label}</button>
              ))}
              <span className="settings-pref-hint">
                {theme === "auto"
                  ? `Auto · 现在 = ${autoResolved === "dark" ? "Dark" : "Paper"}（跟随系统 prefers-color-scheme）`
                  : `当前：${theme}`}
              </span>
            </div>
          </div>

          <div className="settings-pref-row">
            <div className="settings-pref-label">密度</div>
            <div className="settings-pref-ctrl">
              {[
                { v: "compact",     label: "Compact" },
                { v: "comfortable", label: "Comfortable" },
                { v: "airy",        label: "Airy" },
              ].map(o => (
                <button
                  key={o.v}
                  className={"settings-chip" + (density === o.v ? " active" : "")}
                  onClick={() => onCommitDensity && onCommitDensity(o.v)}
                >{o.label}</button>
              ))}
              <span className="settings-pref-hint">控制行高 / 卡片内边距倍率</span>
            </div>
          </div>

          <div className="settings-pref-row">
            <div className="settings-pref-label">基础字号</div>
            <div className="settings-pref-ctrl">
              <input
                type="range"
                min={13} max={18} step={1}
                value={baseSize}
                onChange={e => onCommitBaseSize && onCommitBaseSize(parseInt(e.target.value, 10))}
                style={{ verticalAlign: "middle" }}
              />
              <span className="settings-pref-hint mono">{baseSize}px</span>
            </div>
          </div>
        </Section>

        {/* ───────── 用户偏好 ───────── */}
        <Section title="用户偏好" hint="保存在本机浏览器">
          <div className="settings-pref-row">
            <div className="settings-pref-label">回答语言</div>
            <div className="settings-pref-ctrl">
              <button
                className={"settings-chip" + (userLang === "zh" ? " active" : "")}
                onClick={() => onPickLang && onPickLang("zh")}
              >🇨🇳 中文</button>
              <button
                className={"settings-chip" + (userLang === "en" ? " active" : "")}
                onClick={() => onPickLang && onPickLang("en")}
              >🇺🇸 English</button>
              <span className="settings-pref-hint">
                {userLang ? `当前：${userLang === "zh" ? "中文" : "English"}` : "未设置 — 启动时会弹窗询问"}
              </span>
            </div>
          </div>

          <div className="settings-pref-row">
            <div className="settings-pref-label">Persona（助手名）</div>
            <div className="settings-pref-ctrl">
              <input
                type="text"
                maxLength={40}
                placeholder="Study Assistant"
                className="settings-input"
                value={personaDraft}
                onChange={e => setPersonaDraft(e.target.value)}
                onBlur={() => onCommitPersona && onCommitPersona(personaDraft)}
                onKeyDown={e => {
                  if (e.key === "Enter") { e.target.blur(); }
                  else if (e.key === "Escape") { setPersonaDraft(persona || ""); e.target.blur(); }
                }}
              />
              <span className="settings-pref-hint">{personaDraft.length}/40 字符 · 会出现在系统提示词里</span>
              <span className="settings-pref-hint" style={{ color: "var(--ink-3, #888)" }}>
                ⚠ 这个名字会随每次提问发送到 LLM 后端 · 不要填真名 / 邮箱 / 手机号等隐私信息
              </span>
            </div>
          </div>

          <div className="settings-pref-row">
            <div className="settings-pref-label">隐藏的课程</div>
            <div className="settings-pref-ctrl">
              {hiddenCourseIds && hiddenCourseIds.length ? (
                <>
                  <span className="settings-pref-hint">{hiddenCourseIds.length} 门课程已隐藏（仅前端隐藏；后端数据保留）</span>
                  <button className="settings-btn ghost" onClick={onUnhideAll}>全部恢复显示</button>
                </>
              ) : (
                <span className="settings-pref-hint">无</span>
              )}
            </div>
          </div>

          <div className="settings-pref-row">
            <div className="settings-pref-label">重置所有偏好</div>
            <div className="settings-pref-ctrl">
              <button className="settings-btn danger" onClick={resetAllPrefs}>重置（含语言/backend/persona）</button>
              <span className="settings-pref-hint">页面会刷新；下次进入会重新询问语言</span>
            </div>
          </div>
        </Section>

        {/* ───────── 本机缓存 ───────── */}
        <Section title="本机缓存（localStorage）" hint={`总占用 ${_formatBytes(totalCacheBytes)} · 浏览器上限约 5 MB`}>
          <div className="settings-cache-summary">
            <div>
              <div className="settings-cache-num">{storageScan.global.length}</div>
              <div className="settings-cache-num-label">全局偏好键</div>
            </div>
            <div>
              <div className="settings-cache-num">{Object.keys(storageScan.course).length}</div>
              <div className="settings-cache-num-label">课程缓存</div>
            </div>
            <div>
              <div className="settings-cache-num">{storageScan.other.length}</div>
              <div className="settings-cache-num-label">其它键</div>
            </div>
          </div>

          {Object.keys(storageScan.course).length > 0 && (
            <table className="settings-cache-table">
              <thead><tr><th>课程</th><th>键数</th><th>占用</th><th></th></tr></thead>
              <tbody>
                {Object.entries(storageScan.course).map(([cid, arr]) => {
                  const sumBytes = arr.reduce((s, x) => s + x.bytes, 0);
                  const courseName = courseLookup[cid]?.name || cid;
                  return (
                    <tr key={cid}>
                      <td><code>{cid}</code> <span className="settings-cache-course-name">{courseName !== cid ? courseName : ""}</span></td>
                      <td>{arr.length}</td>
                      <td>{_formatBytes(sumBytes)}</td>
                      <td><button className="settings-btn ghost small" onClick={() => clearCourseCache(cid)}>清空</button></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}

          <div className="settings-cache-actions">
            <button className="settings-btn ghost" onClick={rescan}>重新扫描</button>
            <button className="settings-btn danger" onClick={clearAllAppCache}>清空所有应用缓存（保留偏好）</button>
          </div>
        </Section>

        {/* ───────── 系统状态 ───────── */}
        <Section title="系统状态" hint={`v${s.version || "—"}`}>
          <Row label="活跃课程数" value={s.courses ?? "—"} />
          <Row label="索引 chunks 总数" value={(s.total_chunks ?? 0).toLocaleString()} />
          <Row label="搜索 p50 (ms)" value={s.latency_ms?.search_p50 ?? "—"} mono />
          <Row label="对话 p50 (ms)" value={s.latency_ms?.chat_p50 ?? "—"} mono />
          {/* "累计成本" 行 2026-05-20 删除：router._track_usage 只累加 token
              数，从未接定价表，total_cost 一直返回 0.0。要恢复需为每个 backend
              加 $/1M-token 价目表 — 直到那时之前显示 0 只会误导。 */}
          <Row label="累计 tokens" value={
            s.usage
              ? `in ${(s.usage.input_tokens ?? 0).toLocaleString()} · out ${(s.usage.output_tokens ?? 0).toLocaleString()}`
              : "—"
          } mono />
          <Row label="后端版本" value={<code>{s.version || "—"}</code>} />
        </Section>

      </div>
    </div>
  );
}

Object.assign(window, { Settings });
