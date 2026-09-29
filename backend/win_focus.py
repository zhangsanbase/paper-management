"""Windows 窗口置前辅助模块。

后端是由启动脚本拉起的后台进程。Windows 有"前台锁"：只有当前前台进程、前台进程
启动的进程或刚收到输入的进程才能调用 SetForegroundWindow，否则调用被拒绝，只让
任务栏图标闪一下——表现就是"窗口打开了但没弹到最前面"。本机实测该锁的过期时间是
约 48 天（等于永不过期），所以必须用 AttachThreadInput 等手段绕过。

窗口识别优先按进程号，其次按 PDF 元数据标题 / 文件名，最后按实际选用的阅读器 exe。
SumatraPDF 的窗口标题显示的是 PDF 元数据里的 Title，跟文件名无关（本机实测），
所以标题只是兜底手段。

所有公开函数在非 Windows 平台上都是空操作。设置 PAPER_MANAGER_FOCUS_DEBUG=1 可输出诊断日志。
"""

from __future__ import annotations

import ctypes
import os
import platform
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import NamedTuple

IS_WINDOWS = platform.system() == "Windows"

POLL_INTERVAL = 0.15
PDF_WAIT_SECONDS = 10.0
EXPLORER_WAIT_SECONDS = 6.0
EXISTING_EXPLORER_FALLBACK_SECONDS = 1.0
ACTIVATE_ATTEMPTS = 3
ACTIVATE_INTERVAL = 0.6
MIN_TITLE_HINT_LENGTH = 4

SW_RESTORE = 9
SW_SHOWMAXIMIZED = 3

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_SHOWWINDOW = 0x0040
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2

VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002

SEE_MASK_NOCLOSEPROCESS = 0x00000040
SEE_MASK_NOASYNC = 0x00000100
SEE_MASK_FLAG_NO_UI = 0x00000400

EXPLORER_WINDOW_CLASS = "CabinetWClass"
GW_OWNER = 4
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ASSOCF_NOTRUNCATE = 0x00000040
ASSOCSTR_EXECUTABLE = 2

SCORE_LAUNCHED_PID = 50
SCORE_TITLE_HIT = 30
SCORE_NEW_WINDOW = 20
SCORE_READER_EXE = 15

# 达到这个分数才立刻采信；否则再等等，避免在阅读器新窗口还没建出来时
# 就抓走一个已经开着的旧窗口（本机实测踩过这个坑）。
STRONG_SCORE = SCORE_TITLE_HIT
WEAK_CANDIDATE_GRACE = 2.5


class WindowInfo(NamedTuple):
    hwnd: int
    pid: int
    title: str
    class_name: str


def _log(message: str) -> None:
    if os.environ.get("PAPER_MANAGER_FOCUS_DEBUG") != "1":
        return
    try:
        print(f"[win_focus] {message}", file=sys.stderr, flush=True)
    except (UnicodeEncodeError, OSError):
        print(f"[win_focus] {message.encode('ascii', 'backslashreplace').decode()}", flush=True)


def normalize_title(text: str) -> str:
    return " ".join(text.split()).casefold()


def title_hints(path: Path, pdf_title: str | None = None) -> list[str]:
    """窗口标题的候选值。元数据标题在前（SumatraPDF 用的就是它），文件名兜底。"""
    values = [pdf_title or "", path.name, path.stem]
    hints: list[str] = []
    for value in values:
        hint = normalize_title(value)
        if len(hint) >= MIN_TITLE_HINT_LENGTH and hint not in hints:
            hints.append(hint)
    return hints


def title_matches(hints: list[str], window_title: str) -> bool:
    normalized = normalize_title(window_title)
    return any(hint in normalized for hint in hints)


def score_window(
    window: WindowInfo,
    *,
    launched_pid: int | None,
    before: set[int],
    hints: list[str],
    reader_exe: str | None,
    pid_executables: dict[int, str | None],
) -> int:
    """给候选窗口打分。分数越高越可能是我们刚打开的那一篇。"""
    score = 0
    if launched_pid is not None and window.pid == launched_pid:
        score += SCORE_LAUNCHED_PID
    if window.hwnd not in before:
        score += SCORE_NEW_WINDOW
    if hints and title_matches(hints, window.title):
        score += SCORE_TITLE_HIT
    if reader_exe and (pid_executables.get(window.pid) or "").casefold() == reader_exe.casefold():
        score += SCORE_READER_EXE
    return score


