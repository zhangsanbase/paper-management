from __future__ import annotations

from fastapi import APIRouter, FastAPI

from backend.models import ApiConfig
from backend.runtime import ApplicationRuntime

def register(app: FastAPI, runtime: ApplicationRuntime) -> None:
    router = APIRouter()

    @router.get("/api/config")
    def get_config() -> dict[str, str]:
        return runtime.read_config(mask_key=True)

    @router.post("/api/config/test")
    async def test_config_connection(payload: ApiConfig) -> dict[str, str]:
        data = payload.model_dump()
        current = runtime.read_config(mask_key=False)
        if data["api_key"] == "********":
            data["api_key"] = current.get("api_key", "")
        if not data["base_url"].strip() or not data["api_key"].strip() or not data["model"].strip():
            raise runtime.HTTPException(status_code=400, detail="请填写 Base URL、API Key 和 Model。")
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
        return {"status": "ok", "message": "AI 连接成功。"}

    @router.put("/api/config")
    def set_config(payload: ApiConfig) -> dict[str, str]:
        current = runtime.read_config(mask_key=False)
        data = payload.model_dump()
        if data["api_key"] == "********":
            data["api_key"] = current.get("api_key", "")
        runtime.write_config(runtime.ApiConfig(**data))
        return runtime.read_config(mask_key=True)


    app.include_router(router)
