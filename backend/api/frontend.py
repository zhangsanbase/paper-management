from __future__ import annotations

from fastapi import APIRouter, FastAPI
from fastapi.responses import FileResponse

from backend.runtime import ApplicationRuntime

def register(app: FastAPI, runtime: ApplicationRuntime) -> None:
    router = APIRouter()

    if runtime.DIST_DIR.exists():
        app.mount("/assets", runtime.StaticFiles(directory=runtime.DIST_DIR / "assets"), name="assets")

        @router.get("/{path:path}")
        def serve_frontend(path: str) -> FileResponse:
            target = runtime.DIST_DIR / path
            if path and target.exists() and target.is_file():
                return runtime.FileResponse(target)
            return runtime.FileResponse(runtime.DIST_DIR / "index.html")


    app.include_router(router)
