from __future__ import annotations

from typing import Any

from backend.runtime import ApplicationRuntime


def upsert_paper(runtime: ApplicationRuntime, path_text: str) -> tuple[str, bool]:
    path = runtime.Path(path_text).resolve()
    stat = path.stat()
    with runtime.connect() as conn:
        existing = conn.execute(
            "SELECT id FROM papers WHERE file_path = ? OR file_path = ?",
            (runtime.to_stored_path(path), str(path)),
        ).fetchone()
        if existing:
            return str(existing["id"]), False
        paper_id = str(runtime.uuid.uuid4())
        conn.execute(
            """
            INSERT INTO papers (
              id, file_path, file_name, file_size, modified_at, imported_at,
              status, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                paper_id,
                runtime.to_stored_path(path),
                path.name,
                stat.st_size,
                runtime.datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
                runtime.now_iso(),
                "pending",
                runtime.now_iso(),
            ),
        )
        return paper_id, True


async def lookup_paper_partition(
    runtime: ApplicationRuntime,
    paper_id: str,
    journal_name_override: str | None = None,
    model_selection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    model_selection = model_selection or runtime.capture_ai_profile()
    with runtime.connect() as conn:
        paper = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
        if not paper:
            raise KeyError("文献不存在")
        journal_name = (
            runtime.nullable_str(journal_name_override)
            if journal_name_override is not None
            else runtime.nullable_str(paper["journal_name"])
        )
        file_name = paper["file_name"]
        file_path = paper["file_path"]

    if not journal_name:
        path = runtime.resolve_stored_path(file_path)
        if not path or not path.exists():
            raise RuntimeError("没有期刊名，且原 PDF 文件不存在或已移动，无法提取期刊名")
        first_page_text = runtime.extract_first_page_text(path)
        if not first_page_text:
            raise RuntimeError("没有期刊名，且 PDF 首页未提取到可用文本")
        ai_data = await runtime.call_configured_ai(runtime.build_journal_prompt(file_name, first_page_text), model_selection)
        journal_name = runtime.nullable_str(ai_data.get("journal_name"))

    with runtime.connect() as conn:
        current = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
        if not current:
            raise KeyError("文献不存在")
        result = runtime.journal_partitions.lookup_partition_records(conn, journal_name)
        now = runtime.now_iso()
        runtime.journal_partitions.sync_partition_suggestions(conn, paper_id, result, now)
        cas_partition, cas_top = runtime.journal_partitions.selected_cas_metadata(conn, paper_id, result)
        conn.execute(
            """UPDATE papers SET journal_name = ?, cas_partition_2025 = ?, cas_top_2025 = ?,
               newcomer_partitions_2026_json = ?, partition_checked_at = ?, updated_at = ?
               WHERE id = ?""",
            (
                journal_name,
                cas_partition,
                int(cas_top) if cas_top is not None else None,
                runtime.journal_partitions.encode_newcomer_partitions(result["newcomer_partitions_2026"]),
                now,
                now,
                paper_id,
            ),
        )
        updated = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
        return {"paper": runtime.row_to_paper(updated, conn), "matched": result["matched"], "journal_name": journal_name}


async def run_partition_lookup_batch(
    runtime: ApplicationRuntime,
    papers: list[dict[str, str]],
    scope: str = "all",
) -> None:
    model_selection = runtime.capture_ai_profile()
    with runtime.PARTITION_BATCH_LOCK:
        runtime.PARTITION_BATCH_STATUS = {
            "status": "running", "scope": scope, "total": len(papers), "processed": 0,
            "matched": 0, "unmatched": 0, "failed": 0,
            "cancel_requested": runtime.PARTITION_BATCH_STATUS.get("cancel_requested", False),
            "failed_items": [],
        }
    for paper in papers:
        with runtime.PARTITION_BATCH_LOCK:
            if runtime.PARTITION_BATCH_STATUS["cancel_requested"]:
                runtime.PARTITION_BATCH_STATUS["status"] = "cancelled"
                break
        try:
            result = await runtime.lookup_paper_partition(paper["id"], model_selection=model_selection)
            outcome = "matched" if result["matched"] else "unmatched"
            failure = None
        except Exception as exc:  # noqa: BLE001
            outcome = "failed"
            failure = {
                "title": paper["title"] or paper["file_name"],
                "message": (str(exc).strip() or type(exc).__name__)[:300],
            }
        with runtime.PARTITION_BATCH_LOCK:
            runtime.PARTITION_BATCH_STATUS["processed"] += 1
            runtime.PARTITION_BATCH_STATUS[outcome] += 1
            if failure:
                runtime.PARTITION_BATCH_STATUS["failed_items"].append(failure)
    with runtime.PARTITION_BATCH_LOCK:
        if runtime.PARTITION_BATCH_STATUS["status"] == "running":
            if runtime.PARTITION_BATCH_STATUS["cancel_requested"] and runtime.PARTITION_BATCH_STATUS["processed"] < len(papers):
                runtime.PARTITION_BATCH_STATUS["status"] = "cancelled"
            else:
                runtime.PARTITION_BATCH_STATUS["status"] = "completed"


async def process_paper(runtime: ApplicationRuntime, paper_id: str, rename_after_success: bool = False) -> None:
    model_selection = runtime.capture_ai_profile()
    with runtime.connect() as conn:
        paper = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
        if not paper:
            return
        conn.execute(
            "UPDATE papers SET status = ?, error = NULL, updated_at = ? WHERE id = ?",
            ("processing", runtime.now_iso(), paper_id),
        )
    raw_response = None
    try:
        path = runtime.resolve_stored_path(paper["file_path"])
        if not path or not path.exists():
            raise RuntimeError("原 PDF 文件不存在或已移动")
        first_page_text = runtime.extract_first_page_text(path)
        if not first_page_text:
            raise RuntimeError("PDF 首页未提取到可用文本，可能是扫描版或首页为空")
        with runtime.connect() as conn:
            tags = runtime.get_topic_tags(conn)
        ai_data = await runtime.call_configured_ai(runtime.build_ai_prompt(paper["file_name"], first_page_text, tags), model_selection)
        raw_response = runtime.json.dumps(ai_data, ensure_ascii=False)
        with runtime.connect() as conn:
            if not conn.execute("SELECT 1 FROM papers WHERE id = ?", (paper_id,)).fetchone():
                return
            valid_tag_ids = {tag["id"] for tag in runtime.get_topic_tags(conn)}
            existing_tag_ids = [
                tag_id
                for tag_id in ai_data.get("existing_tag_ids", [])
                if isinstance(tag_id, str) and tag_id in valid_tag_ids
            ][:5]
            runtime.assign_tags(conn, paper_id, existing_tag_ids)
            conn.execute("DELETE FROM tag_suggestions WHERE paper_id = ? AND status = 'pending'", (paper_id,))
            for suggestion in ai_data.get("new_tag_suggestions", [])[:3]:
                if not isinstance(suggestion, dict):
                    continue
                name = str(suggestion.get("name") or "").strip()
                reason = str(suggestion.get("reason") or "").strip()
                if not name:
                    continue
                similar = runtime.find_similar_tag(conn, name)
                if similar:
                    runtime.assign_tags(conn, paper_id, [similar])
                    continue
                if runtime.is_method_tag(name):
                    continue
                conn.execute(
                    """
                    INSERT INTO tag_suggestions (id, paper_id, name, reason, status, created_at)
                    VALUES (?, ?, ?, ?, 'pending', ?)
                    """,
                    (str(runtime.uuid.uuid4()), paper_id, name, reason, runtime.now_iso()),
                )
            confidence = ai_data.get("confidence") or {}
            authors = runtime.coerce_string_list(ai_data.get("authors"))
            affiliations = runtime.coerce_string_list(ai_data.get("affiliations"))
            publication_date = runtime.nullable_str(ai_data.get("publication_date"))
            title = runtime.nullable_str(ai_data.get("title"))
            existing_paper = conn.execute(
                "SELECT title_zh, abstract, journal_name FROM papers WHERE id = ?",
                (paper_id,),
            ).fetchone()
            title_zh = runtime.nullable_str(existing_paper["title_zh"]) if existing_paper else None
            abstract = runtime.nullable_str(existing_paper["abstract"]) if existing_paper else None
            if not title_zh:
                title_zh = runtime.nullable_str(ai_data.get("title_zh"))
            if not abstract:
                abstract = runtime.nullable_str(ai_data.get("abstract"))
            journal_name = (
                runtime.nullable_str(existing_paper["journal_name"]) if existing_paper else None
            ) or runtime.nullable_str(ai_data.get("journal_name"))
            conn.execute(
                """
                UPDATE papers SET
                  title = ?, title_zh = ?, abstract = ?,
                  authors_json = ?, affiliations_json = ?,
                  publication_date = ?, doi_url = ?, journal_name = ?, status = ?,
                  metadata_confidence = ?, tags_confidence = ?, error = NULL,
                  updated_at = ?
                WHERE id = ?
                """,
                (
                    title,
                    title_zh,
                    abstract,
                    runtime.json.dumps(authors, ensure_ascii=False),
                    runtime.json.dumps(affiliations, ensure_ascii=False),
                    publication_date,
                    runtime.normalize_doi_url(ai_data.get("doi_url")),
                    journal_name,
                    "ready",
                    runtime.coerce_confidence(confidence.get("metadata")),
                    runtime.coerce_confidence(confidence.get("tags")),
                    runtime.now_iso(),
                    paper_id,
                ),
            )
            runtime.sync_year_tag(conn, paper_id, publication_date)
            rename_warning = None
            if rename_after_success:
                try:
                    runtime.rename_pdf_to_title(conn, paper_id, title)
                except Exception as exc:  # noqa: BLE001
                    rename_warning = f"元数据已识别，但源文件重命名失败：{exc}"
                    conn.execute(
                        "UPDATE papers SET error = ?, updated_at = ? WHERE id = ?",
                        (rename_warning, runtime.now_iso(), paper_id),
                    )
            conn.execute(
                """
                INSERT INTO ai_runs (id, paper_id, status, error, raw_response, created_at)
                VALUES (?, ?, 'success', ?, ?, ?)
                """,
                (str(runtime.uuid.uuid4()), paper_id, rename_warning, raw_response[:12000], runtime.now_iso()),
            )
        try:
            await runtime.lookup_paper_partition(paper_id, journal_name, model_selection)
        except Exception:  # noqa: BLE001
            # Partition lookup is optional metadata and must not fail the paper import.
            pass
    except Exception as exc:  # noqa: BLE001
        status = "needs_config" if "未配置" in str(exc) else "ai_failed"
        with runtime.connect() as conn:
            if not conn.execute("SELECT 1 FROM papers WHERE id = ?", (paper_id,)).fetchone():
                return
            conn.execute(
                "UPDATE papers SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                (status, str(exc), runtime.now_iso(), paper_id),
            )
            conn.execute(
                """
                INSERT INTO ai_runs (id, paper_id, status, error, raw_response, created_at)
                VALUES (?, ?, 'failed', ?, ?, ?)
                """,
                (str(runtime.uuid.uuid4()), paper_id, str(exc), raw_response, runtime.now_iso()),
            )
