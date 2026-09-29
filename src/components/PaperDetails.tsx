import React, { useEffect, useRef, useState } from "react";
import {
  AlignLeft,
  Check,
  FilePlus2,
  FolderOpen,
  Languages,
  Link,
  Loader2,
  RefreshCw,
  Save,
  Search,
  Trash2,
  X
} from "lucide-react";
import type { Paper, PaperAttachment, PartitionSuggestion, TagItem, TagSuggestion } from "../types";
import { splitTextList, tagMatchesQuery } from "../utils";

function AutoTextarea({
  value,
  onChange,
  rows,
  placeholder
}: {
  value: string;
  onChange: (value: string) => void;
  rows: number;
  placeholder?: string;
}) {
  const ref = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    const node = ref.current;
    if (!node) return;
    node.style.height = "auto";
    node.style.height = `${node.scrollHeight}px`;
  }, [value]);

  return (
    <textarea
      ref={ref}
      value={value}
      onChange={(event) => onChange(event.target.value)}
      rows={rows}
      placeholder={placeholder}
    />
  );
}

type DetailProps = {
  paper: Paper | null;
  tags: TagItem[];
  aiBusy: string | null;
  onChange: (paper: Paper) => void;
  onTranslateTitle: (paper: Paper) => void;
  onGenerateAbstract: (paper: Paper) => void;
  onLookupPartition: (paper: Paper) => void;
  onRelinkFile: (paper: Paper) => void;
  onOpenFolder: (paper: Paper) => void;
  onSelectAttachments: (paper: Paper) => void;
  onOpenAttachment: (paper: Paper, attachment: PaperAttachment) => void;
  onOpenAttachmentFolder: (paper: Paper, attachment: PaperAttachment) => void;
  onRemoveAttachment: (paper: Paper, attachment: PaperAttachment) => void;
  onSetTag: (paper: Paper, tagId: string) => void;
  onSuggestion: (suggestion: TagSuggestion, action: "approve" | "reject") => void;
  onPartitionSuggestion: (suggestion: PartitionSuggestion, action: "approve" | "reject") => void;
  recentTagAction: { paperId: string; tagId: string; mode: "select" | "unselect" } | null;
};

