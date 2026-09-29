from __future__ import annotations

import re
from typing import Any


METHOD_TAG_BLACKLIST = {
    "xps",
    "sem",
    "tem",
    "hrtem",
    "xrd",
    "raman",
    "ftir",
    "uv-vis",
    "uv vis",
    "cv",
    "lsv",
    "eis",
    "bet",
    "gc-ms",
    "gc ms",
    "hplc",
    "nmr",
    "afm",
}


def normalize_label(value: str) -> str:
    return " ".join(value.strip().lower().replace("－", "-").split())


def is_method_tag(name: str) -> bool:
    normalized = normalize_label(name)
    return normalized in METHOD_TAG_BLACKLIST


def nullable_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def coerce_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = value.replace(",", "；").split("；")
        return [part.strip() for part in parts if part.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def normalize_aliases(name: str, aliases: Any) -> list[str]:
    values = coerce_string_list(aliases)
    return values or [name]


def coerce_confidence(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, number))


def normalize_doi_url(value: Any) -> str | None:
    text = nullable_str(value)
    if not text:
        return None
    if text.lower().startswith(("http://", "https://")):
        return text
    if text.lower().startswith("doi:"):
        text = text[4:].strip()
    if text.startswith("10."):
        return f"https://doi.org/{text}"
    return text


def safe_filename_stem(value: str, max_length: int = 180) -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", value)
    text = " ".join(text.split()).strip(" .")
    if not text:
        text = "paper"
    return text[:max_length].rstrip(" .") or "paper"


def extract_publication_year(value: Any) -> str | None:
    text = nullable_str(value)
    if not text:
        return None
    match = re.search(r"\b(19|20)\d{2}\b", text)
    return match.group(0) if match else None
