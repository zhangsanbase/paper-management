from __future__ import annotations

from fastapi import APIRouter, FastAPI
from fastapi.responses import FileResponse

from backend.runtime import ApplicationRuntime

# Methods the unknown-API handlers answer on. CORS preflight never reaches them:
# CORSMiddleware replies to preflight before routing.
API_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def register(app: FastAPI, runtime: ApplicationRuntime) -> None:
    router = APIRouter()

    # Unknown /api paths must answer 404 with JSON. Without these they fall through
    # to the SPA catch-all below and get 200 + index.html, so a mistyped or missing
    # endpoint looks successful and the caller fails on response.json() with an
    # opaque parse error instead of a clear "no such endpoint".
    #
    # Both handlers are registered before the catch-all, and this whole router is
    # included after every real API router, so only genuinely unmatched paths land
    # here. They sit outside the DIST_DIR check on purpose: a missing frontend build
    # should not turn API mistakes into silent HTML responses.
    @router.api_route("/api", methods=API_METHODS, include_in_schema=False)
    def unknown_api_root() -> None:
        raise runtime.HTTPException(status_code=404, detail="未知接口：/api")

    # "/api/{rest:path}" does not match a bare "/api" because of the missing slash.
    @router.api_route("/api/{rest:path}", methods=API_METHODS, include_in_schema=False)
    def unknown_api(rest: str) -> None:
        raise runtime.HTTPException(status_code=404, detail=f"未知接口：/api/{rest}")

    if runtime.DIST_DIR.exists():
        app.mount("/assets", runtime.StaticFiles(directory=runtime.DIST_DIR / "assets"), name="assets")

        @router.get("/{path:path}")
        def serve_frontend(path: str) -> FileResponse:
            target = runtime.DIST_DIR / path
            if path and target.exists() and target.is_file():
                return runtime.FileResponse(target)
            return runtime.FileResponse(runtime.DIST_DIR / "index.html")


    app.include_router(router)