def qualifies_as_target(
    window: WindowInfo,
    *,
    launched_pid: int | None,
    hints: list[str],
    reader_exe: str | None,
    pid_executables: dict[int, str | None],
) -> bool:
    """是否可能是我们要找的窗口。只有"刚出现"这一条不算数，
    否则点开 PDF 的瞬间任何新弹出的窗口（消息提示之类）都会被误选。"""
    if launched_pid is not None and window.pid == launched_pid:
        return True
    if hints and title_matches(hints, window.title):
        return True
    return bool(reader_exe and (pid_executables.get(window.pid) or "").casefold() == reader_exe.casefold())


def best_candidate(
    windows: list[WindowInfo],
    *,
    launched_pid: int | None,
    before: set[int],
    hints: list[str],
    reader_exe: str | None,
    pid_executables: dict[int, str | None],
) -> tuple[WindowInfo | None, int]:
    """EnumWindows 按 Z 序枚举，分数相同时靠前的（更靠上层）胜出。"""
    best: WindowInfo | None = None
    best_score = 0
    for window in windows:
        if not qualifies_as_target(
            window,
            launched_pid=launched_pid,
            hints=hints,
            reader_exe=reader_exe,
            pid_executables=pid_executables,
        ):
            continue
        score = score_window(
            window,
            launched_pid=launched_pid,
            before=before,
            hints=hints,
            reader_exe=reader_exe,
            pid_executables=pid_executables,
        )
        if score > best_score:
            best, best_score = window, score
    return best, best_score




def pick_explorer_window(windows: list[WindowInfo], folder: Path) -> WindowInfo | None:
    """找资源管理器中打开着该文件夹的窗口。先精确匹配标题，再退化为子串匹配。"""
    name = normalize_title(folder.name)
    for window in windows:
        if window.class_name == EXPLORER_WINDOW_CLASS and normalize_title(window.title) == name:
            return window
    for window in windows:
        if window.class_name == EXPLORER_WINDOW_CLASS and name in normalize_title(window.title):
            return window
    return None


def pick_new_explorer_window(windows: list[WindowInfo], folder: Path, before: set[int]) -> WindowInfo | None:
    name = normalize_title(folder.name)
    for window in windows:
        if window.hwnd in before or window.class_name != EXPLORER_WINDOW_CLASS:
            continue
        title = normalize_title(window.title)
        if title == name or name in title:
            return window
    return None