export function DetailPane({
  paper,
  tags,
  aiBusy,
  onChange,
  onTranslateTitle,
  onGenerateAbstract,
  onLookupPartition,
  onRelinkFile,
  onOpenFolder,
  onSelectAttachments,
  onOpenAttachment,
  onOpenAttachmentFolder,
  onRemoveAttachment,
  onSetTag,
  onSuggestion,
  onPartitionSuggestion,
  recentTagAction
}: DetailProps) {
  const [detailTagQuery, setDetailTagQuery] = useState("");

  useEffect(() => {
    setDetailTagQuery("");
  }, [paper?.id]);

  if (!paper) {
    return <aside className="detail-pane empty-state">选择一篇文献查看详情。</aside>;
  }

  function patch(changes: Partial<Paper>) {
    onChange({ ...paper!, ...changes });
  }

  const translating = aiBusy === `translate:${paper.id}`;
  const summarizing = aiBusy === `abstract:${paper.id}`;
  const partitioning = aiBusy === `partition:${paper.id}`;
  const attachments = paper.attachments ?? [];
  const selectedTopicIds = new Set(paper.tags.filter((tag) => tag.kind === "topic").map((tag) => tag.id));
  const paperTopicOrder = paper.tags.filter((tag) => tag.kind === "topic").map((tag) => tag.id);
  const selectedTags = tags
    .filter((tag) => selectedTopicIds.has(tag.id))
    .sort((a, b) => {
      if (recentTagAction?.mode === "select" && a.id === recentTagAction.tagId) return -1;
      if (recentTagAction?.mode === "select" && b.id === recentTagAction.tagId) return 1;
      return paperTopicOrder.indexOf(a.id) - paperTopicOrder.indexOf(b.id);
    });
  const unselectedTags = tags
    .filter((tag) => !selectedTopicIds.has(tag.id))
    .filter((tag) => tagMatchesQuery(tag, detailTagQuery))
    .sort((a, b) => {
      if (recentTagAction?.mode === "unselect" && a.id === recentTagAction.tagId) return -1;
      if (recentTagAction?.mode === "unselect" && b.id === recentTagAction.tagId) return 1;
      return 0;
    });

  return (
    <aside className="detail-pane">
      <div className="detail-content">
        {!paper.exists && <div className="warning">原文件不存在或已移动。</div>}
        {paper.error && <div className="warning">{paper.error}</div>}

        <label>
          标题
          <AutoTextarea value={paper.title ?? ""} onChange={(value) => patch({ title: value })} rows={3} placeholder="论文原始标题。" />
        </label>
        <label>
          <span className="field-label-row">
            标题翻译
            <button className="inline-action" onClick={() => onTranslateTitle(paper)} disabled={translating}>
              {translating ? <Loader2 className="spin" size={15} /> : <Languages size={15} />}
              {paper.title_zh ? "重新翻译" : "AI 翻译标题"}
            </button>
          </span>
          <AutoTextarea value={paper.title_zh ?? ""} onChange={(value) => patch({ title_zh: value })} rows={2} placeholder="可以手动填写中文标题翻译。" />
        </label>
        <label>
          <span className="field-label-row">
            摘要
            <button className="inline-action" onClick={() => onGenerateAbstract(paper)} disabled={summarizing}>
              {summarizing ? <Loader2 className="spin" size={15} /> : <AlignLeft size={15} />}
              {paper.abstract ? "重新生成" : "AI 生成摘要"}
            </button>
          </span>
          <AutoTextarea value={paper.abstract ?? ""} onChange={(value) => patch({ abstract: value })} rows={9} placeholder="可以手动填写摘要，也可以用 AI 基于首页文字生成。" />
        </label>
        <label>
          <span className="field-label-row">
            期刊名
            <button className="inline-action" onClick={() => onLookupPartition(paper)} disabled={partitioning}>
              {partitioning ? <Loader2 className="spin" size={15} /> : <RefreshCw size={15} />}
              {partitioning ? "查询中" : "查询分区"}
            </button>
          </span>
          <input value={paper.journal_name ?? ""} onChange={(event) => patch({ journal_name: event.target.value })} placeholder="可填写或修正期刊名，再查询分区。" />
        </label>
        <label>
          备注
          <AutoTextarea value={paper.notes ?? ""} onChange={(value) => patch({ notes: value })} rows={3} placeholder="可以写阅读进度、重点结论、待办等。" />
        </label>
        <label>
          作者
          <textarea value={paper.authors.join("；")} onChange={(event) => patch({ authors: splitTextList(event.target.value) })} rows={2} />
        </label>
        <label>
          单位
          <textarea value={paper.affiliations.join("；")} onChange={(event) => patch({ affiliations: splitTextList(event.target.value) })} rows={3} />
        </label>
        <div className="two-col">
          <label>
            出版时间
            <input value={paper.publication_date ?? ""} onChange={(event) => patch({ publication_date: event.target.value })} />
          </label>
          <label>
            DOI 链接
            <input value={paper.doi_url ?? ""} onChange={(event) => patch({ doi_url: event.target.value })} />
          </label>
        </div>

        <section className="detail-section">
          <h3>主题标签</h3>
          <div className="tag-search detail-tag-search">
            <Search size={14} />
            <input
              value={detailTagQuery}
              onChange={(event) => setDetailTagQuery(event.target.value)}
              placeholder="检索可添加标签或别名"
            />
          </div>
          <div className="tag-checkboxes">
            {selectedTags.map((tag) => (
              <button
                key={tag.id}
                className="tag-toggle active"
                onClick={() => onSetTag(paper, tag.id)}
                title={tag.description || tag.name}
              >
                {tag.name}
              </button>
            ))}
            {unselectedTags.map((tag) => (
              <button
                key={tag.id}
                className="tag-toggle"
                onClick={() => onSetTag(paper, tag.id)}
                title={tag.description || tag.name}
              >
                {tag.name}
              </button>
            ))}
            {tags.length === 0 && <p className="muted">暂无正式标签，可以从设置里的标签设置新建，或确认 AI 的新标签建议。</p>}
            {tags.length > 0 && unselectedTags.length === 0 && detailTagQuery.trim() && (
              <p className="muted">没有匹配的可添加标签。</p>
            )}
          </div>
        </section>

        <section className="detail-section">
          <h3>待确认标签</h3>
          {paper.tag_suggestions.length === 0 && paper.partition_suggestions.length === 0 ? (
            <p className="muted">暂无待确认建议。</p>
          ) : (
            <>
              {paper.tag_suggestions.map((suggestion) => (
                <div className="suggestion" key={`tag:${suggestion.id}`}>
                  <div>
                    <b>{suggestion.name}</b>
                    <p>{suggestion.reason || "AI 未给出理由"}</p>
                  </div>
                  <div className="suggestion-actions">
                    <button onClick={() => onSuggestion(suggestion, "approve")} title="确认">
                      <Check size={16} />
                    </button>
                    <button onClick={() => onSuggestion(suggestion, "reject")} title="拒绝">
                      <X size={16} />
                    </button>
                  </div>
                </div>
              ))}
              {paper.partition_suggestions.map((suggestion) => (
                <div className="suggestion partition-suggestion" key={`partition:${suggestion.id}`}>
                  <div>
                    <b>{suggestion.name}</b>
                    <p>{suggestion.reason}</p>
                  </div>
                  <div className="suggestion-actions">
                    <button onClick={() => onPartitionSuggestion(suggestion, "approve")} title="确认并显示">
                      <Check size={16} />
                    </button>
                    <button onClick={() => onPartitionSuggestion(suggestion, "reject")} title="忽略">
                      <X size={16} />
                    </button>
                  </div>
                </div>
              ))}
            </>
          )}
        </section>

        <section className="detail-section file-meta">
          <div className="section-title-row">
            <h3>文件</h3>
            <button className="icon-mini-button" onClick={() => onSelectAttachments(paper)} title="添加补充文件" aria-label="添加补充文件">
              <FilePlus2 size={15} />
            </button>
          </div>
          <div className="file-list">
            <div className={paper.exists ? "file-row" : "file-row missing"}>
              <div className="file-type">主 PDF</div>
              <div className="file-main">
                <b>{paper.file_name}</b>
                {!paper.exists && <p className="muted">文件缺失</p>}
              </div>
              <div className="file-status">{paper.exists ? "可用" : "缺失"}</div>
              <div className="file-actions">
                <button onClick={() => onRelinkFile(paper)} title="重新链接本地 PDF" aria-label="重新链接本地 PDF">
                  <Link size={15} />
                </button>
                <button onClick={() => onOpenFolder(paper)} disabled={!paper.exists} title="打开所在文件夹" aria-label="打开所在文件夹">
                  <FolderOpen size={15} />
                </button>
              </div>
            </div>

            {attachments.map((attachment) => (
              <div className={attachment.exists ? "file-row" : "file-row missing"} key={attachment.id}>
                <div className="file-type">补充文件</div>
                <div className="file-main">
                    <b>{attachment.file_name}</b>
                    {!attachment.exists && <p className="muted">文件缺失</p>}
                </div>
                <div className="file-status">{attachment.exists ? "可用" : "缺失"}</div>
                <div className="file-actions">
                    <button onClick={() => onOpenAttachment(paper, attachment)} disabled={!attachment.exists} title="打开补充文件" aria-label="打开补充文件">
                      <Link size={15} />
                    </button>
                    <button onClick={() => onOpenAttachmentFolder(paper, attachment)} disabled={!attachment.exists} title="打开所在文件夹" aria-label="打开所在文件夹">
                      <FolderOpen size={15} />
                    </button>
                    <button onClick={() => onRemoveAttachment(paper, attachment)} title="移除关联" aria-label="移除关联">
                      <X size={15} />
                    </button>
                </div>
              </div>
            ))}
            {attachments.length === 0 && <p className="muted file-empty">暂无补充文件关联。</p>}
          </div>
        </section>
      </div>
    </aside>
  );
}

