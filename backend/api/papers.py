from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, FastAPI

from backend.models import AssignTagsRequest, JournalLookupRequest, PaperUpdate
from backend.runtime import ApplicationRuntime
from backend.services import pdf_viewer

def register(app: FastAPI, runtime: ApplicationRuntime) -> None:
    router = APIRouter()

    @router.post("/api/papers/select")
    async def select_papers(background_tasks: BackgroundTasks) -> dict[str, Any]:
        files = [file for file in runtime.choose_pdf_files() if file.lower().endswith(".pdf")]
        added: list[str] = []
        skipped: list[str] = []
        for file in files:
            paper_id, created = runtime.upsert_paper(file)
            if created:
                added.append(paper_id)
                background_tasks.add_task(runtime.process_paper, paper_id, True)
            else:
                skipped.append(paper_id)
        return {"added": added, "skipped": skipped}

    @router.post("/api/papers/{paper_id}/reprocess")
    async def reprocess_paper(paper_id: str, background_tasks: BackgroundTasks) -> dict[str, str]:
        with runtime.connect() as conn:
            exists = conn.execute("SELECT 1 FROM papers WHERE id = ?", (paper_id,)).fetchone()
        if not exists:
            raise runtime.HTTPException(status_code=404, detail="文献不存在")
        background_tasks.add_task(runtime.process_paper, paper_id)
        return {"status": "queued"}

    @router.post("/api/papers/{paper_id}/lookup-partition")
    async def lookup_single_paper_partition(
        paper_id: str,
        payload: JournalLookupRequest | None = None,
    ) -> dict[str, Any]:
        selection = runtime.capture_ai_profile()
        try:
            result = await runtime.lookup_paper_partition(
                paper_id,
                payload.journal_name if payload else None,
                selection,
            )
            return result
        except KeyError as exc:
            raise runtime.HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise runtime.HTTPException(status_code=502, detail=f"期刊分区查询失败：{exc}") from exc

    @router.post("/api/papers/{paper_id}/translate-title")
    async def translate_paper_title(paper_id: str) -> dict[str, Any]:
        selection = runtime.capture_ai_profile()
        with runtime.connect() as conn:
            paper = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
        if not paper:
            raise runtime.HTTPException(status_code=404, detail="文献不存在")
        if not runtime.nullable_str(paper["title"]):
            raise runtime.HTTPException(status_code=400, detail="缺少原始标题，无法翻译")
        try:
            ai_data = await runtime.call_configured_ai(runtime.build_title_translation_prompt(paper), selection)
            title_zh = runtime.nullable_str(ai_data.get("title_zh"))
            with runtime.connect() as conn:
                conn.execute(
                    "UPDATE papers SET title_zh = ?, updated_at = ? WHERE id = ?",
                    (title_zh, runtime.now_iso(), paper_id),
                )
                conn.execute(
                    """
                    INSERT INTO ai_runs (id, paper_id, status, raw_response, created_at)
                    VALUES (?, ?, 'translate-title', ?, ?)
                    """,
                    (str(runtime.uuid.uuid4()), paper_id, runtime.json.dumps(ai_data, ensure_ascii=False)[:12000], runtime.now_iso()),
                )
                row = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
                return runtime.row_to_paper(row, conn)
        except Exception as exc:  # noqa: BLE001
            detail = runtime.describe_ai_exception("标题翻译失败", exc)
            runtime.record_ai_failure(paper_id, "translate-title-failed", detail)
            raise runtime.HTTPException(status_code=502, detail=detail) from exc

    @router.post("/api/papers/{paper_id}/generate-abstract")
    async def generate_paper_abstract(paper_id: str) -> dict[str, Any]:
        selection = runtime.capture_ai_profile()
        with runtime.connect() as conn:
            paper = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
        if not paper:
            raise runtime.HTTPException(status_code=404, detail="文献不存在")
        path = runtime.resolve_stored_path(paper["file_path"])
        if not path or not path.exists():
            raise runtime.HTTPException(status_code=404, detail="原 PDF 文件不存在或已移动")
        first_page_text = runtime.extract_first_page_text(path)
        if not first_page_text:
            raise runtime.HTTPException(status_code=400, detail="PDF 首页未提取到可用文本，无法生成摘要")
        try:
            ai_data = await runtime.call_configured_ai(runtime.build_abstract_prompt(paper, first_page_text), selection)
            abstract = runtime.nullable_str(ai_data.get("abstract"))
            with runtime.connect() as conn:
                conn.execute(
                    "UPDATE papers SET abstract = ?, updated_at = ? WHERE id = ?",
                    (abstract, runtime.now_iso(), paper_id),
                )
                conn.execute(
                    """
                    INSERT INTO ai_runs (id, paper_id, status, raw_response, created_at)
                    VALUES (?, ?, 'generate-abstract', ?, ?)
                    """,
                    (str(runtime.uuid.uuid4()), paper_id, runtime.json.dumps(ai_data, ensure_ascii=False)[:12000], runtime.now_iso()),
                )
                row = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
                return runtime.row_to_paper(row, conn)
        except Exception as exc:  # noqa: BLE001
            detail = runtime.describe_ai_exception("摘要生成失败", exc)
            runtime.record_ai_failure(paper_id, "generate-abstract-failed", detail)
            raise runtime.HTTPException(status_code=502, detail=detail) from exc

    @router.patch("/api/papers/{paper_id}")
    def update_paper(paper_id: str, payload: PaperUpdate) -> dict[str, Any]:
        with runtime.connect() as conn:
            conn.execute(
                """
                UPDATE papers SET title = ?, title_zh = ?, abstract = ?, notes = ?, journal_name = ?,
                  authors_json = ?, affiliations_json = ?,
                  publication_date = ?, doi_url = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    payload.title,
                    payload.title_zh,
                    payload.abstract,
                    payload.notes,
                    payload.journal_name,
                    runtime.json.dumps(payload.authors, ensure_ascii=False),
                    runtime.json.dumps(payload.affiliations, ensure_ascii=False),
                    payload.publication_date,
                    payload.doi_url,
                    runtime.now_iso(),
                    paper_id,
                ),
            )
            row = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
            if not row:
                raise runtime.HTTPException(status_code=404, detail="文献不存在")
            runtime.sync_year_tag(conn, paper_id, payload.publication_date)
            row = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
            return runtime.row_to_paper(row, conn)

    @router.post("/api/papers/{paper_id}/tags")
    def set_paper_tags(paper_id: str, payload: AssignTagsRequest) -> dict[str, Any]:
        with runtime.connect() as conn:
            conn.execute(
                """
                DELETE FROM paper_tags
                WHERE paper_id = ?
                  AND tag_id IN (SELECT id FROM tags WHERE kind = 'topic')
                """,
                (paper_id,),
            )
            runtime.assign_tags(conn, paper_id, payload.tag_ids[:5])
            row = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
            if not row:
                raise runtime.HTTPException(status_code=404, detail="文献不存在")
            return runtime.row_to_paper(row, conn)

    @router.delete("/api/papers/{paper_id}")
    def remove_paper(paper_id: str, delete_local_files: bool = False) -> dict[str, Any]:
        with runtime.connect() as conn:
            row = conn.execute("SELECT file_path FROM papers WHERE id = ?", (paper_id,)).fetchone()
            if not row:
                raise runtime.HTTPException(status_code=404, detail="文献不存在")
            file_path = str(runtime.resolve_stored_path(row["file_path"]) or row["file_path"])
            moved_files: list[str] = []
            missing_files: list[str] = []
            if delete_local_files:
                stored_paths = [row["file_path"]]
                stored_paths.extend(
                    attachment["file_path"]
                    for attachment in conn.execute(
                        "SELECT file_path FROM paper_attachments WHERE paper_id = ?", (paper_id,)
                    ).fetchall()
                )
                library_root = runtime.LIBRARY_DIR.resolve()
                paths: dict[str, Path] = {}
                for stored_path in stored_paths:
                    path = runtime.resolve_stored_path(stored_path)
                    if path is None:
                        raise runtime.HTTPException(status_code=409, detail="文件路径无效；可取消勾选，只移除文献记录")
                    try:
                        resolved = path.resolve()
                        resolved.relative_to(library_root)
                    except (OSError, RuntimeError, ValueError) as exc:
                        raise runtime.HTTPException(
                            status_code=409,
                            detail=f"文件不在文献库中，未删除任何文件：{path}。可取消勾选，只移除记录",
                        ) from exc
                    if path.is_symlink() or (path.exists() and not path.is_file()):
                        raise runtime.HTTPException(status_code=409, detail=f"文件路径不是普通文件：{path}")
                    paths[runtime.os.path.normcase(str(resolved))] = path

                references = conn.execute(
                    """
                    SELECT id AS paper_id, file_path FROM papers
                    UNION ALL
                    SELECT paper_id, file_path FROM paper_attachments
                    """
                ).fetchall()
                for reference in references:
                    if reference["paper_id"] == paper_id:
                        continue
                    other_path = runtime.resolve_stored_path(reference["file_path"])
                    if other_path is None:
                        continue
                    try:
                        other_key = runtime.os.path.normcase(str(other_path.resolve()))
                    except (OSError, RuntimeError):
                        continue
                    if other_key in paths:
                        raise runtime.HTTPException(
                            status_code=409,
                            detail=f"文件还被其他文献关联，未删除任何文件：{other_path}",
                        )

                for path in paths.values():
                    if not path.exists():
                        missing_files.append(str(path))
                        continue
                    try:
                        runtime.send2trash(str(path))
                    except OSError as exc:
                        moved = "、".join(runtime.Path(value).name for value in moved_files) or "无"
                        raise runtime.HTTPException(
                            status_code=500,
                            detail=(
                                f"移入回收站失败：{path.name}（{exc}）。已移入：{moved}；"
                                "文献记录仍保留，可检查后重试"
                            ),
                        ) from exc
                    moved_files.append(str(path))
            conn.execute("DELETE FROM papers WHERE id = ?", (paper_id,))
            conn.execute("UPDATE tags SET use_count = (SELECT COUNT(*) FROM paper_tags WHERE tag_id = tags.id)")
        return {
            "status": "removed",
            "file_path": file_path,
            "moved_files": moved_files,
            "missing_files": missing_files,
        }

    @router.post("/api/papers/{paper_id}/open")
    def open_paper(paper_id: str) -> dict[str, str]:
        with runtime.connect() as conn:
            row = conn.execute("SELECT file_path FROM papers WHERE id = ?", (paper_id,)).fetchone()
        if not row:
            raise runtime.HTTPException(status_code=404, detail="文献不存在")
        path = runtime.resolve_stored_path(row["file_path"])
        if not path or not path.exists():
            raise runtime.HTTPException(status_code=404, detail="原 PDF 文件不存在或已移动")
        try:
            message = pdf_viewer.open_pdf(path, runtime.PDF_VIEWER_CONFIG_PATH, runtime.platform.system())
        except OSError as exc:
            raise runtime.HTTPException(status_code=500, detail=f"无法打开 PDF：{exc}") from exc
        except RuntimeError as exc:
            raise runtime.HTTPException(status_code=500, detail=str(exc)) from exc
        return {"status": "opened", "message": message}

    @router.post("/api/papers/{paper_id}/open-folder")
    def open_paper_folder(paper_id: str) -> dict[str, str]:
        with runtime.connect() as conn:
            row = conn.execute("SELECT file_path FROM papers WHERE id = ?", (paper_id,)).fetchone()
        if not row:
            raise runtime.HTTPException(status_code=404, detail="文献不存在")
        path = runtime.resolve_stored_path(row["file_path"])
        if not path or not path.exists():
            raise runtime.HTTPException(status_code=404, detail="原 PDF 文件不存在或已移动")
        runtime.open_local_folder_for_file(path)
        return {"status": "opened", "message": "已打开 PDF 所在文件夹。"}

    @router.post("/api/papers/{paper_id}/relink-file")
    def relink_paper_file(paper_id: str) -> Any:
        with runtime.connect() as conn:
            row = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
        if not row:
            raise runtime.HTTPException(status_code=404, detail="文献不存在")
        files = [file for file in runtime.choose_pdf_files() if file.lower().endswith(".pdf")]
        if not files:
            raise runtime.HTTPException(status_code=400, detail="未选择 PDF 文件")
        path = runtime.Path(files[0]).resolve()
        if not path.exists():
            raise runtime.HTTPException(status_code=404, detail="选择的 PDF 文件不存在")
        with runtime.connect() as conn:
            duplicate = conn.execute(
                "SELECT id FROM papers WHERE (file_path = ? OR file_path = ?) AND id <> ?",
                (runtime.to_stored_path(path), str(path), paper_id),
            ).fetchone()
            if duplicate:
                raise runtime.HTTPException(status_code=409, detail="这个 PDF 已经关联到另一条文献记录")
            paper = conn.execute("SELECT title FROM papers WHERE id = ?", (paper_id,)).fetchone()
            target = runtime.library_target_for_main(path, paper["title"] if paper else None)
            if target and not runtime._same_path(path, target):
                if target.exists() or runtime.path_in_library(conn, target, exclude_paper_id=paper_id):
                    conflict = runtime.create_file_conflict(
                        conn,
                        paper_id=paper_id,
                        kind="main",
                        source_path=path,
                        target_path=target,
                        operation="set_main",
                    )
                    return runtime.JSONResponse(status_code=409, content=conflict)
                path = runtime.move_file_to_target(path, target)
            runtime.update_paper_file_path(conn, paper_id, path)
            updated = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
            return runtime.row_to_paper(updated, conn)

    @router.post("/api/papers/{paper_id}/attachments/select")
    def select_paper_attachments(paper_id: str) -> Any:
        with runtime.connect() as conn:
            paper = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
            if not paper:
                raise runtime.HTTPException(status_code=404, detail="文献不存在")
        files = runtime.choose_attachment_files()
        if not files:
            raise runtime.HTTPException(status_code=400, detail="未选择补充文件")
        with runtime.connect() as conn:
            paper = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
            operations: list[tuple[runtime.Path, runtime.Path]] = []
            for file in files:
                source = runtime.Path(file).resolve()
                if not source.exists() or not source.is_file():
                    continue
                target = runtime.library_target_for_attachment(source, paper["title"] if paper else None)
                final_path = target
                if not runtime._same_path(source, target):
                    if target.exists() or runtime.path_in_library(conn, target):
                        conflict = runtime.create_file_conflict(
                            conn,
                            paper_id=paper_id,
                            kind="attachment",
                            source_path=source,
                            target_path=target,
                            operation="add_attachment",
                        )
                        return runtime.JSONResponse(status_code=409, content=conflict)
                elif runtime.path_in_library(conn, final_path):
                    raise runtime.HTTPException(status_code=409, detail="这个文件已经在文献管理器中存在")
                operations.append((source, final_path))
            for source, final_path in operations:
                final_path = runtime.move_file_to_target(source, final_path)
                runtime.insert_attachment(conn, paper_id, final_path)
            row = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
            return runtime.row_to_paper(row, conn)

    @router.delete("/api/papers/{paper_id}/attachments/{attachment_id}")
    def remove_paper_attachment(paper_id: str, attachment_id: str) -> dict[str, Any]:
        with runtime.connect() as conn:
            attachment = conn.execute(
                "SELECT * FROM paper_attachments WHERE id = ? AND paper_id = ?",
                (attachment_id, paper_id),
            ).fetchone()
            if not attachment:
                raise runtime.HTTPException(status_code=404, detail="补充文件关联不存在")
            conn.execute("DELETE FROM paper_attachments WHERE id = ?", (attachment_id,))
            row = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
            return runtime.row_to_paper(row, conn)

    @router.post("/api/papers/{paper_id}/attachments/{attachment_id}/open")
    def open_paper_attachment(paper_id: str, attachment_id: str) -> dict[str, str]:
        with runtime.connect() as conn:
            attachment = conn.execute(
                "SELECT * FROM paper_attachments WHERE id = ? AND paper_id = ?",
                (attachment_id, paper_id),
            ).fetchone()
        if not attachment:
            raise runtime.HTTPException(status_code=404, detail="补充文件关联不存在")
        path = runtime.resolve_stored_path(attachment["file_path"])
        if not path or not path.exists():
            raise runtime.HTTPException(status_code=404, detail="补充文件不存在或已移动")
        try:
            if path.suffix.casefold() == ".pdf":
                message = pdf_viewer.open_pdf(path, runtime.PDF_VIEWER_CONFIG_PATH, runtime.platform.system())
            else:
                runtime.open_local_file(path)
                message = "已打开补充文件。"
        except (OSError, RuntimeError) as exc:
            raise runtime.HTTPException(status_code=500, detail=f"无法打开补充文件：{exc}") from exc
        return {"status": "opened", "message": message}

    @router.post("/api/papers/{paper_id}/attachments/{attachment_id}/open-folder")
    def open_paper_attachment_folder(paper_id: str, attachment_id: str) -> dict[str, str]:
        with runtime.connect() as conn:
            attachment = conn.execute(
                "SELECT * FROM paper_attachments WHERE id = ? AND paper_id = ?",
                (attachment_id, paper_id),
            ).fetchone()
        if not attachment:
            raise runtime.HTTPException(status_code=404, detail="补充文件关联不存在")
        path = runtime.resolve_stored_path(attachment["file_path"])
        if not path or not path.exists():
            raise runtime.HTTPException(status_code=404, detail="补充文件不存在或已移动")
        runtime.open_local_folder_for_file(path)
        return {"status": "opened", "message": "已打开补充文件所在文件夹。"}


    app.include_router(router)
