import { useEffect, useMemo, useRef, useState } from "react";
import {
  BadgeCheck,
  Check,
  ChevronRight,
  Loader2,
  LogIn,
  LogOut,
  Plus,
  RefreshCw,
  Save,
  Trash2,
} from "lucide-react";
import { api } from "../api/client";
import type { ModelConfigState, ModelProfile } from "../types";

type Provider = { id: string; name: string };
type ProviderRuntime = {
  available: boolean;
  node_available: boolean;
  node_version: string | null;
  required_node_version: string;
  message: string;
};
type CatalogModel = { id: string; name: string };
type LoginEvent = {
  id: number;
  type: "auth_url" | "device_code" | "info" | "progress" | "prompt" | "completed" | "cancelled" | "error";
  message?: string;
  url?: string;
  links?: { label?: string; url: string }[];
  instructions?: string;
  userCode?: string;
  verificationUri?: string;
  prompt_id?: string;
  prompt?: {
    type: "text" | "secret" | "select" | "manual_code";
    message: string;
    placeholder?: string;
    options?: { id: string; label: string; description?: string }[];
  };
};
type LoginStatus = {
  session_id: string;
  status: "running" | "completed" | "error" | "cancelled" | "expired";
  events: LoginEvent[];
  next_event_id: number;
};
type Notice = { status: "running" | "success" | "error"; message: string };

const emptyConfiguration = (): ModelConfigState => ({ version: 2, active_profile_id: null, profiles: [] });

function newProfile(kind: ModelProfile["kind"], provider = "openai"): ModelProfile {
  const api = kind === "api";
  return {
    id: crypto.randomUUID(),
    name: api ? "新 API 配置" : "新订阅配置",
    kind,
    provider: api ? "openai-compatible" : provider,
    base_url: api ? "https://api.openai.com/v1" : "",
    api_key: "",
    model: "",
    api_key_configured: false,
  };
}