if IS_WINDOWS:
    from ctypes import wintypes

    class ShellExecuteInfoW(ctypes.Structure):
        # hIcon 与 hMonitor 是 union，两者都是指针宽度，合并为一个字段即可。
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", wintypes.DWORD),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIconOrMonitor", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    # 不要用 ctypes.windll，那样拿不到 GetLastError。
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    # AssocQueryStringW 只从 shlwapi 正确导出，shell32 里那个同名函数是按序号导出的。
    _shlwapi = ctypes.WinDLL("shlwapi", use_last_error=True)

    _WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    # 默认 restype 是 c_int，64 位下句柄会被截断，必须显式声明 argtypes/restype。
    _user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
    _user32.EnumWindows.restype = wintypes.BOOL
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.IsWindowVisible.restype = wintypes.BOOL
    _user32.IsIconic.argtypes = [wintypes.HWND]
    _user32.IsIconic.restype = wintypes.BOOL
    _user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    _user32.GetWindow.restype = wintypes.HWND
    _user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.GetWindowLongW.restype = wintypes.LONG
    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextLengthW.restype = ctypes.c_int
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowTextW.restype = ctypes.c_int
    _user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetClassNameW.restype = ctypes.c_int
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.GetForegroundWindow.argtypes = []
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.ShowWindow.restype = wintypes.BOOL
    _user32.BringWindowToTop.argtypes = [wintypes.HWND]
    _user32.BringWindowToTop.restype = wintypes.BOOL
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.SetForegroundWindow.restype = wintypes.BOOL
    _user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    _user32.AttachThreadInput.restype = wintypes.BOOL
    _user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    _user32.SetWindowPos.restype = wintypes.BOOL
    _user32.SwitchToThisWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
    _user32.SwitchToThisWindow.restype = None
    # dwExtraInfo 是 ULONG_PTR，wintypes 里没有对应类型。
    _user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
    _user32.keybd_event.restype = None

    _kernel32.GetCurrentThreadId.argtypes = []
    _kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    _kernel32.GetProcessId.argtypes = [wintypes.HANDLE]
    _kernel32.GetProcessId.restype = wintypes.DWORD
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    _shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(ShellExecuteInfoW)]
    _shell32.ShellExecuteExW.restype = wintypes.BOOL

    _shlwapi.AssocQueryStringW.argtypes = [
        wintypes.DWORD,
        ctypes.c_int,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _shlwapi.AssocQueryStringW.restype = ctypes.c_long

    _pid_executable_cache: dict[int, str | None] = {}
    _reader_exe_cache: list[str | None] = []
else:
    _user32 = None  # type: ignore[assignment]
    _kernel32 = None  # type: ignore[assignment]
    _shell32 = None  # type: ignore[assignment]
    _shlwapi = None  # type: ignore[assignment]
    _pid_executable_cache = {}
    _reader_exe_cache = []


def open_document(path: Path, reader_executable: Path | None = None) -> None:
    """打开 PDF，并在后台把所用阅读器的窗口提到最前。"""
    if not IS_WINDOWS:
        return
    _reset_caches()
    before = {window.hwnd for window in _enum_top_level_windows()}
    if reader_executable is None:
        pid = _shell_execute(path)
    else:
        # A list keeps paths with spaces intact and never invokes a command shell.
        pid = subprocess.Popen([str(reader_executable), str(path)]).pid
    focus_args = (path, pid, before) if reader_executable is None else (path, pid, before, str(reader_executable))
    threading.Thread(target=_focus_pdf_window, args=focus_args, daemon=True).start()


def reveal_in_explorer(path: Path) -> None:
    """在资源管理器中定位文件并尽量把对应窗口提到最前。"""
    if not IS_WINDOWS:
        return
    path = path.resolve()
    _reset_caches()
    windows = _enum_top_level_windows()
    existing = pick_explorer_window(windows, path.parent)
    before = {window.hwnd for window in windows}
    subprocess.Popen(f'explorer.exe /select,"{path}"')
    threading.Thread(
        target=_focus_revealed_explorer_window,
        args=(path.parent, before, existing),
        daemon=True,
    ).start()


def open_folder(path: Path) -> None:
    """打开文件夹并尽量把对应资源管理器窗口提到最前。"""
    if not IS_WINDOWS:
        return
    folder = path.resolve()
    _reset_caches()
    windows = _enum_top_level_windows()
    existing = pick_explorer_window(windows, folder)
    if existing is not None:
        _log(f"复用已打开的文件夹窗口 hwnd=0x{existing.hwnd:x} title={existing.title!r}")
        threading.Thread(
            target=_activate_until_foreground,
            args=(existing.hwnd, False),
            daemon=True,
        ).start()
        return
    before = {window.hwnd for window in windows}
    subprocess.Popen(f'explorer.exe "{folder}"')
    threading.Thread(
        target=_focus_new_explorer_window,
        args=(folder, before),
        daemon=True,
    ).start()


def _shell_execute(path: Path) -> int | None:
    info = ShellExecuteInfoW()
    info.cbSize = ctypes.sizeof(ShellExecuteInfoW)
    info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC | SEE_MASK_FLAG_NO_UI
    info.lpVerb = "open"
    info.lpFile = str(path)
    info.lpDirectory = str(path.parent)
    info.nShow = SW_SHOWMAXIMIZED
    if not _shell32.ShellExecuteExW(ctypes.byref(info)):
        error = ctypes.get_last_error()
        _log(f"ShellExecuteExW 失败（错误码 {error}），回退 os.startfile")
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except OSError as exc:
            raise RuntimeError(f"无法打开 PDF：{exc}") from exc
        return None
    if not info.hProcess:
        # 走 DDE / COM 转交给已有实例时不会有新进程。
        _log("ShellExecuteExW 未返回进程句柄（交给已有实例处理）")
        return None
    pid = int(_kernel32.GetProcessId(info.hProcess))
    _kernel32.CloseHandle(info.hProcess)
    return pid or None


def _enum_top_level_windows() -> list[WindowInfo]:
    """按 Z 序（最上层在前）枚举可见、有标题、非工具窗口的顶层窗口。"""
    windows: list[WindowInfo] = []

    def callback(hwnd: int, _lparam: int) -> bool:
        # 回调里绝不能抛异常：ctypes 会吞掉异常并中断枚举。
        try:
            if not _user32.IsWindowVisible(hwnd):
                return True
            if _user32.GetWindow(hwnd, GW_OWNER) or _user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
                return True
            length = _user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            title = ctypes.create_unicode_buffer(length + 1)
            _user32.GetWindowTextW(hwnd, title, length + 1)
            class_name = ctypes.create_unicode_buffer(256)
            _user32.GetClassNameW(hwnd, class_name, 256)
            windows.append(WindowInfo(int(hwnd), _window_pid(hwnd), title.value, class_name.value))
        except Exception:  # noqa: BLE001
            return True
        return True

    _user32.EnumWindows(_WNDENUMPROC(callback), 0)
    return windows


def _window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    return int(pid.value)


def _window_thread_id(hwnd: int) -> int:
    return int(_user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), None))


