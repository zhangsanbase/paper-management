from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

import backend.app as app_module
from backend.app import create_app
from backend.model_configs import MASKED_API_KEY, read_config, save_config
from backend.services.pi_bridge import PIModelBridge, runtime_status


def _api_profile(profile_id: str, name: str, key: str = "", model: str = "") -> dict[str, object]:
    return {
        "id": profile_id,
        "name": name,
        "kind": "api",
        "provider": "openai-compatible",
        "base_url": "https://api.example/v1",
        "api_key": key,
        "model": model,
    }


def _subscription_profile(profile_id: str, provider: str = "openai") -> dict[str, object]:
    return {
        "id": profile_id,
        "name": f"{provider} subscription",
        "kind": "subscription",
        "provider": provider,
        "base_url": "",
        "api_key": "",
        "model": "",
    }


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "library.sqlite3")
    monkeypatch.setattr(app_module, "CONFIG_PATH", tmp_path / "data" / "config.json")
    monkeypatch.setattr(app_module, "PDF_VIEWER_CONFIG_PATH", tmp_path / "pdf_viewer.json")
    monkeypatch.setattr(app_module, "LIBRARY_DIR", tmp_path / "library_files")
    monkeypatch.setattr(app_module, "PATH_BASE", tmp_path)
    return TestClient(create_app())


