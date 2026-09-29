from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from prepare_environment import ensure_environment as prepare_environment


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
LOG_DIR = DATA_DIR / "logs"
LAUNCHER_LOG = LOG_DIR / "launcher.log"
SERVER_LOG = LOG_DIR / "server.log"
VENV_DIR = ROOT / ".venv"
VENV_PYTHON = VENV_DIR / "Scripts" / "python.exe"
VENV_PYTHONW = VENV_DIR / "Scripts" / "pythonw.exe"
URL = "http://127.0.0.1:8765"
HEALTH_URL = f"{URL}/api/health"
PORT = 8765
MUTEX_NAME = "Local\\PaperManagerTrayLauncher"


def hidden_subprocess_kwargs() -> dict[str, int]:
    if os.name != "nt":
        return {}
    return {"creationflags": subprocess.CREATE_NO_WINDOW}


class SingleInstanceGuard:
    def __init__(self) -> None:
        self.handle: int | None = None
        self.already_running = False
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        self._kernel32 = kernel32
        self.handle = int(kernel32.CreateMutexW(None, True, MUTEX_NAME))
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
        self.already_running = ctypes.get_last_error() == 183

    def close(self) -> None:
        if os.name == "nt" and self.handle:
            self._kernel32.CloseHandle(self.handle)
            self.handle = None


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=LAUNCHER_LOG,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        encoding="utf-8",
    )


def in_project_venv() -> bool:
    try:
        executable = Path(sys.executable).resolve()
        scripts_dir = VENV_DIR.resolve() / "Scripts"
        return executable.parent == scripts_dir
    except OSError:
        return False


def ensure_environment() -> None:
    logging.info("Checking Python dependencies and frontend build")
    output = StringIO()
    with redirect_stdout(output):
        prepare_environment(ROOT, quiet=True)
    if output.getvalue():
        logging.info(output.getvalue())


def relaunch_inside_venv_if_needed() -> None:
    if in_project_venv():
        return
    pythonw = VENV_PYTHONW if VENV_PYTHONW.exists() else VENV_PYTHON
    subprocess.Popen([str(pythonw), str(Path(__file__).resolve())], cwd=ROOT, **hidden_subprocess_kwargs())
    raise SystemExit(0)


def read_health(timeout: float = 0.8) -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        return data.get("status") == "ok"
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return False


def port_is_open() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", PORT)) == 0


def query_port_processes() -> list[dict[str, str]]:
    script = (
        "$ErrorActionPreference='SilentlyContinue'; "
        f"$items=Get-NetTCPConnection -LocalPort {PORT} -State Listen; "
        "$items | ForEach-Object { "
        "$p=Get-CimInstance Win32_Process -Filter \"ProcessId=$($_.OwningProcess)\"; "
        "[pscustomobject]@{ ProcessId=$_.OwningProcess; CommandLine=$p.CommandLine } "
        "} | ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", script],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            **hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    text = result.stdout.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logging.warning("无法解析端口进程信息：%s", text)
        return []
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def paper_manager_pids_on_port() -> list[int]:
    pids: list[int] = []
    for item in query_port_processes():
        command = str(item.get("CommandLine") or "")
        if "backend.app" not in command:
            continue
        try:
            pids.append(int(item.get("ProcessId", 0)))
        except (TypeError, ValueError):
            continue
    return pids


