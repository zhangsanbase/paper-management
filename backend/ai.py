from __future__ import annotations

import json
import sqlite3
from typing import Any

import httpx

from backend.redaction import redact_sensitive_text


class AIResponseError(RuntimeError):
    """Raised when the upstream AI response shape or JSON payload is unusable."""


def build_ai_prompt(file_name: str, first_page_text: str, tags: list[dict[str, Any]]) -> list[dict[str, str]]:
    tag_payload = [
        {
            "id": tag["id"],
            "name": tag["name"],
            "description": tag["description"],
            "use_count": tag["use_count"],
        }
        for tag in tags
    ]
    system = (
        "你是科研文献管理器的信息抽取模块。必须只返回合法 JSON，不要输出 Markdown。"
        "标签只描述论文研究对象、研究方向或核心问题，不描述常规表征、测试、仪器或实验技术。"
        "XPS、SEM、TEM、XRD、Raman、FTIR、CV、EIS 等默认不是主题标签，除非论文主题本身就是这些方法。"
        "已有标签中的 name 是唯一可用于匹配的正式标签名。"
        "优先从已有英文标签中选择并返回真实 existing_tag_ids，只有标题和摘要证明已有标签无法覆盖研究主题时，才建议新增标签。"
        "new_tag_suggestions.name 必须使用简洁英文研究主题标签，不允许输出中文标签名。"
        "同时生成简洁准确的中文题名 title_zh，以及一段基于首页摘要或可识别信息的中文摘要 abstract。"
        "title_zh 和 abstract 不要编造首页中没有的信息，信息不足时返回 null。"
        "禁止编造 DOI、作者、单位或出版时间。"
        "journal_name 只提取论文首页明确出现的期刊或正式出版物名称，不要把论文标题、出版社或平台名当作期刊；无法确认时返回 null。"
    )
    user = {
        "file_name": file_name,
        "first_page_text": first_page_text,
        "existing_tags": tag_payload,
        "required_json_schema": {
            "title": "string|null",
            "title_zh": "string|null",
            "abstract": "string|null",
            "authors": ["string"],
            "affiliations": ["string"],
            "publication_date": "YYYY-MM-DD|YYYY|null",
            "doi_url": "string|null",
            "journal_name": "string|null",
            "existing_tag_ids": ["string"],
            "new_tag_suggestions": [{"name": "string", "reason": "string"}],
            "confidence": {"metadata": 0.0, "tags": 0.0},
        },
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]


def build_journal_prompt(file_name: str, first_page_text: str) -> list[dict[str, str]]:
    system = (
        "你是科研文献管理器的期刊名称提取模块。必须只返回合法 JSON，不要输出 Markdown。"
        "只提取首页明确出现的期刊或正式出版物名称，不要把论文标题、出版社、数据库或平台名当作期刊。"
        "无法确认时返回 null，不要猜测。"
    )
    user = {
        "file_name": file_name,
        "first_page_text": first_page_text,
        "required_json_schema": {"journal_name": "string|null"},
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]


def build_title_translation_prompt(paper: sqlite3.Row) -> list[dict[str, str]]:
    system = "你是科研文献管理器的标题翻译模块。必须只返回合法 JSON，不要输出 Markdown。"
    user = {
        "task": "把论文标题翻译成简洁准确的中文题名。",
        "title": paper["title"],
        "file_name": paper["file_name"],
        "required_json_schema": {"title_zh": "string|null"},
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]


def build_abstract_prompt(paper: sqlite3.Row, first_page_text: str) -> list[dict[str, str]]:
    system = "你是科研文献管理器的摘要整理模块。必须只返回合法 JSON，不要输出 Markdown。"
    user = {
        "task": "基于首页文字中的摘要或可识别信息，生成一段中文摘要。不要编造首页中没有的信息。",
        "title": paper["title"],
        "title_zh": paper["title_zh"],
        "file_name": paper["file_name"],
        "first_page_text": first_page_text,
        "required_json_schema": {"abstract": "string|null"},
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]


def parse_ai_json(content: str) -> dict[str, Any]:
    content = content.strip()
    if content.startswith("```"):
        content = content.strip("`")
        if content.lower().startswith("json"):
            content = content[4:].strip()
    data = json.loads(content)
    if not isinstance(data, dict):
        raise ValueError("AI 返回不是 JSON 对象")
    return data


async def call_ai(config: dict[str, str], messages: list[dict[str, str]]) -> dict[str, Any]:
    if not config.get("base_url") or not config.get("api_key") or not config.get("model"):
        raise RuntimeError("AI API 未配置")
    base_url = config["base_url"].rstrip("/")
    payload = {
        "model": config["model"],
        "messages": messages,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {config['api_key']}"},
            json=payload,
        )
        if response.status_code in {400, 422}:
            fallback_payload = dict(payload)
            fallback_payload.pop("response_format", None)
            response = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {config['api_key']}"},
                json=fallback_payload,
            )
        response.raise_for_status()
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise AIResponseError(f"AI 上游响应不是合法 JSON：{exc.msg}") from exc
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AIResponseError("AI 返回结构缺少 choices[0].message.content") from exc
    try:
        return parse_ai_json(content)
    except json.JSONDecodeError as exc:
        raise AIResponseError(f"AI 内容不是合法 JSON：{exc.msg}") from exc
    except ValueError as exc:
        raise AIResponseError(str(exc)) from exc


def describe_ai_exception(prefix: str, exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return f"{prefix}：AI 请求超时，请稍后重试或检查模型服务。"
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        authorization = exc.request.headers.get("Authorization", "")
        body = redact_sensitive_text(
            response.text.strip().replace("\n", " "),
            secrets=(authorization, authorization.partition(" ")[2]),
        )[:500]
        suffix = f"：{body}" if body else ""
        return f"{prefix}：AI 上游返回 HTTP {response.status_code}{suffix}"
    if isinstance(exc, AIResponseError):
        return f"{prefix}：{redact_sensitive_text(exc)}"
    if isinstance(exc, json.JSONDecodeError):
        return f"{prefix}：AI 返回不是合法 JSON：{exc.msg}"
    if "AI API 未配置" in str(exc):
        return f"{prefix}：AI API 未配置，请先填写 Base URL、API Key 和 Model。"
    if "socksio" in str(exc).lower() or "socks support" in str(exc).lower():
        return f"{prefix}：当前网络代理使用 SOCKS，需要安装 httpx[socks] 并重启后端服务。"
    return f"{prefix}：{redact_sensitive_text(exc or '未知错误')}"
