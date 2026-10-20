from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
import urllib.parse
import zipfile
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

APP_VERSION = "1.0.41"
SEARCH_LIMIT = 256_000
DATABASE_THREAD_DISCOVERY_LIMIT = 100_000
DATABASE_THREAD_DISCOVERY_ROW_LIMIT = 1_000_000
SQLITE_CANCEL_OPCODE_INTERVAL = 1_000
PREVIEW_ATTACHMENT_FILE_LIMIT = 5_000
STATUS_RECORD_TYPES = {"error", "task_started", "task_complete", "turn_aborted", "turn_interrupted", "turn_failed", "turn_cancelled", "turn_abandoned", "exec_command_begin", "exec_command_end"}
ATTACHMENT_FOLDER_RE = re.compile(r"(?i)(?:^|[\\/])attachments[\\/](?P<id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})[\\/]")


@dataclass
class SessionInfo:
    session_id: str
    title: str = ""
    created_at: str = ""
    updated_at: str = ""
    raw_created_at: str = ""
    raw_updated_at: str = ""
    cwd: str = ""
    archived: bool = False
    user_count: int = 0
    assistant_count: int = 0
    tool_count: int = 0
    event_count: int = 0
    source: str = ""
    status: str = "未扫描"
    paths: list[str] = field(default_factory=list)
    search_text: str = ""
    first_user_message: str = ""
    type_counts: dict[str, int] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    duplicate_count: int = 0
    has_attachment: bool = False
    attachment_ids: set[str] = field(default_factory=set)
    platform: str = "unknown"
    disk_size_bytes: int | None = None
    disk_size_status: str = "未统计"

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("search_text", None)
        data.pop("first_user_message", None)
        data.pop("attachment_ids", None)
        return data


@dataclass
class TableSchema:
    name: str
    columns: list[str]
    metadata_id: str = ""
    title: str = ""
    item_json: str = ""
    item_type: str = ""
    thread_id_indexed: bool = False
    order_column: str = ""


@dataclass
class DatabaseSnapshot:
    source_path: str
    snapshot_path: str
    tables: list[TableSchema] = field(default_factory=list)
    error: str = ""


@dataclass
class Catalog:
    root: str
    sessions: list[SessionInfo] = field(default_factory=list)
    databases: list[DatabaseSnapshot] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    temp_dir: str = ""
    files_scanned: int = 0
    bytes_scanned: int = 0
    cancelled: bool = False
    root_valid: bool = True


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _readonly_uri(path: str) -> str:
    uri = Path(path).resolve().as_uri()
    return uri + ("&" if "?" in uri else "?") + "mode=ro"


def _connect_readonly(path: str) -> sqlite3.Connection:
    return sqlite3.connect(_readonly_uri(path), uri=True, timeout=5.0)


def _enable_sqlite_cancellation(connection: sqlite3.Connection, cancel: threading.Event | None) -> None:
    """Interrupt long SQLite VM scans promptly when the caller cancels."""
    if cancel is not None:
        connection.set_progress_handler(
            lambda: 1 if cancel.is_set() else 0,
            SQLITE_CANCEL_OPCODE_INTERVAL,
        )


def _candidate_columns(table: str, columns: Iterable[str]) -> TableSchema:
    original = list(columns)
    lowered = {c.casefold(): c for c in original}
    table_info = TableSchema(name=table, columns=original)
    table_info.order_column = next(
        (lowered[key] for key in ("rollout_ordinal", "ordinal", "sequence", "seq", "created_at", "created_at_ms", "timestamp") if key in lowered),
        "",
    )
    id_col = next((lowered[k] for k in ("thread_id", "session_id", "id") if k in lowered), "")
    title_col = next((lowered[k] for k in ("title", "display_title", "thread_name") if k in lowered), "")
    time_cols = {"created_at", "created_at_ms", "updated_at", "updated_at_ms", "source_created_at", "source_updated_at", "rollout_path", "cwd"}
    metadata_like = bool(id_col and title_col and time_cols.intersection(lowered) and ("rollout_path" in lowered or "thread_id" in lowered or "session_id" in lowered or "cwd" in lowered))
    if not metadata_like and "rollout_path" in lowered and id_col:
        metadata_like = True
    table_info.metadata_id = id_col if metadata_like else ""
    table_info.title = title_col if metadata_like else ""
    json_col = next((c for k, c in lowered.items() if k.endswith("_json") and k not in {"error_json"}), "")
    thread_key = lowered.get("thread_id") or lowered.get("session_id")
    typed_item_column = any(k in lowered for k in ("item_type", "record_type"))
    item_json_column = "item" in json_col.casefold() and "json" in json_col.casefold()
    if thread_key and json_col and (typed_item_column or item_json_column):
        table_info.item_json = json_col
        table_info.item_type = next((lowered[k] for k in ("item_type", "record_type", "type") if k in lowered), "")
    return table_info


def _merge_platform(current: str, candidate: str) -> str:
    """Keep only explicit, non-conflicting product-source evidence."""
    if candidate not in {"codex", "chatgpt"}:
        return current
    if current in {"unknown", candidate}:
        return candidate
    return "unknown"


def _looks_like_history_item(value: Any) -> bool:
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return False
    if not isinstance(value, str) or not value.lstrip().startswith("{") or len(value) > 4 * 1024 * 1024:
        return False
    try:
        item = json.loads(value)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    if not isinstance(item, dict) or not isinstance(item.get("type"), str):
        return False
    keys = {str(key).casefold() for key in item}
    return bool(keys.intersection({"role", "content", "text", "call_id", "command", "output", "result", "arguments", "phase", "item"}))


def _decode_database_json(value: Any) -> Any:
    """Decode JSON stored as either SQLite TEXT or UTF-8 BLOB."""
    if isinstance(value, bytes):
        value = value.decode("utf-8-sig")
    if isinstance(value, (str, bytes, bytearray)):
        return json.loads(value)
    return value


def _discover_database_item_sessions(database: DatabaseSnapshot, by_id: dict[str, SessionInfo], issues: list[str], cancel: threading.Event | None = None) -> None:
    """Create sessions for thread IDs present only in recognized item/record tables."""
    if not database.snapshot_path:
        return
    connection: sqlite3.Connection | None = None
    try:
        connection = _connect_readonly(database.snapshot_path)
        _enable_sqlite_cancellation(connection, cancel)
        for schema in database.tables:
            if cancel is not None and cancel.is_set():
                break
            if not schema.item_json:
                continue
            lowered = {column.casefold(): column for column in schema.columns}
            thread_column = lowered.get("thread_id") or lowered.get("session_id")
            if not thread_column:
                continue
            invalid_count = 0
            seen_ids: set[str] = set()
            rows_scanned = 0
            try:
                distinct_sql = (
                    f"SELECT DISTINCT {_quote_identifier(thread_column)} FROM {_quote_identifier(schema.name)} "
                    f"WHERE {_quote_identifier(thread_column)} IS NOT NULL"
                )
                plan = connection.execute("EXPLAIN QUERY PLAN " + distinct_sql).fetchall()
                plan_details = [str(row[-1]).upper() for row in plan]
                indexed_distinct = (
                    any("USING" in detail and "INDEX" in detail for detail in plan_details)
                    and not any("USE TEMP B-TREE FOR DISTINCT" in detail for detail in plan_details)
                )
                if indexed_distinct:
                    scan_sql = distinct_sql
                    scan_parameters: tuple[int, ...] = ()
                else:
                    scan_sql = (
                        f"SELECT {_quote_identifier(thread_column)} FROM {_quote_identifier(schema.name)} "
                        f"WHERE {_quote_identifier(thread_column)} IS NOT NULL LIMIT ?"
                    )
                    scan_parameters = (DATABASE_THREAD_DISCOVERY_ROW_LIMIT + 1,)
                cursor = connection.execute(scan_sql, scan_parameters)
                discovered = 0
                over_limit = False
                rows_truncated = False
                try:
                    for (raw_id,) in cursor:
                        if cancel is not None and cancel.is_set():
                            break
                        if not indexed_distinct:
                            rows_scanned += 1
                            if rows_scanned > DATABASE_THREAD_DISCOVERY_ROW_LIMIT:
                                rows_truncated = True
                                break
                        if not isinstance(raw_id, str):
                            invalid_count += 1
                            continue
                        sid = raw_id.strip()
                        if (
                            not sid
                            or sid != raw_id
                            or len(sid) > 256
                            or any(ord(char) < 32 or ord(char) == 127 for char in sid)
                        ):
                            invalid_count += 1
                            continue
                        # Count only usable, normalized IDs toward the cap; invalid
                        # or whitespace-padded values must not hide valid sessions.
                        if sid in seen_ids:
                            continue
                        seen_ids.add(sid)
                        discovered += 1
                        if discovered > DATABASE_THREAD_DISCOVERY_LIMIT:
                            over_limit = True
                            break
                        session = by_id.setdefault(sid, SessionInfo(session_id=sid))
                        source_name = os.path.basename(database.source_path)
                        label = f"SQLite 条目索引（{source_name} / {schema.name}）"
                        if label not in session.source:
                            session.source = label if not session.source else session.source + " + " + label
                finally:
                    cursor.close()
            except sqlite3.Error as exc:
                if cancel is not None and cancel.is_set():
                    break
                issues.append(f"{database.source_path} / 表 {schema.name}：会话 ID 发现失败：{type(exc).__name__}: {exc}")
                continue
            if over_limit:
                issues.append(
                    f"{database.source_path} / 表 {schema.name}：会话 ID 数量超过上限 "
                    f"{DATABASE_THREAD_DISCOVERY_LIMIT:,}，其余记录未检查"
                )
            if rows_truncated:
                issues.append(
                    f"{database.source_path} / 表 {schema.name}：扫描记录达到上限 "
                    f"{DATABASE_THREAD_DISCOVERY_ROW_LIMIT:,} 行，后续会话未检查"
                )
            if invalid_count:
                issues.append(f"{database.source_path} / 表 {schema.name}：忽略 {invalid_count:,} 个无效会话 ID")
        if cancel is not None and cancel.is_set():
            issues.append(f"{database.source_path}：会话 ID 发现已取消")
    except Exception as exc:
        issues.append(f"{database.source_path}：会话 ID 发现失败：{type(exc).__name__}: {exc}")
    finally:
        if connection is not None:
            connection.close()


def _detect_json_item_column(connection: sqlite3.Connection, table: str, rows: list[tuple[Any, ...]], schema: TableSchema) -> str:
    """Detect a conversation-item JSON column from a few read-only samples when its name is unfamiliar."""
    lowered = {column.casefold() for column in schema.columns}
    thread_key = lowered.intersection({"thread_id", "session_id"})
    if not thread_key:
        return ""
    excluded = thread_key | {
        "id", "item_id", "created_at", "created_at_ms", "updated_at", "updated_at_ms",
        "rollout_ordinal", "ordinal", "title", "display_title", "cwd", "path", "type", "item_type", "record_type",
    }
    declared_types = {str(row[1]).casefold(): str(row[2] or "").casefold() for row in rows}
    for column in schema.columns:
        if column.casefold() in excluded or column.casefold().endswith("_json"):
            continue
        declared = declared_types.get(column.casefold(), "")
        if declared and not any(token in declared for token in ("text", "json", "blob", "char", "clob")):
            continue
        try:
            cursor = connection.execute(
                f"SELECT {_quote_identifier(column)} FROM {_quote_identifier(table)} "
                f"WHERE {_quote_identifier(column)} IS NOT NULL LIMIT 5"
            )
            try:
                if any(_looks_like_history_item(sample[0]) for sample in cursor):
                    return column
            finally:
                cursor.close()
        except sqlite3.Error:
            continue
    return ""


def _canonical_path(path: str) -> str:
    value = os.fspath(path)
    if value.startswith("\\\\?\\"):
        value = value[4:]
    return os.path.normcase(os.path.abspath(os.path.normpath(value)))


def _is_within_root(path: str, root: str) -> bool:
    try:
        resolved_path = os.path.normcase(os.path.realpath(path))
        resolved_root = os.path.normcase(os.path.realpath(root))
        return os.path.commonpath((resolved_root, resolved_path)) == resolved_root
    except (OSError, ValueError):
        return False


