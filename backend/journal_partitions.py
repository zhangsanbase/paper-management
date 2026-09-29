from __future__ import annotations

import json
import sqlite3
import unicodedata
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS = {"m": MAIN_NS}
SOURCE_VERSION = "2026-09-v2"
SOURCE_META_KEY = "journal_partition_sources_version"
CAS_NOTE_PREFIX = "中科院分区（2025）："
NEWCOMER_NOTE_PREFIX = "新锐分区（2026）："
PARTITION_NOTE_CLEANUP_KEY = "partition_note_cleanup_v1"
PARTITION_SUGGESTIONS_MIGRATED_KEY = "partition_suggestions_migrated_v1"


def normalize_journal_name(value: str | None) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(char for char in normalized if unicodedata.category(char)[0] in {"L", "N"})


def _column_index(cell_ref: str) -> int:
    result = 0
    for char in cell_ref:
        if not char.isalpha():
            break
        result = result * 26 + ord(char.upper()) - ord("A") + 1
    return result - 1


def _read_first_sheet(path: Path) -> list[list[str]]:
    with zipfile.ZipFile(path) as workbook:
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in workbook.namelist():
            root = ElementTree.fromstring(workbook.read("xl/sharedStrings.xml"))
            shared_strings = [
                "".join(item.text or "" for item in string.findall(".//m:t", NS))
                for string in root.findall("m:si", NS)
            ]

        sheet = ElementTree.fromstring(workbook.read("xl/worksheets/sheet1.xml"))
        rows: list[list[str]] = []
        for row in sheet.findall(".//m:sheetData/m:row", NS):
            values: list[str] = []
            for cell in row.findall("m:c", NS):
                index = _column_index(cell.get("r", ""))
                while len(values) <= index:
                    values.append("")
                value_node = cell.find("m:v", NS)
                value = value_node.text if value_node is not None and value_node.text else ""
                if cell.get("t") == "s" and value:
                    value = shared_strings[int(value)]
                elif cell.get("t") == "inlineStr":
                    value = "".join(item.text or "" for item in cell.findall(".//m:t", NS))
                values[index] = value.strip()
            rows.append(values)
        return rows


def ensure_partition_tables(conn: sqlite3.Connection) -> None:
    cas_schema = """CREATE TABLE IF NOT EXISTS journal_partitions_2025 (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          normalized_name TEXT NOT NULL,
          journal_name TEXT NOT NULL,
          partition TEXT NOT NULL,
          is_top INTEGER NOT NULL DEFAULT 0,
          open_access TEXT NOT NULL DEFAULT '',
          UNIQUE(normalized_name, partition, is_top, open_access)
        )"""
    with conn:
        conn.execute(cas_schema)
        columns = conn.execute("PRAGMA table_info(journal_partitions_2025)").fetchall()
        if any(row["name"] == "normalized_name" and row["pk"] for row in columns):
            # The old primary key discarded different partitions for the same name.
            conn.execute(cas_schema.replace("journal_partitions_2025", "journal_partitions_2025_next"))
            conn.execute(
                """INSERT OR IGNORE INTO journal_partitions_2025_next
                   (normalized_name, journal_name, partition, is_top, open_access)
                   SELECT normalized_name, journal_name, partition, is_top, COALESCE(open_access, '')
                   FROM journal_partitions_2025"""
            )
            conn.execute("DROP TABLE journal_partitions_2025")
            conn.execute("ALTER TABLE journal_partitions_2025_next RENAME TO journal_partitions_2025")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_journal_partitions_2025_name "
            "ON journal_partitions_2025(normalized_name)"
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS journal_partitions_2026 (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 normalized_name TEXT NOT NULL,
                 journal_name TEXT NOT NULL,
                 subject TEXT NOT NULL DEFAULT '',
                 partition TEXT NOT NULL,
                 UNIQUE(normalized_name, subject, partition)
               )"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_journal_partitions_2026_name "
            "ON journal_partitions_2026(normalized_name)"
        )


