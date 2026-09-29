from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from backend.journal_partitions import _partition_label


PATH_MIGRATED_KEY = "file_paths_made_relative_v13"

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS papers (
  id TEXT PRIMARY KEY,
  file_path TEXT NOT NULL UNIQUE,
  file_name TEXT NOT NULL,
  file_size INTEGER NOT NULL,
  modified_at TEXT NOT NULL,
  imported_at TEXT NOT NULL,
  title TEXT,
  title_zh TEXT,
  abstract TEXT,
  notes TEXT,
  authors_json TEXT NOT NULL DEFAULT '[]',
  affiliations_json TEXT NOT NULL DEFAULT '[]',
  publication_date TEXT,
  doi_url TEXT,
  journal_name TEXT,
  cas_partition_2025 TEXT,
  cas_top_2025 INTEGER,
  newcomer_partitions_2026_json TEXT,
  partition_checked_at TEXT,
  status TEXT NOT NULL,
  error TEXT,
  metadata_confidence REAL,
  tags_confidence REAL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tags (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  aliases_json TEXT NOT NULL DEFAULT '[]',
  description TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT 'topic',
  use_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_tags (
  paper_id TEXT NOT NULL,
  tag_id TEXT NOT NULL,
  PRIMARY KEY (paper_id, tag_id),
  FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE,
  FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS tag_suggestions (
  id TEXT PRIMARY KEY,
  paper_id TEXT NOT NULL,
  name TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL,
  FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS paper_partition_suggestions (
  id TEXT PRIMARY KEY,
  paper_id TEXT NOT NULL,
  candidate_key TEXT NOT NULL,
  category TEXT NOT NULL,
  name TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  sort_order INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (paper_id, candidate_key),
  FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS ai_runs (
  id TEXT PRIMARY KEY,
  paper_id TEXT NOT NULL,
  status TEXT NOT NULL,
  error TEXT,
  raw_response TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS paper_attachments (
  id TEXT PRIMARY KEY,
  paper_id TEXT NOT NULL,
  file_path TEXT NOT NULL,
  file_name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'supplementary',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (paper_id, file_path),
  FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS app_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def connect(data_dir: Path, db_path: Path) -> sqlite3.Connection:
    data_dir.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def loads_json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def migrate_schema(
    conn: sqlite3.Connection,
    *,
    sync_year_tag: Callable[[sqlite3.Connection, str, Any], None],
) -> None:
    """Apply additive schema migrations and one-time data migrations."""
    conn.execute("CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_attachments (
          id TEXT PRIMARY KEY,
          paper_id TEXT NOT NULL,
          file_path TEXT NOT NULL,
          file_name TEXT NOT NULL,
          kind TEXT NOT NULL DEFAULT 'supplementary',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE (paper_id, file_path),
          FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE
        )
        """
    )
    paper_columns = table_columns(conn, "papers")
    if "title_zh" not in paper_columns:
        conn.execute("ALTER TABLE papers ADD COLUMN title_zh TEXT")
    if "abstract" not in paper_columns:
        conn.execute("ALTER TABLE papers ADD COLUMN abstract TEXT")
    if "notes" not in paper_columns:
        conn.execute("ALTER TABLE papers ADD COLUMN notes TEXT")
    for column, declaration in (
        ("journal_name", "TEXT"),
        ("cas_partition_2025", "TEXT"),
        ("cas_top_2025", "INTEGER"),
        ("newcomer_partitions_2026_json", "TEXT"),
        ("partition_checked_at", "TEXT"),
    ):
        if column not in paper_columns:
            conn.execute(f"ALTER TABLE papers ADD COLUMN {column} {declaration}")

    tag_columns = table_columns(conn, "tags")
    if "kind" not in tag_columns:
        conn.execute("ALTER TABLE tags ADD COLUMN kind TEXT NOT NULL DEFAULT 'topic'")
    conn.execute("UPDATE tags SET kind = 'topic' WHERE kind IS NULL OR kind = ''")
    if not conn.execute("SELECT 1 FROM app_meta WHERE key = 'topic_descriptions_cleared_v12'").fetchone():
        conn.execute("UPDATE tags SET description = '' WHERE kind = 'topic'")
        conn.execute(
            "INSERT INTO app_meta (key, value) VALUES ('topic_descriptions_cleared_v12', ?)",
            (now_iso(),),
        )
    for row in conn.execute("SELECT id, publication_date FROM papers").fetchall():
        sync_year_tag(conn, str(row["id"]), row["publication_date"])


def row_to_tag(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "aliases": loads_json(row["aliases_json"], []),
        "description": row["description"],
        "kind": row["kind"],
        "use_count": row["use_count"],
        "created_at": row["created_at"],
    }


def row_to_attachment(
    row: sqlite3.Row,
    *,
    resolve_stored_path: Callable[[str | Path | None], Path | None],
) -> dict[str, Any]:
    path = resolve_stored_path(row["file_path"])
    return {
        "id": row["id"],
        "paper_id": row["paper_id"],
        "file_path": str(path) if path else row["file_path"],
        "file_name": row["file_name"],
        "kind": row["kind"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "exists": bool(path and path.exists()),
    }


def paper_payload(
    row: sqlite3.Row,
    tags: list[dict[str, Any]],
    suggestions: list[dict[str, Any]],
    partition_suggestions: list[dict[str, Any]],
    confirmed_partition_labels: list[dict[str, Any]],
    attachments: list[dict[str, Any]],
    *,
    resolve_stored_path: Callable[[str | Path | None], Path | None],
) -> dict[str, Any]:
    path = resolve_stored_path(row["file_path"])
    return {
        "id": row["id"],
        "file_path": str(path) if path else row["file_path"],
        "file_name": row["file_name"],
        "file_size": row["file_size"],
        "modified_at": row["modified_at"],
        "imported_at": row["imported_at"],
        "title": row["title"],
        "title_zh": row["title_zh"],
        "abstract": row["abstract"],
        "notes": row["notes"],
        "authors": loads_json(row["authors_json"], []),
        "affiliations": loads_json(row["affiliations_json"], []),
        "publication_date": row["publication_date"],
        "doi_url": row["doi_url"],
        "journal_name": row["journal_name"],
        "cas_partition_2025": row["cas_partition_2025"],
        "cas_top_2025": bool(row["cas_top_2025"]) if row["cas_top_2025"] is not None else None,
        "newcomer_partitions_2026": loads_json(row["newcomer_partitions_2026_json"], []),
        "partition_checked_at": row["partition_checked_at"],
        "status": row["status"],
        "error": row["error"],
        "metadata_confidence": row["metadata_confidence"],
        "tags_confidence": row["tags_confidence"],
        "updated_at": row["updated_at"],
        "tags": tags,
        "tag_suggestions": suggestions,
        "partition_suggestions": partition_suggestions,
        "confirmed_partition_labels": expand_confirmed_partition_labels(confirmed_partition_labels),
        "attachments": attachments,
        "exists": bool(path and path.exists()),
    }


def expand_confirmed_partition_labels(labels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    for label in labels:
        if label["category"] != "newcomer_combo":
            expanded.append(label)
            continue
        try:
            key = json.loads(label["candidate_key"])
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(key, list) or len(key) != 3:
            continue
        subject, partition = str(key[1]), str(key[2])
        partition_name = f"新锐{_partition_label(partition)}"
        if subject:
            expanded.append({
                **label, "id": f"{label['id']}:subject", "category": "subject",
                "name": subject, "sort_order": 0,
                "reason": f"对应分区：{partition_name}；来自 2026 新锐期刊分区名单",
            })
        expanded.append({
            **label, "id": f"{label['id']}:partition", "category": "newcomer_partition",
            "name": partition_name, "sort_order": 3,
            "reason": f"对应学科：{subject}；来自 2026 新锐期刊分区名单" if subject
            else "来自 2026 新锐期刊分区名单",
        })
    distinct: dict[tuple[str, str], dict[str, Any]] = {}
    for label in sorted(expanded, key=lambda item: (item["sort_order"], item["name"])):
        key = (label["category"], label["name"])
        prior = distinct.get(key)
        if prior is None:
            distinct[key] = dict(label)
        elif label["reason"] and label["reason"] not in prior["reason"]:
            prior["reason"] += f"；{label['reason']}"
    return list(distinct.values())


def row_to_paper(
    row: sqlite3.Row,
    conn: sqlite3.Connection,
    *,
    resolve_stored_path: Callable[[str | Path | None], Path | None],
) -> dict[str, Any]:
    tags = [
        row_to_tag(tag_row)
        for tag_row in conn.execute(
            """
            SELECT t.* FROM tags t
            JOIN paper_tags pt ON pt.tag_id = t.id
            WHERE pt.paper_id = ?
            ORDER BY CASE t.kind WHEN 'year' THEN 0 ELSE 1 END, t.name DESC
            """,
            (row["id"],),
        ).fetchall()
    ]
    suggestions = [
        dict(suggestion)
        for suggestion in conn.execute(
            """
            SELECT id, paper_id, name, reason, status, created_at
            FROM tag_suggestions
            WHERE paper_id = ? AND status = 'pending'
            ORDER BY created_at DESC
            """,
            (row["id"],),
        ).fetchall()
    ]
    partition_suggestions = [
        dict(suggestion)
        for suggestion in conn.execute(
            """SELECT id, paper_id, candidate_key, category, name, reason, sort_order, status, created_at
               FROM paper_partition_suggestions
               WHERE paper_id = ? AND status = 'pending'
               ORDER BY sort_order, name""",
            (row["id"],),
        ).fetchall()
    ]
    confirmed_partition_labels = [
        dict(label)
        for label in conn.execute(
            """SELECT id, paper_id, candidate_key, category, name, reason, sort_order, status, created_at
               FROM paper_partition_suggestions
               WHERE paper_id = ? AND status = 'approved'
               ORDER BY sort_order, name""",
            (row["id"],),
        ).fetchall()
    ]
    attachments = [
        row_to_attachment(attachment, resolve_stored_path=resolve_stored_path)
        for attachment in conn.execute(
            """
            SELECT * FROM paper_attachments
            WHERE paper_id = ?
            ORDER BY created_at DESC
            """,
            (row["id"],),
        ).fetchall()
    ]
    return paper_payload(
        row,
        tags,
        suggestions,
        partition_suggestions,
        confirmed_partition_labels,
        attachments,
        resolve_stored_path=resolve_stored_path,
    )


def rows_to_papers(
    rows: list[sqlite3.Row],
    conn: sqlite3.Connection,
    *,
    resolve_stored_path: Callable[[str | Path | None], Path | None],
) -> list[dict[str, Any]]:
    if not rows:
        return []
    paper_ids = [row["id"] for row in rows]
    placeholders = ",".join("?" for _ in paper_ids)
    tags_by_paper: dict[str, list[dict[str, Any]]] = {paper_id: [] for paper_id in paper_ids}
    suggestions_by_paper: dict[str, list[dict[str, Any]]] = {paper_id: [] for paper_id in paper_ids}
    partition_suggestions_by_paper: dict[str, list[dict[str, Any]]] = {paper_id: [] for paper_id in paper_ids}
    confirmed_partition_labels_by_paper: dict[str, list[dict[str, Any]]] = {paper_id: [] for paper_id in paper_ids}
    attachments_by_paper: dict[str, list[dict[str, Any]]] = {paper_id: [] for paper_id in paper_ids}

    for tag_row in conn.execute(
        f"""
        SELECT pt.paper_id AS paper_id, t.id, t.name, t.aliases_json, t.description,
               t.kind, t.use_count, t.created_at
        FROM tags t
        JOIN paper_tags pt ON pt.tag_id = t.id
        WHERE pt.paper_id IN ({placeholders})
        ORDER BY pt.paper_id, CASE t.kind WHEN 'year' THEN 0 ELSE 1 END, t.name DESC
        """,
        paper_ids,
    ).fetchall():
        tags_by_paper[str(tag_row["paper_id"])].append(row_to_tag(tag_row))

    for suggestion in conn.execute(
        f"""
        SELECT id, paper_id, name, reason, status, created_at
        FROM tag_suggestions
        WHERE status = 'pending' AND paper_id IN ({placeholders})
        ORDER BY paper_id, created_at DESC
        """,
        paper_ids,
    ).fetchall():
        suggestions_by_paper[str(suggestion["paper_id"])].append(dict(suggestion))

    for suggestion in conn.execute(
        f"""SELECT id, paper_id, candidate_key, category, name, reason, sort_order, status, created_at
            FROM paper_partition_suggestions
            WHERE status = 'pending' AND paper_id IN ({placeholders})
            ORDER BY paper_id, sort_order, name""",
        paper_ids,
    ).fetchall():
        partition_suggestions_by_paper[str(suggestion["paper_id"])].append(dict(suggestion))

    for label in conn.execute(
        f"""SELECT id, paper_id, candidate_key, category, name, reason, sort_order, status, created_at
            FROM paper_partition_suggestions
            WHERE status = 'approved' AND paper_id IN ({placeholders})
            ORDER BY paper_id, sort_order, name""",
        paper_ids,
    ).fetchall():
        confirmed_partition_labels_by_paper[str(label["paper_id"])].append(dict(label))

    for attachment in conn.execute(
        f"""
        SELECT * FROM paper_attachments
        WHERE paper_id IN ({placeholders})
        ORDER BY paper_id, created_at DESC
        """,
        paper_ids,
    ).fetchall():
        attachments_by_paper[str(attachment["paper_id"])].append(
            row_to_attachment(attachment, resolve_stored_path=resolve_stored_path)
        )

    return [
        paper_payload(
            row,
            tags_by_paper[row["id"]],
            suggestions_by_paper[row["id"]],
            partition_suggestions_by_paper[row["id"]],
            confirmed_partition_labels_by_paper[row["id"]],
            attachments_by_paper[row["id"]],
            resolve_stored_path=resolve_stored_path,
        )
        for row in rows
    ]


def connection_reads_db_path(
    conn: sqlite3.Connection,
    db_path: Path,
    same_path: Any,
) -> bool:
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
    except sqlite3.Error:
        return False
    target = str(db_path)
    for row in rows:
        file_name = row["file"] if isinstance(row, sqlite3.Row) else row[2]
        if not file_name:
            continue
        if same_path(Path(file_name), db_path) or str(file_name) == target:
            return True
    return False


def backup_database_before_path_migration(
    conn: sqlite3.Connection,
    db_path: Path,
    same_path: Any,
) -> Path | None:
    try:
        if not db_path.exists() or not connection_reads_db_path(conn, db_path, same_path):
            return None
        db_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = db_path.with_name(f"{db_path.name}.before-relative-paths-{stamp}.bak")
        conn.commit()
        shutil.copy2(db_path, backup)
        return backup
    except OSError:
        return None


def read_config(config_path: Path, mask_key: bool = False) -> dict[str, str]:
    if not config_path.exists():
        return {"base_url": "", "api_key": "", "model": ""}
    data = loads_json(config_path.read_text(encoding="utf-8"), {})
    api_key = str(data.get("api_key") or "")
    return {
        "base_url": str(data.get("base_url") or ""),
        "api_key": "********" if mask_key and api_key else api_key,
        "model": str(data.get("model") or ""),
    }


def write_config(config_path: Path, data_dir: Path, data: dict[str, Any]) -> None:
    data_dir.mkdir(exist_ok=True)
    config_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
