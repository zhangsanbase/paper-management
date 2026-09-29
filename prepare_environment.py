from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent


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


def frontend_build_needed(root: Path = ROOT) -> bool:
    dist_index = root / "frontend" / "dist" / "index.html"
    if not dist_index.exists():
        return True
    dist_time = dist_index.stat().st_mtime
    for item in ("src", "index.html", "package.json", "vite.config.ts", "tsconfig.json"):
        target = root / item
        if not target.exists():
            continue
        files = [target] if target.is_file() else [path for path in target.rglob("*") if path.is_file()]
        if any(path.stat().st_mtime > dist_time for path in files):
            return True
    return False


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
            print("Installing Python dependencies...")
        _run_checked(
            [str(venv_python), "-m", "pip", "install", "-r", str(requirements)],
            root=root,
            quiet=quiet,
        )
        marker_path.touch()

    if not (root / "node_modules").exists():
        if not quiet:
            print("Installing frontend dependencies...")
        _run_checked([_npm_executable(), "install"], root=root, quiet=quiet)

    if frontend_build_needed(root):
        if not quiet:
            print("Building frontend...")
        _run_checked([_npm_executable(), "run", "build"], root=root, quiet=quiet)


if __name__ == "__main__":
    ensure_environment(quiet="--quiet" in sys.argv[1:])
