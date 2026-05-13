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
}) {
  const [storageScan, setStorageScan] = useS(() => _scanLocalStorage());
  const [personaDraft, setPersonaDraft] = useS(persona || "");
  useSEffect(() => { setPersonaDraft(persona || ""); }, [persona]);

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
  const qwenConfigured = !!s.qwen_raft_configured;
  const qwenAvailable = !!s.qwen_raft_available;
  // 2026-05-13: parallel base Qwen2.5-7B-Instruct option
  const qwenBaseConfigured = !!s.qwen_base_configured;
  const qwenBaseAvailable = !!s.qwen_base_available;
  const embedWarm = s.embed_warm_ok;
  const embedWarmLabel = embedWarm == null ? "warming…" : (embedWarm ? "ok" : "failed");
  const loadingDash = statusReady ? "—" : "加载中…";

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
            <label className={"settings-radio" + (backend === "codex" ? " active" : "")}>
              <input
                type="radio"
                name="backend"
                value="codex"
                checked={backend === "codex"}
                onChange={() => onCommitBackend && onCommitBackend("codex")}
              />
              <div>
                <div className="settings-radio-title">🤖 Codex {(s.openai_model || "").replace(/^gpt-/i, "GPT-") || "GPT"} <span className="settings-tag">主路径</span></div>
                <div className="settings-radio-desc">
                  生产默认。模型: <code>{s.openai_model || loadingDash}</code>，base URL: <code>{s.openai_base_url || loadingDash}</code>
                </div>
              </div>
            </label>
            <label className={"settings-radio" + (backend === "qwen_raft" ? " active" : "") + (!statusReady || !qwenConfigured || !qwenAvailable ? " disabled" : "")}>
              <input
                type="radio"
                name="backend"
                value="qwen_raft"
                checked={backend === "qwen_raft"}
                disabled={!statusReady || !qwenConfigured || !qwenAvailable}
                onChange={() => onCommitBackend && onCommitBackend("qwen_raft")}
              />
              <div>
                <div className="settings-radio-title">
                  🎓 Qwen2.5-7B-RAFT <span className="settings-tag">微调 · 7B · 4-bit (nf4)</span>
                  {!statusReady && <span className="settings-badge warn">加载中…</span>}
                  {statusReady && !qwenConfigured && <Badge ok={false} labelBad="未配置 QWEN_RAFT_URL" />}
                  {statusReady && qwenConfigured && !qwenAvailable && <Badge ok={false} labelBad="AutoDL 主机不可达" />}
                </div>
                <div className="settings-radio-desc">
                  {qwenConfigured
                    ? <>模型: <code>{s.qwen_raft_model_name || "—"}</code> · host: <code>{s.qwen_raft_url_host || "—"}</code></>
                    : <>在 <code>.env</code> 设置 <code>QWEN_RAFT_URL</code> 启用本地微调后端</>}
                </div>
              </div>
            </label>
            {/* 2026-05-13: parallel base Qwen2.5-7B-Instruct option for
                A/B compare with RAFT. Same backend class, different URL
                (QWEN_BASE_URL pointing at :8002 on AutoDL host). */}
            <label className={"settings-radio" + (backend === "qwen_base" ? " active" : "") + (!statusReady || !qwenBaseConfigured || !qwenBaseAvailable ? " disabled" : "")}>
              <input
                type="radio"
                name="backend"
                value="qwen_base"
                checked={backend === "qwen_base"}
                disabled={!statusReady || !qwenBaseConfigured || !qwenBaseAvailable}
                onChange={() => onCommitBackend && onCommitBackend("qwen_base")}
              />
              <div>
                <div className="settings-radio-title">
                  🐧 Qwen2.5-7B-Instruct <span className="settings-tag">基座 · 7B · 4-bit (nf4)</span>
                  {!statusReady && <span className="settings-badge warn">加载中…</span>}
                  {statusReady && !qwenBaseConfigured && <Badge ok={false} labelBad="未配置 QWEN_BASE_URL" />}
                  {statusReady && qwenBaseConfigured && !qwenBaseAvailable && <Badge ok={false} labelBad="AutoDL 主机不可达" />}
                </div>
                <div className="settings-radio-desc">
                  {qwenBaseConfigured
                    ? <>模型: <code>{s.qwen_base_model_name || "—"}</code> · 未经 RAFT 微调，更善于调用预训练知识</>
                    : <>在 <code>.env</code> 设置 <code>QWEN_BASE_URL</code> 启用基座对照</>}
                </div>
              </div>
            </label>
          </div>

          <hr className="settings-divider" />

          <Row label="Default backend (.env)" value={<code>{s.default_backend || loadingDash}</code>} />
          <Row label="OpenAI / Codex model" value={<code>{s.openai_model || loadingDash}</code>} />
          <Row label="OpenAI base URL" value={<code>{s.openai_base_url || loadingDash}</code>} />
          <Row label="OPENAI_API_KEY" value={<LoadingBadge ready={statusReady} ok={!!s.openai_api_key_configured} />} />
          <Row label="Claude model" value={<code>{s.claude_model || loadingDash}</code>} />
          <Row label="ANTHROPIC_API_KEY" value={<LoadingBadge ready={statusReady} ok={!!s.anthropic_api_key_configured} />} />
          <Row label="Active backends" value={<code>{statusReady ? ((s.backends || []).join(", ") || "—") : "加载中…"}</code>} />
        </Section>

        {/* ───────── Embedding & Tools ───────── */}
        <Section title="Embedding & Tools" hint="只读">
          <Row label="Embedding mode" value={<code>{s.embedding_mode || loadingDash}</code>} />
          <Row label="Embedding model" value={<code>{s.embedding_model || loadingDash}</code>} />
          <Row label="Embed warm-up" value={
            <span className={"settings-badge " + (embedWarm === true ? "ok" : embedWarm === false ? "bad" : "warn")}>
              {embedWarmLabel}
            </span>
          } />
          <Row label="Tectonic (PDF 编译)" value={<LoadingBadge ready={statusReady} ok={!!s.tectonic_available} labelOk="可用" labelBad="未安装" />} />
          <Row label="PPTX → PDF (LibreOffice)" value={<LoadingBadge ready={statusReady} ok={!!s.pptx_pdf_available} labelOk="可用" labelBad="未安装" />} />
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
          <Row label="累计成本" value={
            s.usage?.total_cost != null
              ? `$${Number(s.usage.total_cost).toFixed(4)}`
              : "—"
          } mono />
          <Row label="后端版本" value={<code>{s.version || "—"}</code>} />
        </Section>

      </div>
    </div>
  );
}

Object.assign(window, { Settings });
