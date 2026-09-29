from __future__ import annotations

import json
import os
import platform
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from send2trash import send2trash

from backend import db as db_layer
from backend import file_library
from backend import journal_partitions
from backend import server_config
from backend import win_focus
from backend.services import papers as paper_services
from backend.runtime import ApplicationRuntime
from backend.ai import (
    AIResponseError,
    build_abstract_prompt,
    build_ai_prompt,
    build_journal_prompt,
    build_title_translation_prompt,
    call_ai,
    describe_ai_exception,
    parse_ai_json,
)
from backend.pdf_utils import extract_first_page_text
from backend.text_utils import (
    METHOD_TAG_BLACKLIST,
    coerce_confidence,
    coerce_string_list,
    extract_publication_year,
    is_method_tag,
    normalize_aliases,
    normalize_doi_url,
    normalize_label,
    nullable_str,
    safe_filename_stem,
)
from backend.models import (
    ApiConfig,
    AssignTagsRequest,
    FileConflictResolve,
    JournalLookupRequest,
    PartitionLookupBatchRequest,
    PaperUpdate,
    TagCreate,
    TagUpdate,
)


ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "library.sqlite3"
PARTITION_SOURCE_DIR = ROOT / "assets" / "partition_tables"
CONFIG_PATH = DATA_DIR / "config.json"
PDF_VIEWER_CONFIG_PATH = DATA_DIR / "pdf_viewer.json"
DIST_DIR = ROOT / "frontend" / "dist"
LIBRARY_DIR = ROOT / "library_files"
# Base directory for portable relative paths stored in the database. Changing the
# project location is exactly what PATH_BASE follows, so stored paths keep working.
PATH_BASE = ROOT
PATH_MIGRATED_KEY = db_layer.PATH_MIGRATED_KEY
SCHEMA = db_layer.SCHEMA
RUNTIME = ApplicationRuntime(sys.modules[__name__])


FILE_CONFLICTS: dict[str, dict[str, Any]] = {}
PARTITION_BATCH_LOCK = threading.Lock()
PARTITION_BATCH_STATUS: dict[str, Any] = {
    "status": "idle", "scope": "all", "total": 0, "processed": 0,
    "matched": 0, "unmatched": 0, "failed": 0, "cancel_requested": False,
    "failed_items": [],
}
FILE_CONFLICT_TTL_SECONDS = 1800
_DPI_AWARENESS_INITIALIZED = False
WINDOWS_DIALOG_CANCELLED = 0x800704C7
COINIT_APARTMENTTHREADED = 0x2
RPC_E_CHANGED_MODE = 0x80010106
CLSCTX_INPROC_SERVER = 0x1
FOS_FORCEFILESYSTEM = 0x40
FOS_ALLOWMULTISELECT = 0x200
SIGDN_FILESYSPATH = 0x80058000


def now_iso() -> str:
    return db_layer.now_iso()


def connect() -> sqlite3.Connection:
    return db_layer.connect(DATA_DIR, DB_PATH)


def connection_reads_db_path(conn: sqlite3.Connection) -> bool:
    """True only when this connection actually reads the real DB_PATH file.

    Guards against a connection opened on a different database (an in-memory or
    temp-path database, as tests use) copying the live database as a "backup".
    """
    return db_layer.connection_reads_db_path(conn, DB_PATH, _same_path)


def backup_database_before_path_migration(conn: sqlite3.Connection) -> Path | None:
    """Copy the database once before the absolute→relative path rewrite."""
    return db_layer.backup_database_before_path_migration(conn, DB_PATH, _same_path)


