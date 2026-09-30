from __future__ import annotations

from typing import Any

from fastapi import APIRouter, FastAPI

from backend.runtime import ApplicationRuntime

def register(app: FastAPI, runtime: ApplicationRuntime) -> None:
    router = APIRouter()

    @router.get("/api/health")
    def health_check() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/api/state")
    def get_state(
        tag_id: str | None = None,
        tag_ids: list[str] = runtime.Query(default_factory=list),
        status: str | None = None,
        sort: str = "imported_desc",
    ) -> dict[str, Any]:
        with runtime.connect() as conn:
            tags = runtime.get_all_tags(conn)
            clauses: list[str] = []
            params: list[Any] = []
            tag_filters = runtime.normalize_tag_ids(tag_ids)
            if tag_id and tag_id not in {"unclassified", "failed"}:
                tag_filters.append(tag_id)
            tag_filters = list(dict.fromkeys(tag_filters))
            year_tag_ids = {tag["id"] for tag in tags if tag["kind"] == "year"}
            selected_year_ids = [item for item in tag_filters if item in year_tag_ids]
            selected_topic_ids = [item for item in tag_filters if item not in year_tag_ids]
            if selected_year_ids:
                placeholders = ",".join("?" for _ in selected_year_ids)
                clauses.append(
                    f"EXISTS (SELECT 1 FROM paper_tags pt WHERE pt.paper_id = p.id AND pt.tag_id IN ({placeholders}))"
                )
                params.extend(selected_year_ids)
            for index, selected_tag_id in enumerate(selected_topic_ids):
                clauses.append(
                    f"""
                    EXISTS (
                      SELECT 1 FROM paper_tags pt{index}
                      WHERE pt{index}.paper_id = p.id AND pt{index}.tag_id = ?
                    )
                    """
                )
                params.append(selected_tag_id)
            if tag_id == "unclassified":
                clauses.append(
                    """
                    NOT EXISTS (
                      SELECT 1 FROM paper_tags pt
                      JOIN tags t ON t.id = pt.tag_id
                      WHERE pt.paper_id = p.id AND t.kind = 'topic'
                    )
                    """
                )
            if tag_id == "failed":
                clauses.append("p.status IN ('ai_failed', 'needs_config')")
            if status:
                clauses.append("p.status = ?")
                params.append(status)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            rows = conn.execute(
                f"""
                SELECT p.* FROM papers p
                {where}
                ORDER BY p.imported_at DESC
                """,
                params,
            ).fetchall()
            if sort == "year_desc":
                rows = sorted(
                    rows,
                    key=lambda row: (extract_publication_year(row["publication_date"]) or "0000", row["imported_at"]),
                    reverse=True,
                )
            elif sort == "year_asc":
                rows = sorted(
                    rows,
                    key=lambda row: (extract_publication_year(row["publication_date"]) or "9999", row["imported_at"]),
                )
            papers = runtime.rows_to_papers(rows, conn)
            pending_suggestions = conn.execute(
                """SELECT
                     (SELECT COUNT(*) FROM tag_suggestions WHERE status = 'pending') +
                     (SELECT COUNT(*) FROM paper_partition_suggestions WHERE status = 'pending')
                   AS count"""
            ).fetchone()["count"]
            return {
                "papers": papers,
                "tags": tags,
                "config": runtime.read_model_config_public(mask_key=True),
                "library": runtime.library_status(),
                "counts": {
                    "all": conn.execute("SELECT COUNT(*) AS count FROM papers").fetchone()["count"],
                    "unclassified": conn.execute(
                        """
                        SELECT COUNT(*) AS count FROM papers p
                        WHERE NOT EXISTS (
                          SELECT 1 FROM paper_tags pt
                          JOIN tags t ON t.id = pt.tag_id
                          WHERE pt.paper_id = p.id AND t.kind = 'topic'
                        )
                        """
                    ).fetchone()["count"],
                    "failed": conn.execute(
                        "SELECT COUNT(*) AS count FROM papers WHERE status IN ('ai_failed', 'needs_config')"
                    ).fetchone()["count"],
                    "pending_suggestions": pending_suggestions,
                },
            }


    app.include_router(router)
