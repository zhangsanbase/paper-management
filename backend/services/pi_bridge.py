from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import uuid
from collections.abc import Awaitable
from pathlib import Path
from typing import Any


MINIMUM_NODE_VERSION = (22, 19, 0)


def runtime_status(bridge_root: Path) -> dict[str, Any]:
    executable = shutil.which("node")
    if executable is None:
        return {
            "available": False,
            "node_available": False,
            "node_version": None,
            "required_node_version": "22.19.0",
            "message": "订阅接入需要安装 Node.js 22.19.0 或更新版本。",
        }
    try:
        result = __import__("subprocess").run(
            [executable, "--version"], capture_output=True, text=True, timeout=3, check=True,
        )
        raw_version = result.stdout.strip().removeprefix("v")
        version = tuple(int(part) for part in raw_version.split(".")[:3])
    except (OSError, ValueError, TimeoutError, __import__("subprocess").SubprocessError):
        return {
            "available": False,
            "node_available": False,
            "node_version": None,
            "required_node_version": "22.19.0",
            "message": "无法读取 Node.js 版本，请检查 Node.js 安装后重启应用。",
        }
    dependencies_present = all(path.is_file() for path in (
        bridge_root / "node_modules" / "@earendil-works" / "pi-ai" / "dist" / "index.js",
        bridge_root / "node_modules" / "undici" / "index.js",
    ))
    available = version >= MINIMUM_NODE_VERSION and dependencies_present
    if version < MINIMUM_NODE_VERSION:
        message = f"订阅接入需要 Node.js 22.19.0 或更新版本；当前为 {raw_version}。"
    elif not available:
        message = "订阅适配依赖尚未安装；请运行项目环境准备后重试。"
    else:
        message = "pi 订阅适配已就绪。"
    return {
        "available": available,
        "node_available": True,
        "node_version": raw_version,
        "required_node_version": "22.19.0",
        "message": message,
    }


class PIModelBridge:
    def __init__(self, bridge_root: Path, data_dir: Path) -> None:
        self.bridge_root = bridge_root
        self.script_path = bridge_root / "index.mjs"
        self.auth_path = data_dir / "model_auth.json"
        self.context_path = data_dir / "pi_auth_context.json"
        self.process: asyncio.subprocess.Process | None = None
        self.pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.write_lock = asyncio.Lock()
        self.start_lock = asyncio.Lock()
        self.reader_task: asyncio.Task[None] | None = None

    async def _read_output(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            while line := await process.stdout.readline():
                try:
                    frame = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(frame, dict):
                    continue
                request_id = frame.get("id")
                future = self.pending.get(request_id)
                if future is not None and not future.done():
                    if frame.get("ok"):
                        future.set_result(frame.get("result") or {})
                    else:
                        future.set_exception(RuntimeError(str(frame.get("error") or "订阅服务请求失败。")))
        except (asyncio.CancelledError, OSError):
            pass
        finally:
            message = "订阅模型适配进程已退出，请重启应用后重试。"
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RuntimeError(message))
            if self.process is process:
                self.process = None
            if process is not None:
                await process.wait()

    async def _start(self) -> None:
        async with self.start_lock:
            if self.process is not None and self.process.returncode is None and self.reader_task and not self.reader_task.done():
                return
            await self.close_async()
            status = runtime_status(self.bridge_root)
            if not status["available"]:
                raise RuntimeError(status["message"])
            creation_options: dict[str, Any] = {}
            if os.name == "nt":
                creation_options["creationflags"] = __import__("subprocess").CREATE_NO_WINDOW
            self.process = await asyncio.create_subprocess_exec(
                shutil.which("node"),
                str(self.script_path),
                str(self.auth_path),
                str(self.context_path),
                cwd=self.bridge_root,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                **creation_options,
            )
            self.reader_task = asyncio.create_task(self._read_output())

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        await self._start()
        process = self.process
        if process is None or process.stdin is None:
            raise RuntimeError("无法启动本地 pi 订阅适配进程。")
        request_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self.pending[request_id] = future
        frame = json.dumps({"id": request_id, "method": method, "params": params or {}}, ensure_ascii=False)
        try:
            async with self.write_lock:
                process.stdin.write((frame + "\n").encode("utf-8"))
                await process.stdin.drain()
            return await asyncio.wait_for(future, timeout=80)
        except asyncio.TimeoutError as exc:
            raise RuntimeError("订阅模型请求超时，请重试或检查网络。") from exc
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            self.pending.pop(request_id, None)
            future.cancel()
            await self.close_async()
            raise RuntimeError("本地 pi 订阅适配进程已退出，请重启应用后重试。") from exc
        finally:
            self.pending.pop(request_id, None)

    async def invoke(self, method: str, **params: Any) -> dict[str, Any]:
        return await self.request(method, params)

    async def close_async(self) -> None:
        process = self.process
        reader = self.reader_task
        self.process = None
        self.reader_task = None
        for future in self.pending.values():
            if not future.done():
                future.set_exception(RuntimeError("本地 pi 订阅适配进程已关闭，请重试。"))
        self.pending.clear()
        if process is not None:
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                if process.returncode is None:
                    try:
                        await asyncio.wait_for(process.wait(), timeout=3)
                    except asyncio.TimeoutError:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                        await process.wait()
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

    def close(self) -> None:
        if self.process is not None and self.process.returncode is None:
            self.process.kill()
        self.process = None
        self.reader_task = None
        self.pending.clear()
