from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import Qt

from codex_history_viewer.gui import MainWindow, SessionTableModel, _clear_selection_for_root_change, _export_scope_sessions, _filter_sessions, _preview_target, _prune_selection_for_catalog
from codex_history_viewer.importer import import_export_package
from codex_history_viewer.exporter import _INLINE_IMAGE_CHUNK_SIZE, _copy_file_with_cancel, _copy_inline_image, _copy_related_attachments, _validate_zip_archive, _write_attachment, _write_json_export, _write_unique_json_file, export_batch, export_one, redact_text, safe_name, unique_directory
from codex_history_viewer.reader import Catalog, DatabaseSnapshot, SessionInfo, TableSchema, _AttachmentListingBudget, _append_codex_attachment_refs, _codex_attachment_ids, _discover_database_item_sessions, _enable_sqlite_cancellation, _index_entries, _inspect_history, _is_link_or_junction, _list_attachment_folder_files, _read_metadata, _scan_rollout_file, _search_database_fallback, _search_database_fallback_many, scan_codex_home, load_session_events


def dump_line(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"


def envelope(record_type: str, payload: object, ordinal: int, timestamp: str = "2026-01-01T00:00:00Z") -> dict:
    return {"type": record_type, "timestamp": timestamp, "ordinal": ordinal, "payload": payload}


class HistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "codex"
        self.root.mkdir()
        self.out = Path(self.temp.name) / "exports"
        self.out.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_chat_fixture(self) -> tuple[str, Path]:
        sid = "11111111-1111-4111-8111-111111111111"
        session_dir = self.root / "sessions" / "2026" / "01" / "01"
        session_dir.mkdir(parents=True)
        path = session_dir / f"rollout-2026-01-01T00-00-00-{sid}.jsonl"
        user = {"type": "message", "id": "u1", "role": "user", "content": [{"type": "input_text", "text": "你好，密码=TopSecret123"}, {"type": "input_image", "image_url": "C:\\not-found\\missing.png"}]}
        call = {"type": "custom_tool_call", "id": "tool1", "call_id": "call-1", "name": "exec_command", "input": "echo hello"}
        output = {"type": "custom_tool_call_output", "id": "out1", "call_id": "call-1", "output": [{"type": "output_text", "text": "stdout: hello"}]}
        final = {"type": "message", "id": "a1", "role": "assistant", "phase": "final_answer", "content": [{"type": "output_text", "text": "结果 <script>alert(1)</script> 完成"}]}
        lines = [
            envelope("session_meta", {"session_id": sid, "timestamp": "2026-01-01T00:00:00Z", "cwd": "C:\\repo"}, 0),
            envelope("response_item", user, 1),
            envelope("response_item", call, 2),
            envelope("event_msg", {"type": "item_completed", "item": call}, 3),
            envelope("response_item", output, 4),
            envelope("response_item", final, 5, "2026-01-01T00:02:00Z"),
            envelope("response_item", final, 6, "2026-01-01T00:02:00Z"),
            envelope("turn_aborted", {"reason": "sample"}, 7),
        ]
        with path.open("w", encoding="utf-8", newline="") as stream:
            for item in lines:
                stream.write(dump_line(item))
            stream.write("{broken json line\n")
        folder = self.root / "attachments" / sid
        folder.mkdir(parents=True)
        (folder / "pasted-text.txt").write_text("pasted text\napi_key=AttachmentSecret", encoding="utf-8")
        (self.root / "attachments" / "unrelated-session" ).mkdir()
        (self.root / "attachments" / "unrelated-session" / "private.txt").write_text("not related")
        return sid, path

    def test_generic_titles_fall_back_to_first_user_message_only(self) -> None:
        ids = [
            "33333333-3333-4333-8333-333333333333",
            "44444444-4444-4444-8444-444444444444",
            "55555555-5555-4555-8555-555555555555",
        ]
        (self.root / "session_index.jsonl").write_text(
            dump_line({"id": ids[0], "thread_name": "未命名绘画"})
            + dump_line({"id": ids[1], "thread_name": "已命名标题"}),
            encoding="utf-8",
        )
        session_dir = self.root / "sessions" / "2026" / "01" / "03"
        session_dir.mkdir(parents=True)
        records = {
            ids[0]: [
                envelope("session_meta", {"session_id": ids[0]}, 0),
                envelope("response_item", {"type": "message", "id": "a-first", "role": "assistant", "content": "助手内容不能作为标题"}, 1),
                envelope("response_item", {"type": "custom_tool_call", "id": "t-first", "name": "exec_command", "input": "工具命令不能作为标题"}, 2),
                envelope("response_item", {"type": "message", "id": "u-first", "role": "user", "content": [{"type": "input_text", "text": "请检查这个中文问题"}, {"type": "input_text", "text": "第二行标题应被压成一行"}]}, 3),
                envelope("response_item", {"type": "message", "id": "u-second", "role": "user", "content": "后续消息不能替换标题"}, 4),
            ],
            ids[1]: [
                envelope("session_meta", {"session_id": ids[1]}, 0),
                envelope("response_item", {"type": "message", "id": "u-named", "role": "user", "content": "首条消息不同于标题"}, 1),
            ],
            ids[2]: [
                envelope("session_meta", {"session_id": ids[2]}, 0),
                envelope("response_item", {"type": "message", "id": "a-only", "role": "assistant", "content": "没有用户消息"}, 1),
                envelope("response_item", {"type": "custom_tool_call", "id": "t-only", "name": "exec_command", "input": "echo hidden"}, 2),
            ],
        }
        for sid, entries in records.items():
            path = session_dir / f"rollout-2026-01-03T00-00-00-{sid}.jsonl"
            path.write_text("".join(dump_line(entry) for entry in entries), encoding="utf-8")

        catalog = scan_codex_home(str(self.root))
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))
        sessions = {item.session_id: item for item in catalog.sessions}
        self.assertEqual(sessions[ids[0]].title, "请检查这个中文问题 第二行标题应被压成一行")
        self.assertEqual(sessions[ids[1]].title, "已命名标题")
        self.assertEqual(sessions[ids[2]].title, "未命名会话")

    def test_database_title_fallback_uses_detected_record_order(self) -> None:
        import sqlite3

        sid = "db-first-user-title"
        database = self.root / "state_title_fallback.sqlite"
        connection = sqlite3.connect(database)
        self.addCleanup(connection.close)
        connection.execute(
            "CREATE TABLE conversation_metadata(session_id TEXT, title TEXT, created_at_ms INTEGER, updated_at_ms INTEGER)"
        )
        connection.execute(
            "CREATE TABLE conversation_items(thread_id TEXT, rollout_ordinal INTEGER, data TEXT)"
        )
        connection.execute("INSERT INTO conversation_metadata VALUES(?,?,?,?)", (sid, "Untitled", 1767225600000, 1767225660000))
        connection.execute(
            "INSERT INTO conversation_items VALUES(?,?,?)",
            (sid, 2, json.dumps({"type": "userMessage", "text": "第二条用户消息"}, ensure_ascii=False)),
        )
        connection.execute(
            "INSERT INTO conversation_items VALUES(?,?,?)",
            (sid, 1, json.dumps({"type": "userMessage", "text": "第一条用户消息"}, ensure_ascii=False)),
        )
        connection.commit()
        connection.close()

        catalog = scan_codex_home(str(self.root))
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))
        session = next(item for item in catalog.sessions if item.session_id == sid)
        self.assertEqual(session.title, "第一条用户消息")

    def test_generic_rollout_title_can_use_first_user_message_from_sqlite(self) -> None:
        import sqlite3

        sid = "rollout-with-sqlite-user-title"
        (self.root / "session_index.jsonl").write_text(
            dump_line({"id": sid, "thread_name": "未命名会话"}), encoding="utf-8"
        )
        session_dir = self.root / "sessions" / "2026" / "01" / "04"
        session_dir.mkdir(parents=True)
        rollout = session_dir / f"rollout-2026-01-04T00-00-00-{sid}.jsonl"
        rollout.write_text(
            dump_line(envelope("session_meta", {"session_id": sid}, 0))
            + dump_line(envelope("response_item", {"type": "message", "id": "assistant-only", "role": "assistant", "content": "rollout 助手消息"}, 1)),
            encoding="utf-8",
        )

        database = self.root / "state_rollout_title.sqlite"
        connection = sqlite3.connect(database)
        connection.execute(
            "CREATE TABLE conversation_metadata(session_id TEXT, title TEXT, created_at_ms INTEGER, updated_at_ms INTEGER)"
        )
        connection.execute(
            "CREATE TABLE conversation_items(thread_id TEXT, rollout_ordinal INTEGER, data TEXT)"
        )
        connection.execute("INSERT INTO conversation_metadata VALUES(?,?,?,?)", (sid, "未命名会话", 1767225600000, 1767225660000))
        connection.execute(
            "INSERT INTO conversation_items VALUES(?,?,?)",
            (sid, 1, json.dumps({"type": "userMessage", "text": "数据库里的首条用户消息"}, ensure_ascii=False)),
        )
        connection.commit()
        connection.close()

        catalog = scan_codex_home(str(self.root))
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))
        session = next(item for item in catalog.sessions if item.session_id == sid)
        self.assertEqual(session.title, "数据库里的首条用户消息")
        self.assertIn("SQLite 标题回退", session.source)
        self.assertEqual(session.assistant_count, 1)

    def test_constructed_archive_duplicate_damage_attachment_and_exports(self) -> None:
        sid, raw = self.write_chat_fixture()
        archive_id = "22222222-2222-4222-8222-222222222222"
        archived = self.root / "archived_sessions"
        archived.mkdir()
        try:
            import zstandard
        except ImportError:
            self.skipTest("zstandard dependency is not installed")
        data = dump_line(envelope("session_meta", {"session_id": archive_id, "timestamp": "2025-12-31T23:59:00Z", "cwd": "C:\\archived"}, 0))
        data += dump_line(envelope("response_item", {"type": "message", "id": "arch-u", "role": "user", "content": [{"type": "input_text", "text": "归档会话"}]}, 1))
        compressed = zstandard.ZstdCompressor().compress(data.encode("utf-8"))
        archive_path = archived / f"rollout-2025-12-31T23-59-00-{archive_id}.jsonl.zst"
        archive_path.write_bytes(compressed)

        catalog = scan_codex_home(str(self.root))
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))
        chat = next(item for item in catalog.sessions if item.session_id == sid)
        archive = next(item for item in catalog.sessions if item.session_id == archive_id)
        self.assertEqual(chat.title[:2], "你好")
        self.assertEqual(chat.user_count, 1)
        self.assertEqual(chat.assistant_count, 1)
        self.assertEqual(chat.tool_count, 1)
        self.assertGreaterEqual(chat.duplicate_count, 1)
        self.assertTrue(any("损坏" in value for value in chat.issues))
        self.assertTrue(chat.has_attachment)
        self.assertEqual(chat.disk_size_bytes, raw.stat().st_size + (self.root / "attachments" / sid / "pasted-text.txt").stat().st_size)
        self.assertEqual(chat.disk_size_status, "已统计")
        self.assertTrue(archive.archived)
        self.assertEqual(archive.user_count, 1)
        self.assertEqual(archive.disk_size_bytes, archive_path.stat().st_size)
        archive_export = export_one(archive, catalog, str(self.out), ["zip"], "full", False, True, threading.Event())
        self.assertTrue(archive_export["status"].startswith("完成"))
        compressed_copy = next((Path(archive_export["folder"]) / "原始文件").glob("*.jsonl.zst"))
        self.assertEqual(hashlib.sha256(archive_path.read_bytes()).digest(), hashlib.sha256(compressed_copy.read_bytes()).digest())

        events, issues, _ = load_session_events(chat, catalog)
        visible = [event for event in events if event.get("kind") == "message"]
        self.assertEqual(len(visible), 2)
        call = next(event for event in events if event.get("kind") == "tool_call")
        result = next(event for event in events if event.get("kind") == "tool_result")
        self.assertEqual(call["call_id"], result["call_id"])
        self.assertTrue(any("未找到" in value for value in issues))
        preview_events, _, _ = load_session_events(chat, catalog, include_raw_data=False)
        self.assertTrue(preview_events)
        self.assertTrue(all("data" not in event for event in preview_events))
        self.assertTrue(any(event.get("text") for event in preview_events))

        export = export_one(chat, catalog, str(self.out), ["html", "json", "zip", "raw"], "full", True, True, threading.Event())
        self.assertTrue(export["status"].startswith("完成"))
        folder = Path(export["folder"])
        raw_copy = next((folder / "原始文件").glob("*.jsonl"))
        self.assertEqual(hashlib.sha256(raw.read_bytes()).hexdigest(), hashlib.sha256(raw_copy.read_bytes()).hexdigest())
        html_text = (folder / "聊天记录.html").read_text(encoding="utf-8")
        self.assertIn("&lt;script&gt;", html_text)
        self.assertNotIn("<script>alert", html_text)
        self.assertIn("[已脱敏:", (folder / "聊天记录.json").read_text(encoding="utf-8"))
        self.assertFalse(any("unrelated-session" in item for item in (p.as_posix() for p in folder.rglob("*"))))
        with zipfile.ZipFile(export["zip"]) as archive_file:
            self.assertTrue(archive_file.testzip() is None)
            self.assertTrue(any(name.endswith("manifest.json") for name in archive_file.namelist()))

    def test_disk_usage_marks_skipped_nested_link_as_partial(self) -> None:
        sid, _raw = self.write_chat_fixture()
        nested = self.root / "attachments" / sid / "linked-content"
        nested.mkdir()
        (nested / "outside.txt").write_text("must not be counted", encoding="utf-8")

        with patch("codex_history_viewer.reader._is_link_or_junction", side_effect=lambda path: Path(path) == nested):
            catalog = scan_codex_home(str(self.root))
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))

        session = next(item for item in catalog.sessions if item.session_id == sid)
        self.assertEqual(session.disk_size_status, "部分统计")
        self.assertTrue(any("junction，已跳过" in issue for issue in session.issues))

    def test_attachment_permission_error_is_not_reported_as_missing(self) -> None:
        sid = "permission-checked-attachment"
        attachment_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        attachment_folder = self.root / "attachments" / attachment_id
        attachment_folder.mkdir(parents=True)
        (attachment_folder / "image.png").write_bytes(b"image")
        session_dir = self.root / "sessions" / "2026" / "01" / "01"
        session_dir.mkdir(parents=True)
        rollout = session_dir / f"rollout-2026-01-01T00-00-00-{sid}.jsonl"
        rollout.write_text(
            dump_line(envelope("session_meta", {"session_id": sid}, 0))
            + dump_line(envelope("response_item", {
                "type": "message", "id": "user-image", "role": "user",
                "content": [{"type": "input_image", "image_url": f"attachments/{attachment_id}/image.png"}],
            }, 1)),
            encoding="utf-8",
        )
        real_lstat = Path.lstat

        def denied_lstat(path: Path):
            if path == attachment_folder:
                raise PermissionError(13, "simulated access denied", str(path))
            return real_lstat(path)

        with patch.object(Path, "lstat", denied_lstat):
            catalog = scan_codex_home(str(self.root))
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))

        session = next(item for item in catalog.sessions if item.session_id == sid)
        self.assertEqual(session.disk_size_status, "部分统计")
        self.assertTrue(any("附件目录检查失败" in issue and "PermissionError" in issue for issue in session.issues))
        self.assertFalse(any("未找到关联附件目录" in issue for issue in session.issues))

    def test_disk_summary_separates_incomplete_from_missing_files(self) -> None:
        from types import SimpleNamespace

        class Label:
            def __init__(self) -> None:
                self.value = ""

            def setText(self, value: str) -> None:
                self.value = value

        label = Label()
        window = SimpleNamespace(
            visible_sessions=[
                SessionInfo("cancelled", disk_size_status="未能完整统计"),
                SessionInfo("missing", disk_size_status="未找到本地记录文件"),
                SessionInfo("shared", disk_size_status="共享数据库 / 索引"),
                SessionInfo("counted", disk_size_bytes=123, disk_size_status="已统计"),
            ],
            disk_usage_label=label,
        )

        MainWindow._update_disk_usage_summary(window)

        self.assertIn("未能完整统计 1 条", label.value)
        self.assertIn("未找到本地记录文件 1 条", label.value)
        self.assertIn("共享数据库 / 索引未分摊 1 条", label.value)
        self.assertIn("共享附件可能重复计入", label.value)

    def test_import_exported_json_package_creates_scannable_local_catalog(self) -> None:
        sid = "12345678-1234-4123-8123-123456789abc"
        package = self.root / "package" / "导入会话_" / "manifest.json"
        package.parent.mkdir(parents=True)
        manifest = {
            "tool_version": "1.0.39",
            "session_id": sid,
            "title": "跨电脑导入标题",
            "platform": "codex",
            "complete": True,
        }
        package.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        (package.parent / "聊天记录.json").write_text(
            json.dumps(
                {
                    "session": {"session_id": sid, "title": "跨电脑导入标题", "raw_created_at": "2026-01-01T00:00:00Z", "cwd": "C:/imported"},
                    "events": [
                        {"kind": "message", "role": "user", "text": "导入后的用户消息", "raw_timestamp": "2026-01-01T00:00:01Z"},
                        {"kind": "message", "role": "assistant", "text": "导入后的助手消息", "raw_timestamp": "2026-01-01T00:00:02Z"},
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        destination = Path(self.temp.name) / "import-library"
        root, report = import_export_package(str(package.parent.parent), str(destination), threading.Event())
        self.assertEqual(report["imported_sessions"], 1)
        self.assertEqual(report["reconstructed_sessions"], 1)
        catalog = scan_codex_home(root)
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))
        session = next(item for item in catalog.sessions if item.session_id == sid)
        self.assertEqual(session.title, "跨电脑导入标题")
        events, issues, _ = load_session_events(session, catalog)
        texts = [event.get("text") for event in events if event.get("kind") == "message"]
        self.assertIn("导入后的用户消息", texts)
        self.assertIn("导入后的助手消息", texts)
        self.assertEqual(session.status, "导入包重建")
        self.assertIn("导入包重建", session.source)
        self.assertTrue(any("不是原始 rollout" in issue for issue in issues))

    def test_import_checks_manifest_hashes_and_cleans_failed_staging(self) -> None:
        sid = "bc123456-1234-4123-8123-123456789abc"
        package_dir = self.root / "bad-hash-package" / "session"
        package_dir.mkdir(parents=True)
        payload = package_dir / "聊天记录.json"
        payload.write_text('{"session":{"session_id":"' + sid + '"},"events":[]}', encoding="utf-8")
        manifest = {
            "session_id": sid,
            "title": "哈希校验",
            "complete": True,
            "files": [{"path": "聊天记录.json", "sha256": "0" * 64}],
        }
        (package_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        destination = Path(self.temp.name) / "import-library-hash"
        with self.assertRaisesRegex(ValueError, "完整性校验失败"):
            import_export_package(str(package_dir.parent), str(destination), threading.Event())
        self.assertEqual(list(destination.iterdir()), [])

    def test_import_preserves_all_raw_rollouts_byte_for_byte(self) -> None:
        sid = "cd123456-1234-4123-8123-123456789abc"
        package_dir = self.root / "multi-raw-package" / "会话"
        raw_dir = package_dir / "原始文件"
        raw_dir.mkdir(parents=True)
        raw_contents = [
            dump_line(envelope("session_meta", {"session_id": sid, "timestamp": "2026-01-01T00:00:00Z"}, 0))
            + dump_line(envelope("response_item", {"type": "message", "id": "raw-user", "role": "user", "content": "原始记录第一段"}, 1)),
            dump_line(envelope("session_meta", {"session_id": sid, "timestamp": "2026-01-01T00:00:00Z"}, 0))
            + dump_line(envelope("response_item", {"type": "message", "id": "raw-assistant", "role": "assistant", "content": "原始记录第二段"}, 2)),
        ]
        raw_paths = [raw_dir / "rollout-part-a.jsonl", raw_dir / "rollout-part-b.jsonl"]
        for path, content in zip(raw_paths, raw_contents):
            path.write_text(content, encoding="utf-8")
        file_rows = [
            {"path": path.relative_to(package_dir).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in raw_paths
        ]
        (package_dir / "manifest.json").write_text(
            json.dumps({"session_id": sid, "title": "多段原始记录", "complete": True, "files": file_rows}, ensure_ascii=False),
            encoding="utf-8",
        )
        destination = Path(self.temp.name) / "import-library-raw"
        root, report = import_export_package(str(package_dir.parent), str(destination), threading.Event())
        self.assertEqual(report["integrity_files_checked"], 2)
        catalog = scan_codex_home(root)
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))
        session = next(item for item in catalog.sessions if item.session_id == sid)
        self.assertIn("跨电脑导入（原始记录副本）", session.source)
        copied = sorted((Path(root) / "sessions" / "imported" / sid).glob("*.jsonl"))
        self.assertEqual(len(copied), 2)
        self.assertEqual(
            sorted(hashlib.sha256(path.read_bytes()).hexdigest() for path in copied),
            sorted(hashlib.sha256(path.read_bytes()).hexdigest() for path in raw_paths),
        )
        events, _issues, _counts = load_session_events(session, catalog)
        messages = [event["text"] for event in events if event.get("kind") == "message"]
        self.assertEqual(messages, ["原始记录第一段", "原始记录第二段"])

    def test_import_reconnects_json_attachment_paths(self) -> None:
        sid = "ef123456-1234-4123-8123-123456789abc"
        package_dir = self.root / "attachment-package" / "会话"
        attachments = package_dir / "attachments"
        attachments.mkdir(parents=True)
        attachment = attachments / "picture.png"
        attachment.write_bytes(b"small-image-fixture")
        json_path = package_dir / "聊天记录.json"
        json_path.write_text(
            json.dumps({
                "session": {"session_id": sid, "title": "附件重建"},
                "events": [{
                    "kind": "message", "role": "user", "text": "看这张图",
                    "attachments": [{"kind": "image", "path": "attachments/picture.png", "status": "已导出"}],
                }],
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        files = [json_path, attachment]
        (package_dir / "manifest.json").write_text(json.dumps({
            "session_id": sid,
            "title": "附件重建",
            "complete": True,
            "files": [{"path": path.relative_to(package_dir).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in files],
        }, ensure_ascii=False), encoding="utf-8")
        destination = Path(self.temp.name) / "import-library-attachments"
        root, _report = import_export_package(str(package_dir.parent), str(destination), threading.Event())
        catalog = scan_codex_home(root)
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))
        session = next(item for item in catalog.sessions if item.session_id == sid)
        events, issues, _counts = load_session_events(session, catalog)
        user_message = next(event for event in events if event.get("kind") == "message")
        self.assertEqual(user_message["attachments"][0]["status"], "本地文件存在")
        self.assertTrue((Path(root) / "attachments" / sid / "picture.png").is_file())
        self.assertFalse(any("附件未找到" in issue for issue in issues))

    def test_exported_zip_imports_with_verified_manifest_and_raw_hash(self) -> None:
        sid, raw = self.write_chat_fixture()
        catalog = scan_codex_home(str(self.root))
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))
        session = next(item for item in catalog.sessions if item.session_id == sid)
        exported = export_one(session, catalog, str(self.out), ["json", "zip", "raw"], "full", False, True, threading.Event())
        self.assertTrue(exported["status"].startswith("完成"))
        destination = Path(self.temp.name) / "zip-import-library"
        imported_root, report = import_export_package(exported["zip"], str(destination), threading.Event())
        self.assertGreater(report["integrity_files_checked"], 0)
        imported_catalog = scan_codex_home(imported_root)
        self.addCleanup(lambda: __import__("shutil").rmtree(imported_catalog.temp_dir, ignore_errors=True))
        imported_session = next(item for item in imported_catalog.sessions if item.session_id == sid)
        self.assertIn("跨电脑导入（原始记录副本）", imported_session.source)
        copied_raw = next((Path(imported_root) / "sessions" / "imported" / sid).glob("*.jsonl"))
        self.assertEqual(hashlib.sha256(raw.read_bytes()).digest(), hashlib.sha256(copied_raw.read_bytes()).digest())
        imported_events, _issues, _counts = load_session_events(imported_session, imported_catalog)
        self.assertTrue(any(event.get("text", "").startswith("你好") for event in imported_events))

    def test_import_rejects_zip_path_traversal(self) -> None:
        import zipfile

        archive = Path(self.temp.name) / "bad-import.zip"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("../outside.txt", "blocked")
            output.writestr("session/manifest.json", "{}")
        with self.assertRaises(ValueError):
            import_export_package(str(archive), str(Path(self.temp.name) / "destination"), threading.Event())
        self.assertFalse((Path(self.temp.name).parent / "outside.txt").exists())

    def test_filter_uses_pre_normalized_search_text_and_obeys_filters_and_cancel(self) -> None:
        active = SessionInfo(
            session_id="active-id",
            title="Hello World",
            created_at="2026-01-10 12:00:00",
            search_text="中文正文 password=secret",
        )
        archived = SessionInfo(
            session_id="archived-id",
            title="Old thread",
            created_at="2025-12-20 12:00:00",
            archived=True,
            search_text="旧记录",
        )
        unknown_date = SessionInfo(session_id="unknown-date-id", title="Date not found")
        sessions = [active, archived, unknown_date]

        matched = _filter_sessions(sessions, "中文", "active", True, "2026-01-01", "2026-01-31", threading.Event())
        self.assertEqual(matched, [active])
        matched_dates_only = _filter_sessions(sessions, "", "all", True, "2026-01-01", "2026-01-31", threading.Event())
        self.assertEqual(matched_dates_only, [active])
        matched_by_title = _filter_sessions(sessions, "HELLO", "all", False, "", "", threading.Event())
        self.assertEqual(matched_by_title, [active])
        matched_archived = _filter_sessions(sessions, "", "archived", False, "", "", threading.Event())
        self.assertEqual(matched_archived, [archived])
        cancelled = threading.Event()
        cancelled.set()
        self.assertIsNone(_filter_sessions(sessions, "", "all", False, "", "", cancelled))

    def test_platform_filter_and_visible_export_scope_keep_sources_separate(self) -> None:
        codex = SessionInfo("codex-id", title="Codex", platform="codex")
        chatgpt = SessionInfo("chatgpt-id", title="ChatGPT", platform="chatgpt")
        unknown = SessionInfo("unknown-id", title="Unknown")
        sessions = [codex, chatgpt, unknown]

        self.assertEqual(_filter_sessions(sessions, "", "all", False, "", "", threading.Event(), "codex"), [codex])
        self.assertEqual(_filter_sessions(sessions, "", "all", False, "", "", threading.Event(), "chatgpt"), [chatgpt])
        self.assertEqual(_filter_sessions(sessions, "", "all", False, "", "", threading.Event(), "unknown"), [unknown])
        self.assertEqual(_filter_sessions(sessions, "", "all", False, "", "", threading.Event(), "all"), sessions)

        visible = [chatgpt]
        self.assertEqual(_export_scope_sessions(sessions, visible, {"codex-id"}, True), [chatgpt])
        self.assertEqual(_export_scope_sessions(sessions, visible, {"codex-id"}, False), [codex])

    def test_visible_export_refuses_to_use_a_stale_filter_result(self) -> None:
        from types import SimpleNamespace

        window = SimpleNamespace(
            scan_worker=None,
            preview_worker=None,
            export_worker=None,
            catalog=Catalog(root=str(self.root)),
            visible_sessions=[SessionInfo("stale-codex", platform="codex")],
            selected_ids=set(),
            _filter_applied_generation=4,
            _filter_generation=5,
        )
        with patch("codex_history_viewer.gui.QMessageBox.information") as message:
            with patch("codex_history_viewer.gui.ExportOptions") as options:
                MainWindow._start_export(window, visible_only=True)

        message.assert_called_once()
        options.assert_not_called()

    def test_source_summary_separates_platform_from_model_and_missing_body(self) -> None:
        from types import SimpleNamespace

        class Label:
            text = ""

            def setText(self, value):
                self.text = value

        label = Label()
        catalog = Catalog(
            root=str(self.root),
            sessions=[
                SessionInfo("codex-gpt-model", platform="codex"),
                SessionInfo("chatgpt-metadata", platform="chatgpt", status="仅目录元数据"),
                SessionInfo("chatgpt-with-body", platform="chatgpt", status="读取完成"),
                SessionInfo("unknown"),
            ],
        )
        MainWindow._update_source_summary(SimpleNamespace(catalog=catalog, source_info_label=label))

        self.assertIn("Codex 1 条", label.text)
        self.assertIn("ChatGPT 网页 2 条", label.text)
        self.assertIn("仅目录元数据 1 条", label.text)
        self.assertIn("来源未知 1 条", label.text)
        self.assertIn("GPT 模型也仍属于 Codex", label.text)

    def test_checkbox_cell_is_clickable_and_updates_selection(self) -> None:
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        with patch.object(MainWindow, "start_scan", lambda _self: None):
            window = MainWindow()
        session = SessionInfo("checkbox-click-session")
        window.catalog = Catalog(root=str(self.root), sessions=[session])
        window.visible_sessions = [session]
        window.sessions_by_id = {session.session_id: session}
        window.table_model.set_sessions([session])
        window._render_rows()
        window.show()
        app.processEvents()

        cell = window.table.visualRect(window.table_model.index(0, 0))
        QTest.mouseClick(window.table.viewport(), Qt.MouseButton.LeftButton, pos=cell.center())
        app.processEvents()
        self.assertIn(session.session_id, window.selected_ids)
        QTest.mouseDClick(window.table.viewport(), Qt.MouseButton.LeftButton, pos=cell.center())
        app.processEvents()
        self.assertIn(session.session_id, window.selected_ids)
        QTest.mouseClick(window.table.viewport(), Qt.MouseButton.LeftButton, pos=cell.center())
        app.processEvents()
        self.assertNotIn(session.session_id, window.selected_ids)
        window.close()
        app.processEvents()

    def test_filter_cancellation_is_checked_for_each_session(self) -> None:
        cancel = threading.Event()
        checked_searches = 0

        class CancellingSearchText(str):
            def __contains__(self, needle: object) -> bool:
                nonlocal checked_searches
                checked_searches += 1
                cancel.set()
                return False

        sessions = [
            SessionInfo(session_id=f"session-{index}", search_text=CancellingSearchText("indexed body"))
            for index in range(1_000)
        ]
        result = _filter_sessions(sessions, "not-present", "all", False, "", "", cancel)

        self.assertIsNone(result)
        self.assertEqual(checked_searches, 1)

    def test_preview_uses_current_row_or_unambiguous_single_selection(self) -> None:
        first = SessionInfo("first")
        visible_selected = SessionInfo("visible-selected")
        visible_unselected = SessionInfo("visible-unselected")
        catalog = [first, visible_selected, visible_unselected]
        visible = [visible_selected, visible_unselected]

        self.assertIs(_preview_target(visible, catalog, {"first", "visible-selected"}, 0), visible_selected)
        self.assertIs(_preview_target(visible, catalog, {"first", "visible-selected"}, 1), visible_unselected)
        self.assertIs(_preview_target(visible, catalog, set(), 1), visible_unselected)
        self.assertIsNone(_preview_target(visible, catalog, {"first", "visible-selected"}, -1))
        self.assertIs(_preview_target(visible, catalog, {"first"}, -1), first)

    def test_session_table_shows_compact_columns_with_full_hover_details(self) -> None:
        session = SessionInfo(
            session_id="session-identifier-long",
            title="会话标题",
            created_at="2026-10-17 10:23:11",
            updated_at="2026-10-17 10:45:16",
            cwd=r"C:\Users\Example\project",
            source="rollout + SQLite 条目回退",
            status="部分可读，有报告项",
            platform="codex",
        )
        model = SessionTableModel(set())
        model.set_sessions([session])

        self.assertEqual(MainWindow.COLUMNS[5], "类别 / 来源 / 状态")
        self.assertEqual(model.data(model.index(0, 3)), "2026-10-17 10:23")
        self.assertEqual(model.data(model.index(0, 3), Qt.ItemDataRole.ToolTipRole), "2026-10-17 10:23:11")
        self.assertEqual(model.data(model.index(0, 5)), "Codex · rollout + SQLite · 部分可读，有报告项")
        self.assertEqual(
            model.data(model.index(0, 5), Qt.ItemDataRole.ToolTipRole),
            "会话类别：Codex\n数据来源：rollout + SQLite 条目回退\n读取状态：部分可读，有报告项",
        )
        self.assertEqual(model.data(model.index(0, 6)), r"C:\Users\Example\project")

    def test_catalog_root_change_clears_stale_session_selection(self) -> None:
        selected = {"session-one", "session-two"}
        original = Catalog(root=str(self.root))
        refresh = Catalog(root=str(self.root / "."))
        replacement = Catalog(root=str(self.root / "another-codex-home"))

        self.assertFalse(_clear_selection_for_root_change(original, refresh, selected))
        self.assertEqual(selected, {"session-one", "session-two"})
        self.assertTrue(_clear_selection_for_root_change(original, replacement, selected))
        self.assertEqual(selected, set())

        refresh.sessions = [SessionInfo("session-two")]
        selected = {"session-one", "session-two"}
        self.assertTrue(_prune_selection_for_catalog(refresh, selected))
        self.assertEqual(selected, {"session-two"})

    def test_attachment_id_extraction_handles_deep_nested_payloads(self) -> None:
        first = "11111111-1111-4111-8111-111111111111"
        second = "22222222-2222-4222-8222-222222222222"
        value: object = {"ignored-key": f"attachments/{first}/key.txt"}
        for _ in range(1100):
            value = [value]
        value = {"payload": [value, f"attachments/{first}/one.txt", {"nested": f"attachments/{second}/two.txt"}], first: "not traversed"}
        self.assertEqual(_codex_attachment_ids(value), {first, second})

    def test_html_attachment_link_encodes_url_reserved_characters(self) -> None:
        from codex_history_viewer.exporter import _format_html

        session = SessionInfo("html-link", "离线附件")
        events = [{
            "kind": "message",
            "role": "user",
            "text": "查看附件",
            "attachments": [{"path": "attachments/方案#2?50% 空格.png", "kind": "图片"}],
        }]
        rendered = _format_html(session, events, "full")

        self.assertIn(
            'href="attachments/%E6%96%B9%E6%A1%88%232%3F50%25%20%E7%A9%BA%E6%A0%BC.png"',
            rendered,
        )
        self.assertNotIn('href="attachments/方案#2?50% 空格.png"', rendered)

    def test_text_formatters_stop_when_cancelled_between_events(self) -> None:
        from codex_history_viewer.exporter import _format_html, _format_markdown, _format_txt

        cancel = threading.Event()

        class CancelOnString(str):
            def __new__(cls, value: str, event: threading.Event):
                result = super().__new__(cls, value)
                result.cancel_event = event
                return result

            def __str__(self) -> str:
                self.cancel_event.set()
                return super().__str__()

        events = [
            {"kind": "message", "role": "user", "text": CancelOnString("first", cancel), "attachments": []},
            {"kind": "message", "role": "assistant", "text": "second", "attachments": []},
        ]
        with self.assertRaises(InterruptedError):
            _format_html(SessionInfo("format-cancel"), events, "full", cancel=cancel)

        for formatter in (_format_txt, _format_markdown, _format_html):
            already_cancelled = threading.Event()
            already_cancelled.set()
            with self.assertRaises(InterruptedError):
                formatter(SessionInfo("format-cancel"), events, "full", cancel=already_cancelled)

    def test_streaming_formatters_match_compatibility_renderers(self) -> None:
        from codex_history_viewer.exporter import (
            _format_html, _format_markdown, _format_txt, _map_text_event,
            _iter_html_chunks, _iter_markdown_chunks, _iter_txt_chunks,
        )

        session = SessionInfo(
            "formatter-stream", "中文标题", created_at="本机时间", raw_created_at="原始时间",
            cwd="C:\\repo", archived=True, issues=["一条读取报告 <script>"],
        )
        events = [
            {"kind": "message", "role": "user", "text": "你好\n[[ATTACHMENT:old.png]]\n<script>alert(1)</script>", "attachments": [{"ref": "old.png", "path": "attachments/新 图.png", "kind": "图片"}]},
            {"kind": "tool_call", "name": "exec_command", "call_id": "call-1", "text": "echo 你好"},
            {"kind": "tool_result", "name": "stdout", "call_id": "call-1", "text": "输出\n完成"},
            {"kind": "status", "name": "turn_aborted", "text": "已中断"},
            {"kind": "unknown", "name": "future-v1", "text": "未知类型"},
        ]
        mapping = {"old.png": "attachments/新 图.png"}
        raw_payload = {"blob": "x" * 1024, "password": "not emitted in a text format"}
        projected = _map_text_event({"kind": "tool_call", "name": "tool", "text": "调用", "data": raw_payload}, mapping)
        self.assertNotIn("data", projected)
        self.assertEqual(raw_payload["password"], "not emitted in a text format")
        for mode in ("chat", "full"):
            self.assertEqual(
                _format_txt(session, events, mode, mapping),
                "".join(_iter_txt_chunks(session, events, mode, mapping)),
            )
            self.assertEqual(
                _format_markdown(session, events, mode, mapping),
                "".join(_iter_markdown_chunks(session, events, mode, mapping)),
            )
            self.assertEqual(
                _format_html(session, events, mode, mapping),
                "".join(_iter_html_chunks(session, events, mode, mapping)),
            )
            markdown = _format_markdown(session, events, mode, mapping)
            self.assertNotIn("[[ATTACHMENT:old.png]]", markdown)
            self.assertIn("attachments/新 图.png", markdown)

    def test_streaming_text_writer_cancels_between_generated_chunks(self) -> None:
        from codex_history_viewer.exporter import _write_text_export

        destination = self.out / "cancelled-stream.txt"
        cancel = threading.Event()

        def chunks():
            yield "first chunk"
            cancel.set()
            yield "unwritten chunk"

        with self.assertRaises(InterruptedError):
            _write_text_export(str(destination), chunks(), cancel)
        self.assertFalse(destination.exists())

    def test_markdown_untrusted_tool_text_uses_safe_fence_and_summary(self) -> None:
        from codex_history_viewer.exporter import _format_markdown, _iter_markdown_chunks

        text = "before\n```\n<script>alert(1)</script>\n````\nafter"
        events = [{
            "kind": "tool_result",
            "name": "x</summary><script>bad()</script>",
            "call_id": "call-unsafe",
            "text": text,
        }]
        rendered = _format_markdown(SessionInfo("markdown-fence"), events, "full")
        streamed = "".join(_iter_markdown_chunks(SessionInfo("markdown-fence"), events, "full"))
        self.assertEqual(rendered, streamed)
        self.assertIn("<summary>工具结果：x&lt;/summary&gt;&lt;script&gt;bad()&lt;/script&gt;（call_id=call-unsafe）</summary>", rendered)
        self.assertIn("`````text", rendered)
        self.assertEqual(rendered.count("`````"), 2)
        self.assertIn("<script>alert(1)</script>", rendered)
        self.assertEqual(list(self.out.glob("*.partial")), [])

    def test_item_completed_reuses_canonical_response_item_without_duplicate_output(self) -> None:
        session_file = self.root / "item-completed-dedupe.jsonl"
        large_text = "长正文" * 1000
        canonical_before_deferred = {
            "type": "message", "id": "canonical-first", "role": "assistant",
            "content": [{"type": "output_text", "text": large_text}],
        }
        canonical_after_deferred = {
            "type": "message", "id": "canonical-later", "role": "assistant",
            "content": [{"type": "output_text", "text": "规范消息优先"}],
        }
        deferred_only = {
            "type": "message", "id": "deferred-only", "role": "assistant",
            "content": [{"type": "output_text", "text": "仅有完成事件"}],
        }
        entries = [
            envelope("response_item", canonical_before_deferred, 1),
            envelope("event_msg", {"type": "item_completed", "item": canonical_before_deferred}, 2),
            envelope("event_msg", {"type": "item_completed", "item": canonical_after_deferred}, 3),
            envelope("response_item", canonical_after_deferred, 4),
            envelope("event_msg", {"type": "item_completed", "item": deferred_only}, 5),
            envelope("event_msg", {"type": "item_completed", "item": deferred_only}, 6),
        ]
        session_file.write_text("".join(dump_line(entry) for entry in entries), encoding="utf-8")
        session = SessionInfo("item-completed-dedupe", paths=[str(session_file)])

        events, issues, _counts = load_session_events(session, Catalog(root=str(self.root)))

        self.assertEqual([event["item_id"] for event in events], ["canonical-first", "canonical-later", "deferred-only"])
        self.assertEqual(events[0]["text"], large_text)
        self.assertEqual(events[1]["text"], "规范消息优先")
        self.assertEqual(events[2]["text"], "仅有完成事件")
        self.assertEqual(issues, [])

    def test_records_without_ids_dedupe_by_canonical_body_not_envelope(self) -> None:
        sid = "digest-dedupe-session"
        session_dir = self.root / "sessions" / "2026" / "10" / "17"
        session_dir.mkdir(parents=True)
        path = session_dir / f"rollout-2026-10-17T00-00-00-{sid}.jsonl"
        payload = {"type": "message", "role": "user", "content": "去重中文内容"}
        copy = {"content": "去重中文内容", "role": "user", "type": "message"}
        path.write_text(
            dump_line(envelope("session_meta", {"session_id": sid}, 0))
            + dump_line(envelope("response_item", payload, 1, "2026-10-17T00:00:01Z"))
            + dump_line(envelope("response_item", copy, 2, "2026-10-17T00:00:02Z"))
            + dump_line(envelope("event_msg", {"type": "item_completed", "item": copy}, 3, "2026-10-17T00:00:03Z")),
            encoding="utf-8",
        )

        catalog = scan_codex_home(str(self.root))
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))
        session = next(item for item in catalog.sessions if item.session_id == sid)
        events, issues, _ = load_session_events(session, catalog, include_raw_data=False)

        messages = [event for event in events if event.get("kind") == "message"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["text"], "去重中文内容")
        self.assertEqual(session.duplicate_count, 1)
        self.assertEqual(issues, [])

    def test_nested_archive_rollout_is_marked_archived_and_filterable(self) -> None:
        archived_id = "33333333-3333-4333-8333-333333333333"
        active_id = "44444444-4444-4444-8444-444444444444"
        cases = [
            ("archived_sessions", archived_id, True),
            ("sessions", active_id, False),
        ]
        for folder_name, session_id, _expected in cases:
            folder = self.root / folder_name / "nested" / "2026" / "10" / "17"
            folder.mkdir(parents=True)
            path = folder / f"rollout-2026-10-17T00-00-00-{session_id}.jsonl"
            path.write_text(
                dump_line(envelope("session_meta", {"session_id": session_id, "timestamp": "2026-10-17T00:00:00Z"}, 0))
                + dump_line(envelope("response_item", {
                    "type": "message", "id": f"user-{session_id}", "role": "user",
                    "content": [{"type": "input_text", "text": "nested session"}],
                }, 1)),
                encoding="utf-8",
            )

        catalog = scan_codex_home(str(self.root))
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))
        sessions = {session.session_id: session for session in catalog.sessions}

        self.assertTrue(sessions[archived_id].archived)
        self.assertFalse(sessions[active_id].archived)
        self.assertEqual(_filter_sessions(
            list(sessions.values()), "", "archived", False, "", "", threading.Event()
        ), [sessions[archived_id]])

    def test_cancel_during_related_attachment_copy_stops_export_without_false_error(self) -> None:
        sid, _raw = self.write_chat_fixture()
        catalog = scan_codex_home(str(self.root))
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))
        session = next(item for item in catalog.sessions if item.session_id == sid)
        cancel = threading.Event()

        def interrupt_attachment_copy(*_args, **_kwargs):
            cancel.set()
            raise InterruptedError("cancelled while copying attachment")

        with patch("codex_history_viewer.exporter._write_attachment", side_effect=interrupt_attachment_copy):
            result = export_one(session, catalog, str(self.out), ["json"], cancel=cancel)

        folder = Path(result["folder"])
        self.assertEqual(result["status"], "已取消")
        self.assertTrue((folder / "未完成.txt").is_file())
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        self.assertFalse(manifest["complete"])
        self.assertEqual(manifest["status"], "已取消")
        self.assertIn("复制关联附件", manifest["cancelled_at_stage"])
        self.assertFalse((folder / "聊天记录.json").exists())
        self.assertFalse(any("附件目录读取失败" in issue for issue in result["issues"]))

    def test_cancel_during_zip_keeps_partial_manifest_and_lists_hashed_files(self) -> None:
        sid, _raw = self.write_chat_fixture()
        catalog = scan_codex_home(str(self.root))
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))
        session = next(item for item in catalog.sessions if item.session_id == sid)
        cancel = threading.Event()

        def interrupt_zip(*_args, **_kwargs):
            cancel.set()
            raise InterruptedError("cancelled during zip")

        with patch("codex_history_viewer.exporter._write_zip_entry", side_effect=interrupt_zip):
            result = export_one(session, catalog, str(self.out), ["zip"], cancel=cancel)

        folder = Path(result["folder"])
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "已取消")
        self.assertFalse(manifest["complete"])
        self.assertEqual(manifest["cancelled_at_stage"], "打包 ZIP")
        json_file = next(item for item in manifest["files"] if item["path"] == "聊天记录.json")
        self.assertEqual(json_file["sha256"], hashlib.sha256((folder / "聊天记录.json").read_bytes()).hexdigest())
        self.assertFalse(list(self.out.glob("*.zip")))
        self.assertFalse(list(self.out.glob("*.partial")))

    def test_raw_copy_failure_does_not_stop_other_sources(self) -> None:
        sid, first = self.write_chat_fixture()
        second = self.root / "sessions" / "2026" / "01" / "02" / f"rollout-2026-01-02T00-00-00-{sid}.jsonl"
        second.parent.mkdir(parents=True)
        second.write_bytes(first.read_bytes())
        catalog = scan_codex_home(str(self.root))
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))
        session = next(item for item in catalog.sessions if item.session_id == sid)
        session.paths = [str(first), str(second)]
        real_copy = _copy_file_with_cancel

        def fail_first(source, destination, cancel):
            if source == str(first):
                raise PermissionError("simulated source access denial")
            return real_copy(source, destination, cancel)

        with patch("codex_history_viewer.exporter._copy_file_with_cancel", side_effect=fail_first):
            result = export_one(session, catalog, str(self.out), ["txt"], include_raw=True, cancel=threading.Event())

        self.assertTrue(result["status"].startswith("完成"))
        raw_files = list((Path(result["folder"]) / "原始文件").glob("*.jsonl"))
        self.assertEqual(len(raw_files), 1)
        self.assertEqual(raw_files[0].read_bytes(), second.read_bytes())
        self.assertTrue(any("读取失败，已继续其他来源" in issue for issue in result["issues"]))

    def test_raw_source_change_during_copy_is_reported(self) -> None:
        sid, raw = self.write_chat_fixture()
        catalog = scan_codex_home(str(self.root))
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))
        session = next(item for item in catalog.sessions if item.session_id == sid)
        real_copy = _copy_file_with_cancel

        def report_changed_source(source, destination, cancel):
            copy_info = real_copy(source, destination, cancel)
            copy_info["source_changed_during_copy"] = True
            return copy_info

        with patch("codex_history_viewer.exporter._copy_file_with_cancel", side_effect=report_changed_source):
            result = export_one(session, catalog, str(self.out), ["txt"], include_raw=True, cancel=threading.Event())

        self.assertTrue(result["status"].startswith("完成"))
        self.assertTrue(any("复制期间发生变化" in issue and raw.name in issue for issue in result["issues"]))
        manifest = json.loads((Path(result["folder"]) / "manifest.json").read_text(encoding="utf-8"))
        self.assertTrue(any("可能不是稳定快照" in issue for issue in manifest["missing_content_and_errors"]))

    def test_zip_crc_validation_rejects_corrupt_member(self) -> None:
        archive_path = self.out / "corrupt.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("data.txt", "CRC 检查内容" * 100)
        raw = bytearray(archive_path.read_bytes())
        central_header = raw.index(b"PK\x01\x02")
        raw[central_header + 16] ^= 0x01
        archive_path.write_bytes(raw)
        with self.assertRaises(zipfile.BadZipFile):
            _validate_zip_archive(str(archive_path), threading.Event())

    def test_session_index_projection_and_corruption_reports_are_bounded(self) -> None:
        sid = "bounded-index-session"
        index_path = self.root / "session_index.jsonl"
        index_path.write_text(
            dump_line({"id": sid, "thread_name": "索引标题", "updated_at": "2026-01-01T00:00:00Z", "large_unused_field": ["x" * 1000] * 100})
            + "{bad index row\n" * 20,
            encoding="utf-8",
        )
        issues: list[str] = []
        indexed = _index_entries(str(self.root), issues)
        self.assertEqual(indexed[sid], {"thread_name": "索引标题", "title": None, "updated_at": "2026-01-01T00:00:00Z"})
        self.assertEqual(len(issues), 1)
        self.assertIn("20 行损坏", issues[0])
        self.assertIn("示例行：2, 3, 4, 5, 6", issues[0])

        history_path = self.root / "history.jsonl"
        history_path.write_text("{bad history row\n" * 20, encoding="utf-8")
        history_issues: list[str] = []
        notes: list[str] = []
        _inspect_history(str(self.root), notes, history_issues)
        self.assertEqual(len(history_issues), 1)
        self.assertIn("20 行损坏", history_issues[0])
        self.assertIn("示例行：1, 2, 3, 4, 5", history_issues[0])

    def test_database_wal_snapshot_and_database_only_items(self) -> None:
        import sqlite3

        sid = "db-only-session"
        db_path = self.root / "state_records.sqlite"
        writer = sqlite3.connect(db_path)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE conversation_metadata(session_id TEXT, display_title TEXT, created_at_ms INTEGER, updated_at_ms INTEGER, cwd TEXT, archived INTEGER)")
        writer.execute("CREATE TABLE conversation_items(thread_id TEXT, rollout_ordinal INTEGER, data TEXT)")
        writer.execute("INSERT INTO conversation_metadata VALUES(?,?,?,?,?,?)", (sid, "数据库中文会话", 1767225600000, 1767225660000, "C:\\db", 1))
        writer.execute("INSERT INTO conversation_items VALUES(?,?,?)", (sid, 0, json.dumps({"type": [], "id": "malformed-db-item", "content": "保留数据库未知记录"})))
        item = {"type": "commandExecution", "id": "cmd-1", "command": "echo WAL", "cwd": "C:\\db", "aggregatedOutput": "stdout: WAL visible", "exitCode": 0, "status": "completed"}
        writer.execute("INSERT INTO conversation_items VALUES(?,?,?)", (sid, 1, json.dumps(item)))
        writer.commit()
        catalog = scan_codex_home(str(self.root))
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))
        session = next(item for item in catalog.sessions if item.session_id == sid)
        dynamic_schema = next(table for database in catalog.databases for table in database.tables if table.name == "conversation_items")
        self.assertEqual(dynamic_schema.item_json, "data")
        self.assertEqual(session.title, "数据库中文会话")
        self.assertTrue(session.archived)
        self.assertIn("SQLite 条目回退", session.source)
        events, issues, _ = load_session_events(session, catalog)
        self.assertEqual([event["kind"] for event in events], ["unknown", "tool_call", "tool_result"])
        self.assertTrue(any("type 字段不是文本" in issue for issue in issues))
        call = next(event for event in events if event["kind"] == "tool_call")
        output = next(event for event in events if event["kind"] == "tool_result")
        self.assertIn("stdout: WAL visible", output["text"])
        self.assertEqual(call["call_id"], output["call_id"])
        writer.close()

    def test_database_supplements_rollout_gap_dedupes_and_merges_by_ordinal(self) -> None:
        import sqlite3

        sid = "db-rollout-gap-session"
        session_dir = self.root / "sessions" / "2026" / "01" / "02"
        session_dir.mkdir(parents=True)
        rollout = session_dir / f"rollout-2026-01-02T00-00-00-{sid}.jsonl"
        existing = {"type": "message", "id": "same-user", "role": "user", "content": "rollout 中的用户消息"}
        with rollout.open("w", encoding="utf-8") as stream:
            stream.write(dump_line(envelope("session_meta", {"session_id": sid}, 0)))
            stream.write(dump_line(envelope("response_item", existing, 1)))

        database = self.root / "state_supplement.sqlite"
        writer = sqlite3.connect(database)
        self.addCleanup(writer.close)
        writer.execute(
            "CREATE TABLE conversation_metadata(session_id TEXT, title TEXT, created_at_ms INTEGER, updated_at_ms INTEGER)"
        )
        writer.execute("CREATE TABLE conversation_items(thread_id TEXT, rollout_ordinal INTEGER, data TEXT)")
        writer.execute("CREATE INDEX conversation_items_thread_id ON conversation_items(thread_id)")
        writer.execute(
            "INSERT INTO conversation_metadata VALUES(?,?,?,?)",
            (sid, "数据库补充", 1767312000000, 1767312600000),
        )
        writer.execute(
            "INSERT INTO conversation_items VALUES(?,?,?)",
            (sid, 1, json.dumps(existing, ensure_ascii=False)),
        )
        later = {"type": "message", "id": "db-assistant", "role": "assistant", "phase": "final_answer", "content": "数据库中的后续回复"}
        writer.execute(
            "INSERT INTO conversation_items VALUES(?,?,?)",
            (sid, 2, json.dumps(later, ensure_ascii=False)),
        )
        writer.commit()
        writer.close()

        catalog = scan_codex_home(str(self.root))
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))
        session = next(item for item in catalog.sessions if item.session_id == sid)
        events, issues, _ = load_session_events(session, catalog)
        messages = [event for event in events if event.get("kind") == "message"]

        self.assertEqual([event["item_id"] for event in messages], ["same-user", "db-assistant"])
        self.assertEqual([event["text"] for event in messages], ["rollout 中的用户消息", "数据库中的后续回复"])
        self.assertTrue(any("新增 1 条" in issue for issue in issues))
        self.assertIn("SQLite 条目补充", session.source)

        preview_events, preview_issues, _ = load_session_events(
            session, catalog, include_raw_data=False, max_records=2
        )
        preview_messages = [event for event in preview_events if event.get("kind") == "message"]
        self.assertEqual([event["item_id"] for event in preview_messages], ["same-user"])
        self.assertTrue(any("达到 2 条源记录上限" in issue for issue in preview_issues))

    def test_corrupt_rollout_line_still_recovers_database_items(self) -> None:
        import sqlite3

        sid = "db-rollout-damaged-session"
        session_dir = self.root / "sessions" / "2026" / "01" / "03"
        session_dir.mkdir(parents=True)
        rollout = session_dir / f"rollout-2026-01-03T00-00-00-{sid}.jsonl"
        existing = {"type": "message", "id": "before-damage", "role": "user", "content": "保留的部分内容"}
        with rollout.open("w", encoding="utf-8") as stream:
            stream.write(dump_line(envelope("session_meta", {"session_id": sid}, 0)))
            stream.write(dump_line(envelope("response_item", existing, 1)))
            stream.write("{truncated json\n")

        database = self.root / "state_damaged.sqlite"
        writer = sqlite3.connect(database)
        self.addCleanup(writer.close)
        writer.execute("CREATE TABLE conversation_items(thread_id TEXT, rollout_ordinal INTEGER, data TEXT)")
        writer.execute("CREATE INDEX conversation_items_thread_id ON conversation_items(thread_id)")
        recovered = {"type": "message", "id": "after-damage", "role": "assistant", "phase": "final_answer", "content": "从 SQLite 恢复"}
        writer.execute(
            "INSERT INTO conversation_items VALUES(?,?,?)",
            (sid, 2, json.dumps(recovered, ensure_ascii=False)),
        )
        writer.commit()
        writer.close()

        catalog = scan_codex_home(str(self.root))
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))
        session = next(item for item in catalog.sessions if item.session_id == sid)
        events, issues, _ = load_session_events(session, catalog)

        self.assertTrue(any(event.get("item_id") == "after-damage" for event in events))
        self.assertTrue(any("JSON 损坏" in issue for issue in issues))
        self.assertTrue(any("新增 1 条" in issue for issue in issues))

    def test_healthy_rollout_reports_unindexed_database_coverage_limit(self) -> None:
        import sqlite3

        sid = "db-unindexed-rollout-session"
        session_dir = self.root / "sessions" / "2026" / "01" / "04"
        session_dir.mkdir(parents=True)
        rollout = session_dir / f"rollout-2026-01-04T00-00-00-{sid}.jsonl"
        message = {"type": "message", "id": "rollout-only", "role": "user", "content": "正常 rollout"}
        rollout.write_text(
            dump_line(envelope("session_meta", {"session_id": sid}, 0))
            + dump_line(envelope("response_item", message, 1)),
            encoding="utf-8",
        )

        database = self.root / "state_unindexed.sqlite"
        writer = sqlite3.connect(database)
        self.addCleanup(writer.close)
        writer.execute("CREATE TABLE conversation_items(thread_id TEXT, rollout_ordinal INTEGER, data TEXT)")
        writer.execute(
            "INSERT INTO conversation_items VALUES(?,?,?)",
            (sid, 2, json.dumps({"type": "message", "id": "db-extra", "role": "assistant", "content": "额外记录"}, ensure_ascii=False)),
        )
        writer.commit()
        writer.close()

        catalog = scan_codex_home(str(self.root))
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))
        session = next(item for item in catalog.sessions if item.session_id == sid)
        events, issues, _ = load_session_events(session, catalog)

        self.assertFalse(any(event.get("item_id") == "db-extra" for event in events))
        self.assertTrue(any("未建立索引" in issue and "无法据此核对" in issue for issue in issues))

    def test_large_jsonl_is_streamed_and_unknown_type_reported(self) -> None:
        sid = "33333333-3333-4333-8333-333333333333"
        session_dir = self.root / "sessions" / "2026" / "02" / "01"
        session_dir.mkdir(parents=True)
        path = session_dir / f"rollout-2026-02-01T00-00-00-{sid}.jsonl"
        with path.open("w", encoding="utf-8") as stream:
            stream.write(dump_line(envelope("session_meta", {"session_id": sid, "timestamp": "2026-02-01T00:00:00Z"}, 0)))
            for i in range(20_000):
                stream.write(dump_line(envelope("event_msg", {"type": "token_count", "sample": i}, i + 1)))
            stream.write(dump_line(envelope("future_record_v99", {"payload": "kept"}, 20_001)))
        catalog = scan_codex_home(str(self.root))
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))
        session = next(item for item in catalog.sessions if item.session_id == sid)
        self.assertEqual(session.event_count, 20_002)
        self.assertTrue(any("未知记录类型" in issue for issue in session.issues))
        events, _, _ = load_session_events(session, catalog)
        self.assertTrue(any(event.get("name") == "future_record_v99" for event in events))

    def test_preview_record_limit_stops_loading_but_full_read_is_unchanged(self) -> None:
        sid = "preview-limit-session"
        session_dir = self.root / "sessions" / "2026" / "05" / "01"
        session_dir.mkdir(parents=True)
        path = session_dir / f"rollout-2026-05-01T00-00-00-{sid}.jsonl"
        with path.open("w", encoding="utf-8") as stream:
            stream.write(dump_line(envelope("session_meta", {"session_id": sid}, 0)))
            for i in range(8):
                item = {"type": "message", "id": f"item-{i}", "role": "user", "content": f"记录 {i}"}
                stream.write(dump_line(envelope("response_item", item, i + 1)))

        catalog = scan_codex_home(str(self.root))
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))
        session = next(item for item in catalog.sessions if item.session_id == sid)
        preview_events, preview_issues, _ = load_session_events(session, catalog, include_raw_data=False, max_records=4)
        full_events, full_issues, _ = load_session_events(session, catalog, include_raw_data=False)
        self.assertEqual(len(preview_events), 3)
        self.assertTrue(any("达到 4 条源记录上限" in issue for issue in preview_issues))
        self.assertEqual(len(full_events), 8)
        self.assertFalse(any("源记录上限" in issue for issue in full_issues))

        text_preview, text_issues, _ = load_session_events(
            session, catalog, include_raw_data=False, max_records=20, max_text_chars=6
        )
        self.assertLessEqual(sum(len(event.get("text", "")) for event in text_preview), 6)
        self.assertTrue(any("预览文本已达到 6 字符上限" in issue for issue in text_issues))

    def test_non_text_item_types_and_extreme_timestamps_do_not_stop_jsonl(self) -> None:
        sid = "77777777-7777-4777-8777-777777777777"
        session_dir = self.root / "sessions" / "2026" / "04" / "01"
        session_dir.mkdir(parents=True)
        path = session_dir / f"rollout-2026-04-01T00-00-00-{sid}.jsonl"
        records = [
            envelope("session_meta", {"session_id": sid, "timestamp": 10**400}, 0),
            envelope("response_item", {"type": [], "id": "malformed-response", "content": "保留为未知条目"}, 1),
            envelope("event_msg", {"type": "item_completed", "item": {"type": {}, "id": "malformed-completed", "content": "也保留为未知条目"}}, 2),
            envelope("response_item", {"type": "message", "id": "valid-user", "role": "user", "content": "后续中文用户消息"}, 3),
            envelope("response_item", {"type": "message", "id": "valid-assistant", "role": "assistant", "content": "后续中文助手回复"}, 4),
        ]
        path.write_text("".join(dump_line(record) for record in records), encoding="utf-8")

        catalog = scan_codex_home(str(self.root))
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))
        session = next(item for item in catalog.sessions if item.session_id == sid)
        self.assertEqual(session.user_count, 1)
        self.assertEqual(session.assistant_count, 1)
        self.assertEqual(session.status, "部分可读，有报告项")
        self.assertTrue(any("response_item.type 不是文本" in issue for issue in session.issues))
        self.assertTrue(any("item_completed.item.type 不是文本" in issue for issue in session.issues))

        events, issues, _ = load_session_events(session, catalog)
        text = "\n".join(event.get("text", "") for event in events)
        self.assertIn("后续中文用户消息", text)
        self.assertIn("后续中文助手回复", text)
        self.assertGreaterEqual(sum(event.get("kind") == "unknown" for event in events), 2)
        self.assertTrue(any("不是文本" in issue for issue in issues))

        result = export_one(session, catalog, str(self.out), ["txt"], "full", cancel=threading.Event())
        self.assertTrue(result["status"].startswith("完成"))
        exported = (Path(result["folder"]) / "聊天记录.txt").read_text(encoding="utf-8")
        self.assertIn("后续中文用户消息", exported)
        self.assertIn("后续中文助手回复", exported)

    def test_timestamp_cache_keeps_the_latest_timestamp(self) -> None:
        sid = "78787878-7878-4787-8787-787878787878"
        path = Path(self.temp.name) / "rollout-time-cache.jsonl"
        entries = [
            envelope("event_msg", {"type": "token_count"}, 1, "2026-01-01T00:09:00Z"),
            envelope("event_msg", {"type": "token_count"}, 2, "2026-01-01T00:20:00Z"),
            envelope("event_msg", {"type": "token_count"}, 3, "2026-01-01T00:15:00Z"),
        ]
        path.write_text("".join(dump_line(item) for item in entries), encoding="utf-8")
        session = SessionInfo(sid, raw_updated_at="2026-01-01T00:10:00Z")
        _scan_rollout_file(str(path), session, threading.Event())
        self.assertEqual(session.raw_updated_at, "2026-01-01T00:20:00Z")

    def test_redacted_export_failure_does_not_leak_exception_text(self) -> None:
        session = SessionInfo("redacted-failure", r"C:\Users\tester\secret-project")
        catalog = Catalog(root=str(self.root))
        failure_text = r'Bearer abc.def.ghi password=FailureSecret C:\Users\tester\secret-project\broken.json {"token":"JSONFailureSecret","password":"Failure Quoted Secret"}'
        with patch("codex_history_viewer.exporter.load_session_events", side_effect=OSError(failure_text)):
            result = export_one(session, catalog, str(self.out), ["json"], redact=True, cancel=threading.Event())

        self.assertEqual(result["status"], "失败")
        combined = json.dumps(result, ensure_ascii=False) + (Path(result["folder"]) / "未完成.txt").read_text(encoding="utf-8")
        for secret in ("abc.def.ghi", "FailureSecret", "JSONFailureSecret", "Failure Quoted Secret", "secret-project", "broken.json"):
            self.assertNotIn(secret, combined)
        self.assertIn("[已脱敏:", combined)

        cancel_event = threading.Event()

        def cancel_while_reading(*_args, **_kwargs):
            cancel_event.set()
            return [], [failure_text], {}

        with patch("codex_history_viewer.exporter.load_session_events", side_effect=cancel_while_reading):
            cancelled = export_one(session, catalog, str(self.out), ["json"], redact=True, cancel=cancel_event)
        cancelled_text = json.dumps(cancelled, ensure_ascii=False) + (Path(cancelled["folder"]) / "未完成.txt").read_text(encoding="utf-8")
        self.assertEqual(cancelled["status"], "已取消")
        self.assertNotIn("FailureSecret", cancelled_text)
        self.assertNotIn("JSONFailureSecret", cancelled_text)
        self.assertNotIn("Failure Quoted Secret", cancelled_text)
        self.assertNotIn("secret-project", cancelled_text)

    def test_redaction_covers_compound_secret_field_names(self) -> None:
        from codex_history_viewer.exporter import _sanitize

        text = "OPENAI_API_KEY=SampleApiKey access_token=SampleAccess refresh_token=SampleRefresh client_secret=SampleClient private_key=SamplePrivate"
        cleaned, count = redact_text(text)

        for secret in ("SampleApiKey", "SampleAccess", "SampleRefresh", "SampleClient", "SamplePrivate"):
            self.assertNotIn(secret, cleaned)
        self.assertEqual(count, 5)
        self.assertEqual(cleaned.count("[已脱敏: 敏感值]"), 5)
        structured = _sanitize({"ssh_private_key": "StructuredPrivateKey"}, True, [0])
        self.assertEqual(structured["ssh_private_key"], "[已脱敏: 敏感字段]")

    def test_cancelable_redaction_matches_existing_rules_and_counts_across_lines(self) -> None:
        text = (
            'password="first \\"quoted\\" value"\n'
            "Bearer SampleBearerToken\n"
            "-----BEGIN PRIVATE KEY-----\nprivate-key-line\n-----END PRIVATE KEY-----\n"
            "C:\\Users\\sample\\secret.txt\n"
        )

        expected = redact_text(text)
        actual = redact_text(text, threading.Event())

        self.assertEqual(actual, expected)
        self.assertEqual(actual[1], 4)
        for secret in ("first", "SampleBearerToken", "private-key-line", "C:\\Users\\sample"):
            self.assertNotIn(secret, actual[0])

    def test_cancelable_redaction_fails_closed_for_multiline_sensitive_quote(self) -> None:
        with self.assertRaisesRegex(ValueError, "跨行引号敏感值"):
            redact_text('password="first line\nsecond line"\n', threading.Event())

    def test_multiline_sensitive_attachment_is_skipped_without_partial_output(self) -> None:
        source = Path(self.temp.name) / "notes.txt"
        destination = Path(self.temp.name) / "exported-notes.txt"
        source.write_text('password="AttachmentSecret\nsecond line"\n', encoding="utf-8")
        issues: list[str] = []

        copied = _write_attachment(source, destination, True, issues, cancel=threading.Event())

        self.assertFalse(copied)
        self.assertFalse(destination.exists())
        self.assertEqual(list(Path(self.temp.name).glob("*.partial")), [])
        self.assertNotIn("AttachmentSecret", "\n".join(issues))
        self.assertTrue(any("跨行或未闭合" in issue for issue in issues))

    def test_redacted_attachment_cancel_before_publish_removes_staged_file(self) -> None:
        source = Path(self.temp.name) / "cancel-notes.txt"
        destination = Path(self.temp.name) / "cancel-exported-notes.txt"
        source.write_text("ordinary content\n", encoding="utf-8")
        import codex_history_viewer.exporter as exporter

        cancel = threading.Event()
        redact_ordinary_text = exporter._redact_ordinary_text

        def redact_then_cancel(value: str):
            result = redact_ordinary_text(value, cancel)
            cancel.set()
            return result

        with patch("codex_history_viewer.exporter._redact_ordinary_text", side_effect=redact_then_cancel):
            with self.assertRaises(InterruptedError):
                _write_attachment(source, destination, True, [], cancel=cancel)

        self.assertFalse(destination.exists())
        self.assertEqual(list(Path(self.temp.name).glob("*.partial")), [])

    def test_multiline_sensitive_title_uses_safe_placeholder_everywhere(self) -> None:
        secret = "MultilineTitleSecret"
        session = SessionInfo("safe-title-id", f'password="{secret}\nsecond line"')
        catalog = Catalog(root=str(self.root))
        with patch("codex_history_viewer.exporter.load_session_events", return_value=([], [], {})):
            result = export_one(session, catalog, str(self.out), ["json"], redact=True, cancel=threading.Event())

        folder = Path(result["folder"])
        self.assertEqual(result["title"], "已脱敏会话")
        self.assertNotIn(secret, folder.name)
        self.assertNotIn(secret, json.dumps(result, ensure_ascii=False))
        exported_text = "\n".join(path.read_text(encoding="utf-8") for path in folder.rglob("*.json"))
        self.assertNotIn(secret, exported_text)
        self.assertIn('"title": "已脱敏会话"', exported_text)

    def test_cancel_during_redaction_regex_stops_before_next_pass(self) -> None:
        import codex_history_viewer.exporter as exporter

        cancel = threading.Event()

        class CancellingRegex:
            def sub(self, _replacement, text):
                cancel.set()
                return text

        class UnreachableRegex:
            def subn(self, *_args):
                self.called = True
                return "", 0

            called = False

        next_pass = UnreachableRegex()
        with patch.object(exporter, "_SECRET_VALUE", CancellingRegex()), patch.object(exporter, "_BEARER", next_pass):
            with self.assertRaises(InterruptedError):
                redact_text("large line", cancel)
        self.assertFalse(next_pass.called)

    def test_large_html_message_escaping_checks_cancel_between_chunks(self) -> None:
        from codex_history_viewer.exporter import _FORMAT_CHUNK_SIZE, _escape_html_with_breaks

        cancel = threading.Event()

        def cancel_after_escape(value: str, quote: bool = True) -> str:
            cancel.set()
            return value

        with patch("codex_history_viewer.exporter.html.escape", side_effect=cancel_after_escape) as escape:
            with self.assertRaises(InterruptedError):
                _escape_html_with_breaks("x" * (_FORMAT_CHUNK_SIZE + 1), cancel)
        self.assertEqual(escape.call_count, 1)

    def test_large_redaction_checks_cancel_while_processing_lines(self) -> None:
        class CancelAfterSeveralChecks:
            checks = 0

            def is_set(self) -> bool:
                self.checks += 1
                return self.checks >= 5

        with self.assertRaises(InterruptedError):
            redact_text(("ordinary line\n" * 50_000), CancelAfterSeveralChecks())

    def test_long_redaction_line_fails_closed_without_publishing_text_file(self) -> None:
        from codex_history_viewer.exporter import _MAX_REDACTION_LINE_CHARS

        session = SessionInfo("oversized-record", "超长记录")
        events = [{"kind": "message", "role": "user", "text": "x" * (_MAX_REDACTION_LINE_CHARS + 1)}]
        with patch("codex_history_viewer.exporter.load_session_events", return_value=(events, [], {})):
            result = export_one(session, Catalog(root=str(self.root)), str(self.out), ["txt"], redact=True)

        folder = Path(result["folder"])
        self.assertEqual(result["status"], "失败")
        self.assertIn("超过 1 Mi 字符脱敏上限", "\n".join(result["issues"]))
        self.assertFalse((folder / "聊天记录.txt").exists())
        self.assertEqual(list(folder.glob("*.partial")), [])
        self.assertIn("超过 1 Mi 字符脱敏上限", (folder / "未完成.txt").read_text(encoding="utf-8"))

    def test_cancelled_chunked_text_write_removes_partial_output(self) -> None:
        from codex_history_viewer.exporter import _FORMAT_CHUNK_SIZE, _write_text_export

        destination = self.out / "cancelled.txt"

        class CancelOnSecondCheck:
            checks = 0

            def is_set(self) -> bool:
                self.checks += 1
                return self.checks >= 2

        with self.assertRaises(InterruptedError):
            _write_text_export(str(destination), "x" * (_FORMAT_CHUNK_SIZE * 2), CancelOnSecondCheck())
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.out.glob("*.partial")), [])

    def test_redaction_removes_complete_authorization_scheme_and_credential(self) -> None:
        text = "Authorization: Bearer SampleBearerCredential\nauthorization: Basic SampleBasicCredential=="
        cleaned, count = redact_text(text)

        self.assertNotIn("SampleBearerCredential", cleaned)
        self.assertNotIn("SampleBasicCredential", cleaned)
        self.assertEqual(count, 2)
        self.assertEqual(cleaned.count("[已脱敏: 敏感值]"), 2)

    def test_zip_failure_cleans_partial_and_final_archives(self) -> None:
        sid, _raw = self.write_chat_fixture()
        catalog = scan_codex_home(str(self.root))
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))
        session = next(item for item in catalog.sessions if item.session_id == sid)
        with patch("codex_history_viewer.exporter._write_zip_entry", side_effect=OSError("simulated zip write failure")):
            result = export_one(session, catalog, str(self.out), ["zip"], cancel=threading.Event())
        self.assertEqual(result["status"], "失败")
        self.assertFalse(list(self.out.glob("*.zip")))
        self.assertFalse(list(self.out.glob("*.partial")))
        manifest = json.loads((Path(result["folder"]) / "manifest.json").read_text(encoding="utf-8"))
        self.assertFalse(manifest["complete"])
        self.assertEqual(manifest["status"], "失败")
        self.assertTrue(result["files"])

    def test_initial_failure_marker_write_removes_empty_session_directory(self) -> None:
        session = SessionInfo("marker-write-failure", "合成会话")
        catalog = Catalog(root=str(self.root))
        real_write_text = Path.write_text

        def fail_marker(path, *args, **kwargs):
            if path.name == "未完成.txt":
                raise OSError("simulated disk full")
            return real_write_text(path, *args, **kwargs)

        with patch.object(Path, "write_text", fail_marker):
            results = export_batch(
                [session], catalog, str(self.out), ["txt"], "full", False, False, threading.Event()
            )

        self.assertEqual(results[0]["status"], "失败")
        self.assertFalse(list(self.out.glob("合成会话__marker-write-failure*")))
        self.assertTrue((self.out / "导出报告.json").is_file())

    def test_output_enumeration_failure_marks_export_failed(self) -> None:
        sid, _raw = self.write_chat_fixture()
        catalog = scan_codex_home(str(self.root))
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))
        session = next(item for item in catalog.sessions if item.session_id == sid)
        real_walk = os.walk

        def fail_export_tree(path, *args, **kwargs):
            try:
                Path(path).relative_to(self.out)
            except ValueError:
                yield from real_walk(path, *args, **kwargs)
                return
            onerror = kwargs.get("onerror")
            if onerror:
                onerror(PermissionError(13, "access denied", str(path)))

        with patch("codex_history_viewer.exporter.os.walk", side_effect=fail_export_tree):
            result = export_one(session, catalog, str(self.out), ["json"], cancel=threading.Event())

        self.assertEqual(result["status"], "失败")
        self.assertTrue((Path(result["folder"]) / "未完成.txt").is_file())
        self.assertTrue(any("PermissionError" in issue for issue in result["issues"]))

    def test_zip_uses_private_temp_and_preserves_existing_partial_file_or_symlink(self) -> None:
        sid, _raw = self.write_chat_fixture()
        catalog = scan_codex_home(str(self.root))
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))
        session = next(item for item in catalog.sessions if item.session_id == sid)
        archive_stem = safe_name(session.title) + "__" + safe_name(session.session_id, 50)
        legacy_partial = self.out / f"{archive_stem}.zip.partial"
        sentinel = self.out / "partial-target.txt"
        sentinel.write_text("keep this file intact", encoding="utf-8")
        try:
            legacy_partial.symlink_to(sentinel)
        except (OSError, NotImplementedError):
            legacy_partial.write_text("keep this partial file intact", encoding="utf-8")

        result = export_one(session, catalog, str(self.out), ["zip"], cancel=threading.Event())

        self.assertTrue(result["status"].startswith("完成"))
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep this file intact")
        if legacy_partial.is_symlink():
            self.assertTrue(legacy_partial.is_symlink())
        else:
            self.assertEqual(legacy_partial.read_text(encoding="utf-8"), "keep this partial file intact")
        with zipfile.ZipFile(result["zip"]) as archive:
            self.assertIsNone(archive.testzip())
        self.assertEqual(list(self.out.glob("*.partial")), [legacy_partial])

    def test_database_search_fallback_appends_large_text_in_record_order_with_a_fixed_cap(self) -> None:
        import sqlite3

        sid = "database-search-session"
        path = self.root / "search.sqlite"
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE records(thread_id TEXT, data TEXT)")
        connection.executemany(
            "INSERT INTO records VALUES(?, ?)",
            ((sid, json.dumps({"type": "assistantMessage", "id": str(i), "text": f"片段{i}-" + "字" * 80}, ensure_ascii=False)) for i in range(4000)),
        )
        connection.commit()
        connection.close()
        session = SessionInfo(sid)
        snapshot = DatabaseSnapshot(str(path), str(path), [TableSchema("records", ["thread_id", "data"], item_json="data")])
        issues: list[str] = []
        _search_database_fallback(session, [snapshot], threading.Event(), issues)

        self.assertEqual(session.assistant_count, 4000)
        self.assertLessEqual(len(session.search_text), 256_000)
        self.assertTrue(session.search_text.startswith("片段0-字"))
        self.assertEqual(issues, [])

    def test_database_only_fallback_scans_unindexed_table_once_for_many_sessions(self) -> None:
        import sqlite3
        from codex_history_viewer.reader import _connect_readonly

        database_path = self.root / "many_sessions.sqlite"
        connection = sqlite3.connect(database_path)
        connection.execute("CREATE TABLE records(thread_id TEXT, item_json TEXT)")
        connection.executemany(
            "INSERT INTO records VALUES(?, ?)",
            (
                (f"thread-{index}", json.dumps({"type": "assistantMessage", "id": f"a-{index}", "text": f"消息-{index}"}))
                for index in range(200)
            ),
        )
        connection.commit()
        connection.close()
        snapshot = DatabaseSnapshot(
            str(database_path), str(database_path), [TableSchema("records", ["thread_id", "item_json"], item_json="item_json")]
        )
        sessions = [SessionInfo(f"thread-{index}") for index in range(200)]
        issues: list[str] = []
        opened: list[str] = []
        statements: list[str] = []

        def trace_connection(path: str):
            opened.append(path)
            conn = _connect_readonly(path)
            conn.set_trace_callback(statements.append)
            return conn

        with patch("codex_history_viewer.reader._connect_readonly", side_effect=trace_connection):
            _search_database_fallback_many(sessions, [snapshot], threading.Event(), issues)

        self.assertEqual(len(opened), 1)
        table_selects = [sql for sql in statements if sql.startswith("SELECT") and 'FROM "records"' in sql]
        self.assertEqual(len(table_selects), 1)
        self.assertNotIn(" WHERE ", table_selects[0])
        self.assertTrue(all(session.assistant_count == 1 for session in sessions))
        self.assertTrue(all(f"消息-{index}" in sessions[index].search_text for index in range(len(sessions))))
        self.assertEqual(issues, [])

    def test_batch_export_indexes_only_the_temporary_sqlite_snapshot(self) -> None:
        import sqlite3

        database_path = self.root / "batch_unindexed.sqlite"
        connection = sqlite3.connect(database_path)
        connection.execute("CREATE TABLE records(thread_id TEXT, rollout_ordinal INTEGER, data TEXT)")
        connection.executemany(
            "INSERT INTO records VALUES(?, ?, ?)",
            (
                (f"db-only-{index}", 1, json.dumps({"type": "assistantMessage", "id": f"a-{index}", "text": f"正文-{index}"}, ensure_ascii=False))
                for index in range(4)
            ),
        )
        connection.commit()
        connection.close()

        catalog = scan_codex_home(str(self.root))
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))
        sessions = [session for session in catalog.sessions if session.session_id.startswith("db-only-")]
        self.assertEqual(len(sessions), 4)
        schema = next(table for database in catalog.databases for table in database.tables if table.name == "records")
        self.assertFalse(schema.thread_id_indexed)

        results = export_batch(
            sessions, catalog, str(self.out), ["json"], "full", False, False, threading.Event()
        )
        self.assertEqual(len(results), 4)
        self.assertTrue(all(result["status"].startswith("完成") for result in results))

        snapshot = next(database.snapshot_path for database in catalog.databases if database.source_path == str(database_path))
        snapshot_connection = sqlite3.connect(snapshot)
        try:
            indexes = snapshot_connection.execute("PRAGMA index_list(\"records\")").fetchall()
            self.assertTrue(any(str(row[1]).startswith("codex_hv_thread_") for row in indexes))
            plan = snapshot_connection.execute(
                "EXPLAIN QUERY PLAN SELECT data FROM records WHERE thread_id = ?", (sessions[0].session_id,)
            ).fetchall()
            self.assertTrue(any("USING INDEX" in str(row[-1]).upper() for row in plan))
        finally:
            snapshot_connection.close()

        source_connection = sqlite3.connect(database_path)
        try:
            self.assertEqual(source_connection.execute("PRAGMA index_list(\"records\")").fetchall(), [])
        finally:
            source_connection.close()

    def test_one_batch_session_failure_does_not_block_later_sessions_and_report_is_redacted(self) -> None:
        sessions = [
            SessionInfo("first", "会话 password=TopSecret123"),
            SessionInfo("second", "第二个会话"),
        ]
        catalog = Catalog(root=str(self.root))
        successful = {"session_id": "second", "title": "第二个会话", "folder": "", "zip": "", "status": "完成", "issues": [], "files": []}
        with patch("codex_history_viewer.exporter.export_one", side_effect=[OSError("password=TopSecret123"), successful]) as export_mock:
            results = export_batch(
                sessions, catalog, str(self.out), ["txt"], "full", True, False, threading.Event()
            )

        self.assertEqual(export_mock.call_count, 2)
        self.assertEqual([item["status"] for item in results], ["失败", "完成"])
        report_files = list(self.out.glob("导出报告*.json"))
        self.assertEqual(len(report_files), 1)
        report_text = report_files[0].read_text(encoding="utf-8")
        self.assertNotIn("TopSecret123", report_text)
        report = json.loads(report_text)
        self.assertEqual(report["requested_sessions"], 2)
        self.assertEqual(report["processed_sessions"], 2)

    def test_cancelled_batch_report_lists_unstarted_sessions_with_redaction(self) -> None:
        sessions = [
            SessionInfo("not-started-1", "password=BatchSecret"),
            SessionInfo("not-started-2", "第二个未开始会话"),
        ]
        cancel = threading.Event()
        cancel.set()
        catalog = Catalog(root=str(self.root))

        with patch("codex_history_viewer.exporter.export_one") as export_mock:
            results = export_batch(
                sessions, catalog, str(self.out), ["txt"], "full", True, False, cancel
            )

        export_mock.assert_not_called()
        self.assertEqual(results, [])
        report_path = next(self.out.glob("导出报告*.json"))
        report_text = report_path.read_text(encoding="utf-8")
        self.assertNotIn("BatchSecret", report_text)
        report = json.loads(report_text)
        self.assertTrue(report["cancelled"])
        self.assertEqual(report["requested_sessions"], 2)
        self.assertEqual(report["processed_sessions"], 0)
        self.assertEqual(
            report["not_started_sessions"],
            [
                {"session_id": "not-started-1", "title": "password=[已脱敏: 敏感值]", "platform": "unknown", "status": "未开始"},
                {"session_id": "not-started-2", "title": "第二个未开始会话", "platform": "unknown", "status": "未开始"},
            ],
        )

    def test_batch_report_write_failure_is_visible_to_caller(self) -> None:
        catalog = Catalog(root=str(self.root))
        completed = {
            "session_id": "report-failure-session",
            "title": "合成会话",
            "folder": str(self.out / "session-dir"),
            "zip": "",
            "status": "完成",
            "issues": [],
            "files": [],
        }

        with patch("codex_history_viewer.exporter.export_one", return_value=completed):
            with patch("codex_history_viewer.exporter._write_unique_json_file", side_effect=PermissionError("simulated denied")):
                with self.assertRaisesRegex(RuntimeError, "批量报告写入失败") as raised:
                    export_batch(
                        [SessionInfo("report-failure-session", "合成会话")],
                        catalog,
                        str(self.out),
                        ["txt"],
                        "full",
                        False,
                        False,
                        threading.Event(),
                    )

        self.assertIn("已处理 1 个会话", str(raised.exception))

    def test_zero_processed_batch_creates_missing_output_directory_and_report(self) -> None:
        catalog = Catalog(root=str(self.root))
        cancelled_output = self.out / "not-created-yet" / "cancelled"
        cancel = threading.Event()
        cancel.set()
        cancelled_sessions = [SessionInfo("never-started", "未开始")]

        with patch("codex_history_viewer.exporter.export_one") as export_mock:
            cancelled_results = export_batch(
                cancelled_sessions, catalog, str(cancelled_output), ["txt"], "full", False, False, cancel
            )

        export_mock.assert_not_called()
        self.assertEqual(cancelled_results, [])
        self.assertTrue(cancelled_output.is_dir())
        cancelled_report = json.loads((cancelled_output / "导出报告.json").read_text(encoding="utf-8"))
        self.assertTrue(cancelled_report["cancelled"])
        self.assertEqual(cancelled_report["processed_sessions"], 0)
        self.assertEqual(cancelled_report["not_started_sessions"][0]["session_id"], "never-started")

        empty_output = self.out / "not-created-yet" / "empty"
        empty_results = export_batch(
            [], catalog, str(empty_output), ["txt"], "full", False, False, threading.Event()
        )
        self.assertEqual(empty_results, [])
        self.assertTrue(empty_output.is_dir())
        empty_report = json.loads((empty_output / "导出报告.json").read_text(encoding="utf-8"))
        self.assertFalse(empty_report["cancelled"])
        self.assertEqual(empty_report["requested_sessions"], 0)
        self.assertEqual(empty_report["processed_sessions"], 0)
        self.assertEqual(empty_report["not_started_sessions"], [])

    def test_referenced_codex_attachment_folder_is_exported_without_neighbor_folders(self) -> None:
        sid = "44444444-4444-4444-8444-444444444444"
        attachment_id = "55555555-5555-4555-8555-555555555555"
        attachment_dir = self.root / "attachments" / attachment_id
        attachment_dir.mkdir(parents=True)
        (attachment_dir / "pasted-text.txt").write_text("api_key=AttachmentSecret", encoding="utf-8")
        unrelated = self.root / "attachments" / "66666666-6666-4666-8666-666666666666"
        unrelated.mkdir()
        (unrelated / "private.txt").write_text("must not be exported", encoding="utf-8")

        session_dir = self.root / "sessions" / "2026" / "03" / "01"
        session_dir.mkdir(parents=True)
        raw = session_dir / f"rollout-2026-03-01T00-00-00-{sid}.jsonl"
        message = {
            "type": "message",
            "id": "user-attachment",
            "role": "user",
            "content": [{"type": "input_text", "text": f"附件：{attachment_dir / 'pasted-text.txt'}"}],
        }
        raw.write_text(
            dump_line(envelope("session_meta", {"session_id": sid, "timestamp": "2026-03-01T00:00:00Z"}, 0))
            + dump_line(envelope("response_item", message, 1)),
            encoding="utf-8",
        )

        catalog = scan_codex_home(str(self.root))
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))
        session = next(item for item in catalog.sessions if item.session_id == sid)
        self.assertTrue(session.has_attachment)
        self.assertNotIn("attachment_ids", session.public_dict())

        events, issues, _ = load_session_events(session, catalog)
        self.assertFalse(issues)
        linked = next(event for event in events if event.get("role") == "user")
        self.assertEqual([item["ref"] for item in linked["attachments"]], [str(attachment_dir / "pasted-text.txt")])
        self.assertEqual(linked["attachments"][0]["status"], "本地文件存在")

        result = export_one(session, catalog, str(self.out), ["md", "zip"], "chat", True, False, threading.Event())
        self.assertTrue(result["status"].startswith("完成"))
        exported_files = list((Path(result["folder"]) / "attachments").glob("*"))
        self.assertEqual(len(exported_files), 1)
        self.assertTrue(exported_files[0].name.startswith("attachment-"))
        copied_text = exported_files[0].read_text(encoding="utf-8")
        self.assertIn("[已脱敏:", copied_text)
        self.assertNotIn("AttachmentSecret", copied_text)
        markdown = (Path(result["folder"]) / "聊天记录.md").read_text(encoding="utf-8")
        self.assertIn("[附件：Codex 附件](attachments/", markdown)
        self.assertNotIn("![Codex 附件]", markdown)
        exported_json = json.loads((Path(result["folder"]) / "聊天记录.json").read_text(encoding="utf-8"))
        attachment = next(item for event in exported_json["events"] for item in event.get("attachments", []))
        self.assertTrue(attachment["path"].startswith("attachments/attachment-"))
        self.assertEqual(attachment["reference"], attachment["path"])
        self.assertNotIn(str(attachment_dir), json.dumps(exported_json, ensure_ascii=False))
        self.assertNotIn("pasted-text.txt", json.dumps(exported_json, ensure_ascii=False))
        txt_result = export_one(session, catalog, str(self.out), ["txt"], "chat", False, False, threading.Event())
        self.assertIn("[附件] attachments/", (Path(txt_result["folder"]) / "聊天记录.txt").read_text(encoding="utf-8"))
        self.assertFalse(any(path.name == "private.txt" for path in Path(result["folder"]).rglob("*")))

    def test_untrusted_event_paths_and_export_destination_are_restricted(self) -> None:
        sid = "77777777-7777-4777-8777-777777777777"
        secret = Path(self.temp.name) / "auth.json"
        secret.write_text('{"token":"do-not-export"}', encoding="utf-8")
        session_dir = self.root / "sessions" / "2026" / "04" / "01"
        session_dir.mkdir(parents=True)
        raw = session_dir / f"rollout-2026-04-01T00-00-00-{sid}.jsonl"
        raw.write_text(
            dump_line(envelope("session_meta", {"session_id": sid, "timestamp": "2026-04-01T00:00:00Z"}, 0))
            + dump_line(envelope("response_item", {"type": "future_event", "id": "future-1", "file_path": str(secret)}, 1)),
            encoding="utf-8",
        )
        catalog = scan_codex_home(str(self.root))
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))
        session = next(item for item in catalog.sessions if item.session_id == sid)
        with self.assertRaisesRegex(ValueError, "不能位于 Codex 数据目录内"):
            export_one(session, catalog, str(self.root / "attachments" / sid), ["zip"], cancel=threading.Event())

        result = export_one(session, catalog, str(self.out), ["zip"], cancel=threading.Event())
        self.assertTrue(result["status"].startswith("完成"))
        self.assertFalse(any(path.name == "auth.json" for path in Path(result["folder"]).rglob("*")))
        with zipfile.ZipFile(result["zip"]) as archive_file:
            self.assertFalse(any(name.lower().endswith("auth.json") for name in archive_file.namelist()))

    def test_sensitive_attachments_are_skipped_and_utf16_text_is_redacted(self) -> None:
        sid = "88888888-8888-4888-8888-888888888888"
        folder = self.root / "attachments" / sid
        folder.mkdir(parents=True)
        (folder / "auth.json").write_text('{"token":"must-not-ship"}', encoding="utf-8")
        (folder / ".npmrc").write_text("//registry.npmjs.org/:_authToken=SyntheticNpmToken", encoding="utf-8")
        (folder / ".pypirc").write_text("password = SyntheticPyPiPassword", encoding="utf-8")
        (folder / ".netrc").write_text("machine example.test login user password SyntheticNetrcPassword", encoding="utf-8")
        (folder / "notes.txt").write_bytes(
            "password=PrivatePass123\n"
            "{\"token\":\"JsonAttachmentSecret\",\"password\": \"Quoted Pass with spaces\"}\n"
            "C:\\Users\\admin\\Documents\\private.txt\n/Users/admin/private/file.txt".encode("utf-16")
        )
        session_dir = self.root / "sessions" / "2026" / "05" / "01"
        session_dir.mkdir(parents=True)
        raw = session_dir / f"rollout-2026-05-01T00-00-00-{sid}.jsonl"
        raw.write_text(dump_line(envelope("session_meta", {"session_id": sid, "timestamp": "2026-05-01T00:00:00Z"}, 0)), encoding="utf-8")
        catalog = scan_codex_home(str(self.root))
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))
        session = next(item for item in catalog.sessions if item.session_id == sid)
        result = export_one(session, catalog, str(self.out), ["zip"], redact=True, cancel=threading.Event())
        self.assertTrue(result["status"].startswith("完成"))
        exported = Path(result["folder"])
        note = next((exported / "attachments").glob("attachment-*.txt"))
        note_text = note.read_text(encoding="utf-8")
        self.assertIn("[已脱敏:", note_text)
        self.assertNotIn("PrivatePass123", note_text)
        self.assertNotIn("JsonAttachmentSecret", note_text)
        self.assertNotIn("Quoted Pass with spaces", note_text)
        self.assertNotIn("C:\\Users\\admin", note_text)
        self.assertNotIn("/Users/admin", note_text)
        self.assertFalse(any(path.name in {"auth.json", ".npmrc", ".pypirc", ".netrc"} for path in exported.rglob("*")))
        manifest = json.loads((exported / "manifest.json").read_text(encoding="utf-8"))
        self.assertGreater(manifest["redacted_occurrences"], 0)
        self.assertTrue(any("凭据或配置类附件" in issue for issue in manifest["missing_content_and_errors"]))
        exported_text = json.dumps(manifest, ensure_ascii=False) + "\n" + "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in exported.rglob("*") if path.is_file() and path.name != "未完成.txt"
        )
        for secret in ("SyntheticNpmToken", "SyntheticPyPiPassword", "SyntheticNetrcPassword"):
            self.assertNotIn(secret, exported_text)
        with zipfile.ZipFile(result["zip"]) as archive:
            self.assertTrue(archive.testzip() is None)
            self.assertFalse(any(name.endswith(("/.npmrc", "/.pypirc", "/.netrc")) for name in archive.namelist()))

    def test_chat_text_json_secrets_with_quotes_and_spaces_are_redacted(self) -> None:
        sid, raw = self.write_chat_fixture()
        with raw.open("a", encoding="utf-8") as stream:
            stream.write(dump_line(envelope(
                "response_item",
                {"type": "message", "id": "quoted-secret", "role": "user", "content": "{\"token\":\"ChatJsonSecret\", \"password\": \"Chat Secret With Spaces\"}"},
                20,
            )))
        catalog = scan_codex_home(str(self.root))
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))
        session = next(item for item in catalog.sessions if item.session_id == sid)

        result = export_one(session, catalog, str(self.out), ["json", "txt"], redact=True, cancel=threading.Event())

        self.assertTrue(result["status"].startswith("完成"))
        for path in Path(result["folder"]).rglob("*"):
            if path.is_file() and path.suffix.casefold() in {".json", ".txt"}:
                output = path.read_text(encoding="utf-8", errors="replace")
                self.assertNotIn("ChatJsonSecret", output)
                self.assertNotIn("Chat Secret With Spaces", output)

    def test_nested_manifest_json_is_listed_and_hashed(self) -> None:
        sid, _raw = self.write_chat_fixture()
        catalog = scan_codex_home(str(self.root))
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))
        session = next(item for item in catalog.sessions if item.session_id == sid)

        def add_nested_manifest(_session, _catalog, target, _events, _cancel, _issues, *_args):
            attachments = Path(target) / "attachments"
            attachments.mkdir(parents=True, exist_ok=True)
            (attachments / "manifest.json").write_text('{"note":"must be hashed"}', encoding="utf-8")
            return {}

        with patch("codex_history_viewer.exporter._copy_related_attachments", side_effect=add_nested_manifest):
            result = export_one(session, catalog, str(self.out), ["zip"], cancel=threading.Event())

        manifest = json.loads((Path(result["folder"]) / "manifest.json").read_text(encoding="utf-8"))
        attachment_entry = next(
            item for item in manifest["files"] if item["path"] == "attachments/manifest.json"
        )
        attachment_path = Path(result["folder"]) / Path(attachment_entry["path"])
        self.assertEqual(attachment_entry["sha256"], hashlib.sha256(attachment_path.read_bytes()).hexdigest())

    def test_export_skips_files_when_attachment_root_is_a_junction(self) -> None:
        sid, _raw = self.write_chat_fixture()
        catalog = scan_codex_home(str(self.root))
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))
        session = next(item for item in catalog.sessions if item.session_id == sid)
        attachment_root = self.root / "attachments"
        blocked = lambda path, _stop=None: Path(path) == attachment_root or attachment_root in Path(path).parents

        with patch("codex_history_viewer.exporter._has_link_or_junction_component", side_effect=blocked):
            result = export_one(session, catalog, str(self.out), ["zip"], cancel=threading.Event())

        self.assertTrue(result["status"].startswith("完成"))
        self.assertFalse(list((Path(result["folder"]) / "attachments").glob("**/*")) if (Path(result["folder"]) / "attachments").exists() else [])
        with zipfile.ZipFile(result["zip"]) as archive:
            self.assertFalse(any(name.startswith(Path(result["folder"]).name + "/attachments/") for name in archive.namelist()))

    def test_export_skips_nested_junction_content_but_keeps_regular_attachments(self) -> None:
        sid, _raw = self.write_chat_fixture()
        nested = self.root / "attachments" / sid / "nested-junction"
        nested.mkdir()
        (nested / "unrelated.txt").write_text("must not enter export", encoding="utf-8")
        catalog = scan_codex_home(str(self.root))
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))
        session = next(item for item in catalog.sessions if item.session_id == sid)

        with patch("codex_history_viewer.exporter._is_link_or_junction", side_effect=lambda path: Path(path) == nested):
            result = export_one(session, catalog, str(self.out), ["zip"], cancel=threading.Event())

        self.assertTrue(result["status"].startswith("完成"))
        with zipfile.ZipFile(result["zip"]) as archive:
            names = archive.namelist()
            self.assertTrue(any(name.endswith(".txt") and "attachment" in name for name in names))
            self.assertFalse(any(name.endswith("unrelated.txt") for name in names))

    def test_junction_entries_are_not_followed_when_listing_linked_attachments(self) -> None:
        linked = self.root / "attachments" / "session" / "linked-junction"
        linked.mkdir(parents=True)
        (linked / "unrelated.txt").write_text("must not read", encoding="utf-8")
        folder = linked.parent
        (folder / "related.txt").write_text("related", encoding="utf-8")
        with patch.object(Path, "is_junction", lambda candidate: candidate == linked, create=True):
            files = _list_attachment_folder_files(folder, str((self.root / "attachments").resolve()))

        self.assertEqual([Path(value).name for value in files], ["related.txt"])

    def test_attachment_walk_permission_error_is_reported(self) -> None:
        folder = self.root / "attachments" / "referenced-id"
        folder.mkdir(parents=True)
        issues: list[str] = []

        def denied_walk(path, followlinks=False, onerror=None):
            if onerror:
                onerror(PermissionError(13, "access denied", str(folder / "private")))
            return iter(())

        with patch("codex_history_viewer.reader.os.walk", side_effect=denied_walk):
            files = _list_attachment_folder_files(folder, str((self.root / "attachments").resolve()), issues=issues)

        self.assertEqual(files, [])
        self.assertTrue(any("附件目录读取失败" in issue and "PermissionError" in issue for issue in issues))

    def test_attachment_listing_limit_is_reported_for_preview(self) -> None:
        folder = self.root / "attachments" / "preview-many-files"
        folder.mkdir(parents=True)
        (folder / "one.txt").write_text("1", encoding="utf-8")
        (folder / "two.txt").write_text("2", encoding="utf-8")
        issues: list[str] = []

        files = _list_attachment_folder_files(
            folder,
            str((self.root / "attachments").resolve()),
            issues=issues,
            max_files=1,
        )

        self.assertEqual(len(files), 1)
        self.assertTrue(any("达到 1 个文件检查上限" in issue for issue in issues))

    def test_attachment_preview_budget_is_shared_across_referenced_folders(self) -> None:
        attachment_ids = (
            "11111111-1111-4111-8111-111111111111",
            "22222222-2222-4222-8222-222222222222",
        )
        for attachment_id in attachment_ids:
            folder = self.root / "attachments" / attachment_id
            folder.mkdir(parents=True)
            (folder / "related.txt").write_text(attachment_id, encoding="utf-8")

        output: list[dict[str, str]] = []
        issues: list[str] = []
        budget = _AttachmentListingBudget(1)
        _append_codex_attachment_refs(
            [f"attachments/{attachment_id}/related.txt" for attachment_id in attachment_ids],
            str(self.root),
            output,
            issues=issues,
            budget=budget,
        )

        found_files = [item["ref"] for item in output if Path(item["ref"]).is_file()]
        self.assertEqual(len(found_files), 1)
        self.assertEqual(budget.examined, 1)
        self.assertTrue(budget.limit_reported)
        self.assertEqual(sum("上限" in issue for issue in issues), 1)

    def test_preview_marks_over_budget_attachment_unchecked_instead_of_missing(self) -> None:
        sid = "33333333-3333-4333-8333-333333333333"
        attachment_ids = (
            "11111111-1111-4111-8111-111111111111",
            "22222222-2222-4222-8222-222222222222",
        )
        for attachment_id in attachment_ids:
            folder = self.root / "attachments" / attachment_id
            folder.mkdir(parents=True)
            (folder / "image.png").write_bytes(b"image")

        session_dir = self.root / "sessions" / "2026" / "01" / "01"
        session_dir.mkdir(parents=True)
        path = session_dir / f"rollout-2026-01-01T00-00-00-{sid}.jsonl"
        content = [{"type": "input_image", "image_url": f"attachments/{item}/image.png"} for item in attachment_ids]
        path.write_text(
            dump_line(envelope("session_meta", {"session_id": sid}, 0))
            + dump_line(envelope("response_item", {"type": "message", "id": "user-1", "role": "user", "content": content}, 1)),
            encoding="utf-8",
        )
        catalog = scan_codex_home(str(self.root))
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))
        session = next(item for item in catalog.sessions if item.session_id == sid)

        with patch("codex_history_viewer.reader.PREVIEW_ATTACHMENT_FILE_LIMIT", 1):
            events, issues, _ = load_session_events(session, catalog, include_raw_data=False, max_records=10)

        attachments = [attachment for event in events for attachment in event.get("attachments", [])]
        self.assertTrue(any(item.get("status") == "预览上限，未检查" for item in attachments))
        self.assertTrue(any("预览达到检查上限" in issue for issue in issues))
        self.assertFalse(any("附件未找到或不可离线读取" in issue for issue in issues))

    def test_attachment_precheck_permission_error_is_reported(self) -> None:
        folder = self.root / "attachments" / "referenced-id"
        folder.mkdir(parents=True)
        real_lstat = Path.lstat

        def denied_lstat(path: Path):
            if path == folder:
                raise PermissionError(13, "access denied", str(path))
            return real_lstat(path)

        issues: list[str] = []
        with patch.object(Path, "lstat", denied_lstat):
            files = _list_attachment_folder_files(folder, str((self.root / "attachments").resolve()), issues=issues)

        self.assertEqual(files, [])
        self.assertTrue(any("附件目录检查失败" in issue and "PermissionError" in issue for issue in issues))

    def test_preview_attachment_enumeration_caps_directory_only_trees(self) -> None:
        folder = self.root / "attachments" / "many-empty-folders"
        folder.mkdir(parents=True)
        for index in range(25):
            (folder / f"empty-{index:02d}").mkdir()
        issues: list[str] = []
        budget = _AttachmentListingBudget(1)

        files = _list_attachment_folder_files(
            folder,
            str((self.root / "attachments").resolve()),
            issues=issues,
            budget=budget,
        )

        self.assertEqual(files, [])
        self.assertEqual(budget.entries_seen, budget.entry_limit)
        self.assertTrue(budget.entry_limit_reported)
        self.assertTrue(any("目录项检查上限" in issue for issue in issues))

    def test_rollout_discovery_stops_consuming_directory_entries_on_cancel(self) -> None:
        sessions = self.root / "sessions"
        sessions.mkdir()
        cancel = threading.Event()
        consumed = 0

        class Entry:
            def __init__(self, index: int):
                self.name = f"unrelated-{index}.txt"
                self.path = str(sessions / self.name)

            def is_symlink(self) -> bool:
                return False

            def is_dir(self, follow_symlinks: bool = False) -> bool:
                return False

        class Entries:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def __iter__(self):
                nonlocal consumed
                for index in range(10_000):
                    consumed += 1
                    if index == 0:
                        cancel.set()
                    yield Entry(index)

        with patch("codex_history_viewer.reader._iter_candidate_databases", return_value=[]):
            with patch("codex_history_viewer.reader.os.scandir", return_value=Entries()):
                catalog = scan_codex_home(str(self.root), cancel=cancel)

        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))
        self.assertTrue(catalog.cancelled)
        self.assertEqual(consumed, 1)

    def test_session_root_permission_error_is_reported_and_archive_scan_continues(self) -> None:
        import zstandard

        denied = self.root / "sessions"
        denied.mkdir()
        archived_id = "77777777-7777-4777-8777-777777777777"
        archive_root = self.root / "archived_sessions"
        archive_root.mkdir()
        archived_data = (
            dump_line(envelope("session_meta", {"session_id": archived_id, "timestamp": "2026-01-01T00:00:00Z"}, 0))
            + dump_line(envelope("response_item", {"type": "message", "id": "archived-user", "role": "user", "content": [{"type": "input_text", "text": "归档可继续读取"}]}, 1))
        )
        (archive_root / f"rollout-2026-01-01T00-00-00-{archived_id}.jsonl.zst").write_bytes(
            zstandard.ZstdCompressor().compress(archived_data.encode("utf-8"))
        )
        real_scandir = os.scandir

        def denied_scandir(path):
            if os.path.abspath(os.fspath(path)) == os.path.abspath(denied):
                raise PermissionError(13, "simulated access denied", str(path))
            return real_scandir(path)

        with patch("codex_history_viewer.reader.os.scandir", side_effect=denied_scandir):
            catalog = scan_codex_home(str(self.root))
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))

        self.assertTrue(any("记录目录读取失败" in issue and "PermissionError" in issue for issue in catalog.issues))
        archived_session = next(item for item in catalog.sessions if item.session_id == archived_id)
        self.assertEqual(archived_session.user_count, 1)
        self.assertTrue(archived_session.archived)

    def test_concurrent_session_directory_allocation_never_collides(self) -> None:
        worker_count = 2
        barrier = threading.Barrier(worker_count)
        paths: list[str] = []
        errors: list[BaseException] = []
        first_candidate = os.path.join(str(self.out), "same-session")
        real_mkdir = os.mkdir

        def synchronized_mkdir(path, *args, **kwargs):
            if os.path.normcase(os.path.abspath(os.fspath(path))) == os.path.normcase(os.path.abspath(first_candidate)):
                barrier.wait(timeout=5)
            return real_mkdir(path, *args, **kwargs)

        def allocate() -> None:
            try:
                paths.append(unique_directory(str(self.out), "same-session"))
            except BaseException as exc:
                errors.append(exc)

        workers = [threading.Thread(target=allocate) for _ in range(worker_count)]
        with patch("codex_history_viewer.exporter.os.mkdir", side_effect=synchronized_mkdir):
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=10)

        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(errors, [])
        self.assertEqual(len(paths), worker_count)
        self.assertEqual(len(set(paths)), worker_count)
        self.assertEqual({os.path.basename(path) for path in paths}, {"same-session", "same-session_2"})
        self.assertTrue(all(Path(path).is_dir() for path in paths))

    def test_scan_reports_file_size_race_and_continues(self) -> None:
        _sid, rollout = self.write_chat_fixture()
        real_getsize = os.path.getsize

        def fail_for_rollout(path):
            if Path(path) == rollout:
                raise PermissionError(13, "simulated size race", str(path))
            return real_getsize(path)

        with patch("codex_history_viewer.reader.os.path.getsize", side_effect=fail_for_rollout):
            catalog = scan_codex_home(str(self.root))
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))

        session = next(item for item in catalog.sessions if item.paths and Path(item.paths[0]) == rollout)
        self.assertGreater(session.event_count, 0)
        self.assertTrue(any("文件属性读取失败" in issue and "PermissionError" in issue for issue in catalog.issues))

    def test_concurrent_export_reports_publish_without_overwrite(self) -> None:
        barrier = threading.Barrier(2)
        results: list[str] = []
        errors: list[BaseException] = []

        def write_report(label: str) -> None:
            try:
                barrier.wait(timeout=5)
                results.append(_write_unique_json_file(str(self.out), "导出报告.json", {"writer": label}))
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=write_report, args=(label,)) for label in ("left", "right")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertFalse(errors)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(set(results)), 2)
        self.assertEqual({json.loads(Path(path).read_text(encoding="utf-8"))["writer"] for path in results}, {"left", "right"})

    def test_attachment_walk_cancels_even_when_directory_tree_has_no_files(self) -> None:
        sid = "empty-directory-cancel"
        folder = self.root / "attachments" / sid
        folder.mkdir(parents=True)
        catalog = Catalog(root=str(self.root))
        session = SessionInfo(session_id=sid)
        cancel = threading.Event()
        yielded = 0

        def many_empty_directories(_path, followlinks=False, onerror=None):
            nonlocal yielded
            yielded += 1
            yield str(folder), ["empty"], []
            cancel.set()
            for index in range(10_000):
                yielded += 1
                yield str(folder / f"empty-{index}"), [], []

        with patch("codex_history_viewer.exporter.os.walk", side_effect=many_empty_directories):
            with self.assertRaises(InterruptedError):
                _copy_related_attachments(session, catalog, str(self.out), [], cancel, [])

        self.assertLess(yielded, 10)

    def test_attachment_root_junction_is_not_enumerated(self) -> None:
        attachment_root = self.root / "attachments"
        folder = attachment_root / "abababab-abab-4bab-8bab-abababababab"
        folder.mkdir(parents=True)
        (folder / "unrelated.txt").write_text("do not enumerate", encoding="utf-8")
        output: list[dict[str, str]] = []
        is_junction = lambda candidate: Path(candidate) == attachment_root
        with patch("codex_history_viewer.reader._is_link_or_junction", side_effect=is_junction):
            with patch("codex_history_viewer.reader._list_attachment_folder_files") as list_files:
                _append_codex_attachment_refs(
                    f"attachments/{folder.name}/unrelated.txt", str(self.root), output
                )
                list_files.assert_not_called()
        self.assertEqual(output[0]["ref"], str(folder))

    def test_cloud_reparse_point_is_not_mistaken_for_a_junction(self) -> None:
        from types import SimpleNamespace

        candidate = Path("cloud-placeholder")
        metadata = SimpleNamespace(st_file_attributes=0x400, st_reparse_tag=0x9000001A)
        with patch.object(Path, "is_symlink", return_value=False), patch.object(
            Path, "is_junction", return_value=False, create=True
        ), patch.object(Path, "lstat", return_value=metadata):
            self.assertFalse(_is_link_or_junction(candidate))

    def test_invalid_text_attachment_is_skipped_in_redacted_export(self) -> None:
        sid = "99999999-9999-4999-8999-999999999999"
        folder = self.root / "attachments" / sid
        folder.mkdir(parents=True)
        (folder / "broken.txt").write_bytes(b"password=\xff\xfe\x00")
        session_dir = self.root / "sessions" / "2026" / "06" / "01"
        session_dir.mkdir(parents=True)
        raw = session_dir / f"rollout-2026-06-01T00-00-00-{sid}.jsonl"
        raw.write_text(dump_line(envelope("session_meta", {"session_id": sid, "timestamp": "2026-06-01T00:00:00Z"}, 0)), encoding="utf-8")
        catalog = scan_codex_home(str(self.root))
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))
        session = next(item for item in catalog.sessions if item.session_id == sid)
        result = export_one(session, catalog, str(self.out), ["json"], redact=True, cancel=threading.Event())
        self.assertTrue(result["status"].startswith("完成"))
        self.assertFalse(any(path.name.startswith("broken-") for path in Path(result["folder"]).rglob("*")))
        manifest = json.loads((Path(result["folder"]) / "manifest.json").read_text(encoding="utf-8"))
        self.assertTrue(any("无法安全解码，已跳过" in issue for issue in manifest["missing_content_and_errors"]))

    def test_large_redacted_attachment_streams_and_covers_multiline_private_keys(self) -> None:
        source = Path(self.temp.name) / "large-notes.txt"
        destination = Path(self.temp.name) / "redacted-notes.txt"
        source.write_text(
            ("普通记录\n" * 20_000)
            + "password=LongSecretValue\n"
            + "-----BEGIN PRIVATE KEY-----\nsecret-key-line\n-----END PRIVATE KEY-----\n"
            + "Bearer abc.def.ghi\n",
            encoding="utf-8",
        )
        issues: list[str] = []
        counter = [0]
        self.assertTrue(_write_attachment(source, destination, True, issues, counter))
        exported = destination.read_text(encoding="utf-8")
        self.assertIn("password=[已脱敏:", exported)
        self.assertIn("[已脱敏: 私钥]", exported)
        self.assertIn("Bearer [已脱敏: 令牌]", exported)
        self.assertNotIn("LongSecretValue", exported)
        self.assertNotIn("secret-key-line", exported)
        self.assertGreaterEqual(counter[0], 3)

    def test_batch_failure_with_multiline_secret_is_redacted_and_later_session_runs(self) -> None:
        first = SessionInfo("batch-failure", "第一条")
        second = SessionInfo("batch-success", "第二条")
        catalog = Catalog(root=str(self.root))
        secret = "MultilineBatchSecret"
        successful_result = {
            "session_id": second.session_id,
            "title": second.title,
            "folder": "",
            "zip": "",
            "status": "完成",
            "issues": [],
            "files": [],
        }
        with patch(
            "codex_history_viewer.exporter.export_one",
            side_effect=[OSError(f'password="first line\n{secret}\nlast line"'), successful_result],
        ):
            results = export_batch(
                [first, second], catalog, str(self.out), ["txt"], "full", True, False, threading.Event()
            )

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["status"], "失败")
        self.assertEqual(results[1]["status"], "完成")
        report_text = next(self.out.glob("导出报告*.json")).read_text(encoding="utf-8")
        self.assertNotIn(secret, report_text)
        self.assertIn("错误信息无法安全脱敏", report_text)

    def test_large_attachment_copy_cancels_and_removes_partial_file(self) -> None:
        source = Path(self.temp.name) / "large.bin"
        destination = Path(self.temp.name) / "partial.bin"
        source.write_bytes(b"x" * (2 * 1024 * 1024))

        class CancelAfterFirstChunk:
            calls = 0

            def is_set(self) -> bool:
                self.calls += 1
                return self.calls >= 2

        with self.assertRaises(InterruptedError):
            _write_attachment(source, destination, False, [], cancel=CancelAfterFirstChunk())
        self.assertFalse(destination.exists())

    def test_inline_image_decodes_in_chunks_and_cancels_without_partial_file(self) -> None:
        import base64
        import math

        payload = b"image-payload" * 40_000
        value = "data:image/png;base64," + base64.b64encode(payload).decode("ascii")
        image_root = Path(self.temp.name) / "image-export"
        relative = _copy_inline_image(value, image_root, threading.Event())
        image_path = image_root / relative
        self.assertEqual(hashlib.sha256(image_path.read_bytes()).digest(), hashlib.sha256(payload).digest())

        cancel_root = Path(self.temp.name) / "cancelled-image-export"
        hash_chunks = math.ceil(len(value) / _INLINE_IMAGE_CHUNK_SIZE)

        class CancelDuringDecode:
            calls = 0

            def is_set(self) -> bool:
                self.calls += 1
                # Hashing checks once per chunk; cancel before the second decode chunk.
                return self.calls >= hash_chunks + 2

        with self.assertRaises(InterruptedError):
            _copy_inline_image(value, cancel_root, CancelDuringDecode())
        self.assertFalse(any(path.is_file() for path in cancel_root.rglob("*")) if cancel_root.exists() else False)

    def test_database_rollout_path_cannot_copy_auth_but_raw_rollout_hash_matches(self) -> None:
        import sqlite3

        sid = "abababab-abab-4bab-8bab-abababababab"
        session_dir = self.root / "sessions" / "2026" / "07" / "01"
        session_dir.mkdir(parents=True)
        raw = session_dir / f"rollout-2026-07-01T00-00-00-{sid}.jsonl"
        raw_bytes = (dump_line(envelope("session_meta", {"session_id": sid, "timestamp": "2026-07-01T00:00:00Z"}, 0))
                     + dump_line(envelope("response_item", {"type": "message", "id": "u1", "role": "user", "content": [{"type": "input_text", "text": "原样字节测试"}]}, 1))).encode("utf-8")
        raw.write_bytes(raw_bytes)
        auth = self.root / "auth.json"
        auth.write_text('{"access_token":"must-never-be-copied"}', encoding="utf-8")
        database = self.root / "state_paths.sqlite"
        connection = sqlite3.connect(database)
        connection.execute("CREATE TABLE conversation_metadata(session_id TEXT, title TEXT, created_at_ms INTEGER, updated_at_ms INTEGER, rollout_path TEXT)")
        connection.execute("INSERT INTO conversation_metadata VALUES(?,?,?,?,?)", (sid, "原始来源白名单", 1782864000000, 1782864000000, str(auth)))
        connection.commit()
        connection.close()

        catalog = scan_codex_home(str(self.root))
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))
        session = next(item for item in catalog.sessions if item.session_id == sid)
        self.assertIn(str(auth), session.paths)
        result = export_one(session, catalog, str(self.out), ["zip"], "full", False, True, threading.Event())
        self.assertTrue(result["status"].startswith("完成"))
        exported = Path(result["folder"])
        self.assertFalse(any(path.name == "auth.json" for path in exported.rglob("*")))
        raw_copy = next((exported / "原始文件").glob("*.jsonl"))
        self.assertEqual(hashlib.sha256(raw_bytes).digest(), hashlib.sha256(raw_copy.read_bytes()).digest())
        manifest = json.loads((exported / "manifest.json").read_text(encoding="utf-8"))
        self.assertTrue(any("忽略非 rollout 文件" in issue for issue in manifest["missing_content_and_errors"]))
        with zipfile.ZipFile(result["zip"]) as archive:
            self.assertIsNone(archive.testzip())
            self.assertFalse(any(name.lower().endswith("auth.json") for name in archive.namelist()))

    def test_database_metadata_cannot_redirect_reader_outside_selected_root(self) -> None:
        import sqlite3

        sid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        outside = Path(self.temp.name) / "outside.jsonl"
        outside.write_text(
            dump_line(envelope("session_meta", {"session_id": sid, "timestamp": "2026-07-01T00:00:00Z"}, 0))
            + dump_line(envelope("response_item", {"type": "message", "id": "external", "role": "assistant", "content": [{"type": "output_text", "text": "must not be read"}]}, 1)),
            encoding="utf-8",
        )
        database = self.root / "state_records.sqlite"
        connection = sqlite3.connect(database)
        connection.execute("CREATE TABLE conversation_metadata(session_id TEXT, title TEXT, created_at_ms INTEGER, updated_at_ms INTEGER, rollout_path TEXT)")
        connection.execute("INSERT INTO conversation_metadata VALUES(?,?,?,?,?)", (sid, "外部路径测试", 1782864000000, 1782864000000, str(outside)))
        connection.commit()
        connection.close()

        catalog = scan_codex_home(str(self.root))
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))
        session = next(item for item in catalog.sessions if item.session_id == sid)
        self.assertFalse(any(path == str(outside) for path in session.paths))
        self.assertTrue(any("位于所选目录之外" in issue for issue in catalog.issues))
        events, issues, _ = load_session_events(session, catalog)
        self.assertFalse(any("must not be read" in str(event) for event in events))
        self.assertFalse(any("external.jsonl" in issue and "之外" not in issue for issue in issues))

    def test_item_only_database_table_discovers_sessions_without_metadata(self) -> None:
        import sqlite3

        sid = "item-only-session-42"
        database = self.root / "thread_history_synthetic.sqlite"
        connection = sqlite3.connect(database)
        connection.execute("CREATE TABLE arbitrary_records(thread_id TEXT, payload TEXT, unused_blob BLOB)")
        connection.executemany(
            "INSERT INTO arbitrary_records VALUES(?, ?, ?)",
            [
                (sid, json.dumps({"type": "assistantMessage", "id": "a1", "text": "\u4ec5\u6570\u636e\u5e93"}, ensure_ascii=False), b"x" * (1024 * 1024)),
                ("bad\nthread", json.dumps({"type": "assistantMessage", "id": "a2", "text": "invalid"}, ensure_ascii=False), b"y" * (1024 * 1024)),
            ],
        )
        connection.commit()
        connection.close()

        catalog = scan_codex_home(str(self.root))
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))
        session = next((item for item in catalog.sessions if item.session_id == sid), None)
        self.assertIsNotNone(session)
        assert session is not None
        self.assertIn("SQLite", session.source)
        self.assertEqual(session.assistant_count, 1)
        self.assertEqual(session.status, "仅数据库条目")
        self.assertIn("\u4ec5\u6570\u636e\u5e93", session.search_text)
        self.assertTrue(any("thread_history_synthetic.sqlite" in issue and "arbitrary_records" in issue for issue in catalog.issues))

        real_connect = sqlite3.connect
        statements: list[str] = []

        class TracedConnection(sqlite3.Connection):
            def execute(self, sql, parameters=(), /):
                statements.append(sql)
                return super().execute(sql, parameters)

        def traced_connect(*args, **kwargs):
            kwargs["factory"] = TracedConnection
            return real_connect(*args, **kwargs)

        with patch("codex_history_viewer.reader.sqlite3.connect", side_effect=traced_connect):
            events, _, _ = load_session_events(session, catalog, include_raw_data=False)
        self.assertTrue(events)
        read_statements = [sql for sql in statements if "arbitrary_records" in sql]
        self.assertTrue(read_statements)
        self.assertTrue(all("unused_blob" not in sql and "SELECT *" not in sql for sql in read_statements))

    def test_database_search_fallback_decodes_utf8_json_blob(self) -> None:
        import sqlite3

        sid = "blob-json-session"
        path = self.root / "blob_records.sqlite"
        payload = json.dumps(
            {"type": "assistantMessage", "id": "blob-a1", "text": "BLOB \u4e2d\u6587"}, ensure_ascii=False
        ).encode("utf-8")
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE arbitrary_records(thread_id TEXT, item_json BLOB, unused_blob BLOB)")
        connection.execute("INSERT INTO arbitrary_records VALUES(?, ?, ?)", (sid, payload, b"not-needed" * 100_000))
        connection.commit()
        connection.close()

        session = SessionInfo(sid)
        snapshot = DatabaseSnapshot(
            str(path), str(path), [TableSchema("arbitrary_records", ["thread_id", "item_json"], item_json="item_json")]
        )
        issues: list[str] = []
        real_connect = sqlite3.connect
        statements: list[str] = []

        class TracedConnection(sqlite3.Connection):
            def execute(self, sql, parameters=(), /):
                statements.append(sql)
                return super().execute(sql, parameters)

        def traced_connect(*args, **kwargs):
            kwargs["factory"] = TracedConnection
            return real_connect(*args, **kwargs)

        with patch("codex_history_viewer.reader.sqlite3.connect", side_effect=traced_connect):
            _search_database_fallback(session, [snapshot], threading.Event(), issues)

        self.assertEqual(session.assistant_count, 1)
        self.assertIn("BLOB \u4e2d\u6587", session.search_text)
        self.assertEqual(issues, [])
        self.assertTrue(any("SELECT" in sql and "arbitrary_records" in sql for sql in statements))
        self.assertTrue(all("unused_blob" not in sql and "SELECT *" not in sql for sql in statements))

    def test_sqlite_query_interrupts_after_cancel_without_read_error(self) -> None:
        import sqlite3

        snapshot_path = self.root / "cancel-discovery.sqlite"
        connection = sqlite3.connect(snapshot_path)
        connection.execute("CREATE TABLE arbitrary_records(thread_id TEXT, item_json TEXT)")
        connection.executemany(
            "INSERT INTO arbitrary_records VALUES(?, ?)",
            [(f"thread-{index:05d}", dump_line({"type": "assistantMessage", "text": str(index)})) for index in range(5_000)],
        )
        connection.commit()
        connection.close()
        cancel = threading.Event()

        def cancel_when_handler_is_installed(conn, event):
            _enable_sqlite_cancellation(conn, event)
            event.set()

        database = DatabaseSnapshot(
            str(snapshot_path),
            str(snapshot_path),
            [TableSchema("arbitrary_records", ["thread_id", "item_json"], item_json="item_json")],
        )
        issues: list[str] = []
        with patch("codex_history_viewer.reader._enable_sqlite_cancellation", side_effect=cancel_when_handler_is_installed):
            _discover_database_item_sessions(database, {}, issues, cancel)

        self.assertTrue(cancel.is_set())
        self.assertTrue(any("发现已取消" in issue for issue in issues))
        self.assertFalse(any("发现失败" in issue for issue in issues))

    def test_database_session_discovery_bounds_duplicate_row_scans(self) -> None:
        import sqlite3

        snapshot_path = self.root / "duplicate-ids.sqlite"
        connection = sqlite3.connect(snapshot_path)
        connection.execute("CREATE TABLE records(thread_id TEXT, item_json TEXT)")
        connection.executemany(
            "INSERT INTO records VALUES(?, ?)",
            [(thread_id, "{}") for thread_id in ("first", "first", "first", "later")],
        )
        connection.commit()
        connection.close()
        database = DatabaseSnapshot(
            str(snapshot_path), str(snapshot_path), [TableSchema("records", ["thread_id", "item_json"], item_json="item_json")]
        )
        sessions: dict[str, SessionInfo] = {}
        issues: list[str] = []

        with patch("codex_history_viewer.reader.DATABASE_THREAD_DISCOVERY_ROW_LIMIT", 3):
            _discover_database_item_sessions(database, sessions, issues, threading.Event())

        self.assertEqual(set(sessions), {"first"})
        self.assertTrue(any("扫描记录达到上限 3 行" in issue for issue in issues))

    def test_database_session_discovery_uses_indexed_distinct_without_row_cap(self) -> None:
        import sqlite3

        snapshot_path = self.root / "indexed-ids.sqlite"
        connection = sqlite3.connect(snapshot_path)
        connection.execute("CREATE TABLE records(thread_id TEXT, item_json TEXT)")
        connection.executemany(
            "INSERT INTO records VALUES(?, ?)",
            [(f"thread-{index}", "{}") for index in range(8)],
        )
        connection.execute("CREATE INDEX records_thread_id_idx ON records(thread_id)")
        connection.commit()
        connection.close()
        database = DatabaseSnapshot(
            str(snapshot_path), str(snapshot_path), [TableSchema("records", ["thread_id", "item_json"], item_json="item_json")]
        )
        sessions: dict[str, SessionInfo] = {}
        issues: list[str] = []

        with patch("codex_history_viewer.reader.DATABASE_THREAD_DISCOVERY_ROW_LIMIT", 2):
            _discover_database_item_sessions(database, sessions, issues, threading.Event())

        self.assertEqual(set(sessions), {f"thread-{index}" for index in range(8)})
        self.assertFalse(any("扫描记录达到上限" in issue for issue in issues))

    def test_indexed_discovery_does_not_count_invalid_ids_toward_session_cap(self) -> None:
        import sqlite3

        snapshot_path = self.root / "indexed-invalid-ids.sqlite"
        connection = sqlite3.connect(snapshot_path)
        connection.execute("CREATE TABLE records(thread_id TEXT, item_json TEXT)")
        connection.executemany(
            "INSERT INTO records VALUES(?, ?)",
            [(thread_id, "{}") for thread_id in ("", " ", "thread-a", "thread-b", "thread-c")],
        )
        connection.execute("CREATE INDEX records_thread_id_idx ON records(thread_id)")
        connection.commit()
        connection.close()
        database = DatabaseSnapshot(
            str(snapshot_path), str(snapshot_path), [TableSchema("records", ["thread_id", "item_json"], item_json="item_json")]
        )
        sessions: dict[str, SessionInfo] = {}
        issues: list[str] = []

        with patch("codex_history_viewer.reader.DATABASE_THREAD_DISCOVERY_LIMIT", 2):
            with patch("codex_history_viewer.reader.DATABASE_THREAD_DISCOVERY_ROW_LIMIT", 1):
                _discover_database_item_sessions(database, sessions, issues, threading.Event())

        self.assertEqual(set(sessions), {"thread-a", "thread-b"})
        self.assertTrue(any("会话 ID 数量超过上限 2" in issue for issue in issues))
        self.assertTrue(any("忽略 2 个无效会话 ID" in issue for issue in issues))

    def test_sqlite_progress_handler_interrupts_long_query(self) -> None:
        import sqlite3

        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        cancel = threading.Event()
        _enable_sqlite_cancellation(connection, cancel)
        cancel.set()

        with self.assertRaisesRegex(sqlite3.OperationalError, "interrupted"):
            connection.execute(
                "WITH RECURSIVE counter(value) AS (VALUES(1) UNION ALL "
                "SELECT value + 1 FROM counter WHERE value < 1000000) SELECT sum(value) FROM counter"
            ).fetchone()

    def test_invalid_codex_root_is_reported_instead_of_empty_success(self) -> None:
        missing = self.root / "does-not-exist"
        invalid_file = self.root / "ordinary-file.txt"
        invalid_file.write_text("not a directory", encoding="utf-8")

        for path in (missing, invalid_file):
            catalog = scan_codex_home(str(path))
            self.addCleanup(lambda temp_dir=catalog.temp_dir: __import__("shutil").rmtree(temp_dir, ignore_errors=True))
            self.assertFalse(catalog.root_valid)
            self.assertEqual(catalog.sessions, [])
            self.assertTrue(any("Codex 数据目录无法读取" in issue for issue in catalog.issues))

        real_scandir = os.scandir

        def denied_root(path):
            if os.path.abspath(os.fspath(path)) == os.path.abspath(self.root):
                raise PermissionError(13, "simulated denied root", str(path))
            return real_scandir(path)

        with patch("codex_history_viewer.reader.os.scandir", side_effect=denied_root):
            denied_catalog = scan_codex_home(str(self.root))
        self.addCleanup(lambda: __import__("shutil").rmtree(denied_catalog.temp_dir, ignore_errors=True))
        self.assertFalse(denied_catalog.root_valid)
        self.assertTrue(any("PermissionError" in issue for issue in denied_catalog.issues))

    def test_cancel_stops_deferred_item_completed_normalization(self) -> None:
        from codex_history_viewer.reader import _normalized_response

        session_file = self.root / "cancel-deferred.jsonl"
        count = 2_000
        with session_file.open("w", encoding="utf-8") as stream:
            for index in range(count):
                stream.write(dump_line(envelope(
                    "event_msg",
                    {"type": "item_completed", "item": {"type": "message", "id": f"item-{index}", "role": "assistant", "content": [{"type": "output_text", "text": "synthetic"}]}},
                    index,
                )))
        session = SessionInfo("cancel-deferred-items", paths=[str(session_file)])
        catalog = Catalog(root=str(self.root))
        cancel = threading.Event()
        reader_module = __import__("codex_history_viewer.reader", fromlist=["_iter_jsonl"])
        original_iter = reader_module._iter_jsonl
        cancel_after = 500

        def cancel_after_collecting(path):
            for line_no, line in original_iter(path):
                yield line_no, line
                if line_no == cancel_after:
                    cancel.set()

        with patch("codex_history_viewer.reader._iter_jsonl", side_effect=cancel_after_collecting):
            with patch("codex_history_viewer.reader._normalized_response", wraps=_normalized_response) as normalize:
                events, issues, _ = load_session_events(session, catalog, cancel=cancel)

        self.assertEqual(normalize.call_count, 0)
        self.assertEqual(events, [])
        self.assertTrue(any("事件列表不完整" in issue for issue in issues))

    def test_cancel_skips_sorting_partial_event_list(self) -> None:
        from codex_history_viewer.reader import _event_time

        session_file = self.root / "cancel-sort.jsonl"
        with session_file.open("w", encoding="utf-8") as stream:
            for index in range(100):
                stream.write(dump_line(envelope("event_msg", {"type": "synthetic_event", "message": str(index)}, index)))
        session = SessionInfo("cancel-sort", paths=[str(session_file)])
        catalog = Catalog(root=str(self.root))
        cancel = threading.Event()
        reader_module = __import__("codex_history_viewer.reader", fromlist=["_iter_jsonl"])
        original_iter = reader_module._iter_jsonl

        def cancel_after_five(path):
            for line_no, line in original_iter(path):
                yield line_no, line
                if line_no == 5:
                    cancel.set()

        with patch("codex_history_viewer.reader._iter_jsonl", side_effect=cancel_after_five):
            with patch("codex_history_viewer.reader._event_time", wraps=_event_time) as event_time:
                events, issues, _ = load_session_events(session, catalog, cancel=cancel)

        self.assertTrue(cancel.is_set())
        self.assertEqual(len(events), 5)
        self.assertEqual(event_time.call_count, 0)
        self.assertTrue(any("事件列表不完整" in issue for issue in issues))

    def test_streaming_json_export_cancels_without_publishing_partial_json(self) -> None:
        output = self.out / "聊天记录.json"
        cancel = threading.Event()
        session = SessionInfo("json-cancel-session", title="合成测试")
        events = [
            {"kind": "message", "role": "assistant", "text": "第一条"},
            {"kind": "message", "role": "assistant", "text": "第二条"},
        ]

        class CancelDuringIteration(list):
            def __iter__(self):
                yield self[0]
                cancel.set()
                yield self[1]

        with self.assertRaises(InterruptedError):
            _write_json_export(
                str(output), session, CancelDuringIteration(events), "full", False, {}, [], [0], cancel
            )

        self.assertFalse(output.exists())
        self.assertEqual(list(self.out.glob(".聊天记录.json.*.partial")), [])

    def test_json_export_preserves_large_unicode_string_and_escaping(self) -> None:
        from codex_history_viewer.exporter import _FORMAT_CHUNK_SIZE

        output = self.out / "长文本.json"
        text = ('中文🙂 quote="反斜杠\\ 换行\n制表\t空字符\x00' * (_FORMAT_CHUNK_SIZE // 16 + 5))
        session = SessionInfo("large-json-session", title="长文本")
        events = [{"kind": "message", "role": "assistant", "text": text}]

        _write_json_export(str(output), session, events, "full", False, {}, [], [0], threading.Event())

        decoded = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(decoded["events"][0]["text"], text)
        self.assertEqual(decoded["session"]["session_id"], "large-json-session")
        self.assertIn('\n      "text": "', output.read_text(encoding="utf-8"))

    def test_json_export_cancels_during_single_large_string_without_partial_file(self) -> None:
        from codex_history_viewer.exporter import _FORMAT_CHUNK_SIZE

        output = self.out / "json-long-cancel.json"
        output_directory = self.out

        class CancelAfterFirstStringChunk:
            triggered = False

            def is_set(self) -> bool:
                if any(
                    path.stat().st_size >= _FORMAT_CHUNK_SIZE
                    for path in output_directory.glob(".json-long-cancel.json.*.partial")
                ):
                    self.triggered = True
                return self.triggered

        cancel = CancelAfterFirstStringChunk()
        session = SessionInfo("json-long-cancel", title="取消测试")
        events = [{"kind": "message", "role": "assistant", "text": "x" * (_FORMAT_CHUNK_SIZE * 3)}]

        with self.assertRaises(InterruptedError):
            _write_json_export(str(output), session, events, "full", False, {}, [], [0], cancel)

        self.assertTrue(cancel.triggered)
        self.assertFalse(output.exists())
        self.assertEqual(list(self.out.glob(".json-long-cancel.json.*.partial")), [])

    def test_cancelled_export_dialog_does_not_claim_completion(self) -> None:
        from types import SimpleNamespace

        folder = self.out / "partial-export"
        folder.mkdir()

        class Widget:
            def setEnabled(self, _value):
                pass

            def setRange(self, _minimum, _maximum):
                pass

            def setValue(self, _value):
                pass

            def setText(self, _value):
                pass

            def setPlainText(self, _value):
                pass

        class FakeMessageBox:
            class StandardButton:
                Ok = 1

            instances = []

            def __init__(self, _parent):
                self.instances.append(self)
                self.title = ""
                self.text = ""

            def setWindowTitle(self, value):
                self.title = value

            def setText(self, value):
                self.text = value

            def setStandardButtons(self, _value):
                pass

            def setInformativeText(self, _value):
                pass

            def exec(self):
                pass

        cancel = threading.Event()
        cancel.set()
        window = SimpleNamespace(
            export_button=Widget(),
            scan_button=Widget(),
            choose_path_button=Widget(),
            cancel_button=Widget(),
            progress_bar=Widget(),
            export_worker=SimpleNamespace(sessions=[object()], cancel_event=cancel),
            status_label=Widget(),
            preview_title=Widget(),
            preview=Widget(),
            _refresh_controls=lambda: None,
        )
        result = {"status": "已取消", "title": "合成会话", "folder": str(folder), "issues": []}

        with patch("codex_history_viewer.gui.QMessageBox", FakeMessageBox):
            with patch.object(MainWindow, "_refresh_controls"):
                MainWindow._export_completed(window, [result])

        self.assertEqual(FakeMessageBox.instances[0].title, "导出已取消")
        self.assertIn("可能不完整", FakeMessageBox.instances[0].text)

    def test_large_batch_result_preview_is_bounded(self) -> None:
        from types import SimpleNamespace

        class Widget:
            text = ""

            def setEnabled(self, _value):
                pass

            def setRange(self, _minimum, _maximum):
                pass

            def setValue(self, _value):
                pass

            def setText(self, value):
                self.text = value

            def setPlainText(self, value):
                self.text = value

        results = [{"status": "完成", "title": f"会话 {index}", "folder": "", "issues": []} for index in range(75)]
        window = SimpleNamespace(
            export_button=Widget(), scan_button=Widget(), choose_path_button=Widget(), cancel_button=Widget(),
            progress_bar=Widget(), status_label=Widget(), preview_title=Widget(), preview=Widget(),
            export_worker=SimpleNamespace(sessions=[None] * len(results), cancel_event=threading.Event()),
            _refresh_controls=lambda: None,
        )

        MainWindow._export_completed(window, results)

        self.assertEqual(window.preview.text.count("[完成]"), 50)
        self.assertIn("仅显示前 50 条", window.preview.text)
        self.assertIn("会话 49", window.preview.text)
        self.assertNotIn("会话 50", window.preview.text)

    def test_json_only_export_maps_each_event_once(self) -> None:
        from codex_history_viewer.exporter import _map_attachments_in_event

        session = SessionInfo("json-only-session", title="JSON-only")
        events = [
            {"kind": "message", "role": "user", "text": "你好", "attachments": []},
            {"kind": "message", "role": "assistant", "text": "完成", "attachments": []},
        ]
        catalog = Catalog(root=str(self.root))
        with patch("codex_history_viewer.exporter.load_session_events", return_value=(events, [], {})):
            with patch("codex_history_viewer.exporter._map_attachments_in_event", wraps=_map_attachments_in_event) as map_event:
                result = export_one(session, catalog, str(self.out), ["json"], cancel=threading.Event())

        self.assertTrue(result["status"].startswith("完成"))
        self.assertEqual(map_event.call_count, len(events))
        exported = json.loads((Path(result["folder"]) / "聊天记录.json").read_text(encoding="utf-8"))
        self.assertEqual([event["text"] for event in exported["events"]], ["你好", "完成"])

    def test_metadata_reader_projects_only_recognized_fields(self) -> None:
        import sqlite3

        sid = "metadata-projection-session"
        path = self.root / "metadata-projection.sqlite"
        connection = sqlite3.connect(path)
        connection.execute(
            "CREATE TABLE arbitrary_metadata(session_id TEXT, title TEXT, created_at TEXT, cwd TEXT, archived INTEGER, unused_blob BLOB)"
        )
        connection.execute(
            "INSERT INTO arbitrary_metadata VALUES(?, ?, ?, ?, ?, ?)",
            (sid, "仅读取必要列", "2026-10-17T00:00:00Z", "C:/synthetic", 1, b"unused" * 100_000),
        )
        connection.commit()
        connection.close()

        real_connect = sqlite3.connect
        statements: list[str] = []

        class TracedConnection(sqlite3.Connection):
            def execute(self, sql, parameters=(), /):
                statements.append(sql)
                return super().execute(sql, parameters)

        def traced_connect(*args, **kwargs):
            kwargs["factory"] = TracedConnection
            return real_connect(*args, **kwargs)

        schema = TableSchema(
            "arbitrary_metadata",
            ["session_id", "title", "created_at", "cwd", "archived", "unused_blob"],
            metadata_id="session_id",
            title="title",
        )
        database = DatabaseSnapshot(str(path), str(path), [schema])
        sessions: dict[str, SessionInfo] = {}
        issues: list[str] = []
        with patch("codex_history_viewer.reader.sqlite3.connect", side_effect=traced_connect):
            _read_metadata(database, {}, sessions, issues)

        self.assertEqual(issues, [])
        self.assertEqual(sessions[sid].title, "仅读取必要列")
        self.assertEqual(sessions[sid].cwd, "C:/synthetic")
        self.assertTrue(sessions[sid].archived)
        metadata_queries = [sql for sql in statements if "arbitrary_metadata" in sql]
        self.assertTrue(metadata_queries)
        self.assertTrue(all("unused_blob" not in sql and "SELECT *" not in sql for sql in metadata_queries))

    def test_scan_classifies_explicit_chatgpt_and_codex_catalog_sources(self) -> None:
        import sqlite3

        path = self.root / "sqlite" / "sample-catalog.sqlite"
        path.parent.mkdir(parents=True)
        connection = sqlite3.connect(path)
        connection.execute(
            "CREATE TABLE local_thread_catalog(host_id TEXT, thread_id TEXT, display_title TEXT, source_created_at TEXT, source_updated_at TEXT, cwd TEXT, source_kind TEXT)"
        )
        connection.executemany(
            "INSERT INTO local_thread_catalog VALUES(?, ?, ?, ?, ?, ?, ?)",
            [
                ("chatgpt-host", "chatgpt-thread", "网页会话标题", "2026-08-25T12:00:00Z", "2026-08-25T12:10:00Z", None, "chatgpt"),
                ("local-host", "codex-thread", "Codex 会话标题", "2026-08-26T12:00:00Z", "2026-08-26T12:10:00Z", "C:/work", "vscode"),
            ],
        )
        connection.commit()
        connection.close()

        catalog = scan_codex_home(str(self.root))
        self.addCleanup(lambda: __import__("shutil").rmtree(catalog.temp_dir, ignore_errors=True))
        sessions = {session.session_id: session for session in catalog.sessions}

        self.assertEqual(sessions["chatgpt-thread"].platform, "chatgpt")
        self.assertEqual(sessions["chatgpt-thread"].status, "仅目录元数据")
        self.assertIsNone(sessions["chatgpt-thread"].disk_size_bytes)
        self.assertEqual(sessions["chatgpt-thread"].disk_size_status, "共享数据库 / 索引")
        self.assertTrue(any("未找到本地聊天正文" in issue for issue in sessions["chatgpt-thread"].issues))
        self.assertEqual(sessions["codex-thread"].platform, "codex")

        exported = export_one(
            sessions["chatgpt-thread"],
            catalog,
            str(self.out),
            ["txt", "md", "html", "json"],
            "full",
            False,
            False,
            threading.Event(),
        )
        self.assertTrue(exported["status"].startswith("完成"))
        folder = Path(exported["folder"])
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["platform"], "chatgpt")
        self.assertTrue(any("未找到本地聊天正文" in issue for issue in manifest["missing_content_and_errors"]))
        self.assertIn("来源类别：ChatGPT 网页", (folder / "聊天记录.txt").read_text(encoding="utf-8"))
        self.assertIn("来源类别：ChatGPT 网页", (folder / "聊天记录.md").read_text(encoding="utf-8"))
        self.assertIn("类别 ChatGPT 网页", (folder / "聊天记录.html").read_text(encoding="utf-8"))
        record = json.loads((folder / "聊天记录.json").read_text(encoding="utf-8"))
        self.assertEqual(record["session"]["platform"], "chatgpt")

if __name__ == "__main__":
    unittest.main()
