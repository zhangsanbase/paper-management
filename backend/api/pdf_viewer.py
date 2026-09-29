from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, FastAPI
from pydantic import BaseModel

from backend.runtime import ApplicationRuntime
from backend.services import pdf_viewer


class ViewerUpdate(BaseModel):
    mode: Literal["system", "custom"]
    executable_path: str | None = None


def register(app: FastAPI, runtime: ApplicationRuntime) -> None:
    router = APIRouter()

    @router.get("/api/pdf-viewer")
    def get_pdf_viewer() -> pdf_viewer.ViewerState:
        return pdf_viewer.read_settings(runtime.PDF_VIEWER_CONFIG_PATH, runtime.platform.system())

    @router.put("/api/pdf-viewer")
    def put_pdf_viewer(payload: ViewerUpdate) -> pdf_viewer.ViewerState:
        try:
            return pdf_viewer.save_settings(
                runtime.PDF_VIEWER_CONFIG_PATH,
                payload.mode,
                payload.executable_path,
                runtime.platform.system(),
            )
        except ValueError as exc:
            raise runtime.HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/api/pdf-viewer/select")
    def select_pdf_viewer() -> dict[str, str | bool | None]:
        if runtime.platform.system() != "Windows":
            raise runtime.HTTPException(status_code=400, detail="当前系统暂不支持自选 PDF 阅读器。")
        files = runtime.choose_local_files("选择 PDF 阅读器", [("Windows 程序", "*.exe")], multiple=False)
        if not files:
            return {"cancelled": True, "path": None}
        return {"cancelled": False, "path": files[0]}

    app.include_router(router)
