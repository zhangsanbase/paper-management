from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, FastAPI

from backend.models import PartitionLookupBatchRequest
from backend.runtime import ApplicationRuntime

def register(app: FastAPI, runtime: ApplicationRuntime) -> None:
    router = APIRouter()

    @router.post("/api/partitions/lookup-all")
    def lookup_all_paper_partitions(
        background_tasks: BackgroundTasks,
        payload: PartitionLookupBatchRequest | None = None,
    ) -> dict[str, Any]:
        scope = payload.scope if payload else "all"
        query = "SELECT id, title, file_name FROM papers"
        if scope == "unchecked":
            query += " WHERE partition_checked_at IS NULL"
        query += " ORDER BY imported_at"
        with runtime.connect() as conn:
            papers = [
                {
                    "id": str(row["id"]),
                    "title": runtime.nullable_str(row["title"]) or "",
                    "file_name": str(row["file_name"]),
                }
                for row in conn.execute(query).fetchall()
            ]
        with runtime.PARTITION_BATCH_LOCK:
            if runtime.PARTITION_BATCH_STATUS.get("status") == "running":
                return dict(runtime.PARTITION_BATCH_STATUS)
            runtime.PARTITION_BATCH_STATUS = {
                "status": "running", "scope": scope, "total": len(papers), "processed": 0,
                "matched": 0, "unmatched": 0, "failed": 0,
                "cancel_requested": False, "failed_items": [],
            }
        background_tasks.add_task(runtime.run_partition_lookup_batch, papers, scope)
        return dict(runtime.PARTITION_BATCH_STATUS)

    @router.get("/api/partitions/lookup-all/status")
    def get_partition_lookup_status() -> dict[str, Any]:
        with runtime.PARTITION_BATCH_LOCK:
            return dict(runtime.PARTITION_BATCH_STATUS)

    @router.post("/api/partitions/lookup-all/cancel")
    def cancel_partition_lookup_batch() -> dict[str, Any]:
        with runtime.PARTITION_BATCH_LOCK:
            if runtime.PARTITION_BATCH_STATUS.get("status") == "running":
                runtime.PARTITION_BATCH_STATUS["cancel_requested"] = True
            return dict(runtime.PARTITION_BATCH_STATUS)

    def set_partition_suggestion_status(suggestion_id: str, status: str) -> dict[str, Any]:
        with runtime.connect() as conn:
            suggestion = conn.execute(
                "SELECT paper_id, category FROM paper_partition_suggestions WHERE id = ?",
                (suggestion_id,),
            ).fetchone()
            if not suggestion:
                raise runtime.HTTPException(status_code=404, detail="分区建议不存在")
            if status == "approved" and suggestion["category"] == "cas_partition":
                conn.execute(
                    """UPDATE paper_partition_suggestions SET status = 'rejected', updated_at = ?
                       WHERE paper_id = ? AND category = 'cas_partition' AND id <> ?
                         AND status <> 'rejected'""",
                    (runtime.now_iso(), suggestion["paper_id"], suggestion_id),
                )
            conn.execute(
                "UPDATE paper_partition_suggestions SET status = ?, updated_at = ? WHERE id = ?",
                (status, runtime.now_iso(), suggestion_id),
            )
            row = conn.execute("SELECT * FROM papers WHERE id = ?", (suggestion["paper_id"],)).fetchone()
            if suggestion["category"] == "cas_partition":
                result = runtime.journal_partitions.lookup_partition_records(conn, row["journal_name"])
                cas_partition, cas_top = runtime.journal_partitions.selected_cas_metadata(conn, row["id"], result)
                conn.execute(
                    "UPDATE papers SET cas_partition_2025 = ?, cas_top_2025 = ?, updated_at = ? WHERE id = ?",
                    (cas_partition, int(cas_top) if cas_top is not None else None, runtime.now_iso(), row["id"]),
                )
                row = conn.execute("SELECT * FROM papers WHERE id = ?", (suggestion["paper_id"],)).fetchone()
            return runtime.row_to_paper(row, conn)

    @router.post("/api/partition-suggestions/{suggestion_id}/approve")
    def approve_partition_suggestion(suggestion_id: str) -> dict[str, Any]:
        return set_partition_suggestion_status(suggestion_id, "approved")

    @router.post("/api/partition-suggestions/{suggestion_id}/reject")
    def reject_partition_suggestion(suggestion_id: str) -> dict[str, Any]:
        return set_partition_suggestion_status(suggestion_id, "rejected")


    app.include_router(router)