def normalize_stored_paths(conn: sqlite3.Connection) -> dict[str, int]:
    """Rewrite stored absolute paths inside PATH_BASE into portable relative paths.

    Idempotent: runs once, guarded by an app_meta marker. Only paths that can be
    expressed relative to PATH_BASE are touched; files outside the project tree,
    missing files and cross-drive paths keep their absolute form.
    """
    stats = {"papers": 0, "attachments": 0, "skipped": 0}
    if conn.execute("SELECT 1 FROM app_meta WHERE key = ?", (PATH_MIGRATED_KEY,)).fetchone():
        return stats
    pending: list[tuple[str, str, str]] = []
    for table in ("papers", "paper_attachments"):
        for row in conn.execute(f"SELECT id, file_path FROM {table}").fetchall():
            stored = str(row["file_path"])
            if not Path(stored).is_absolute():
                continue
            relative = to_stored_path(Path(stored))
            if relative == stored or Path(relative).is_absolute():
                stats["skipped"] += 1
                continue
            taken = conn.execute(
                f"SELECT 1 FROM {table} WHERE file_path = ? AND id <> ?",
                (relative, row["id"]),
            ).fetchone()
            if taken:
                stats["skipped"] += 1
                continue
            pending.append((table, str(row["id"]), relative))
    if pending:
        backup_database_before_path_migration(conn)
    for table, row_id, relative in pending:
        conn.execute(f"UPDATE {table} SET file_path = ? WHERE id = ?", (relative, row_id))
        stats["attachments" if table == "paper_attachments" else "papers"] += 1
    # OR IGNORE keeps concurrent first runs from failing on the primary key while
    # still marking the rewrite as done.
    conn.execute(
        "INSERT OR IGNORE INTO app_meta (key, value) VALUES (?, ?)",
        (PATH_MIGRATED_KEY, now_iso()),
    )
    return stats


def init_db() -> None:
    ensure_library_dir()
    with connect() as conn:
        conn.executescript(SCHEMA)
        migrate_schema(conn)
        normalize_stored_paths(conn)
        journal_partitions.import_partition_sources(conn, PARTITION_SOURCE_DIR)
        journal_partitions.migrate_stored_partition_suggestions(conn)
        journal_partitions.migrate_partition_suggestion_display(conn)
        journal_partitions.remove_legacy_partition_notes(conn)


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return db_layer.table_columns(conn, table)


def migrate_schema(conn: sqlite3.Connection) -> None:
    db_layer.migrate_schema(conn, sync_year_tag=sync_year_tag)


def loads_json(value: str | None, fallback: Any) -> Any:
    return db_layer.loads_json(value, fallback)


def row_to_tag(row: sqlite3.Row) -> dict[str, Any]:
    return db_layer.row_to_tag(row)


def row_to_attachment(row: sqlite3.Row) -> dict[str, Any]:
    return db_layer.row_to_attachment(row, resolve_stored_path=resolve_stored_path)


def paper_payload(
    row: sqlite3.Row,
    tags: list[dict[str, Any]],
    suggestions: list[dict[str, Any]],
    partition_suggestions: list[dict[str, Any]],
    confirmed_partition_labels: list[dict[str, Any]],
    attachments: list[dict[str, Any]],
) -> dict[str, Any]:
    return db_layer.paper_payload(
        row,
        tags,
        suggestions,
        partition_suggestions,
        confirmed_partition_labels,
        attachments,
        resolve_stored_path=resolve_stored_path,
    )


def row_to_paper(row: sqlite3.Row, conn: sqlite3.Connection) -> dict[str, Any]:
    return db_layer.row_to_paper(row, conn, resolve_stored_path=resolve_stored_path)


