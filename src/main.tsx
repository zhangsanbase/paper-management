import React, { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  FilePlus2,
  FolderOpen,
  Loader2,
  RefreshCw,
  Save,
  Search,
  Settings,
  Tag,
  Trash2,
  X
} from "lucide-react";
import "./styles.css";
import type {
  AddTagDialog,
  ApiConfig,
  AppState,
  ConflictResolveResult,
  FileConflict,
  Paper,
  PaperAttachment,
  PaperContextMenu,
  PartitionBatchScope,
  PartitionBatchStatus,
  PartitionSuggestion,
  RemovePaperDialog,
  SettingsTab,
  SortMode,
  TagItem,
  TagSuggestion,
  Toast
} from "./types";
import { api, isFileConflict } from "./api/client";
import { splitTextList, tagMatchesQuery } from "./utils";
import { DetailActionRail, DetailPane } from "./components/PaperDetails";
import { PdfViewerSettings } from "./components/PdfViewerSettings";

type ErrorBoundaryState = {
  error: Error | null;
};

const emptyState: AppState = {
  papers: [],
  tags: [],
  config: { base_url: "", api_key: "", model: "" },
  library: { path: "" },
  counts: { all: 0, unclassified: 0, failed: 0, pending_suggestions: 0 }
};

class ErrorBoundary extends React.Component<{ children: React.ReactNode }, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  render() {
    if (this.state.error) {
      return (
        <div className="app-error">
          <div>
            <p className="eyebrow">Local Paper Index</p>
            <h1>界面加载出错</h1>
            <p>前端组件渲染时遇到异常。请刷新页面；如果仍然出现，把下面这行错误发给我即可。</p>
            <code>{this.state.error.message}</code>
            <button className="primary-button" onClick={() => window.location.reload()}>
              <RefreshCw size={18} />
              刷新页面
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

function statusText(status: string): string {
  const map: Record<string, string> = {
    pending: "等待处理",
    processing: "处理中",
    ready: "已完成",
    needs_config: "待配置",
    ai_failed: "提取失败"
  };
  return map[status] ?? status;
}

function isPaper(value: ConflictResolveResult): value is Paper {
  return typeof value === "object" && value !== null && "id" in value && "file_path" in value;
}

function compact(values: string[], fallback = "未识别"): string {
  const text = values.filter(Boolean).join("；");
  return text || fallback;
}

function aliasSummary(tag: TagItem): string {
  return tag.aliases.filter((alias) => alias.trim() && alias.trim() !== tag.name).join("；");
}

function App() {
  const [state, setState] = useState<AppState>(emptyState);
  const [activeTag, setActiveTag] = useState<string>("");
  const [selectedTagIds, setSelectedTagIds] = useState<string[]>([]);
  const [sortOrder, setSortOrder] = useState<SortMode>("imported_desc");
  const [query, setQuery] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<Toast | null>(null);
  const [showConfig, setShowConfig] = useState(false);
  const [settingsTab, setSettingsTab] = useState<SettingsTab>("api");
  const [config, setConfig] = useState<ApiConfig>(emptyState.config);
  const [savedConfig, setSavedConfig] = useState<ApiConfig>(emptyState.config);
  const configInitialized = useRef(false);
  const aiTestGeneration = useRef(0);
  const [aiTestState, setAiTestState] = useState<{ status: "running" | "success" | "error"; message: string } | null>(null);
  const [newTagName, setNewTagName] = useState("");
  const [newTagAliases, setNewTagAliases] = useState("");
  const [topicTagQuery, setTopicTagQuery] = useState("");
  const [settingsTagQuery, setSettingsTagQuery] = useState("");
  const [editingTag, setEditingTag] = useState<TagItem | null>(null);
  const [tagDraft, setTagDraft] = useState({ name: "", aliases: "", description: "" });
  const [aiBusy, setAiBusy] = useState<string | null>(null);
  const [partitionBatchStatus, setPartitionBatchStatus] = useState<PartitionBatchStatus | null>(null);
  const [partitionBatchScope, setPartitionBatchScope] = useState<PartitionBatchScope>("all");
  const [showTitleTranslation, setShowTitleTranslation] = useState(true);
  const [recentTagPick, setRecentTagPick] = useState<{ paperId: string; tagId: string; mode: "select" | "unselect" } | null>(null);
  const [paperContextMenu, setPaperContextMenu] = useState<PaperContextMenu | null>(null);
  const [addTagDialog, setAddTagDialog] = useState<AddTagDialog | null>(null);
  const [removePaperDialog, setRemovePaperDialog] = useState<RemovePaperDialog | null>(null);
  const [tagSubmitting, setTagSubmitting] = useState(false);
  const [paperRemoving, setPaperRemoving] = useState(false);
  const [removePaperError, setRemovePaperError] = useState<string | null>(null);
  const [fileConflict, setFileConflict] = useState<FileConflict | null>(null);

  const selectedPaper = useMemo(
    () => state.papers.find((paper) => paper.id === selectedId) ?? state.papers[0] ?? null,
    [selectedId, state.papers]
  );

  const yearTags = useMemo(() => state.tags.filter((tag) => tag.kind === "year"), [state.tags]);
  const topicTags = useMemo(() => state.tags.filter((tag) => tag.kind === "topic"), [state.tags]);
  const visibleTopicTags = useMemo(
    () => topicTags.filter((tag) => tagMatchesQuery(tag, topicTagQuery)),
    [topicTags, topicTagQuery]
  );
  const visibleSettingsTags = useMemo(
    () => topicTags.filter((tag) => tagMatchesQuery(tag, settingsTagQuery)),
    [topicTags, settingsTagQuery]
  );

  function showToast(text: string, type: Toast["type"] = "info") {
    setToast({ text, type });
  }

  useEffect(() => {
    if (!toast) return;
    const timeout = toast.type === "error" ? 6000 : 2500;
    const timer = window.setTimeout(() => setToast(null), timeout);
    return () => window.clearTimeout(timer);
  }, [toast]);

  async function load(tag = activeTag, sort = sortOrder, tagIds = selectedTagIds) {
    const params = new URLSearchParams();
    if (tag) params.set("tag_id", tag);
    for (const tagId of tagIds) params.append("tag_ids", tagId);
    params.set("sort", sort);
    const data = await api<AppState>(`/api/state?${params}`);
    setState(data);
    if (!configInitialized.current) {
      configInitialized.current = true;
      setConfig(data.config);
      setSavedConfig(data.config);
    }
    if (selectedId && !data.papers.some((paper) => paper.id === selectedId)) {
      setSelectedId(data.papers[0]?.id ?? null);
    }
  }

  function hasUnsavedTagSettings() {
    const tagDraftChanged = editingTag && (
      tagDraft.name !== editingTag.name ||
      tagDraft.aliases !== editingTag.aliases.join("；") ||
      tagDraft.description !== editingTag.description
    );
    return Boolean(tagDraftChanged || newTagName.trim() || newTagAliases.trim());
  }

  function requestCloseSettings(overrides?: { configDraft?: ApiConfig; savedConfig?: ApiConfig }) {
    const nextSavedConfig = overrides?.savedConfig ?? savedConfig;
    const nextConfigDraft = overrides?.configDraft ?? config;
    const hasUnsaved = JSON.stringify(nextConfigDraft) !== JSON.stringify(nextSavedConfig) || hasUnsavedTagSettings();
    if (hasUnsaved && !window.confirm("设置中有尚未保存的更改，放弃这些更改并关闭吗？")) return;
    if (hasUnsaved) {
      setConfig(nextSavedConfig);
      setNewTagName("");
      setNewTagAliases("");
      setEditingTag(null);
      setTagDraft({ name: "", aliases: "", description: "" });
    }
    aiTestGeneration.current += 1;
    setAiTestState(null);
    setShowConfig(false);
  }

  useEffect(() => {
    load().catch((error) => showToast(error.message, "error"));
  }, []);

  useEffect(() => {
    if (!showConfig || settingsTab !== "partitions" || partitionBatchStatus?.status !== "running") return;
    const timer = window.setTimeout(() => {
      api<PartitionBatchStatus>("/api/partitions/lookup-all/status")
        .then(async (status) => {
          setPartitionBatchStatus(status);
          if (status.status !== "running") {
            await load().catch((error) => showToast(error.message, "error"));
          }
        })
        .catch((error) => showToast(error instanceof Error ? error.message : "读取补查状态失败", "error"));
    }, 1200);
    return () => window.clearTimeout(timer);
  }, [showConfig, settingsTab, partitionBatchStatus]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      if (state.papers.some((paper) => ["pending", "processing"].includes(paper.status))) {
        load().catch(() => undefined);
      }
    }, 2500);
    return () => window.clearInterval(timer);
  }, [state.papers, activeTag, sortOrder, selectedTagIds]);

