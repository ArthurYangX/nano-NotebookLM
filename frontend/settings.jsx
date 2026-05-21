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
  const lang = (typeof window !== "undefined" && window.LangContext)
    ? React.useContext(window.LangContext) : "en";
  const T = (k) => window.I18N.t(k, lang || "en");
  return (
    <span className={"settings-badge " + (ok ? "ok" : "bad")}>
      {ok ? (labelOk || T("settings.badge.configured")) : (labelBad || T("settings.badge.unconfigured"))}
    </span>
  );
}

// review-swarm M1: 三态 badge — backendStatus 尚未返回时（首屏 ~200ms）所有
// API key 都会 bool-coerce 成 false，badge 闪红"未配置"误导用户。loading
// 时改用 warn 态"加载中"，与 embed_warm 三态约定一致。
function LoadingBadge({ ready, ok, labelOk, labelBad }) {
  const lang = (typeof window !== "undefined" && window.LangContext)
    ? React.useContext(window.LangContext) : "en";
  const T = (k) => window.I18N.t(k, lang || "en");
  if (!ready) {
    return <span className="settings-badge warn">{T("settings.badge.loading")}</span>;
  }
  return <Badge ok={ok} labelOk={labelOk} labelBad={labelBad} />;
}

// Subform shared by "edit existing" and "add new" provider flows. Kept
// at module scope (above Settings) so it can hold its own render state
// without polluting the parent component. `isEdit=true` locks the id
// field (router-dict key is immutable) and lets the api_key_ref blank
// out to "keep current".
function ProviderForm({ t, draft, setDraft, isEdit, onSave, onCancel, saving }) {
  if (!draft) return null;
  const upd = (k, v) => setDraft(prev => ({ ...prev, [k]: v }));
  const showBaseUrl = draft.kind === "openai_compat" || draft.kind === "openai_compat_local";
  return (
    <div className="settings-prov-form">
      <div className="settings-prov-form-row">
        <label>
          <span>{t("settings.providers.form.id")}</span>
          <input
            type="text" value={draft.id}
            onChange={e => upd("id", e.target.value)}
            disabled={isEdit}
            placeholder="openai-alt"
          />
        </label>
        <label>
          <span>{t("settings.providers.col.kind")}</span>
          <select value={draft.kind} onChange={e => upd("kind", e.target.value)}>
            <option value="openai_compat">{t("settings.providers.kind.openai_compat")}</option>
            <option value="openai_compat_local">{t("settings.providers.kind.openai_compat_local")}</option>
            <option value="anthropic">{t("settings.providers.kind.anthropic")}</option>
          </select>
        </label>
      </div>
      <div className="settings-prov-form-row">
        <label>
          <span>{t("settings.providers.form.label")}</span>
          <input type="text" value={draft.label} onChange={e => upd("label", e.target.value)} placeholder="OpenAI" />
        </label>
        <label>
          <span>{t("settings.providers.col.model")}</span>
          <input type="text" value={draft.model} onChange={e => upd("model", e.target.value)} placeholder="gpt-4o-mini" />
        </label>
      </div>
      {showBaseUrl && (
        <div className="settings-prov-form-row">
          <label className="wide">
            <span>{t("settings.providers.form.base_url")}</span>
            <input type="text" value={draft.base_url || ""} onChange={e => upd("base_url", e.target.value)} placeholder="https://api.openai.com/v1" />
          </label>
        </div>
      )}
      <div className="settings-prov-form-row">
        <label className="wide">
          <span>
            {t("settings.providers.form.api_key_ref")}
            {isEdit && (
              <em style={{ marginLeft: 6, fontStyle: "normal", opacity: 0.6 }}>
                {t("settings.providers.api_key_ref_disabled_hint")}
              </em>
            )}
          </span>
          <input
            type="text" value={draft.api_key_ref || ""}
            onChange={e => upd("api_key_ref", e.target.value)}
            placeholder="env:OPENAI_API_KEY"
          />
        </label>
      </div>
      <div className="settings-prov-form-row">
        <label className="inline">
          <input type="checkbox" checked={!!draft.enabled} onChange={e => upd("enabled", e.target.checked)} />
          enabled
        </label>
        <div style={{ flex: 1 }} />
        <button type="button" onClick={onCancel} disabled={saving}>{t("settings.providers.action.cancel")}</button>
        <button type="button" className="primary" onClick={onSave} disabled={saving}>{t("settings.providers.action.save")}</button>
      </div>
    </div>
  );
}


