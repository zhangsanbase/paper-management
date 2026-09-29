"""Local PDF reader preference and opening policy.

This service is deliberately independent of AI settings and the paper database.
Only the API layer supplies the location of the local preference file.
"""

from __future__ import annotations

import json
import platform
import subprocess
import tempfile
from pathlib import Path
from typing import Literal, TypedDict

from backend import win_focus


ViewerMode = Literal["system", "custom"]


class ViewerState(TypedDict):
    mode: ViewerMode
    executable_path: str | None
    effective_mode: ViewerMode
    selected_available: bool
    supported: bool
    warning: str | None


def _read_preference(settings_path: Path) -> tuple[ViewerMode, str | None, str | None]:
    if not settings_path.exists():
        return "system", None, None
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "system", None, "PDF 阅读器设置无法读取，当前使用系统默认程序。"
    if not isinstance(data, dict) or data.get("mode") not in ("system", "custom"):
        return "system", None, "PDF 阅读器设置无效，当前使用系统默认程序。"
    executable_path = data.get("executable_path")
    if executable_path is not None and not isinstance(executable_path, str):
        return "system", None, "PDF 阅读器设置无效，当前使用系统默认程序。"
    return data["mode"], executable_path, None


def _valid_executable(path: str | None) -> bool:
    if not path:
        return False
    candidate = Path(path)
    return candidate.is_absolute() and candidate.suffix.casefold() == ".exe" and candidate.is_file()


def read_settings(settings_path: Path, system_name: str | None = None) -> ViewerState:
    mode, executable_path, warning = _read_preference(settings_path)
    supported = (system_name or platform.system()) == "Windows"
    selected_available = _valid_executable(executable_path)
    effective_mode: ViewerMode = "system"
    if mode == "custom":
        if not supported:
            warning = "当前系统暂不支持自选 PDF 阅读器，已使用系统默认程序。"
        elif selected_available:
            effective_mode = "custom"
        else:
            warning = "自选 PDF 阅读器路径已失效，当前使用系统默认程序。请重新选择程序。"
    return {
        "mode": mode,
        "executable_path": executable_path,
        "effective_mode": effective_mode,
        "selected_available": selected_available,
        "supported": supported,
        "warning": warning,
    }


def save_settings(
    settings_path: Path,
    mode: ViewerMode,
    executable_path: str | None = None,
    system_name: str | None = None,
) -> ViewerState:
    if mode not in ("system", "custom"):
        raise ValueError("PDF 阅读器模式无效。")
    _, current_path, _ = _read_preference(settings_path)
    if mode == "custom":
        if (system_name or platform.system()) != "Windows":
            raise ValueError("当前系统暂不支持自选 PDF 阅读器。")
        selected_path = executable_path if executable_path is not None else current_path
        if not _valid_executable(selected_path):
            raise ValueError("请选择存在的 Windows 程序（.exe）。")
        saved_path = str(Path(selected_path).resolve())
    else:
        # Remember one selected reader so users can switch back without browsing.
        saved_path = current_path

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=settings_path.parent, prefix=".pdf_viewer-", suffix=".tmp", delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(
                json.dumps({"mode": mode, "executable_path": saved_path}, ensure_ascii=False, indent=2) + "\n"
            )
        temporary_path.replace(settings_path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return read_settings(settings_path, system_name)


def open_pdf(path: Path, settings_path: Path, system_name: str | None = None) -> str:
    """Open one PDF, returning a user-facing result message."""
    state = read_settings(settings_path, system_name)
    current_system = system_name or platform.system()
    if current_system == "Windows":
        if state["effective_mode"] == "custom":
            executable = Path(state["executable_path"] or "")
            try:
                win_focus.open_document(path, reader_executable=executable)
            except FileNotFoundError:
                # The program may have disappeared between validation and launch.
                win_focus.open_document(path)
                return "自选 PDF 阅读器已失效，已使用系统默认程序打开 PDF。"
        else:
            win_focus.open_document(path)
            if state["mode"] == "custom":
                return "自选 PDF 阅读器路径已失效，已使用系统默认程序打开 PDF。"
        return "已打开 PDF，正在把阅读器窗口置前。"
    if current_system == "Darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])
    return "已打开 PDF。"