def _is_link_or_junction(path: str | Path) -> bool:
    """Detect filesystem links, including Windows directory junctions on older Python versions."""
    candidate = Path(path)
    try:
        if candidate.is_symlink():
            return True
        junction_check = getattr(candidate, "is_junction", None)
        if callable(junction_check) and junction_check():
            return True
        metadata = candidate.lstat()
        attributes = getattr(metadata, "st_file_attributes", 0)
        if not attributes & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
            return False
        tag = getattr(metadata, "st_reparse_tag", None)
        if tag is not None:
            return tag in {0xA000000C, 0xA0000003}  # symlink or mount-point/junction
        try:
            os.readlink(candidate)
            return True
        except OSError:
            return False
    except FileNotFoundError:
        return False
    except (OSError, RuntimeError):
        return True


def _has_link_or_junction_component(path: str | Path, stop_before: str | Path | None = None) -> bool:
    current = Path(os.path.abspath(path))
    stop = Path(os.path.abspath(stop_before)) if stop_before is not None else None
    while stop is None or current != stop:
        if _is_link_or_junction(current):
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent
    return False


def _is_link_or_junction_reported(path: str | Path, issues: list[str] | None, label: str) -> bool:
    """Keep the conservative link check while exposing access errors in the read report."""
    if not _is_link_or_junction(path):
        return False
    try:
        Path(path).lstat()
    except FileNotFoundError:
        return True
    except OSError as exc:
        if issues is not None:
            message = f"{label}检查失败：{path}（{type(exc).__name__}: {exc}）"
            if message not in issues:
                issues.append(message)
    return True


def _has_link_or_junction_component_reported(
    path: str | Path, stop_before: str | Path | None, issues: list[str] | None, label: str
) -> bool:
    current = Path(os.path.abspath(path))
    stop = Path(os.path.abspath(stop_before)) if stop_before is not None else None
    while stop is None or current != stop:
        if _is_link_or_junction_reported(current, issues, label):
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent
    return False


@dataclass
class _AttachmentListingBudget:
    """A per-preview cap shared by all attachment folders in one session read."""

    limit: int
    examined: int = 0
    entries_seen: int = 0
    limit_reported: bool = False
    entry_limit_reported: bool = False

    @property
    def entry_limit(self) -> int:
        # os.walk materializes every child name in a directory before yielding.
        # The bounded preview uses scandir and caps the total tree entries too.
        return max(self.limit * 10, self.limit)

    @property
    def exhausted(self) -> bool:
        return self.examined >= self.limit


def _attachment_folder_state(folder: Path, root_real: str) -> tuple[str, str]:
    """Inspect one attachment directory without following links or hiding ACL errors."""
    try:
        metadata = folder.lstat()
    except FileNotFoundError:
        return "missing", ""
    except OSError as exc:
        return "inaccessible", type(exc).__name__
    if _is_link_or_junction(folder) or _has_link_or_junction_component(folder, root_real):
        return "unsafe", ""
    if not stat.S_ISDIR(metadata.st_mode):
        return "not_directory", ""
    return "available", ""


def _open_database_snapshot(source: str, temp_dir: str, cancel: threading.Event | None = None) -> DatabaseSnapshot:
    snapshot = DatabaseSnapshot(source_path=source, snapshot_path="")
    src_conn: sqlite3.Connection | None = None
    try:
        src_conn = _connect_readonly(source)
        _enable_sqlite_cancellation(src_conn, cancel)
        names = src_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        schemas: list[TableSchema] = []
        for (table,) in names:
            rows = src_conn.execute(f"PRAGMA table_info({_quote_identifier(table)})").fetchall()
            schema = _candidate_columns(table, (r[1] for r in rows))
            lowered = {column.casefold(): column for column in schema.columns}
            thread_col = lowered.get("thread_id") or lowered.get("session_id")
            if thread_col:
                index_rows = src_conn.execute(f"PRAGMA index_list({_quote_identifier(table)})").fetchall()
                for index_row in index_rows:
                    index_name = str(index_row[1])
                    index_columns = src_conn.execute(
                        f"PRAGMA index_info({_quote_identifier(index_name)})"
                    ).fetchall()
                    if index_columns and str(index_columns[0][2] or "").casefold() == thread_col.casefold():
                        schema.thread_id_indexed = True
                        break
            if not schema.item_json:
                schema.item_json = _detect_json_item_column(src_conn, table, rows, schema)
                schema.item_type = next((lowered[name] for name in ("item_type", "record_type", "type") if name in lowered), "")
            schemas.append(schema)
        snapshot.tables = schemas
        if not any(s.metadata_id or s.item_json for s in schemas):
            snapshot.error = "未发现符合会话元数据或记录条目结构的表"
            src_conn.close()
            return snapshot

        key = hashlib.sha256(os.path.abspath(source).encode("utf-8", "replace")).hexdigest()[:16]
        dest_path = os.path.join(temp_dir, key + ".sqlite")
        dest_conn = sqlite3.connect(dest_path)
        try:
            def backup_progress(_status: int, _remaining: int, _total: int) -> None:
                if cancel is not None and cancel.is_set():
                    raise InterruptedError("SQLite 快照已取消")

            src_conn.backup(dest_conn, pages=512, sleep=0.02, progress=backup_progress)
        finally:
            dest_conn.close()
        snapshot.snapshot_path = dest_path
    except Exception as exc:
        if cancel is not None and cancel.is_set():
            snapshot.error = "SQLite 快照已取消"
        else:
            snapshot.error = f"SQLite 只读快照失败：{type(exc).__name__}: {exc}"
    finally:
        if src_conn is not None:
            try:
                src_conn.close()
            except Exception:
                pass
    return snapshot


def _epoch_string(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)):
        try:
            numeric = float(value)
        except (OverflowError, ValueError):
            return str(value)
        if numeric > 10_000_000_000:
            numeric /= 1000.0
        try:
            return datetime.fromtimestamp(numeric, timezone.utc).isoformat().replace("+00:00", "Z")
        except (OverflowError, OSError, ValueError):
            return str(value)
    return str(value)


def _time_sort_key(raw: str) -> float:
    if not raw:
        return 0.0
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError):
        try:
            return float(raw)
        except ValueError:
            return 0.0


def _append_search_part(parts: list[str], current_length: int, text: str) -> int:
    if not text or current_length >= SEARCH_LIMIT:
        return current_length
    separator = "\n" if current_length else ""
    value = (separator + text)[: SEARCH_LIMIT - current_length]
    if value:
        parts.append(value)
    return current_length + len(value)


def _record_digest(value: Any) -> str:
    """Hash a canonical record body independently of key order and whitespace."""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8", "replace")
    return hashlib.sha256(encoded).hexdigest()


def _local_display(raw: str) -> str:
    if not raw:
        return ""
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return raw


def _decode_zstd(path: str):
    try:
        import zstandard
    except ImportError as exc:
        raise RuntimeError("缺少 zstandard 依赖，无法读取归档 JSONL.ZST") from exc
    raw = open(path, "rb")
    reader = zstandard.ZstdDecompressor().stream_reader(raw)
    return io.TextIOWrapper(reader, encoding="utf-8", errors="replace"), raw


def _iter_jsonl(path: str):
    if path.casefold().endswith(".zst"):
        text_stream, raw = _decode_zstd(path)
        try:
            for line_no, line in enumerate(text_stream, 1):
                yield line_no, line
        finally:
            text_stream.close()
            raw.close()
    else:
        with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as stream:
            for line_no, line in enumerate(stream, 1):
                yield line_no, line


def _content_text(value: Any, attachments: list[dict[str, Any]] | None = None) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for entry in value:
            if isinstance(entry, str):
                parts.append(entry)
            elif isinstance(entry, dict):
                kind = str(entry.get("type", ""))
                text = entry.get("text")
                if isinstance(text, str):
                    parts.append(text)
                if "image" in kind.casefold() or "file" in kind.casefold():
                    ref = entry.get("image_url") or entry.get("url") or entry.get("path") or entry.get("file_path")
                    if ref:
                        if attachments is not None:
                            attachments.append({"ref": ref, "kind": kind or "附件"})
                        parts.append(f"[[ATTACHMENT:{ref}]]")
        return "\n".join(part for part in parts if part)
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _codex_attachment_ids(value: Any) -> set[str]:
    found: set[str] = set()
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, str):
            found.update(match.group("id") for match in ATTACHMENT_FOLDER_RE.finditer(current))
        elif isinstance(current, dict):
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    return found


def _list_attachment_folder_files(
    folder: Path,
    root_real: str,
    cancel: threading.Event | None = None,
    issues: list[str] | None = None,
    max_files: int | None = None,
    budget: _AttachmentListingBudget | None = None,
) -> list[str]:
    if budget is None and max_files is not None:
        budget = _AttachmentListingBudget(max_files)

    def report_check_error(path: Path, exc: OSError) -> None:
        if issues is None:
            return
        message = f"附件目录检查失败：{path}（{type(exc).__name__}: {exc}）"
        if message not in issues:
            issues.append(message)

    def skip_link(path: Path, label: str) -> bool:
        if not _is_link_or_junction_reported(path, issues, label):
            return False
        if issues is not None:
            message = f"{label}包含符号链接或 junction，已跳过：{path}"
            if message not in issues:
                issues.append(message)
        return True

    try:
        folder_metadata = folder.lstat()
    except FileNotFoundError:
        return []
    except OSError as exc:
        report_check_error(folder, exc)
        return []
    if skip_link(folder, "附件目录") or skip_link(folder.parent, "附件父目录"):
        return []
    if not stat.S_ISDIR(folder_metadata.st_mode):
        return []
    found: list[str] = []

    def report_walk_error(error: OSError) -> None:
        if issues is None:
            return
        location = error.filename or str(folder)
        message = f"附件目录读取失败：{location}（{type(error).__name__}）"
        if message not in issues:
            issues.append(message)

    def note_file_limit() -> None:
        if budget is None or budget.limit_reported:
            return
        budget.limit_reported = True
        if issues is not None:
            message = f"附件列表预览达到 {budget.limit:,} 个文件检查上限；其余文件未检查。"
            if message not in issues:
                issues.append(message)

    def exhausted_before_next_entry() -> bool:
        if budget is None or not budget.exhausted:
            return False
        note_file_limit()
        return True

    def note_entry_limit() -> None:
        if budget is None or budget.entry_limit_reported:
            return
        budget.entry_limit_reported = True
        budget.limit_reported = True
        if issues is not None:
            message = f"附件目录预览达到 {budget.entry_limit:,} 个目录项检查上限；其余内容未检查。"
            if message not in issues:
                issues.append(message)

    if budget is not None:
        pending_directories = [folder]
        while pending_directories:
            if cancel is not None and cancel.is_set():
                return found
            directory = pending_directories.pop()
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if cancel is not None and cancel.is_set():
                            return found
                        if budget.entries_seen >= budget.entry_limit:
                            note_entry_limit()
                            return found
                        budget.entries_seen += 1
                        path = Path(entry.path)
                        if skip_link(path, "附件路径"):
                            continue
                        try:
                            resolved = path.resolve()
                            if os.path.commonpath((os.path.normcase(root_real), os.path.normcase(str(resolved)))) != os.path.normcase(root_real):
                                continue
                            is_directory = entry.is_dir(follow_symlinks=False)
                        except (OSError, ValueError) as exc:
                            if isinstance(exc, OSError):
                                report_check_error(path, exc)
                            continue
                        if is_directory:
                            pending_directories.append(path)
                            continue
                        if exhausted_before_next_entry():
                            return found
                        budget.examined += 1
                        found.append(str(path))
            except OSError as exc:
                report_walk_error(exc)
        return found

    if exhausted_before_next_entry():
        return found

    file_limit_reached = False
    for directory, directories, filenames in os.walk(folder, followlinks=False, onerror=report_walk_error):
        if cancel is not None and cancel.is_set():
            break
        if exhausted_before_next_entry():
            directories.clear()
            break
        directories[:] = [
            name for name in directories
            if not skip_link(Path(directory) / name, "附件子目录")
        ]
        for filename in filenames:
            if cancel is not None and cancel.is_set():
                return found
            if exhausted_before_next_entry():
                file_limit_reached = True
                directories.clear()
                break
            path = Path(directory) / filename
            if budget is not None:
                budget.examined += 1
            if skip_link(path, "附件文件"):
                continue
            try:
                resolved = path.resolve()
                if os.path.commonpath((os.path.normcase(root_real), os.path.normcase(str(resolved)))) != os.path.normcase(root_real):
                    continue
            except (OSError, ValueError) as exc:
                if isinstance(exc, OSError):
                    report_check_error(path, exc)
                continue
            found.append(str(path))
        if file_limit_reached:
            break
    return found


