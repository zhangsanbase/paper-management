from __future__ import annotations

from typing import Any

from fastapi import APIRouter, FastAPI

from backend.runtime import ApplicationRuntime

def register(app: FastAPI, runtime: ApplicationRuntime) -> None:
    router = APIRouter()

    @router.get("/api/library")
    def get_library() -> dict[str, str]:
        return runtime.library_status()

    @router.post("/api/library/open")
    def open_library() -> dict[str, str]:
        runtime.open_local_directory(runtime.ensure_library_dir())
        return {"status": "opened", "message": "已打开文件库文件夹。"}

    @router.post("/api/library/migrate")
    def migrate_library() -> Any:
        with runtime.connect() as conn:
            result = runtime.migrate_files_to_library(conn)
        if result.get("status") == "conflict":
            return runtime.JSONResponse(status_code=409, content=result["conflict"])
        return result


    app.include_router(router)
