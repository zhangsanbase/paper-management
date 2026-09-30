from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 2
MASKED_API_KEY = "********"
SUBSCRIPTION_PROVIDERS = {"openai", "anthropic", "github-copilot"}


def default_config() -> dict[str, Any]:
    return {"version": SCHEMA_VERSION, "active_profile_id": None, "profiles": []}


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("模型配置文件不是合法 JSON，原文件已保留，请先修复后重试。") from exc


def read_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return default_config()
    data = _read_json(path)
    if isinstance(data, dict) and data.get("version") == SCHEMA_VERSION:
        if not isinstance(data.get("profiles"), list):
            raise ValueError("模型配置文件格式无效：profiles 必须是数组。")
        ids = [profile.get("id") for profile in data["profiles"] if isinstance(profile, dict)]
        if (
            len(ids) != len(data["profiles"])
            or any(not isinstance(profile_id, str) or not profile_id.strip() for profile_id in ids)
            or len(ids) != len(set(ids))
        ):
            raise ValueError("模型配置文件格式无效：配置 ID 缺失或重复。")
        active_id = data.get("active_profile_id")
        if active_id is not None and active_id not in ids:
            raise ValueError("模型配置文件格式无效：当前配置不存在。")
        return data

    if not isinstance(data, dict):
        raise ValueError("旧模型配置文件格式无效，原文件已保留。")
    old_fields = {"base_url", "api_key", "model"}
    if not old_fields.intersection(data) and data:
        raise ValueError("旧模型配置包含不支持的字段，原文件已保留。")

    profile_id = "legacy-api"
    base_url = str(data.get("base_url") or "")
    api_key = str(data.get("api_key") or "")
    model = str(data.get("model") or "")
    profile = {
        "id": profile_id,
        "name": "原 API 配置",
        "kind": "api",
        "provider": "openai-compatible",
        "base_url": base_url,
        "api_key": api_key,
        "model": model,
    }
    migrated = {
        "version": SCHEMA_VERSION,
        "active_profile_id": profile_id if base_url.strip() and api_key.strip() and model.strip() else None,
        "profiles": [profile] if any((base_url.strip(), api_key.strip(), model.strip())) else [],
    }

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_path = path.with_name(f"{path.name}.before-model-config-{stamp}.bak")
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("rb") as source, backup_path.open("xb") as backup:
        while chunk := source.read(1024 * 1024):
            backup.write(chunk)
        backup.flush()
        os.fsync(backup.fileno())
    _atomic_write(path, migrated)
    return migrated


def public_config(data: dict[str, Any], *, mask_keys: bool) -> dict[str, Any]:
    result = json.loads(json.dumps(data, ensure_ascii=False))
    for profile in result["profiles"]:
        if profile.get("kind") == "api":
            key = str(profile.get("api_key") or "")
            if mask_keys:
                profile["api_key"] = MASKED_API_KEY if key else ""
            profile["api_key_configured"] = bool(key and key != MASKED_API_KEY)
        elif profile.get("kind") == "subscription":
            profile["base_url"] = ""
            profile["api_key"] = ""
            profile["api_key_configured"] = False
    return result


def get_profile(data: dict[str, Any], profile_id: str) -> dict[str, Any] | None:
    return next((profile for profile in data["profiles"] if profile["id"] == profile_id), None)


def is_profile_ready(profile: dict[str, Any], auth_path: Path) -> bool:
    if profile.get("kind") == "api":
        return all(
            str(profile.get(field) or "").strip()
            for field in ("base_url", "api_key", "model")
        )
    if profile.get("kind") != "subscription" or profile.get("provider") not in SUBSCRIPTION_PROVIDERS:
        return False
    if not str(profile.get("model") or "").strip() or not auth_path.exists():
        return False
    try:
        credentials = _read_json(auth_path)
    except ValueError:
        return False
    if not isinstance(credentials, dict):
        return False
    all_profiles = credentials.get("profiles", {})
    if not isinstance(all_profiles, dict):
        return False
    profile_credentials = all_profiles.get(profile["id"], {})
    if not isinstance(profile_credentials, dict):
        return False
    provider_credential = profile_credentials.get(profile["provider"], {})
    return isinstance(provider_credential, dict) and provider_credential.get("type") == "oauth"