def test_legacy_config_migrates_once_and_preserves_backup(tmp_path, monkeypatch) -> None:
    path = tmp_path / "data" / "config.json"
    path.parent.mkdir(parents=True)
    legacy_contents = '{"base_url":"https://legacy.example/v1","api_key":"old-local-key","model":"paper-reader"}\n'
    path.write_text(legacy_contents, encoding="utf-8")

    migrated = read_config(path)

    assert migrated["version"] == 2
    assert migrated["active_profile_id"] == "legacy-api"
    assert migrated["profiles"][0]["name"] == "原 API 配置"
    assert migrated["profiles"][0]["api_key"] == "old-local-key"
    backups = list(path.parent.glob("config.json.before-model-config-*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == legacy_contents

    read_config(path)

    assert list(path.parent.glob("config.json.before-model-config-*.bak")) == backups


def test_failed_config_migration_preserves_the_original_file(tmp_path, monkeypatch) -> None:
    path = tmp_path / "config.json"
    original = '{"base_url":"https://legacy.example/v1","api_key":"local-test-key","model":"paper-reader"}\n'
    path.write_text(original, encoding="utf-8")

    def fail_atomic_write(*_args, **_kwargs):
        raise OSError("synthetic disk error")

    monkeypatch.setattr("backend.model_configs._atomic_write", fail_atomic_write)
    with pytest.raises(OSError, match="synthetic disk error"):
        read_config(path)

    assert path.read_text(encoding="utf-8") == original
    assert len(list(path.parent.glob("config.json.before-model-config-*.bak"))) == 1


def test_incomplete_legacy_config_is_preserved_without_enabling(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"base_url":"","api_key":"","model":""}', encoding="utf-8")

    config = read_config(path)

    assert config["active_profile_id"] is None
    assert config["profiles"] == []


def test_partially_filled_legacy_config_is_preserved_without_enabling(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"base_url":"https://legacy.example/v1","api_key":"","model":""}', encoding="utf-8")

    config = read_config(path)

    assert config["active_profile_id"] is None
    assert config["profiles"] == [{
        "id": "legacy-api",
        "name": "原 API 配置",
        "kind": "api",
        "provider": "openai-compatible",
        "base_url": "https://legacy.example/v1",
        "api_key": "",
        "model": "",
    }]


def test_model_config_masking_and_profile_scoped_credentials(client, monkeypatch) -> None:
    state = {
        "version": 2,
        "active_profile_id": "profile-a",
        "profiles": [
            _api_profile("profile-a", "First", "local-key-alpha", "paper-a"),
            _api_profile("profile-b", "Second", "local-key-beta", "paper-b"),
            _subscription_profile("subscription-a"),
        ],
    }
    saved = client.put("/api/config", json=state).json()

    assert saved["active_profile_id"] == "profile-a"
    assert [item.get("api_key", "") for item in saved["profiles"]] == [MASKED_API_KEY, MASKED_API_KEY, ""]
    assert client.get("/api/state").json()["config"] == saved
    assert "local-key-alpha" not in json.dumps(saved)
    assert "local-key-beta" not in json.dumps(saved)

    saved["profiles"][0].update({"base_url": "https://draft.example/v1", "model": "draft-model"})

    received: dict[str, object] = {}

    async def fake_call_ai(config, messages):
        received["config"] = config
        received["messages"] = messages
        return {"ok": True}

    monkeypatch.setattr(app_module, "call_ai", fake_call_ai)
    response = client.post("/api/config/test", json={"profile": saved["profiles"][0]})

    assert response.status_code == 200
    assert received["config"] == {
        "base_url": "https://draft.example/v1",
        "api_key": "local-key-alpha",
        "model": "draft-model",
    }
    persisted = client.get("/api/config").json()
    assert persisted["active_profile_id"] == "profile-a"
    assert persisted["profiles"][0]["model"] == "paper-a"


def test_ai_task_keeps_the_profile_captured_at_task_start(client, monkeypatch) -> None:
    client.put("/api/config", json={
        "version": 2,
        "active_profile_id": "first",
        "profiles": [
            _api_profile("first", "First", "first-local-key", "model-one"),
            _api_profile("second", "Second", "second-local-key", "model-two"),
        ],
    })
    selection = app_module.capture_ai_profile()
    client.post("/api/config/active", json={"profile_id": "second"})
    received: dict[str, object] = {}

    async def fake_call_ai(config, _messages):
        received["config"] = config
        return {"ok": True}

    monkeypatch.setattr(app_module, "call_ai", fake_call_ai)
    result = asyncio.run(app_module.call_configured_ai([], selection))

    assert result == {"ok": True}
    assert received["config"] == {
        "base_url": "https://api.example/v1",
        "api_key": "first-local-key",
        "model": "model-one",
    }


def test_masked_key_from_a_different_profile_cannot_be_reused(tmp_path) -> None:
    path = tmp_path / "config.json"
    original = {
        "version": 2,
        "active_profile_id": None,
        "profiles": [_api_profile("real-owner", "Owner", "owner-local-key")],
    }
    save_config(path, tmp_path, original)
    impostor = {
        "version": 2,
        "active_profile_id": None,
        "profiles": [_api_profile("new-profile", "Impostor", MASKED_API_KEY)],
    }

    with pytest.raises(ValueError, match="无法验证"):
        save_config(path, tmp_path, impostor)

    assert read_config(path)["profiles"][0]["api_key"] == "owner-local-key"


def test_activation_requires_complete_api_and_preserves_other_profiles(client) -> None:
    saved = client.put("/api/config", json={
        "version": 2,
        "active_profile_id": None,
        "profiles": [
            _api_profile("unfinished", "Work in progress", ""),
            _api_profile("ready", "Paper API", "valid-paper-key", "paper-model"),
            _subscription_profile("chatgpt"),
        ],
    }).json()
    assert saved["active_profile_id"] is None

    rejected = client.post("/api/config/active", json={"profile_id": "unfinished"})
    assert rejected.status_code == 400
    activated = client.post("/api/config/active", json={"profile_id": "ready"})

    assert activated.status_code == 200
    assert activated.json()["active_profile_id"] == "ready"
    assert [profile["id"] for profile in activated.json()["profiles"]] == ["unfinished", "ready", "chatgpt"]


def test_legacy_api_update_adds_profile_without_overwriting_subscription(client) -> None:
    state = {
        "version": 2,
        "active_profile_id": "chatgpt",
        "profiles": [_subscription_profile("chatgpt"), _api_profile("other-api", "Saved API", "kept-local-key", "saved")],
    }
    client.put("/api/config", json=state)

    result = client.put("/api/config", json={
        "base_url": "https://new.example/v1",
        "api_key": "new-api-key",
        "model": "new-model",
    })

    profiles = result.json()["profiles"]
    assert result.json()["active_profile_id"] not in {"chatgpt", "other-api"}
    assert next(profile for profile in profiles if profile["id"] == "chatgpt")["provider"] == "openai"
    assert next(profile for profile in profiles if profile["id"] == "other-api")["api_key"] == MASKED_API_KEY
    assert next(profile for profile in profiles if profile["id"] == result.json()["active_profile_id"])["model"] == "new-model"


def test_config_test_requires_profile_specific_api_fields_and_keeps_config_unmodified(client) -> None:
    response = client.post("/api/config/test", json={
        "profile": _api_profile("draft", "Missing fields", "", ""),
    })

    assert response.status_code == 400
    assert client.get("/api/config").json()["profiles"] == []


def test_subscription_provider_catalog_and_login_api_use_session_scoped_bridge(client, tmp_path, monkeypatch) -> None:
    class StubBridge:
        content = '{"ok":true}'

        async def invoke(self, method, **params):
            if method == "login.start":
                return {"session_id": params["session_id"], "status": "running"}
            if method == "login.status":
                return {
                    "session_id": params["session_id"],
                    "status": "running",
                    "next_event_id": 2,
                    "events": [
                        {"id": 1, "type": "auth_url", "url": "https://account.example/login"},
                        {"id": 2, "type": "device_code", "userCode": "LOCAL-CODE"},
                    ],
                }
            if method == "login.respond":
                return {"accepted": params["answer"] == "copied redirect URL"}
            if method == "login.cancel":
                return {"cancelled": True}
            if method == "auth.status":
                return {"authenticated": True}
            if method == "models.list":
                return [{"id": "subscription-model", "name": "Subscription Model"}]
            if method == "models.complete":
                return {"content": self.content}
            raise AssertionError(f"Unexpected adapter call: {method}")

    profile = _subscription_profile("chatgpt-profile", "openai")
    saved = client.put("/api/config", json={"version": 2, "active_profile_id": None, "profiles": [profile]})
    assert saved.status_code == 200
    auth_path = tmp_path / "data" / "model_auth.json"
    auth_path.write_text(json.dumps({"profiles": {"chatgpt-profile": {"openai": {"type": "oauth"}}}}), encoding="utf-8")
    bridge = StubBridge()
    monkeypatch.setitem(app_module._PI_BRIDGES, (tmp_path / "data").resolve(), bridge)

    providers = client.get("/api/model-providers").json()
    session = client.post("/api/model-configs/chatgpt-profile/auth/login", json={})
    assert session.status_code == 200, session.text
    status = client.get(f"/api/model-config-sessions/{session.json()['session_id']}?after_event_id=0")
    answer = client.post(
        f"/api/model-config-sessions/{session.json()['session_id']}/respond",
        json={"prompt_id": "pi-auth-prompt", "answer": "copied redirect URL"},
    )
    models = client.get("/api/model-configs/chatgpt-profile/models")

    assert [item["id"] for item in providers["providers"]] == ["openai", "anthropic", "github-copilot"]
    assert session.status_code == 200
    assert status.json()["events"][1]["userCode"] == "LOCAL-CODE"
    assert answer.json()["accepted"] is True
    cancelled = client.delete(f"/api/model-config-sessions/{session.json()['session_id']}")
    assert models.json()["models"][0]["id"] == "subscription-model"
    assert cancelled.json()["cancelled"] is True
    connection_profile = {**profile, "model": "subscription-model"}
    connection = client.post("/api/config/test", json={"profile": connection_profile})
    assert connection.status_code == 200
    bridge.content = "not JSON"
    invalid_response = client.post("/api/config/test", json={"profile": connection_profile})
    assert invalid_response.status_code == 502
    assert "响应格式错误" in invalid_response.json()["detail"]
    assert client.get("/api/config").json()["profiles"][0]["model"] == ""
    assert "LOCAL-CODE" not in json.dumps(client.get("/api/config").json())


def test_api_state_never_returns_local_subscription_credentials(client, tmp_path, monkeypatch) -> None:
    subscription = _subscription_profile("chatgpt-profile")
    auth_path = tmp_path / "data" / "model_auth.json"
    auth_path.parent.mkdir(parents=True)
    auth_path.write_text(json.dumps({"profiles": {"chatgpt-profile": {"openai": {
        "type": "oauth",
        "marker": "synthetic-credential-alpha",
        "expires": 999,
    }}}}), encoding="utf-8")
    client.put("/api/config", json={"version": 2, "active_profile_id": None, "profiles": [subscription]})

    serialized = json.dumps(client.get("/api/state").json())

    assert "synthetic-credential-alpha" not in serialized
    assert "access" not in serialized


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        ("HTTP 429 rate limit", "订阅额度或请求频率受限"),
        ("OAuth token expired", "重新登录"),
        ("model not found", "刷新目录"),
        ("request timed out", "超时"),
    ],
)
def test_subscription_failure_messages_offer_the_next_step(failure, expected) -> None:
    assert expected in app_module.describe_subscription_exception(RuntimeError(failure))


def test_pi_adapter_restarts_after_an_unexpected_process_exit(tmp_path) -> None:
    status = runtime_status(app_module.PI_BRIDGE_ROOT)
    if not status["available"]:
        pytest.skip(status["message"])
    bridge = PIModelBridge(app_module.PI_BRIDGE_ROOT, tmp_path)

    async def exercise() -> None:
        first = await bridge.invoke("runtime.status")
        assert first["version"] == "0.99.1"
        process = bridge.process
        reader = bridge.reader_task
        assert process is not None and reader is not None
        process.kill()
        await process.wait()
        await reader
        second = await bridge.invoke("runtime.status")
        assert second["version"] == "0.99.1"
        await bridge.close_async()

    asyncio.run(exercise())