def _process_executable(pid: int) -> str | None:
    if pid in _pid_executable_cache:
        return _pid_executable_cache[pid]
    path: str | None = None
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if handle:
        try:
            size = wintypes.DWORD(1024)
            buffer = ctypes.create_unicode_buffer(size.value)
            if _kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                path = buffer.value
        finally:
            _kernel32.CloseHandle(handle)
    _pid_executable_cache[pid] = path
    return path


def _reader_executable() -> str | None:
    """问系统"打开 .pdf 用哪个 exe"。必须走 AssocQueryStringW，它认 UserChoice；
    直接读 HKCR\\.pdf 会拿到过时的默认值（本机是 Foxit，而不是实际在用的 SumatraPDF）。"""
    if _reader_exe_cache:
        return _reader_exe_cache[0]
    path: str | None = None
    size = wintypes.DWORD(0)
    # 第一次调用只问长度，缓冲区不够时返回 S_FALSE(1) 而不是 S_OK(0)。
    probe = _shlwapi.AssocQueryStringW(
        ASSOCF_NOTRUNCATE, ASSOCSTR_EXECUTABLE, ".pdf", "open", None, ctypes.byref(size)
    )
    if probe in (0, 1) and size.value:
        buffer = ctypes.create_unicode_buffer(size.value)
        if _shlwapi.AssocQueryStringW(
            ASSOCF_NOTRUNCATE, ASSOCSTR_EXECUTABLE, ".pdf", "open", buffer, ctypes.byref(size)
        ) == 0:
            path = buffer.value
    _log(f"系统关联的 PDF 程序：{path}")
    _reader_exe_cache.append(path)
    return path


def _pdf_metadata_title(path: Path) -> str | None:
    try:
        import fitz

        with fitz.open(path) as doc:
            title = (doc.metadata or {}).get("title") or ""
        return title.strip() or None
    except Exception:  # noqa: BLE001
        return None


def _pid_executables(windows: list[WindowInfo]) -> dict[int, str | None]:
    return {window.pid: _process_executable(window.pid) for window in windows}


def _reset_caches() -> None:
    # 进程号会被系统回收，跨调用缓存可能把新进程认成旧的阅读器进程。
    _pid_executable_cache.clear()
    _reader_exe_cache.clear()


def _wait_for_window(picker, timeout: float) -> WindowInfo | None:
    deadline = time.monotonic() + timeout
    while True:
        window = picker()
        if window is not None:
            return window
        if time.monotonic() >= deadline:
            return None
        time.sleep(POLL_INTERVAL)


def wait_for_best_candidate(
    picker,
    timeout: float,
    grace: float = WEAK_CANDIDATE_GRACE,
    strong_score: int = STRONG_SCORE,
) -> WindowInfo | None:
    """一直等到出现"强"候选（进程号或标题命中）为止；
    只出现弱候选（比如已经开着的、同属 SumatraPDF 的旧窗口）时，
    先忍一小段时间，免得把旧窗口当成刚打开的那一篇。"""
    deadline = time.monotonic() + timeout
    grace_deadline = time.monotonic() + grace
    best: WindowInfo | None = None
    best_score = 0
    while True:
        window, score = picker()
        if window is not None and score > best_score:
            best, best_score = window, score
        if best_score >= strong_score:
            return best
        now = time.monotonic()
        if best is not None and now >= grace_deadline:
            return best
        if now >= deadline:
            return best
        time.sleep(POLL_INTERVAL)


def _focus_pdf_window(path: Path, pid: int | None, before: set[int], reader_hint: str | None = None) -> None:
    hints = title_hints(path, _pdf_metadata_title(path))
    reader_exe = reader_hint or _reader_executable()
    _log(f"标题候选：{hints}")

    def picker() -> tuple[WindowInfo | None, int]:
        windows = _enum_top_level_windows()
        return best_candidate(
            windows,
            launched_pid=pid,
            before=before,
            hints=hints,
            reader_exe=reader_exe,
            pid_executables=_pid_executables(windows),
        )

    window = wait_for_best_candidate(picker, PDF_WAIT_SECONDS)
    if window is None:
        _log(f"未能定位 PDF 窗口：{path.name}（pid={pid}）")
        return
    _log(
        f"定位到窗口 hwnd=0x{window.hwnd:x} class={window.class_name} "
        f"pid={window.pid} title={window.title!r}"
    )
    if not _activate_until_foreground(window.hwnd, maximize=True):
        _log("置前未完全成功，窗口已在最上层，但可能没有键盘焦点")


