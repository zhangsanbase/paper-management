from __future__ import annotations

import json

import httpx
import pytest

from backend.ai import describe_ai_exception
from backend.app import describe_subscription_exception
from backend.redaction import redact_sensitive_text


@pytest.mark.parametrize("field", [
    "access_token", "refresh_token", "access", "refresh", "api_key", "password", "token", "code_verifier",
])
def test_error_redaction_covers_json_and_quoted_or_unquoted_fields(field) -> None:
    marker = "synthetic-credential-alpha"
    for text in [json.dumps({field: marker}), f"{field}='{marker}'", f"{field}={marker}"]:
        assert marker not in redact_sensitive_text(text)


@pytest.mark.parametrize("template", [
    "Bearer {}",
    "http://user:{}@proxy.invalid",
    "http://127.0.0.1/callback?code={}&state=synthetic-state",
])
def test_error_redaction_covers_headers_proxy_passwords_and_callback_codes(template) -> None:
    marker = "synthetic-credential-alpha"
    assert marker not in redact_sensitive_text(template.format(marker))


def test_api_errors_remove_the_request_key_even_when_echoed_as_plain_text() -> None:
    marker = "synthetic-credential-alpha"
    request = httpx.Request("POST", "https://api.example/v1/chat/completions", headers={"Authorization": f"Bearer {marker}"})
    response = httpx.Response(401, request=request, text=f"Invalid credential: {marker}")
    error = httpx.HTTPStatusError("upstream failure", request=request, response=response)

    message = describe_ai_exception("AI 连接测试失败", error)

    assert marker not in message
    assert "HTTP 401" in message
    assert "Invalid credential" in message


def test_generic_and_subscription_errors_redact_credentials_and_keep_useful_context() -> None:
    marker = "synthetic-credential-alpha"
    error = RuntimeError(f"upstream failed: password='{marker}'")
    assert marker not in describe_ai_exception("AI 失败", error)
    assert marker not in describe_subscription_exception(error)
    assert redact_sensitive_text("model not found; HTTP 404") == "model not found; HTTP 404"