export function ModelConfigSettings({
  value,
  savedValue,
  onChange,
  onSaved,
}: {
  value: ModelConfigState;
  savedValue: ModelConfigState;
  onChange: (value: ModelConfigState) => void;
  onSaved: (value: ModelConfigState) => void;
}) {
  const [selectedId, setSelectedId] = useState<string | null>(value.active_profile_id ?? value.profiles[0]?.id ?? null);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [runtime, setRuntime] = useState<ProviderRuntime | null>(null);
  const [models, setModels] = useState<CatalogModel[]>([]);
  const [modelsLoading, setModelsLoading] = useState(false);
  const [authenticated, setAuthenticated] = useState(false);
  const [authRefreshGeneration, setAuthRefreshGeneration] = useState(0);
  const [working, setWorking] = useState(false);
  const [loginSessionId, setLoginSessionId] = useState<string | null>(null);
  const [loginStatus, setLoginStatus] = useState<LoginStatus["status"] | null>(null);
  const [loginEvents, setLoginEvents] = useState<LoginEvent[]>([]);
  const [loginAnswer, setLoginAnswer] = useState("");
  const [notice, setNotice] = useState<Notice | null>(null);
  const eventCursor = useRef(0);
  const pollInFlight = useRef(false);
  const testGeneration = useRef(0);
  const activeSession = useRef<string | null>(null);
  const sessionProfileId = useRef<string | null>(null);
  const authProfileIdentity = useRef<string | null>(null);

  const selected = value.profiles.find((profile) => profile.id === selectedId) ?? null;
  const selectedSaved = savedValue.profiles.find((profile) => profile.id === selectedId) ?? null;
  const selectedDirty = JSON.stringify(selected) !== JSON.stringify(selectedSaved);
  const hasDirtyProfiles = value.profiles.some((profile) =>
    JSON.stringify(profile) !== JSON.stringify(savedValue.profiles.find((saved) => saved.id === profile.id) ?? null),
  ) || value.profiles.length !== savedValue.profiles.length || value.active_profile_id !== savedValue.active_profile_id;
  const providerName = providers.find((provider) => provider.id === selected?.provider)?.name
    ?? (selected?.kind === "api" ? "OpenAI 兼容接口" : selected?.provider ?? "订阅服务商");
  const loginPrompt = [...loginEvents].reverse().find((event) => event.type === "prompt") ?? null;
  const visibleLoginEvents = useMemo(
    () => loginEvents.filter((event) => event.type !== "prompt" && event.type !== "completed").slice(-4),
    [loginEvents],
  );

  useEffect(() => {
    if (!selectedId && value.profiles.length) setSelectedId(value.active_profile_id ?? value.profiles[0].id);
  }, [selectedId, value.active_profile_id, value.profiles]);

  useEffect(() => {
    let live = true;
    api<{ runtime: ProviderRuntime; providers: Provider[] }>("/api/model-providers")
      .then((result) => {
        if (!live) return;
        setRuntime(result.runtime);
        setProviders(result.providers);
      })
      .catch((error) => {
        if (live) setNotice({ status: "error", message: error instanceof Error ? error.message : "读取订阅服务商失败" });
      });
    return () => { live = false; };
  }, []);

  useEffect(() => {
    if (!selected || selected.kind !== "subscription" || !selectedSaved || selectedSaved.kind !== "subscription" || selectedSaved.provider !== selected.provider) {
      authProfileIdentity.current = null;
      setAuthenticated(false);
      setModels([]);
      setModelsLoading(false);
      return;
    }
    let live = true;
    let loginConfirmed = false;
    const identity = `${selected.id}:${selected.provider}`;
    if (authProfileIdentity.current !== identity) {
      authProfileIdentity.current = identity;
      setAuthenticated(false);
      setModels([]);
    }
    api<{ authenticated: boolean; runtime: ProviderRuntime }>(`/api/model-configs/${selected.id}/auth`)
      .then(async (result) => {
        if (!live) return;
        setAuthenticated(result.authenticated);
        setRuntime(result.runtime);
        loginConfirmed = result.authenticated;
        if (!result.authenticated || !result.runtime.available) {
          setModels([]);
          return;
        }
        setModelsLoading(true);
        const catalog = await api<{ models: CatalogModel[] }>(`/api/model-configs/${selected.id}/models`);
        if (live) setModels(catalog.models);
      })
      .catch((error) => {
        if (!live) return;
        const message = error instanceof Error ? error.message : "读取登录状态失败";
        setNotice({ status: "error", message: loginConfirmed ? `订阅账号已连接，但读取模型目录失败：${message}` : message });
      })
      .finally(() => { if (live) setModelsLoading(false); });
    return () => { live = false; };
  }, [selected?.id, selected?.provider, selected?.kind, selectedSaved?.provider, selectedSaved?.kind, authRefreshGeneration]);

  useEffect(() => {
    if (!loginSessionId || !loginStatus || loginStatus !== "running") return;
    let live = true;
    const poll = async () => {
      if (pollInFlight.current || !live) return;
      pollInFlight.current = true;
      try {
        const status = await api<LoginStatus>(`/api/model-config-sessions/${loginSessionId}?after_event_id=${eventCursor.current}`);
        if (!live) return;
        eventCursor.current = status.next_event_id;
        setLoginStatus(status.status);
        if (status.events.length) setLoginEvents((events) => [...events, ...status.events]);
        if (status.status === "completed") {
          const profileId = sessionProfileId.current;
          if (profileId === selectedId && profileId) {
            // Changing loginStatus ends this polling effect. Refresh account
            // data in its own effect so cleanup cannot discard the response.
            setAuthenticated(true);
            setAuthRefreshGeneration((generation) => generation + 1);
          }
          setNotice({ status: "success", message: "订阅账号已连接，请选择模型、保存并测试。" });
        } else if (status.status === "cancelled" || status.status === "error" || status.status === "expired") {
          const message = status.events.at(-1)?.message ?? (status.status === "cancelled" ? "登录已取消。" : "订阅登录失败，请重试。");
          setNotice({ status: status.status === "cancelled" ? "running" : "error", message });
        }
        if (status.status !== "running") {
          activeSession.current = null;
          sessionProfileId.current = null;
        }
      } catch (error) {
        if (live) {
          setLoginStatus("error");
          setNotice({ status: "error", message: error instanceof Error ? error.message : "读取订阅登录状态失败" });
        }
      } finally {
        pollInFlight.current = false;
      }
    };
    void poll();
    const timer = window.setInterval(() => { void poll(); }, 1000);
    return () => { live = false; window.clearInterval(timer); };
  }, [loginSessionId, loginStatus, selectedId]);

  useEffect(() => () => {
    if (activeSession.current) void api(`/api/model-config-sessions/${activeSession.current}`, { method: "DELETE" }).catch(() => {});
  }, []);

  function updateProfile(updated: ModelProfile) {
    onChange({ ...value, profiles: value.profiles.map((profile) => profile.id === updated.id ? updated : profile) });
    testGeneration.current += 1;
    setNotice(null);
  }

  function discardProfileDraft(profileId: string): ModelConfigState {
    const saved = savedValue.profiles.find((profile) => profile.id === profileId);
    const profiles = saved
      ? value.profiles.map((profile) => profile.id === profileId ? saved : profile)
      : value.profiles.filter((profile) => profile.id !== profileId);
    return {
      ...value,
      profiles,
      active_profile_id: savedValue.active_profile_id,
    };
  }

  function selectProfile(profileId: string) {
    if (profileId === selectedId) return;
    if (selectedDirty && !window.confirm("此模型配置有尚未保存的修改，切换后放弃本组修改吗？")) return;
    if (selectedDirty && selectedId) onChange(discardProfileDraft(selectedId));
    testGeneration.current += 1;
    setNotice(null);
    setSelectedId(profileId);
  }

  function addProfile(kind: ModelProfile["kind"]) {
    if (selectedDirty && !window.confirm("此模型配置有尚未保存的修改，新增后放弃本组修改吗？")) return;
    const base = selectedDirty && selectedId ? discardProfileDraft(selectedId) : value;
    const provider = providers[0]?.id ?? "openai";
    const profile = newProfile(kind, provider);
    onChange({ ...base, profiles: [...base.profiles, profile] });
    testGeneration.current += 1;
    setSelectedId(profile.id);
    setLoginStatus(null);
    setLoginEvents([]);
    setNotice(null);
  }

  async function saveProfiles(
    candidate = value,
    options: { manageWorking?: boolean; clearNotice?: boolean } = {},
  ): Promise<ModelConfigState | null> {
    const manageWorking = options.manageWorking ?? true;
    if (manageWorking) setWorking(true);
    if (options.clearNotice ?? true) setNotice(null);
    try {
      const saved = await api<ModelConfigState>("/api/config", { method: "PUT", body: JSON.stringify(candidate) });
      onSaved(saved);
      return saved;
    } catch (error) {
      setNotice({ status: "error", message: error instanceof Error ? error.message : "保存模型配置失败" });
      return null;
    } finally {
      if (manageWorking) setWorking(false);
    }
  }

  async function testConnection() {
    if (!selected) return;
    const generation = ++testGeneration.current;
    setNotice({ status: "running", message: "正在发送一次最小 JSON 请求…" });
    try {
      const result = await api<{ message: string }>("/api/config/test", {
        method: "POST",
        body: JSON.stringify({ profile: selected }),
      });
      if (generation === testGeneration.current) setNotice({ status: "success", message: result.message });
    } catch (error) {
      if (generation === testGeneration.current) {
        setNotice({ status: "error", message: error instanceof Error ? error.message : "模型连接测试失败" });
      }
    }
  }

  async function activateProfile() {
    if (!selected || hasDirtyProfiles) return;
    setWorking(true);
    setNotice(null);
    try {
      const result = await api<ModelConfigState>("/api/config/active", {
        method: "POST",
        body: JSON.stringify({ profile_id: selected.id }),
      });
      onSaved(result);
      setNotice({ status: "success", message: `“${selected.name}”已设为当前模型配置。` });
    } catch (error) {
      setNotice({ status: "error", message: error instanceof Error ? error.message : "启用模型配置失败" });
    } finally {
      setWorking(false);
    }
  }

  async function deleteProfile() {
    if (!selected || !window.confirm(`删除“${selected.name}”并清除其本机凭据吗？`)) return;
    const candidate = {
      ...value,
      profiles: value.profiles.filter((profile) => profile.id !== selected.id),
      active_profile_id: value.active_profile_id === selected.id ? null : value.active_profile_id,
    };
    const saved = await saveProfiles(candidate);
    if (!saved) return;
    setSelectedId(saved.profiles[0]?.id ?? null);
    setModels([]);
    setAuthenticated(false);
    setNotice({ status: "success", message: "模型配置已删除。" });
  }

  async function startLogin() {
    if (!selected || selected.kind !== "subscription") return;
    setWorking(true);
    setNotice({ status: "running", message: "正在保存配置并启动订阅授权…" });
    try {
      const saved = await saveProfiles(value, { manageWorking: false, clearNotice: false });
      if (!saved) return;
      setSelectedId(selected.id);
      const result = await api<{ session_id: string; status: LoginStatus["status"] }>(
        `/api/model-configs/${selected.id}/auth/login`,
        { method: "POST" },
      );
      eventCursor.current = 0;
      setLoginEvents([]);
      setLoginStatus(result.status);
      activeSession.current = result.session_id;
      sessionProfileId.current = selected.id;
      setLoginSessionId(result.session_id);
      setNotice({ status: "running", message: "请按下方提示完成服务商授权；可随时取消。" });
    } catch (error) {
      setNotice({ status: "error", message: error instanceof Error ? error.message : "无法开始订阅登录" });
    } finally {
      setWorking(false);
    }
  }

  async function answerLoginPrompt(answer: string) {
    if (!loginSessionId || !loginPrompt?.prompt_id || !answer.trim()) return;
    setWorking(true);
    try {
      await api(`/api/model-config-sessions/${loginSessionId}/respond`, {
        method: "POST",
        body: JSON.stringify({ prompt_id: loginPrompt.prompt_id, answer }),
      });
      setLoginAnswer("");
      setLoginEvents((events) => events.filter((event) => event.id !== loginPrompt.id));
      setNotice({ status: "running", message: "正在等待服务商完成授权…" });
    } catch (error) {
      setNotice({ status: "error", message: error instanceof Error ? error.message : "提交登录信息失败" });
    } finally {
      setWorking(false);
    }
  }

  async function cancelLogin() {
    if (!loginSessionId) return;
    setWorking(true);
    try {
      await api(`/api/model-config-sessions/${loginSessionId}`, { method: "DELETE" });
      setLoginStatus("cancelled");
      activeSession.current = null;
      setNotice({ status: "running", message: "登录已取消。" });
    } catch (error) {
      setNotice({ status: "error", message: error instanceof Error ? error.message : "取消订阅登录失败" });
    } finally {
      setWorking(false);
    }
  }

  async function logout(relogin = false) {
    if (!selected || !window.confirm(relogin
      ? "重新登录会替换这组配置的本机授权凭据，继续吗？"
      : "退出此订阅账号并删除保存在本机的授权凭据吗？")) return;
    setWorking(true);
    try {
      if (selectedDirty) {
        const saved = await saveProfiles(value);
        if (!saved) return;
      }
      const result = await api<{ message: string }>(`/api/model-configs/${selected.id}/auth`, { method: "DELETE" });
      setAuthenticated(false);
      setModels([]);
      const fresh = await api<ModelConfigState>("/api/config");
      onSaved(fresh);
      setNotice({ status: "success", message: result.message });
      if (relogin) await startLogin();
    } catch (error) {
      setNotice({ status: "error", message: error instanceof Error ? error.message : "退出订阅账号失败" });
    } finally {
      setWorking(false);
    }
  }

  async function refreshModels() {
    if (!selected || selected.kind !== "subscription") return;
    setWorking(true);
    try {
      const result = await api<{ models: CatalogModel[] }>(`/api/model-configs/${selected.id}/models`);
      setModels(result.models);
      setNotice({ status: "success", message: `已读取 ${result.models.length} 个可用模型。` });
    } catch (error) {
      setNotice({ status: "error", message: error instanceof Error ? error.message : "刷新模型列表失败" });
    } finally {
      setWorking(false);
    }
  }

  return (
    <section className="model-config-settings" aria-label="模型配置">
      <aside className="model-profile-list">
        <div className="model-profile-heading">
          <div>
            <span className="model-overline">配置集</span>
            <h3>接入方式</h3>
          </div>
          <button
            className="model-add-button"
            type="button"
            title="新增模型配置"
            aria-label="新增模型配置"
            onClick={() => addProfile("api")}
          ><Plus size={17} /></button>
        </div>
        <button className="model-new-choice" type="button" onClick={() => addProfile("api")}>＋　API 接入</button>
        <button className="model-new-choice" type="button" onClick={() => addProfile("subscription")}>＋　订阅接入</button>
        <div className="model-profile-items" role="list" aria-label="已保存的模型配置">
          {value.profiles.map((profile) => {
            const active = value.active_profile_id === profile.id;
            const isSelected = selectedId === profile.id;
            const profileProviderName = providers.find((provider) => provider.id === profile.provider)?.name
              ?? (profile.kind === "api" ? "API 接入" : "订阅接入");
            return (
              <button
                className={`model-profile-item${isSelected ? " selected" : ""}`}
                type="button"
                role="listitem"
                aria-current={active ? "true" : undefined}
                key={profile.id}
                onClick={() => selectProfile(profile.id)}
              >
                <span className="model-profile-item-top">
                  <span className="model-profile-item-name">{profile.name || "未命名配置"}</span>
                  {active && <BadgeCheck size={15} aria-label="当前配置" />}
                </span>
                <span className="model-profile-item-subtitle">{profileProviderName}{profile.model ? ` · ${profile.model}` : ""}</span>
                <ChevronRight className="model-profile-chevron" size={15} />
              </button>
            );
          })}
          {value.profiles.length === 0 && (
            <p className="model-profile-empty">创建一组配置，文献 AI 功能就可以开始工作。</p>
          )}
        </div>
        <div className="model-profile-current">
          <span className={`model-status-light${value.active_profile_id ? " is-active" : ""}`} />
          <span>{value.active_profile_id ? "已有当前模型" : "尚未启用模型"}</span>
        </div>
      </aside>

      <div className="model-profile-editor">
        {selected ? (
          <>
            <header className="model-editor-heading">
              <div>
                <span className="model-overline">{selected.kind === "api" ? "API 接入" : "订阅接入"}　/　{providerName}</span>
                <h3>{selected.name || "未命名配置"}</h3>
              </div>
              <button className="model-icon-button model-danger-icon" type="button" onClick={() => void deleteProfile()} disabled={working} aria-label="删除这组配置" title="删除这组配置">
                <Trash2 size={17} />
              </button>
            </header>

            <label className="model-field">
              <span>配置名称</span>
              <input value={selected.name} disabled={working && Boolean(loginSessionId)} placeholder="例如：个人 ChatGPT" onChange={(event) => updateProfile({ ...selected, name: event.target.value })} />
            </label>

            {selected.kind === "api" ? (
              <div className="model-fields">
                <label className="model-field">
                  <span>API 兼容地址</span>
                  <input value={selected.base_url} placeholder="https://api.openai.com/v1" autoComplete="url" onChange={(event) => updateProfile({ ...selected, base_url: event.target.value })} />
                  <small>使用兼容 Chat Completions 的服务商接口地址。</small>
                </label>
                <label className="model-field">
                  <span>API Key</span>
                  <input value={selected.api_key} type="password" autoComplete="new-password" placeholder={selected.api_key_configured ? "已保存；留空以继续使用" : "sk-…"} onChange={(event) => updateProfile({ ...selected, api_key: event.target.value, api_key_configured: Boolean(event.target.value) })} />
                  <small>密钥保存在本机，页面返回时已自动遮盖。</small>
                </label>
                <label className="model-field">
                  <span>模型标识</span>
                  <input value={selected.model} placeholder="gpt-4.1-mini" autoComplete="off" onChange={(event) => updateProfile({ ...selected, model: event.target.value })} />
                </label>
              </div>
            ) : (
              <div className="model-fields">
                <label className="model-field">
                  <span>订阅服务商</span>
                  <select
                    value={selected.provider}
                    disabled={working || loginStatus === "running"}
                    onChange={(event) => {
                      updateProfile({ ...selected, provider: event.target.value, model: "" });
                      setAuthenticated(false);
                      setModels([]);
                    }}
                  >
                    {providers.map((provider) => <option key={provider.id} value={provider.id}>{provider.name}</option>)}
                    {!providers.some((provider) => provider.id === selected.provider) && <option value={selected.provider}>{selected.provider}</option>}
                  </select>
                  <small>使用已有的个人订阅账号，通过服务商 OAuth 登录。</small>
                </label>
                <div className="model-auth-card">
                  <span className={`model-auth-indicator${authenticated ? " connected" : ""}`} />
                  <div className="model-auth-copy">
                    <strong>{authenticated ? "账号已连接" : "尚未连接订阅账号"}</strong>
                    <span>{authenticated ? "授权凭据仅保存在此电脑的本机数据目录。" : runtime?.message ?? "读取订阅运行环境…"}</span>
                  </div>
                  {authenticated ? (
                    <div className="model-auth-actions">
                      <button className="model-secondary-button" type="button" disabled={working} onClick={() => void logout(true)}><RefreshCw size={15} />重新登录</button>
                      <button className="model-secondary-button" type="button" disabled={working} onClick={() => void logout()}><LogOut size={15} />退出账号</button>
                    </div>
                  ) : (
                    <button className="model-connect-button" type="button" disabled={working || !runtime?.available} onClick={() => void startLogin()}>
                      {working ? <Loader2 size={15} className="spin" /> : <LogIn size={15} />}
                      登录订阅
                    </button>
                  )}
                </div>
                <label className="model-field">
                  <span className="model-label-row"><span>订阅模型</span><button className="model-refresh-link" type="button" disabled={!authenticated || working || modelsLoading} onClick={() => void refreshModels()}><RefreshCw size={14} />刷新目录</button></span>
                  <select value={selected.model} disabled={!authenticated || working || modelsLoading} onChange={(event) => updateProfile({ ...selected, model: event.target.value })}>
                    <option value="">{modelsLoading ? "正在读取当前账号可用模型…" : "选择一个当前账号可用的模型"}</option>
                    {models.map((model) => <option value={model.id} key={model.id}>{model.name === model.id ? model.id : `${model.name}（${model.id}）`}</option>)}
                    {selected.model && !models.some((model) => model.id === selected.model) && <option value={selected.model}>{selected.model}（已保存）</option>}
                  </select>
                  <small>可用模型依据该订阅账号的模型目录；启用前仍需测试一次连接。</small>
                </label>

                {loginStatus === "running" && loginSessionId && (
                  <section className="model-login-session" aria-live="polite">
                    <div className="model-login-session-heading">
                      <span className="model-status-light is-active" />
                      <strong>等待浏览器授权</strong>
                      <button className="model-refresh-link" type="button" disabled={working} onClick={() => void cancelLogin()}>取消登录</button>
                    </div>
                    {visibleLoginEvents.map((event) => (
                      <div className="model-login-event" key={event.id}>
                        {event.type === "auth_url" && event.url && <a href={event.url} target="_blank" rel="noreferrer">打开 {providerName} 授权页面</a>}
                        {event.type === "device_code" && <p className="model-device-code">登录验证码：<strong>{event.userCode}</strong>　<a href={event.verificationUri} target="_blank" rel="noreferrer">前往验证</a></p>}
                        {event.type === "info" && event.links?.map((link) => <a href={link.url} target="_blank" rel="noreferrer" key={link.url}>{link.label ?? "更多信息"}</a>)}
                        {(event.type === "info" || event.type === "progress") && <p>{event.message}</p>}
                      </div>
                    ))}
                    {loginPrompt?.prompt && (
                      <form className="model-login-prompt" onSubmit={(event) => { event.preventDefault(); void answerLoginPrompt(loginAnswer); }}>
                        <label className="model-field">
                          <span>{loginPrompt.prompt.message}</span>
                          {loginPrompt.prompt.type === "select" ? (
                            <select autoFocus value={loginAnswer} onChange={(event) => setLoginAnswer(event.target.value)}>
                              <option value="">选择一个选项</option>
                              {(loginPrompt.prompt.options ?? []).map((option) => <option key={option.id} value={option.id}>{option.label}{option.description ? ` — ${option.description}` : ""}</option>)}
                            </select>
                          ) : (
                            <input autoFocus type={loginPrompt.prompt.type === "secret" ? "password" : "text"} value={loginAnswer} placeholder={loginPrompt.prompt.placeholder ?? "完成后返回此处"} onChange={(event) => setLoginAnswer(event.target.value)} />
                          )}
                        </label>
                        <button className="model-connect-button" type="submit" disabled={working || !loginAnswer.trim()}><Check size={15} />继续</button>
                      </form>
                    )}
                  </section>
                )}
              </div>
            )}

            <div className="model-action-footer">
              <button className="primary-button model-save-button" type="button" onClick={() => void saveProfiles()} disabled={working || !selectedDirty && !hasDirtyProfiles}>
                {working ? <Loader2 size={16} className="spin" /> : <Save size={16} />}
                保存配置
              </button>
              <button className="ghost-button" type="button" onClick={() => void testConnection()} disabled={working || Boolean(loginSessionId && loginStatus === "running")}><RefreshCw size={15} />测试连接</button>
              <button className="model-activate-button" type="button" onClick={() => void activateProfile()} disabled={working || hasDirtyProfiles || value.active_profile_id === selected.id} title={hasDirtyProfiles ? "先保存修改，再启用配置" : "将这组配置用于后续 AI 请求"}>设为当前配置</button>
            </div>
            {notice && <p className={`settings-inline-status ${notice.status}`} role="status">{notice.message}</p>}
            <p className="model-footnote">保存和启用分开。连接测试仅在点击时调用当前草稿，不会切换当前配置。</p>
          </>
        ) : (
          <div className="model-empty-editor">
            <span className="model-overline">本地文献 AI</span>
            <h3>先选择接入方式</h3>
            <p>添加 API 配置或订阅账号。文献首页文本只会通过当前启用的模型处理。</p>
            <button className="primary-button" type="button" onClick={() => addProfile("api")}><Plus size={16} />新增模型配置</button>
          </div>
        )}
      </div>
    </section>
  );
}