// Provider matrix — the editable table of LLM providers that lives in
// the Settings page. Replaces the legacy "three fixed radios" block.
// Mirrors open-notebook's provider matrix layout but scoped to LLM only
// (embedding presets keep their own section). All mutation goes through
// /api/providers; we do an `onStatusRefresh` after each successful op
// so `available_backends` and the topbar chip stay in sync.
function ProvidersMatrix({ t, status, onStatusRefresh, onCommitBackend }) {
  const providersData = (status && status.providers) || { providers: [], default_backend_id: null };
  const rows = providersData.providers || [];
  const defaultId = providersData.default_backend_id;
  const [editingId, setEditingId] = useS(null);
  const [draft, setDraft] = useS(null);
  const [testing, setTesting] = useS(null);
  const [testResults, setTestResults] = useS({});
  const [opError, setOpError] = useS(null);
  const [saving, setSaving] = useS(false);

  function startEdit(row) {
    setOpError(null);
    setEditingId(row.id);
    setDraft({
      id: row.id, kind: row.kind, label: row.label,
      base_url: row.base_url || "",
      // Blank on edit so the user can "keep current" without typing the
      // env var name again. The save handler falls back to the stored
      // value when this stays blank.
      api_key_ref: "",
      model: row.model, enabled: row.enabled,
      _current_api_key_ref: row.api_key_ref,
    });
  }

  function startAdd() {
    setOpError(null);
    setEditingId("__add__");
    setDraft({
      id: "", kind: "openai_compat", label: "",
      base_url: "https://api.openai.com/v1",
      api_key_ref: "env:OPENAI_API_KEY",
      model: "", enabled: true,
    });
  }

  function cancelEdit() {
    setEditingId(null); setDraft(null); setOpError(null);
  }

  async function save() {
    if (!draft) return;
    const isEdit = editingId !== "__add__";
    const targetId = isEdit ? editingId : (draft.id || "").trim();
    if (!/^[A-Za-z0-9_\-]{1,80}$/.test(targetId)) {
      setOpError(t("settings.providers.error", { msg: "ID must match [A-Za-z0-9_-]{1,80}" }));
      return;
    }
    let apiKeyRef = (draft.api_key_ref || "").trim();
    if (isEdit && !apiKeyRef) apiKeyRef = draft._current_api_key_ref || "";
    const body = {
      kind: draft.kind,
      label: (draft.label || "").trim() || targetId,
      base_url: (draft.base_url || "").trim() || null,
      api_key_ref: apiKeyRef,
      model: (draft.model || "").trim(),
      enabled: !!draft.enabled,
    };
    setSaving(true);
    try {
      await window.API.upsertProvider(targetId, body);
      cancelEdit();
      if (onStatusRefresh) onStatusRefresh();
    } catch (e) {
      setOpError(t("settings.providers.error", { msg: (e && e.message) || String(e) }));
    } finally {
      setSaving(false);
    }
  }

  async function doDelete(id) {
    if (!window.confirm(t("settings.providers.confirm_delete", { id }))) return;
    try {
      await window.API.deleteProvider(id);
      if (onStatusRefresh) onStatusRefresh();
    } catch (e) {
      setOpError(t("settings.providers.error", { msg: (e && e.message) || String(e) }));
    }
  }

  async function doSetDefault(id) {
    try {
      await window.API.setDefaultProvider(id);
      if (onStatusRefresh) onStatusRefresh();
      if (onCommitBackend) onCommitBackend(id);
    } catch (e) {
      setOpError(t("settings.providers.error", { msg: (e && e.message) || String(e) }));
    }
  }

  async function doTest(id) {
    setTesting(id);
    setTestResults(prev => ({ ...prev, [id]: null }));
    try {
      const result = await window.API.testProvider(id);
      setTestResults(prev => ({ ...prev, [id]: result }));
    } catch (e) {
      setTestResults(prev => ({ ...prev, [id]: { ok: false, error_type: (e && e.message) || String(e) } }));
    } finally {
      setTesting(null);
    }
  }

  return (
    <div className="settings-providers">
      {opError && <div className="settings-banner bad" style={{ marginBottom: 8 }}>{opError}</div>}
      {rows.length === 0 && editingId !== "__add__" && (
        <div className="settings-pref-hint" style={{ marginBottom: 8 }}>
          {t("settings.providers.empty")}
        </div>
      )}
      {rows.length > 0 && (
        <table className="settings-providers-table">
          <thead>
            <tr>
              <th>{t("settings.providers.col.label")}</th>
              <th>{t("settings.providers.col.kind")}</th>
              <th>{t("settings.providers.col.model")}</th>
              <th>{t("settings.providers.col.base_url")}</th>
              <th>{t("settings.providers.col.status")}</th>
              <th>{t("settings.providers.col.actions")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(row => {
              const editing = editingId === row.id;
              const isDefault = defaultId === row.id;
              const test = testResults[row.id];
              if (editing) {
                return (
                  <tr key={row.id} className="editing">
                    <td colSpan={6}>
                      <ProviderForm
                        t={t}
                        draft={draft} setDraft={setDraft}
                        isEdit={true}
                        onSave={save} onCancel={cancelEdit}
                        saving={saving}
                      />
                    </td>
                  </tr>
                );
              }
              return (
                <tr key={row.id} className={isDefault ? "is-default" : ""}>
                  <td>
                    <div className="settings-prov-label">
                      <strong>{row.label}</strong>{" "}
                      <code className="settings-prov-id">{row.id}</code>
                      {isDefault && <span className="settings-tag" style={{ marginLeft: 4 }}>{t("settings.providers.badge.default")}</span>}
                    </div>
                  </td>
                  <td>{t(`settings.providers.kind.${row.kind}`)}</td>
                  <td><code>{row.model}</code></td>
                  <td><code>{row.base_url || "—"}</code></td>
                  <td>
                    {!row.enabled && <span className="settings-badge warn" style={{ marginRight: 6 }}>{t("settings.providers.badge.disabled")}</span>}
                    {row.api_key_configured
                      ? <span className="settings-badge ok">{t("settings.providers.badge.key_ok")}</span>
                      : <span className="settings-badge bad">{t("settings.providers.badge.key_missing")}</span>}
                    {test && (
                      <span
                        className={"settings-badge " + (test.ok ? "ok" : "bad")}
                        style={{ marginLeft: 6 }}
                        title={test.detail || ""}
                      >
                        {test.ok
                          ? t("settings.providers.test.ok", { ms: test.latency_ms })
                          : t("settings.providers.test.fail", { err: test.error_type || "error" })}
                      </span>
                    )}
                  </td>
                  <td className="settings-prov-actions">
                    <button type="button" onClick={() => doTest(row.id)} disabled={testing === row.id}>
                      {testing === row.id ? t("settings.providers.test.running") : t("settings.providers.action.test")}
                    </button>
                    <button type="button" onClick={() => startEdit(row)}>{t("settings.providers.action.edit")}</button>
                    <button type="button" disabled={isDefault} onClick={() => doSetDefault(row.id)}>
                      {t("settings.providers.action.set_default")}
                    </button>
                    <button type="button" className="danger" disabled={isDefault} onClick={() => doDelete(row.id)}>
                      {t("settings.providers.action.delete")}
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      {editingId === "__add__" ? (
        <ProviderForm
          t={t}
          draft={draft} setDraft={setDraft}
          isEdit={false}
          onSave={save} onCancel={cancelEdit}
          saving={saving}
        />
      ) : (
        <button type="button" className="settings-prov-add" onClick={startAdd} style={{ marginTop: 8 }}>
          {t("settings.providers.add")}
        </button>
      )}
    </div>
  );
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
  const t = (k, vars) => window.I18N.t(k, userLang || "en", vars);
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
    if (!window.confirm(t("settings.clear_course_cache_confirm", { cid: courseId }))) return;
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
    if (!window.confirm(t("settings.clear_cache_confirm"))) return;
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
    if (!window.confirm(t("settings.reset_prefs_confirm"))) return;
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
  // The AI Backend & Models block is now driven by <ProvidersMatrix>;
  // the legacy `available` / `claudeAvailable` / `localAvailable` /
  // `loadingDash` derived state is no longer needed here.
  const s = backendStatus || {};
  const statusReady = !!backendStatus;
  const embedWarm = s.embed_warm_ok;
  const embedWarmLabel = embedWarm == null
    ? t("settings.warm.warming")
    : (embedWarm ? t("settings.warm.ok") : t("settings.warm.failed"));

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
        <h2>{t("settings.title")}</h2>
        <p className="settings-sub">{t("settings.head_sub")}</p>
      </header>

      <div className="settings-grid">

        {/* ───────── AI Backend & Models (providers matrix) ───────── */}
        <Section title={t("settings.section.ai")} hint={t("settings.section.ai_hint")}>
          <ProvidersMatrix
            t={t}
            status={backendStatus}
            onStatusRefresh={onStatusRefresh}
            onCommitBackend={onCommitBackend}
          />
        </Section>

        {/* ───────── Embedding Model ───────── */}
        <Section title={t("settings.section.embedding")} hint={t("settings.section.embedding_hint")}>
          {/* Rebuild progress banner — surfaces after a switch while
              kb.build_index runs across all courses for the new preset. */}
          {rebuildState && rebuildState.status === "running" && (
            <div className="settings-banner warn" style={{ marginBottom: 10 }}>
              <strong>{t("settings.rebuild.running_title")}</strong>
              {" · "}{t("settings.rebuild.preset")} <code>{rebuildState.preset_id}</code>
              {" · "}{t("settings.rebuild.progress")} {rebuildState.done_courses}/{rebuildState.total_courses}
              {rebuildState.current_course ? <> · {t("settings.rebuild.current_course")} <code>{rebuildState.current_course}</code></> : null}
              <div className="settings-pref-hint">{t("settings.rebuild.running_hint")}</div>
            </div>
          )}
          {rebuildState && rebuildState.status === "done" && rebuildState.preset_id && (
            <div className="settings-banner ok" style={{ marginBottom: 10 }}>
              {t("settings.rebuild.done", { preset: rebuildState.preset_id, n: rebuildState.done_courses })}
            </div>
          )}
          {/* H5: some courses failed mid-build. Status is "partial" — render
              the failed list so the user knows which courses' retrieval is
              broken instead of seeing a misleading green "done" banner. */}
          {rebuildState && rebuildState.status === "partial" && (
            <div className="settings-banner bad" style={{ marginBottom: 10 }}>
              {t("settings.rebuild.partial_title")}
              {" · "}{t("settings.rebuild.preset")} <code>{rebuildState.preset_id}</code>
              {" "}{t("settings.rebuild.partial_count", { done: rebuildState.done_courses, total: rebuildState.total_courses })}
              {Array.isArray(rebuildState.failed_courses) && rebuildState.failed_courses.length > 0 && (
                <div className="settings-pref-hint">
                  {t("settings.rebuild.failed_label")}{rebuildState.failed_courses.map(c => <code key={c} style={{ marginRight: 6 }}>{c}</code>)}
                  <br />{t("settings.rebuild.partial_hint")}
                </div>
              )}
            </div>
          )}
          {rebuildState && rebuildState.status === "error" && (
            <div className="settings-banner bad" style={{ marginBottom: 10 }}>
              {t("settings.rebuild.error", { msg: rebuildState.error || "unknown" })}
            </div>
          )}
          {embedSwitchError && (
            <div className="settings-banner bad" style={{ marginBottom: 10 }}>
              {t("settings.embed.switch_failed", { msg: embedSwitchError })}
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
                      {" "}<span className="settings-tag">{p.mode === "api" ? t("settings.preset.tag_api") : t("settings.preset.tag_local")} · {p.dim}d</span>
                      {isSwitching && <span className="settings-badge warn" style={{ marginLeft: 8 }}>{t("settings.preset.switching")}</span>}
                      {statusReady && blockedByKey && <Badge ok={false} labelBad={t("settings.preset.unconfigured")} />}
                    </div>
                    <div className="settings-radio-desc">
                      {/* Prefer the localized description (keyed by preset_id);
                          fall back to the backend's `description` for custom /
                          user-added presets that have no i18n entry. */}
                      {(() => {
                        const key = `settings.preset.desc.${p.id}`;
                        const localized = window.I18N.STRINGS[key];
                        return localized ? t(key) : p.description;
                      })()}
                      {p.download_size_mb > 0 && (
                        <> · {t("settings.preset.first_download", { gb: (p.download_size_mb / 1024).toFixed(1) })}</>
                      )}
                      <br />
                      <span style={{ color: "var(--ink-3, #888)" }}>
                        {t("settings.field.model")}: <code>{p.model}</code>
                      </span>
                    </div>
                  </div>
                </label>
              );
            })}
            {activePresetId === "custom" && (
              <div className="settings-pref-hint" style={{ marginTop: 6 }}>
                {t("settings.preset.custom_hint", { model: s.embedding_model || "—" })}
              </div>
            )}
          </div>

          <hr className="settings-divider" />

          <Row label={t("settings.row.embed_warm")} value={
            <span className={"settings-badge " + (embedWarm === true ? "ok" : embedWarm === false ? "bad" : "warn")}>
              {embedWarmLabel}
            </span>
          } />
          <Row label={t("settings.row.tectonic")} value={<LoadingBadge ready={statusReady} ok={!!s.tectonic_available} labelOk={t("settings.badge.available")} labelBad={t("settings.badge.unavailable")} />} />
          <Row label={t("settings.row.pptx_pdf")} value={<LoadingBadge ready={statusReady} ok={!!s.pptx_pdf_available} labelOk={t("settings.badge.available")} labelBad={t("settings.badge.unavailable")} />} />
        </Section>

        {/* ───────── 外观 ───────── */}
        <Section title={t("settings.section.appearance")} hint={t("settings.section.appearance_hint")}>
          <div className="settings-pref-row">
            <div className="settings-pref-label">{t("settings.theme_label")}</div>
            <div className="settings-pref-ctrl">
              {[
                { v: "paper",  label: t("settings.theme.paper"),  hint: t("settings.theme.paper_hint") },
                { v: "sepia",  label: t("settings.theme.sepia"),  hint: t("settings.theme.sepia_hint") },
                { v: "slate",  label: t("settings.theme.slate"),  hint: t("settings.theme.slate_hint") },
                { v: "dark",   label: t("settings.theme.dark"),   hint: t("settings.theme.dark_hint") },
                { v: "auto",   label: t("settings.theme.auto"),   hint: t("settings.theme.auto_hint") },
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
                  ? t("settings.theme_auto_current", { resolved: autoResolved === "dark" ? t("settings.theme.dark") : t("settings.theme.paper") })
                  : t("settings.theme_current", { theme })}
              </span>
            </div>
          </div>

          <div className="settings-pref-row">
            <div className="settings-pref-label">{t("settings.density_label")}</div>
            <div className="settings-pref-ctrl">
              {[
                { v: "compact",     label: t("settings.density.compact") },
                { v: "comfortable", label: t("settings.density.comfortable") },
                { v: "airy",        label: t("settings.density.airy") },
              ].map(o => (
                <button
                  key={o.v}
                  className={"settings-chip" + (density === o.v ? " active" : "")}
                  onClick={() => onCommitDensity && onCommitDensity(o.v)}
                >{o.label}</button>
              ))}
              <span className="settings-pref-hint">{t("settings.density_hint")}</span>
            </div>
          </div>

          <div className="settings-pref-row">
            <div className="settings-pref-label">{t("settings.basesize_label")}</div>
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
        <Section title={t("settings.section.user_prefs")} hint={t("settings.section.appearance_hint")}>
          <div className="settings-pref-row">
            <div className="settings-pref-label">{t("settings.lang_row_label")}</div>
            <div className="settings-pref-ctrl">
              <button
                className={"settings-chip" + (userLang === "zh" ? " active" : "")}
                onClick={() => onPickLang && onPickLang("zh")}
              >{t("settings.lang_zh_chip")}</button>
              <button
                className={"settings-chip" + (userLang === "en" ? " active" : "")}
                onClick={() => onPickLang && onPickLang("en")}
              >{t("settings.lang_en_chip")}</button>
              <span className="settings-pref-hint">
                {userLang
                  ? t("settings.lang_current", { label: t(userLang === "zh" ? "settings.lang_label_zh" : "settings.lang_label_en") })
                  : t("settings.lang_unset")}
              </span>
            </div>
          </div>

          <div className="settings-pref-row">
            <div className="settings-pref-label">{t("settings.persona_label")}</div>
            <div className="settings-pref-ctrl">
              <input
                type="text"
                maxLength={40}
                placeholder={t("assistant.default_persona")}
                className="settings-input"
                value={personaDraft}
                onChange={e => setPersonaDraft(e.target.value)}
                onBlur={() => onCommitPersona && onCommitPersona(personaDraft)}
                onKeyDown={e => {
                  if (e.key === "Enter") { e.target.blur(); }
                  else if (e.key === "Escape") { setPersonaDraft(persona || ""); e.target.blur(); }
                }}
              />
              <span className="settings-pref-hint">{t("settings.persona_count_hint", { n: personaDraft.length })}</span>
              <span className="settings-pref-hint" style={{ color: "var(--ink-3, #888)" }}>
                {t("settings.persona_privacy_warn")}
              </span>
            </div>
          </div>

          <div className="settings-pref-row">
            <div className="settings-pref-label">{t("settings.hidden_courses_label")}</div>
            <div className="settings-pref-ctrl">
              {hiddenCourseIds && hiddenCourseIds.length ? (
                <>
                  <span className="settings-pref-hint">{t("settings.hidden_courses_count", { n: hiddenCourseIds.length })}</span>
                  <button className="settings-btn ghost" onClick={onUnhideAll}>{t("settings.hidden_courses_unhide")}</button>
                </>
              ) : (
                <span className="settings-pref-hint">{t("settings.hidden_courses_none")}</span>
              )}
            </div>
          </div>

          <div className="settings-pref-row">
            <div className="settings-pref-label">{t("settings.reset_row_label")}</div>
            <div className="settings-pref-ctrl">
              <button className="settings-btn danger" onClick={resetAllPrefs}>{t("settings.reset_btn")}</button>
              <span className="settings-pref-hint">{t("settings.reset_hint")}</span>
            </div>
          </div>
        </Section>

        {/* ───────── 本机缓存 ───────── */}
        <Section title={t("settings.section.cache")} hint={t("settings.section.cache_hint", { bytes: _formatBytes(totalCacheBytes) })}>
          <div className="settings-cache-summary">
            <div>
              <div className="settings-cache-num">{storageScan.global.length}</div>
              <div className="settings-cache-num-label">{t("settings.cache.global_keys")}</div>
            </div>
            <div>
              <div className="settings-cache-num">{Object.keys(storageScan.course).length}</div>
              <div className="settings-cache-num-label">{t("settings.cache.course_cache")}</div>
            </div>
            <div>
              <div className="settings-cache-num">{storageScan.other.length}</div>
              <div className="settings-cache-num-label">{t("settings.cache.other_keys")}</div>
            </div>
          </div>

          {Object.keys(storageScan.course).length > 0 && (
            <table className="settings-cache-table">
              <thead><tr><th>{t("settings.cache.th_course")}</th><th>{t("settings.cache.th_keys")}</th><th>{t("settings.cache.th_bytes")}</th><th></th></tr></thead>
              <tbody>
                {Object.entries(storageScan.course).map(([cid, arr]) => {
                  const sumBytes = arr.reduce((s, x) => s + x.bytes, 0);
                  const courseName = courseLookup[cid]?.name || cid;
                  return (
                    <tr key={cid}>
                      <td><code>{cid}</code> <span className="settings-cache-course-name">{courseName !== cid ? courseName : ""}</span></td>
                      <td>{arr.length}</td>
                      <td>{_formatBytes(sumBytes)}</td>
                      <td><button className="settings-btn ghost small" onClick={() => clearCourseCache(cid)}>{t("settings.cache.clear_one")}</button></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}

          <div className="settings-cache-actions">
            <button className="settings-btn ghost" onClick={rescan}>{t("settings.cache.rescan")}</button>
            <button className="settings-btn danger" onClick={clearAllAppCache}>{t("settings.cache.clear_all")}</button>
          </div>
        </Section>

        {/* ───────── 系统状态 ───────── */}
        <Section title={t("settings.section.system")} hint={`v${s.version || "—"}`}>
          <Row label={t("settings.row.courses")} value={s.courses ?? "—"} />
          <Row label={t("settings.row.chunks")} value={(s.total_chunks ?? 0).toLocaleString()} />
          {/* "累计成本" 行 2026-05-20 删除：router._track_usage 只累加 token
              数，从未接定价表，total_cost 一直返回 0.0。要恢复需为每个 backend
              加 $/1M-token 价目表 — 直到那时之前显示 0 只会误导。 */}
          <Row label={t("settings.row.tokens")} value={
            s.usage
              ? t("settings.tokens_value", {
                  in_: (s.usage.input_tokens ?? 0).toLocaleString(),
                  out_: (s.usage.output_tokens ?? 0).toLocaleString(),
                })
              : "—"
          } mono />
        </Section>

      </div>
    </div>
  );
}

Object.assign(window, { Settings });
