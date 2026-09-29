from __future__ import annotations

import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


FileConflictStore = dict[str, dict[str, Any]]


def resolved_base(path_base: Path) -> Path:
    return path_base.resolve()


def to_stored_path(path: Path, path_base: Path) -> str:
    try:
        absolute = Path(path).resolve()
    except OSError:
        return str(path)
    try:
        relative = absolute.relative_to(resolved_base(path_base))
    except ValueError:
        return str(absolute)
    return str(relative) or str(absolute)


def resolve_stored_path(value: str | Path | None, path_base: Path) -> Path | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    candidate = Path(text)
    if candidate.is_absolute():
        return candidate
    return resolved_base(path_base) / candidate


def ensure_library_dir(library_dir: Path) -> Path:
    library_dir.mkdir(parents=True, exist_ok=True)
    return library_dir


def same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return str(left) == str(right)


def is_in_library_dir(path: Path, library_dir: Path) -> bool:
    try:
        path.resolve().relative_to(ensure_library_dir(library_dir).resolve())
        return True
    except ValueError:
        return False


def path_in_library(
    conn: sqlite3.Connection,
    path: Path,
    *,
    path_base: Path,
    exclude_paper_id: str | None = None,
    exclude_attachment_id: str | None = None,
) -> dict[str, str] | None:
    path_text = to_stored_path(path, path_base)
    row = conn.execute(
        "SELECT id FROM papers WHERE file_path = ? OR file_path = ?",
        (path_text, str(path)),
    ).fetchone()
    if row and row["id"] != exclude_paper_id:
        return {"kind": "main", "paper_id": row["id"]}
    row = conn.execute(
        "SELECT id, paper_id FROM paper_attachments WHERE file_path = ? OR file_path = ?",
        (path_text, str(path)),
    ).fetchone()
    if row and row["id"] != exclude_attachment_id:
        return {"kind": "attachment", "paper_id": row["paper_id"], "attachment_id": row["id"]}
    return None


def unique_named_path(
    folder: Path,
    stem: str,
    extension: str,
    current_path: Path,
    conn: sqlite3.Connection,
    *,
    path_base: Path,
    exclude_paper_id: str | None = None,
    exclude_attachment_id: str | None = None,
) -> Path:
    if extension and not extension.startswith("."):
        extension = f".{extension}"
    for index in range(1000):
        suffix = "" if index == 0 else f" ({index})"
        candidate = folder / f"{stem}{suffix}{extension}"
        if same_path(candidate, current_path):
            return candidate
        if candidate.exists():
            continue
        if not path_in_library(
            conn,
            candidate,
            path_base=path_base,
            exclude_paper_id=exclude_paper_id,
            exclude_attachment_id=exclude_attachment_id,
        ):
            return candidate
    raise RuntimeError("无法生成不冲突的文件名")


def library_target_for_main(
    source: Path,
    title: str | None,
    *,
    library_dir: Path,
    nullable_str: Callable[[Any], str | None],
    safe_filename_stem: Callable[[str], str],
) -> Path:
    title = nullable_str(title)
    stem = safe_filename_stem(title or source.stem)
    return ensure_library_dir(library_dir) / f"{stem}.pdf"


def library_target_for_attachment(
    source: Path,
    title: str | None,
    *,
    library_dir: Path,
    nullable_str: Callable[[Any], str | None],
    safe_filename_stem: Callable[[str], str],
) -> Path:
    title = nullable_str(title)
    stem = f"{safe_filename_stem(title)}_sp" if title else safe_filename_stem(source.stem)
    suffix = source.suffix or ""
    return ensure_library_dir(library_dir) / f"{stem}{suffix}"


def move_file_to_target(source: Path, target: Path) -> Path:
    if same_path(source, target):
        return source
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))
    return target


def cleanup_file_conflicts(
    file_conflicts: FileConflictStore,
    *,
    ttl_seconds: int,
    now: float,
) -> None:
    cutoff = now - ttl_seconds
    for conflict_id, conflict in list(file_conflicts.items()):
        if float(conflict.get("created_at", 0)) < cutoff:
            file_conflicts.pop(conflict_id, None)


def create_file_conflict(
    conn: sqlite3.Connection,
    *,
    file_conflicts: FileConflictStore,
    ttl_seconds: int,
    new_id: Callable[[], str],
    path_base: Path,
    paper_id: str,
    kind: str,
    source_path: Path,
    target_path: Path,
    operation: str,
    attachment_id: str | None = None,
    migration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cleanup_file_conflicts(file_conflicts, ttl_seconds=ttl_seconds, now=time.time())
    conflict_id = new_id()
    target_in_library = path_in_library(
        conn,
        target_path,
        path_base=path_base,
        exclude_paper_id=paper_id if kind == "main" else None,
        exclude_attachment_id=attachment_id,
    ) is not None
    conflict = {
        "conflict_id": conflict_id,
        "paper_id": paper_id,
        "kind": kind,
        "operation": operation,
        "source_path": str(source_path),
        "target_path": str(target_path),
        "target_in_library": target_in_library,
        "attachment_id": attachment_id,
        "migration": migration,
        "created_at": time.time(),
    }
    file_conflicts[conflict_id] = conflict
    return {key: value for key, value in conflict.items() if key != "created_at" and value is not None}


def update_paper_file_path(
    conn: sqlite3.Connection,
    paper_id: str,
    path: Path,
    *,
    path_base: Path,
    now_iso: Callable[[], str],
) -> None:
    stat = path.stat()
    conn.execute(
        """
        UPDATE papers
        SET file_path = ?, file_name = ?, file_size = ?, modified_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            to_stored_path(path, path_base),
            path.name,
            stat.st_size,
            datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
            now_iso(),
            paper_id,
        ),
    )


def insert_attachment(
    conn: sqlite3.Connection,
    paper_id: str,
    path: Path,
    *,
    path_base: Path,
    now_iso: Callable[[], str],
    new_id: Callable[[], str],
) -> None:
    now = now_iso()
    conn.execute(
        """
        INSERT OR IGNORE INTO paper_attachments (
          id, paper_id, file_path, file_name, kind, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'supplementary', ?, ?)
        """,
        (new_id(), paper_id, to_stored_path(path, path_base), path.name, now, now),
    )


def update_attachment_file_path(
    conn: sqlite3.Connection,
    attachment_id: str,
    path: Path,
    *,
    path_base: Path,
    now_iso: Callable[[], str],
) -> None:
    conn.execute(
        """
        UPDATE paper_attachments
        SET file_path = ?, file_name = ?, updated_at = ?
        WHERE id = ?
        """,
        (to_stored_path(path, path_base), path.name, now_iso(), attachment_id),
    )
