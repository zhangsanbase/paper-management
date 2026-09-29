from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import backend.app as app_module
from backend import win_focus
from backend.services import pdf_viewer


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "library.sqlite3")
    monkeypatch.setattr(app_module, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(app_module, "PDF_VIEWER_CONFIG_PATH", tmp_path / "pdf_viewer.json")
    monkeypatch.setattr(app_module, "LIBRARY_DIR", tmp_path / "library_files")
    monkeypatch.setattr(app_module, "PATH_BASE", tmp_path)
    monkeypatch.setattr(app_module.platform, "system", lambda: "Windows")
    return TestClient(app_module.create_app())


def _add_paper_and_attachments(tmp_path: Path) -> tuple[Path, Path, Path]:
    paper = tmp_path / "paper.pdf"
    supplement = tmp_path / "supplement.PDF"
    spreadsheet = tmp_path / "data.xlsx"
    for path in (paper, supplement, spreadsheet):
        path.write_bytes(b"test")
    with app_module.connect() as conn:
        conn.execute(
            """INSERT INTO papers (
                 id, file_path, file_name, file_size, modified_at, imported_at, status, updated_at
               ) VALUES ('p1', ?, 'paper.pdf', 4, '2026', '2026', 'ready', '2026')""",
            (str(paper),),
        )
        for attachment_id, path in (("pdf", supplement), ("sheet", spreadsheet)):
            conn.execute(
                """INSERT INTO paper_attachments (
                     id, paper_id, file_path, file_name, kind, created_at, updated_at
                   ) VALUES (?, 'p1', ?, ?, 'supplementary', '2026', '2026')""",
                (attachment_id, str(path), path.name),
            )
    return paper, supplement, spreadsheet


def test_settings_default_custom_roundtrip_and_ai_config_is_separate(
    client: TestClient, tmp_path: Path,
) -> None:
    default = client.get("/api/pdf-viewer")
    assert default.status_code == 200
    assert default.json() == {
        "mode": "system", "executable_path": None, "effective_mode": "system",
        "selected_available": False, "supported": True, "warning": None,
    }
    assert not (tmp_path / "pdf_viewer.json").exists()

    executable = tmp_path / "My Reader.exe"
    executable.write_bytes(b"reader")
    chosen = client.put("/api/pdf-viewer", json={"mode": "custom", "executable_path": str(executable)})
    assert chosen.status_code == 200
    assert chosen.json()["effective_mode"] == "custom"
    assert chosen.json()["executable_path"] == str(executable.resolve())
    assert json.loads((tmp_path / "pdf_viewer.json").read_text(encoding="utf-8"))["mode"] == "custom"
    assert not (tmp_path / "config.json").exists()

    assert client.put("/api/pdf-viewer", json={"mode": "system"}).json()["effective_mode"] == "system"
    restored = client.put("/api/pdf-viewer", json={"mode": "custom"})
    assert restored.status_code == 200
    assert restored.json()["effective_mode"] == "custom"
    assert client.get("/api/pdf-viewer").json()["executable_path"] == str(executable.resolve())


def test_invalid_program_and_cancelled_selection_keep_previous_settings(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = client.put("/api/pdf-viewer", json={"mode": "custom", "executable_path": str(tmp_path / "missing.exe")})
    assert missing.status_code == 422
    wrong_type = tmp_path / "reader.txt"
    wrong_type.write_text("reader", encoding="utf-8")
    assert client.put("/api/pdf-viewer", json={"mode": "custom", "executable_path": str(wrong_type)}).status_code == 422
    assert client.get("/api/pdf-viewer").json()["mode"] == "system"

    calls: list[bool] = []

    def choose(_title: str, _filetypes: list[tuple[str, str]], multiple: bool = True) -> list[str]:
        calls.append(multiple)
        return []

    monkeypatch.setattr(app_module, "choose_local_files", choose)
    assert client.post("/api/pdf-viewer/select").json() == {"cancelled": True, "path": None}
    assert calls == [False]
    assert not (tmp_path / "pdf_viewer.json").exists()

    executable = tmp_path / "reader.exe"
    executable.write_bytes(b"reader")
    monkeypatch.setattr(app_module, "choose_local_files", lambda *_args, **_kwargs: [str(executable)])
    assert client.post("/api/pdf-viewer/select").json() == {"cancelled": False, "path": str(executable)}
    assert not (tmp_path / "pdf_viewer.json").exists()


def test_main_and_pdf_attachment_use_custom_reader_non_pdf_keeps_default(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    paper, supplement, spreadsheet = _add_paper_and_attachments(tmp_path)
    executable = tmp_path / "reader.exe"
    executable.write_bytes(b"reader")
    assert client.put("/api/pdf-viewer", json={"mode": "custom", "executable_path": str(executable)}).status_code == 200
    pdf_calls: list[tuple[Path, Path | None]] = []
    other_calls: list[Path] = []

    def open_document(path: Path, reader_executable: Path | None = None) -> None:
        pdf_calls.append((path, reader_executable))

    monkeypatch.setattr(win_focus, "open_document", open_document)
    monkeypatch.setattr(app_module, "open_local_file", lambda path: other_calls.append(path))

    assert client.post("/api/papers/p1/open").status_code == 200
    assert client.post("/api/papers/p1/attachments/pdf/open").status_code == 200
    assert client.post("/api/papers/p1/attachments/sheet/open").status_code == 200
    assert pdf_calls == [(paper, executable.resolve()), (supplement, executable.resolve())]
    assert other_calls == [spreadsheet]


def test_missing_reader_falls_back_and_warns_without_changing_preference(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    paper, _, _ = _add_paper_and_attachments(tmp_path)
    executable = tmp_path / "reader.exe"
    executable.write_bytes(b"reader")
    assert client.put("/api/pdf-viewer", json={"mode": "custom", "executable_path": str(executable)}).status_code == 200
    executable.unlink()

    opened: list[Path] = []
    monkeypatch.setattr(win_focus, "open_document", lambda path: opened.append(path))
    response = client.post("/api/papers/p1/open")
    assert response.status_code == 200
    assert "已失效" in response.json()["message"]
    assert opened == [paper]
    settings = client.get("/api/pdf-viewer").json()
    assert settings["mode"] == "custom"
    assert settings["effective_mode"] == "system"
    assert settings["warning"]


def test_launch_failure_is_reported_without_fallback(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_paper_and_attachments(tmp_path)
    executable = tmp_path / "reader.exe"
    executable.write_bytes(b"reader")
    assert client.put("/api/pdf-viewer", json={"mode": "custom", "executable_path": str(executable)}).status_code == 200
    calls: list[Path | None] = []

    def fail(_path: Path, reader_executable: Path | None = None) -> None:
        calls.append(reader_executable)
        raise OSError("无法启动")

    monkeypatch.setattr(win_focus, "open_document", fail)
    response = client.post("/api/papers/p1/open")
    assert response.status_code == 500
    assert "无法启动" in response.json()["detail"]
    assert calls == [executable.resolve()]


def test_reader_disappearing_during_launch_uses_system_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    preference = tmp_path / "pdf_viewer.json"
    executable = tmp_path / "reader.exe"
    executable.write_bytes(b"reader")
    pdf_viewer.save_settings(preference, "custom", str(executable), "Windows")
    calls: list[Path | None] = []

    def open_document(_path: Path, reader_executable: Path | None = None) -> None:
        calls.append(reader_executable)
        if reader_executable is not None:
            raise FileNotFoundError("reader vanished")

    monkeypatch.setattr(win_focus, "open_document", open_document)
    message = pdf_viewer.open_pdf(tmp_path / "paper.pdf", preference, "Windows")
    assert "已使用系统默认程序" in message
    assert calls == [executable.resolve(), None]


def test_custom_launch_passes_pdf_as_one_argument_and_focuses_selected_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf = tmp_path / "paper with spaces.pdf"
    reader = tmp_path / "My Reader.exe"
    launches: list[list[str]] = []
    focus_calls: list[tuple[object, tuple[object, ...], bool]] = []
    monkeypatch.setattr(win_focus, "IS_WINDOWS", True)
    monkeypatch.setattr(win_focus, "_enum_top_level_windows", lambda: [])
    monkeypatch.setattr(win_focus.subprocess, "Popen", lambda args: launches.append(args) or SimpleNamespace(pid=42))

    class FakeThread:
        def __init__(self, target: object, args: tuple[object, ...], daemon: bool) -> None:
            focus_calls.append((target, args, daemon))

        def start(self) -> None:
            pass

    monkeypatch.setattr(win_focus.threading, "Thread", FakeThread)
    win_focus.open_document(pdf, reader_executable=reader)

    assert launches == [[str(reader), str(pdf)]]
    assert focus_calls == [(win_focus._focus_pdf_window, (pdf, 42, set(), str(reader)), True)]


def test_non_windows_still_uses_system_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    preference = tmp_path / "pdf_viewer.json"
    preference.write_text(json.dumps({"mode": "custom", "executable_path": str(tmp_path / "reader.exe")}), encoding="utf-8")
    launches: list[list[str]] = []
    monkeypatch.setattr(pdf_viewer.subprocess, "Popen", lambda args: launches.append(args))
    pdf = tmp_path / "paper.pdf"
    assert pdf_viewer.open_pdf(pdf, preference, "Linux") == "已打开 PDF。"
    assert launches == [["xdg-open", str(pdf)]]
    assert pdf_viewer.read_settings(preference, "Linux")["effective_mode"] == "system"