def _focus_new_explorer_window(folder: Path, before: set[int]) -> None:
    window = _wait_for_window(
        lambda: pick_new_explorer_window(_enum_top_level_windows(), folder, before),
        EXPLORER_WAIT_SECONDS,
    )
    if window is None:
        _log(f"未能定位新开的资源管理器窗口：{folder}")
        return
    _log(f"定位到新开的资源管理器窗口 hwnd=0x{window.hwnd:x} title={window.title!r}")
    if not _activate_until_foreground(window.hwnd, maximize=False):
        _log("置前未完全成功，窗口已在最上层，但可能没有键盘焦点")


def _focus_revealed_explorer_window(folder: Path, before: set[int], existing: WindowInfo | None) -> None:
    timeout = EXISTING_EXPLORER_FALLBACK_SECONDS if existing is not None else EXPLORER_WAIT_SECONDS
    window = _wait_for_window(
        lambda: pick_new_explorer_window(_enum_top_level_windows(), folder, before),
        timeout,
    )
    if window is None:
        window = existing or pick_explorer_window(_enum_top_level_windows(), folder)
    if window is None:
        _log(f"未能定位资源管理器窗口：{folder}")
        return
    _log(f"定位到资源管理器窗口 hwnd=0x{window.hwnd:x} title={window.title!r}")
    if not _activate_until_foreground(window.hwnd, maximize=False):
        _log("置前未完全成功，窗口已在最上层，但可能没有键盘焦点")


def _activate_until_foreground(hwnd: int, maximize: bool = False) -> bool:
    """窗口刚创建时激活常失败，在预算内重试几次。"""
    for attempt in range(ACTIVATE_ATTEMPTS):
        if _force_foreground(hwnd, maximize=maximize and attempt == 0):
            return True
        time.sleep(ACTIVATE_INTERVAL)
    return False


def _is_foreground(hwnd: int) -> bool:
    return _user32.GetForegroundWindow() == hwnd


def _force_foreground(hwnd: int, maximize: bool = False) -> bool:
    """把窗口提到最前。返回是否真正拿到了键盘焦点。"""
    target = wintypes.HWND(hwnd)
    if _user32.IsIconic(target):
        _user32.ShowWindow(target, SW_RESTORE)
    if maximize:
        _user32.ShowWindow(target, SW_SHOWMAXIMIZED)
    if _is_foreground(hwnd):
        _log("窗口已经是前台窗口")
        return True

    # 返回值不代表真的拿到了前台，每一级之后都要用 GetForegroundWindow 复核。
    _attach_threads_and_activate(target)
    if _is_foreground(hwnd):
        _log("已通过 AttachThreadInput 置前（第 2 级）")
        return True

    _press_alt_key()
    _user32.SetForegroundWindow(target)
    if _is_foreground(hwnd):
        _log("已通过 ALT 键释放前台锁后置前（第 3 级）")
        return True

    _raise_to_top_then_activate(target)
    if _is_foreground(hwnd):
        _log("已通过 TOPMOST 三明治置前（第 4 级）")
        return True

    _user32.SwitchToThisWindow(target, True)
    if _is_foreground(hwnd):
        _log("已通过 SwitchToThisWindow 置前（第 5 级）")
        return True
    return False


def _attach_threads_and_activate(target: wintypes.HWND) -> None:
    # 附加线程会让两个线程共享输入队列，对方卡死会连带卡住自己，
    # 所以附加期间不做任何阻塞调用，并且必须解除附加。
    current = int(_kernel32.GetCurrentThreadId())
    foreground = _user32.GetForegroundWindow()
    thread_ids = {_window_thread_id(foreground) if foreground else 0, _window_thread_id(target.value)}
    attached: list[int] = []
    try:
        for thread_id in thread_ids:
            if thread_id and thread_id != current and _user32.AttachThreadInput(current, thread_id, True):
                attached.append(thread_id)
        _user32.BringWindowToTop(target)
        _user32.SetForegroundWindow(target)
    finally:
        for thread_id in attached:
            _user32.AttachThreadInput(current, thread_id, False)


def _press_alt_key() -> None:
    # 模拟一次 ALT 按键，让系统认为本进程刚收到输入，从而释放前台锁。
    _user32.keybd_event(VK_MENU, 0, 0, 0)
    _user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)


def _raise_to_top_then_activate(target: wintypes.HWND) -> None:
    flags = SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW
    _user32.SetWindowPos(target, wintypes.HWND(HWND_TOPMOST), 0, 0, 0, 0, flags)
    _user32.SetForegroundWindow(target)
    _user32.SetWindowPos(target, wintypes.HWND(HWND_NOTOPMOST), 0, 0, 0, 0, flags)
