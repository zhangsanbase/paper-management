export type TagKind = "topic" | "year";

export type TagItem = {
  id: string;
  name: string;
  aliases: string[];
  description: string;
  kind: TagKind;
  use_count: number;
  created_at: string;
};

export type TagSuggestion = {
  id: string;
  paper_id: string;
  name: string;
  reason: string;
  status: string;
  created_at: string;
};

export type PartitionSuggestion = {
  id: string;
  paper_id: string;
  candidate_key: string;
  category: string;
  name: string;
  reason: string;
  sort_order: number;
  status: string;
  created_at: string;
};

export type PaperAttachment = {
  id: string;
  paper_id: string;
  file_path: string;
  file_name: string;
  kind: "supplementary";
  created_at: string;
  updated_at: string;
  exists: boolean;
};

export type Paper = {
  id: string;
  file_path: string;
  file_name: string;
  file_size: number;
  modified_at: string;
  imported_at: string;
  title: string | null;
  title_zh: string | null;
  abstract: string | null;
  notes: string | null;
  authors: string[];
  affiliations: string[];
  publication_date: string | null;
  doi_url: string | null;
  journal_name: string | null;
  cas_partition_2025: string | null;
  cas_top_2025: boolean | null;
  newcomer_partitions_2026: { subject: string; partition: string }[];
  partition_checked_at: string | null;
  status: string;
  error: string | null;
  metadata_confidence: number | null;
  tags_confidence: number | null;
  updated_at: string;
  tags: TagItem[];
  tag_suggestions: TagSuggestion[];
  partition_suggestions: PartitionSuggestion[];
  confirmed_partition_labels: PartitionSuggestion[];
  attachments?: PaperAttachment[];
  exists: boolean;
};

export type ApiConfig = {
  base_url: string;
  api_key: string;
  model: string;
};

export type ModelProfile = ApiConfig & {
  id: string;
  name: string;
  kind: "api" | "subscription";
  provider: string;
  api_key_configured?: boolean;
};

export type ModelConfigState = {
  version: 2;
  active_profile_id: string | null;
  profiles: ModelProfile[];
};

export type PdfViewerState = {
  mode: "system" | "custom";
  executable_path: string | null;
  effective_mode: "system" | "custom";
  selected_available: boolean;
  supported: boolean;
  warning: string | null;
};

export type LibraryInfo = {
  path: string;
};

export type SortMode = "imported_desc" | "year_desc" | "year_asc";

export type Toast = {
  text: string;
  type: "info" | "success" | "error";
};

export type SettingsTab = "model_configs" | "tags" | "library" | "partitions" | "pdf_viewer";

export type PartitionBatchScope = "all" | "unchecked";

export type PartitionBatchStatus = {
  status: "idle" | "running" | "completed" | "cancelled";
  scope: PartitionBatchScope;
  total: number;
  processed: number;
  matched: number;
  unmatched: number;
  failed: number;
  cancel_requested: boolean;
  failed_items: { title: string; message: string }[];
};

export type PaperContextMenu = {
  paper: Paper;
  x: number;
  y: number;
};

export type AddTagDialog = {
  paper: Paper;
  name: string;
  aliases: string;
};

export type RemovePaperDialog = {
  paper: Paper;
  deleteLocalFiles: boolean;
};

export type FileConflict = {
  conflict_id: string;
  paper_id: string;
  kind: "main" | "attachment";
  operation?: string;
  source_path: string;
  target_path: string;
  target_in_library: boolean;
};

export type ConflictResolveResult = Paper | { status: string; message?: string };

export type AppState = {
  papers: Paper[];
  tags: TagItem[];
  config: ModelConfigState;
  library: LibraryInfo;
  counts: {
    all: number;
    unclassified: number;
    failed: number;
    pending_suggestions: number;
  };
};
