from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, FastAPI

from backend.model_configs import SUBSCRIPTION_PROVIDERS, get_profile, is_profile_ready, profile_draft_to_api_config, public_config
from backend.models import ApiConfig, ModelConfigState, ModelLoginReply, ModelProfile
from backend.runtime import ApplicationRuntime
from backend.services.pi_bridge import runtime_status as pi_runtime_status


def register(app: FastAPI, runtime: ApplicationRuntime) -> None:
    router = APIRouter()

    def subscription_profile(profile_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        data = runtime.read_model_config(False)
        profile = get_profile(data, profile_id)
        if not profile:
            raise runtime.HTTPException(status_code=404, detail="模型配置不存在，请刷新页面后重试。")
        if profile.get("kind") != "subscription" or profile.get("provider") not in SUBSCRIPTION_PROVIDERS:
            raise runtime.HTTPException(status_code=400, detail="此配置没有使用订阅接入。")
        return data, profile

    @router.get("/api/config")
    def get_config() -> dict[str, Any]:
        return runtime.read_model_config_public(mask_key=True)

    @router.put("/api/config")
    async def set_config(payload: dict[str, Any]) -> dict[str, Any]:
        if "version" not in payload:
            try:
                runtime.write_config(ApiConfig.model_validate(payload))
                data = runtime.read_model_config(False)
            except (ValueError, TypeError) as exc:
                raise runtime.HTTPException(status_code=400, detail=str(exc)) from exc
            return public_config(data, mask_keys=True)

        try:
            ModelConfigState.model_validate(payload)
            before = runtime.read_model_config(False)
            saved = runtime.write_model_config(payload)
        except (ValueError, TypeError) as exc:
            raise runtime.HTTPException(status_code=400, detail=str(exc)) from exc

        old_subscriptions = {profile["id"]: profile for profile in before["profiles"] if profile.get("kind") == "subscription"}
        new_subscriptions = {profile["id"]: profile for profile in saved["profiles"] if profile.get("kind") == "subscription"}
        obsolete_ids = {
            profile_id
            for profile_id, profile in old_subscriptions.items()
            if profile_id not in new_subscriptions or profile.get("provider") != new_subscriptions[profile_id].get("provider")
        }
        auth_path = runtime.CONFIG_PATH.parent / "model_auth.json"
        try:
            if obsolete_ids and auth_path.exists():
                bridge_status = pi_runtime_status(runtime.PI_BRIDGE_ROOT)
                if not bridge_status["available"]:
                    raise RuntimeError("安装或修复 Node.js 订阅适配依赖后，才能安全删除此订阅配置。")
                for profile_id in obsolete_ids:
                    await runtime.get_pi_bridge().invoke("auth.remove-profile", profile_id=profile_id)
        except Exception as exc:  # noqa: BLE001
            try:
                runtime.write_model_config(before)
            except Exception as rollback_error:  # noqa: BLE001
                raise runtime.HTTPException(
                    status_code=500,
                    detail="订阅密钥清理失败，配置回滚也失败；请重启应用并检查本机模型配置。",
                ) from rollback_error
            raise runtime.HTTPException(status_code=502, detail=f"无法安全删除订阅配置：{exc}") from exc
        return public_config(saved, mask_keys=True)

    @router.post("/api/config/active")
    async def activate_model_config(payload: dict[str, Any]) -> dict[str, Any]:
        profile_id = str(payload.get("profile_id") or "").strip()
        data = runtime.read_model_config(False)
        profile = get_profile(data, profile_id)
        if profile is None:
            raise runtime.HTTPException(status_code=404, detail="模型配置不存在，请重新选择。")
        if not is_profile_ready(profile, runtime.CONFIG_PATH.parent / "model_auth.json"):
            detail = (
                "请填写 Base URL、API Key 和模型标识后再启用。"
                if profile.get("kind") == "api"
                else "请先登录订阅服务商并选择模型后再启用。"
            )
            raise runtime.HTTPException(status_code=400, detail=detail)
        data["active_profile_id"] = profile_id
        return public_config(runtime.write_model_config(data), mask_keys=True)

    @router.get("/api/model-providers")
    def get_model_providers() -> dict[str, Any]:
        return {
            "runtime": pi_runtime_status(runtime.PI_BRIDGE_ROOT),
            "providers": [
                {"id": "openai", "name": "ChatGPT 订阅"},
                {"id": "anthropic", "name": "Claude 订阅"},
                {"id": "github-copilot", "name": "GitHub Copilot"},
            ],
        }

    @router.post("/api/config/test")
    async def test_config_connection(payload: dict[str, Any]) -> dict[str, str]:
        state = runtime.read_model_config(False)
        try:
            raw_profile = payload.get("profile")
            if isinstance(raw_profile, dict):
                profile = ModelProfile.model_validate(raw_profile).model_dump()
            else:
                legacy = ApiConfig.model_validate(payload)
                active = get_profile(state, str(state.get("active_profile_id") or ""))
                if active and active.get("kind") == "api":
                    profile = {**active, **legacy.model_dump()}
                else:
                    profile = {
                        "id": f"connection-test-{uuid.uuid4()}",
                        "name": "连接测试",
                        "kind": "api",
                        "provider": "openai-compatible",
                        **legacy.model_dump(),
                    }
        except (ValueError, TypeError) as exc:
            raise runtime.HTTPException(status_code=400, detail=f"模型配置格式无效：{exc}") from exc

        profile["api_key"] = profile_draft_to_api_config(profile, state).get("api_key", "")
        if profile["kind"] == "api":
            data = {field: str(profile.get(field) or "") for field in ("base_url", "api_key", "model")}
            if not all(value.strip() for value in data.values()):
                raise runtime.HTTPException(status_code=400, detail="请填写 API Base URL、API Key 和模型标识。")
            try:
                await runtime.call_ai(
                    data,
                    [
                        {"role": "system", "content": "你是连接测试助手，只返回合法 JSON 对象。"},
                        {"role": "user", "content": '{"task":"connection_test","reply":{"ok":true}}'},
                    ],
                )
            except Exception as exc:  # noqa: BLE001
                raise runtime.HTTPException(
                    status_code=502,
                    detail=runtime.describe_ai_exception("AI 连接测试失败", exc),
                ) from exc
            return {"status": "ok", "message": "API 模型连接成功。"}

        if profile.get("provider") not in SUBSCRIPTION_PROVIDERS:
            raise runtime.HTTPException(status_code=400, detail="请选择有效的订阅服务商。")
        saved_profile = get_profile(state, str(profile["id"]))
        if not saved_profile or saved_profile.get("kind") != "subscription" or saved_profile.get("provider") != profile["provider"]:
            raise runtime.HTTPException(status_code=400, detail="请先保存此订阅配置并完成登录，再测试连接。")
        profile["model"] = str(profile.get("model") or "").strip()
        if not profile["model"] or not is_profile_ready(profile, runtime.CONFIG_PATH.parent / "model_auth.json"):
            raise runtime.HTTPException(status_code=400, detail="请先完成服务商登录，并选择一个可用模型。")
        try:
            result = await runtime.get_pi_bridge().invoke(
                "models.complete",
                profile_id=profile["id"],
                provider=profile["provider"],
                model=profile["model"],
                messages=[
                    {"role": "system", "content": "你是连接测试助手，只返回合法 JSON 对象。"},
                    {"role": "user", "content": '{"task":"connection_test","reply":{"ok":true}}'},
                ],
            )
            try:
                runtime.parse_ai_json(str(result.get("content") or ""))
            except (TypeError, ValueError) as parse_error:
                raise RuntimeError(f"订阅模型响应不是合法 JSON：{parse_error}") from parse_error
        except Exception as exc:  # noqa: BLE001
            raise runtime.HTTPException(
                status_code=502,
                detail=f"订阅模型连接测试失败：{runtime.describe_subscription_exception(exc)}",
            ) from exc
        return {"status": "ok", "message": "订阅模型连接成功。"}

    @router.get("/api/model-configs/{profile_id}/models")
    async def list_profile_models(profile_id: str) -> dict[str, list[dict[str, str]]]:
        _, profile = subscription_profile(profile_id)
        auth_path = runtime.CONFIG_PATH.parent / "model_auth.json"
        if not is_profile_ready(profile, auth_path):
            try:
                auth = await runtime.get_pi_bridge().invoke(
                    "auth.status", profile_id=profile_id, provider=profile["provider"],
                )
            except Exception as exc:  # noqa: BLE001
                raise runtime.HTTPException(status_code=503, detail=str(exc)) from exc
            if not auth.get("authenticated"):
                raise runtime.HTTPException(status_code=401, detail="请先登录此订阅服务商，然后刷新模型目录。")
        try:
            models = await runtime.get_pi_bridge().invoke("models.list", profile_id=profile_id, provider=profile["provider"])
        except Exception as exc:  # noqa: BLE001
            raise runtime.HTTPException(status_code=502, detail=f"无法读取订阅模型目录：{exc}") from exc
        return {"models": models}

    @router.get("/api/model-configs/{profile_id}/auth")
    async def get_profile_auth(profile_id: str) -> dict[str, Any]:
        _, profile = subscription_profile(profile_id)
        status = pi_runtime_status(runtime.PI_BRIDGE_ROOT)
        if not status["available"]:
            return {"authenticated": is_profile_ready(profile, runtime.CONFIG_PATH.parent / "model_auth.json"), "runtime": status}
        try:
            auth = await runtime.get_pi_bridge().invoke("auth.status", profile_id=profile_id, provider=profile["provider"])
            return {**auth, "runtime": status}
        except Exception as exc:  # noqa: BLE001
            raise runtime.HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post("/api/model-configs/{profile_id}/auth/login")
    async def start_profile_login(profile_id: str) -> dict[str, Any]:
        _, profile = subscription_profile(profile_id)
        status = pi_runtime_status(runtime.PI_BRIDGE_ROOT)
        if not status["available"]:
            raise runtime.HTTPException(status_code=503, detail=status["message"])
        session_id = str(uuid.uuid4())
        try:
            return await runtime.get_pi_bridge().invoke(
                "login.start", profile_id=profile_id, provider=profile["provider"], session_id=session_id,
            )
        except Exception as exc:  # noqa: BLE001
            raise runtime.HTTPException(status_code=502, detail=f"无法开始订阅登录：{exc}") from exc

    @router.get("/api/model-config-sessions/{session_id}")
    async def read_profile_login_session(session_id: str, after_event_id: int = 0) -> dict[str, Any]:
        if after_event_id < 0:
            raise runtime.HTTPException(status_code=400, detail="事件游标不能小于零。")
        try:
            return await runtime.get_pi_bridge().invoke("login.status", session_id=session_id, after_event_id=after_event_id)
        except Exception as exc:  # noqa: BLE001
            raise runtime.HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post("/api/model-config-sessions/{session_id}/respond")
    async def respond_profile_login_session(session_id: str, payload: ModelLoginReply) -> dict[str, Any]:
        try:
            return await runtime.get_pi_bridge().invoke(
                "login.respond", session_id=session_id, prompt_id=payload.prompt_id, answer=payload.answer,
            )
        except Exception as exc:  # noqa: BLE001
            raise runtime.HTTPException(status_code=400, detail=str(exc)) from exc

    @router.delete("/api/model-config-sessions/{session_id}")
    async def cancel_profile_login_session(session_id: str) -> dict[str, Any]:
        try:
            return await runtime.get_pi_bridge().invoke("login.cancel", session_id=session_id)
        except Exception as exc:  # noqa: BLE001
            raise runtime.HTTPException(status_code=503, detail=str(exc)) from exc

    @router.delete("/api/model-configs/{profile_id}/auth")
    async def logout_profile(profile_id: str) -> dict[str, str]:
        _, profile = subscription_profile(profile_id)
        try:
            await runtime.get_pi_bridge().invoke("auth.logout", profile_id=profile_id, provider=profile["provider"])
            data = runtime.read_model_config(False)
            if data.get("active_profile_id") == profile_id:
                data["active_profile_id"] = None
                runtime.write_model_config(data)
            return {"status": "ok", "message": "已退出此订阅账号。"}
        except Exception as exc:  # noqa: BLE001
            raise runtime.HTTPException(status_code=502, detail=f"退出订阅账号失败：{exc}") from exc

    app.include_router(router)
