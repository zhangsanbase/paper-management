from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent

# 参与前端构建哈希的输入。判断"dist 是否过期"看的是这些文件的内容，
# 不是修改时间：git clone / checkout 会把所有文件的 mtime 统一成检出时间，
# 拿 mtime 比较会得出与内容无关的结论，可能让你对着旧界面查半天。
BUILD_INPUTS = (
    "src",
    "index.html",
    "package.json",
    "package-lock.json",
    "vite.config.ts",
    "tsconfig.json",
)

# 构建戳记，记录 frontend/dist 是由哪一份源码构建出来的。
# 它随 dist 一起进入版本库，所以新克隆的目录不需要 Node.js 也能确认产物是最新的。
BUILD_STAMP = Path("frontend") / ".build-stamp"


def _npm_executable() -> str:
    name = "npm.cmd" if os.name == "nt" else "npm"
    executable = shutil.which(name)
    if executable is None:
        raise RuntimeError("未找到 npm，请安装 Node.js 并确认 npm 已加入 PATH")
    return executable


def _run_checked(command: list[str], *, root: Path, quiet: bool) -> None:
    kwargs: dict[str, object] = {}
    if quiet:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    result = subprocess.run(command, cwd=root, **kwargs)
    output = result.stdout if quiet else ""
    if output:
        print(output)
    if result.returncode != 0:
        raise RuntimeError(f"命令执行失败：{' '.join(command)}\n{output}")


def build_input_hash(root: Path = ROOT) -> str:
    """把前端构建输入的内容摘要成一个十六进制哈希。"""
    root = root.resolve()
    digest = hashlib.sha256()
    for item in BUILD_INPUTS:
        target = root / item
        if not target.exists():
            continue
        paths = [target] if target.is_file() else sorted(path for path in target.rglob("*") if path.is_file())
        for path in paths:
            if "__pycache__" in path.parts:
                continue
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            # 先把行尾统一成 LF 再入哈希：core.autocrlf 会让同一份源码在克隆后
            # 变成 CRLF，按原始字节比较会把没改过的代码误判成已修改。
            digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
            digest.update(b"\0")
    return digest.hexdigest()


def read_build_stamp(root: Path = ROOT) -> str | None:
    """读取戳记里的哈希；文件缺失或损坏时返回 None（视为来源不明）。"""
    try:
        data = json.loads((root.resolve() / BUILD_STAMP).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = data.get("hash") if isinstance(data, dict) else None
    return value if isinstance(value, str) else None


def write_build_stamp(root: Path = ROOT, *, stamp_hash: str, quiet: bool = False) -> None:
    """写入构建戳记。哈希没变就不改写，免得每次启动都产生无意义的 diff。"""
    root = root.resolve()
    if read_build_stamp(root) == stamp_hash:
        return
    path = root / BUILD_STAMP
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "hash": stamp_hash,
        "built_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not quiet:
        print(f"Updated {BUILD_STAMP.as_posix()}", flush=True)


def frontend_build_needed(root: Path = ROOT) -> bool:
    """构建产物缺失、或不是由当前源码构建出来的时候返回 True。"""
    root = root.resolve()
    if not (root / "frontend" / "dist" / "index.html").exists():
        return True
    return read_build_stamp(root) != build_input_hash(root)


def ensure_environment(root: Path = ROOT, *, quiet: bool = False) -> None:
    root = root.resolve()
    venv_dir = root / ".venv"
    venv_python = venv_dir / "Scripts" / "python.exe"
    marker_path = venv_dir / ".requirements-installed"

    if not venv_python.exists():
        _run_checked([sys.executable, "-m", "venv", str(venv_dir)], root=root, quiet=quiet)
    if not venv_python.exists():
        raise RuntimeError("无法创建 .venv Python 环境")

    requirements = root / "requirements.txt"
    needs_install = not marker_path.exists()
    if marker_path.exists() and requirements.exists():
        needs_install = requirements.stat().st_mtime > marker_path.stat().st_mtime
    if needs_install:
        if not quiet:
            print("Installing Python dependencies...", flush=True)
        _run_checked(
            [str(venv_python), "-m", "pip", "install", "-r", str(requirements)],
            root=root,
            quiet=quiet,
        )
        marker_path.touch()

    # 前端只在确实需要重建时才碰 npm。仓库里已经带上 dist 和匹配的戳记，
    # 所以没装 Node.js 的机器可以完全跳过这一段。
    if not frontend_build_needed(root):
        return

    if not (root / "node_modules").exists():
        if not quiet:
            print("Installing frontend dependencies...", flush=True)
        _run_checked([_npm_executable(), "install"], root=root, quiet=quiet)

    if not quiet:
        print("Building frontend...", flush=True)
    _run_checked([_npm_executable(), "run", "build"], root=root, quiet=quiet)
    # 构建后再算一次哈希：npm install 可能改写过 package-lock.json。
    write_build_stamp(root, stamp_hash=build_input_hash(root), quiet=quiet)


if __name__ == "__main__":
    ensure_environment(quiet="--quiet" in sys.argv[1:])