def open_app_url() -> None:
    edge_candidates = [
        Path(os.environ.get("ProgramFiles", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
    ]
    for edge in edge_candidates:
        if edge.exists():
            subprocess.Popen([str(edge), URL])
            return
    webbrowser.open(URL)


def notify(icon: object, title: str, message: str) -> None:
    try:
        icon.notify(message, title)  # type: ignore[attr-defined]
    except Exception:
        logging.info("%s: %s", title, message)


class PaperManagerTray:
    def __init__(self) -> None:
        from PIL import Image, ImageDraw
        import pystray

        self.pystray = pystray
        self.status = "启动中"
        self.error = ""
        self.server_process: subprocess.Popen[bytes] | None = None
        self.lock = threading.Lock()
        menu = pystray.Menu(
            pystray.MenuItem(lambda _: f"当前状态：{self.status}", None, enabled=False),
            pystray.MenuItem("打开界面", self.on_open),
            pystray.MenuItem("重启服务", self.on_restart),
            pystray.MenuItem("停止服务", self.on_stop),
            pystray.MenuItem("退出托盘", self.on_exit),
        )
        self.icon = pystray.Icon("paper-manager", self.make_icon(Image, ImageDraw), "科研文献管理器", menu)

    @staticmethod
    def make_icon(image_module: object, draw_module: object) -> object:
        image = image_module.new("RGBA", (64, 64), (255, 253, 248, 255))
        draw = draw_module.Draw(image)
        draw.rounded_rectangle((10, 8, 48, 56), radius=5, fill=(245, 239, 224, 255), outline=(36, 104, 93, 255), width=3)
        draw.rectangle((18, 18, 40, 23), fill=(36, 104, 93, 255))
        draw.rectangle((18, 29, 42, 33), fill=(212, 171, 80, 255))
        draw.rectangle((18, 39, 36, 43), fill=(36, 104, 93, 255))
        return image

    def set_status(self, status: str, error: str = "") -> None:
        with self.lock:
            self.status = status
            self.error = error
        self.icon.title = f"科研文献管理器：{status}"
        try:
            self.icon.update_menu()
        except Exception:
            pass

    def run(self) -> None:
        threading.Thread(target=self.start_service, kwargs={"open_when_ready": True}, daemon=True).start()
        self.icon.run()

    def on_open(self, _icon: object, _item: object) -> None:
        if read_health():
            open_app_url()
        elif port_is_open():
            notify(self.icon, "文献管理器", "8765 端口被占用，但不是当前服务。请查看日志。")
        else:
            threading.Thread(target=self.start_service, kwargs={"open_when_ready": True}, daemon=True).start()

    def on_restart(self, _icon: object, _item: object) -> None:
        threading.Thread(target=self.restart_service, daemon=True).start()

    def on_stop(self, _icon: object, _item: object) -> None:
        threading.Thread(target=self.stop_service, daemon=True).start()

    def on_exit(self, icon: object, _item: object) -> None:
        icon.stop()  # type: ignore[attr-defined]
        threading.Thread(target=self.stop_service, daemon=False).start()

    def start_service(self, *, open_when_ready: bool) -> None:
        self.set_status("启动中")
        try:
            ensure_environment()
            if read_health():
                self.set_status("运行中")
                if open_when_ready:
                    open_app_url()
                return
            if port_is_open():
                processes = query_port_processes()
                logging.error("端口 %s 已被其他程序占用：%s", PORT, processes)
                self.set_status("启动失败", f"端口 {PORT} 已被其他程序占用")
                notify(self.icon, "文献管理器启动失败", f"端口 {PORT} 已被其他程序占用。")
                return

            env = os.environ.copy()
            env["PAPER_MANAGER_SKIP_BROWSER"] = "1"
            server_log = SERVER_LOG.open("ab")
            self.server_process = subprocess.Popen(
                [str(VENV_PYTHON), "-m", "backend.app"],
                cwd=ROOT,
                env=env,
                stdout=server_log,
                stderr=subprocess.STDOUT,
                **hidden_subprocess_kwargs(),
            )
            deadline = time.time() + 30
            while time.time() < deadline:
                if self.server_process.poll() is not None:
                    raise RuntimeError(f"后端服务已退出，退出码 {self.server_process.returncode}")
                if read_health():
                    self.set_status("运行中")
                    if open_when_ready:
                        open_app_url()
                    return
                time.sleep(0.4)
            raise RuntimeError("后端服务启动超时")
        except Exception as exc:
            logging.exception("启动失败")
            self.set_status("启动失败", str(exc))
            notify(self.icon, "文献管理器启动失败", f"{exc}\n详情见 data/logs/launcher.log")

    def stop_service(self) -> None:
        self.set_status("停止中")
        try:
            if self.server_process and self.server_process.poll() is None:
                self.server_process.terminate()
                try:
                    self.server_process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self.server_process.kill()
            for pid in paper_manager_pids_on_port():
                try:
                    subprocess.run(
                        ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        **hidden_subprocess_kwargs(),
                    )
                except OSError:
                    logging.warning("无法停止后端进程 PID %s", pid)
            self.set_status("已停止")
        except Exception as exc:
            logging.exception("停止失败")
            self.set_status("停止失败", str(exc))
            notify(self.icon, "文献管理器停止失败", str(exc))

    def restart_service(self) -> None:
        self.stop_service()
        time.sleep(0.8)
        self.start_service(open_when_ready=True)


def main() -> None:
    setup_logging()
    logging.info("launcher start, executable=%s", sys.executable)
    ensure_environment()
    relaunch_inside_venv_if_needed()
    guard = SingleInstanceGuard()
    if guard.already_running:
        logging.info("another launcher instance is already running")
        if read_health():
            open_app_url()
        else:
            logging.info("existing launcher found, but backend health is not ready")
        guard.close()
        return
    try:
        tray = PaperManagerTray()
        tray.run()
    finally:
        guard.close()


def show_fatal_error(message: str) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("科研文献管理器启动失败", f"{message}\n\n详情见：{LAUNCHER_LOG}")
        root.destroy()
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        setup_logging()
        logging.error("fatal launcher error:\n%s", traceback.format_exc())
        show_fatal_error(str(exc))