def _append_codex_attachment_refs(
    value: Any,
    root: str,
    output: list[dict[str, Any]],
    session: SessionInfo | None = None,
    folder_cache: dict[str, list[str]] | None = None,
    cancel: threading.Event | None = None,
    issues: list[str] | None = None,
    max_files: int | None = None,
    budget: _AttachmentListingBudget | None = None,
) -> None:
    attachment_root = Path(root) / "attachments"
    has_linked_component = _has_link_or_junction_component_reported(
        attachment_root, root, issues, "附件根目录"
    )
    attachment_root_safe = not has_linked_component and _is_within_root(str(attachment_root), root)
    if has_linked_component and issues is not None:
        message = "附件根目录包含链接或无法检查的路径组件，关联附件未读取"
        if message not in issues:
            issues.append(message)
    root_real = os.path.normcase(str(attachment_root.resolve()))
    existing = {str(item.get("ref", "")) for item in output}
    for attachment_id in sorted(_codex_attachment_ids(value)):
        if cancel is not None and cancel.is_set():
            return
        if session is not None:
            session.attachment_ids.add(attachment_id)
        folder = attachment_root / attachment_id
        cache_key = os.path.normcase(os.path.abspath(folder))
        files = folder_cache.get(cache_key) if folder_cache is not None else None
        if files is None:
            files = _list_attachment_folder_files(
                folder, root_real, cancel, issues, max_files, budget
            ) if attachment_root_safe else []
            if folder_cache is not None and (cancel is None or not cancel.is_set()):
                folder_cache[cache_key] = files
        if not files:
            reference = str(folder)
            if reference not in existing:
                output.append({"ref": reference, "kind": "Codex 附件"})
                existing.add(reference)
            continue
        for reference in files:
            if cancel is not None and cancel.is_set():
                return
            if reference not in existing:
                output.append({"ref": reference, "kind": "Codex 附件"})
                existing.add(reference)


def _normalized_response(payload: dict[str, Any], envelope: dict[str, Any], attachments: list[dict[str, Any]]) -> dict[str, Any] | None:
    raw_kind = payload.get("type", "unknown")
    kind = raw_kind if isinstance(raw_kind, str) else "unknown"
    timestamp = str(envelope.get("timestamp", ""))
    ordinal = envelope.get("ordinal")
    item_id = str(payload.get("id", ""))
    shared = {"timestamp": timestamp, "raw_timestamp": timestamp, "ordinal": ordinal, "item_id": item_id, "source_type": kind}
    if kind in {"message", "agentMessage", "userMessage"}:
        role = str(payload.get("role", ""))
        if not role:
            role = "user" if kind == "userMessage" else "assistant"
        phase = str(payload.get("phase", ""))
        if role not in {"user", "assistant"} or phase.casefold() in {"analysis", "justify", "confidence", "delegate_summary", "juicepred"}:
            return None
        text = _content_text(payload.get("content", payload.get("text", "")), attachments)
        return {**shared, "kind": "message", "role": role, "phase": phase, "text": text, "data": payload}
    if kind in {"function_call", "custom_tool_call", "mcp_tool_call", "dynamic_tool_call", "web_search_call", "computer_call"} or (kind.endswith("_call") and kind != "message"):
        tool_name = payload.get("name") or payload.get("tool") or payload.get("namespace") or kind
        tool_input = payload.get("input", payload.get("arguments", payload.get("query", "")))
        text = _content_text(tool_input)
        return {**shared, "kind": "tool_call", "role": "tool", "name": str(tool_name), "call_id": payload.get("call_id") or item_id, "text": text, "data": payload}
    if kind in {"function_call_output", "custom_tool_call_output", "mcp_tool_call_output"} or kind.endswith("_output"):
        output = payload.get("output", payload.get("content", payload.get("result", "")))
        return {**shared, "kind": "tool_result", "role": "tool", "name": kind, "call_id": payload.get("call_id", ""), "text": _content_text(output, attachments), "data": payload}
    if kind == "reasoning":
        return None
    if kind in {"web_search_result", "image_generation_call", "file_search_call"}:
        return {**shared, "kind": "tool_call", "role": "tool", "name": kind, "call_id": payload.get("call_id", item_id), "text": _content_text(payload), "data": payload}
    return {**shared, "kind": "unknown", "role": "event", "name": kind, "text": _content_text(payload), "data": payload}