type DetailActionRailProps = {
  paper: Paper | null;
  onSave: (paper: Paper) => void;
  onOpenDoi: (paper: Paper) => void;
  onOpenFolder: (paper: Paper) => void;
  onDelete: (paper: Paper) => void;
  onReprocess: (paper: Paper) => void;
};

export function DetailActionRail({ paper, onSave, onOpenDoi, onOpenFolder, onDelete, onReprocess }: DetailActionRailProps) {
  const disabled = !paper;
  return (
    <aside className="detail-action-rail" aria-label="文献操作">
      <button className="rail-button primary" onClick={() => paper && onSave(paper)} disabled={disabled} title="保存当前文献信息" aria-label="保存当前文献信息" data-tooltip="保存">
        <Save size={17} />
      </button>
      <button
        className="rail-button"
        onClick={() => paper && onOpenDoi(paper)}
        disabled={!paper?.doi_url}
        title={paper?.doi_url ? "打开 DOI 链接" : "暂无 DOI"}
        aria-label={paper?.doi_url ? "打开 DOI 链接" : "暂无 DOI"}
        data-tooltip={paper?.doi_url ? "打开 DOI" : "暂无 DOI"}
      >
        <Link size={17} />
      </button>
      <button className="rail-button" onClick={() => paper && onOpenFolder(paper)} disabled={!paper?.exists} title="打开本地 PDF 所在文件夹" aria-label="打开本地 PDF 所在文件夹" data-tooltip="打开文件夹">
        <FolderOpen size={17} />
      </button>
      <button className="rail-button" onClick={() => paper && onReprocess(paper)} disabled={disabled} title="重新提取首页信息和主题标签" aria-label="重新提取首页信息和主题标签" data-tooltip="重新提取">
        <RefreshCw size={17} />
      </button>
      <button className="rail-button danger" onClick={() => paper && onDelete(paper)} disabled={disabled} title="移除文献" aria-label="移除文献" data-tooltip="移除文献">
        <Trash2 size={17} />
      </button>
    </aside>
  );
}
