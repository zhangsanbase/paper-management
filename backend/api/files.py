from __future__ import annotations

from typing import Any

from fastapi import APIRouter, FastAPI

from backend.models import FileConflictResolve
from backend.runtime import ApplicationRuntime

def register(app: FastAPI, runtime: ApplicationRuntime) -> None:
    router = APIRouter()

    @router.post("/api/file-conflicts/{conflict_id}/open-folder")
    def open_file_conflict_folder(conflict_id: str) -> dict[str, str]:
        runtime.cleanup_file_conflicts()
        conflict = runtime.FILE_CONFLICTS.get(conflict_id)
        if not conflict:
            raise runtime.HTTPException(status_code=404, detail="文件冲突已失效，请重新执行刚才的操作")
        target = runtime.Path(conflict["target_path"])
        runtime.open_local_folder_for_file(target)
        return {"status": "opened", "message": "已打开同名文件所在文件夹。"}

    @router.post("/api/file-conflicts/{conflict_id}/resolve")
    def resolve_file_conflict(conflict_id: str, payload: FileConflictResolve) -> dict[str, Any]:
        runtime.cleanup_file_conflicts()
        conflict = runtime.FILE_CONFLICTS.get(conflict_id)
        if not conflict:
            raise runtime.HTTPException(status_code=404, detail="文件冲突已失效，请重新执行刚才的操作")
        action = payload.action
        if action not in {"auto_number", "use_existing", "cancel"}:
            raise runtime.HTTPException(status_code=400, detail="未知的冲突处理方式")
        paper_id = str(conflict["paper_id"])
        kind = str(conflict["kind"])
        operation = str(conflict.get("operation") or "")
        attachment_id = runtime.nullable_str(conflict.get("attachment_id"))
        source = runtime.Path(str(conflict["source_path"]))
        target = runtime.Path(str(conflict["target_path"]))
        if action == "cancel":
            runtime.FILE_CONFLICTS.pop(conflict_id, None)
            return {"status": "cancelled", "message": "已取消，本地文件和管理器记录均未改变。"}
        with runtime.connect() as conn:
            if not conn.execute("SELECT 1 FROM papers WHERE id = ?", (paper_id,)).fetchone():
                runtime.FILE_CONFLICTS.pop(conflict_id, None)
                raise runtime.HTTPException(status_code=404, detail="文献不存在")
            if action == "use_existing":
                if not target.exists():
                    raise runtime.HTTPException(status_code=404, detail="同名文件不存在，无法使用已有文件")
                if runtime.path_in_library(
                    conn,
                    target,
                    exclude_paper_id=paper_id if kind == "main" else None,
                    exclude_attachment_id=attachment_id,
                ):
                    raise runtime.HTTPException(status_code=409, detail="该文件已在文献管理器中存在")
                if kind == "main":
                    runtime.update_paper_file_path(conn, paper_id, target)
                elif operation == "migrate_attachment" and attachment_id:
                    runtime.update_attachment_file_path(conn, attachment_id, target)
                else:
                    runtime.insert_attachment(conn, paper_id, target)
            else:
                if not source.exists():
                    raise runtime.HTTPException(status_code=404, detail="待处理文件不存在，无法自动编号")
                stem = target.stem
                extension = target.suffix
                numbered = runtime.unique_named_path(
                    target.parent,
                    stem,
                    extension,
                    source,
                    conn,
                    exclude_paper_id=paper_id if kind == "main" else None,
                    exclude_attachment_id=attachment_id,
                )
                numbered = runtime.move_file_to_target(source, numbered)
                if kind == "main":
                    runtime.update_paper_file_path(conn, paper_id, numbered)
                elif operation == "migrate_attachment" and attachment_id:
                    runtime.update_attachment_file_path(conn, attachment_id, numbered)
                else:
                    runtime.insert_attachment(conn, paper_id, numbered)
            runtime.FILE_CONFLICTS.pop(conflict_id, None)
            row = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
            return runtime.row_to_paper(row, conn)


    app.include_router(router)