def rows_to_papers(rows: list[sqlite3.Row], conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return db_layer.rows_to_papers(rows, conn, resolve_stored_path=resolve_stored_path)


def read_config(mask_key: bool = False) -> dict[str, str]:
    return db_layer.read_config(CONFIG_PATH, mask_key)


def write_config(config: ApiConfig) -> None:
    db_layer.write_config(CONFIG_PATH, DATA_DIR, config.model_dump())


def enable_windows_dpi_awareness() -> None:
    global _DPI_AWARENESS_INITIALIZED
    if _DPI_AWARENESS_INITIALIZED:
        return
    _DPI_AWARENESS_INITIALIZED = True
    if platform.system() != "Windows":
        return
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
            return
        except Exception:  # noqa: BLE001
            pass
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        pass


def _signed_hresult(value: int) -> int:
    if value < 0:
        return value
    return value - 0x100000000 if value & 0x80000000 else value


def _hresult_failed(value: int) -> bool:
    return _signed_hresult(value) < 0


def _raise_for_hresult(value: int, message: str) -> None:
    if _hresult_failed(value):
        raise OSError(_signed_hresult(value), message)


def _choose_windows_files_native(title: str, filetypes: list[tuple[str, str]], multiple: bool = True) -> list[str]:
    import ctypes
    import uuid as uuidlib
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

        @classmethod
        def from_string(cls, value: str) -> "GUID":
            parsed = uuidlib.UUID(value)
            return cls(
                parsed.time_low,
                parsed.time_mid,
                parsed.time_hi_version,
                (ctypes.c_ubyte * 8).from_buffer_copy(parsed.bytes[8:]),
            )

    class COMDLG_FILTERSPEC(ctypes.Structure):
        _fields_ = [("pszName", wintypes.LPCWSTR), ("pszSpec", wintypes.LPCWSTR)]

    def method(interface: ctypes.c_void_p, index: int, restype: Any, *argtypes: Any) -> Any:
        prototype = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
        vtable = ctypes.cast(interface, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return prototype(vtable[index])

    def release(interface: ctypes.c_void_p | None) -> None:
        if interface:
            method(interface, 2, wintypes.ULONG) (interface)

    def get_shell_item_path(item: ctypes.c_void_p) -> str:
        raw_path = ctypes.c_wchar_p()
        try:
            get_display_name = method(item, 5, ctypes.c_long, wintypes.DWORD, ctypes.POINTER(ctypes.c_wchar_p))
            _raise_for_hresult(get_display_name(item, SIGDN_FILESYSPATH, ctypes.byref(raw_path)), "无法读取所选文件路径")
            return str(Path(raw_path.value).resolve())
        finally:
            if raw_path:
                ole32.CoTaskMemFree(raw_path)

    clsid_file_open_dialog = GUID.from_string("DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7")
    iid_file_open_dialog = GUID.from_string("D57C7288-D4AD-4768-BE02-9D969532D960")
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    ole32.CoInitializeEx.argtypes = [wintypes.LPVOID, wintypes.DWORD]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None
    ole32.CoCreateInstance.argtypes = [
        ctypes.POINTER(GUID),
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(GUID),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    ole32.CoCreateInstance.restype = ctypes.c_long
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole32.CoTaskMemFree.restype = None
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND

    initialized = False
    dialog = ctypes.c_void_p()
    shell_items = ctypes.c_void_p()
    filters = (COMDLG_FILTERSPEC * len(filetypes))(
        *[COMDLG_FILTERSPEC(name, pattern) for name, pattern in filetypes]
    )
    try:
        hr = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
        if hr == _signed_hresult(RPC_E_CHANGED_MODE):
            initialized = False
        else:
            _raise_for_hresult(hr, "无法初始化 Windows 文件选择器")
            initialized = True

        _raise_for_hresult(
            ole32.CoCreateInstance(
                ctypes.byref(clsid_file_open_dialog),
                None,
                CLSCTX_INPROC_SERVER,
                ctypes.byref(iid_file_open_dialog),
                ctypes.byref(dialog),
            ),
            "无法创建 Windows 文件选择器",
        )

        set_options = method(dialog, 9, ctypes.c_long, wintypes.DWORD)
        get_options = method(dialog, 10, ctypes.c_long, ctypes.POINTER(wintypes.DWORD))
        set_file_types = method(dialog, 4, ctypes.c_long, wintypes.UINT, ctypes.POINTER(COMDLG_FILTERSPEC))
        set_file_type_index = method(dialog, 5, ctypes.c_long, wintypes.UINT)
        set_title = method(dialog, 17, ctypes.c_long, wintypes.LPCWSTR)
        show = method(dialog, 3, ctypes.c_long, wintypes.HWND)
        get_results = method(dialog, 27, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))

        options = wintypes.DWORD()
        _raise_for_hresult(get_options(dialog, ctypes.byref(options)), "无法读取文件选择器选项")
        dialog_options = options.value | FOS_FORCEFILESYSTEM
        dialog_options = (dialog_options | FOS_ALLOWMULTISELECT) if multiple else (dialog_options & ~FOS_ALLOWMULTISELECT)
        _raise_for_hresult(
            set_options(dialog, dialog_options),
            "无法设置文件选择器选项",
        )
        if filetypes:
            _raise_for_hresult(
                set_file_types(dialog, len(filetypes), filters),
                "无法设置文件选择器类型过滤",
            )
            _raise_for_hresult(set_file_type_index(dialog, 1), "无法设置默认文件类型")
        _raise_for_hresult(set_title(dialog, title), "无法设置文件选择器标题")

        hr = show(dialog, user32.GetForegroundWindow())
        if hr == _signed_hresult(WINDOWS_DIALOG_CANCELLED):
            return []
        _raise_for_hresult(hr, "Windows 文件选择器打开失败")

        _raise_for_hresult(get_results(dialog, ctypes.byref(shell_items)), "无法读取所选文件列表")
        get_count = method(shell_items, 7, ctypes.c_long, ctypes.POINTER(wintypes.DWORD))
        get_item_at = method(shell_items, 8, ctypes.c_long, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))
        count = wintypes.DWORD()
        _raise_for_hresult(get_count(shell_items, ctypes.byref(count)), "无法读取所选文件数量")
        paths: list[str] = []
        for index in range(count.value):
            item = ctypes.c_void_p()
            try:
                _raise_for_hresult(get_item_at(shell_items, index, ctypes.byref(item)), "无法读取所选文件")
                paths.append(get_shell_item_path(item))
            finally:
                release(item)
        return paths
    finally:
        release(shell_items)
        release(dialog)
        if initialized:
            ole32.CoUninitialize()


def _choose_tk_files(title: str, filetypes: list[tuple[str, str]], multiple: bool = True) -> list[str]:
    result: list[str] = []
    dialog_error: list[BaseException] = []

    def run_dialog() -> None:
        enable_windows_dpi_awareness()
        import tkinter as tk
        from tkinter import filedialog

        root: tk.Tk | None = None
        try:
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            root.lift()
            root.update_idletasks()
            root.update()
            if multiple:
                files = filedialog.askopenfilenames(parent=root, title=title, filetypes=filetypes)
            else:
                selected = filedialog.askopenfilename(parent=root, title=title, filetypes=filetypes)
                files = (selected,) if selected else ()
            result.extend(str(Path(file).resolve()) for file in files)
        except BaseException as exc:  # noqa: BLE001
            dialog_error.append(exc)
        finally:
            if root is not None:
                root.destroy()

    thread = threading.Thread(target=run_dialog)
    thread.start()
    thread.join()
    if dialog_error:
        raise dialog_error[0]
    return result


def choose_local_files(title: str, filetypes: list[tuple[str, str]], multiple: bool = True) -> list[str]:
    if platform.system() == "Windows":
        enable_windows_dpi_awareness()
        try:
            if multiple:
                return _choose_windows_files_native(title, filetypes)
            return _choose_windows_files_native(title, filetypes, multiple=False)
        except Exception as exc:  # noqa: BLE001
            print(f"Windows 原生文件选择器不可用，回退到 Tk：{exc}", file=sys.stderr, flush=True)
    if multiple:
        return _choose_tk_files(title, filetypes)
    return _choose_tk_files(title, filetypes, multiple=False)


def choose_pdf_files() -> list[str]:
    return choose_local_files("选择 PDF 文献", [("PDF 文献", "*.pdf"), ("所有文件", "*.*")])


def choose_attachment_files() -> list[str]:
    return choose_local_files("选择补充文件", [("所有文件", "*.*")])


def get_all_tags(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM tags
        ORDER BY
          CASE kind WHEN 'year' THEN 0 ELSE 1 END,
          CASE kind WHEN 'year' THEN name END DESC,
          use_count DESC,
          name
        """
    ).fetchall()
    return [row_to_tag(row) for row in rows]


def get_topic_tags(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM tags WHERE kind = 'topic' ORDER BY use_count DESC, name").fetchall()
    return [row_to_tag(row) for row in rows]


def record_ai_failure(paper_id: str, status: str, error: str, raw_response: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO ai_runs (id, paper_id, status, error, raw_response, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (str(uuid.uuid4()), paper_id, status, error, raw_response, now_iso()),
        )


def normalize_tag_ids(values: list[str] | None) -> list[str]:
    result: list[str] = []
    for value in values or []:
        for part in value.split(","):
            tag_id = part.strip()
            if tag_id and tag_id not in result:
                result.append(tag_id)
    return result


def resolved_base() -> Path:
    """Directory that stored relative paths are resolved against."""
    return file_library.resolved_base(PATH_BASE)


def to_stored_path(path: Path) -> str:
    """Convert an absolute path into the portable value saved in the database.

    Paths inside PATH_BASE become relative (so the whole project directory can be
    moved or copied anywhere); anything else keeps its absolute form.
    """
    return file_library.to_stored_path(path, PATH_BASE)


def resolve_stored_path(value: str | Path | None) -> Path | None:
    """Turn a stored value back into an absolute path against the current PATH_BASE."""
    return file_library.resolve_stored_path(value, PATH_BASE)


def ensure_library_dir() -> Path:
    return file_library.ensure_library_dir(LIBRARY_DIR)


def is_in_library_dir(path: Path) -> bool:
    return file_library.is_in_library_dir(path, LIBRARY_DIR)


def _same_path(left: Path, right: Path) -> bool:
    return file_library.same_path(left, right)


def path_in_library(
    conn: sqlite3.Connection,
    path: Path,
    *,
    exclude_paper_id: str | None = None,
    exclude_attachment_id: str | None = None,
) -> dict[str, str] | None:
    return file_library.path_in_library(
        conn,
        path,
        path_base=PATH_BASE,
        exclude_paper_id=exclude_paper_id,
        exclude_attachment_id=exclude_attachment_id,
    )


def unique_named_path(
    folder: Path,
    stem: str,
    extension: str,
    current_path: Path,
    conn: sqlite3.Connection,
    *,
    exclude_paper_id: str | None = None,
    exclude_attachment_id: str | None = None,
) -> Path:
    return file_library.unique_named_path(
        folder,
        stem,
        extension,
        current_path,
        conn,
        path_base=PATH_BASE,
        exclude_paper_id=exclude_paper_id,
        exclude_attachment_id=exclude_attachment_id,
    )


def library_target_for_main(source: Path, title: str | None) -> Path:
    return file_library.library_target_for_main(
        source,
        title,
        library_dir=LIBRARY_DIR,
        nullable_str=nullable_str,
        safe_filename_stem=safe_filename_stem,
    )


def library_target_for_attachment(source: Path, title: str | None) -> Path:
    return file_library.library_target_for_attachment(
        source,
        title,
        library_dir=LIBRARY_DIR,
        nullable_str=nullable_str,
        safe_filename_stem=safe_filename_stem,
    )


def move_file_to_target(source: Path, target: Path) -> Path:
    return file_library.move_file_to_target(source, target)


def cleanup_file_conflicts() -> None:
    file_library.cleanup_file_conflicts(
        FILE_CONFLICTS,
        ttl_seconds=FILE_CONFLICT_TTL_SECONDS,
        now=time.time(),
    )


def create_file_conflict(
    conn: sqlite3.Connection,
    *,
    paper_id: str,
    kind: str,
    source_path: Path,
    target_path: Path,
    operation: str,
    attachment_id: str | None = None,
    migration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return file_library.create_file_conflict(
        conn,
        file_conflicts=FILE_CONFLICTS,
        ttl_seconds=FILE_CONFLICT_TTL_SECONDS,
        new_id=lambda: str(uuid.uuid4()),
        path_base=PATH_BASE,
        paper_id=paper_id,
        kind=kind,
        source_path=source_path,
        target_path=target_path,
        operation=operation,
        attachment_id=attachment_id,
        migration=migration,
    )


def update_paper_file_path(conn: sqlite3.Connection, paper_id: str, path: Path) -> None:
    file_library.update_paper_file_path(
        conn,
        paper_id,
        path,
        path_base=PATH_BASE,
        now_iso=now_iso,
    )


def insert_attachment(conn: sqlite3.Connection, paper_id: str, path: Path) -> None:
    file_library.insert_attachment(
        conn,
        paper_id,
        path,
        path_base=PATH_BASE,
        now_iso=now_iso,
        new_id=lambda: str(uuid.uuid4()),
    )


def update_attachment_file_path(conn: sqlite3.Connection, attachment_id: str, path: Path) -> None:
    file_library.update_attachment_file_path(
        conn,
        attachment_id,
        path,
        path_base=PATH_BASE,
        now_iso=now_iso,
    )


def rename_pdf_to_title(conn: sqlite3.Connection, paper_id: str, title: str | None) -> str | None:
    title = nullable_str(title)
    if not title:
        return None
    row = conn.execute("SELECT file_path FROM papers WHERE id = ?", (paper_id,)).fetchone()
    if not row:
        return None
    source = resolve_stored_path(row["file_path"])
    if not source or not source.exists():
        raise RuntimeError("源 PDF 文件不存在，无法按标题重命名")
    target = library_target_for_main(source, title)
    if not _same_path(source, target) and target.exists():
        raise RuntimeError(f"目标文件已存在：{target}")
    if path_in_library(conn, target, exclude_paper_id=paper_id):
        raise RuntimeError(f"目标文件已在文献管理器中存在：{target}")
    target = move_file_to_target(source, target)
    if _same_path(source, target):
        return None
    update_paper_file_path(conn, paper_id, target)
    return str(target)


def find_similar_tag(conn: sqlite3.Connection, name: str, kind: str | None = None) -> str | None:
    normalized = normalize_label(name)
    if kind:
        rows = conn.execute("SELECT id, name, aliases_json FROM tags WHERE kind = ?", (kind,)).fetchall()
    else:
        rows = conn.execute("SELECT id, name, aliases_json FROM tags").fetchall()
    for row in rows:
        values = [row["name"], *loads_json(row["aliases_json"], [])]
        if any(normalize_label(value) == normalized for value in values):
            return str(row["id"])
    return None


def assign_tags(conn: sqlite3.Connection, paper_id: str, tag_ids: list[str]) -> None:
    existing = {
        row["id"]
        for row in conn.execute(
            f"SELECT id FROM tags WHERE id IN ({','.join('?' for _ in tag_ids)})",
            tag_ids,
        ).fetchall()
    } if tag_ids else set()
    for tag_id in existing:
        conn.execute(
            "INSERT OR IGNORE INTO paper_tags (paper_id, tag_id) VALUES (?, ?)",
            (paper_id, tag_id),
        )
    conn.execute("UPDATE tags SET use_count = (SELECT COUNT(*) FROM paper_tags WHERE tag_id = tags.id)")


def sync_year_tag(conn: sqlite3.Connection, paper_id: str, publication_date: Any) -> None:
    conn.execute(
        """
        DELETE FROM paper_tags
        WHERE paper_id = ?
          AND tag_id IN (SELECT id FROM tags WHERE kind = 'year')
        """,
        (paper_id,),
    )
    year = extract_publication_year(publication_date)
    if not year:
        conn.execute("UPDATE tags SET use_count = (SELECT COUNT(*) FROM paper_tags WHERE tag_id = tags.id)")
        return
    row = conn.execute("SELECT id FROM tags WHERE name = ?", (year,)).fetchone()
    if row:
        tag_id = str(row["id"])
        conn.execute("UPDATE tags SET kind = 'year' WHERE id = ?", (tag_id,))
    else:
        tag_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO tags (id, name, aliases_json, description, kind, created_at)
            VALUES (?, ?, '[]', ?, 'year', ?)
            """,
            (tag_id, year, f"{year} 年出版", now_iso()),
        )
    assign_tags(conn, paper_id, [tag_id])


def remove_topic_tag(conn: sqlite3.Connection, tag_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM tags WHERE id = ?", (tag_id,)).fetchone()
    if not row:
        raise KeyError("标签不存在")
    if row["kind"] == "year":
        raise ValueError("年份标签由系统自动维护，不能移除")
    tag = row_to_tag(row)
    conn.execute("DELETE FROM paper_tags WHERE tag_id = ?", (tag_id,))
    conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
    conn.execute("UPDATE tags SET use_count = (SELECT COUNT(*) FROM paper_tags WHERE tag_id = tags.id)")
    return tag


def upsert_paper(path_text: str) -> tuple[str, bool]:
    return paper_services.upsert_paper(RUNTIME, path_text)


async def lookup_paper_partition(
    paper_id: str,
    journal_name_override: str | None = None,
) -> dict[str, Any]:
    return await paper_services.lookup_paper_partition(RUNTIME, paper_id, journal_name_override)


async def run_partition_lookup_batch(papers: list[dict[str, str]], scope: str = "all") -> None:
    await paper_services.run_partition_lookup_batch(RUNTIME, papers, scope)


async def process_paper(paper_id: str, rename_after_success: bool = False) -> None:
    await paper_services.process_paper(RUNTIME, paper_id, rename_after_success)


def open_local_file(path: Path) -> None:
    if platform.system() == "Windows":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif platform.system() == "Darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def open_local_folder_for_file(path: Path) -> None:
    if platform.system() == "Windows" and path.is_file():
        win_focus.reveal_in_explorer(path)
        return
    folder = path if path.is_dir() else path.parent
    open_local_directory(folder)


def open_local_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if platform.system() == "Windows":
        win_focus.open_folder(path)
    elif platform.system() == "Darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def library_status() -> dict[str, str]:
    return {"path": str(ensure_library_dir())}


def migrate_files_to_library(conn: sqlite3.Connection) -> dict[str, Any]:
    ensure_library_dir()
    stats: dict[str, Any] = {
        "status": "completed",
        "library_path": str(LIBRARY_DIR),
        "total": 0,
        "migrated": 0,
        "already_in_library": 0,
        "skipped_missing": 0,
        "failed": 0,
    }
    papers = conn.execute("SELECT * FROM papers ORDER BY imported_at ASC").fetchall()
    attachments = conn.execute(
        """
        SELECT a.*, p.title AS paper_title
        FROM paper_attachments a
        LEFT JOIN papers p ON p.id = a.paper_id
        ORDER BY a.created_at ASC
        """
    ).fetchall()
    stats["total"] = len(papers) + len(attachments)

    for paper in papers:
        source = resolve_stored_path(paper["file_path"])
        if not source or not source.exists():
            stats["skipped_missing"] += 1
            continue
        if is_in_library_dir(source):
            stats["already_in_library"] += 1
            continue
        target = library_target_for_main(source, paper["title"])
        if not _same_path(source, target) and (target.exists() or path_in_library(conn, target, exclude_paper_id=paper["id"])):
            stats["status"] = "conflict"
            conflict = create_file_conflict(
                conn,
                paper_id=paper["id"],
                kind="main",
                source_path=source,
                target_path=target,
                operation="migrate_main",
                migration=stats,
            )
            return {"status": "conflict", "conflict": conflict}
        try:
            target = move_file_to_target(source, target)
            update_paper_file_path(conn, paper["id"], target)
            stats["migrated"] += 1
        except OSError:
            stats["failed"] += 1

    for attachment in attachments:
        source = resolve_stored_path(attachment["file_path"])
        if not source or not source.exists():
            stats["skipped_missing"] += 1
            continue
        if is_in_library_dir(source):
            stats["already_in_library"] += 1
            continue
        target = library_target_for_attachment(source, attachment["paper_title"])
        if not _same_path(source, target) and (
            target.exists() or path_in_library(conn, target, exclude_attachment_id=attachment["id"])
        ):
            stats["status"] = "conflict"
            conflict = create_file_conflict(
                conn,
                paper_id=attachment["paper_id"],
                kind="attachment",
                source_path=source,
                target_path=target,
                operation="migrate_attachment",
                attachment_id=attachment["id"],
                migration=stats,
            )
            return {"status": "conflict", "conflict": conflict}
        try:
            target = move_file_to_target(source, target)
            update_attachment_file_path(conn, attachment["id"], target)
            stats["migrated"] += 1
        except OSError:
            stats["failed"] += 1
    stats["message"] = "文件库迁移完成。"
    return stats


def create_app() -> FastAPI:
    init_db()
    app = FastAPI(title="本地科研文献管理器 V1")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    from backend.api import files, frontend, library, papers, partitions, pdf_viewer, settings, system, tags

    for route_module in (system, library, papers, partitions, files, tags, settings, pdf_viewer, frontend):
        route_module.register(app, RUNTIME)

    return app


app = create_app()


def open_browser_later(url: str) -> None:
    if os.environ.get("PAPER_MANAGER_SKIP_BROWSER"):
        return

    def open_url() -> None:
        time.sleep(1.0)
        if platform.system() == "Windows":
            edge_candidates = [
                Path(os.environ.get("ProgramFiles", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
                Path(os.environ.get("ProgramFiles(x86)", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            ]
            for edge in edge_candidates:
                if edge.exists():
                    subprocess.Popen([str(edge), url])
                    return
        webbrowser.open(url)

    threading.Thread(target=open_url, daemon=True).start()


def main() -> None:
    port = server_config.resolve_port()
    url = server_config.server_url(port)
    print(f"本地科研文献管理器：{url}")
    open_browser_later(url)
    uvicorn.run("backend.app:app", host=server_config.HOST, port=port, reload=False)


if __name__ == "__main__":
    main()