def import_partition_sources(conn: sqlite3.Connection, source_dir: Path) -> dict[str, int] | None:
    ensure_partition_tables(conn)
    version = conn.execute("SELECT value FROM app_meta WHERE key = ?", (SOURCE_META_KEY,)).fetchone()
    if version and version["value"] == SOURCE_VERSION:
        return None

    cas_path = source_dir / "cas_partition_2025.xlsx"
    newcomer_path = source_dir / "newcomer_partition_2026.xlsx"
    if not cas_path.exists() or not newcomer_path.exists():
        return None

    cas_rows = _read_first_sheet(cas_path)
    newcomer_rows = _read_first_sheet(newcomer_path)
    cas_header = {value: index for index, value in enumerate(cas_rows[0])}
    newcomer_header_row = newcomer_rows[1] if len(newcomer_rows) > 1 else []
    newcomer_header = {value: index for index, value in enumerate(newcomer_header_row)}

    cas_items: list[tuple[str, str, str, int, str]] = []
    for row in cas_rows[1:]:
        journal = row[cas_header["期刊"]] if len(row) > cas_header["期刊"] else ""
        quartile = row[cas_header["分区"]] if len(row) > cas_header["分区"] else ""
        if not journal or not quartile:
            continue
        normalized = normalize_journal_name(journal)
        if not normalized:
            continue
        top = row[cas_header["Top"]].strip().casefold() == "是" if len(row) > cas_header["Top"] else False
        open_access = row[cas_header["Open Access"]] if len(row) > cas_header["Open Access"] else ""
        cas_items.append((normalized, journal, quartile, int(top), open_access))

    newcomer_items: list[tuple[str, str, str, str]] = []
    for row in newcomer_rows[2:]:
        journal_index = newcomer_header.get("期刊名称", -1)
        partition_index = newcomer_header.get("新锐分区", -1)
        subject_index = newcomer_header.get("学科", -1)
        journal = row[journal_index] if journal_index >= 0 and len(row) > journal_index else ""
        quartile = row[partition_index] if partition_index >= 0 and len(row) > partition_index else ""
        subject = row[subject_index] if subject_index >= 0 and len(row) > subject_index else ""
        if not journal or not quartile:
            continue
        normalized = normalize_journal_name(journal)
        if normalized:
            newcomer_items.append((normalized, journal, subject, quartile))

    refreshed_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    with conn:
        conn.execute("DELETE FROM journal_partitions_2025")
        conn.execute("DELETE FROM journal_partitions_2026")
        # A new source version invalidates every decision made against the old
        # tables. Keep the cached rows and paper state in the same transaction
        # so a failed import cannot leave old labels attached to new source data.
        conn.execute("DELETE FROM paper_partition_suggestions")
        conn.execute(
            """UPDATE papers
               SET cas_partition_2025 = NULL,
                   cas_top_2025 = NULL,
                   newcomer_partitions_2026_json = NULL,
                   partition_checked_at = NULL,
                   updated_at = ?
               WHERE partition_checked_at IS NOT NULL
                  OR cas_partition_2025 IS NOT NULL
                  OR cas_top_2025 IS NOT NULL
                  OR newcomer_partitions_2026_json IS NOT NULL""",
            (refreshed_at,),
        )
        conn.executemany(
            """INSERT OR IGNORE INTO journal_partitions_2025
               (normalized_name, journal_name, partition, is_top, open_access)
               VALUES (?, ?, ?, ?, ?)""",
            cas_items,
        )
        conn.executemany(
            """INSERT OR IGNORE INTO journal_partitions_2026
               (normalized_name, journal_name, subject, partition)
               VALUES (?, ?, ?, ?)""",
            newcomer_items,
        )
        conn.execute(
            """INSERT INTO app_meta(key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (SOURCE_META_KEY, SOURCE_VERSION),
        )
    return {"cas": len(cas_items), "newcomer": len(newcomer_items)}


def lookup_partition_records(conn: sqlite3.Connection, journal_name: str | None) -> dict[str, Any]:
    normalized = normalize_journal_name(journal_name)
    cas_rows = conn.execute(
        """SELECT partition, is_top FROM journal_partitions_2025
           WHERE normalized_name = ? ORDER BY partition, is_top""",
        (normalized,),
    ).fetchall() if normalized else []
    cas_matches = [
        {"partition": str(row["partition"]), "is_top": bool(row["is_top"])}
        for row in cas_rows
    ]
    cas_partitions = {item["partition"] for item in cas_matches}
    cas_partition = next(iter(cas_partitions)) if len(cas_partitions) == 1 else None
    top_values = {item["is_top"] for item in cas_matches}
    cas_top = next(iter(top_values)) if cas_partition and len(top_values) == 1 else None
    newcomer_rows = conn.execute(
        """SELECT subject, partition FROM journal_partitions_2026
           WHERE normalized_name = ? ORDER BY subject, partition""",
        (normalized,),
    ).fetchall() if normalized else []
    newcomer = [{"subject": row["subject"], "partition": row["partition"]} for row in newcomer_rows]
    return {
        "cas_partition_2025": cas_partition,
        "cas_top_2025": cas_top,
        "cas_matches": cas_matches,
        "newcomer_partitions_2026": newcomer,
        "matched": bool(cas_matches or newcomer),
    }


def _partition_label(value: str) -> str:
    compact = "".join(value.split())
    number_map = str.maketrans("0123456789", "零一二三四五六七八九")
    chinese_number = compact.translate(number_map)
    return chinese_number if chinese_number.endswith("区") else f"{chinese_number}区"


def partition_suggestion_candidates(result: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(
        category: str,
        values: list[str],
        name: str,
        reason: str,
        sort_order: int,
        status: str,
    ) -> None:
        key = json.dumps([category, *values], ensure_ascii=False, separators=(",", ":"))
        if key in seen:
            return
        seen.add(key)
        candidates.append({
            "candidate_key": key,
            "category": category,
            "name": name,
            "reason": reason,
            "sort_order": sort_order,
            "default_status": status,
        })

    newcomer = result.get("newcomer_partitions_2026") or []
    newcomer_pairs = sorted({
        (str(item.get("subject") or "").strip(), str(item.get("partition") or "").strip())
        for item in newcomer
        if str(item.get("partition") or "").strip()
    })
    if len(newcomer_pairs) == 1:
        subject, partition = newcomer_pairs[0]
        if subject:
            add("subject", [subject], subject, "来自 2026 新锐期刊分区名单", 0, "approved")
    cas_matches = result.get("cas_matches")
    cas_partitions = sorted({
        str(item.get("partition") or "").strip()
        for item in cas_matches
        if str(item.get("partition") or "").strip()
    }) if cas_matches is not None else ([str(result["cas_partition_2025"])] if result.get("cas_partition_2025") else [])
    for cas_partition in cas_partitions:
        add(
            "cas_partition",
            [cas_partition],
            f"中科{_partition_label(cas_partition)}",
            "同名期刊存在多个中科院分区，请选择一个" if len(cas_partitions) > 1
            else "来自 2025 中科院期刊分区名单",
            1,
            "pending" if len(cas_partitions) > 1 else "approved",
        )

    for subject, partition in newcomer_pairs:
        label = f"新锐{_partition_label(partition)}"
        if len(newcomer_pairs) > 1:
            add(
                "newcomer_combo", [subject, partition],
                f"{subject} · {label}" if subject else label,
                "同名期刊存在多个新锐学科与分区组合；来自 2026 新锐期刊分区名单",
                3, "pending",
            )
            continue
        reason = "来自 2026 新锐期刊分区名单"
        if subject:
            reason = f"对应学科：{subject}；{reason}"
        add("newcomer_partition", [subject, partition], label, reason, 3, "approved")
    return candidates


def sync_partition_suggestions(
    conn: sqlite3.Connection,
    paper_id: str,
    result: dict[str, Any],
    updated_at: str | None = None,
    *,
    auto_approve_unique: bool = True,
) -> None:
    now = updated_at or datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    candidates = partition_suggestion_candidates(result)
    newcomer_pairs = {
        (str(item.get("subject") or "").strip(), str(item.get("partition") or "").strip())
        for item in result.get("newcomer_partitions_2026") or []
        if str(item.get("partition") or "").strip()
    }
    existing = {
        row["candidate_key"]: row
        for row in conn.execute(
            """SELECT id, candidate_key, category, name, reason, sort_order, status
               FROM paper_partition_suggestions WHERE paper_id = ?""",
            (paper_id,),
        ).fetchall()
    }

    def old_status(category: str, *values: str) -> str | None:
        key = json.dumps([category, *values], ensure_ascii=False, separators=(",", ":"))
        row = existing.get(key)
        return str(row["status"]) if row else None

    def inherited_status(candidate: dict[str, Any]) -> str | None:
        key = json.loads(candidate["candidate_key"])
        if candidate["category"] == "newcomer_combo":
            subject, partition = key[1], key[2]
            subject_status = old_status("subject", subject) if subject else "approved"
            partition_status = old_status("newcomer_partition", subject, partition)
            if subject_status == partition_status == "approved":
                return "approved"
            if "rejected" in {subject_status, partition_status}:
                return "rejected"
        elif candidate["category"] in {"subject", "newcomer_partition"}:
            subject = key[1]
            partition = key[2] if candidate["category"] == "newcomer_partition" else (
                next(iter(newcomer_pairs))[1] if len(newcomer_pairs) == 1 else None
            )
            for old_key, row in existing.items():
                try:
                    old_parts = json.loads(old_key)
                except (TypeError, json.JSONDecodeError):
                    continue
                if (len(old_parts) == 3 and old_parts[0] == "newcomer_combo"
                    and old_parts[1] == subject and old_parts[2] == partition
                    and row["status"] in {"approved", "rejected"}):
                    return str(row["status"])
        return None

    desired_keys = {candidate["candidate_key"] for candidate in candidates}
    for key, row in existing.items():
        if key not in desired_keys:
            conn.execute("DELETE FROM paper_partition_suggestions WHERE id = ?", (row["id"],))
    for candidate in candidates:
        prior = existing.get(candidate["candidate_key"])
        default_status = candidate["default_status"] if auto_approve_unique else "pending"
        if prior:
            status = default_status if prior["status"] == "pending" and default_status == "approved" else prior["status"]
            if any((
                candidate["category"] != prior["category"],
                candidate["name"] != prior["name"],
                candidate["reason"] != prior["reason"],
                candidate["sort_order"] != prior["sort_order"],
                status != prior["status"],
            )):
                conn.execute(
                    """UPDATE paper_partition_suggestions
                       SET category = ?, name = ?, reason = ?, sort_order = ?, status = ?, updated_at = ?
                       WHERE id = ?""",
                    (
                        candidate["category"], candidate["name"], candidate["reason"],
                        candidate["sort_order"], status, now, prior["id"],
                    ),
                )
        else:
            status = inherited_status(candidate) or default_status
            conn.execute(
                """INSERT INTO paper_partition_suggestions
                   (id, paper_id, candidate_key, category, name, reason, sort_order, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(uuid.uuid4()), paper_id, candidate["candidate_key"], candidate["category"],
                    candidate["name"], candidate["reason"], candidate["sort_order"], status, now, now,
                ),
            )


