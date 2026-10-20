"""安全导入本工具生成的会话导出包。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Callable


MAX_IMPORT_FILES = 100_000
MAX_IMPORT_BYTES = 20 * 1024 * 1024 * 1024
COPY_CHUNK_SIZE = 1024 * 1024


def _safe_name(value: str, limit: int = 70) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value))
    value = re.sub(r"\s+", " ", value).strip(" ._")[:limit].rstrip(" ._")
    return value or "未命名会话"


def _safe_session_id(value: Any) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"[0-9a-fA-F-]{8,128}", text):
        return text
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:24]
    return f"import-{digest}"


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _is_reparse_or_symlink(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse)


def _safe_tree_path(path: Path, root: Path) -> bool:
    """Require every existing component to be a regular directory/file, not a link."""
    if not _inside(path, root):
        return False
    try:
        relative = path.absolute().relative_to(root.absolute())
    except ValueError:
        return False
    cursor = root
    if _is_reparse_or_symlink(cursor):
        return False
    for part in relative.parts:
        cursor = cursor / part
        if _is_reparse_or_symlink(cursor):
            return False
    return True


def _safe_relative_path(value: Any) -> PurePosixPath | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.replace("\\", "/")
    relative = PurePosixPath(normalized)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        return None
    if relative.parts and re.match(r"^[a-zA-Z]:", relative.parts[0]):
        return None
    return relative


def _copy_file(source: Path, destination: Path, cancel, progress: Callable[[int], None] | None = None) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    copied = 0
    with source.open("rb") as src, destination.open("wb") as dst:
        while True:
            if cancel.is_set():
                raise InterruptedError("导入已取消")
            block = src.read(COPY_CHUNK_SIZE)
            if not block:
                break
            dst.write(block)
            copied += len(block)
            if progress:
                progress(len(block))
    return copied


def _extract_zip(source: Path, cancel, progress: Callable[[str, int], None] | None = None) -> Path:
    temporary = Path(tempfile.mkdtemp(prefix="codex-history-import-"))
    try:
        with zipfile.ZipFile(source) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_IMPORT_FILES:
                raise ValueError(f"导入包文件数超过上限（{MAX_IMPORT_FILES:,}）")
            total = sum(max(0, int(info.file_size)) for info in infos)
            if total > MAX_IMPORT_BYTES:
                raise ValueError("导入包展开后超过 20 GiB 上限")
            extracted = 0
            seen_names: set[str] = set()
            for info in infos:
                if cancel.is_set():
                    raise InterruptedError("导入已取消")
                name = info.filename.replace("\\", "/")
                safe_relative = _safe_relative_path(name.rstrip("/")) if name.rstrip("/") else None
                if safe_relative is None:
                    raise ValueError(f"导入包包含无效路径：{info.filename}")
                canonical_name = safe_relative.as_posix().casefold()
                if canonical_name in seen_names:
                    raise ValueError(f"导入包包含重复路径：{info.filename}")
                seen_names.add(canonical_name)
                target = temporary / name
                if not _inside(target, temporary):
                    raise ValueError(f"导入包包含越界路径：{info.filename}")
                # Reject Unix symlink entries. Windows reparse points are not
                # created by ZipFile extraction, but links are never trusted.
                mode = (info.external_attr >> 16) & 0xFFFF
                if (mode & 0o170000) == 0o120000:
                    raise ValueError(f"导入包包含符号链接：{info.filename}")
                if name.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info, "r") as src, target.open("wb") as dst:
                    while True:
                        if cancel.is_set():
                            raise InterruptedError("导入已取消")
                        block = src.read(COPY_CHUNK_SIZE)
                        if not block:
                            break
                        extracted += len(block)
                        if extracted > MAX_IMPORT_BYTES:
                            raise ValueError("导入包实际展开内容超过 20 GiB 上限")
                        dst.write(block)
                        if progress and total:
                            progress(f"展开导入包：{name}", int(extracted * 100 / total))
        return temporary
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _find_manifests(root: Path) -> list[Path]:
    manifests: list[Path] = []
    root = root.resolve()
    file_count = 0
    total_bytes = 0
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(directory)
        dirnames[:] = [
            name for name in dirnames
            if _safe_tree_path(current / name, root)
        ]
        for name in filenames:
            path = current / name
            file_count += 1
            if file_count > MAX_IMPORT_FILES:
                raise ValueError(f"导入目录文件数超过上限（{MAX_IMPORT_FILES:,}）")
            if not _safe_tree_path(path, root):
                continue
            try:
                total_bytes += path.stat().st_size
            except OSError:
                raise ValueError(f"无法读取导入文件属性：{path.name}")
            if total_bytes > MAX_IMPORT_BYTES:
                raise ValueError("导入目录超过 20 GiB 上限")
            if name.casefold() == "manifest.json" and path.is_file():
                manifests.append(path)
    return sorted(manifests, key=lambda p: p.as_posix().casefold())


def _hash_file(path: Path, cancel) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            if cancel.is_set():
                raise InterruptedError("导入已取消")
            block = stream.read(COPY_CHUNK_SIZE)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _validate_manifest(manifest: dict[str, Any], manifest_path: Path, package_root: Path, cancel) -> int:
    if manifest.get("complete") is False:
        raise ValueError(f"导出未完成，不能导入：{manifest_path.parent.name}")
    files = manifest.get("files")
    if files is None:
        return 0  # Older manifests did not include a file inventory.
    if not isinstance(files, list):
        raise ValueError(f"文件清单格式错误：{manifest_path.parent.name}")
    checked = 0
    package_dir = manifest_path.parent
    for item in files:
        if cancel.is_set():
            raise InterruptedError("导入已取消")
        if not isinstance(item, dict):
            raise ValueError(f"文件清单条目格式错误：{manifest_path.parent.name}")
        relative = _safe_relative_path(item.get("path"))
        if relative is None:
            raise ValueError(f"清单包含不安全文件路径：{item.get('path')}")
        path = package_dir.joinpath(*relative.parts)
        if not _safe_tree_path(path, package_root) or not path.is_file():
            raise ValueError(f"导出包缺少文件或路径不安全：{relative.as_posix()}")
        expected = item.get("sha256")
        if expected:
            if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
                raise ValueError(f"文件清单中的 SHA-256 格式错误：{relative.as_posix()}")
            actual = _hash_file(path, cancel)
            if actual.casefold() != expected.casefold():
                raise ValueError(f"文件完整性校验失败：{relative.as_posix()}")
            checked += 1
    return checked


def _event_payload(event: dict[str, Any], ordinal: int, session_id: str = "") -> dict[str, Any]:
    """Convert a normalized exported event into a readable synthetic rollout item."""
    kind = str(event.get("kind") or "unknown")
    event_id = str(event.get("id") or f"imported-{ordinal}")
    text = event.get("text", "")
    attachments = event.get("attachments")
    if kind == "message":
        content: Any = text if isinstance(text, str) else str(text)
        if isinstance(attachments, list) and attachments:
            content = [{"type": "input_text", "text": content}]
            for item in attachments:
                if isinstance(item, dict):
                    reference = item.get("path") or item.get("ref")
                    relative = _safe_relative_path(reference)
                    if relative is not None and item.get("status") != "未找到":
                        content.append({"type": "input_file", "path": f"attachments/{session_id}/{relative.name}"})
                    elif item.get("status") == "未找到":
                        content.append({"type": "input_text", "text": "[附件未找到]"})
        payload = {"type": "message", "id": event_id, "role": event.get("role") or "assistant", "content": content}
        if event.get("phase"):
            payload["phase"] = event["phase"]
        return payload
    if kind == "tool_call":
        return {"type": "custom_tool_call", "id": event_id, "call_id": event.get("call_id"), "name": event.get("name"), "input": text}
    if kind == "tool_result":
        return {"type": "custom_tool_call_output", "id": event_id, "call_id": event.get("call_id"), "output": text}
    return {"type": "event_msg", "id": event_id, "payload": {"type": kind, "text": text}}


def _write_synthetic_rollout(manifest: dict[str, Any], json_path: Path, destination: Path, session_id: str) -> int:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    events = data.get("events") if isinstance(data, dict) else None
    if not isinstance(events, list):
        raise ValueError("聊天记录.json 缺少 events 数组")
    session = data.get("session") if isinstance(data.get("session"), dict) else {}
    created = session.get("raw_created_at") or session.get("created_at") or ""
    cwd = session.get("cwd") or ""
    lines = [{"type": "session_meta", "payload": {"session_id": session_id, "timestamp": created, "cwd": cwd}, "timestamp": created, "ordinal": 0}]
    for ordinal, event in enumerate(events, 1):
        if not isinstance(event, dict):
            continue
        timestamp = event.get("raw_timestamp") or event.get("timestamp") or created
        lines.append({"type": "response_item", "payload": _event_payload(event, ordinal, session_id), "timestamp": timestamp, "ordinal": ordinal})
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="") as stream:
        for line in lines:
            stream.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n")
    return len(lines) - 1


def _copy_exported_attachments(package_dir: Path, root: Path, session_id: str, cancel) -> int:
    source = package_dir / "attachments"
    if not source.is_dir():
        return 0
    destination = root / "attachments" / session_id
    count = 0
    for directory, dirnames, filenames in os.walk(source, followlinks=False):
        current = Path(directory)
        dirnames[:] = [name for name in dirnames if _safe_tree_path(current / name, source)]
        for name in filenames:
            path = current / name
            if cancel.is_set():
                raise InterruptedError("导入已取消")
            if not _safe_tree_path(path, source) or not path.is_file():
                continue
            relative = path.relative_to(source)
            _copy_file(path, destination / relative, cancel)
            count += 1
    return count


def import_export_package(
    source: str,
    destination_root: str,
    cancel,
    progress: Callable[[str, int], None] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Import one ZIP or extracted export tree into a synthetic Codex root.

    The returned root is safe to scan with ``scan_codex_home``. Original raw
    JSONL is copied when present; otherwise the readable JSON export is
    converted into a synthetic rollout and clearly marked as reconstructed.
    """
    source_path = Path(os.path.abspath(os.path.expandvars(source)))
    if not source_path.is_file() and not source_path.is_dir():
        raise FileNotFoundError("导入源不存在")
    temporary: Path | None = None
    staging_root: Path | None = None
    package_root = source_path
    try:
        if source_path.is_file():
            if source_path.suffix.casefold() != ".zip":
                raise ValueError("请选择本工具导出的 ZIP 文件，或已解压的导出目录")
            package_root = _extract_zip(source_path, cancel, progress)
            temporary = package_root
        package_root = package_root.resolve(strict=True)
        manifests = _find_manifests(package_root)
        if not manifests:
            raise ValueError("未找到 manifest.json；这不是本工具可识别的导出包")
        destination = Path(os.path.abspath(os.path.expandvars(destination_root)))
        destination.mkdir(parents=True, exist_ok=True)
        import_root = destination / f"导入_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        staging_root = destination / f".导入暂存_{uuid.uuid4().hex}"
        staging_root.mkdir(parents=True, exist_ok=False)
        import_root_staging = staging_root
        (import_root_staging / "sessions" / "imported").mkdir(parents=True, exist_ok=False)
        (import_root_staging / "attachments").mkdir(parents=True, exist_ok=True)
        imported = 0
        reconstructed = 0
        attachment_count = 0
        integrity_checked = 0
        imported_modes: dict[str, dict[str, str]] = {}
        index_rows: list[dict[str, Any]] = []
        for index, manifest_path in enumerate(manifests, 1):
            if cancel.is_set():
                raise InterruptedError("导入已取消")
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise ValueError(f"manifest.json 无法读取：{type(exc).__name__}") from exc
            if not isinstance(manifest, dict):
                raise ValueError("manifest.json 顶层不是对象")
            integrity_checked += _validate_manifest(manifest, manifest_path, package_root, cancel)
            session_id = _safe_session_id(manifest.get("session_id"))
            index_rows.append({
                "id": session_id,
                "thread_name": str(manifest.get("title") or "导入会话"),
                "cwd": str((manifest.get("session") or {}).get("cwd") or "") if isinstance(manifest.get("session"), dict) else "",
            })
            package_dir = manifest_path.parent
            session_dir = import_root_staging / "sessions" / "imported" / session_id
            session_dir.mkdir(parents=True, exist_ok=True)
            raw_dir = package_dir / "原始文件"
            raw_files = [
                path for path in sorted(raw_dir.iterdir(), key=lambda p: p.name.casefold())
                if path.is_file() and _safe_tree_path(path, package_root)
            ] if raw_dir.is_dir() and _safe_tree_path(raw_dir, package_root) else []
            copied_raw = 0
            for raw_index, raw in enumerate(raw_files, 1):
                lowered_name = raw.name.casefold()
                if lowered_name.startswith("rollout-") and lowered_name.endswith((".jsonl", ".jsonl.zst")):
                    suffix = ".jsonl.zst" if lowered_name.endswith(".jsonl.zst") else ".jsonl"
                    target = session_dir / f"rollout-imported-{session_id}-part-{raw_index:04d}{suffix}"
                    _copy_file(raw, target, cancel)
                    copied_raw += 1
            attachment_count += _copy_exported_attachments(package_dir, import_root_staging, session_id, cancel)
            if not copied_raw:
                json_path = package_dir / "聊天记录.json"
                if not _safe_tree_path(json_path, package_root) or not json_path.is_file():
                    raise ValueError(f"会话 {session_id} 缺少原始 JSONL 和聊天记录.json")
                _write_synthetic_rollout(manifest, json_path, session_dir / f"rollout-imported-{session_id}.jsonl", session_id)
                reconstructed += 1
                imported_modes[session_id] = {"mode": "reconstructed", "title": str(manifest.get("title") or "导入会话")}
            else:
                imported_modes[session_id] = {"mode": "original", "title": str(manifest.get("title") or "导入会话")}
            imported += 1
            if progress:
                progress(f"导入会话 {index}/{len(manifests)}：{manifest.get('title') or session_id}", int(index * 100 / len(manifests)))
        with (import_root_staging / "session_index.jsonl").open("w", encoding="utf-8", newline="") as index_file:
            for row in index_rows:
                index_file.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        with (import_root_staging / "codex_history_import.json").open("w", encoding="utf-8", newline="") as metadata_file:
            json.dump({"format": "codex-history-import-v1", "sessions": imported_modes}, metadata_file, ensure_ascii=False, indent=2)
            metadata_file.write("\n")
        if cancel.is_set():
            raise InterruptedError("导入已取消")
        os.replace(import_root_staging, import_root)
        staging_root = None
        report = {"imported_sessions": imported, "reconstructed_sessions": reconstructed, "attachments": attachment_count, "integrity_files_checked": integrity_checked, "root": str(import_root)}
        return str(import_root), report
    finally:
        if staging_root is not None:
            shutil.rmtree(staging_root, ignore_errors=True)
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)
