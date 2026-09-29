import asyncio
import json
import sqlite3
import sys
import types
from pathlib import Path

import pytest
import backend.app as app_module
from fastapi.testclient import TestClient

from backend import win_focus
from backend import journal_partitions

from backend.app import (
    SCHEMA,
    build_ai_prompt,
    coerce_string_list,
    create_app,
    extract_publication_year,
    is_method_tag,
    migrate_schema,
    normalize_doi_url,
    normalize_stored_paths,
    parse_ai_json,
    rename_pdf_to_title,
    remove_topic_tag,
    resolve_stored_path,
    safe_filename_stem,
    sync_year_tag,
    to_stored_path,
)


@pytest.fixture(autouse=True)
def isolate_file_library(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "LIBRARY_DIR", tmp_path / "library_files")
    monkeypatch.setattr(app_module, "PDF_VIEWER_CONFIG_PATH", tmp_path / "pdf_viewer.json")
    # Keep the portable-path base in the same sandbox so tests exercise the real
    # relative-path code path instead of silently degrading to absolute paths.
    monkeypatch.setattr(app_module, "PATH_BASE", tmp_path)
    app_module.FILE_CONFLICTS.clear()


def test_method_tags_are_blocked() -> None:
    assert is_method_tag("XPS")
    assert is_method_tag("xrd")
    assert not is_method_tag("尖晶石氧化物")


def test_parse_ai_json_from_markdown_fence() -> None:
    data = parse_ai_json('```json\n{"title":"A paper","authors":["Li"]}\n```')
    assert data["title"] == "A paper"
    assert data["authors"] == ["Li"]


def test_normalize_doi_url() -> None:
    assert normalize_doi_url("10.1000/example") == "https://doi.org/10.1000/example"
    assert normalize_doi_url("doi:10.1000/example") == "https://doi.org/10.1000/example"
    assert normalize_doi_url(None) is None


def test_safe_filename_stem_removes_windows_illegal_characters() -> None:
    assert safe_filename_stem('A<B>: "paper"? * / test.') == "A B paper test"


def test_coerce_string_list() -> None:
    assert coerce_string_list("A；B, C") == ["A", "B", "C"]
    assert coerce_string_list([" A ", 123, ""]) == ["A", "123"]


def test_health_check_endpoint(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "library.sqlite3")
    monkeypatch.setattr(app_module, "CONFIG_PATH", tmp_path / "config.json")
    client = TestClient(create_app())

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_extract_publication_year() -> None:
    assert extract_publication_year("2024-06-01") == "2024"
    assert extract_publication_year("Available online 15 March 2023") == "2023"
    assert extract_publication_year("no year") is None


def test_sync_year_tag_creates_one_year_tag() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        """
        INSERT INTO papers (
          id, file_path, file_name, file_size, modified_at, imported_at,
          status, updated_at
        ) VALUES ('p1', 'a.pdf', 'a.pdf', 1, '2026', '2026', 'ready', '2026')
        """
    )
    sync_year_tag(conn, "p1", "2024-01-01")
    sync_year_tag(conn, "p1", "2024")

    tags = conn.execute("SELECT name, kind, use_count FROM tags").fetchall()
    assert [(row["name"], row["kind"], row["use_count"]) for row in tags] == [("2024", "year", 1)]