def _normalized_database_items(payload: dict[str, Any], envelope: dict[str, Any], attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    raw_kind = payload.get("type", "unknown")
    kind = raw_kind if isinstance(raw_kind, str) else "unknown"
    base = {"timestamp": envelope.get("timestamp", ""), "raw_timestamp": envelope.get("timestamp", ""), "ordinal": envelope.get("ordinal"), "item_id": payload.get("id", ""), "source_type": kind}
    if kind == "commandExecution":
        call_id = str(payload.get("id") or "")
        command = str(payload.get("command") or "[命令未保存]")
        prefix = f"cwd: {payload.get('cwd') or '未找到'}\nexit_code: {payload.get('exitCode', '未保存')}\nstatus: {payload.get('status') or '未保存'}\n"
        output = payload.get("aggregatedOutput")
        return [
            {**base, "kind": "tool_call", "role": "tool", "name": "命令执行", "call_id": call_id, "text": command, "data": payload},
            {**base, "kind": "tool_result", "role": "tool", "name": "命令输出", "call_id": call_id, "text": prefix + (str(output) if output is not None else "[命令输出未保存]"), "data": payload},
        ]
    if kind in {"mcpToolCall", "dynamicToolCall"}:
        name = ".".join(str(payload.get(k)) for k in ("server", "tool") if payload.get(k)) or str(payload.get("tool") or kind)
        call_id = str(payload.get("call_id") or payload.get("id") or "")
        arguments = _content_text(payload.get("arguments", payload.get("input", {})))
        call = {**base, "kind": "tool_call", "role": "tool", "name": name, "call_id": call_id, "text": arguments or "[参数未保存]", "data": payload}
        result = payload.get("result") or payload.get("contentItems")
        error = payload.get("error")
        result_text = _content_text(error if error else result) if error or result is not None else "[工具结果未保存]"
        output = {**base, "kind": "tool_result", "role": "tool", "name": name + " 结果", "call_id": call_id, "text": result_text, "data": payload}
        return [call, output]
    if kind == "functionCallOutput":
        return [{**base, "kind": "tool_result", "role": "tool", "name": str(payload.get("name") or "函数结果"), "call_id": str(payload.get("id") or ""), "text": str(payload.get("output") or "[工具结果未保存]"), "data": payload}]
    if kind == "subAgentActivity":
        return [{**base, "kind": "status", "role": "event", "name": "子代理活动", "text": _content_text(payload), "data": payload}]
    if kind == "imageView":
        path = payload.get("path")
        if path:
            attachments.append({"ref": str(path), "kind": "imageView"})
        return [{**base, "kind": "status", "role": "event", "name": "图片查看", "text": str(path or "[图片路径未保存]"), "data": payload}]
    record = _normalized_response(payload, envelope, attachments)
    return [record] if record else []


def _event_label(payload: dict[str, Any]) -> str:
    item_type = payload.get("type", "unknown")
    return item_type if isinstance(item_type, str) else "unknown"


def _is_user_message_item(item: Any) -> bool:
    """Recognize user messages without hashing untrusted JSON field values."""
    if not isinstance(item, dict):
        return False
    item_type = item.get("type")
    return (
        item_type == "userMessage"
        or item_type == "user_message"
        or (item_type == "message" and item.get("role") == "user")
    )


def _user_message_text(item: Any) -> str:
    """Return only explicit text from a recognized user message, never serialized unknown data."""
    if not _is_user_message_item(item):
        return ""
    content = item.get("content")
    pieces: list[str] = []
    if isinstance(content, str):
        pieces.append(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, str):
                pieces.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                pieces.append(part["text"])
    if not any(piece.strip() for piece in pieces) and isinstance(item.get("text"), str):
        pieces = [item["text"]]
    return " ".join(" ".join(pieces).split())


_GENERIC_TITLES = {
    "untitled", "new chat", "new conversation", "new thread", "未命名", "未命名会话",
    "未命名对话", "未命名绘画", "新会话", "新对话",
}


def _normalize_title(value: Any) -> str:
    if not isinstance(value, (str, int, float)):
        return ""
    return " ".join(str(value).split())


def _is_generic_title(value: Any) -> bool:
    title = _normalize_title(value).casefold()
    return title in _GENERIC_TITLES or bool(re.fullmatch(r"未命名(?:会话|对话|绘画)?", title))


def _finalize_session_title(session: SessionInfo) -> None:
    title = _normalize_title(session.title)
    if not title or len(title) > 180 or _is_generic_title(title):
        first_message = _normalize_title(session.first_user_message)
        title = first_message[:100].rstrip() if first_message else "未命名会话"
    session.title = title


def _iter_candidate_databases(root: str) -> list[str]:
    paths: list[str] = []
    root_real = os.path.realpath(root)
    for folder in (root, os.path.join(root, "sqlite")):
        if not os.path.isdir(folder) or (os.path.abspath(folder) != os.path.abspath(root) and _is_link_or_junction(folder)) or not _is_within_root(folder, root_real):
            continue
        try:
            for entry in os.scandir(folder):
                if not entry.is_symlink() and entry.is_file(follow_symlinks=False) and _is_within_root(entry.path, root_real) and entry.name.casefold().endswith((".sqlite", ".sqlite3", ".db")):
                    paths.append(entry.path)
        except OSError:
            continue
    return sorted(set(paths), key=str.casefold)


def _read_metadata(database: DatabaseSnapshot, index: dict[str, dict[str, Any]], by_id: dict[str, SessionInfo], issues: list[str], root: str = "", cancel: threading.Event | None = None) -> None:
    if not database.snapshot_path:
        return
    # Metadata tables can contain many rows for the same session. Keep the
    # canonical rollout paths once per session instead of rebuilding a set and
    # resolving every existing path for every duplicate row.
    canonical_paths_by_id: dict[str, set[str]] = {}
    rollout_validation_cache: dict[str, tuple[bool, str, bool]] = {}
    try:
        connection = _connect_readonly(database.snapshot_path)
        _enable_sqlite_cancellation(connection, cancel)
        for schema in database.tables:
            if cancel is not None and cancel.is_set():
                break
            if not schema.metadata_id:
                continue
            try:
                lowered_columns = {column.casefold(): column for column in schema.columns}
                needed = [schema.metadata_id, schema.title]
                for key in (
                    "cwd", "working_directory", "workdir",
                    "created_at", "created_at_ms", "source_created_at", "created_time", "created",
                    "updated_at", "updated_at_ms", "source_updated_at", "recency_at", "last_updated",
                    "rollout_path", "session_path", "path", "archived", "is_archived",
                    "source_kind", "originator",
                ):
                    if key in lowered_columns:
                        needed.append(lowered_columns[key])
                columns = list(dict.fromkeys(column for column in needed if column))
                projection = ", ".join(_quote_identifier(column) for column in columns)
                cursor = connection.execute(f"SELECT {projection} FROM {_quote_identifier(schema.name)}")
                for row in cursor:
                    if cancel is not None and cancel.is_set():
                        break
                    data = dict(zip(columns, row))
                    sid = str(data.get(schema.metadata_id) or "").strip()
                    if not sid:
                        continue
                    item = by_id.setdefault(sid, SessionInfo(session_id=sid))
                    known_paths = canonical_paths_by_id.setdefault(
                        sid,
                        {
                            _canonical_path(existing)
                            for existing in item.paths
                            if " :: " not in existing
                        },
                    )
                    title_column = schema.title
                    title = data.get(title_column) if title_column else ""
                    title_candidate = _normalize_title(title)
                    if title_candidate and (
                        not _normalize_title(item.title)
                        or _is_generic_title(item.title)
                        or len(_normalize_title(item.title)) > 180
                    ):
                        item.title = title_candidate
                    lowered = {k.casefold(): v for k, v in data.items()}
                    source_kind = str(lowered.get("source_kind") or "").strip().casefold()
                    if source_kind == "chatgpt":
                        item.platform = _merge_platform(item.platform, "chatgpt")
                    elif source_kind in {"codex", "vscode", "cli"}:
                        item.platform = _merge_platform(item.platform, "codex")
                    originator = str(lowered.get("originator") or "").strip().casefold()
                    if "codex" in originator:
                        item.platform = _merge_platform(item.platform, "codex")
                    cwd = next((lowered[k] for k in ("cwd", "working_directory", "workdir") if lowered.get(k)), "")
                    if cwd and not item.cwd:
                        item.cwd = str(cwd)
                    raw_created = next((lowered[k] for k in ("created_at", "created_at_ms", "source_created_at", "created_time", "created") if lowered.get(k) is not None), None)
                    raw_updated = next((lowered[k] for k in ("updated_at", "updated_at_ms", "source_updated_at", "recency_at", "last_updated") if lowered.get(k) is not None), None)
                    created = _epoch_string(raw_created)
                    updated = _epoch_string(raw_updated)
                    if created and (not item.raw_created_at or _time_sort_key(created) < _time_sort_key(item.raw_created_at)):
                        item.raw_created_at = created
                    if updated and _time_sort_key(updated) >= _time_sort_key(item.raw_updated_at):
                        item.raw_updated_at = updated
                    rollout = next((lowered[k] for k in ("rollout_path", "session_path", "path") if lowered.get(k)), "")
                    if rollout:
                        rollout_path = str(rollout)
                        validation = rollout_validation_cache.get(rollout_path)
                        if validation is None:
                            within_root = not root or _is_within_root(rollout_path, root)
                            canonical_rollout = _canonical_path(rollout_path)
                            exists = os.path.isfile(rollout_path) if within_root else False
                            validation = (within_root, canonical_rollout, exists)
                            # Keep duplicate metadata rows cheap without
                            # allowing an unbounded cache for unique paths.
                            if len(rollout_validation_cache) < 100_000:
                                rollout_validation_cache[rollout_path] = validation
                        within_root, canonical_rollout, exists = validation
                        if root and not within_root:
                            issue = f"{database.source_path} / {schema.name}：数据库记录路径位于所选目录之外，已忽略"
                            if issue not in issues:
                                issues.append(issue)
                        else:
                            if exists and canonical_rollout not in known_paths:
                                item.paths.append(rollout_path)
                                known_paths.add(canonical_rollout)
                    source_name = os.path.basename(database.source_path)
                    if "SQLite 元数据" not in item.source:
                        item.source = f"SQLite 元数据（{source_name}）" if not item.source else item.source + f" + SQLite 元数据（{source_name}）"
                    archived = lowered.get("archived") or lowered.get("is_archived")
                    if archived not in (None, ""):
                        item.archived = bool(int(archived)) if str(archived).isdigit() else str(archived).casefold() in {"true", "yes", "archived"}
                    if sid in index:
                        idx = index[sid]
                        idx_title = idx.get("thread_name") or idx.get("title")
                        idx_candidate = _normalize_title(idx_title)
                        if idx_candidate and (
                            not _normalize_title(item.title)
                            or _is_generic_title(item.title)
                            or len(_normalize_title(item.title)) > 180
                        ):
                            item.title = idx_candidate
                if cancel is not None and cancel.is_set():
                    break
            except sqlite3.Error as exc:
                if cancel is None or not cancel.is_set():
                    issues.append(f"{database.source_path} / 表 {schema.name}：{type(exc).__name__}: {exc}")
        connection.close()
    except Exception as exc:
        if cancel is None or not cancel.is_set():
            issues.append(f"{database.source_path}：数据库元数据读取失败：{type(exc).__name__}: {exc}")


def _index_entries(root: str, issues: list[str], cancel: threading.Event | None = None) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    path = os.path.join(root, "session_index.jsonl")
    if not os.path.isfile(path):
        return result
    bad = 0
    bad_types: Counter[str] = Counter()
    bad_samples: list[int] = []
    try:
        for line_no, line in _iter_jsonl(path):
            if cancel is not None and cancel.is_set():
                break
            try:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise TypeError("顶层 JSON 不是对象")
                sid = str(value.get("id") or value.get("session_id") or "")
                if sid:
                    result[sid] = {
                        "thread_name": value.get("thread_name") if isinstance(value.get("thread_name"), (str, int, float)) else None,
                        "title": value.get("title") if isinstance(value.get("title"), (str, int, float)) else None,
                        "updated_at": value.get("updated_at") if isinstance(value.get("updated_at"), (str, int, float)) else None,
                    }
            except Exception as exc:
                bad += 1
                bad_types[type(exc).__name__] += 1
                if len(bad_samples) < 5:
                    bad_samples.append(line_no)
    except OSError as exc:
        issues.append(f"session_index.jsonl 读取失败：{exc}")
    if bad:
        issue = f"session_index.jsonl：{bad} 行损坏，错误类型 {', '.join(f'{name} {count} 行' for name, count in sorted(bad_types.items()))}；示例行：{', '.join(map(str, bad_samples))}"
        issues.append(issue)
    return result


def _inspect_history(root: str, notes: list[str], issues: list[str], cancel: threading.Event | None = None) -> None:
    path = os.path.join(root, "history.jsonl")
    if not os.path.isfile(path):
        notes.append("history.jsonl：未找到；不影响以 rollout 为主的数据读取")
        return
    total = 0
    valid = 0
    bad = 0
    bad_types: Counter[str] = Counter()
    bad_samples: list[int] = []
    keys: set[str] = set()
    keys_truncated = False
    try:
        for line_no, line in _iter_jsonl(path):
            if cancel is not None and cancel.is_set():
                break
            total += 1
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    valid += 1
                    for key in value.keys():
                        rendered = str(key)[:120]
                        if rendered in keys:
                            continue
                        if len(keys) < 128:
                            keys.add(rendered)
                        else:
                            keys_truncated = True
                else:
                    bad += 1
                    bad_types["非对象 JSON"] += 1
                    if len(bad_samples) < 5:
                        bad_samples.append(line_no)
            except Exception as exc:
                bad += 1
                bad_types[type(exc).__name__] += 1
                if len(bad_samples) < 5:
                    bad_samples.append(line_no)
        key_summary = ", ".join(sorted(keys)) or "未识别"
        if keys_truncated:
            key_summary += "，另有未列出的字段"
        notes.append(f"history.jsonl：检查 {total} 行，{valid} 行有效，{bad} 行损坏；字段 {key_summary}。仅作为辅助索引，不作完整聊天记录。")
        if bad:
            issues.append(
                f"history.jsonl：{bad} 行损坏，错误类型 {', '.join(f'{name} {count} 行' for name, count in sorted(bad_types.items()))}；"
                f"示例行：{', '.join(map(str, bad_samples))}"
            )
    except OSError as exc:
        issues.append(f"history.jsonl 读取失败：{type(exc).__name__}: {exc}")


def _scan_rollout_file(path: str, session: SessionInfo, cancel: threading.Event, progress: Callable[[str, int], None] | None = None, seen_ids: set[str] | None = None, seen_hashes: set[str] | None = None) -> None:
    session.platform = _merge_platform(session.platform, "codex")
    seen_ids = seen_ids if seen_ids is not None else set()
    seen_hashes = seen_hashes if seen_hashes is not None else set()
    counts = Counter(session.type_counts)
    search_parts = [session.search_text] if session.search_text else []
    search_length = len(session.search_text)
    malformed_before = len(session.issues)
    issue_keys = set(session.issues)
    diagnostics: dict[str, list[Any]] = {}
    meta_created = ""
    meta_cwd = ""
    latest_timestamp_key = _time_sort_key(session.raw_updated_at)

    def add_issue(message: str) -> None:
        if message not in issue_keys:
            issue_keys.add(message)
            session.issues.append(message)

    def note_diagnostic(key: str, label: str, line_no: int) -> None:
        diagnostic = diagnostics.setdefault(key, [label, 0, []])
        diagnostic[1] += 1
        if len(diagnostic[2]) < 5:
            diagnostic[2].append(line_no)

    def flush_diagnostics() -> None:
        filename = os.path.basename(path)
        for label, count, sample_lines in diagnostics.values():
            sample = ", ".join(str(line_no) for line_no in sample_lines)
            add_issue(f"{filename}：{label}，共 {count} 行；示例行：{sample}")
        diagnostics.clear()

    try:
        for line_no, line in _iter_jsonl(path):
            if cancel.is_set():
                session.status = "扫描已取消（不完整）"
                add_issue(f"{os.path.basename(path)}：扫描在第 {line_no} 行取消，计数和搜索索引可能不完整")
                flush_diagnostics()
                session.type_counts = dict(counts)
                session.search_text = "".join(search_parts)
                return
            try:
                entry = json.loads(line)
            except Exception as exc:
                error_type = type(exc).__name__
                note_diagnostic(f"bad-json:{error_type}", f"JSON 损坏（{error_type}）", line_no)
                continue
            if not isinstance(entry, dict):
                counts["<非对象 JSON>"] += 1
                continue
            record_type = str(entry.get("type", "<未知>") or "<未知>")
            payload = entry.get("payload")
            if record_type == "response_item" and isinstance(payload, dict):
                if not isinstance(payload.get("type"), str):
                    note_diagnostic("response_item.type", "response_item.type 不是文本，按未知条目处理", line_no)
                if _is_user_message_item(payload):
                    session.attachment_ids.update(_codex_attachment_ids(payload.get("content", payload.get("text", ""))))
            elif record_type == "event_msg" and isinstance(payload, dict) and _event_label(payload) == "item_completed":
                item = payload.get("item")
                if isinstance(item, dict) and not isinstance(item.get("type"), str):
                    note_diagnostic("item_completed.item.type", "item_completed.item.type 不是文本，按未知条目处理", line_no)
                if _is_user_message_item(item):
                    session.attachment_ids.update(_codex_attachment_ids(item.get("content", item.get("text", ""))))
                    first_user_text = _user_message_text(item)
                    if first_user_text and not session.first_user_message:
                        session.first_user_message = first_user_text
            elif record_type == "event_msg" and isinstance(payload, dict) and _event_label(payload) == "user_message":
                user_text = payload.get("message")
                if isinstance(user_text, str) and user_text.strip() and not session.first_user_message:
                    session.first_user_message = " ".join(user_text.split())
            raw_timestamp = _epoch_string(entry.get("timestamp"))
            if raw_timestamp:
                timestamp_key = _time_sort_key(raw_timestamp)
                if timestamp_key >= latest_timestamp_key:
                    session.raw_updated_at = raw_timestamp
                    latest_timestamp_key = timestamp_key
            counts[record_type] += 1
            session.event_count += 1
            if record_type not in {"session_meta", "event_msg", "response_item", "turn_context", "token_usage_record", "world_state", *STATUS_RECORD_TYPES}:
                note_diagnostic(f"unknown-record:{record_type[:120]}", f"未知记录类型 {record_type[:120]}", line_no)
            if record_type == "session_meta" and isinstance(entry.get("payload"), dict):
                meta = entry["payload"]
                sid = meta.get("session_id") or meta.get("id")
                if sid and str(sid) != session.session_id:
                    add_issue(f"{os.path.basename(path)} 中会话 ID 与文件归属不一致：{sid}")
                meta_created = _epoch_string(meta.get("timestamp"))
                meta_cwd = str(meta.get("cwd") or "")
                continue
            payload = entry.get("payload")
            if record_type == "response_item" and isinstance(payload, dict):
                item_id = str(payload.get("id") or "")
                digest = "" if item_id else _record_digest(payload)
                if (item_id and item_id in seen_ids) or (not item_id and digest in seen_hashes):
                    session.duplicate_count += 1
                    continue
                if item_id:
                    seen_ids.add(item_id)
                else:
                    seen_hashes.add(digest)
                raw_item_type = payload.get("type", "unknown")
                item_type = raw_item_type if isinstance(raw_item_type, str) else "unknown"
                if item_type == "message":
                    role = str(payload.get("role", ""))
                    phase = str(payload.get("phase", "")).casefold()
                    if phase not in {"analysis", "justify", "confidence", "delegate_summary", "juicepred"}:
                        attachments: list[dict[str, Any]] = []
                        text = _content_text(payload.get("content", ""), attachments)
                        if role == "user":
                            session.user_count += 1
                            first_user_text = _user_message_text(payload)
                            if first_user_text and not session.first_user_message:
                                session.first_user_message = first_user_text
                        elif role == "assistant":
                            session.assistant_count += 1
                        if text:
                            search_length = _append_search_part(search_parts, search_length, text)
                elif item_type in {"userMessage", "user_message"}:
                    session.user_count += 1
                    first_user_text = _user_message_text(payload)
                    if first_user_text and not session.first_user_message:
                        session.first_user_message = first_user_text
                    text = _content_text(payload.get("content", payload.get("text", "")))
                    if text:
                        search_length = _append_search_part(search_parts, search_length, text)
                elif item_type == "reasoning":
                    continue
                elif item_type.endswith("_call") or item_type in {"custom_tool_call", "function_call"}:
                    session.tool_count += 1
                    for k in ("name", "input", "arguments"):
                        val = payload.get(k)
                        if isinstance(val, str):
                            search_length = _append_search_part(search_parts, search_length, val)
                elif item_type.endswith("_output"):
                    val = _content_text(payload.get("output", payload.get("content", "")))
                    if val:
                        search_length = _append_search_part(search_parts, search_length, val)
            elif record_type == "event_msg" and isinstance(payload, dict):
                event_kind = _event_label(payload)
                if event_kind not in {"item_completed", "token_count", "task_started", "task_complete", "turn_aborted", "thread_settings_applied", "user_message", "error", "turn_abandoned", "agent_message_delta", "exec_command_begin", "exec_command_end"}:
                    note_diagnostic(f"unknown-event:{event_kind[:120]}", f"未知 event_msg 类型 {event_kind[:120]}", line_no)
                if event_kind not in {"item_completed", "token_count"}:
                    if event_kind not in {"task_started", "task_complete", "turn_aborted", "thread_settings_applied"}:
                        counts["event_msg:" + event_kind] += 0
                    for k in ("message", "error", "reason", "stderr", "stdout"):
                        val = payload.get(k)
                        if isinstance(val, str):
                            search_length = _append_search_part(search_parts, search_length, val)
            if progress and line_no % 3000 == 0:
                progress(f"读取 {os.path.basename(path)}：{line_no:,} 行", -1)
    except Exception as exc:
        add_issue(f"{os.path.basename(path)} 读取失败：{type(exc).__name__}: {exc}")
    flush_diagnostics()
    if meta_created and (not session.raw_created_at or _time_sort_key(meta_created) < _time_sort_key(session.raw_created_at)):
        session.raw_created_at = meta_created
    if meta_created and _time_sort_key(meta_created) > _time_sort_key(session.raw_updated_at):
        session.raw_updated_at = meta_created
    if meta_cwd and not session.cwd:
        session.cwd = meta_cwd
    if path not in session.paths:
        session.paths.append(path)
    session.type_counts = dict(counts)
    session.search_text = "".join(search_parts)
    session.archived = session.archived or os.path.basename(os.path.dirname(path)).casefold() == "archived_sessions"
    if len(session.issues) > malformed_before:
        session.status = "部分可读，有报告项"


def _search_database_fallback_many(sessions: list[SessionInfo], databases: list[DatabaseSnapshot], cancel: threading.Event, issues: list[str]) -> None:
    full_fallback = [session for session in sessions if not any(os.path.isfile(path) for path in session.paths)]
    title_fallback = [
        session for session in sessions
        if any(os.path.isfile(path) for path in session.paths)
        and not session.first_user_message
        and (not _normalize_title(session.title) or _is_generic_title(session.title) or len(_normalize_title(session.title)) > 180)
    ]
    eligible = full_fallback + title_fallback
    if not eligible:
        return
    title_fallback_ids = {session.session_id for session in title_fallback}
    by_id = {session.session_id: session for session in eligible}
    states = {
        session.session_id: {
            "seen": set(),
            "parts": [session.search_text] if session.search_text else [],
            "length": len(session.search_text),
            "found_tables": set(),
        }
        for session in eligible
    }

    def consume(database: DatabaseSnapshot, schema: TableSchema, lowered: dict[str, str], columns: list[str], row: tuple[Any, ...]) -> None:
        data = dict(zip(columns, row))
        raw_id = data.get(lowered.get("thread_id") or lowered.get("session_id", ""))
        if not isinstance(raw_id, str):
            return
        session = by_id.get(raw_id.strip())
        if session is None:
            return
        state = states[session.session_id]
        if session.session_id in title_fallback_ids and session.first_user_message:
            return
        try:
            item = _decode_database_json(data.get(schema.item_json))
        except Exception:
            session.issues.append(f"{os.path.basename(database.source_path)} / {schema.name}：数据库 JSON 条目损坏")
            return
        if not isinstance(item, dict):
            return
        if session.session_id in title_fallback_ids:
            kind = str(item.get("type") or data.get(schema.item_type, "unknown"))
            if kind not in {"userMessage", "user_message"} and not (kind == "message" and item.get("role") == "user"):
                return
            title_item = item if _is_user_message_item(item) else {
                "type": kind,
                "role": item.get("role"),
                "content": item.get("content"),
                "text": item.get("text"),
            }
            first_user_text = _user_message_text(title_item)
            if first_user_text:
                session.first_user_message = first_user_text
                state["found_tables"].add((database.source_path, schema.name))
            return
        state["found_tables"].add((database.source_path, schema.name))
        item_id = str(item.get("id") or data.get(lowered.get("item_id", "")) or "")
        dedupe_key = item_id or hashlib.sha256(
            json.dumps(item, ensure_ascii=False, sort_keys=True).encode("utf-8", "replace")
        ).hexdigest()
        if dedupe_key in state["seen"]:
            session.duplicate_count += 1
            return
        state["seen"].add(dedupe_key)
        kind = str(item.get("type") or data.get(schema.item_type, "unknown"))
        session.type_counts[kind] = session.type_counts.get(kind, 0) + 1
        session.event_count += 1
        if kind in {"userMessage", "user_message"} or (kind == "message" and item.get("role") == "user"):
            session.user_count += 1
            text = _content_text(item.get("content", item.get("text", "")))
            first_user_text = _user_message_text(item)
            if first_user_text and not session.first_user_message:
                session.first_user_message = first_user_text
        elif kind in {"agentMessage", "assistantMessage", "assistant_message"}:
            session.assistant_count += 1
            text = str(item.get("text") or _content_text(item.get("content", "")))
        elif kind.casefold() in {"reasoning", "thought"}:
            return
        else:
            text = _content_text(item)
            if "tool" in kind.casefold() or "command" in kind.casefold():
                session.tool_count += 1
        if text:
            state["length"] = _append_search_part(state["parts"], state["length"], text)

    for database in databases:
        if cancel.is_set() or not database.snapshot_path:
            continue
        connection: sqlite3.Connection | None = None
        try:
            connection = _connect_readonly(database.snapshot_path)
            _enable_sqlite_cancellation(connection, cancel)
            for schema in database.tables:
                if cancel.is_set():
                    break
                if not schema.item_json:
                    continue
                lowered = {column.casefold(): column for column in schema.columns}
                thread_col = lowered.get("thread_id") or lowered.get("session_id")
                if not thread_col:
                    continue
                order_column = schema.order_column if schema.order_column in schema.columns else ""
                columns = list(dict.fromkeys(
                    column for column in (thread_col, schema.item_json, lowered.get("item_id", ""), schema.item_type, order_column) if column
                ))
                projection = ", ".join(_quote_identifier(column) for column in columns)
                order_clause = f" ORDER BY {_quote_identifier(order_column)} ASC" if order_column else ""
                filtered_sql = f"SELECT {projection} FROM {_quote_identifier(schema.name)} WHERE {_quote_identifier(thread_col)} = ?{order_clause}"
                plan = connection.execute("EXPLAIN QUERY PLAN " + filtered_sql, (eligible[0].session_id,)).fetchall()
                indexed = any("USING" in str(row[-1]).upper() and "INDEX" in str(row[-1]).upper() for row in plan)
                try:
                    if indexed:
                        for session in eligible:
                            if cancel.is_set():
                                break
                            cursor = connection.execute(filtered_sql, (session.session_id,))
                            try:
                                for row in cursor:
                                    if cancel.is_set():
                                        break
                                    consume(database, schema, lowered, columns, row)
                            finally:
                                cursor.close()
                    else:
                        connection.execute("DROP TABLE IF EXISTS temp._codex_hv_wanted_thread_ids")
                        connection.execute(
                            "CREATE TEMP TABLE _codex_hv_wanted_thread_ids "
                            "(thread_id TEXT PRIMARY KEY) WITHOUT ROWID"
                        )
                        def wanted_session_ids() -> Iterable[tuple[str]]:
                            for session in eligible:
                                if cancel.is_set():
                                    break
                                yield (session.session_id,)

                        connection.executemany(
                            "INSERT OR IGNORE INTO temp._codex_hv_wanted_thread_ids(thread_id) VALUES(?)",
                            wanted_session_ids(),
                        )
                        if cancel.is_set():
                            break
                        source_projection = ", ".join(
                            f"source.{_quote_identifier(column)}" for column in columns
                        )
                        cursor = connection.execute(
                            f"SELECT {source_projection} FROM {_quote_identifier(schema.name)} AS source "
                            f"JOIN temp._codex_hv_wanted_thread_ids AS wanted "
                            f"ON source.{_quote_identifier(thread_col)} = wanted.thread_id"
                            + (f" ORDER BY source.{_quote_identifier(order_column)} ASC" if order_column else "")
                        )
                        try:
                            for row in cursor:
                                if cancel.is_set():
                                    break
                                consume(database, schema, lowered, columns, row)
                        finally:
                            cursor.close()
                except sqlite3.Error as exc:
                    if not cancel.is_set():
                        issues.append(f"{database.source_path} / {schema.name}：回退读取失败：{type(exc).__name__}: {exc}")
        except Exception as exc:
            if not cancel.is_set():
                issues.append(f"{database.source_path}：回退读取失败：{type(exc).__name__}: {exc}")
        finally:
            if connection is not None:
                connection.close()

    for session in eligible:
        state = states[session.session_id]
        for source_path, table_name in state["found_tables"]:
            source_path_label = source_path + " :: " + table_name
            if source_path_label not in session.paths:
                session.paths.append(source_path_label)
            fallback_label = "标题回退" if session.session_id in title_fallback_ids else "条目回退"
            source_label = f"SQLite {fallback_label}（{os.path.basename(source_path)} / {table_name}）"
            if source_label not in session.source:
                session.source = source_label if not session.source else session.source + " + " + source_label
        session.search_text = "".join(state["parts"])


def _search_database_fallback(session: SessionInfo, databases: list[DatabaseSnapshot], cancel: threading.Event, issues: list[str]) -> None:
    _search_database_fallback_many([session], databases, cancel, issues)


def _prepare_batch_snapshot_indexes(
    sessions: list[SessionInfo],
    catalog: Catalog,
    cancel: threading.Event,
    progress: Callable[[str, int], None] | None = None,
) -> dict[str, str]:
    """Index app-owned SQLite snapshots once before exporting multiple DB-only sessions."""
    database_only = [
        session for session in sessions
        if not any(os.path.isfile(path) for path in session.paths)
    ]
    if len(database_only) < 2 or not catalog.temp_dir:
        return {}
    try:
        snapshot_root = os.path.realpath(catalog.temp_dir)
    except OSError:
        return {}

    needed_labels = {
        path
        for session in database_only
        for path in session.paths
        if " :: " in path
    }
    errors: dict[str, str] = {}
    for database in catalog.databases:
        if cancel.is_set():
            break
        if not database.snapshot_path or not database.tables:
            continue
        snapshot_path = os.path.realpath(database.snapshot_path)
        try:
            if os.path.commonpath((snapshot_root, snapshot_path)) != snapshot_root:
                continue
        except ValueError:
            continue
        if os.path.normcase(snapshot_path) == os.path.normcase(os.path.realpath(database.source_path)):
            continue
        if os.path.exists(database.source_path):
            try:
                if os.path.samefile(snapshot_path, database.source_path):
                    continue
            except OSError:
                continue
        schemas = []
        for schema in database.tables:
            lowered = {column.casefold(): column for column in schema.columns}
            thread_col = lowered.get("thread_id") or lowered.get("session_id")
            if not schema.item_json or not thread_col or schema.thread_id_indexed:
                continue
            source_label = database.source_path + " :: " + schema.name
            if source_label not in needed_labels:
                continue
            schemas.append((schema, thread_col))
        if not schemas:
            continue
        connection: sqlite3.Connection | None = None
        try:
            if progress:
                progress(f"为批量数据库会话建立临时查询索引：{os.path.basename(database.source_path)}", -1)
            connection = sqlite3.connect(snapshot_path, timeout=5.0)
            _enable_sqlite_cancellation(connection, cancel)
            for schema, thread_col in schemas:
                if cancel.is_set():
                    break
                columns = [thread_col]
                if schema.order_column and schema.order_column in schema.columns and schema.order_column != thread_col:
                    columns.append(schema.order_column)
                index_name = "codex_hv_thread_" + hashlib.sha256(
                    (database.source_path + "\0" + schema.name).encode("utf-8", "replace")
                ).hexdigest()[:20]
                projection = ", ".join(_quote_identifier(column) for column in columns)
                connection.execute(
                    f"CREATE INDEX IF NOT EXISTS {_quote_identifier(index_name)} "
                    f"ON {_quote_identifier(schema.name)} ({projection})"
                )
            if not cancel.is_set():
                connection.commit()
            else:
                connection.rollback()
        except (OSError, sqlite3.Error) as exc:
            if not cancel.is_set():
                errors[database.source_path] = f"SQLite 临时查询索引创建失败（{type(exc).__name__}: {exc}）"
        finally:
            if connection is not None:
                connection.close()
    return errors


def _filename_id(path: str) -> str:
    name = os.path.basename(path)
    stem = name.removesuffix(".jsonl.zst").removesuffix(".jsonl")
    match = re.search(r"([0-9a-fA-F]{8}-[0-9a-fA-F-]{27,})", stem)
    if match:
        return match.group(1).rstrip("_")
    parts = re.split(r"[_-]", stem)
    return parts[-1] if parts else stem


def _measure_session_disk_usage(
    session: SessionInfo, root: str, cancel: threading.Event | None = None
) -> tuple[int | None, str]:
    """Measure only files that can be attributed to this session without guessing."""
    cancel = cancel or threading.Event()
    root_real = os.path.realpath(root)
    seen_paths: set[str] = set()
    total_bytes = 0
    file_count = 0
    partial = False
    shared_source = False

    def add_file(path: str | Path) -> None:
        nonlocal total_bytes, file_count, partial
        if cancel.is_set():
            partial = True
            return
        try:
            absolute = os.path.abspath(path)
            if not _is_within_root(absolute, root_real) or _has_link_or_junction_component(absolute, root_real):
                partial = True
                return
            key = os.path.normcase(os.path.realpath(absolute))
            if key in seen_paths:
                return
            metadata = os.stat(absolute, follow_symlinks=False)
            if not stat.S_ISREG(metadata.st_mode):
                return
            seen_paths.add(key)
            total_bytes += metadata.st_size
            file_count += 1
        except OSError:
            partial = True

    for source_path in session.paths:
        if cancel.is_set():
            partial = True
            break
        if " :: " in source_path:
            shared_source = True
            continue
        basename = os.path.basename(source_path).casefold()
        if basename in {"session_index.jsonl", "history.jsonl"}:
            shared_source = True
            continue
        if basename.startswith("rollout-") and basename.endswith((".jsonl", ".jsonl.zst")):
            add_file(source_path)

    attachment_root = Path(root) / "attachments"
    referenced_attachment_ids = set(session.attachment_ids)
    related_ids = {session.session_id, *session.attachment_ids}
    for attachment_id in related_ids:
        if cancel.is_set():
            partial = True
            break
        folder = attachment_root / attachment_id
        folder_state, error_type = _attachment_folder_state(folder, root_real)
        if folder_state == "missing":
            if attachment_id in referenced_attachment_ids:
                partial = True
            continue
        if folder_state != "available":
            partial = True
            if folder_state == "unsafe":
                message = f"关联附件目录包含符号链接或 junction，已跳过：{folder}"
            elif folder_state == "inaccessible":
                message = f"附件目录检查失败：{folder}（{error_type or '访问错误'}）"
            else:
                message = f"关联附件路径不是目录，已跳过：{folder}"
            if message not in session.issues:
                session.issues.append(message)
            continue
        budget = _AttachmentListingBudget(100_000)
        attachment_issues: list[str] = []
        files = _list_attachment_folder_files(
            folder,
            root_real,
            cancel=cancel,
            issues=attachment_issues,
            budget=budget,
        )
        for issue in attachment_issues:
            if "附件列表预览达到" in issue or "附件目录预览达到" in issue:
                continue
            if issue not in session.issues:
                session.issues.append(issue)
        partial = partial or bool(attachment_issues)
        partial = partial or budget.limit_reported
        for file_path in files:
            add_file(file_path)
            if cancel.is_set():
                partial = True
                break

    if partial:
        message = "会话本地文件占用统计未能完整确认，显示值可能偏小"
        if message not in session.issues:
            session.issues.append(message)
    if file_count:
        return total_bytes, "部分统计" if partial else "已统计"
    if partial:
        return None, "未能完整统计"
    if shared_source or session.platform == "chatgpt":
        return None, "共享数据库 / 索引"
    return None, "未找到本地记录文件"


def scan_codex_home(root: str, cancel: threading.Event | None = None, progress: Callable[[str, int], None] | None = None) -> Catalog:
    cancel = cancel or threading.Event()
    root = os.path.abspath(os.path.expandvars(root))
    catalog = Catalog(root=root, temp_dir=tempfile.mkdtemp(prefix="codex-history-viewer-"))
    try:
        if not os.path.isdir(root):
            raise NotADirectoryError("路径不存在或不是文件夹")
        with os.scandir(root):
            pass
    except OSError as exc:
        catalog.root_valid = False
        catalog.issues.append(f"Codex 数据目录无法读取：{root}（{type(exc).__name__}: {exc}）")
        return catalog
    imported_sessions: dict[str, dict[str, Any]] = {}
    import_metadata_path = Path(root) / "codex_history_import.json"
    try:
        if import_metadata_path.is_file() and not _is_link_or_junction(import_metadata_path):
            import_metadata = json.loads(import_metadata_path.read_text(encoding="utf-8"))
            imported_value = import_metadata.get("sessions") if isinstance(import_metadata, dict) else None
            if isinstance(imported_value, dict):
                imported_sessions = {
                    str(session_id): value
                    for session_id, value in imported_value.items()
                    if isinstance(value, dict)
                }
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        catalog.issues.append(f"导入来源标记无法读取：{type(exc).__name__}")
    by_id: dict[str, SessionInfo] = {}
    index = _index_entries(root, catalog.issues, cancel)
    _inspect_history(root, catalog.notes, catalog.issues, cancel)
    for sid, entry in index.items():
        session = by_id.setdefault(sid, SessionInfo(session_id=sid))
        title = entry.get("thread_name") or entry.get("title")
        if title:
            session.title = str(title)
        updated = _epoch_string(entry.get("updated_at"))
        if updated:
            session.raw_updated_at = updated

    database_paths = _iter_candidate_databases(root)
    for i, path in enumerate(database_paths):
        if cancel.is_set():
            catalog.issues.append("数据库扫描已取消")
            break
        database = _open_database_snapshot(path, catalog.temp_dir, cancel)
        catalog.databases.append(database)
        if database.error:
            catalog.issues.append(f"{path}：{database.error}")
        elif progress:
            recognized = [t.name for t in database.tables if t.metadata_id or t.item_json]
            progress(f"已检查数据库 {i + 1}/{len(database_paths)}：{os.path.basename(path)}（{', '.join(recognized) or '无相关表'}）", -1)
        _read_metadata(database, index, by_id, catalog.issues, root, cancel)
        _discover_database_item_sessions(database, by_id, catalog.issues, cancel)

    rollout_paths: list[str] = []
    for folder in (os.path.join(root, "sessions"), os.path.join(root, "archived_sessions")):
        folder_path = Path(folder)
        try:
            folder_metadata = folder_path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            catalog.issues.append(f"会话目录检查失败：{folder}（{type(exc).__name__}: {exc}）")
            continue
        if _has_link_or_junction_component_reported(folder_path, root, catalog.issues, "会话目录"):
            message = f"会话目录包含链接或无法检查的路径组件，已跳过：{folder}"
            if message not in catalog.issues:
                catalog.issues.append(message)
            continue
        if stat.S_ISDIR(folder_metadata.st_mode):
            def report_walk_error(error: OSError) -> None:
                location = error.filename or folder
                message = f"记录目录读取失败：{location}（{type(error).__name__}）"
                if message not in catalog.issues:
                    catalog.issues.append(message)

            pending_directories = [Path(folder)]
            while pending_directories and not cancel.is_set():
                directory = pending_directories.pop()
                try:
                    with os.scandir(directory) as entries:
                        for entry in entries:
                            if cancel.is_set():
                                break
                            path = Path(entry.path)
                            if _is_link_or_junction_reported(path, catalog.issues, "会话路径"):
                                continue
                            try:
                                is_directory = entry.is_dir(follow_symlinks=False)
                            except OSError as exc:
                                report_walk_error(exc)
                                continue
                            if is_directory:
                                pending_directories.append(path)
                                continue
                            lowered = entry.name.casefold()
                            if lowered.startswith("rollout-") and (lowered.endswith(".jsonl") or lowered.endswith(".jsonl.zst")):
                                if _has_link_or_junction_component(path, root) or not _is_within_root(str(path), root):
                                    catalog.issues.append(f"忽略符号链接或所选目录之外的记录文件：{path}")
                                    continue
                                rollout_paths.append(str(path))
                except OSError as exc:
                    report_walk_error(exc)
                if progress and len(rollout_paths) and len(rollout_paths) % 1000 == 0:
                    progress(f"发现会话记录文件：{len(rollout_paths):,} 个", -1)
    rollout_paths.sort(key=str.casefold)
    seen_response_ids: dict[str, set[str]] = {}
    seen_response_hashes: dict[str, set[str]] = {}
    total_bytes = 0
    for path in rollout_paths:
        if cancel.is_set():
            break
        try:
            total_bytes += os.path.getsize(path)
        except OSError as exc:
            issue = f"会话记录文件属性读取失败：{path}（{type(exc).__name__}）"
            if issue not in catalog.issues:
                catalog.issues.append(issue)
    done_bytes = 0
    for index_path, path in enumerate(rollout_paths):
        if cancel.is_set():
            catalog.issues.append("會話掃描已取消；已完成的項目仍可查看")
            break
        sid = _filename_id(path)
        try:
            first = None
            for _, line in _iter_jsonl(path):
                if cancel.is_set():
                    break
                if not line.strip():
                    continue
                candidate = json.loads(line)
                if isinstance(candidate, dict) and candidate.get("type") == "session_meta":
                    first = candidate
                    break
            if isinstance(first, dict):
                payload = first.get("payload", {})
                sid = str(payload.get("session_id") or payload.get("id") or sid)
        except StopIteration:
            pass
        except Exception as exc:
            catalog.issues.append(f"{path}：無法读取首条元数据：{type(exc).__name__}: {exc}")
        session = by_id.setdefault(sid, SessionInfo(session_id=sid))
        relative_parts = Path(os.path.relpath(path, root)).parts
        archived_path = bool(relative_parts and relative_parts[0].casefold() == "archived_sessions")
        if archived_path:
            session.archived = True
        if path.casefold().endswith(".zst"):
            session.archived = True
        if _canonical_path(path) not in {_canonical_path(existing) for existing in session.paths if " :: " not in existing}:
            session.paths.append(path)
        source_label = "归档 JSONL.ZST" if path.casefold().endswith(".zst") else "归档 rollout JSONL" if archived_path else "rollout JSONL"
        if source_label not in session.source:
            session.source = source_label if not session.source else session.source + " + " + source_label
        try:
            _scan_rollout_file(
                path,
                session,
                cancel,
                progress,
                seen_response_ids.setdefault(sid, set()),
                seen_response_hashes.setdefault(sid, set()),
            )
        except Exception as exc:
            session.issues.append(f"{path}：{type(exc).__name__}: {exc}")
            session.status = "读取失败"
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        done_bytes += size
        catalog.files_scanned += 1
        catalog.bytes_scanned += size
        if progress:
            pct = int(done_bytes * 100 / total_bytes) if total_bytes else 100
            progress(f"扫描会话 {index_path + 1}/{len(rollout_paths)}：{os.path.basename(path)}", pct)

    catalog.cancelled = cancel.is_set()
    if catalog.cancelled:
        catalog.issues.append("扫描已取消；已完成和部分读取的会话均已保留，请刷新以获取完整结果")
    _search_database_fallback_many(list(by_id.values()), catalog.databases, cancel, catalog.issues)
    for session in by_id.values():
        _finalize_session_title(session)
        # Keep the search index normalized once, off the UI thread. The original
        # text is no longer needed after the fallback title has been selected.
        session.search_text = session.search_text.casefold()
        if not session.raw_created_at and session.paths:
            session.raw_created_at = ""
        if not session.raw_updated_at:
            session.raw_updated_at = session.raw_created_at
        session.created_at = _local_display(session.raw_created_at)
        session.updated_at = _local_display(session.raw_updated_at)
        attachment_root = Path(root) / "attachments"
        attachment_root_real = os.path.realpath(root)
        own_attachment_folder = attachment_root / session.session_id
        own_attachment_state, own_attachment_error = _attachment_folder_state(own_attachment_folder, attachment_root_real)
        own_attachment_safe = own_attachment_state == "available"
        linked_attachment_states = {
            attachment_id: _attachment_folder_state(attachment_root / attachment_id, attachment_root_real)
            for attachment_id in session.attachment_ids
        }
        session.has_attachment = own_attachment_safe or any(
            state == "available" for state, _error in linked_attachment_states.values()
        )

        def report_attachment_folder_problem(
            folder: Path, state: str, error_type: str, *, own: bool = False
        ) -> None:
            if state in {"available", "missing"}:
                return
            if state == "unsafe":
                message = (
                    "会话附件目录包含符号链接或 junction，已跳过"
                    if own else f"关联附件目录包含符号链接或 junction，已跳过：{folder}"
                )
            elif state == "inaccessible":
                message = f"附件目录检查失败：{folder}（{error_type or '访问错误'}）"
            else:
                message = f"关联附件路径不是目录，已跳过：{folder}"
            if message not in session.issues:
                session.issues.append(message)

        report_attachment_folder_problem(
            own_attachment_folder, own_attachment_state, own_attachment_error, own=True
        )
        for attachment_id, (state, error_type) in linked_attachment_states.items():
            folder = attachment_root / attachment_id
            if state == "missing":
                message = f"未找到关联附件目录：attachments/{attachment_id}"
                if message not in session.issues:
                    session.issues.append(message)
            else:
                report_attachment_folder_problem(folder, state, error_type)
        has_file_source = any(os.path.isfile(path) for path in session.paths)
        if not has_file_source and session.platform == "chatgpt":
            session.status = "仅目录元数据"
            message = "ChatGPT 网页会话目录只包含标题和时间等元数据；未找到本地聊天正文，阅读版导出将不含对话内容"
            if message not in session.issues:
                session.issues.append(message)
        elif session.issues and session.status == "未扫描":
            session.status = "部分可读"
        elif "SQLite 条目回退" in session.source and not has_file_source and session.status == "未扫描":
            session.status = "仅数据库条目"
        elif has_file_source and session.status == "未扫描":
            session.status = "可读取"
        elif not has_file_source:
            session.status = "未找到正文"
            if not session.issues:
                session.issues.append("未找到 rollout 正文；已检查可识别的 SQLite 条目回退来源")
        import_info = imported_sessions.get(session.session_id)
        if import_info:
            import_mode = str(import_info.get("mode") or "")
            if import_mode == "reconstructed":
                session.source = "导入包重建（基于聊天记录.json）"
                session.status = "导入包重建"
                message = "此会话由阅读版 JSON 重建，不是原始 rollout；源文件未提供的内容无法恢复"
                if message not in session.issues:
                    session.issues.append(message)
            elif import_mode == "original":
                session.source = (session.source + " + " if session.source else "") + "跨电脑导入（原始记录副本）"
        session.disk_size_bytes, session.disk_size_status = _measure_session_disk_usage(session, root, cancel)
    catalog.sessions = sorted(by_id.values(), key=lambda s: (_time_sort_key(s.raw_updated_at), s.session_id), reverse=True)
    return catalog


def _event_time(entry: dict[str, Any]) -> tuple[int, int, int]:
    ordinal = entry.get("ordinal")
    try:
        order_value = int(ordinal)
    except (TypeError, ValueError):
        order_value = int(entry.get("_sequence", 0))
    return (int(entry.get("_source_file_index", 0)), order_value, int(entry.get("_sequence", 0)))


def _attachment_exists(reference: Any, session: SessionInfo, catalog: Catalog, related_names: set[str]) -> bool:
    if not isinstance(reference, str) or not reference:
        return False
    if reference.startswith("data:image/") and ";base64," in reference:
        return True
    value = reference
    if value.casefold().startswith("file://"):
        value = value[7:]
        if value.startswith("/") and len(value) > 2 and value[2] == ":":
            value = value[1:]
    if not _has_link_or_junction_component(value) and os.path.isfile(value):
        return True
    basename = os.path.basename(value.replace("\\", "/")).casefold()
    if basename and basename in related_names:
        return True
    if os.path.isdir(os.path.join(catalog.root, "attachments", session.session_id)) and basename:
        return False
    return False


def load_session_events(
    session: SessionInfo,
    catalog: Catalog,
    cancel: threading.Event | None = None,
    include_raw_data: bool = True,
    max_records: int | None = None,
    max_text_chars: int | None = None,
) -> tuple[list[dict[str, Any]], list[str], Counter[str]]:
    if max_records is not None and max_records < 1:
        raise ValueError("max_records 必须为正整数或 None")
    if max_text_chars is not None and max_text_chars < 1:
        raise ValueError("max_text_chars 必须为正整数或 None")
    cancel = cancel or threading.Event()
    issues: list[str] = list(session.issues)
    type_counts: Counter[str] = Counter()
    normalized: list[dict[str, Any]] = []
    canonical_ids: set[str] = set()
    canonical_hashes: set[str] = set()
    item_completed: list[tuple[dict[str, Any], dict[str, Any], int]] = []
    sequence = 0
    source_records_seen = 0
    database_records_seen = 0
    database_rows_added = 0
    preview_truncated = False
    preview_record_truncated = False
    preview_text_truncated = False
    preview_text_chars = 0
    rollout_latest_timestamp = ""
    rollout_has_records = False
    rollout_ordinals_complete = True
    rollout_incomplete = False
    attachment_folder_cache: dict[str, list[str]] = {}
    attachment_listing_budget = (
        _AttachmentListingBudget(PREVIEW_ATTACHMENT_FILE_LIMIT) if max_records is not None else None
    )

    def constrain_preview_text(record: dict[str, Any]) -> None:
        nonlocal preview_text_chars, preview_text_truncated, preview_truncated
        if max_text_chars is None:
            return
        text = record.get("text")
        if not isinstance(text, str):
            return
        remaining = max(0, max_text_chars - preview_text_chars)
        if len(text) > remaining:
            record["text"] = text[:remaining]
            preview_text_chars = max_text_chars
            preview_text_truncated = True
            preview_truncated = True
        else:
            preview_text_chars += len(text)

    unique_paths: dict[str, str] = {}
    for path in session.paths:
        if os.path.isfile(path) and _is_within_root(path, catalog.root):
            unique_paths.setdefault(_canonical_path(path), path)
        elif os.path.isfile(path):
            issue = f"忽略所选 Codex 目录之外的会话来源：{path}"
            if issue not in issues:
                issues.append(issue)
    source_paths = sorted(unique_paths.values(), key=lambda value: value.casefold())
    for source_file_index, path in enumerate(source_paths):
        if cancel.is_set():
            break
        try:
            for line_no, line in _iter_jsonl(path):
                if cancel.is_set():
                    break
                if preview_text_truncated:
                    preview_truncated = True
                    break
                if max_records is not None and source_records_seen + database_records_seen >= max_records:
                    preview_truncated = True
                    preview_record_truncated = True
                    break
                source_records_seen += 1
                try:
                    entry = json.loads(line)
                except Exception as exc:
                    rollout_incomplete = True
                    issues.append(f"{os.path.basename(path)} 第 {line_no} 行 JSON 损坏：{type(exc).__name__}")
                    continue
                if not isinstance(entry, dict):
                    continue
                rollout_has_records = True
                ordinal = entry.get("ordinal")
                try:
                    int(ordinal)
                except (TypeError, ValueError):
                    rollout_ordinals_complete = False
                timestamp = _epoch_string(entry.get("timestamp"))
                if timestamp and _time_sort_key(timestamp) > _time_sort_key(rollout_latest_timestamp):
                    rollout_latest_timestamp = timestamp
                record_type = str(entry.get("type", "unknown"))
                type_counts[record_type] += 1
                payload = entry.get("payload")
                if record_type == "session_meta":
                    continue
                sequence += 1
                event_base = {"timestamp": entry.get("timestamp", ""), "raw_timestamp": entry.get("timestamp", ""), "ordinal": entry.get("ordinal"), "_sequence": sequence, "_source_file_index": source_file_index, "source_type": record_type}
                if record_type == "response_item" and isinstance(payload, dict):
                    if not isinstance(payload.get("type"), str):
                        issue = f"{os.path.basename(path)} 第 {line_no} 行：response_item.type 不是文本，按未知条目处理"
                        if issue not in issues:
                            issues.append(issue)
                    item_id = str(payload.get("id") or "")
                    digest = "" if item_id else _record_digest(payload)
                    if (item_id and item_id in canonical_ids) or (not item_id and digest in canonical_hashes):
                        continue
                    if item_id:
                        canonical_ids.add(item_id)
                    else:
                        canonical_hashes.add(digest)
                    attachments: list[dict[str, Any]] = []
                    record = _normalized_response(payload, entry, attachments)
                    if record:
                        if not include_raw_data:
                            record.pop("data", None)
                        if _is_user_message_item(payload):
                            _append_codex_attachment_refs(
                                payload.get("content", payload.get("text", "")), catalog.root, attachments, session,
                                attachment_folder_cache, cancel, issues,
                                None,
                                attachment_listing_budget,
                            )
                        record["attachments"] = attachments
                        record["_sequence"] = sequence
                        constrain_preview_text(record)
                        normalized.append(record)
                elif record_type == "event_msg" and isinstance(payload, dict):
                    event_kind = _event_label(payload)
                    if event_kind == "item_completed" and isinstance(payload.get("item"), dict):
                        if not isinstance(payload["item"].get("type"), str):
                            issue = f"{os.path.basename(path)} 第 {line_no} 行：item_completed.item.type 不是文本，按未知条目处理"
                            if issue not in issues:
                                issues.append(issue)
                        item_id = str(payload["item"].get("id") or "")
                        # response_item is canonical when present. Do not keep a
                        # second copy of a possibly large payload until EOF.
                        if item_id and item_id in canonical_ids:
                            continue
                        item_completed.append((payload, event_base, sequence))
                    elif event_kind not in {"token_count", "turn_context", "session_meta"}:
                        event = {**event_base, "kind": "status", "role": "event", "name": event_kind, "text": _content_text(payload)}
                        if include_raw_data:
                            event["data"] = payload
                        constrain_preview_text(event)
                        normalized.append(event)
                else:
                    if record_type not in {"turn_context", "token_usage_record", "world_state"}:
                        kind = "status" if record_type in STATUS_RECORD_TYPES else "unknown"
                        event = {**event_base, "kind": kind, "role": "event", "name": record_type, "text": _content_text(payload)}
                        if include_raw_data:
                            event["data"] = payload
                        constrain_preview_text(event)
                        normalized.append(event)
        except Exception as exc:
            rollout_incomplete = True
            issues.append(f"{os.path.basename(path)} 读取失败：{type(exc).__name__}: {exc}")
        if preview_truncated:
            break

    for payload, event_base, order in item_completed:
        if cancel.is_set():
            break
        item = payload.get("item")
        item_id = str(item.get("id") or "")
        if item_id and item_id in canonical_ids:
            continue
        digest = "" if item_id else _record_digest(item)
        if not item_id and digest in canonical_hashes:
            continue
        if item_id:
            canonical_ids.add(item_id)
        else:
            canonical_hashes.add(digest)
        attachments: list[dict[str, Any]] = []
        record = _normalized_response(item, event_base, attachments)
        if record:
            if not include_raw_data:
                record.pop("data", None)
            if _is_user_message_item(item):
                _append_codex_attachment_refs(
                    item.get("content", item.get("text", "")), catalog.root, attachments, session,
                    attachment_folder_cache, cancel, issues,
                    None,
                    attachment_listing_budget,
                )
            record["_sequence"] = order
            record["attachments"] = attachments
            constrain_preview_text(record)
            normalized.append(record)
            if preview_text_truncated:
                break

    metadata_ahead = (
        bool(source_paths)
        and "SQLite 元数据" in session.source
        and bool(session.raw_updated_at)
        and bool(rollout_latest_timestamp)
        and _time_sort_key(session.raw_updated_at) - _time_sort_key(rollout_latest_timestamp) > 1.0
    )
    prior_incomplete_hint = any(
        any(token in issue for token in ("JSON 损坏", "读取失败", "扫描已取消", "不完整"))
        for issue in session.issues
    )
    should_supplement_database = not source_paths or rollout_incomplete or prior_incomplete_hint or metadata_ahead
    skipped_unindexed_tables: list[str] = []
    if source_paths and not should_supplement_database:
        for database in catalog.databases:
            for schema in database.tables:
                lowered = {column.casefold(): column for column in schema.columns}
                if (
                    schema.item_json
                    and (lowered.get("thread_id") or lowered.get("session_id"))
                    and not schema.thread_id_indexed
                ):
                    skipped_unindexed_tables.append(f"{os.path.basename(database.source_path)} / {schema.name}")
    if should_supplement_database:
        for database in catalog.databases:
            if preview_truncated:
                break
            if not database.snapshot_path:
                continue
            for schema in database.tables:
                if not schema.item_json:
                    continue
                lowered = {c.casefold(): c for c in schema.columns}
                thread_col = lowered.get("thread_id") or lowered.get("session_id")
                if not thread_col:
                    continue
                if cancel.is_set() or preview_truncated:
                    break
                connection: sqlite3.Connection | None = None
                try:
                    connection = _connect_readonly(database.snapshot_path)
                    _enable_sqlite_cancellation(connection, cancel)
                    order_col = lowered.get("rollout_ordinal") or lowered.get("ordinal") or lowered.get("created_at_ms")
                    order_sql = f" ORDER BY {_quote_identifier(order_col)}" if order_col else ""
                    selected_columns = list(dict.fromkeys(
                        column for column in (
                            thread_col,
                            schema.item_json,
                            lowered.get("item_id", ""),
                            lowered.get("created_at_ms", ""),
                            lowered.get("completed_at_ms", ""),
                            lowered.get("rollout_ordinal", ""),
                            lowered.get("ordinal", ""),
                        ) if column
                    ))
                    projection = ", ".join(_quote_identifier(column) for column in selected_columns)
                    order_col = lowered.get("rollout_ordinal") or lowered.get("ordinal")
                    order_sql = f" ORDER BY {_quote_identifier(order_col)}" if order_col else ""
                    query = (
                        f"SELECT {projection} FROM {_quote_identifier(schema.name)} "
                        f"WHERE {_quote_identifier(thread_col)} = ?{order_sql}"
                    )
                    plan = connection.execute(
                        "EXPLAIN QUERY PLAN " + query, (session.session_id,)
                    ).fetchall()
                    indexed = any(
                        "USING" in str(row[-1]).upper() and "INDEX" in str(row[-1]).upper()
                        for row in plan
                    )
                    if source_paths and not indexed and not (rollout_incomplete or prior_incomplete_hint or metadata_ahead):
                        skipped_unindexed_tables.append(f"{os.path.basename(database.source_path)} / {schema.name}")
                        continue
                    cursor = connection.execute(
                        query,
                        (session.session_id,),
                    )
                    columns = selected_columns
                    table_had_rows = False
                    for row in cursor:
                        if cancel.is_set():
                            break
                        if max_text_chars is not None and preview_text_truncated:
                            preview_truncated = True
                            break
                        if max_records is not None and source_records_seen + database_records_seen >= max_records:
                            preview_truncated = True
                            preview_record_truncated = True
                            break
                        database_records_seen += 1
                        table_had_rows = True
                        data = dict(zip(columns, row))
                        try:
                            item = _decode_database_json(data.get(schema.item_json) or "{}")
                        except Exception as exc:
                            issues.append(f"数据库条目 JSON 损坏：{type(exc).__name__}")
                            continue
                        if not isinstance(item, dict):
                            continue
                        if not isinstance(item.get("type"), str):
                            issue = f"{database.source_path} / {schema.name}：条目 type 字段不是文本，按未知条目处理"
                            if issue not in issues:
                                issues.append(issue)
                        item_id = str(item.get("id") or data.get(lowered.get("item_id", "")) or "")
                        digest = "" if item_id else _record_digest(item)
                        if (item_id and item_id in canonical_ids) or (not item_id and digest in canonical_hashes):
                            continue
                        if item_id:
                            canonical_ids.add(item_id)
                        else:
                            canonical_hashes.add(digest)
                        database_rows_added += 1
                        timestamp = _epoch_string(data.get(lowered.get("created_at_ms", "")) or data.get(lowered.get("completed_at_ms", "")))
                        envelope = {"timestamp": timestamp, "ordinal": data.get(order_col) if order_col else None}
                        attachments: list[dict[str, Any]] = []
                        records = _normalized_database_items(item, envelope, attachments)
                        if not include_raw_data:
                            for record in records:
                                record.pop("data", None)
                        if _is_user_message_item(item):
                            _append_codex_attachment_refs(
                                item.get("content", item.get("text", "")), catalog.root, attachments, session,
                                attachment_folder_cache, cancel, issues,
                                None,
                                attachment_listing_budget,
                            )
                        for record in records:
                            record["attachments"] = attachments
                            has_merge_order = bool(
                                order_col and rollout_has_records and rollout_ordinals_complete and len(source_paths) == 1
                            )
                            record["_source_file_index"] = 0 if has_merge_order else len(source_paths)
                            record["_sequence"] = data.get(order_col) if order_col else sequence + 1
                            constrain_preview_text(record)
                            normalized.append(record)
                            if preview_text_truncated:
                                break
                        if timestamp and _time_sort_key(timestamp) >= _time_sort_key(session.raw_updated_at):
                            session.raw_updated_at = timestamp
                        type_counts[str(item.get("type", "unknown"))] += 1
                    if table_had_rows:
                        source_path_label = f"{database.source_path} :: {schema.name}"
                        if source_path_label not in session.paths:
                            session.paths.append(source_path_label)
                        source_label = f"SQLite 条目补充（{os.path.basename(database.source_path)} / {schema.name}）"
                        if source_label not in session.source:
                            session.source = source_label if not session.source else session.source + " + " + source_label
                        if source_paths and not (
                            order_col and rollout_has_records and rollout_ordinals_complete and len(source_paths) == 1
                        ):
                            issue = f"{os.path.basename(database.source_path)} / {schema.name}：数据库条目缺少可与 rollout 对齐的序号，已附加在 rollout 后；跨来源精确顺序未保存。"
                            if issue not in issues:
                                issues.append(issue)
                    if preview_truncated:
                        break
                except Exception as exc:
                    if not cancel.is_set():
                        issues.append(f"{database.source_path} / {schema.name}：条目读取失败：{type(exc).__name__}: {exc}")
                finally:
                    if connection is not None:
                        connection.close()
            if preview_truncated:
                break
    if skipped_unindexed_tables:
        for table in skipped_unindexed_tables:
            issue = f"{table}：会话 ID 列未建立索引，为避免每条会话重复扫描整表，本次未用其补充 rollout；无法据此核对该表是否还有额外条目。"
            if issue not in issues:
                issues.append(issue)
    if metadata_ahead and database_records_seen == 0:
        issue = "SQLite 元数据更新时间晚于 rollout 最后记录时间；已检查可用 SQLite 条目来源，但没有找到可补充条目，正文完整性无法确认。"
        if issue not in issues:
            issues.append(issue)
    if database_rows_added:
        issue = f"SQLite 条目补充：新增 {database_rows_added:,} 条未在 rollout 中找到的数据库记录；重复条目已跳过。"
        if issue not in issues:
            issues.append(issue)
    if preview_record_truncated:
        issue = f"预览已达到 {max_records:,} 条源记录上限；后续内容未读取，完整导出不受此限制。"
        if issue not in issues:
            issues.append(issue)
    if preview_text_truncated:
        issue = f"预览文本已达到 {max_text_chars:,} 字符上限；后续文本未读取，完整导出不受此限制。"
        if issue not in issues:
            issues.append(issue)
    related_folder = os.path.join(catalog.root, "attachments", session.session_id)
    related_key = os.path.normcase(os.path.abspath(related_folder))
    related_files = attachment_folder_cache.get(related_key)
    if related_files is None:
        attachment_root = Path(catalog.root) / "attachments"
        if not _has_link_or_junction_component_reported(
            attachment_root, catalog.root, issues, "附件根目录"
        ) and _is_within_root(str(attachment_root), catalog.root):
            related_files = _list_attachment_folder_files(
                Path(related_folder),
                os.path.normcase(str(attachment_root.resolve())),
                cancel,
                issues,
                None,
                attachment_listing_budget,
            )
        else:
            related_files = []
            issues.append("附件根目录包含链接或无法检查的路径组件，关联附件未读取")
        if not cancel.is_set():
            attachment_folder_cache[related_key] = related_files
    related_names = {Path(path).name.casefold() for path in related_files}
    for event in normalized:
        if cancel.is_set():
            break
        for attachment in event.get("attachments", []):
            if cancel.is_set():
                break
            reference = attachment.get("ref")
            exists = _attachment_exists(reference, session, catalog, related_names)
            if exists:
                attachment["status"] = "本地文件存在"
            elif attachment_listing_budget is not None and attachment_listing_budget.limit_reported:
                attachment["status"] = "预览上限，未检查"
            else:
                attachment["status"] = "未找到或仅有外部引用"
            if not exists:
                issue_kind = (
                    "附件预览达到检查上限，存在性未检查"
                    if attachment.get("status") == "预览上限，未检查"
                    else "附件未找到或不可离线读取"
                )
                issue = f"{issue_kind}：{str(reference)[:180]}"
                if issue not in issues:
                    issues.append(issue)
    if cancel.is_set():
        message = "会话读取已取消；返回的事件列表不完整。"
        if message not in issues:
            issues.append(message)
    else:
        # A partial event list is discarded by exporters and visibly marked
        # in previews, so avoid an expensive O(n log n) sort after cancellation.
        normalized.sort(key=_event_time)
    return normalized, issues, type_counts