def save_config(
    path: Path,
    data_dir: Path,
    submitted: dict[str, Any],
) -> dict[str, Any]:
    if submitted.get("version") != SCHEMA_VERSION or not isinstance(submitted.get("profiles"), list):
        raise ValueError("模型配置版本无效，请重新加载页面后再保存。")
    current = read_config(path)
    ids: set[str] = set()
    profiles: list[dict[str, Any]] = []
    for incoming in submitted["profiles"]:
        if not isinstance(incoming, dict):
            raise ValueError("模型配置格式无效。")
        profile_id = str(incoming.get("id") or "").strip()
        if not profile_id or len(profile_id) > 100 or profile_id in ids:
            raise ValueError("模型配置 ID 缺失或重复，请重新加载页面。")
        ids.add(profile_id)
        name = str(incoming.get("name") or "").strip()
        kind = incoming.get("kind")
        provider = incoming.get("provider")
        model = str(incoming.get("model") or "").strip()
        if not name:
            raise ValueError("请填写每组模型配置的名称。")
        if kind not in {"api", "subscription"}:
            raise ValueError(f"配置「{name}」的接入方式无效。")
        if kind == "api" and provider != "openai-compatible":
            raise ValueError(f"配置「{name}」的 API 服务商无效。")
        if kind == "subscription" and provider not in SUBSCRIPTION_PROVIDERS:
            raise ValueError(f"配置「{name}」的订阅服务商无效。")

        profile: dict[str, Any] = {
            "id": profile_id,
            "name": name,
            "kind": kind,
            "provider": provider,
            "model": model,
        }
        if kind == "api":
            previous = get_profile(current, profile_id)
            submitted_key = str(incoming.get("api_key") or "")
            if submitted_key == MASKED_API_KEY:
                if not previous or previous.get("kind") != "api" or not previous.get("api_key"):
                    raise ValueError("此 API 配置的密钥无法验证，请重新填写 API Key。")
                submitted_key = str(previous["api_key"])
            profile.update(
                base_url=str(incoming.get("base_url") or "").strip(),
                api_key=submitted_key,
            )
        profiles.append(profile)

    active_id = submitted.get("active_profile_id")
    if active_id is not None:
        active_profile = next((item for item in profiles if item["id"] == active_id), None)
        if active_profile is None:
            raise ValueError("当前配置不存在，请重新选择后再保存。")
        if not is_profile_ready(active_profile, data_dir / "model_auth.json"):
            active_id = None

    saved = {"version": SCHEMA_VERSION, "active_profile_id": active_id, "profiles": profiles}
    _atomic_write(path, saved)
    return saved


def resolve_active_config(data: dict[str, Any]) -> dict[str, str]:
    profile = get_profile(data, str(data.get("active_profile_id") or ""))
    if profile is None or profile.get("kind") != "api":
        return {"base_url": "", "api_key": "", "model": ""}
    return {
        "base_url": str(profile.get("base_url") or ""),
        "api_key": str(profile.get("api_key") or ""),
        "model": str(profile.get("model") or ""),
    }


def profile_draft_to_api_config(profile: dict[str, Any], data: dict[str, Any]) -> dict[str, str]:
    if profile.get("kind") != "api":
        return {"base_url": "", "api_key": "", "model": ""}
    existing = get_profile(data, str(profile.get("id") or ""))
    key = str(profile.get("api_key") or "")
    if key == MASKED_API_KEY:
        key = str(existing.get("api_key") or "") if existing and existing.get("kind") == "api" else ""
    return {
        "base_url": str(profile.get("base_url") or ""),
        "api_key": key,
        "model": str(profile.get("model") or ""),
    }


def update_legacy_config(path: Path, data_dir: Path, config: dict[str, str]) -> dict[str, Any]:
    data = read_config(path)
    active = get_profile(data, str(data.get("active_profile_id") or ""))
    if active and active.get("kind") == "api":
        active_id = active["id"]
    else:
        active_id = str(uuid.uuid4())
        data["profiles"].append(
            {"id": active_id, "name": "API 配置", "kind": "api", "provider": "openai-compatible", "model": ""}
        )
    for profile in data["profiles"]:
        if profile["id"] != active_id:
            continue
        key = str(config.get("api_key") or "")
        if key == MASKED_API_KEY:
            existing = profile.get("api_key")
            if not existing:
                raise ValueError("此 API 配置的密钥无法验证，请重新填写 API Key。")
            key = str(existing)
        profile.update(
            base_url=str(config.get("base_url") or "").strip(),
            api_key=key,
            model=str(config.get("model") or "").strip(),
        )
        break
    data["active_profile_id"] = active_id if all(
        str(get_profile(data, active_id).get(key) or "").strip() for key in ("base_url", "api_key", "model")
    ) else None
    _atomic_write(path, data)
    return data
