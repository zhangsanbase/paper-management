import { useEffect, useState } from "react";
import { FolderOpen, Loader2 } from "lucide-react";

import { api } from "../api/client";
import type { PdfViewerState, Toast } from "../types";

type Props = {
  onNotice: (message: string, type?: Toast["type"]) => void;
};

export function PdfViewerSettings({ onNotice }: Props) {
  const [settings, setSettings] = useState<PdfViewerState | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    api<PdfViewerState>("/api/pdf-viewer")
      .then((value) => {
        if (mounted) setSettings(value);
      })
      .catch((cause) => {
        if (mounted) setError(cause instanceof Error ? cause.message : "读取 PDF 阅读器设置失败。");
      });
    return () => { mounted = false; };
  }, []);

  async function save(mode: "system" | "custom", executablePath?: string) {
    setBusy(true);
    setError(null);
    try {
      const value = await api<PdfViewerState>("/api/pdf-viewer", {
        method: "PUT",
        body: JSON.stringify({ mode, executable_path: executablePath ?? null })
      });
      setSettings(value);
      onNotice(mode === "custom" ? "已使用自选 PDF 阅读器。" : "已使用系统默认 PDF 阅读器。", "success");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "保存 PDF 阅读器设置失败。");
    } finally {
      setBusy(false);
    }
  }

  async function chooseProgram() {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const result = await api<{ cancelled: boolean; path: string | null }>("/api/pdf-viewer/select", {
        method: "POST"
      });
      if (result.cancelled || !result.path) return;
      const value = await api<PdfViewerState>("/api/pdf-viewer", {
        method: "PUT",
        body: JSON.stringify({ mode: "custom", executable_path: result.path })
      });
      setSettings(value);
      onNotice("已使用自选 PDF 阅读器。", "success");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "选择 PDF 阅读器失败。");
    } finally {
      setBusy(false);
    }
  }

  if (!settings) {
    return (
      <div className="pdf-viewer-settings">
        <h3>PDF 阅读器</h3>
        <p className="muted">{error ?? "正在读取设置…"}</p>
      </div>
    );
  }

  return (
    <div className="pdf-viewer-settings">
      <h3>PDF 阅读器</h3>
      <p className="muted">此设置用于主文献和 PDF 补充文件；其他补充文件仍由系统决定打开软件。</p>
      <fieldset className="pdf-viewer-options" disabled={busy}>
        <legend>打开方式</legend>
        <label>
          <input
            type="radio"
            name="pdf-viewer-mode"
            checked={settings.mode === "system"}
            onChange={() => { if (settings.mode !== "system") void save("system"); }}
          />
          <span>系统默认程序</span>
        </label>
        <label>
          <input
            type="radio"
            name="pdf-viewer-mode"
            checked={settings.mode === "custom"}
            disabled={!settings.supported}
            onChange={() => {
              if (settings.mode === "custom") return;
              if (settings.selected_available) void save("custom");
              else void chooseProgram();
            }}
          />
          <span>自选程序</span>
        </label>
      </fieldset>
      <label className="pdf-viewer-path">
        已选程序
        <input value={settings.executable_path ?? "尚未选择"} title={settings.executable_path ?? ""} readOnly />
      </label>
      <div className="pdf-viewer-actions">
        <button type="button" className="ghost-button" onClick={() => void chooseProgram()} disabled={!settings.supported || busy}>
          {busy ? <Loader2 size={17} className="spin" /> : <FolderOpen size={17} />}
          {settings.executable_path ? "更换程序" : "选择程序"}
        </button>
      </div>
      <p className="help-text settings-field-help">当前实际使用：{settings.effective_mode === "custom" ? "自选程序" : "系统默认程序"}。自选程序需能接收 PDF 文件路径作为启动参数。</p>
      {!settings.supported && <p className="help-text settings-field-help">自选程序目前仅支持 Windows。</p>}
      {settings.warning && <p className="settings-inline-status error" role="status">{settings.warning}</p>}
      {error && <p className="settings-inline-status error" role="alert">{error}</p>}
    </div>
  );
}
