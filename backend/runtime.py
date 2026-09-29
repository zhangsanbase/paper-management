from __future__ import annotations

from types import ModuleType
from typing import Any


class ApplicationRuntime:
    """Dynamic application dependency boundary used by routes and services."""

    def __init__(self, source: ModuleType) -> None:
        object.__setattr__(self, "_source", source)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._source, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_source":
            object.__setattr__(self, name, value)
        else:
            setattr(self._source, name, value)