  useEffect(() => {
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key !== "Escape") return;
      if (showConfig) {
        requestCloseSettings();
        return;
      }
      setPaperContextMenu(null);
      if (!tagSubmitting) setAddTagDialog(null);
      if (!paperRemoving) setRemovePaperDialog(null);
    }
    function closeOnOutsideClick(event: MouseEvent) {
      if (event.target instanceof Element && !event.target.closest(".paper-context-menu")) {
        setPaperContextMenu(null);
      }
    }
    window.addEventListener("keydown", closeOnEscape);
    window.addEventListener("mousedown", closeOnOutsideClick);
    return () => {
      window.removeEventListener("keydown", closeOnEscape);
      window.removeEventListener("mousedown", closeOnOutsideClick);
    };
  }, [showConfig, config, savedConfig, editingTag, tagDraft, newTagName, newTagAliases, tagSubmitting, paperRemoving]);

  const filteredPapers = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return state.papers;
    return state.papers.filter((paper) => {
      const haystack = [
        paper.title,
        paper.title_zh,
        paper.abstract,
        paper.notes,
        paper.file_name,
        paper.publication_date,
        paper.doi_url,
        paper.journal_name,
        ...paper.authors,
        ...paper.affiliations,
        ...paper.tags.map((tag) => tag.name)
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      return haystack.includes(needle);
    });
  }, [query, state.papers]);

  function applyPaper(nextPaper: Paper) {
    setState((current) => ({
      ...current,
      papers: current.papers.map((paper) => (paper.id === nextPaper.id ? nextPaper : paper))
    }));
    setSelectedId(nextPaper.id);
  }

  function showConflictIfNeeded(error: unknown): boolean {
    if (!isFileConflict(error)) return false;
    setFileConflict(error.body);
    return true;
  }

  async function selectFiles() {
    setBusy(true);
    showToast("正在等待文件选择...");
    try {
      const result = await api<{ added: string[]; skipped: string[] }>("/api/papers/select", { method: "POST" });
      showToast(`新增 ${result.added.length} 篇，跳过重复 ${result.skipped.length} 篇`, "success");
      await load();
    } catch (error) {
      showToast(error instanceof Error ? error.message : "导入失败", "error");
    } finally {
      setBusy(false);
    }
  }

  async function openPaper(paper: Paper) {
    try {
      const result = await api<{ status: string; message?: string }>(`/api/papers/${paper.id}/open`, { method: "POST" });
      showToast(result.message ?? "已请求打开 PDF，请查看任务栏。", "success");
    } catch (error) {
      showToast(error instanceof Error ? error.message : "打开失败", "error");
    }
  }

  function openDoi(paper: Paper) {
    if (!paper.doi_url) {
      showToast("暂无 DOI 来源", "info");
      return;
    }
    window.open(paper.doi_url, "_blank", "noopener,noreferrer");
  }

  async function openFolder(paper: Paper) {
    try {
      const result = await api<{ status: string; message?: string }>(`/api/papers/${paper.id}/open-folder`, { method: "POST" });
      showToast(result.message ?? "已打开 PDF 所在文件夹。", "success");
    } catch (error) {
      showToast(error instanceof Error ? error.message : "打开文件夹失败", "error");
    }
  }

  async function relinkFile(paper: Paper) {
    try {
      const nextPaper = await api<Paper>(`/api/papers/${paper.id}/relink-file`, { method: "POST" });
      applyPaper(nextPaper);
      showToast("本地 PDF 链接已更新", "success");
    } catch (error) {
      if (showConflictIfNeeded(error)) return;
      showToast(error instanceof Error ? error.message : "重新链接文件失败", "error");
    }
  }

  async function selectAttachments(paper: Paper) {
    try {
      const nextPaper = await api<Paper>(`/api/papers/${paper.id}/attachments/select`, { method: "POST" });
      applyPaper(nextPaper);
      showToast("补充文件已关联", "success");
    } catch (error) {
      if (showConflictIfNeeded(error)) return;
      showToast(error instanceof Error ? error.message : "添加补充文件失败", "error");
    }
  }

  async function openLibraryFolder() {
    try {
      const result = await api<{ status: string; message?: string }>("/api/library/open", { method: "POST" });
      showToast(result.message ?? "已打开文件库文件夹。", "success");
    } catch (error) {
      showToast(error instanceof Error ? error.message : "打开文件库失败", "error");
    }
  }

  async function openConflictFolder(conflict: FileConflict) {
    try {
      const result = await api<{ status: string; message?: string }>(`/api/file-conflicts/${conflict.conflict_id}/open-folder`, { method: "POST" });
      showToast(result.message ?? "已打开同名文件所在文件夹。", "success");
    } catch (error) {
      showToast(error instanceof Error ? error.message : "打开文件夹失败", "error");
    }
  }

  async function resolveFileConflict(conflict: FileConflict, action: "auto_number" | "use_existing" | "cancel") {
    try {
      const result = await api<ConflictResolveResult>(`/api/file-conflicts/${conflict.conflict_id}/resolve`, {
        method: "POST",
        body: JSON.stringify({ action })
      });
      if (isPaper(result)) {
        applyPaper(result);
        showToast(action === "auto_number" ? "已自动编号并更新记录" : "已改为使用已有文件", "success");
      } else {
        showToast(result.message ?? "已取消文件处理", "info");
      }
      setFileConflict(null);
    } catch (error) {
      showToast(error instanceof Error ? error.message : "处理文件冲突失败", "error");
    }
  }

  async function openAttachment(paper: Paper, attachment: PaperAttachment) {
    try {
      const result = await api<{ status: string; message?: string }>(
        `/api/papers/${paper.id}/attachments/${attachment.id}/open`,
        { method: "POST" }
      );
      showToast(result.message ?? "已打开补充文件。", "success");
    } catch (error) {
      showToast(error instanceof Error ? error.message : "打开补充文件失败", "error");
    }
  }

  async function openAttachmentFolder(paper: Paper, attachment: PaperAttachment) {
    try {
      const result = await api<{ status: string; message?: string }>(
        `/api/papers/${paper.id}/attachments/${attachment.id}/open-folder`,
        { method: "POST" }
      );
      showToast(result.message ?? "已打开补充文件所在文件夹。", "success");
    } catch (error) {
      showToast(error instanceof Error ? error.message : "打开补充文件夹失败", "error");
    }
  }

  async function removeAttachment(paper: Paper, attachment: PaperAttachment) {
    const confirmed = window.confirm(`只移除补充文件关联，不会删除本地文件。\n\n${attachment.file_name}`);
    if (!confirmed) return;
    try {
      const nextPaper = await api<Paper>(`/api/papers/${paper.id}/attachments/${attachment.id}`, { method: "DELETE" });
      applyPaper(nextPaper);
      showToast("补充文件关联已移除", "success");
    } catch (error) {
      showToast(error instanceof Error ? error.message : "移除补充文件失败", "error");
    }
  }

  async function reprocess(paper: Paper) {
    try {
      await api(`/api/papers/${paper.id}/reprocess`, { method: "POST" });
      showToast("已加入重新提取队列", "success");
      await load();
    } catch (error) {
      showToast(error instanceof Error ? error.message : "重新提取失败", "error");
    }
  }

  function openRemovePaper(paper: Paper) {
    setPaperContextMenu(null);
    setRemovePaperError(null);
    setRemovePaperDialog({ paper, deleteLocalFiles: false });
  }

  async function removePaper() {
    if (!removePaperDialog || paperRemoving) return;
    const { paper, deleteLocalFiles } = removePaperDialog;
    setPaperRemoving(true);
    setRemovePaperError(null);
    try {
      const suffix = deleteLocalFiles ? "?delete_local_files=true" : "";
      const result = await api<{ status: string; moved_files: string[]; missing_files: string[] }>(
        `/api/papers/${paper.id}${suffix}`,
        { method: "DELETE" }
      );
      setRemovePaperDialog(null);
      if (deleteLocalFiles) {
        const missing = result.missing_files.length ? `，${result.missing_files.length} 个文件原本缺失` : "";
        showToast(`文献已移除，${result.moved_files.length} 个文件已移入回收站${missing}`, "success");
      } else {
        showToast("已从文献管理器移除，本地文件未删除", "success");
      }
      setSelectedId(null);
      load().catch((error) => showToast(error.message, "error"));
    } catch (error) {
      setRemovePaperError(error instanceof Error ? error.message : "移除失败");
    } finally {
      setPaperRemoving(false);
    }
  }

  async function savePaper(paper: Paper) {
    try {
      const nextPaper = await api<Paper>(`/api/papers/${paper.id}`, {
        method: "PATCH",
        body: JSON.stringify({
          title: paper.title,
          title_zh: paper.title_zh,
          abstract: paper.abstract,
          notes: paper.notes,
          authors: paper.authors,
          affiliations: paper.affiliations,
          publication_date: paper.publication_date,
          doi_url: paper.doi_url,
          journal_name: paper.journal_name
        })
      });
      applyPaper(nextPaper);
      showToast("文献信息已保存", "success");
      await load();
    } catch (error) {
      showToast(error instanceof Error ? error.message : "保存失败", "error");
    }
  }

  async function lookupPartition(paper: Paper) {
    setAiBusy(`partition:${paper.id}`);
    try {
      const result = await api<{ paper: Paper; matched: boolean; journal_name: string | null }>(
        `/api/papers/${paper.id}/lookup-partition`,
        { method: "POST", body: JSON.stringify({ journal_name: paper.journal_name }) }
      );
      applyPaper({
        ...result.paper,
        title: paper.title,
        title_zh: paper.title_zh,
        abstract: paper.abstract,
        authors: paper.authors,
        affiliations: paper.affiliations,
        publication_date: paper.publication_date,
        doi_url: paper.doi_url
      });
      showToast(
        result.matched ? "期刊分区已更新，候选标签已放入待确认区" : `未匹配到分区${result.journal_name ? `：${result.journal_name}` : "，也未识别到期刊名"}`,
        result.matched ? "success" : "info"
      );
    } catch (error) {
      showToast(error instanceof Error ? error.message : "期刊分区查询失败", "error");
    } finally {
      setAiBusy(null);
    }
  }

  async function lookupAllPartitions() {
    try {
      const status = await api<PartitionBatchStatus>("/api/partitions/lookup-all", {
        method: "POST",
        body: JSON.stringify({ scope: partitionBatchScope })
      });
      setPartitionBatchStatus(status);
      showToast(
        status.scope === "unchecked" ? "已开始补查未查询或上次失败的文献" : "已开始补查全部文献的期刊分区"
      );
    } catch (error) {
      showToast(error instanceof Error ? error.message : "启动批量补查失败", "error");
    }
  }

  async function cancelPartitionLookup() {
    try {
      const status = await api<PartitionBatchStatus>("/api/partitions/lookup-all/cancel", { method: "POST" });
      setPartitionBatchStatus(status);
      if (status.cancel_requested) showToast("当前文献查询结束后将停止补查");
    } catch (error) {
      showToast(error instanceof Error ? error.message : "停止批量补查失败", "error");
    }
  }

  useEffect(() => {
    function saveOnShortcut(event: KeyboardEvent) {
      if (event.key.toLowerCase() !== "s" || (!event.ctrlKey && !event.metaKey)) return;
      event.preventDefault();
      if (event.repeat) return;
      if (showConfig) return;
      if (!selectedPaper) {
        showToast("当前没有可保存的文献", "info");
        return;
      }
      savePaper(selectedPaper).catch(() => undefined);
    }

    window.addEventListener("keydown", saveOnShortcut);
    return () => window.removeEventListener("keydown", saveOnShortcut);
  }, [showConfig, selectedPaper, activeTag, sortOrder, selectedTagIds]);

  async function translateTitle(paper: Paper) {
    if (paper.title_zh && !window.confirm("当前已有标题翻译，是否用 AI 结果覆盖？")) return;
    setAiBusy(`translate:${paper.id}`);
    try {
      const nextPaper = await api<Paper>(`/api/papers/${paper.id}/translate-title`, { method: "POST" });
      applyPaper(nextPaper);
      showToast("标题翻译已生成", "success");
    } catch (error) {
      showToast(error instanceof Error ? error.message : "标题翻译失败", "error");
    } finally {
      setAiBusy(null);
    }
  }

  async function generateAbstract(paper: Paper) {
    if (paper.abstract && !window.confirm("当前已有摘要，是否用 AI 结果覆盖？")) return;
    setAiBusy(`abstract:${paper.id}`);
    try {
      const nextPaper = await api<Paper>(`/api/papers/${paper.id}/generate-abstract`, { method: "POST" });
      applyPaper(nextPaper);
      showToast("摘要已生成", "success");
    } catch (error) {
      showToast(error instanceof Error ? error.message : "摘要生成失败", "error");
    } finally {
      setAiBusy(null);
    }
  }

  async function createTag() {
    const name = newTagName.trim();
    if (!name) return;
    try {
      const aliases = newTagAliases.trim() ? splitTextList(newTagAliases) : [name];
      await api<TagItem>("/api/tags", {
        method: "POST",
        body: JSON.stringify({ name, aliases, description: "" })
      });
      setNewTagName("");
      setNewTagAliases("");
      showToast("主题标签已创建", "success");
      await load();
    } catch (error) {
      showToast(error instanceof Error ? error.message : "创建标签失败", "error");
    }
  }

  function openPaperContextMenu(event: React.MouseEvent, paper: Paper) {
    event.preventDefault();
    setSelectedId(paper.id);
    setPaperContextMenu({
      paper,
      x: Math.max(8, Math.min(event.clientX, window.innerWidth - 192)),
      y: Math.max(8, Math.min(event.clientY, window.innerHeight - 96))
    });
  }

  function openAddTagDialog(paper: Paper) {
    setPaperContextMenu(null);
    setAddTagDialog({ paper, name: "", aliases: "" });
  }

  async function submitQuickTag() {
    if (!addTagDialog || tagSubmitting) return;
    const name = addTagDialog.name.trim();
    if (!name) return;
    const paper = state.papers.find((item) => item.id === addTagDialog.paper.id) ?? addTagDialog.paper;
    const currentTopicTags = paper.tags.filter((item) => item.kind === "topic");
    if (currentTopicTags.length >= 5) {
      showToast("这篇文献已有 5 个主题标签，请先移除一个", "error");
      return;
    }
    setTagSubmitting(true);
    try {
      const tag = await api<TagItem>("/api/tags", {
        method: "POST",
        body: JSON.stringify({ name, aliases: splitTextList(addTagDialog.aliases || name), description: "" })
      });
      if (currentTopicTags.some((item) => item.id === tag.id)) {
        showToast("这篇文献已经有关联标签", "info");
        setAddTagDialog(null);
        return;
      }
      setRecentTagPick({ paperId: paper.id, tagId: tag.id, mode: "select" });
      const nextPaper = await api<Paper>(`/api/papers/${paper.id}/tags`, {
        method: "POST",
        body: JSON.stringify({ tag_ids: [tag.id, ...currentTopicTags.map((item) => item.id)] })
      });
      applyPaper(nextPaper);
      setAddTagDialog(null);
      showToast(
        topicTags.some((item) => item.id === tag.id)
          ? "已关联已有标签；如需调整别名，请到标签设置编辑"
          : "标签已创建并关联到文献",
        "success"
      );
      await load();
    } catch (error) {
      showToast(error instanceof Error ? error.message : "添加标签失败", "error");
    } finally {
      setTagSubmitting(false);
    }
  }

  function openTagEditor(tag: TagItem) {
    setEditingTag(tag);
    setTagDraft({
      name: tag.name,
      aliases: tag.aliases.join("；"),
      description: tag.description
    });
  }

  async function saveTagEdit() {
    if (!editingTag) return;
    try {
      await api<TagItem>(`/api/tags/${editingTag.id}`, {
        method: "PUT",
        body: JSON.stringify({
          name: tagDraft.name.trim(),
          aliases: splitTextList(tagDraft.aliases),
          description: tagDraft.description.trim()
        })
      });
      setEditingTag(null);
      showToast("标签已更新", "success");
      await load();
    } catch (error) {
      showToast(error instanceof Error ? error.message : "标签更新失败", "error");
    }
  }

  async function removeTag(tag: TagItem) {
    const confirmed = window.confirm(`移除主题标签会把它从所有文献上解绑，但不会删除任何文献或 PDF。\n\n${tag.name}`);
    if (!confirmed) return;
    try {
      await api<{ status: string; tag: TagItem }>(`/api/tags/${tag.id}`, { method: "DELETE" });
      if (activeTag === tag.id) {
        setActiveTag("");
      }
      const nextTagIds = selectedTagIds.filter((tagId) => tagId !== tag.id);
      setSelectedTagIds(nextTagIds);
      setEditingTag(null);
      showToast("主题标签已移除", "success");
      await load(activeTag === tag.id ? "" : activeTag, sortOrder, nextTagIds);
    } catch (error) {
      showToast(error instanceof Error ? error.message : "移除标签失败", "error");
    }
  }

  async function setPaperTags(paper: Paper, tagId: string) {
    const currentTopicTags = paper.tags.filter((tag) => tag.kind === "topic");
    const exists = currentTopicTags.some((tag) => tag.id === tagId);
    const tag = topicTags.find((item) => item.id === tagId);
    const next = exists ? currentTopicTags.filter((item) => item.id !== tagId) : tag ? [tag, ...currentTopicTags] : currentTopicTags;
    try {
      setRecentTagPick({ paperId: paper.id, tagId, mode: exists ? "unselect" : "select" });
      const nextPaper = await api<Paper>(`/api/papers/${paper.id}/tags`, {
        method: "POST",
        body: JSON.stringify({ tag_ids: next.map((item) => item.id) })
      });
      applyPaper(nextPaper);
      await load();
    } catch (error) {
      showToast(error instanceof Error ? error.message : "更新标签失败", "error");
    }
  }

  async function handleSuggestion(suggestion: TagSuggestion, action: "approve" | "reject") {
    try {
      const next = await api<Paper | { status: string }>(`/api/suggestions/${suggestion.id}/${action}`, { method: "POST" });
      if ("id" in next) applyPaper(next);
      await load();
    } catch (error) {
      showToast(error instanceof Error ? error.message : "处理标签建议失败", "error");
    }
  }

  async function handlePartitionSuggestion(suggestion: PartitionSuggestion, action: "approve" | "reject") {
    try {
      const next = await api<Paper>(`/api/partition-suggestions/${suggestion.id}/${action}`, { method: "POST" });
      applyPaper(next);
    } catch (error) {
      showToast(error instanceof Error ? error.message : "处理分区建议失败", "error");
    }
  }

  async function saveConfig() {
    aiTestGeneration.current += 1;
    setAiTestState(null);
    try {
      const next = await api<ApiConfig>("/api/config", { method: "PUT", body: JSON.stringify(config) });
      setConfig(next);
      setSavedConfig(next);
      showToast("AI 配置已保存", "success");
      requestCloseSettings({ configDraft: next, savedConfig: next });
    } catch (error) {
      showToast(error instanceof Error ? error.message : "配置保存失败", "error");
    }
  }

  async function testConfigConnection() {
    const generation = ++aiTestGeneration.current;
    setAiTestState({ status: "running", message: "正在测试连接…" });
    try {
      const result = await api<{ status: string; message: string }>("/api/config/test", {
        method: "POST",
        body: JSON.stringify(config)
      });
      if (generation === aiTestGeneration.current) {
        setAiTestState({ status: "success", message: result.message });
      }
    } catch (error) {
      if (generation === aiTestGeneration.current) {
        setAiTestState({
          status: "error",
          message: error instanceof Error ? error.message : "AI 连接测试失败"
        });
      }
    }
  }

  function openSettings(tab: SettingsTab = "api") {
    setSettingsTab(tab);
    setShowConfig(true);
    if (tab === "partitions") {
      api<PartitionBatchStatus>("/api/partitions/lookup-all/status")
        .then(async (status) => {
          setPartitionBatchStatus(status);
          if (status.status !== "running") {
            await load().catch((error) => showToast(error.message, "error"));
          }
        })
        .catch(() => undefined);
    }
  }

  async function chooseTag(tagId: string) {
    const clearsTopicFilters = tagId === "" || tagId === "unclassified" || tagId === "failed";
    const nextTagIds = clearsTopicFilters ? [] : selectedTagIds;
    setActiveTag(tagId);
    if (clearsTopicFilters) setSelectedTagIds([]);
    setSelectedId(null);
    await load(tagId, sortOrder, nextTagIds);
  }

  async function toggleTagFilter(tagId: string) {
    const nextTagIds = selectedTagIds.includes(tagId)
      ? selectedTagIds.filter((item) => item !== tagId)
      : [...selectedTagIds, tagId];
    setSelectedTagIds(nextTagIds);
    setSelectedId(null);
    await load(activeTag, sortOrder, nextTagIds);
  }

  async function chooseSort(nextSort: SortMode) {
    setSortOrder(nextSort);
    setSelectedId(null);
    await load(activeTag, nextSort, selectedTagIds);
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">Local Paper Index</p>
          <h1>科研文献管理器</h1>
        </div>
        <div className="toolbar">
          <button className="ghost-button" onClick={() => openSettings("api")} title="设置">
            <Settings size={18} />
          </button>
          <button className="primary-button" onClick={selectFiles} disabled={busy}>
            {busy ? <Loader2 className="spin" size={18} /> : <FilePlus2 size={18} />}
            添加 PDF
          </button>
        </div>
      </header>

      {toast && (
        <div className={`message ${toast.type}`} onClick={() => setToast(null)}>
          {toast.text}
        </div>
      )}

      <main className="workspace">
        <aside className="sidebar">
          <button className={activeTag === "" ? "nav-item active" : "nav-item"} onClick={() => chooseTag("")}>
            全部文献 <span>{state.counts.all}</span>
          </button>
          <button
            className={activeTag === "unclassified" ? "nav-item active" : "nav-item"}
            onClick={() => chooseTag("unclassified")}
          >
            无主题标签 <span>{state.counts.unclassified}</span>
          </button>
          <button className={activeTag === "failed" ? "nav-item active" : "nav-item"} onClick={() => chooseTag("failed")}>
            提取异常 <span>{state.counts.failed}</span>
          </button>

          <div className="sidebar-title">主题标签</div>
          <div className="tag-search">
            <Search size={14} />
            <input
              value={topicTagQuery}
              onChange={(event) => setTopicTagQuery(event.target.value)}
              placeholder="检索标签或别名"
            />
          </div>
          <div className="tag-list topic-list">
            {visibleTopicTags.map((tag) => (
              <button
                key={tag.id}
                className={selectedTagIds.includes(tag.id) ? "tag-toggle filter-chip active" : "tag-toggle filter-chip"}
                onClick={() => toggleTagFilter(tag.id)}
                aria-pressed={selectedTagIds.includes(tag.id)}
                title={tag.description || tag.name}
              >
                {tag.name}
                <b>{tag.use_count}</b>
              </button>
            ))}
            {visibleTopicTags.length === 0 && <p className="muted mini">没有匹配的主题标签。</p>}
          </div>
          <div className="sidebar-title">年份标签</div>
          <div className="tag-list year-list">
            {yearTags.length === 0 ? (
              <p className="muted mini">导入或保存出版时间后自动生成。</p>
            ) : (
              yearTags.map((tag) => (
                <button
                  key={tag.id}
                  className={selectedTagIds.includes(tag.id) ? "tag-filter year active" : "tag-filter year"}
                  onClick={() => toggleTagFilter(tag.id)}
                  aria-pressed={selectedTagIds.includes(tag.id)}
                  title={tag.description || tag.name}
                >
                  <Tag size={14} />
                  <span>{tag.name}</span>
                  <b>{tag.use_count}</b>
                </button>
              ))
            )}
          </div>
        </aside>

        <section className="paper-pane">
          <div className="search-row">
            <Search size={18} />
            <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="按标题、译名、摘要、备注、作者、单位、DOI 或标签过滤" />
            <button
              className={showTitleTranslation ? "toggle-button active" : "toggle-button"}
              onClick={() => setShowTitleTranslation((value) => !value)}
              title="显示或隐藏列表中的标题译名"
            >
              译名
            </button>
            <select value={sortOrder} onChange={(event) => chooseSort(event.target.value as SortMode)} title="排序方式">
              <option value="imported_desc">导入时间</option>
              <option value="year_desc">年份新到旧</option>
              <option value="year_asc">年份旧到新</option>
            </select>
            <button className="ghost-button" onClick={() => load()} title="刷新">
              <RefreshCw size={17} />
            </button>
          </div>
          <div className="paper-result-count" role="status">
            {activeTag || selectedTagIds.length > 0 || query.trim() ? "筛选结果" : "全部文献"}：{filteredPapers.length} 篇
          </div>
          <div className="paper-list">
            {filteredPapers.length === 0 ? (
              <div className="empty-state">还没有匹配的文献。点击“添加 PDF”开始建立本地索引。</div>
            ) : (
              filteredPapers.map((paper) => (
                <article
                  key={paper.id}
                  className={selectedPaper?.id === paper.id ? "paper-row selected" : "paper-row"}
                  onClick={() => setSelectedId(paper.id)}
                  onDoubleClick={() => openPaper(paper)}
                  onContextMenu={(event) => openPaperContextMenu(event, paper)}
                >
                  <div className="paper-main">
                    <h2>{paper.title || paper.file_name}</h2>
                    {showTitleTranslation && paper.title_zh && <p className="title-zh">{paper.title_zh}</p>}
                    <p>{compact(paper.authors)} · {paper.publication_date || "出版时间未识别"}</p>
                    <div className="tag-chips">
                      {paper.tags.map((tag) => <span className={tag.kind} key={tag.id}>{tag.name}</span>)}
                      {!paper.tags.some((tag) => tag.kind === "topic") && <span className="muted-chip">无主题标签</span>}
                    </div>
                  </div>
                  <div className="paper-status-stack">
                    <div className={`status-pill ${paper.tag_suggestions.length || paper.partition_suggestions.length ? "incomplete" : paper.status}`}>
                      {paper.tag_suggestions.length || paper.partition_suggestions.length ? "未完成" : statusText(paper.status)}
                    </div>
                    {paper.confirmed_partition_labels.length > 0 && (
                      <div className="partition-badges">
                        {paper.confirmed_partition_labels.map((label) => (
                          <span className={`partition-badge ${label.category}`} key={label.id} title={`${label.name}：${label.reason}`}>{label.name}</span>
                        ))}
                      </div>
                    )}
                  </div>
                </article>
              ))
            )}
          </div>
        </section>

        <DetailPane
          paper={selectedPaper}
          tags={topicTags}
          aiBusy={aiBusy}
          onChange={applyPaper}
          onTranslateTitle={translateTitle}
          onGenerateAbstract={generateAbstract}
          onLookupPartition={lookupPartition}
          onRelinkFile={relinkFile}
          onOpenFolder={openFolder}
          onSelectAttachments={selectAttachments}
          onOpenAttachment={openAttachment}
          onOpenAttachmentFolder={openAttachmentFolder}
          onRemoveAttachment={removeAttachment}
          onSetTag={setPaperTags}
          onSuggestion={handleSuggestion}
          onPartitionSuggestion={handlePartitionSuggestion}
          recentTagAction={recentTagPick && selectedPaper && recentTagPick.paperId === selectedPaper.id ? recentTagPick : null}
        />
        <DetailActionRail
          paper={selectedPaper}
          onSave={savePaper}
          onOpenDoi={openDoi}
          onOpenFolder={openFolder}
          onDelete={openRemovePaper}
          onReprocess={reprocess}
        />
      </main>

      {paperContextMenu && (
        <div
          className="paper-context-menu"
          role="menu"
          style={{ left: paperContextMenu.x, top: paperContextMenu.y }}
          onContextMenu={(event) => event.preventDefault()}
        >
          <button role="menuitem" onClick={() => openAddTagDialog(paperContextMenu.paper)}>
            <Tag size={15} />添加标签
          </button>
          <button role="menuitem" className="danger" onClick={() => openRemovePaper(paperContextMenu.paper)}>
            <Trash2 size={15} />移除文献
          </button>
        </div>
      )}

      {addTagDialog && (
        <div className="modal-backdrop" onMouseDown={(event) => {
          if (event.target === event.currentTarget && !tagSubmitting) setAddTagDialog(null);
        }}>
          <div className="modal paper-dialog" role="dialog" aria-modal="true" aria-labelledby="add-tag-title">
            <div className="modal-title">
              <h2 id="add-tag-title">添加标签</h2>
              <button className="ghost-button" onClick={() => setAddTagDialog(null)} disabled={tagSubmitting} aria-label="关闭">
                <X size={18} />
              </button>
            </div>
            <p className="paper-dialog-subject">{addTagDialog.paper.title || addTagDialog.paper.file_name}</p>
            <form onSubmit={(event) => { event.preventDefault(); submitQuickTag(); }}>
              <label>
                标签名
                <input
                  autoFocus
                  required
                  disabled={tagSubmitting}
                  value={addTagDialog.name}
                  onChange={(event) => setAddTagDialog({ ...addTagDialog, name: event.target.value })}
                  placeholder="输入主题标签名"
                />
              </label>
              <label>
                别名
                <input
                  disabled={tagSubmitting}
                  value={addTagDialog.aliases}
                  onChange={(event) => setAddTagDialog({ ...addTagDialog, aliases: event.target.value })}
                  placeholder="可选，默认同标签名"
                />
              </label>
              <div className="paper-dialog-actions">
                <button type="button" className="ghost-button" onClick={() => setAddTagDialog(null)} disabled={tagSubmitting}>取消</button>
                <button type="submit" className="primary-button" disabled={tagSubmitting || !addTagDialog.name.trim()}>添加</button>
              </div>
            </form>
          </div>
        </div>
      )}

      {removePaperDialog && (
        <div className="modal-backdrop" onMouseDown={(event) => {
          if (event.target === event.currentTarget && !paperRemoving) setRemovePaperDialog(null);
        }}>
          <div className="modal paper-dialog" role="dialog" aria-modal="true" aria-labelledby="remove-paper-title">
            <div className="modal-title">
              <h2 id="remove-paper-title">移除文献</h2>
              <button className="ghost-button" onClick={() => setRemovePaperDialog(null)} disabled={paperRemoving} aria-label="关闭">
                <X size={18} />
              </button>
            </div>
            <p className="paper-dialog-subject">{removePaperDialog.paper.title || removePaperDialog.paper.file_name}</p>
            <p className="paper-dialog-help">
              {removePaperDialog.paper.attachments?.length
                ? `关联 1 个主 PDF 和 ${removePaperDialog.paper.attachments.length} 个补充文件。`
                : "关联 1 个主 PDF。"}
              {!removePaperDialog.deleteLocalFiles && "本地文件将保留。"}
            </p>
            {removePaperError && <div className="warning" role="alert">{removePaperError}</div>}
            <div className="paper-dialog-actions">
              <button autoFocus className="ghost-button" onClick={() => setRemovePaperDialog(null)} disabled={paperRemoving}>取消</button>
              <button className="danger-button" onClick={removePaper} disabled={paperRemoving}>
                {removePaperDialog.deleteLocalFiles ? "移除并移入回收站" : "移除文献"}
              </button>
            </div>
            <label className="paper-delete-option">
              <input
                type="checkbox"
                checked={removePaperDialog.deleteLocalFiles}
                disabled={paperRemoving}
                onChange={(event) => {
                  setRemovePaperDialog({ ...removePaperDialog, deleteLocalFiles: event.target.checked });
                  setRemovePaperError(null);
                }}
              />
              同时将本地文件移入回收站（主 PDF 和补充文件）
            </label>
          </div>
        </div>
      )}

      {fileConflict && (
        <div className="modal-backdrop" onKeyDown={(event) => {
          if (event.key === "Enter") event.preventDefault();
        }}>
          <div className="modal conflict-modal" role="dialog" aria-modal="true" aria-labelledby="file-conflict-title">
            <div className="modal-title">
              <h2 id="file-conflict-title">文件名冲突</h2>
              <button className="ghost-button" onClick={() => setFileConflict(null)} aria-label="关闭">
                <X size={18} />
              </button>
            </div>
            <p className="conflict-help">
              目标文件名已经存在。自动编号会把当前文件改成不冲突的新名字；使用已有文件只会更新管理器记录，不会删除当前文件。
            </p>
            <div className="conflict-paths">
              <div>
                <b>当前文件</b>
                <p>{fileConflict.source_path}</p>
              </div>
              <div>
                <b>已存在同名文件</b>
                <p>{fileConflict.target_path}</p>
              </div>
            </div>
            {fileConflict.target_in_library && <div className="warning compact-warning">该文件已在文献管理器中存在，不能直接“使用已有文件”。</div>}
            <div className="conflict-actions">
              <button type="button" onClick={() => openConflictFolder(fileConflict)}>打开文件夹</button>
              <button type="button" onClick={() => resolveFileConflict(fileConflict, "auto_number")}>自动编号</button>
              <button type="button" onClick={() => resolveFileConflict(fileConflict, "use_existing")} disabled={fileConflict.target_in_library}>使用已有文件</button>
              <button type="button" onClick={() => resolveFileConflict(fileConflict, "cancel")}>取消</button>
            </div>
          </div>
        </div>
      )}

      {showConfig && (
        <div className="modal-backdrop">
          <div className={`modal settings-modal ${settingsTab === "tags" ? "settings-modal-tags" : ""}`}>
            <div className="modal-title">
              <h2>设置</h2>
              <button className="ghost-button" onClick={() => requestCloseSettings()} aria-label="关闭设置" title="关闭设置">
                <X size={18} />
              </button>
            </div>
            <div className="settings-body">
              <nav className="settings-nav">
                <button className={settingsTab === "api" ? "settings-tab active" : "settings-tab"} onClick={() => setSettingsTab("api")}>
                  AI 设置
                </button>
                <button className={settingsTab === "tags" ? "settings-tab active" : "settings-tab"} onClick={() => setSettingsTab("tags")}>
                  标签设置
                </button>
                <button className={settingsTab === "library" ? "settings-tab active" : "settings-tab"} onClick={() => setSettingsTab("library")}>
                  文件库
                </button>
                <button className={settingsTab === "pdf_viewer" ? "settings-tab active" : "settings-tab"} onClick={() => setSettingsTab("pdf_viewer")}>
                  PDF 阅读器
                </button>
                <button className={settingsTab === "partitions" ? "settings-tab active" : "settings-tab"} onClick={() => setSettingsTab("partitions")}>
                  分区补查
                </button>
              </nav>
              <section className="settings-panel">
                {settingsTab === "api" ? (
                  <>
                    <label>
                      Base URL
                      <input
                        value={config.base_url}
                        onChange={(event) => {
                          aiTestGeneration.current += 1;
                          setConfig({ ...config, base_url: event.target.value });
                          setAiTestState(null);
                        }}
                        placeholder="https://api.openai.com/v1"
                      />
                      <span className="help-text settings-field-help">填写兼容 Chat Completions 的 API 根地址，程序会请求其 /chat/completions 接口。</span>
                    </label>
                    <label>
                      API Key
                      <input
                        value={config.api_key}
                        onChange={(event) => {
                          aiTestGeneration.current += 1;
                          setConfig({ ...config, api_key: event.target.value });
                          setAiTestState(null);
                        }}
                        type="password"
                      />
                    </label>
                    <label>
                      Model
                      <input
                        value={config.model}
                        onChange={(event) => {
                          aiTestGeneration.current += 1;
                          setConfig({ ...config, model: event.target.value });
                          setAiTestState(null);
                        }}
                        placeholder="gpt-4.1-mini"
                      />
                      <span className="help-text settings-field-help">使用服务商提供的模型标识。</span>
                    </label>
                    <div className="settings-api-actions">
                      <button className="primary-button" onClick={saveConfig}>
                        <Save size={18} />
                        保存配置
                      </button>
                      <button className="ghost-button" onClick={testConfigConnection} disabled={aiTestState?.status === "running"}>
                        {aiTestState?.status === "running" ? <Loader2 size={17} className="spin" /> : <RefreshCw size={17} />}
                        {aiTestState?.status === "running" ? "正在测试…" : "测试连接"}
                      </button>
                    </div>
                    {aiTestState && (
                      <p className={`settings-inline-status ${aiTestState.status}`} role="status">
                        {aiTestState.message}
                      </p>
                    )}
                    <p className="help-text settings-field-help">连接测试只在点击时发送一条最小请求，可能产生少量 API 用量；测试不会保存配置。</p>
                  </>
                ) : settingsTab === "tags" ? (
                  <>
                    <div className="new-tag settings-new-tag">
                      <input
                        value={newTagName}
                        onChange={(event) => setNewTagName(event.target.value)}
                        onKeyDown={(event) => {
                          if (event.key === "Enter") createTag();
                        }}
                        placeholder="标签名"
                      />
                      <input
                        value={newTagAliases}
                        onChange={(event) => setNewTagAliases(event.target.value)}
                        onKeyDown={(event) => {
                          if (event.key === "Enter") createTag();
                        }}
                        placeholder="别名，用分号或逗号分隔"
                      />
                      <button onClick={createTag}>添加</button>
                    </div>
                    <div className="settings-tag-workspace">
                      <div className="settings-tag-browser">
                        <div className="tag-search settings-tag-search">
                          <Search size={14} />
                          <input
                            value={settingsTagQuery}
                            onChange={(event) => setSettingsTagQuery(event.target.value)}
                            placeholder="检索标签、别名或说明"
                          />
                        </div>
                        <div className="settings-tag-list">
                          {visibleSettingsTags.map((tag) => (
                            <button key={tag.id} className={editingTag?.id === tag.id ? "settings-tag active" : "settings-tag"} onClick={() => openTagEditor(tag)}>
                              <span>
                                <strong>{tag.name}</strong>
                                {aliasSummary(tag) && <em>{aliasSummary(tag)}</em>}
                              </span>
                              <b>{tag.use_count}</b>
                            </button>
                          ))}
                          {visibleSettingsTags.length === 0 && <p className="muted mini">没有匹配的主题标签。</p>}
                        </div>
                      </div>
                      <div className="settings-tag-editor">
                        {editingTag ? (
                          <div className="tag-editor-panel">
                            <label>
                              标签名
                              <input value={tagDraft.name} onChange={(event) => setTagDraft({ ...tagDraft, name: event.target.value })} />
                            </label>
                            <label>
                              别名
                              <textarea value={tagDraft.aliases} onChange={(event) => setTagDraft({ ...tagDraft, aliases: event.target.value })} rows={2} placeholder="用分号或逗号分隔" />
                              <span className="help-text">别名用于同义词归并和 AI 匹配已有标签，不会作为单独标签显示。</span>
                            </label>
                            <label>
                              说明
                              <textarea value={tagDraft.description} onChange={(event) => setTagDraft({ ...tagDraft, description: event.target.value })} rows={3} />
                            </label>
                            <div className="modal-actions">
                              <button className="danger-button" onClick={() => removeTag(editingTag)}>
                                <Trash2 size={18} />
                                移除标签
                              </button>
                              <button className="primary-button" onClick={saveTagEdit}>
                                <Save size={18} />
                                保存标签
                              </button>
                            </div>
                          </div>
                        ) : (
                          <p className="muted">选择一个主题标签后可编辑标签名、别名和说明。年份标签由系统自动维护。</p>
                        )}
                      </div>
                    </div>
                  </>
                ) : settingsTab === "pdf_viewer" ? (
                  <PdfViewerSettings onNotice={showToast} />
                ) : settingsTab === "partitions" ? (
                  <div className="library-settings">
                    <h3>分区补查</h3>
                    <p className="muted">查询 2025 中科院分区和 2026 新锐分区。缺少期刊名时，会使用当前 AI 配置从 PDF 首页提取；结果保存到论文记录，候选项进入详情页待确认区。</p>
                    <fieldset className="partition-scope-options" disabled={partitionBatchStatus?.status === "running"}>
                      <legend>查询范围</legend>
                      <label>
                        <input type="radio" name="partition-batch-scope" checked={partitionBatchScope === "all"} onChange={() => setPartitionBatchScope("all")} />
                        <span><strong>全部文献</strong><small>重新查询库内每篇文献。</small></span>
                      </label>
                      <label>
                        <input type="radio" name="partition-batch-scope" checked={partitionBatchScope === "unchecked"} onChange={() => setPartitionBatchScope("unchecked")} />
                        <span><strong>仅查未查询或上次失败</strong><small>筛选分区检查时间为空的文献。</small></span>
                      </label>
                    </fieldset>
                    {partitionBatchStatus?.status === "running" ? (
                      <button className="danger-button wide partition-cancel-button" onClick={cancelPartitionLookup} disabled={partitionBatchStatus.cancel_requested}>
                        <X size={18} />
                        {partitionBatchStatus.cancel_requested ? "正在停止…" : "停止补查"}
                      </button>
                    ) : (
                      <button className="primary-button wide" onClick={lookupAllPartitions}>
                        <RefreshCw size={18} />
                        {partitionBatchScope === "unchecked" ? "补查未查询或失败文献" : "补查全部文献"}
                      </button>
                    )}
                    {partitionBatchStatus && partitionBatchStatus.status !== "idle" && (
                      <div className="partition-batch-status">
                        <p className="muted">
                          {partitionBatchStatus.status === "running"
                            ? partitionBatchStatus.cancel_requested ? "正在完成当前文献，随后停止。" : "正在补查："
                            : partitionBatchStatus.status === "cancelled" ? "补查已停止：" : "上次补查完成："}
                          {partitionBatchStatus.processed}/{partitionBatchStatus.total}，匹配 {partitionBatchStatus.matched}，未匹配 {partitionBatchStatus.unmatched}，失败 {partitionBatchStatus.failed}
                        </p>
                        {partitionBatchStatus.failed_items.length > 0 && (
                          <details className="partition-failures">
                            <summary>查看失败文献（{partitionBatchStatus.failed_items.length}）</summary>
                            <ul>
                              {partitionBatchStatus.failed_items.map((item, index) => (
                                <li key={`${item.title}-${index}`}>
                                  <strong>{item.title}</strong>
                                  <span>{item.message}</span>
                                </li>
                              ))}
                            </ul>
                          </details>
                        )}
                      </div>
                    )}
                  </div>
                ) : (
                  <div className="library-settings">
                    <label>
                      文件库位置
                      <input value={state.library.path} readOnly />
                    </label>
                    <p className="muted">
                      新导入、重链接和补充文件会集中存放在此目录；数据库只记录文件路径，不保存 PDF 内容。可以打开文件库查看文件。
                    </p>
                    <div className="library-actions">
                      <button className="ghost-button" onClick={openLibraryFolder}>
                        <FolderOpen size={17} />
                        打开文件库
                      </button>
                    </div>
                  </div>
                )}
              </section>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <ErrorBoundary>
    <App />
  </ErrorBoundary>
);