def selected_cas_metadata(
    conn: sqlite3.Connection,
    paper_id: str,
    result: dict[str, Any],
) -> tuple[str | None, bool | None]:
    matches = result.get("cas_matches") or []
    partitions = {str(item["partition"]) for item in matches}
    if len(partitions) == 1:
        selected = next(iter(partitions))
    else:
        approved = {
            json.loads(row["candidate_key"])[1]
            for row in conn.execute(
                """SELECT candidate_key FROM paper_partition_suggestions
                   WHERE paper_id = ? AND category = 'cas_partition' AND status = 'approved'""",
                (paper_id,),
            ).fetchall()
        }
        selected = next(iter(approved)) if len(approved) == 1 and approved <= partitions else None
    if selected is None:
        return None, None
    top_values = {bool(item["is_top"]) for item in matches if item["partition"] == selected}
    return selected, next(iter(top_values)) if len(top_values) == 1 else None


def migrate_stored_partition_suggestions(conn: sqlite3.Connection) -> int:
    """Create review candidates from partition results already stored on papers.

    This is a local schema migration only: it never performs a journal lookup or
    calls an external service.
    """
    marker = conn.execute(
        "SELECT value FROM app_meta WHERE key = ?",
        (PARTITION_SUGGESTIONS_MIGRATED_KEY,),
    ).fetchone()
    if marker:
        return 0

    rows = conn.execute(
        """SELECT id, cas_partition_2025, cas_top_2025,
                  newcomer_partitions_2026_json, partition_checked_at
           FROM papers WHERE partition_checked_at IS NOT NULL"""
    ).fetchall()
    for row in rows:
        try:
            newcomer = json.loads(row["newcomer_partitions_2026_json"] or "[]")
        except json.JSONDecodeError:
            newcomer = []
        if not isinstance(newcomer, list):
            newcomer = []
        newcomer = [item for item in newcomer if isinstance(item, dict)]
        sync_partition_suggestions(
            conn,
            str(row["id"]),
            {
                "cas_partition_2025": row["cas_partition_2025"],
                "cas_top_2025": bool(row["cas_top_2025"]) if row["cas_top_2025"] is not None else None,
                "newcomer_partitions_2026": newcomer,
            },
            str(row["partition_checked_at"]),
            auto_approve_unique=False,
        )

    conn.execute(
        "INSERT INTO app_meta(key, value) VALUES (?, ?)",
        (PARTITION_SUGGESTIONS_MIGRATED_KEY, datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")),
    )
    return len(rows)


def remove_legacy_partition_notes(conn: sqlite3.Connection) -> int:
    marker = conn.execute(
        "SELECT value FROM app_meta WHERE key = ?",
        (PARTITION_NOTE_CLEANUP_KEY,),
    ).fetchone()
    if marker:
        return 0

    changed = 0
    for row in conn.execute("SELECT id, notes FROM papers WHERE notes IS NOT NULL").fetchall():
        lines = row["notes"].splitlines(keepends=True)
        kept = [
            line for line in lines
            if not line.rstrip("\r\n").startswith((CAS_NOTE_PREFIX, NEWCOMER_NOTE_PREFIX))
        ]
        cleaned = "".join(kept)
        if not cleaned.strip():
            cleaned = None
        if cleaned != row["notes"]:
            conn.execute("UPDATE papers SET notes = ? WHERE id = ?", (cleaned, row["id"]))
            changed += 1
    conn.execute(
        "INSERT INTO app_meta(key, value) VALUES (?, ?)",
        (PARTITION_NOTE_CLEANUP_KEY, datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")),
    )
    return changed


def migrate_partition_suggestion_display(conn: sqlite3.Connection) -> int:
    """Apply the current compact labels to existing suggestions and drop Top."""
    changed = conn.execute(
        "DELETE FROM paper_partition_suggestions WHERE category = 'cas_top'"
    ).rowcount
    rows = conn.execute(
        "SELECT id, candidate_key, category, name, reason FROM paper_partition_suggestions"
    ).fetchall()
    for row in rows:
        try:
            key = json.loads(row["candidate_key"])
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(key, list) or len(key) < 2:
            continue
        category = row["category"]
        reason = row["reason"]
        if category == "subject":
            name = str(key[1])
            reason = "来自 2026 新锐期刊分区名单"
        elif category == "cas_partition":
            name = f"中科{_partition_label(str(key[1]))}"
            if not reason.startswith("同名期刊存在多个中科院分区"):
                reason = "来自 2025 中科院期刊分区名单"
        elif category == "newcomer_partition" and len(key) >= 3:
            subject = str(key[1])
            name = f"新锐{_partition_label(str(key[2]))}"
            reason = "来自 2026 新锐期刊分区名单"
            if subject:
                reason = f"对应学科：{subject}；{reason}"
        else:
            continue
        if name != row["name"] or reason != row["reason"]:
            conn.execute(
                "UPDATE paper_partition_suggestions SET name = ?, reason = ? WHERE id = ?",
                (name, reason, row["id"]),
            )
            changed += 1
    return changed


def encode_newcomer_partitions(items: list[dict[str, Any]]) -> str | None:
    return json.dumps(items, ensure_ascii=False) if items else None
