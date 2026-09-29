"""服务监听地址与端口的唯一来源。

改端口的唯一入口是环境变量 ``PAPER_MANAGER_PORT``；后端、托盘启动器和两个
PowerShell 启停脚本都从这里取值，避免端口散落在多个文件里各写一份。
"""

from __future__ import annotations

import os

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PORT_ENV = "PAPER_MANAGER_PORT"


def resolve_port() -> int:
    """读取监听端口，未设置时用 :data:`DEFAULT_PORT`。

    取值非法时直接抛错而不是静默回退：这类问题应该立刻暴露，
    否则后端和托盘会各自监听到不同端口上，症状会非常难查。
    """
    raw = os.environ.get(PORT_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_PORT
    try:
        port = int(raw.strip())
    except ValueError:
        raise RuntimeError(f"{PORT_ENV} 必须是整数端口号，当前值：{raw!r}") from None
    if not 1 <= port <= 65535:
        raise RuntimeError(f"{PORT_ENV} 必须在 1-65535 之间，当前值：{port}")
    return port


def server_url(port: int | None = None) -> str:
    """拼出服务地址，端口缺省时按环境变量解析。"""
    return f"http://{HOST}:{resolve_port() if port is None else port}"
