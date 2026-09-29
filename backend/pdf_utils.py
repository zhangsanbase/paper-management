from __future__ import annotations

from pathlib import Path

import fitz


def extract_first_page_text(path: Path) -> str:
    try:
        with fitz.open(path) as doc:
            if doc.page_count == 0:
                return ""
            text = doc[0].get_text("text")
            return " ".join(text.split())[:6000]
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"PDF 首页文本提取失败：{exc}") from exc