def test_migrate_schema_adds_v11_columns_to_old_database() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE papers (
          id TEXT PRIMARY KEY,
          file_path TEXT NOT NULL UNIQUE,
          file_name TEXT NOT NULL,
          file_size INTEGER NOT NULL,
          modified_at TEXT NOT NULL,
          imported_at TEXT NOT NULL,
          title TEXT,
          authors_json TEXT NOT NULL DEFAULT '[]',
          affiliations_json TEXT NOT NULL DEFAULT '[]',
          publication_date TEXT,
          doi_url TEXT,
          status TEXT NOT NULL,
          error TEXT,
          metadata_confidence REAL,
          tags_confidence REAL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE tags (
          id TEXT PRIMARY KEY,
          name TEXT NOT NULL UNIQUE,
          aliases_json TEXT NOT NULL DEFAULT '[]',
          description TEXT NOT NULL DEFAULT '',
          use_count INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL
        );
        CREATE TABLE paper_tags (
          paper_id TEXT NOT NULL,
          tag_id TEXT NOT NULL,
          PRIMARY KEY (paper_id, tag_id)
        );
        INSERT INTO papers (
          id, file_path, file_name, file_size, modified_at, imported_at,
          publication_date, status, updated_at
        ) VALUES ('p1', 'a.pdf', 'a.pdf', 1, '2026', '2026', '2022', 'ready', '2026');
        """
    )
    migrate_schema(conn)
    paper_columns = {row["name"] for row in conn.execute("PRAGMA table_info(papers)").fetchall()}
    tag_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tags)").fetchall()}

    assert {"title_zh", "abstract", "notes"}.issubset(paper_columns)
    assert "kind" in tag_columns
    assert conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'paper_attachments'").fetchone()
    assert conn.execute("SELECT kind FROM tags WHERE name = '2022'").fetchone()["kind"] == "year"


def test_migrate_schema_clears_topic_descriptions_once_only() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        """
        INSERT INTO tags (id, name, aliases_json, description, kind, created_at)
        VALUES
          ('t1', 'MXene', '[]', 'This paper focuses on one imported article.', 'topic', '2026'),
          ('y1', '2026', '[]', '2026 年出版', 'year', '2026')
        """
    )

    migrate_schema(conn)

    assert conn.execute("SELECT description FROM tags WHERE id = 't1'").fetchone()["description"] == ""
    assert conn.execute("SELECT description FROM tags WHERE id = 'y1'").fetchone()["description"] == "2026 年出版"

    conn.execute("UPDATE tags SET description = 'manual description' WHERE id = 't1'")
    migrate_schema(conn)

    assert conn.execute("SELECT description FROM tags WHERE id = 't1'").fetchone()["description"] == "manual description"


def test_remove_topic_tag_deletes_tag_and_links_only() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        """
        INSERT INTO papers (
          id, file_path, file_name, file_size, modified_at, imported_at,
          status, updated_at
        ) VALUES ('p1', 'a.pdf', 'a.pdf', 1, '2026', '2026', 'ready', '2026')
        """
    )
    conn.execute(
        """
        INSERT INTO tags (id, name, aliases_json, description, kind, created_at)
        VALUES ('t1', 'PBA', '[]', '', 'topic', '2026')
        """
    )
    conn.execute("INSERT INTO paper_tags (paper_id, tag_id) VALUES ('p1', 't1')")

    removed = remove_topic_tag(conn, "t1")

    assert removed["name"] == "PBA"
    assert conn.execute("SELECT COUNT(*) AS count FROM papers").fetchone()["count"] == 1
    assert conn.execute("SELECT COUNT(*) AS count FROM tags").fetchone()["count"] == 0
    assert conn.execute("SELECT COUNT(*) AS count FROM paper_tags").fetchone()["count"] == 0


def test_remove_topic_tag_rejects_year_and_missing_tags() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        """
        INSERT INTO tags (id, name, aliases_json, description, kind, created_at)
        VALUES ('y1', '2026', '[]', '', 'year', '2026')
        """
    )

    try:
        remove_topic_tag(conn, "y1")
    except ValueError as exc:
        assert "年份标签" in str(exc)
    else:
        raise AssertionError("year tag deletion should fail")

    try:
        remove_topic_tag(conn, "missing")
    except KeyError:
        pass
    else:
        raise AssertionError("missing tag deletion should fail")


def test_delete_tag_api_removes_topic_but_rejects_year_and_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "library.sqlite3")
    monkeypatch.setattr(app_module, "CONFIG_PATH", tmp_path / "config.json")
    client = TestClient(create_app())

    topic = client.post("/api/tags", json={"name": "Prussian blue analogues"}).json()
    assert topic["aliases"] == ["Prussian blue analogues"]
    with app_module.connect() as conn:
        conn.execute(
            """
            INSERT INTO tags (id, name, aliases_json, description, kind, created_at)
            VALUES ('y1', '2026', '[]', '', 'year', '2026')
            """
        )

    assert client.delete(f"/api/tags/{topic['id']}").status_code == 200
    assert client.delete("/api/tags/y1").status_code == 400
    assert client.delete("/api/tags/missing").status_code == 404


def seed_paper_files(main_path: Path, attachment_path: Path | None = None) -> None:
    with app_module.connect() as conn:
        conn.execute(
            """
            INSERT INTO papers (
              id, file_path, file_name, file_size, modified_at, imported_at,
              status, updated_at
            ) VALUES ('p1', ?, ?, 1, '2026', '2026', 'ready', '2026')
            """,
            (to_stored_path(main_path), main_path.name),
        )
        if attachment_path is not None:
            conn.execute(
                """
                INSERT INTO paper_attachments (
                  id, paper_id, file_path, file_name, kind, created_at, updated_at
                ) VALUES ('a1', 'p1', ?, ?, 'supplementary', '2026', '2026')
                """,
                (to_stored_path(attachment_path), attachment_path.name),
            )


def test_remove_paper_without_file_option_keeps_main_and_attachment(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "library.sqlite3")
    monkeypatch.setattr(app_module, "CONFIG_PATH", tmp_path / "config.json")
    client = TestClient(create_app())
    main = tmp_path / "library_files" / "paper.pdf"
    attachment = main.with_name("support.xlsx")
    main.write_text("pdf", encoding="utf-8")
    attachment.write_text("data", encoding="utf-8")
    seed_paper_files(main, attachment)

    response = client.delete("/api/papers/p1")

    assert response.status_code == 200
    assert response.json()["moved_files"] == []
    assert main.exists() and attachment.exists()
    with app_module.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM papers WHERE id = 'p1'").fetchone()[0] == 0


def test_remove_paper_with_files_moves_main_and_attachment(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "library.sqlite3")
    monkeypatch.setattr(app_module, "CONFIG_PATH", tmp_path / "config.json")
    client = TestClient(create_app())
    main = tmp_path / "library_files" / "paper.pdf"
    attachment = main.with_name("support.xlsx")
    main.write_text("pdf", encoding="utf-8")
    attachment.write_text("data", encoding="utf-8")
    seed_paper_files(main, attachment)
    moved: list[str] = []

    def fake_send2trash(path: str) -> None:
        moved.append(path)
        Path(path).unlink()

    monkeypatch.setattr(app_module, "send2trash", fake_send2trash)

    response = client.delete("/api/papers/p1?delete_local_files=true")

    assert response.status_code == 200
    assert moved == [str(main), str(attachment)]
    assert response.json()["moved_files"] == moved
    assert response.json()["missing_files"] == []
    with app_module.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM papers WHERE id = 'p1'").fetchone()[0] == 0


def test_remove_paper_with_missing_file_skips_it(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "library.sqlite3")
    monkeypatch.setattr(app_module, "CONFIG_PATH", tmp_path / "config.json")
    client = TestClient(create_app())
    main = tmp_path / "library_files" / "paper.pdf"
    attachment = main.with_name("missing.xlsx")
    main.write_text("pdf", encoding="utf-8")
    seed_paper_files(main, attachment)
    moved: list[str] = []
    monkeypatch.setattr(app_module, "send2trash", lambda path: moved.append(path))

    response = client.delete("/api/papers/p1?delete_local_files=true")

    assert response.status_code == 200
    assert moved == [str(main)]
    assert response.json()["missing_files"] == [str(attachment)]


def test_remove_paper_rejects_external_or_shared_file_before_moving(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "library.sqlite3")
    monkeypatch.setattr(app_module, "CONFIG_PATH", tmp_path / "config.json")
    client = TestClient(create_app())
    main = tmp_path / "library_files" / "paper.pdf"
    outside = tmp_path / "outside.xlsx"
    main.write_text("pdf", encoding="utf-8")
    outside.write_text("data", encoding="utf-8")
    seed_paper_files(main, outside)
    moved: list[str] = []
    monkeypatch.setattr(app_module, "send2trash", lambda path: moved.append(path))

    response = client.delete("/api/papers/p1?delete_local_files=true")
    assert response.status_code == 409
    assert moved == []

    with app_module.connect() as conn:
        conn.execute("UPDATE paper_attachments SET file_path = ? WHERE id = 'a1'", (to_stored_path(main),))
        conn.execute(
            """
            INSERT INTO papers (
              id, file_path, file_name, file_size, modified_at, imported_at,
              status, updated_at
            ) VALUES ('p2', ?, 'other.pdf', 1, '2026', '2026', 'ready', '2026')
            """,
            (to_stored_path(main.with_name("other.pdf")),),
        )
        conn.execute(
            """
            INSERT INTO paper_attachments (
              id, paper_id, file_path, file_name, kind, created_at, updated_at
            ) VALUES ('a2', 'p2', ?, 'paper.pdf', 'supplementary', '2026', '2026')
            """,
            (str(main),),
        )

    response = client.delete("/api/papers/p1?delete_local_files=true")
    assert response.status_code == 409
    assert "其他文献" in response.json()["detail"]
    assert moved == []
    with app_module.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM papers WHERE id = 'p1'").fetchone()[0] == 1


def test_remove_paper_trash_failure_retains_record_and_can_retry(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "library.sqlite3")
    monkeypatch.setattr(app_module, "CONFIG_PATH", tmp_path / "config.json")
    client = TestClient(create_app())
    main = tmp_path / "library_files" / "paper.pdf"
    attachment = main.with_name("support.xlsx")
    main.write_text("pdf", encoding="utf-8")
    attachment.write_text("data", encoding="utf-8")
    seed_paper_files(main, attachment)

    def fail_on_attachment(path: str) -> None:
        if path == str(attachment):
            raise OSError("recycle bin unavailable")
        Path(path).unlink()

    monkeypatch.setattr(app_module, "send2trash", fail_on_attachment)
    response = client.delete("/api/papers/p1?delete_local_files=true")
    assert response.status_code == 500
    assert "paper.pdf" in response.json()["detail"]
    assert not main.exists() and attachment.exists()
    with app_module.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM papers WHERE id = 'p1'").fetchone()[0] == 1

    monkeypatch.setattr(app_module, "send2trash", lambda path: Path(path).unlink())
    response = client.delete("/api/papers/p1?delete_local_files=true")
    assert response.status_code == 200
    assert response.json()["missing_files"] == [str(main)]
    assert not attachment.exists()


def test_approve_suggestion_creates_tag_with_empty_description(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "library.sqlite3")
    monkeypatch.setattr(app_module, "CONFIG_PATH", tmp_path / "config.json")
    client = TestClient(create_app())
    with app_module.connect() as conn:
        conn.executescript(
            """
            INSERT INTO papers (
              id, file_path, file_name, file_size, modified_at, imported_at,
              status, updated_at
            ) VALUES ('p1', 'paper.pdf', 'paper.pdf', 1, '2026', '2026', 'ready', '2026');
            INSERT INTO tag_suggestions (id, paper_id, name, reason, status, created_at)
            VALUES ('s1', 'p1', 'Pressure sensors', 'This paper proposes one sensor.', 'pending', '2026');
            """
        )

    response = client.post("/api/suggestions/s1/approve")

    assert response.status_code == 200
    with app_module.connect() as conn:
        tag = conn.execute("SELECT name, aliases_json, description FROM tags WHERE name = 'Pressure sensors'").fetchone()
    assert tag["aliases_json"] == '["Pressure sensors"]'
    assert tag["description"] == ""


def test_ai_prompt_uses_tag_names_without_aliases_and_requires_english_suggestions() -> None:
    messages = build_ai_prompt(
        "paper.pdf",
        "first page",
        [
            {
                "id": "t1",
                "name": "Prussian blue analogues",
                "aliases": ["PBA", "普鲁士蓝类似物"],
                "description": "",
                "use_count": 3,
            }
        ],
    )
    payload = json.loads(messages[1]["content"])

    assert "name 是唯一可用于匹配的正式标签名" in messages[0]["content"]
    assert "必须使用简洁英文研究主题标签" in messages[0]["content"]
    assert "中文题名 title_zh" in messages[0]["content"]
    assert payload["required_json_schema"]["title_zh"] == "string|null"
    assert payload["required_json_schema"]["abstract"] == "string|null"
    assert payload["existing_tags"][0] == {
        "id": "t1",
        "name": "Prussian blue analogues",
        "description": "",
        "use_count": 3,
    }


def test_generate_abstract_failure_records_ai_run(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "library.sqlite3")
    monkeypatch.setattr(app_module, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(app_module, "extract_first_page_text", lambda _path: "Abstract text")
    client = TestClient(create_app())
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_text("not a real pdf", encoding="utf-8")
    with app_module.connect() as conn:
        conn.execute(
            """
            INSERT INTO papers (
              id, file_path, file_name, file_size, modified_at, imported_at,
              title, title_zh, status, updated_at
            ) VALUES ('p1', ?, 'paper.pdf', 1, '2026', '2026',
              'A paper', '一篇论文', 'ready', '2026')
            """,
            (str(pdf_path),),
        )

    response = client.post("/api/papers/p1/generate-abstract")

    assert response.status_code == 502
    assert "AI API 未配置" in response.json()["detail"]
    with app_module.connect() as conn:
        row = conn.execute("SELECT status, error FROM ai_runs WHERE paper_id = 'p1'").fetchone()
    assert row["status"] == "generate-abstract-failed"
    assert "AI API 未配置" in row["error"]


def test_translate_title_failure_records_ai_run(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "library.sqlite3")
    monkeypatch.setattr(app_module, "CONFIG_PATH", tmp_path / "config.json")
    client = TestClient(create_app())
    with app_module.connect() as conn:
        conn.execute(
            """
            INSERT INTO papers (
              id, file_path, file_name, file_size, modified_at, imported_at,
              title, status, updated_at
            ) VALUES ('p1', 'paper.pdf', 'paper.pdf', 1, '2026', '2026',
              'A paper', 'ready', '2026')
            """
        )

    response = client.post("/api/papers/p1/translate-title")

    assert response.status_code == 502
    assert "AI API 未配置" in response.json()["detail"]
    with app_module.connect() as conn:
        row = conn.execute("SELECT status, error FROM ai_runs WHERE paper_id = 'p1'").fetchone()
    assert row["status"] == "translate-title-failed"
    assert "AI API 未配置" in row["error"]


def test_process_paper_stores_imported_title_translation_and_abstract(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "library.sqlite3")
    monkeypatch.setattr(app_module, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(app_module, "extract_first_page_text", lambda _path: "Abstract text")

    async def fake_call_ai(_config, _messages):
        return {
            "title": "A paper",
            "title_zh": "一篇论文",
            "abstract": "这是一段中文摘要。",
            "authors": ["Li"],
            "affiliations": ["Lab"],
            "publication_date": "2026",
            "doi_url": "10.1000/example",
            "existing_tag_ids": [],
            "new_tag_suggestions": [],
            "confidence": {"metadata": 0.9, "tags": 0.8},
        }

    monkeypatch.setattr(app_module, "call_ai", fake_call_ai)
    client = TestClient(create_app())
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_text("pdf", encoding="utf-8")
    with app_module.connect() as conn:
        conn.execute(
            """
            INSERT INTO papers (
              id, file_path, file_name, file_size, modified_at, imported_at,
              status, updated_at
            ) VALUES ('p1', ?, 'paper.pdf', 1, '2026', '2026', 'pending', '2026')
            """,
            (str(pdf_path),),
        )

    asyncio.run(app_module.process_paper("p1"))

    paper = client.get("/api/state").json()["papers"][0]
    assert paper["title"] == "A paper"
    assert paper["title_zh"] == "一篇论文"
    assert paper["abstract"] == "这是一段中文摘要。"
    assert paper["status"] == "ready"


def test_process_paper_does_not_overwrite_existing_translation_or_abstract(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "library.sqlite3")
    monkeypatch.setattr(app_module, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(app_module, "extract_first_page_text", lambda _path: "Abstract text")

    async def fake_call_ai(_config, _messages):
        return {
            "title": "A revised paper",
            "title_zh": "模型返回的新中文题名",
            "abstract": "模型返回的新摘要。",
            "authors": [],
            "affiliations": [],
            "publication_date": None,
            "doi_url": None,
            "existing_tag_ids": [],
            "new_tag_suggestions": [],
            "confidence": {"metadata": 0.7, "tags": 0.6},
        }

    monkeypatch.setattr(app_module, "call_ai", fake_call_ai)
    client = TestClient(create_app())
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_text("pdf", encoding="utf-8")
    with app_module.connect() as conn:
        conn.execute(
            """
            INSERT INTO papers (
              id, file_path, file_name, file_size, modified_at, imported_at,
              title, title_zh, abstract, status, updated_at
            ) VALUES ('p1', ?, 'paper.pdf', 1, '2026', '2026',
              'Old title', '人工校对题名', '人工校对摘要', 'ready', '2026')
            """,
            (str(pdf_path),),
        )

    asyncio.run(app_module.process_paper("p1"))

    paper = client.get("/api/state").json()["papers"][0]
    assert paper["title"] == "A revised paper"
    assert paper["title_zh"] == "人工校对题名"
    assert paper["abstract"] == "人工校对摘要"


def test_state_filters_multiple_tags_with_and_relation(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "library.sqlite3")
    monkeypatch.setattr(app_module, "CONFIG_PATH", tmp_path / "config.json")
    client = TestClient(create_app())
    with app_module.connect() as conn:
        conn.executescript(
            """
            INSERT INTO papers (
              id, file_path, file_name, file_size, modified_at, imported_at,
              title, status, updated_at
            ) VALUES
              ('p1', 'p1.pdf', 'p1.pdf', 1, '2026', '2026', 'Paper 1', 'ready', '2026'),
              ('p2', 'p2.pdf', 'p2.pdf', 1, '2026', '2026', 'Paper 2', 'ready', '2026');
            INSERT INTO tags (id, name, aliases_json, description, kind, created_at)
            VALUES
              ('t1', 'PBA', '[]', '', 'topic', '2026'),
              ('t2', 'MXene', '[]', '', 'topic', '2026');
            INSERT INTO paper_tags (paper_id, tag_id) VALUES
              ('p1', 't1'), ('p1', 't2'), ('p2', 't1');
            """
        )

    response = client.get("/api/state?tag_ids=t1&tag_ids=t2")

    assert response.status_code == 200
    assert [paper["id"] for paper in response.json()["papers"]] == ["p1"]


@pytest.mark.parametrize(
    ("query", "expected_ids"),
    [
        ("tag_ids=y2023&tag_ids=y2024", {"p1", "p2"}),
        ("tag_ids=y2024", {"p2"}),
        ("", {"p1", "p2", "p3", "p4"}),
        ("tag_ids=y2023&tag_ids=y2024&tag_ids=t1", {"p1", "p2"}),
        ("tag_ids=y2023&tag_ids=y2024&tag_ids=t1&tag_ids=t2", {"p1"}),
        ("tag_ids=t1&tag_ids=t2", {"p1", "p3"}),
        ("tag_id=y2023&tag_ids=y2024", {"p1", "p2"}),
        ("tag_ids=y2023&tag_ids=missing", set()),
    ],
)
def test_state_year_filters_use_or_with_topic_intersection(tmp_path, monkeypatch, query, expected_ids) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "library.sqlite3")
    monkeypatch.setattr(app_module, "CONFIG_PATH", tmp_path / "config.json")
    client = TestClient(create_app())
    with app_module.connect() as conn:
        conn.executescript(
            """
            INSERT INTO papers (
              id, file_path, file_name, file_size, modified_at, imported_at,
              title, status, updated_at
            ) VALUES
              ('p1', 'p1.pdf', 'p1.pdf', 1, '2026', '2026', 'Paper 1', 'ready', '2026'),
              ('p2', 'p2.pdf', 'p2.pdf', 1, '2026', '2026', 'Paper 2', 'ready', '2026'),
              ('p3', 'p3.pdf', 'p3.pdf', 1, '2026', '2026', 'Paper 3', 'ready', '2026'),
              ('p4', 'p4.pdf', 'p4.pdf', 1, '2026', '2026', 'Paper 4', 'ready', '2026');
            INSERT INTO tags (id, name, aliases_json, description, kind, created_at)
            VALUES
              ('y2023', '2023', '[]', '', 'year', '2026'),
              ('y2024', '2024', '[]', '', 'year', '2026'),
              ('y2025', '2025', '[]', '', 'year', '2026'),
              ('t1', 'Battery', '[]', '', 'topic', '2026'),
              ('t2', 'Carbon', '[]', '', 'topic', '2026');
            INSERT INTO paper_tags (paper_id, tag_id) VALUES
              ('p1', 'y2023'), ('p1', 't1'), ('p1', 't2'),
              ('p2', 'y2024'), ('p2', 't1'),
              ('p3', 'y2025'), ('p3', 't1'), ('p3', 't2'),
              ('p4', 't2');
            """
        )

    response = client.get(f"/api/state?{query}")

    assert response.status_code == 200
    assert {paper["id"] for paper in response.json()["papers"]} == expected_ids


def test_state_unclassified_means_without_topic_tags(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "library.sqlite3")
    monkeypatch.setattr(app_module, "CONFIG_PATH", tmp_path / "config.json")
    client = TestClient(create_app())
    with app_module.connect() as conn:
        conn.executescript(
            """
            INSERT INTO papers (
              id, file_path, file_name, file_size, modified_at, imported_at,
              title, status, updated_at
            ) VALUES
              ('p1', 'p1.pdf', 'p1.pdf', 1, '2026', '2026', 'Paper 1', 'ready', '2026'),
              ('p2', 'p2.pdf', 'p2.pdf', 1, '2026', '2026', 'Paper 2', 'ready', '2026'),
              ('p3', 'p3.pdf', 'p3.pdf', 1, '2026', '2026', 'Paper 3', 'ready', '2026'),
              ('p4', 'p4.pdf', 'p4.pdf', 1, '2026', '2026', 'Paper 4', 'ready', '2026');
            INSERT INTO tags (id, name, aliases_json, description, kind, created_at)
            VALUES
              ('y2026', '2026', '[]', '', 'year', '2026'),
              ('t1', 'Battery', '[]', '', 'topic', '2026');
            INSERT INTO paper_tags (paper_id, tag_id) VALUES
              ('p1', 'y2026'),
              ('p2', 't1'),
              ('p4', 'y2026'),
              ('p4', 't1');
            """
        )

    all_state = client.get("/api/state").json()
    filtered_state = client.get("/api/state?tag_id=unclassified").json()

    assert all_state["counts"]["unclassified"] == 2
    assert {paper["id"] for paper in filtered_state["papers"]} == {"p1", "p3"}


def test_state_batches_related_paper_data_without_changing_shape(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "library.sqlite3")
    monkeypatch.setattr(app_module, "CONFIG_PATH", tmp_path / "config.json")
    client = TestClient(create_app())
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_text("pdf", encoding="utf-8")
    attachment_path = tmp_path / "support.xlsx"
    attachment_path.write_text("data", encoding="utf-8")
    with app_module.connect() as conn:
        conn.execute(
            """
            INSERT INTO papers (
              id, file_path, file_name, file_size, modified_at, imported_at,
              title, status, updated_at
            ) VALUES ('p1', ?, 'paper.pdf', 1, '2026', '2026',
              'Paper 1', 'ready', '2026')
            """,
            (str(pdf_path),),
        )
        conn.executescript(
            """
            INSERT INTO tags (id, name, aliases_json, description, kind, created_at)
            VALUES ('t1', 'PBA', '[]', '', 'topic', '2026');
            INSERT INTO paper_tags (paper_id, tag_id) VALUES ('p1', 't1');
            INSERT INTO tag_suggestions (id, paper_id, name, reason, status, created_at)
            VALUES ('s1', 'p1', 'Battery cathodes', 'reason', 'pending', '2026');
            """
        )
        conn.execute(
            """
            INSERT INTO paper_attachments (
              id, paper_id, file_path, file_name, kind, created_at, updated_at
            ) VALUES ('a1', 'p1', ?, 'support.xlsx', 'supplementary', '2026', '2026')
            """,
            (str(attachment_path),),
        )

    response = client.get("/api/state")

    assert response.status_code == 200
    paper = response.json()["papers"][0]
    assert paper["id"] == "p1"
    assert paper["exists"] is True
    assert [tag["name"] for tag in paper["tags"]] == ["PBA"]
    assert paper["tag_suggestions"][0]["name"] == "Battery cathodes"
    assert paper["attachments"][0]["file_name"] == "support.xlsx"
    assert paper["attachments"][0]["exists"] is True


def test_open_paper_on_windows_opens_once_and_focuses(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "library.sqlite3")
    monkeypatch.setattr(app_module, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(app_module.platform, "system", lambda: "Windows")
    opened: list[Path] = []
    monkeypatch.setattr(app_module.win_focus, "open_document", lambda path: opened.append(path))
    monkeypatch.setattr(app_module.os, "startfile", lambda path: pytest.fail("PDF opened twice"), raising=False)
    client = TestClient(create_app())
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_text("pdf", encoding="utf-8")
    with app_module.connect() as conn:
        conn.execute(
            """
            INSERT INTO papers (
              id, file_path, file_name, file_size, modified_at, imported_at,
              status, updated_at
            ) VALUES ('p1', ?, 'paper.pdf', 1, '2026', '2026', 'ready', '2026')
            """,
            (str(pdf_path),),
        )

    response = client.post("/api/papers/p1/open")

    assert response.status_code == 200
    assert opened == [pdf_path]


def test_open_paper_on_windows_reports_open_failure(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "library.sqlite3")
    monkeypatch.setattr(app_module, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(app_module.platform, "system", lambda: "Windows")

    def fail_open(path: Path) -> None:
        raise RuntimeError("无法打开 PDF：阅读器启动失败")

    monkeypatch.setattr(app_module.win_focus, "open_document", fail_open)
    client = TestClient(create_app())
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_text("pdf", encoding="utf-8")
    with app_module.connect() as conn:
        conn.execute(
            """
            INSERT INTO papers (
              id, file_path, file_name, file_size, modified_at, imported_at,
              status, updated_at
            ) VALUES ('p1', ?, 'paper.pdf', 1, '2026', '2026', 'ready', '2026')
            """,
            (str(pdf_path),),
        )

    response = client.post("/api/papers/p1/open")

    assert response.status_code == 500
    assert response.json()["detail"] == "无法打开 PDF：阅读器启动失败"


def test_open_folder_endpoint_opens_parent_location(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "library.sqlite3")
    monkeypatch.setattr(app_module, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(app_module.platform, "system", lambda: "Linux")
    calls: list[list[str]] = []
    monkeypatch.setattr(app_module.subprocess, "Popen", lambda args: calls.append(args))
    client = TestClient(create_app())
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_text("pdf", encoding="utf-8")
    with app_module.connect() as conn:
        conn.execute(
            """
            INSERT INTO papers (
              id, file_path, file_name, file_size, modified_at, imported_at,
              status, updated_at
            ) VALUES ('p1', ?, 'paper.pdf', 1, '2026', '2026', 'ready', '2026')
            """,
            (str(pdf_path),),
        )

    response = client.post("/api/papers/p1/open-folder")

    assert response.status_code == 200
    assert calls == [["xdg-open", str(tmp_path)]]


def test_open_folder_endpoint_reveals_file_on_windows(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "library.sqlite3")
    monkeypatch.setattr(app_module, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(app_module.platform, "system", lambda: "Windows")
    calls: list[Path] = []
    monkeypatch.setattr(app_module.win_focus, "reveal_in_explorer", lambda path: calls.append(path))
    client = TestClient(create_app())
    pdf_path = tmp_path / "library_files" / "paper.pdf"
    pdf_path.parent.mkdir(exist_ok=True)
    pdf_path.write_text("pdf", encoding="utf-8")
    with app_module.connect() as conn:
        conn.execute(
            """
            INSERT INTO papers (
              id, file_path, file_name, file_size, modified_at, imported_at,
              status, updated_at
            ) VALUES ('p1', ?, 'paper.pdf', 1, '2026', '2026', 'ready', '2026')
            """,
            (str(pdf_path),),
        )

    response = client.post("/api/papers/p1/open-folder")

    assert response.status_code == 200
    assert calls == [pdf_path]


def test_open_local_directory_uses_explorer_focus_on_windows(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module.platform, "system", lambda: "Windows")
    calls: list[Path] = []
    monkeypatch.setattr(app_module.win_focus, "open_folder", lambda path: calls.append(path))
    folder = tmp_path / "library_files"

    app_module.open_local_directory(folder)

    assert folder.exists()
    assert calls == [folder]


def test_enable_windows_dpi_awareness_prefers_shcore_once(monkeypatch) -> None:
    calls: list[tuple[str, int | None]] = []

    fake_ctypes = types.SimpleNamespace(
        windll=types.SimpleNamespace(
            shcore=types.SimpleNamespace(SetProcessDpiAwareness=lambda value: calls.append(("shcore", value))),
            user32=types.SimpleNamespace(SetProcessDPIAware=lambda: calls.append(("user32", None))),
        )
    )
    monkeypatch.setattr(app_module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(app_module, "_DPI_AWARENESS_INITIALIZED", False)
    monkeypatch.setitem(sys.modules, "ctypes", fake_ctypes)

    app_module.enable_windows_dpi_awareness()
    app_module.enable_windows_dpi_awareness()

    assert calls == [("shcore", 2)]


def test_enable_windows_dpi_awareness_falls_back_to_user32(monkeypatch) -> None:
    calls: list[str] = []

    def fail_shcore(_value: int) -> None:
        raise OSError("unsupported")

    fake_ctypes = types.SimpleNamespace(
        windll=types.SimpleNamespace(
            shcore=types.SimpleNamespace(SetProcessDpiAwareness=fail_shcore),
            user32=types.SimpleNamespace(SetProcessDPIAware=lambda: calls.append("user32")),
        )
    )
    monkeypatch.setattr(app_module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(app_module, "_DPI_AWARENESS_INITIALIZED", False)
    monkeypatch.setitem(sys.modules, "ctypes", fake_ctypes)

    app_module.enable_windows_dpi_awareness()

    assert calls == ["user32"]


def test_enable_windows_dpi_awareness_ignores_missing_windows_api(monkeypatch) -> None:
    monkeypatch.setattr(app_module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(app_module, "_DPI_AWARENESS_INITIALIZED", False)
    monkeypatch.setitem(sys.modules, "ctypes", types.SimpleNamespace())

    app_module.enable_windows_dpi_awareness()

    assert app_module._DPI_AWARENESS_INITIALIZED is True


def test_choose_local_files_uses_native_windows_dialog(monkeypatch) -> None:
    native_calls: list[tuple[str, list[tuple[str, str]]]] = []

    def fake_native(title: str, filetypes: list[tuple[str, str]]) -> list[str]:
        native_calls.append((title, filetypes))
        return ["C:\\paper.pdf"]

    monkeypatch.setattr(app_module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(app_module, "enable_windows_dpi_awareness", lambda: None)
    monkeypatch.setattr(app_module, "_choose_windows_files_native", fake_native)
    monkeypatch.setattr(app_module, "_choose_tk_files", lambda *_args: ["tk"])

    files = app_module.choose_local_files("选择 PDF 文献", [("PDF 文献", "*.pdf")])

    assert files == ["C:\\paper.pdf"]
    assert native_calls == [("选择 PDF 文献", [("PDF 文献", "*.pdf")])]


def test_choose_local_files_falls_back_to_tk_when_native_dialog_fails(monkeypatch) -> None:
    tk_calls: list[tuple[str, list[tuple[str, str]]]] = []

    def fake_tk(title: str, filetypes: list[tuple[str, str]]) -> list[str]:
        tk_calls.append((title, filetypes))
        return ["fallback.pdf"]

    monkeypatch.setattr(app_module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(app_module, "enable_windows_dpi_awareness", lambda: None)
    monkeypatch.setattr(app_module, "_choose_windows_files_native", lambda *_args: (_ for _ in ()).throw(OSError("COM failed")))
    monkeypatch.setattr(app_module, "_choose_tk_files", fake_tk)

    files = app_module.choose_local_files("选择文件", [("所有文件", "*.*")])

    assert files == ["fallback.pdf"]
    assert tk_calls == [("选择文件", [("所有文件", "*.*")])]


def test_relink_file_updates_local_pdf_path_only(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "library.sqlite3")
    monkeypatch.setattr(app_module, "CONFIG_PATH", tmp_path / "config.json")
    new_pdf = tmp_path / "new.pdf"
    new_pdf.write_text("new pdf", encoding="utf-8")
    monkeypatch.setattr(app_module, "choose_pdf_files", lambda: [str(new_pdf)])
    client = TestClient(create_app())
    with app_module.connect() as conn:
        conn.executescript(
            """
            INSERT INTO papers (
              id, file_path, file_name, file_size, modified_at, imported_at,
              title, title_zh, abstract, status, updated_at
            ) VALUES (
              'p1', 'missing.pdf', 'missing.pdf', 1, '2026', '2026',
              'Original title', '原始标题', '摘要', 'ready', '2026'
            );
            INSERT INTO tags (id, name, aliases_json, description, kind, created_at)
            VALUES ('t1', 'PBA', '[]', '', 'topic', '2026');
            INSERT INTO paper_tags (paper_id, tag_id) VALUES ('p1', 't1');
            """
        )

    response = client.post("/api/papers/p1/relink-file")

    assert response.status_code == 200
    data = response.json()
    renamed = tmp_path / "library_files" / "Original title.pdf"
    assert data["file_path"] == str(renamed.resolve())
    assert data["file_name"] == "Original title.pdf"
    assert renamed.exists()
    assert not new_pdf.exists()
    assert data["title"] == "Original title"
    assert data["title_zh"] == "原始标题"
    assert data["abstract"] == "摘要"
    assert [tag["name"] for tag in data["tags"]] == ["PBA"]


def test_rename_pdf_to_title_updates_path_when_no_conflict(tmp_path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    source = tmp_path / "old.pdf"
    source.write_text("pdf", encoding="utf-8")
    conn.execute(
        """
        INSERT INTO papers (
          id, file_path, file_name, file_size, modified_at, imported_at,
          status, updated_at
        ) VALUES ('p1', ?, 'old.pdf', 3, '2026', '2026', 'ready', '2026')
        """,
        (str(source),),
    )

    renamed = rename_pdf_to_title(conn, "p1", "A Great Paper")

    renamed_path = tmp_path / "library_files" / "A Great Paper.pdf"
    assert renamed == str(renamed_path)
    assert not source.exists()
    assert renamed_path.exists()
    row = conn.execute("SELECT file_path, file_name FROM papers WHERE id = 'p1'").fetchone()
    assert row["file_path"] == to_stored_path(renamed_path)
    assert row["file_name"] == "A Great Paper.pdf"


def test_rename_pdf_to_title_reports_name_conflict(tmp_path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    source = tmp_path / "old.pdf"
    source.write_text("pdf", encoding="utf-8")
    existing = tmp_path / "library_files" / "A Great Paper.pdf"
    existing.parent.mkdir()
    existing.write_text("other", encoding="utf-8")
    conn.execute(
        """
        INSERT INTO papers (
          id, file_path, file_name, file_size, modified_at, imported_at,
          status, updated_at
        ) VALUES ('p1', ?, 'old.pdf', 3, '2026', '2026', 'ready', '2026')
        """,
        (str(source),),
    )

    with pytest.raises(RuntimeError, match="目标文件已存在"):
        rename_pdf_to_title(conn, "p1", "A Great Paper")

    assert source.exists()
    assert existing.exists()


def test_relink_file_name_conflict_can_auto_number(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "library.sqlite3")
    monkeypatch.setattr(app_module, "CONFIG_PATH", tmp_path / "config.json")
    app_module.FILE_CONFLICTS.clear()
    selected = tmp_path / "selected.pdf"
    selected.write_text("pdf", encoding="utf-8")
    existing = tmp_path / "library_files" / "A Great Paper.pdf"
    existing.parent.mkdir()
    existing.write_text("existing", encoding="utf-8")
    monkeypatch.setattr(app_module, "choose_pdf_files", lambda: [str(selected)])
    client = TestClient(create_app())
    with app_module.connect() as conn:
        conn.execute(
            """
            INSERT INTO papers (
              id, file_path, file_name, file_size, modified_at, imported_at,
              title, status, updated_at
            ) VALUES ('p1', 'missing.pdf', 'missing.pdf', 1, '2026', '2026',
              'A Great Paper', 'ready', '2026')
            """
        )

    response = client.post("/api/papers/p1/relink-file")

    assert response.status_code == 409
    conflict = response.json()
    assert conflict["kind"] == "main"
    assert conflict["source_path"] == str(selected.resolve())
    assert conflict["target_path"] == str(existing.resolve())
    assert selected.exists()
    response = client.post(f"/api/file-conflicts/{conflict['conflict_id']}/resolve", json={"action": "auto_number"})

    assert response.status_code == 200
    data = response.json()
    numbered = tmp_path / "library_files" / "A Great Paper (1).pdf"
    assert data["file_path"] == str(numbered.resolve())
    assert numbered.exists()
    assert not selected.exists()


def test_attachment_select_and_remove_only_changes_links(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "library.sqlite3")
    monkeypatch.setattr(app_module, "CONFIG_PATH", tmp_path / "config.json")
    supplement = tmp_path / "supporting-info.xlsx"
    supplement.write_text("data", encoding="utf-8")
    monkeypatch.setattr(app_module, "choose_attachment_files", lambda: [str(supplement)])
    client = TestClient(create_app())
    with app_module.connect() as conn:
        conn.execute(
            """
            INSERT INTO papers (
              id, file_path, file_name, file_size, modified_at, imported_at,
              title, status, updated_at
            ) VALUES ('p1', 'paper.pdf', 'paper.pdf', 1, '2026', '2026',
              'A Great Paper', 'ready', '2026')
            """
        )

    response = client.post("/api/papers/p1/attachments/select")

    assert response.status_code == 200
    data = response.json()
    assert data["attachments"][0]["file_name"] == "A Great Paper_sp.xlsx"
    assert data["attachments"][0]["file_path"] == str(tmp_path / "library_files" / "A Great Paper_sp.xlsx")
    assert data["attachments"][0]["exists"] is True
    attachment_id = data["attachments"][0]["id"]
    with app_module.connect() as conn:
        stored = conn.execute(
            "SELECT file_path FROM paper_attachments WHERE id = ?", (attachment_id,)
        ).fetchone()
    assert stored["file_path"] == to_stored_path(tmp_path / "library_files" / "A Great Paper_sp.xlsx")

    response = client.delete(f"/api/papers/p1/attachments/{attachment_id}")

    assert response.status_code == 200
    assert response.json()["attachments"] == []
    assert (tmp_path / "library_files" / "A Great Paper_sp.xlsx").exists()


def test_attachment_conflict_cancel_does_not_add_link(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "library.sqlite3")
    monkeypatch.setattr(app_module, "CONFIG_PATH", tmp_path / "config.json")
    app_module.FILE_CONFLICTS.clear()
    supplement = tmp_path / "raw.xlsx"
    supplement.write_text("data", encoding="utf-8")
    existing = tmp_path / "library_files" / "A Great Paper_sp.xlsx"
    existing.parent.mkdir()
    existing.write_text("existing", encoding="utf-8")
    monkeypatch.setattr(app_module, "choose_attachment_files", lambda: [str(supplement)])
    client = TestClient(create_app())
    with app_module.connect() as conn:
        conn.execute(
            """
            INSERT INTO papers (
              id, file_path, file_name, file_size, modified_at, imported_at,
              title, status, updated_at
            ) VALUES ('p1', 'paper.pdf', 'paper.pdf', 1, '2026', '2026',
              'A Great Paper', 'ready', '2026')
            """
        )

    response = client.post("/api/papers/p1/attachments/select")

    assert response.status_code == 409
    conflict = response.json()
    assert conflict["kind"] == "attachment"
    response = client.post(f"/api/file-conflicts/{conflict['conflict_id']}/resolve", json={"action": "cancel"})

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    with app_module.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM paper_attachments").fetchone()[0] == 0
    assert supplement.exists()
    assert existing.exists()


def test_library_migration_moves_existing_main_and_attachment(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "library.sqlite3")
    monkeypatch.setattr(app_module, "CONFIG_PATH", tmp_path / "config.json")
    source_pdf = tmp_path / "external" / "old.pdf"
    source_pdf.parent.mkdir()
    source_pdf.write_text("pdf", encoding="utf-8")
    source_attachment = tmp_path / "external" / "support.xlsx"
    source_attachment.write_text("data", encoding="utf-8")
    client = TestClient(create_app())
    with app_module.connect() as conn:
        conn.executescript(
            f"""
            INSERT INTO papers (
              id, file_path, file_name, file_size, modified_at, imported_at,
              title, status, updated_at
            ) VALUES ('p1', '{source_pdf}', 'old.pdf', 3, '2026', '2026',
              'A Great Paper', 'ready', '2026');
            INSERT INTO paper_attachments (
              id, paper_id, file_path, file_name, kind, created_at, updated_at
            ) VALUES ('a1', 'p1', '{source_attachment}', 'support.xlsx', 'supplementary', '2026', '2026');
            """
        )

    response = client.post("/api/library/migrate")

    assert response.status_code == 200
    result = response.json()
    assert result["total"] == 2
    assert result["migrated"] == 2
    main_target = tmp_path / "library_files" / "A Great Paper.pdf"
    attachment_target = tmp_path / "library_files" / "A Great Paper_sp.xlsx"
    assert main_target.exists()
    assert attachment_target.exists()
    assert not source_pdf.exists()
    assert not source_attachment.exists()
    with app_module.connect() as conn:
        paper = conn.execute("SELECT file_path, file_name FROM papers WHERE id = 'p1'").fetchone()
        attachment = conn.execute("SELECT file_path, file_name FROM paper_attachments WHERE id = 'a1'").fetchone()
    assert paper["file_path"] == to_stored_path(main_target)
    assert paper["file_name"] == "A Great Paper.pdf"
    assert attachment["file_path"] == to_stored_path(attachment_target)
    assert attachment["file_name"] == "A Great Paper_sp.xlsx"


def test_library_migration_returns_conflict_without_overwriting(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "library.sqlite3")
    monkeypatch.setattr(app_module, "CONFIG_PATH", tmp_path / "config.json")
    source_pdf = tmp_path / "external" / "old.pdf"
    source_pdf.parent.mkdir()
    source_pdf.write_text("pdf", encoding="utf-8")
    existing = tmp_path / "library_files" / "A Great Paper.pdf"
    existing.parent.mkdir()
    existing.write_text("existing", encoding="utf-8")
    client = TestClient(create_app())
    with app_module.connect() as conn:
        conn.execute(
            """
            INSERT INTO papers (
              id, file_path, file_name, file_size, modified_at, imported_at,
              title, status, updated_at
            ) VALUES ('p1', ?, 'old.pdf', 3, '2026', '2026',
              'A Great Paper', 'ready', '2026')
            """,
            (str(source_pdf),),
        )

    response = client.post("/api/library/migrate")

    assert response.status_code == 409
    conflict = response.json()
    assert conflict["operation"] == "migrate_main"
    assert conflict["source_path"] == str(source_pdf)
    assert conflict["target_path"] == str(existing)
    assert conflict["migration"]["total"] == 1
    assert source_pdf.exists()
    assert existing.read_text(encoding="utf-8") == "existing"


def test_update_paper_saves_notes(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "library.sqlite3")
    monkeypatch.setattr(app_module, "CONFIG_PATH", tmp_path / "config.json")
    client = TestClient(create_app())
    with app_module.connect() as conn:
        conn.execute(
            """
            INSERT INTO papers (
              id, file_path, file_name, file_size, modified_at, imported_at,
              title, status, updated_at
            ) VALUES ('p1', 'paper.pdf', 'paper.pdf', 1, '2026', '2026',
              'A paper', 'ready', '2026')
            """
        )

    response = client.patch(
        "/api/papers/p1",
        json={"title": "A paper", "notes": "待精读；已引用在第 3 章", "authors": ["Li"], "publication_date": "2024"},
    )

    assert response.status_code == 200
    assert response.json()["notes"] == "待精读；已引用在第 3 章"
    assert client.get("/api/state").json()["papers"][0]["notes"] == "待精读；已引用在第 3 章"


def test_partition_lookup_auto_confirms_unique_source_and_reviews_newcomer_combos(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "library.sqlite3")
    monkeypatch.setattr(app_module, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(app_module, "PARTITION_SOURCE_DIR", tmp_path / "no_partition_sources")
    client = TestClient(create_app())
    with app_module.connect() as conn:
        conn.execute(
            """
            INSERT INTO papers (
              id, file_path, file_name, file_size, modified_at, imported_at,
              title, notes, journal_name, status, updated_at
            ) VALUES ('p1', 'paper.pdf', 'paper.pdf', 1, '2026', '2026',
              'A paper', 'Handwritten note', 'Shared Journal', 'ready', '2026')
            """
        )
        normalized = journal_partitions.normalize_journal_name("Shared Journal")
        conn.execute(
            """INSERT INTO journal_partitions_2025
               (normalized_name, journal_name, partition, is_top)
               VALUES (?, 'Shared Journal', '1区', 1)""",
            (normalized,),
        )
        conn.executemany(
            """INSERT INTO journal_partitions_2026
               (normalized_name, journal_name, subject, partition)
               VALUES (?, 'Shared Journal', ?, ?)""",
            [(normalized, "材料科学", "1区"), (normalized, "医学", "2区")],
        )
        conn.execute(
            """INSERT INTO tag_suggestions
               (id, paper_id, name, reason, status, created_at)
               VALUES ('ai1', 'p1', '电化学', 'AI 推荐', 'pending', '2026')"""
        )

    response = client.post("/api/papers/p1/lookup-partition", json={})

    assert response.status_code == 200
    paper = response.json()["paper"]
    assert paper["notes"] == "Handwritten note"
    assert [item["name"] for item in paper["confirmed_partition_labels"]] == ["中科一区"]
    assert [item["category"] for item in paper["partition_suggestions"]] == [
        "newcomer_combo", "newcomer_combo"
    ]
    assert [item["name"] for item in paper["partition_suggestions"]] == [
        "医学 · 新锐二区", "材料科学 · 新锐一区"
    ]
    assert paper["tag_suggestions"][0]["name"] == "电化学"

    combo = paper["partition_suggestions"][0]
    approved = client.post(f"/api/partition-suggestions/{combo['id']}/approve")
    assert approved.status_code == 200
    assert [item["name"] for item in approved.json()["confirmed_partition_labels"]] == [
        "医学", "中科一区", "新锐二区"
    ]
    assert approved.json()["tags"] == []
    assert len(approved.json()["partition_suggestions"]) == 1

    ignored = approved.json()["partition_suggestions"][0]
    rejected = client.post(f"/api/partition-suggestions/{ignored['id']}/reject")
    assert rejected.status_code == 200
    assert ignored["id"] not in {item["id"] for item in rejected.json()["partition_suggestions"]}
    assert rejected.json()["tag_suggestions"][0]["name"] == "电化学"


def test_ai_config_connection_test_uses_masked_saved_key_without_saving(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "library.sqlite3")
    monkeypatch.setattr(app_module, "CONFIG_PATH", tmp_path / "config.json")
    client = TestClient(create_app())
    saved = {"base_url": "https://saved.example", "api_key": "saved-secret", "model": "saved-model"}
    assert client.put("/api/config", json=saved).status_code == 200

    received: dict[str, object] = {}

    async def fake_call_ai(config, messages):
        received["config"] = config
        received["messages"] = messages
        return {"ok": True}

    monkeypatch.setattr(app_module, "call_ai", fake_call_ai)
    draft = client.get("/api/config").json()
    draft.update({"base_url": "https://draft.example", "model": "draft-model"})
    response = client.post("/api/config/test", json=draft)

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "message": "AI 连接成功。"}
    assert received["config"] == {
        "base_url": "https://draft.example",
        "api_key": "saved-secret",
        "model": "draft-model",
    }
    assert app_module.read_config(mask_key=False) == saved


def test_partition_batch_supports_unchecked_scope_and_legacy_full_scope(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "library.sqlite3")
    monkeypatch.setattr(app_module, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(app_module, "PARTITION_BATCH_STATUS", {
        "status": "idle", "scope": "all", "total": 0, "processed": 0,
        "matched": 0, "unmatched": 0, "failed": 0, "cancel_requested": False,
        "failed_items": [],
    })
    client = TestClient(create_app())
    with app_module.connect() as conn:
        conn.executemany(
            """INSERT INTO papers (
                 id, file_path, file_name, file_size, modified_at, imported_at,
                 title, partition_checked_at, status, updated_at
               ) VALUES (?, ?, ?, 1, '2026', '2026', ?, ?, 'ready', '2026')""",
            [
                ("unchecked", "unchecked.pdf", "unchecked.pdf", "Unchecked paper", None),
                ("checked", "checked.pdf", "checked.pdf", "Checked paper", "2026-01-01"),
            ],
        )

    looked_up: list[str] = []

    async def fake_lookup(paper_id):
        looked_up.append(paper_id)
        return {"matched": True}

    monkeypatch.setattr(app_module, "lookup_paper_partition", fake_lookup)
    unchecked = client.post("/api/partitions/lookup-all", json={"scope": "unchecked"})
    assert unchecked.status_code == 200
    assert unchecked.json()["scope"] == "unchecked"
    assert unchecked.json()["total"] == 1
    assert looked_up == ["unchecked"]

    looked_up.clear()
    legacy = client.post("/api/partitions/lookup-all")
    assert legacy.status_code == 200
    assert legacy.json()["scope"] == "all"
    assert legacy.json()["total"] == 2
    assert looked_up == ["unchecked", "checked"]


def test_partition_batch_collects_failure_details_and_stops_after_current_paper(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "library.sqlite3")
    monkeypatch.setattr(app_module, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(app_module, "PARTITION_BATCH_STATUS", {
        "status": "idle", "scope": "all", "total": 0, "processed": 0,
        "matched": 0, "unmatched": 0, "failed": 0, "cancel_requested": False,
        "failed_items": [],
    })
    client = TestClient(create_app())
    with app_module.connect() as conn:
        conn.executemany(
            """INSERT INTO papers (
                 id, file_path, file_name, file_size, modified_at, imported_at,
                 title, status, updated_at
               ) VALUES (?, ?, ?, 1, '2026', '2026', ?, 'ready', '2026')""",
            [
                ("p1", "p1.pdf", "p1.pdf", "First paper"),
                ("p2", "p2.pdf", "p2.pdf", "Second paper"),
                ("p3", "p3.pdf", "p3.pdf", "Third paper"),
            ],
        )

    looked_up: list[str] = []

    async def fake_lookup(paper_id):
        looked_up.append(paper_id)
        if paper_id == "p1":
            raise RuntimeError("temporary provider error")
        with app_module.PARTITION_BATCH_LOCK:
            app_module.PARTITION_BATCH_STATUS["cancel_requested"] = True
        return {"matched": False}

    monkeypatch.setattr(app_module, "lookup_paper_partition", fake_lookup)
    response = client.post("/api/partitions/lookup-all", json={"scope": "all"})

    assert response.status_code == 200
    status = client.get("/api/partitions/lookup-all/status").json()
    assert looked_up == ["p1", "p2"]
    assert status["status"] == "cancelled"
    assert status["processed"] == 2
    assert status["failed"] == 1
    assert status["failed_items"] == [{"title": "First paper", "message": "temporary provider error"}]
    assert response.json()["total"] == 3


def test_partition_batch_cancel_endpoint_marks_running_job(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "library.sqlite3")
    monkeypatch.setattr(app_module, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(app_module, "PARTITION_BATCH_STATUS", {
        "status": "running", "scope": "all", "total": 2, "processed": 1,
        "matched": 1, "unmatched": 0, "failed": 0, "cancel_requested": False,
        "failed_items": [],
    })
    client = TestClient(create_app())

    response = client.post("/api/partitions/lookup-all/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "running"
    assert response.json()["cancel_requested"] is True


def test_partition_suggestion_recheck_preserves_choices_and_replaces_stale_candidates() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        """INSERT INTO papers (
             id, file_path, file_name, file_size, modified_at, imported_at, status, updated_at
           ) VALUES ('p1', 'paper.pdf', 'paper.pdf', 1, '2026', '2026', 'ready', '2026')"""
    )
    initial = {
        "cas_partition_2025": "1区",
        "cas_top_2025": True,
        "newcomer_partitions_2026": [
            {"subject": "医学", "partition": "1区"},
            {"subject": "材料科学", "partition": "2区"},
            {"subject": "医学", "partition": "1区"},
        ],
    }
    journal_partitions.sync_partition_suggestions(conn, "p1", initial, "2026-01-01")
    conn.execute("UPDATE paper_partition_suggestions SET status = 'approved' WHERE name = '医学 · 新锐一区'")
    conn.execute("UPDATE paper_partition_suggestions SET status = 'rejected' WHERE name = '材料科学 · 新锐二区'")
    original_ids = {
        row["candidate_key"]: row["id"]
        for row in conn.execute("SELECT candidate_key, id FROM paper_partition_suggestions")
    }

    journal_partitions.sync_partition_suggestions(
        conn,
        "p1",
        {
            **initial,
            "newcomer_partitions_2026": [
                {"subject": "医学", "partition": "1区"},
                {"subject": "计算机科学", "partition": "3区"},
            ],
        },
        "2026-01-02",
    )

    rows = conn.execute("SELECT * FROM paper_partition_suggestions ORDER BY sort_order, name").fetchall()
    by_key = {row["candidate_key"]: row for row in rows}
    cas_key = '["cas_partition","1区"]'
    medicine_key = '["newcomer_combo","医学","1区"]'
    new_key = '["newcomer_combo","计算机科学","3区"]'
    stale_key = '["newcomer_combo","材料科学","2区"]'
    assert by_key[cas_key]["id"] == original_ids[cas_key]
    assert by_key[cas_key]["status"] == "approved"
    assert by_key[medicine_key]["id"] == original_ids[medicine_key]
    assert by_key[medicine_key]["status"] == "approved"
    assert by_key[new_key]["status"] == "pending"
    assert stale_key not in by_key
    assert conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0] == 0


def test_unique_pending_partition_becomes_approved_only_on_explicit_sync() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        """INSERT INTO papers (id, file_path, file_name, file_size, modified_at,
             imported_at, status, updated_at)
           VALUES ('p1', 'paper.pdf', 'paper.pdf', 1, '2026', '2026', 'ready', '2026')"""
    )
    result = {
        "cas_partition_2025": "1区", "cas_top_2025": False,
        "newcomer_partitions_2026": [{"subject": "医学", "partition": "2区"}],
    }
    journal_partitions.sync_partition_suggestions(conn, "p1", result, auto_approve_unique=False)
    assert conn.execute(
        "SELECT COUNT(*) FROM paper_partition_suggestions WHERE status = 'pending'"
    ).fetchone()[0] == 3
    journal_partitions.sync_partition_suggestions(conn, "p1", result)
    assert conn.execute(
        "SELECT COUNT(*) FROM paper_partition_suggestions WHERE status = 'approved'"
    ).fetchone()[0] == 3
    conn.execute("UPDATE paper_partition_suggestions SET status = 'rejected' WHERE category = 'subject'")
    journal_partitions.sync_partition_suggestions(conn, "p1", result)
    assert conn.execute(
        "SELECT status FROM paper_partition_suggestions WHERE category = 'subject'"
    ).fetchone()[0] == "rejected"


def test_cas_source_migration_preserves_same_name_different_partitions() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        """CREATE TABLE journal_partitions_2025 (
             normalized_name TEXT PRIMARY KEY, journal_name TEXT NOT NULL,
             partition TEXT NOT NULL, is_top INTEGER NOT NULL DEFAULT 0,
             open_access TEXT)"""
    )
    source_dir = Path(__file__).resolve().parents[1] / "assets" / "partition_tables"
    journal_partitions.import_partition_sources(conn, source_dir)
    matches = journal_partitions.lookup_partition_records(conn, "Science Education")
    assert {item["partition"] for item in matches["cas_matches"]} == {"2", "3"}
    assert matches["cas_partition_2025"] is None
    assert journal_partitions.import_partition_sources(conn, source_dir) is None


def test_cas_ambiguity_is_single_choice_while_unique_newcomer_is_approved(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "library.sqlite3")
    monkeypatch.setattr(app_module, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(app_module, "PARTITION_SOURCE_DIR", tmp_path / "no_partition_sources")
    client = TestClient(create_app())
    normalized = journal_partitions.normalize_journal_name("Shared Journal")
    with app_module.connect() as conn:
        conn.execute(
            """INSERT INTO papers (id, file_path, file_name, file_size, modified_at,
                 imported_at, journal_name, status, updated_at)
               VALUES ('p1', 'paper.pdf', 'paper.pdf', 1, '2026', '2026',
                 'Shared Journal', 'ready', '2026')"""
        )
        conn.executemany(
            """INSERT INTO journal_partitions_2025
               (normalized_name, journal_name, partition, is_top)
               VALUES (?, 'Shared Journal', ?, ?)""",
            [(normalized, "1区", 1), (normalized, "2区", 0)],
        )
        conn.execute(
            """INSERT INTO journal_partitions_2026
               (normalized_name, journal_name, subject, partition)
               VALUES (?, 'Shared Journal', '医学', '1区')""",
            (normalized,),
        )
    first = client.post("/api/papers/p1/lookup-partition", json={}).json()["paper"]
    assert [item["name"] for item in first["partition_suggestions"]] == ["中科一区", "中科二区"]
    assert [item["name"] for item in first["confirmed_partition_labels"]] == ["医学", "新锐一区"]
    assert first["cas_partition_2025"] is None

    chosen = client.post(
        f"/api/partition-suggestions/{first['partition_suggestions'][0]['id']}/approve"
    ).json()
    assert chosen["cas_partition_2025"] == "1区"
    assert chosen["cas_top_2025"] is True
    assert chosen["partition_suggestions"] == []
    assert [item["name"] for item in chosen["confirmed_partition_labels"]] == [
        "医学", "中科一区", "新锐一区"
    ]
    repeated = client.post("/api/papers/p1/lookup-partition", json={}).json()["paper"]
    assert repeated["partition_suggestions"] == []
    assert [item["id"] for item in repeated["confirmed_partition_labels"]] == [
        item["id"] for item in chosen["confirmed_partition_labels"]
    ]


def test_newcomer_combos_can_be_selected_together_without_duplicate_badges(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "library.sqlite3")
    monkeypatch.setattr(app_module, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(app_module, "PARTITION_SOURCE_DIR", tmp_path / "no_partition_sources")
    client = TestClient(create_app())
    normalized = journal_partitions.normalize_journal_name("Shared Journal")
    with app_module.connect() as conn:
        conn.execute(
            """INSERT INTO papers (id, file_path, file_name, file_size, modified_at,
                 imported_at, journal_name, status, updated_at)
               VALUES ('p1', 'paper.pdf', 'paper.pdf', 1, '2026', '2026',
                 'Shared Journal', 'ready', '2026')"""
        )
        conn.executemany(
            """INSERT INTO journal_partitions_2026
               (normalized_name, journal_name, subject, partition)
               VALUES (?, 'Shared Journal', '医学', ?)""",
            [(normalized, "1区"), (normalized, "2区")],
        )
    first = client.post("/api/papers/p1/lookup-partition", json={}).json()["paper"]
    assert len(first["partition_suggestions"]) == 2
    for candidate in first["partition_suggestions"]:
        response = client.post(f"/api/partition-suggestions/{candidate['id']}/approve")
        assert response.status_code == 200
    final = response.json()
    assert [item["name"] for item in final["confirmed_partition_labels"]] == [
        "医学", "新锐一区", "新锐二区"
    ]
    assert final["partition_suggestions"] == []


def test_saved_partition_results_migrate_to_review_candidates_without_lookup() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        """INSERT INTO papers (
             id, file_path, file_name, file_size, modified_at, imported_at,
             cas_partition_2025, cas_top_2025, newcomer_partitions_2026_json,
             partition_checked_at, status, updated_at
           ) VALUES ('p1', 'paper.pdf', 'paper.pdf', 1, '2026', '2026',
             '1区', 1, '[{"subject":"医学","partition":"2区"}]',
             '2026-01-01', 'ready', '2026')"""
    )

    assert journal_partitions.migrate_stored_partition_suggestions(conn) == 1
    rows = conn.execute(
        "SELECT category, name, status FROM paper_partition_suggestions ORDER BY sort_order, name"
    ).fetchall()
    assert [(row["category"], row["name"], row["status"]) for row in rows] == [
        ("subject", "医学", "pending"),
        ("cas_partition", "中科一区", "pending"),
        ("newcomer_partition", "新锐二区", "pending"),
    ]
    conn.execute("UPDATE paper_partition_suggestions SET status = 'rejected'")
    assert journal_partitions.migrate_stored_partition_suggestions(conn) == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM paper_partition_suggestions WHERE status = 'pending'"
    ).fetchone()[0] == 0


def test_partition_suggestion_display_migration_updates_labels_and_removes_top() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        """INSERT INTO papers (
             id, file_path, file_name, file_size, modified_at, imported_at, status, updated_at
           ) VALUES ('p1', 'paper.pdf', 'paper.pdf', 1, '2026', '2026', 'ready', '2026')"""
    )
    conn.execute(
        """INSERT INTO paper_partition_suggestions (
             id, paper_id, candidate_key, category, name, reason, status, created_at, updated_at
           ) VALUES
             ('subject1', 'p1', '[\"subject\",\"医学\"]', 'subject', '学科：医学', '来自 2026 新锐期刊分区名单', 'approved', '2026', '2026'),
             ('cas1', 'p1', '[\"cas_partition\",\"1区\"]', 'cas_partition', '中科院1区', '来自 2025 中科院期刊分区名单', 'rejected', '2026', '2026'),
             ('new1', 'p1', '[\"newcomer_partition\",\"医学\",\"2区\"]', 'newcomer_partition', '新锐2区（医学）', '来自 2026 新锐期刊分区名单', 'approved', '2026', '2026'),
             ('top1', 'p1', '[\"cas_top\",\"1\"]', 'cas_top', 'Top', '', 'approved', '2026', '2026')"""
    )

    assert journal_partitions.migrate_partition_suggestion_display(conn) == 4
    rows = conn.execute(
        "SELECT category, name, reason, status FROM paper_partition_suggestions ORDER BY category"
    ).fetchall()
    assert [(row["category"], row["name"], row["status"]) for row in rows] == [
        ("cas_partition", "中科一区", "rejected"),
        ("newcomer_partition", "新锐二区", "approved"),
        ("subject", "医学", "approved"),
    ]
    assert rows[1]["reason"] == "对应学科：医学；来自 2026 新锐期刊分区名单"
    assert journal_partitions.migrate_partition_suggestion_display(conn) == 0


def test_legacy_partition_note_cleanup_is_precise_and_runs_once() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        """INSERT INTO papers (
             id, file_path, file_name, file_size, modified_at, imported_at, notes, status, updated_at
           ) VALUES ('p1', 'paper.pdf', 'paper.pdf', 1, '2026', '2026',
             'Handwritten intro\n\n中科院分区（2025）：中科院1区 Top\nhandwritten after\n新锐分区（2026）：医学 新锐1区\n',
             'ready', '2026')"""
    )

    assert journal_partitions.remove_legacy_partition_notes(conn) == 1
    notes = conn.execute("SELECT notes FROM papers WHERE id = 'p1'").fetchone()["notes"]
    assert notes == "Handwritten intro\n\nhandwritten after\n"

    conn.execute("UPDATE papers SET notes = notes || '中科院分区（2025）：后来添加的文本\n' WHERE id = 'p1'")
    assert journal_partitions.remove_legacy_partition_notes(conn) == 0
    assert "后来添加的文本" in conn.execute(
        "SELECT notes FROM papers WHERE id = 'p1'"
    ).fetchone()["notes"]


def test_stored_path_is_relative_inside_base_and_round_trips(tmp_path) -> None:
    target = tmp_path / "library_files" / "A Great Paper.pdf"

    stored = to_stored_path(target)

    assert not Path(stored).is_absolute()
    assert stored == str(Path("library_files") / "A Great Paper.pdf")
    assert resolve_stored_path(stored) == target


def test_stored_path_keeps_absolute_form_outside_base(tmp_path, monkeypatch) -> None:
    outside = tmp_path / "outside"
    monkeypatch.setattr(app_module, "PATH_BASE", tmp_path / "project")
    (tmp_path / "project").mkdir()
    external = outside / "somewhere-else.pdf"

    stored = to_stored_path(external)

    assert Path(stored).is_absolute()
    assert resolve_stored_path(stored) == external


def test_normalize_stored_paths_is_idempotent(tmp_path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    inside = tmp_path / "library_files" / "inside.pdf"
    conn.execute(
        """
        INSERT INTO papers (
          id, file_path, file_name, file_size, modified_at, imported_at, status, updated_at
        ) VALUES ('p1', ?, 'inside.pdf', 1, '2026', '2026', 'ready', '2026')
        """,
        (str(inside),),
    )

    first = normalize_stored_paths(conn)
    second = normalize_stored_paths(conn)

    assert first["papers"] == 1
    assert second == {"papers": 0, "attachments": 0, "skipped": 0}
    row = conn.execute("SELECT file_path FROM papers WHERE id = 'p1'").fetchone()
    assert row["file_path"] == to_stored_path(inside)
    marker = conn.execute(
        "SELECT value FROM app_meta WHERE key = ?", (app_module.PATH_MIGRATED_KEY,)
    ).fetchone()
    assert marker is not None


def test_normalize_stored_paths_skips_outside_and_missing_entries(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "PATH_BASE", tmp_path / "project")
    (tmp_path / "project").mkdir()
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    inside = tmp_path / "project" / "library_files" / "inside.pdf"
    outside = tmp_path / "elsewhere" / "outside.pdf"
    conn.execute(
        """
        INSERT INTO papers (
          id, file_path, file_name, file_size, modified_at, imported_at, status, updated_at
        ) VALUES ('p1', ?, 'inside.pdf', 1, '2026', '2026', 'ready', '2026')
        """,
        (str(inside),),
    )
    conn.execute(
        """
        INSERT INTO papers (
          id, file_path, file_name, file_size, modified_at, imported_at, status, updated_at
        ) VALUES ('p2', ?, 'outside.pdf', 1, '2026', '2026', 'ready', '2026')
        """,
        (str(outside),),
    )
    conn.execute(
        """
        INSERT INTO papers (
          id, file_path, file_name, file_size, modified_at, imported_at, status, updated_at
        ) VALUES ('p3', 'library_files/already.pdf', 'already.pdf', 1, '2026', '2026', 'ready', '2026')
        """
    )

    stats = normalize_stored_paths(conn)

    assert stats["papers"] == 1
    assert stats["skipped"] == 1
    rows = {row["id"]: row["file_path"] for row in conn.execute("SELECT id, file_path FROM papers")}
    assert rows["p1"] == to_stored_path(inside)
    assert rows["p2"] == str(outside)
    assert rows["p3"] == "library_files/already.pdf"


def test_upsert_paper_deduplicates_relative_and_absolute_forms(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "library.sqlite3")
    monkeypatch.setattr(app_module, "CONFIG_PATH", tmp_path / "config.json")
    pdf = tmp_path / "library_files" / "paper.pdf"
    pdf.parent.mkdir()
    pdf.write_text("pdf", encoding="utf-8")
    with app_module.connect() as conn:
        conn.executescript(SCHEMA)
        conn.execute(
            """
            INSERT INTO papers (
              id, file_path, file_name, file_size, modified_at, imported_at, status, updated_at
            ) VALUES ('p1', ?, 'paper.pdf', 3, '2026', '2026', 'ready', '2026')
            """,
            (to_stored_path(pdf),),
        )

    existing_id, created = app_module.upsert_paper(str(pdf))

    assert created is False
    assert existing_id == "p1"
    with app_module.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM papers").fetchone()["n"] == 1


def _window(hwnd: int, pid: int, title: str, class_name: str = "SUMATRA_PDF_FRAME") -> win_focus.WindowInfo:
    return win_focus.WindowInfo(hwnd, pid, title, class_name)


def _pick(windows, **overrides):
    options = {
        "launched_pid": None,
        "before": set(),
        "hints": [],
        "reader_exe": None,
        "pid_executables": {},
    }
    options.update(overrides)
    return win_focus.best_candidate(windows, **options)[0]


def test_title_hints_prefer_metadata_title_over_file_name() -> None:
    hints = win_focus.title_hints(
        Path("D:/sample-papers/sample-paper-16.pdf"),
        "Asymmetric supercapacitor for sensitive elastic-electrochemical stress sensor",
    )

    assert hints[0] == "asymmetric supercapacitor for sensitive elastic-electrochemical stress sensor"
    assert "sample-paper-16.pdf" in hints
    assert "sample-paper-16" in hints


def test_title_hints_drop_values_too_short_to_match_safely() -> None:
    assert win_focus.title_hints(Path("a.b")) == []


def test_pick_best_window_prefers_launched_process_over_title_match() -> None:
    windows = [
        _window(1, 100, "SumatraPDF"),
        _window(2, 200, "sample-paper-16.pdf - SumatraPDF"),
    ]

    best = _pick(windows, launched_pid=100, before={2}, hints=["sample-paper-16.pdf"])

    assert best == windows[0]


def test_pick_best_window_matches_metadata_title_when_instance_is_reused() -> None:
    # 复用实例时没有新进程号，窗口标题是 PDF 元数据标题（本机实测 SumatraPDF 的行为）
    hints = win_focus.title_hints(
        Path("D:/sample-papers/sample-paper-16.pdf"),
        "Asymmetric supercapacitor for sensitive elastic-electrochemical stress sensor",
    )
    windows = [_window(5, 300, "Asymmetric supercapacitor for sensitive elastic-electrochemical stress sensor - SumatraPDF")]

    assert _pick(windows, before={5}, hints=hints) == windows[0]


def test_pick_best_window_falls_back_to_reader_executable() -> None:
    windows = [
        _window(9, 300, "SumatraPDF"),
        _window(8, 400, "抖音-记录美好生活 - Microsoft Edge"),
    ]

    best = _pick(
        windows,
        before={8, 9},
        hints=["sample-paper-16.pdf"],
        reader_exe=r"C:\Users\ExampleUser\AppData\Local\SumatraPDF\SumatraPDF.exe",
        pid_executables={300: r"C:\Users\ExampleUser\AppData\Local\SumatraPDF\SumatraPDF.exe", 400: r"C:\msedge.exe"},
    )

    assert best == windows[0]


def test_pick_best_window_ignores_unrelated_windows() -> None:
    windows = [_window(1, 10, "Paper_mangement - Visual Studio Code")]

    assert _pick(windows, hints=["sample-paper-16.pdf"]) is None


def test_wait_for_best_candidate_prefers_strong_signal_over_early_weak_one() -> None:
    # 实测踩过的坑：阅读器新窗口还没建出来时，已经开着的旧窗口会先"合格"
    old_window = _window(1, 300, "23afm-1.pdf - [...] - SumatraPDF")
    new_window = _window(2, 400, "23ea.pdf - [...] - SumatraPDF")
    polls: list[int] = []

    def picker():
        polls.append(1)
        if len(polls) < 3:
            return old_window, win_focus.SCORE_READER_EXE
        return new_window, win_focus.SCORE_LAUNCHED_PID + win_focus.SCORE_NEW_WINDOW

    chosen = win_focus.wait_for_best_candidate(picker, timeout=1.0, grace=1.0)

    assert chosen == new_window
    assert len(polls) == 3


def test_wait_for_best_candidate_falls_back_to_weak_candidate_after_grace() -> None:
    weak = _window(1, 300, "SumatraPDF")

    chosen = win_focus.wait_for_best_candidate(lambda: (weak, win_focus.SCORE_READER_EXE), timeout=2.0, grace=0.0)

    assert chosen == weak


def test_wait_for_best_candidate_returns_none_when_nothing_matches() -> None:
    assert win_focus.wait_for_best_candidate(lambda: (None, 0), timeout=0.0) is None


def test_pick_explorer_window_prefers_exact_title_and_explorer_class() -> None:
    windows = [
        _window(1, 10, "论文 - 文件资源管理器", class_name="CabinetWClass"),
        _window(2, 10, "论文", class_name="NotExplorer"),
        _window(3, 10, "论文", class_name="CabinetWClass"),
    ]

    window = win_focus.pick_explorer_window(windows, Path("C:/Users/ExampleUser/Desktop/我的文章/我的文章/论文"))

    assert window == windows[2]


def test_pick_explorer_window_falls_back_to_substring_title() -> None:
    windows = [_window(1, 10, "论文 - 文件资源管理器", class_name="CabinetWClass")]

    assert win_focus.pick_explorer_window(windows, Path("C:/x/论文")) == windows[0]
    assert win_focus.pick_explorer_window(windows, Path("C:/x/其他文件夹")) is None


def test_pick_new_explorer_window_requires_new_handle_and_matching_folder() -> None:
    folder = Path("C:/Users/ExampleUser/Desktop/我的文章/我的文章/论文")
    windows = [
        _window(1, 10, "论文", class_name="CabinetWClass"),
        _window(2, 10, "下载", class_name="CabinetWClass"),
    ]

    assert win_focus.pick_new_explorer_window(windows, folder, before={1}) is None
    assert win_focus.pick_new_explorer_window(windows, folder, before=set()) == windows[0]


def test_reveal_in_explorer_always_selects_file_when_folder_window_exists(monkeypatch) -> None:
    path = Path("C:/Users/ExampleUser/Desktop/论文/paper.pdf")
    existing = _window(1, 10, "论文", class_name="CabinetWClass")
    popen_calls: list[str] = []
    thread_calls: list[tuple[object, tuple[object, ...], bool | None]] = []

    class FakeThread:
        def __init__(self, *, target, args, daemon=None):
            thread_calls.append((target, args, daemon))

        def start(self) -> None:
            pass

    monkeypatch.setattr(win_focus, "IS_WINDOWS", True)
    monkeypatch.setattr(win_focus, "_reset_caches", lambda: None)
    monkeypatch.setattr(win_focus, "_enum_top_level_windows", lambda: [existing])
    monkeypatch.setattr(win_focus.subprocess, "Popen", lambda args: popen_calls.append(args))
    monkeypatch.setattr(win_focus.threading, "Thread", FakeThread)

    win_focus.reveal_in_explorer(path)

    assert popen_calls == [f'explorer.exe /select,"{path.resolve()}"']
    assert len(thread_calls) == 1
    target, args, daemon = thread_calls[0]
    assert target == win_focus._focus_revealed_explorer_window
    assert args == (path.parent, {existing.hwnd}, existing)
    assert daemon is True


def test_reveal_in_explorer_selects_file_without_existing_folder_window(monkeypatch) -> None:
    path = Path("C:/Users/ExampleUser/Desktop/论文/paper.pdf")
    other = _window(1, 10, "下载", class_name="CabinetWClass")
    popen_calls: list[str] = []
    thread_calls: list[tuple[object, tuple[object, ...], bool | None]] = []

    class FakeThread:
        def __init__(self, *, target, args, daemon=None):
            thread_calls.append((target, args, daemon))

        def start(self) -> None:
            pass

    monkeypatch.setattr(win_focus, "IS_WINDOWS", True)
    monkeypatch.setattr(win_focus, "_reset_caches", lambda: None)
    monkeypatch.setattr(win_focus, "_enum_top_level_windows", lambda: [other])
    monkeypatch.setattr(win_focus.subprocess, "Popen", lambda args: popen_calls.append(args))
    monkeypatch.setattr(win_focus.threading, "Thread", FakeThread)

    win_focus.reveal_in_explorer(path)

    assert popen_calls == [f'explorer.exe /select,"{path.resolve()}"']
    assert len(thread_calls) == 1
    target, args, daemon = thread_calls[0]
    assert target == win_focus._focus_revealed_explorer_window
    assert args == (path.parent, {other.hwnd}, None)
    assert daemon is True


def test_reveal_in_explorer_quotes_only_selected_path(monkeypatch) -> None:
    path = Path("C:/Users/ExampleUser/Desktop/论文 with spaces/paper file.pdf")
    popen_calls: list[str] = []

    class FakeThread:
        def __init__(self, *, target, args, daemon=None):
            pass

        def start(self) -> None:
            pass

    monkeypatch.setattr(win_focus, "IS_WINDOWS", True)
    monkeypatch.setattr(win_focus, "_reset_caches", lambda: None)
    monkeypatch.setattr(win_focus, "_enum_top_level_windows", lambda: [])
    monkeypatch.setattr(win_focus.subprocess, "Popen", lambda args: popen_calls.append(args))
    monkeypatch.setattr(win_focus.threading, "Thread", FakeThread)

    win_focus.reveal_in_explorer(path)

    assert popen_calls == [f'explorer.exe /select,"{path.resolve()}"']


def test_open_document_executes_once_then_focuses_in_background(monkeypatch) -> None:
    path = Path("C:/Users/ExampleUser/Desktop/paper.pdf")
    existing = _window(1, 10, "论文", class_name="AcrobatSDIWindow")
    opened: list[Path] = []
    thread_calls: list[tuple[object, tuple[object, ...], bool | None]] = []

    class FakeThread:
        def __init__(self, *, target, args, daemon=None):
            thread_calls.append((target, args, daemon))

        def start(self) -> None:
            pass

    monkeypatch.setattr(win_focus, "IS_WINDOWS", True)
    monkeypatch.setattr(win_focus, "_reset_caches", lambda: None)
    monkeypatch.setattr(win_focus, "_enum_top_level_windows", lambda: [existing])
    monkeypatch.setattr(win_focus, "_shell_execute", lambda target: opened.append(target) or 42)
    monkeypatch.setattr(win_focus.threading, "Thread", FakeThread)

    win_focus.open_document(path)

    assert opened == [path]
    assert thread_calls == [(win_focus._focus_pdf_window, (path, 42, {existing.hwnd}), True)]


def test_open_folder_reuses_existing_explorer_window(monkeypatch) -> None:
    folder = Path("C:/Users/ExampleUser/Desktop/论文")
    existing = _window(1, 10, "论文", class_name="CabinetWClass")
    popen_calls: list[str] = []
    thread_calls: list[tuple[object, tuple[object, ...], bool | None]] = []

    class FakeThread:
        def __init__(self, *, target, args, daemon=None):
            thread_calls.append((target, args, daemon))

        def start(self) -> None:
            pass

    monkeypatch.setattr(win_focus, "IS_WINDOWS", True)
    monkeypatch.setattr(win_focus, "_reset_caches", lambda: None)
    monkeypatch.setattr(win_focus, "_enum_top_level_windows", lambda: [existing])
    monkeypatch.setattr(win_focus.subprocess, "Popen", lambda args: popen_calls.append(args))
    monkeypatch.setattr(win_focus.threading, "Thread", FakeThread)

    win_focus.open_folder(folder)

    assert popen_calls == []
    assert len(thread_calls) == 1
    target, args, daemon = thread_calls[0]
    assert target == win_focus._activate_until_foreground
    assert args == (existing.hwnd, False)
    assert daemon is True


def test_open_folder_opens_and_focuses_new_explorer_window(monkeypatch) -> None:
    folder = Path("C:/Users/ExampleUser/Desktop/论文 with spaces")
    other = _window(1, 10, "下载", class_name="CabinetWClass")
    popen_calls: list[str] = []
    thread_calls: list[tuple[object, tuple[object, ...], bool | None]] = []

    class FakeThread:
        def __init__(self, *, target, args, daemon=None):
            thread_calls.append((target, args, daemon))

        def start(self) -> None:
            pass

    monkeypatch.setattr(win_focus, "IS_WINDOWS", True)
    monkeypatch.setattr(win_focus, "_reset_caches", lambda: None)
    monkeypatch.setattr(win_focus, "_enum_top_level_windows", lambda: [other])
    monkeypatch.setattr(win_focus.subprocess, "Popen", lambda args: popen_calls.append(args))
    monkeypatch.setattr(win_focus.threading, "Thread", FakeThread)

    win_focus.open_folder(folder)

    assert popen_calls == [f'explorer.exe "{folder.resolve()}"']
    assert len(thread_calls) == 1
    target, args, daemon = thread_calls[0]
    assert target == win_focus._focus_new_explorer_window
    assert args == (folder.resolve(), {other.hwnd})
    assert daemon is True
