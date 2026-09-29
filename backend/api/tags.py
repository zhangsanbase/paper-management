from __future__ import annotations

from typing import Any

from fastapi import APIRouter, FastAPI

from backend.models import TagCreate, TagUpdate
from backend.runtime import ApplicationRuntime

def register(app: FastAPI, runtime: ApplicationRuntime) -> None:
    router = APIRouter()

    @router.post("/api/tags")
    def create_tag(payload: TagCreate) -> dict[str, Any]:
        name = payload.name.strip()
        if not name:
            raise runtime.HTTPException(status_code=400, detail="标签名不能为空")
        if runtime.is_method_tag(name):
            raise runtime.HTTPException(status_code=400, detail="常规测试/表征方法不应作为主标签")
        aliases = runtime.normalize_aliases(name, payload.aliases)
        with runtime.connect() as conn:
            similar = runtime.find_similar_tag(conn, name, "topic")
            if similar:
                row = conn.execute("SELECT * FROM tags WHERE id = ?", (similar,)).fetchone()
                return runtime.row_to_tag(row)
            tag_id = str(runtime.uuid.uuid4())
            conn.execute(
                """
                INSERT INTO tags (id, name, aliases_json, description, kind, created_at)
                VALUES (?, ?, ?, ?, 'topic', ?)
                """,
                (
                    tag_id,
                    name,
                    runtime.json.dumps(aliases, ensure_ascii=False),
                    payload.description,
                    runtime.now_iso(),
                ),
            )
            row = conn.execute("SELECT * FROM tags WHERE id = ?", (tag_id,)).fetchone()
            return runtime.row_to_tag(row)

    @router.put("/api/tags/{tag_id}")
    def update_tag(tag_id: str, payload: TagUpdate) -> dict[str, Any]:
        if runtime.is_method_tag(payload.name):
            raise runtime.HTTPException(status_code=400, detail="常规测试/表征方法不应作为主标签")
        with runtime.connect() as conn:
            tag = conn.execute("SELECT kind FROM tags WHERE id = ?", (tag_id,)).fetchone()
            if not tag:
                raise runtime.HTTPException(status_code=404, detail="标签不存在")
            if tag["kind"] == "year":
                raise runtime.HTTPException(status_code=400, detail="年份标签由系统自动维护，不能手动编辑")
            conn.execute(
                """
                UPDATE tags SET name = ?, aliases_json = ?, description = ?
                WHERE id = ?
                """,
                (
                    payload.name.strip(),
                    runtime.json.dumps(payload.aliases, ensure_ascii=False),
                    payload.description,
                    tag_id,
                ),
            )
            row = conn.execute("SELECT * FROM tags WHERE id = ?", (tag_id,)).fetchone()
            if not row:
                raise runtime.HTTPException(status_code=404, detail="标签不存在")
            return runtime.row_to_tag(row)

    @router.delete("/api/tags/{tag_id}")
    def delete_tag(tag_id: str) -> dict[str, Any]:
        with runtime.connect() as conn:
            try:
                tag = runtime.remove_topic_tag(conn, tag_id)
            except KeyError as exc:
                raise runtime.HTTPException(status_code=404, detail="标签不存在") from exc
            except ValueError as exc:
                raise runtime.HTTPException(status_code=400, detail=str(exc)) from exc
        return {"status": "removed", "tag": tag}

    @router.post("/api/suggestions/{suggestion_id}/approve")
    def approve_suggestion(suggestion_id: str) -> dict[str, Any]:
        with runtime.connect() as conn:
            suggestion = conn.execute(
                "SELECT * FROM tag_suggestions WHERE id = ?",
                (suggestion_id,),
            ).fetchone()
            if not suggestion:
                raise runtime.HTTPException(status_code=404, detail="建议不存在")
            name = suggestion["name"].strip()
            if runtime.is_method_tag(name):
                conn.execute("UPDATE tag_suggestions SET status = 'rejected' WHERE id = ?", (suggestion_id,))
                raise runtime.HTTPException(status_code=400, detail="常规测试/表征方法不应作为主标签")
            tag_id = runtime.find_similar_tag(conn, name, "topic")
            if not tag_id:
                tag_id = str(runtime.uuid.uuid4())
                conn.execute(
                    """
                    INSERT INTO tags (id, name, aliases_json, description, kind, created_at)
                    VALUES (?, ?, ?, ?, 'topic', ?)
                    """,
                    (tag_id, name, runtime.json.dumps([name], ensure_ascii=False), "", runtime.now_iso()),
                )
            runtime.assign_tags(conn, suggestion["paper_id"], [tag_id])
            conn.execute("UPDATE tag_suggestions SET status = 'approved' WHERE id = ?", (suggestion_id,))
            row = conn.execute("SELECT * FROM papers WHERE id = ?", (suggestion["paper_id"],)).fetchone()
            return runtime.row_to_paper(row, conn)

    @router.post("/api/suggestions/{suggestion_id}/reject")
    def reject_suggestion(suggestion_id: str) -> dict[str, str]:
        with runtime.connect() as conn:
            conn.execute("UPDATE tag_suggestions SET status = 'rejected' WHERE id = ?", (suggestion_id,))
        return {"status": "rejected"}


    app.include_router(router)
