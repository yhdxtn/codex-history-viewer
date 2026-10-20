from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import tempfile
import zipfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote, unquote, urlparse

from .reader import (
    APP_VERSION,
    Catalog,
    SessionInfo,
    _filename_id,
    _has_link_or_junction_component,
    _is_link_or_junction,
    _is_within_root,
    _prepare_batch_snapshot_indexes,
    load_session_events,
)


_SECRET_VALUE = re.compile(
    r"(?ix)((?:['\"]?(?:(?:[a-z0-9]+[_-])*(?:password|passwd|token|secret|authorization|credential|api[_-]?key|access[_-]?key|private[_-]?key)(?:[_-][a-z0-9]+)*|密码|口令|令牌|密钥)['\"]?\s*[:=]\s*))"
    r"(?:(['\"])((?:\\.|(?!\2).)*?)\2|((?:(?:bearer|basic)\s+)?[^\s'\"&,;}\]]+))"
)
_SECRET_QUOTED_OPEN = re.compile(
    r'''(?ix)(?:['"]?(?:(?:[a-z0-9]+[_-])*(?:password|passwd|token|secret|authorization|credential|api[_-]?key|access[_-]?key|private[_-]?key)(?:[_-][a-z0-9]+)*|密码|口令|令牌|密钥)['"]?\s*[:=]\s*)(?P<quote>['"])'''
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)
_PRIVATE_KEY_BEGIN = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_PRIVATE_KEY_END = re.compile(r"-----END [A-Z ]*PRIVATE KEY-----")
_LOCAL_PATH = re.compile(r"(?<![\w])(?:[A-Za-z]:[\\/]|\\\\[^\\/\s]+[\\/][^\\/\s]+[\\/]|/(?:Users|home|private|tmp|var|mnt|opt|root)/)[^\r\n\t\"'<>|]+")
_ATTACHMENT_MARKER = re.compile(r"\[\[ATTACHMENT:(.*?)\]\]", re.S)
_SECRET_KEY_NAME = re.compile(r"(?i)(password|passwd|token|api[_-]?key|secret|authorization|credential|access[_-]?key|private[_-]?key|密码|口令|令牌|密钥)")
_SENSITIVE_ATTACHMENT_NAMES = {
    "auth.json", "credentials.json", "credentials", "secrets.json", "id_rsa", "id_ed25519",
    "known_hosts", "config", ".npmrc", ".pypirc", ".netrc", ".git-credentials",
    ".dockercfg", "dockerconfig.json", "application_default_credentials.json",
}
_SENSITIVE_ATTACHMENT_PARTS = {".ssh", "secrets", "credentials"}
_SENSITIVE_ATTACHMENT_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".kdbx"}
_REDACTABLE_SUFFIXES = {
    ".txt", ".md", ".json", ".jsonl", ".csv", ".log", ".xml", ".yaml", ".yml",
    ".ini", ".toml", ".conf", ".cfg", ".properties", ".sql", ".sh", ".ps1", ".bat", ".cmd",
    ".py", ".js", ".ts", ".html", ".htm",
}
_COPY_CHUNK_SIZE = 1024 * 1024
_INLINE_IMAGE_CHUNK_SIZE = 256 * 1024
_FORMAT_CHUNK_SIZE = 64 * 1024
_MAX_REDACTION_LINE_CHARS = 1024 * 1024


def _check_cancel(cancel, message: str = "导出已取消") -> None:
    if cancel is not None and cancel.is_set():
        raise InterruptedError(message)


def _raise_walk_error(error: OSError) -> None:
    raise error


def safe_name(value: str, limit: int = 70) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value))
    value = re.sub(r"\s+", " ", value).strip(" ._")
    value = value[:limit].rstrip(" ._")
    if not value:
        value = "未命名会话"
    if value.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        value = "_" + value
    return value


def unique_directory(parent: str, base: str) -> str:
    suffix = 1
    while True:
        name = base if suffix == 1 else f"{base}_{suffix}"
        candidate = os.path.join(parent, name)
        try:
            os.mkdir(candidate)
            return candidate
        except FileExistsError:
            # Directory allocation must be atomic: another export may claim a
            # candidate between checking it and creating it.
            suffix += 1


def _validate_output_root(catalog: Catalog, output_root: str) -> str:
    root = os.path.normcase(os.path.realpath(catalog.root))
    target = os.path.normcase(os.path.realpath(output_root))
    if _is_within_root(target, root):
        raise ValueError("导出位置不能位于 Codex 数据目录内；请选择文档或其他独立文件夹。")
    return os.path.abspath(output_root)


def _is_raw_rollout_source(path: str, root: str, session_id: str) -> bool:
    """Restrict raw copies to regular rollout files for this session under known data folders."""
    source = Path(os.path.abspath(path))
    base = Path(os.path.abspath(root))
    try:
        relative = source.relative_to(base)
    except ValueError:
        return False
    if len(relative.parts) < 2 or relative.parts[0].casefold() not in {"sessions", "archived_sessions"}:
        return False
    name = source.name.casefold()
    if not name.startswith("rollout-") or not (name.endswith(".jsonl") or name.endswith(".jsonl.zst")):
        return False
    current = source
    while current != base:
        if _is_link_or_junction(current):
            return False
        current = current.parent
    if not source.is_file() or not _is_within_root(str(source), root):
        return False
    return _filename_id(str(source)).casefold() == str(session_id).casefold()


def _sensitive_attachment(path: Path) -> bool:
    parts = {part.casefold() for part in path.parts}
    name = path.name.casefold()
    return bool(
        parts.intersection(_SENSITIVE_ATTACHMENT_PARTS)
        or name in _SENSITIVE_ATTACHMENT_NAMES
        or (".docker" in parts and name in {"config", "config.json"})
        or name.startswith(".env")
        or path.suffix.casefold() in _SENSITIVE_ATTACHMENT_SUFFIXES
    )


def _has_symlink_component(path: Path) -> bool:
    current = Path(os.path.abspath(path))
    while True:
        try:
            if _is_link_or_junction(current):
                return True
            if os.path.normcase(os.path.realpath(current)) != os.path.normcase(os.path.abspath(current)):
                return True
        except (OSError, RuntimeError):
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def _redact_attachment_stream(source: Path, destination: Path, cancel, redaction_counter: list[int] | None = None) -> None:
    """Redact a text attachment line by line so large files do not need to fit in memory."""
    if cancel is None:
        import threading

        cancel = threading.Event()
    with source.open("rb") as probe:
        prefix = probe.read(4)
    encoding = "utf-16" if prefix.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    count = 0
    in_private_key = False
    pending = ""
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".partial", dir=str(destination.parent)
    )

    def redact_line(line: str, output) -> None:
        nonlocal count, in_private_key
        if _has_unclosed_secret_quote(line, cancel):
            raise ValueError("附件包含跨行或未闭合的引号敏感值，已跳过")
        remaining = line
        while remaining:
            if cancel.is_set():
                raise InterruptedError("附件导出已取消")
            if in_private_key:
                end = _PRIVATE_KEY_END.search(remaining)
                if not end:
                    return
                remaining = remaining[end.end():]
                in_private_key = False
                continue
            begin = _PRIVATE_KEY_BEGIN.search(remaining)
            if begin:
                prefix_text, n = _redact_ordinary_text(remaining[:begin.start()])
                output.write(prefix_text + "[已脱敏: 私钥]")
                count += n + 1
                end = _PRIVATE_KEY_END.search(remaining, begin.end())
                if end:
                    remaining = remaining[end.end():]
                    continue
                in_private_key = True
                return
            cleaned, n = _redact_ordinary_text(remaining)
            output.write(cleaned)
            count += n
            return

    try:
        # newline="" preserves the original line endings while bounding memory to one line.
        with source.open("r", encoding=encoding, errors="strict", newline="") as reader, os.fdopen(
            fd, "w", encoding="utf-8", newline=""
        ) as writer:
            fd = -1
            while True:
                if cancel.is_set():
                    raise InterruptedError("附件导出已取消")
                chunk = reader.read(64 * 1024)
                if not chunk:
                    break
                pending += chunk
                while True:
                    newline_at = pending.find("\n")
                    if newline_at < 0:
                        if len(pending) > _MAX_REDACTION_LINE_CHARS:
                            raise ValueError("单行超过脱敏处理上限")
                        break
                    if newline_at + 1 > _MAX_REDACTION_LINE_CHARS:
                        raise ValueError("单行超过脱敏处理上限")
                    line = pending[:newline_at + 1]
                    pending = pending[newline_at + 1:]
                    redact_line(line, writer)
            if pending:
                redact_line(pending, writer)
            writer.flush()
            os.fsync(writer.fileno())
        if cancel.is_set():
            raise InterruptedError("附件导出已取消")
        os.replace(temporary, destination)
        if redaction_counter is not None:
            redaction_counter[0] += count
    except Exception:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise


def _redact_ordinary_text(text: str, cancel=None) -> tuple[str, int]:
    """Apply single-line secret, bearer, and local-path rules; PEM blocks are streamed."""
    count = 0

    def secret_repl(match: re.Match[str]) -> str:
        nonlocal count
        _check_cancel(cancel, "脱敏已取消")
        count += 1
        quote = match.group(2) or ""
        return match.group(1) + quote + "[已脱敏: 敏感值]" + quote

    _check_cancel(cancel, "脱敏已取消")
    text = _SECRET_VALUE.sub(secret_repl, text)
    _check_cancel(cancel, "脱敏已取消")
    text, n = _BEARER.subn("Bearer [已脱敏: 令牌]", text)
    count += n
    _check_cancel(cancel, "脱敏已取消")
    text, n = _LOCAL_PATH.subn("[已脱敏: 本地路径]", text)
    count += n
    _check_cancel(cancel, "脱敏已取消")
    return text, count


def _has_unclosed_secret_quote(value: str, cancel=None) -> bool:
    for match in _SECRET_QUOTED_OPEN.finditer(value):
        _check_cancel(cancel, "脱敏已取消")
        quote_char = match.group("quote")
        escaped = False
        for offset in range(match.end(), len(value), _FORMAT_CHUNK_SIZE):
            _check_cancel(cancel, "脱敏已取消")
            segment = value[offset:offset + _FORMAT_CHUNK_SIZE]
            for char in segment:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote_char:
                    break
            else:
                continue
            break
        else:
            return True
    return False


def _redact_text_cancelable(text: str, cancel) -> tuple[str, int]:
    """Redact bounded line buffers so cancellation is checked during large records."""
    output: list[str] = []
    count = 0
    pending = ""
    quoted_pending = ""
    in_private_key = False

    def redact_pem_blocks(value: str) -> str:
        nonlocal count, in_private_key
        remaining = value
        parts: list[str] = []
        while remaining:
            _check_cancel(cancel, "脱敏已取消")
            if in_private_key:
                end = _PRIVATE_KEY_END.search(remaining)
                if not end:
                    return "".join(parts)
                remaining = remaining[end.end():]
                in_private_key = False
                continue
            begin = _PRIVATE_KEY_BEGIN.search(remaining)
            if not begin:
                parts.append(remaining)
                break
            parts.append(remaining[:begin.start()])
            parts.append("[已脱敏: 私钥]")
            count += 1
            end = _PRIVATE_KEY_END.search(remaining, begin.end())
            if end:
                remaining = remaining[end.end():]
                continue
            in_private_key = True
            break
        return "".join(parts)

    def process_line(line: str) -> None:
        nonlocal count, quoted_pending
        _check_cancel(cancel, "脱敏已取消")
        if quoted_pending:
            raise ValueError("检测到跨行引号敏感值，无法安全脱敏，已停止导出")
        elif _has_unclosed_secret_quote(line, cancel):
            quoted_pending = line
            return
        cleaned, occurrences = _redact_ordinary_text(line, cancel)
        count += occurrences
        output.append(redact_pem_blocks(cleaned))

    for offset in range(0, len(text), _FORMAT_CHUNK_SIZE):
        _check_cancel(cancel, "脱敏已取消")
        pending += text[offset:offset + _FORMAT_CHUNK_SIZE]
        while True:
            newline_at = pending.find("\n")
            if newline_at < 0:
                if len(pending) > _MAX_REDACTION_LINE_CHARS:
                    raise ValueError("单条记录超过 1 Mi 字符脱敏上限")
                break
            if newline_at + 1 > _MAX_REDACTION_LINE_CHARS:
                raise ValueError("单条记录超过 1 Mi 字符脱敏上限")
            line = pending[:newline_at + 1]
            pending = pending[newline_at + 1:]
            process_line(line)
    if pending:
        process_line(pending)
    if quoted_pending:
        raise ValueError("记录包含未闭合的引号敏感值，已停止脱敏导出")
    _check_cancel(cancel, "脱敏已取消")
    return "".join(output), count


def _write_attachment(
    source: Path,
    destination: Path,
    redact: bool,
    issues: list[str],
    redaction_counter: list[int] | None = None,
    cancel=None,
) -> bool:
    if _sensitive_attachment(source):
        issues.append(f"已跳过凭据或配置类附件：{source.name}")
        return False
    if redact and source.suffix.casefold() in _REDACTABLE_SUFFIXES:
        try:
            _redact_attachment_stream(source, destination, cancel, redaction_counter)
        except InterruptedError:
            raise
        except UnicodeDecodeError as exc:
            issues.append(f"脱敏附件无法安全解码，已跳过：{source.name}（{type(exc).__name__}）")
            return False
        except ValueError as exc:
            issues.append(f"脱敏附件无法安全处理，已跳过：{source.name}（{exc}）")
            return False
        except OSError as exc:
            issues.append(f"脱敏附件无法安全解码，已跳过：{source.name}（{type(exc).__name__}）")
            return False
        return True
    _copy_path_with_cancel(source, destination, cancel)
    return True


def _copy_path_with_cancel(source: Path, destination: Path, cancel=None) -> None:
    if cancel is None:
        import threading

        cancel = threading.Event()
    try:
        with source.open("rb") as src, destination.open("wb") as dst:
            while True:
                if cancel is not None and cancel.is_set():
                    raise InterruptedError("附件导出已取消")
                block = src.read(_COPY_CHUNK_SIZE)
                if not block:
                    break
                dst.write(block)
    except Exception:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _copy_inline_image(value: str, output_root: Path, cancel) -> str:
    """Decode a base64 image data URL in bounded chunks and clean up on failure."""
    header, encoded = value.split(",", 1)
    if not header.casefold().startswith("data:image/") or not header.casefold().endswith(";base64"):
        raise ValueError("不是受支持的 base64 图片 data URL")
    if len(encoded) % 4:
        raise ValueError("base64 图片长度无效")
    padding = len(encoded) - len(encoded.rstrip("="))
    if padding > 2 or (padding and "=" in encoded[:-padding]):
        raise ValueError("base64 图片填充无效")
    if any(char.isspace() for char in encoded):
        raise ValueError("base64 图片包含无效空白")

    mime_subtype = header[len("data:image/"):-len(";base64")].casefold()
    extension = "jpg" if mime_subtype == "jpeg" else mime_subtype.replace("svg+xml", "svg")
    extension = safe_name(extension, 10)
    digest = hashlib.sha256()
    # Hash incrementally to avoid a second full-size byte copy for large images.
    for offset in range(0, len(value), _INLINE_IMAGE_CHUNK_SIZE):
        if cancel.is_set():
            raise InterruptedError("内嵌图片导出已取消")
        digest.update(value[offset:offset + _INLINE_IMAGE_CHUNK_SIZE].encode("utf-8", "replace"))
    relative = Path("attachments") / f"image-{digest.hexdigest()[:16]}.{extension}"
    destination = output_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("wb") as target:
            for offset in range(0, len(encoded), _INLINE_IMAGE_CHUNK_SIZE):
                if cancel.is_set():
                    raise InterruptedError("内嵌图片导出已取消")
                chunk = encoded[offset:offset + _INLINE_IMAGE_CHUNK_SIZE]
                target.write(base64.b64decode(chunk, validate=True))
            if cancel.is_set():
                raise InterruptedError("内嵌图片导出已取消")
    except Exception:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return relative.as_posix()


def redact_text(text: str, cancel=None) -> tuple[str, int]:
    return _redact_text_cancelable(text, cancel)


def _sanitize(value: Any, redact: bool, counter: list[int], cancel=None) -> Any:
    _check_cancel(cancel, "脱敏已取消")
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            _check_cancel(cancel, "脱敏已取消")
            lowered = str(key).casefold()
            if lowered in {"encrypted_content", "reasoning", "analysis"}:
                continue
            if redact and _SECRET_KEY_NAME.search(str(key)):
                result[key] = "[已脱敏: 敏感字段]"
            else:
                result[key] = _sanitize(child, redact, counter, cancel)
        return result
    if isinstance(value, list):
        result = []
        for item in value:
            _check_cancel(cancel, "脱敏已取消")
            result.append(_sanitize(item, redact, counter, cancel))
        return result
    if isinstance(value, str) and redact:
        cleaned, count = redact_text(value, cancel)
        counter[0] += count
        return cleaned
    return value


def _safe_redacted_text(value: str, cancel, fallback: str, counter: list[int] | None = None) -> tuple[str, bool]:
    try:
        cleaned, count = redact_text(str(value), cancel)
        if counter is not None:
            counter[0] += count
        return cleaned, False
    except InterruptedError:
        raise
    except ValueError:
        return fallback, True


def _safe_report_sanitize(value: Any, redact: bool, cancel=None, counter: list[int] | None = None) -> Any:
    """Sanitize report fields independently; unsafe fields become fixed placeholders."""
    _check_cancel(cancel, "报告脱敏已取消")
    if not redact:
        return value
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            _check_cancel(cancel, "报告脱敏已取消")
            if str(key).casefold() in {"encrypted_content", "reasoning", "analysis"}:
                continue
            if _SECRET_KEY_NAME.search(str(key)):
                result[key] = "[已脱敏: 敏感字段]"
            else:
                result[key] = _safe_report_sanitize(child, redact, cancel, counter)
        return result
    if isinstance(value, list):
        result = []
        for item in value:
            _check_cancel(cancel, "报告脱敏已取消")
            result.append(_safe_report_sanitize(item, redact, cancel, counter))
        return result
    if isinstance(value, str):
        return _safe_redacted_text(value, cancel, "[已省略: 无法安全脱敏]", counter)[0]
    return value


def _safe_batch_result(result: dict[str, Any], redact: bool, cancel=None) -> dict[str, Any]:
    if not redact:
        return result
    safe = dict(result)
    safe["session_id"] = _safe_redacted_text(str(result.get("session_id", "")), cancel, "已脱敏ID")[0]
    safe["title"] = _safe_redacted_text(str(result.get("title", "")), cancel, "已脱敏会话")[0]
    safe["folder"] = _safe_redacted_text(str(result.get("folder", "")), cancel, "[已省略: 路径无法安全脱敏]")[0]
    safe["zip"] = _safe_redacted_text(str(result.get("zip", "")), cancel, "[已省略: 路径无法安全脱敏]")[0]
    safe["zip_file"] = _safe_report_sanitize(result.get("zip_file"), redact, cancel)
    safe["issues"] = _safe_report_sanitize(result.get("issues", []), redact, cancel)
    safe["files"] = _safe_report_sanitize(result.get("files", []), redact, cancel)
    return safe


def _render_session(
    session: SessionInfo,
    redact: bool,
    counter: list[int],
    cancel=None,
    safe_title: str | None = None,
    safe_session_id: str | None = None,
) -> SessionInfo:
    if not redact:
        return session
    fields = {
        "session_id": safe_session_id if safe_session_id is not None else session.session_id,
        "title": safe_title if safe_title is not None else session.title,
        "created_at": session.created_at,
        "raw_created_at": session.raw_created_at,
        "updated_at": session.updated_at,
        "cwd": session.cwd,
        "issues": session.issues,
    }
    safe = _sanitize(fields, True, counter, cancel)
    return replace(session, **safe)


def _copy_related_attachments(session: SessionInfo, catalog: Catalog, target: str, events: list[dict[str, Any]], cancel, issues: list[str], redact: bool = False, redaction_counter: list[int] | None = None) -> dict[str, str]:
    """Copy only the session-ID attachment folder and exact paths referenced by its records."""
    root = Path(catalog.root)
    attachment_root = root / "attachments"
    session_folder = attachment_root / session.session_id
    output_root = Path(target)
    copied: dict[str, str] = {}
    by_basename: dict[str, list[str]] = {}
    folder_files: list[Path] = []
    attachment_root_safe = not _has_link_or_junction_component(attachment_root, catalog.root) and _is_within_root(str(attachment_root), catalog.root)
    linked_folders = list(dict.fromkeys([*(attachment_root / attachment_id for attachment_id in sorted(session.attachment_ids)), session_folder])) if attachment_root_safe else []
    for linked_folder in linked_folders:
        if cancel.is_set():
            raise InterruptedError("附件目录扫描已取消")
        if linked_folder.is_dir() and not _has_link_or_junction_component(linked_folder, catalog.root):
            try:
                def report_walk_error(error: OSError) -> None:
                    location = error.filename or str(linked_folder)
                    message = f"附件目录读取失败：{location}（{type(error).__name__}）"
                    if message not in issues:
                        issues.append(message)

                for directory, directories, filenames in os.walk(
                    linked_folder, followlinks=False, onerror=report_walk_error
                ):
                    if cancel.is_set():
                        raise InterruptedError("附件目录扫描已取消")
                    directories[:] = [name for name in directories if not _is_link_or_junction(Path(directory) / name)]
                    for filename in filenames:
                        path = Path(directory) / filename
                        destination: Path | None = None
                        if cancel.is_set():
                            raise InterruptedError("附件复制已取消")
                        if not path.is_file() or _has_link_or_junction_component(path, catalog.root):
                            continue
                        resolved = path.resolve()
                        if not _is_within_root(str(resolved), catalog.root) or not _is_within_root(str(resolved), str(attachment_root)):
                            continue
                        digest = hashlib.sha256(str(resolved).encode("utf-8", "replace")).hexdigest()[:10]
                        suffix = "." + safe_name(path.suffix.lstrip("."), 12) if path.suffix else ""
                        stem = f"attachment-{digest}" if redact else f"{safe_name(path.stem, 64)}-{digest}"
                        destination_rel = Path("attachments") / f"{stem}{suffix}"
                        destination = output_root / destination_rel
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        try:
                            if _write_attachment(path, destination, redact, issues, redaction_counter, cancel):
                                folder_files.append(path)
                                copied[str(path.resolve())] = destination_rel.as_posix()
                                by_basename.setdefault(path.name.casefold(), []).append(destination_rel.as_posix())
                            elif destination.exists():
                                destination.unlink()
                        except InterruptedError:
                            raise
                        except OSError as exc:
                            try:
                                if destination is not None and destination.exists():
                                    destination.unlink()
                            except OSError:
                                pass
                            issues.append(f"附件读取失败，已跳过：{path.name}（{type(exc).__name__}）")
            except InterruptedError:
                raise
            except OSError as exc:
                issues.append(f"附件目录读取失败：{type(exc).__name__}: {exc}")

    # Keep only the deduplication set; a second flattened copy of every event's
    # attachment references can be large for histories with many pasted files.
    image_files = [path for path in folder_files if path.suffix.casefold() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}]
    seen_refs: set[str] = set()
    for event in events:
        for ref in event.get("attachments", []):
            if cancel.is_set():
                raise InterruptedError("附件处理已取消")
            value = ref.get("ref")
            if not isinstance(value, str) or not value:
                continue
            if value in seen_refs:
                continue
            seen_refs.add(value)
            if value.startswith("[[ATTACHMENT:"):
                value = value.removeprefix("[[ATTACHMENT:").removesuffix("]]" )
            if value.startswith("data:image/") and ";base64," in value:
                destination: Path | None = None
                try:
                    copied[value] = _copy_inline_image(value, output_root, cancel)
                    continue
                except InterruptedError:
                    raise
                except Exception as exc:
                    if destination is not None:
                        try:
                            destination.unlink(missing_ok=True)
                        except OSError:
                            pass
                    issues.append(f"内嵌图片附件无法解码：{type(exc).__name__}")
                    continue
            local_path = _local_path_from_reference(value)
            if local_path and os.path.isfile(local_path):
                destination: Path | None = None
                try:
                    src = Path(local_path)
                    if _has_symlink_component(src):
                        issues.append(f"附件路径包含符号链接，已跳过：{src.name}")
                        continue
                    resolved_source = src.resolve(strict=True)
                    if _sensitive_attachment(src) or _sensitive_attachment(resolved_source):
                        issues.append(f"已跳过凭据或配置类附件：{src.name}")
                        continue
                    digest = hashlib.sha256(str(src.resolve()).encode("utf-8", "replace")).hexdigest()[:10]
                    suffix = "." + safe_name(src.suffix.lstrip("."), 12) if src.suffix else ""
                    stem = f"attachment-{digest}" if redact else f"{safe_name(src.stem, 64)}-{digest}"
                    rel = Path("attachments") / f"{stem}{suffix}"
                    destination = output_root / rel
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if not destination.exists():
                        if not _write_attachment(src, destination, redact, issues, redaction_counter, cancel):
                            continue
                    copied[value] = rel.as_posix()
                    continue
                except InterruptedError:
                    raise
                except (OSError, RuntimeError) as exc:
                    try:
                        if destination is not None and destination.exists():
                            destination.unlink()
                    except OSError:
                        pass
                    issues.append(f"明确引用的附件读取失败：{value}：{exc}")
                    continue
            basename = Path(value.replace("\\", "/")).name.casefold()
            matches = by_basename.get(basename, [])
            if len(matches) == 1:
                copied[value] = matches[0]
            elif len(matches) > 1:
                issues.append(f"附件有多个同名候选，未自动关联：{basename or value[:180]}")
            elif "image" in str(ref.get("kind", "")).casefold():
                if len(image_files) == 1:
                    copied[value] = copied.get(str(image_files[0].resolve()), "")
                    if not copied[value]:
                        issues.append(f"附件未找到或关联不明确：{value[:180]}")
                else:
                    issues.append(f"附件未找到或关联不明确：{value[:180]}")
            else:
                issues.append(f"附件未找到：{value[:180]}")
    for path in folder_files:
        key = str(path.resolve())
        containing_folder = next((folder for folder in linked_folders if folder.is_dir() and folder in path.parents), session_folder)
        rel = Path("attachments") / Path(*[safe_name(p, 120) for p in (Path(containing_folder.name) / path.relative_to(containing_folder)).parts])
        copied.setdefault(key, rel.as_posix())
    return copied


def _local_path_from_reference(value: str) -> str:
    if value.casefold().startswith("file:"):
        parsed = urlparse(value)
        path = unquote(parsed.path)
        if os.name == "nt" and path.startswith("/") and re.match(r"^/[A-Za-z]:/", path):
            path = path[1:]
        if parsed.netloc and os.name == "nt":
            path = "\\\\" + parsed.netloc + path.replace("/", "\\")
        return path
    if os.path.isabs(value) or re.match(r"^[A-Za-z]:[\\/]", value):
        return value
    return ""


def _map_attachments_in_event(event: dict[str, Any], mapping: dict[str, str], cancel=None) -> dict[str, Any]:
    record = dict(event)
    linked = []
    for attachment in event.get("attachments", []):
        _check_cancel(cancel, "附件引用处理已取消")
        ref = attachment.get("ref")
        path = mapping.get(ref) if isinstance(ref, str) else None
        linked.append({"kind": attachment.get("kind", "附件"), "path": path or "", "status": "已导出" if path else "未找到", "reference": path or "未找到"})
    record["attachments"] = linked
    if isinstance(record.get("text"), str):
        record["text"] = _replace_attachment_references(record["text"], mapping, cancel)
    if isinstance(record.get("data"), (dict, list)):
        record["data"] = _rewrite_attachment_refs(record["data"], mapping, cancel)
    return record


def _map_text_event(event: dict[str, Any], mapping: dict[str, str], cancel=None) -> dict[str, Any]:
    """Build only the fields used by readable text formats, avoiding a copy of raw payload data."""
    _check_cancel(cancel, "附件引用处理已取消")
    fields = ("kind", "role", "phase", "name", "call_id", "text")
    record = {key: event[key] for key in fields if key in event}
    if isinstance(record.get("text"), str):
        record["text"] = _replace_attachment_references(record["text"], mapping, cancel)
    attachments = []
    for attachment in event.get("attachments", []):
        _check_cancel(cancel, "附件引用处理已取消")
        reference = attachment.get("ref")
        path = mapping.get(reference) if isinstance(reference, str) else None
        attachments.append({
            "kind": attachment.get("kind", "附件"),
            "path": path or "",
            "status": "已导出" if path else "未找到",
            "reference": path or "未找到",
        })
    record["attachments"] = attachments
    return record


def _replace_attachment_references(text: str, mapping: dict[str, str], cancel=None) -> str:
    def replace_marker(match: re.Match[str]) -> str:
        _check_cancel(cancel, "附件引用处理已取消")
        return mapping.get(match.group(1), "[附件未找到]")

    _check_cancel(cancel, "附件引用处理已取消")
    # Readable exports pass an empty mapping after their projection phase. Keep
    # cancellation semantics while avoiding a regex pass for ordinary text.
    if not mapping and "[[ATTACHMENT:" not in text:
        return text
    text = _ATTACHMENT_MARKER.sub(replace_marker, text)
    for reference, relative in mapping.items():
        _check_cancel(cancel, "附件引用处理已取消")
        if not isinstance(reference, str) or not reference:
            continue
        variants = {reference, reference.replace("\\", "/"), reference.replace("/", "\\")}
        for variant in variants:
            _check_cancel(cancel, "附件引用处理已取消")
            if variant in text:
                text = text.replace(variant, relative)
        attachment_id = re.search(r"(?i)(?:^|[\\/])attachments[\\/]([0-9a-f-]{20,})[\\/]", reference)
        if attachment_id:
            file_name = re.escape(Path(reference.replace("\\", "/")).name)
            pattern = re.compile(rf"(?i)(?:[A-Z]:[\\/]|\\\\)[^\s\"'<>]*?[\\/]attachments[\\/]{re.escape(attachment_id.group(1))}[\\/]{file_name}")

            def replace_attachment_path(_match: re.Match[str]) -> str:
                _check_cancel(cancel, "附件引用处理已取消")
                return relative

            text = pattern.sub(replace_attachment_path, text)
    return text


def _rewrite_attachment_refs(value: Any, mapping: dict[str, str], cancel=None) -> Any:
    _check_cancel(cancel, "附件引用处理已取消")
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            _check_cancel(cancel, "附件引用处理已取消")
            if str(key).casefold() in {"image_url", "image_path", "file_path", "attachment_path", "local_image_path"} and isinstance(child, str):
                result[key] = mapping.get(child, "[附件未找到]")
            else:
                result[key] = _rewrite_attachment_refs(child, mapping, cancel)
        return result
    if isinstance(value, list):
        result = []
        for item in value:
            _check_cancel(cancel, "附件引用处理已取消")
            result.append(_rewrite_attachment_refs(item, mapping, cancel))
        return result
    if isinstance(value, str) and value in mapping:
        return mapping[value]
    if isinstance(value, str):
        return _replace_attachment_references(value, mapping, cancel)
    return value


def _chat_events(events: list[dict[str, Any]], mode: str) -> Iterable[dict[str, Any]]:
    for event in events:
        if mode != "chat" or (event.get("kind") == "message" and event.get("role") in {"user", "assistant"}):
            yield event


def _escape_html_with_breaks(text: str, cancel=None, line_breaks: bool = True) -> str:
    chunks = []
    for offset in range(0, len(text), _FORMAT_CHUNK_SIZE):
        _check_cancel(cancel, "HTML 格式化已取消")
        escaped = html.escape(text[offset:offset + _FORMAT_CHUNK_SIZE], quote=True)
        chunks.append(escaped.replace("\n", "<br>\n") if line_breaks else escaped)
    _check_cancel(cancel, "HTML 格式化已取消")
    return "".join(chunks)


def _markdown_code_fence(text: str) -> str:
    """Choose a fence longer than any backtick run in untrusted event text."""
    longest = 0
    current = 0
    for char in text:
        if char == "`":
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return "`" * max(3, longest + 1)


def _markdown_summary_text(value: Any) -> str:
    """Escape the HTML summary wrapper while preserving ordinary Unicode."""
    return html.escape(str(value), quote=False).replace("\r", " ").replace("\n", " ")


def _markdown_attachment_label(value: Any) -> str:
    """Keep untrusted attachment labels inside one Markdown link label."""
    return (
        html.escape(str(value), quote=False)
        .replace("\\", "\\\\")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def _format_txt(session: SessionInfo, events: list[dict[str, Any]], mode: str, mapping: dict[str, str] | None = None, cancel=None) -> str:
    platform = {"codex": "Codex", "chatgpt": "ChatGPT 网页", "unknown": "来源未知"}.get(session.platform, "来源未知")
    lines = [f"会话：{session.title}", f"会话 ID：{session.session_id}", f"来源类别：{platform}", f"创建时间（本机）：{session.created_at or '未找到'}", f"创建时间（原始）：{session.raw_created_at or '未找到'}", f"更新时间（本机）：{session.updated_at or '未找到'}", f"工作目录：{session.cwd or '未找到'}", f"是否归档：{'是' if session.archived else '否'}", f"阅读版：{'纯聊天' if mode == 'chat' else '完整版'}", ""]
    for event in _chat_events(events, mode):
        if cancel is not None and cancel.is_set():
            raise InterruptedError("文本格式化已取消")
        kind = event.get("kind")
        if kind == "message":
            role = "用户" if event.get("role") == "user" else "助手"
            suffix = f"（{event.get('phase')}）" if event.get("phase") else ""
            text = str(event.get("text", ""))
            _check_cancel(cancel, "文本格式化已取消")
            text = _replace_attachment_references(text, mapping or {}, cancel)
            lines.extend([f"[{role}{suffix}]", text, ""])
            for item in event.get("attachments", []):
                if cancel is not None and cancel.is_set():
                    raise InterruptedError("文本格式化已取消")
                reference = item.get("ref")
                path = item.get("path") or ((mapping or {}).get(reference) if isinstance(reference, str) else None)
                lines.append(f"[附件] {path or '未找到'}")
            if event.get("attachments"):
                lines.append("")
        elif kind == "tool_call":
            _check_cancel(cancel, "文本格式化已取消")
            lines.extend([f"[工具调用] {event.get('name', '未知工具')}  call_id={event.get('call_id') or '未找到'}", _replace_attachment_references(str(event.get("text", "")), mapping or {}, cancel), ""])
        elif kind == "tool_result":
            _check_cancel(cancel, "文本格式化已取消")
            lines.extend([f"[工具结果] {event.get('name', '工具结果')}  call_id={event.get('call_id') or '未找到'}", _replace_attachment_references(str(event.get("text", "")), mapping or {}, cancel), ""])
        elif kind in {"status", "unknown"}:
            _check_cancel(cancel, "文本格式化已取消")
            lines.extend([f"[状态 / {event.get('name', '未知类型')}]", _replace_attachment_references(str(event.get("text", "")), mapping or {}, cancel), ""])
    if session.issues:
        _check_cancel(cancel, "文本格式化已取消")
        lines.extend(["[读取报告]", *session.issues])
    _check_cancel(cancel, "文本格式化已取消")
    return "\n".join(lines)


def _format_markdown(session: SessionInfo, events: list[dict[str, Any]], mode: str, mapping: dict[str, str] | None = None, cancel=None) -> str:
    platform = {"codex": "Codex", "chatgpt": "ChatGPT 网页", "unknown": "来源未知"}.get(session.platform, "来源未知")
    lines = [f"# {session.title}", "", f"- 来源类别：{platform}", f"- 会话 ID：`{session.session_id}`", f"- 创建时间（本机）：{session.created_at or '未找到'}", f"- 创建时间（原始）：`{session.raw_created_at or '未找到'}`", f"- 更新时间（本机）：{session.updated_at or '未找到'}", f"- 工作目录：`{session.cwd or '未找到'}`", f"- 归档：{'是' if session.archived else '否'}", f"- 阅读版：{'纯聊天' if mode == 'chat' else '完整版'}", ""]
    for event in _chat_events(events, mode):
        if cancel is not None and cancel.is_set():
            raise InterruptedError("Markdown 格式化已取消")
        kind = event.get("kind")
        if kind == "message":
            role = "用户" if event.get("role") == "user" else "助手"
            phase = f"（{event.get('phase')}）" if event.get("phase") else ""
            _check_cancel(cancel, "Markdown 格式化已取消")
            text = _replace_attachment_references(str(event.get("text", "")), mapping or {}, cancel)
            lines.extend([f"## {role}{phase}", "", text, ""])
            for item in event.get("attachments", []):
                if cancel is not None and cancel.is_set():
                    raise InterruptedError("Markdown 格式化已取消")
                reference = item.get("ref")
                path = item.get("path") or ((mapping or {}).get(reference) if isinstance(reference, str) else None)
                if path:
                    label = _markdown_attachment_label(item.get("kind", "附件"))
                    destination = quote(str(path), safe="/._-")
                    if Path(str(path)).suffix.casefold() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}:
                        lines.append(f"![{label}]({destination})")
                    else:
                        lines.append(f"[附件：{label}]({destination})")
                else:
                    lines.append("[附件未找到]")
        elif kind in {"tool_call", "tool_result", "status", "unknown"}:
            label = {"tool_call": "工具调用", "tool_result": "工具结果", "status": "状态事件", "unknown": "未知记录"}[kind]
            heading = f"{label}：{event.get('name', '')}"
            if kind in {"tool_call", "tool_result"}:
                heading += f"（call_id={event.get('call_id') or '未找到'}）"
            text = _replace_attachment_references(str(event.get("text", "")), mapping or {}, cancel)
            fence = _markdown_code_fence(text)
            lines.extend([f"<details><summary>{_markdown_summary_text(heading)}</summary>", "", f"{fence}text", text, fence, "", "</details>", ""])
    if session.issues:
        _check_cancel(cancel, "Markdown 格式化已取消")
        lines.extend(["## 读取报告", "", *[f"- {issue}" for issue in session.issues], ""])
    _check_cancel(cancel, "Markdown 格式化已取消")
    return "\n".join(lines)


def _format_html(session: SessionInfo, events: list[dict[str, Any]], mode: str, mapping: dict[str, str] | None = None, cancel=None) -> str:
    def esc(value: Any) -> str:
        return html.escape(str(value), quote=True)

    chunks = []
    for event in _chat_events(events, mode):
        if cancel is not None and cancel.is_set():
            raise InterruptedError("HTML 格式化已取消")
        kind = event.get("kind")
        raw_text = str(event.get("text", ""))
        raw_text = _replace_attachment_references(raw_text, mapping or {}, cancel)
        text = _escape_html_with_breaks(raw_text, cancel, line_breaks=(kind == "message"))
        if kind == "message":
            role = "用户" if event.get("role") == "user" else "助手"
            phase = f" · {esc(event.get('phase'))}" if event.get("phase") else ""
            attachments = []
            for item in event.get("attachments", []):
                if cancel is not None and cancel.is_set():
                    raise InterruptedError("HTML 格式化已取消")
                if item.get("path"):
                    rel = esc(quote(str(item["path"]), safe="/._-"))
                    attachments.append(f'<a href="{rel}">{esc(item.get("kind", "附件"))}</a>')
                else:
                    attachments.append("附件未找到")
            content = text
            if attachments:
                content += "<p class=\"attachments\">" + " · ".join(attachments) + "</p>"
            chunks.append(f'<article class="message {"user" if role == "用户" else "assistant"}"><h2>{role}{phase}</h2><div>{content}</div></article>')
        elif kind in {"tool_call", "tool_result", "status", "unknown"}:
            label = {"tool_call": "工具调用", "tool_result": "工具结果", "status": "状态事件", "unknown": "未知记录"}[kind]
            title = f"{label} · {event.get('name', '')}"
            call_id = event.get("call_id")
            if call_id:
                title += f" · call_id={call_id}"
            chunks.append(f"<details><summary>{esc(title)}</summary><pre>{text}</pre></details>")
    _check_cancel(cancel, "HTML 格式化已取消")
    issues = "".join(f"<li>{esc(issue)}</li>" for issue in session.issues)
    platform = {"codex": "Codex", "chatgpt": "ChatGPT 网页", "unknown": "来源未知"}.get(session.platform, "来源未知")
    meta = " · ".join([f"类别 {esc(platform)}", f"ID {esc(session.session_id)}", f"创建 {esc(session.created_at or '未找到')}", f"更新 {esc(session.updated_at or '未找到')}", f"工作目录 {esc(session.cwd or '未找到')}", "已归档" if session.archived else "未归档"])
    issue_section = f"<section><h2>读取报告</h2><ul>{issues}</ul></section>" if issues else ""
    _check_cancel(cancel, "HTML 格式化已取消")
    return """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><style>
body{{font:15px/1.65 system-ui,"Microsoft YaHei",sans-serif;max-width:1000px;margin:28px auto;padding:0 18px;color:#202124;background:#fafafa}}
h1{{font-size:24px;margin:0 0 8px}}.meta{{color:#59636d;border-bottom:1px solid #d9dde1;padding:0 0 18px;margin-bottom:20px;overflow-wrap:anywhere}}
article,details,section{{border:1px solid #d7dce0;border-radius:6px;background:white;margin:12px 0;padding:12px 15px;overflow-wrap:anywhere}}
article.user{{border-left:4px solid #168b78}}article.assistant{{border-left:4px solid #4868a8}}h2{{font-size:15px;margin:0 0 8px}}
pre{{white-space:pre-wrap;word-break:break-word;overflow:auto;max-height:65vh;background:#f3f5f6;padding:10px;border-radius:4px}}
summary{{cursor:pointer;font-weight:600}}a{{color:#176e60}}.attachments{{margin-bottom:0}}
</style></head><body><h1>{title}</h1><p class="meta">{meta}</p>{content}{issues}</body></html>""".format(title=esc(session.title), meta=meta, content="\n".join(chunks), issues=issue_section)


def _iter_joined_lines(lines: Iterable[str]) -> Iterable[str]:
    first = True
    for line in lines:
        if not first:
            yield "\n"
        yield line
        first = False


def _iter_txt_chunks(session: SessionInfo, events: Iterable[dict[str, Any]], mode: str, mapping: dict[str, str] | None = None, cancel=None) -> Iterable[str]:
    def lines() -> Iterable[str]:
        platform = {"codex": "Codex", "chatgpt": "ChatGPT 网页", "unknown": "来源未知"}.get(session.platform, "来源未知")
        yield f"会话：{session.title}"
        yield f"会话 ID：{session.session_id}"
        yield f"来源类别：{platform}"
        yield f"创建时间（本机）：{session.created_at or '未找到'}"
        yield f"创建时间（原始）：{session.raw_created_at or '未找到'}"
        yield f"更新时间（本机）：{session.updated_at or '未找到'}"
        yield f"工作目录：{session.cwd or '未找到'}"
        yield f"是否归档：{'是' if session.archived else '否'}"
        yield f"阅读版：{'纯聊天' if mode == 'chat' else '完整版'}"
        yield ""
        for event in _chat_events(events, mode):
            _check_cancel(cancel, "文本格式化已取消")
            kind = event.get("kind")
            if kind == "message":
                role = "用户" if event.get("role") == "user" else "助手"
                suffix = f"（{event.get('phase')}）" if event.get("phase") else ""
                text = _replace_attachment_references(str(event.get("text", "")), mapping or {}, cancel)
                yield f"[{role}{suffix}]"
                yield text
                yield ""
                for item in event.get("attachments", []):
                    _check_cancel(cancel, "文本格式化已取消")
                    reference = item.get("ref")
                    path = item.get("path") or ((mapping or {}).get(reference) if isinstance(reference, str) else None)
                    yield f"[附件] {path or '未找到'}"
                if event.get("attachments"):
                    yield ""
            elif kind == "tool_call":
                yield f"[工具调用] {event.get('name', '未知工具')}  call_id={event.get('call_id') or '未找到'}"
                yield _replace_attachment_references(str(event.get("text", "")), mapping or {}, cancel)
                yield ""
            elif kind == "tool_result":
                yield f"[工具结果] {event.get('name', '工具结果')}  call_id={event.get('call_id') or '未找到'}"
                yield _replace_attachment_references(str(event.get("text", "")), mapping or {}, cancel)
                yield ""
            elif kind in {"status", "unknown"}:
                yield f"[状态 / {event.get('name', '未知类型')}]"
                yield _replace_attachment_references(str(event.get("text", "")), mapping or {}, cancel)
                yield ""
        if session.issues:
            _check_cancel(cancel, "文本格式化已取消")
            yield "[读取报告]"
            yield from session.issues
        _check_cancel(cancel, "文本格式化已取消")

    yield from _iter_joined_lines(lines())


def _iter_markdown_chunks(session: SessionInfo, events: Iterable[dict[str, Any]], mode: str, mapping: dict[str, str] | None = None, cancel=None) -> Iterable[str]:
    def lines() -> Iterable[str]:
        platform = {"codex": "Codex", "chatgpt": "ChatGPT 网页", "unknown": "来源未知"}.get(session.platform, "来源未知")
        yield f"# {session.title}"
        yield ""
        yield f"- 来源类别：{platform}"
        yield f"- 会话 ID：`{session.session_id}`"
        yield f"- 创建时间（本机）：{session.created_at or '未找到'}"
        yield f"- 创建时间（原始）：`{session.raw_created_at or '未找到'}`"
        yield f"- 更新时间（本机）：{session.updated_at or '未找到'}"
        yield f"- 工作目录：`{session.cwd or '未找到'}`"
        yield f"- 归档：{'是' if session.archived else '否'}"
        yield f"- 阅读版：{'纯聊天' if mode == 'chat' else '完整版'}"
        yield ""
        for event in _chat_events(events, mode):
            _check_cancel(cancel, "Markdown 格式化已取消")
            kind = event.get("kind")
            if kind == "message":
                role = "用户" if event.get("role") == "user" else "助手"
                phase = f"（{event.get('phase')}）" if event.get("phase") else ""
                yield f"## {role}{phase}"
                yield ""
                yield _replace_attachment_references(str(event.get("text", "")), mapping or {}, cancel)
                yield ""
                for item in event.get("attachments", []):
                    _check_cancel(cancel, "Markdown 格式化已取消")
                    reference = item.get("ref")
                    path = item.get("path") or ((mapping or {}).get(reference) if isinstance(reference, str) else None)
                    if path:
                        label = _markdown_attachment_label(item.get("kind", "附件"))
                        destination = quote(str(path), safe="/._-")
                        if Path(str(path)).suffix.casefold() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}:
                            yield f"![{label}]({destination})"
                        else:
                            yield f"[附件：{label}]({destination})"
                    else:
                        yield "[附件未找到]"
            elif kind in {"tool_call", "tool_result", "status", "unknown"}:
                label = {"tool_call": "工具调用", "tool_result": "工具结果", "status": "状态事件", "unknown": "未知记录"}[kind]
                heading = f"{label}：{event.get('name', '')}"
                if kind in {"tool_call", "tool_result"}:
                    heading += f"（call_id={event.get('call_id') or '未找到'}）"
                text = _replace_attachment_references(str(event.get("text", "")), mapping or {}, cancel)
                fence = _markdown_code_fence(text)
                yield f"<details><summary>{_markdown_summary_text(heading)}</summary>"
                yield ""
                yield f"{fence}text"
                yield text
                yield fence
                yield ""
                yield "</details>"
                yield ""
        if session.issues:
            _check_cancel(cancel, "Markdown 格式化已取消")
            yield "## 读取报告"
            yield ""
            for issue in session.issues:
                yield f"- {issue}"
            yield ""
        _check_cancel(cancel, "Markdown 格式化已取消")

    yield from _iter_joined_lines(lines())


def _iter_html_escape(text: str, cancel=None, line_breaks: bool = True) -> Iterable[str]:
    for offset in range(0, len(text), _FORMAT_CHUNK_SIZE):
        _check_cancel(cancel, "HTML 格式化已取消")
        escaped = html.escape(text[offset:offset + _FORMAT_CHUNK_SIZE], quote=True)
        yield escaped.replace("\n", "<br>\n") if line_breaks else escaped
    _check_cancel(cancel, "HTML 格式化已取消")


def _iter_html_chunks(session: SessionInfo, events: Iterable[dict[str, Any]], mode: str, mapping: dict[str, str] | None = None, cancel=None) -> Iterable[str]:
    def esc(value: Any) -> str:
        return html.escape(str(value), quote=True)

    title = esc(session.title)
    platform = {"codex": "Codex", "chatgpt": "ChatGPT 网页", "unknown": "来源未知"}.get(session.platform, "来源未知")
    meta = " · ".join([f"类别 {esc(platform)}", f"ID {esc(session.session_id)}", f"创建 {esc(session.created_at or '未找到')}", f"更新 {esc(session.updated_at or '未找到')}", f"工作目录 {esc(session.cwd or '未找到')}", "已归档" if session.archived else "未归档"])
    yield """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><style>
body{{font:15px/1.65 system-ui,"Microsoft YaHei",sans-serif;max-width:1000px;margin:28px auto;padding:0 18px;color:#202124;background:#fafafa}}
h1{{font-size:24px;margin:0 0 8px}}.meta{{color:#59636d;border-bottom:1px solid #d9dde1;padding:0 0 18px;margin-bottom:20px;overflow-wrap:anywhere}}
article,details,section{{border:1px solid #d7dce0;border-radius:6px;background:white;margin:12px 0;padding:12px 15px;overflow-wrap:anywhere}}
article.user{{border-left:4px solid #168b78}}article.assistant{{border-left:4px solid #4868a8}}h2{{font-size:15px;margin:0 0 8px}}
pre{{white-space:pre-wrap;word-break:break-word;overflow:auto;max-height:65vh;background:#f3f5f6;padding:10px;border-radius:4px}}
summary{{cursor:pointer;font-weight:600}}a{{color:#176e60}}.attachments{{margin-bottom:0}}
</style></head><body><h1>{title}</h1><p class="meta">{meta}</p>""".format(title=title, meta=meta)
    first_event = True
    for event in _chat_events(events, mode):
        _check_cancel(cancel, "HTML 格式化已取消")
        if not first_event:
            yield "\n"
        first_event = False
        kind = event.get("kind")
        raw_text = _replace_attachment_references(str(event.get("text", "")), mapping or {}, cancel)
        if kind == "message":
            role = "用户" if event.get("role") == "user" else "助手"
            phase = f" · {esc(event.get('phase'))}" if event.get("phase") else ""
            attachments = []
            for item in event.get("attachments", []):
                _check_cancel(cancel, "HTML 格式化已取消")
                if item.get("path"):
                    rel = esc(quote(str(item["path"]), safe="/._-"))
                    attachments.append(f'<a href="{rel}">{esc(item.get("kind", "附件"))}</a>')
                else:
                    attachments.append("附件未找到")
            yield f'<article class="message {"user" if role == "用户" else "assistant"}"><h2>{role}{phase}</h2><div>'
            yield from _iter_html_escape(raw_text, cancel, line_breaks=True)
            if attachments:
                yield "<p class=\"attachments\">" + " · ".join(attachments) + "</p>"
            yield "</div></article>"
        elif kind in {"tool_call", "tool_result", "status", "unknown"}:
            label = {"tool_call": "工具调用", "tool_result": "工具结果", "status": "状态事件", "unknown": "未知记录"}[kind]
            heading = f"{label} · {event.get('name', '')}"
            call_id = event.get("call_id")
            if call_id:
                heading += f" · call_id={call_id}"
            yield f"<details><summary>{esc(heading)}</summary><pre>"
            yield from _iter_html_escape(raw_text, cancel, line_breaks=False)
            yield "</pre></details>"
    _check_cancel(cancel, "HTML 格式化已取消")
    if session.issues:
        yield "<section><h2>读取报告</h2><ul>"
        for issue in session.issues:
            _check_cancel(cancel, "HTML 格式化已取消")
            yield f"<li>{esc(issue)}</li>"
        yield "</ul></section>"
    _check_cancel(cancel, "HTML 格式化已取消")
    yield "</body></html>"


def _write_json_export(
    path: str,
    session: SessionInfo,
    events: list[dict[str, Any]],
    mode: str,
    redact: bool,
    mapping: dict[str, str],
    issues: list[str],
    counter: list[int],
    cancel,
) -> None:
    """Write JSON one event at a time to avoid building a second full event tree."""
    destination = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".partial", dir=str(destination.parent))

    def write_value(stream, value: Any, indent: int = 0, *, line_start: bool = True) -> None:
        _check_cancel(cancel, "JSON 导出已取消")
        if line_start:
            stream.write(" " * indent)

        def write_string(text: str) -> None:
            stream.write('"')
            for offset in range(0, len(text), _FORMAT_CHUNK_SIZE):
                _check_cancel(cancel, "JSON 导出已取消")
                # Encoding each bounded slice independently is safe because JSON
                # escapes are local to a character; joining the escaped slices
                # produces the same string token as json.dumps(text).
                escaped = json.dumps(text[offset:offset + _FORMAT_CHUNK_SIZE], ensure_ascii=False)
                stream.write(escaped[1:-1])
            _check_cancel(cancel, "JSON 导出已取消")
            stream.write('"')

        def write_key(key: Any) -> None:
            if isinstance(key, str):
                write_string(key)
            elif key is None:
                write_string("null")
            elif key is True:
                write_string("true")
            elif key is False:
                write_string("false")
            elif isinstance(key, int):
                write_string(str(key))
            elif isinstance(key, float):
                write_string(json.dumps(key, ensure_ascii=False))
            else:
                raise TypeError(f"键类型 {type(key).__name__} 不能转换为 JSON")

        if isinstance(value, str):
            write_string(value)
        elif isinstance(value, dict):
            stream.write("{")
            if value:
                for index, (key, child) in enumerate(value.items()):
                    _check_cancel(cancel, "JSON 导出已取消")
                    stream.write("\n" if index == 0 else ",\n")
                    stream.write(" " * (indent + 2))
                    write_key(key)
                    stream.write(": ")
                    write_value(stream, child, indent + 2, line_start=False)
                stream.write("\n" + " " * indent)
            stream.write("}")
        elif isinstance(value, (list, tuple)):
            stream.write("[")
            for index, child in enumerate(value):
                _check_cancel(cancel, "JSON 导出已取消")
                stream.write("\n" if index == 0 else ",\n")
                write_value(stream, child, indent + 2, line_start=True)
            if value:
                stream.write("\n" + " " * indent)
            stream.write("]")
        else:
            stream.write(json.dumps(value, ensure_ascii=False))

    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write("{\n  \"tool_version\": ")
            stream.write(json.dumps(APP_VERSION, ensure_ascii=False))
            stream.write(",\n  \"session\":\n")
            write_value(stream, _sanitize(session.public_dict(), redact, counter, cancel), 2)
            stream.write(",\n  \"reading_mode\": ")
            stream.write(json.dumps(mode, ensure_ascii=False))
            stream.write(",\n  \"events\": [")
            first = True
            for event in _chat_events(events, mode):
                if cancel.is_set():
                    raise InterruptedError("JSON 导出已取消")
                selected = _map_attachments_in_event(event, mapping, cancel)
                safe_event = _sanitize(selected, redact, counter, cancel)
                stream.write("\n" if first else ",\n")
                first = False
                write_value(stream, safe_event, 4)
            if not first:
                stream.write("\n  ")
            stream.write("],\n  \"read_issues\":\n")
            write_value(stream, _sanitize(issues, redact, counter, cancel), 2)
            stream.write(",\n  \"attachment_files\":\n")
            write_value(stream, _sanitize(sorted(set(mapping.values())), redact, counter, cancel), 2)
            stream.write("\n}\n")
            stream.flush()
            os.fsync(stream.fileno())
        if cancel.is_set():
            raise InterruptedError("JSON 导出已取消")
        os.replace(temporary, destination)
    except Exception:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise


def _write_text_export(path: str, text: str | Iterable[str], cancel) -> None:
    destination = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".partial", dir=str(destination.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            pieces = (text,) if isinstance(text, str) else text
            for piece in pieces:
                _check_cancel(cancel, "文本写入已取消")
                if not isinstance(piece, str):
                    raise TypeError(f"文本块必须是字符串，实际为 {type(piece).__name__}")
                for offset in range(0, len(piece), _FORMAT_CHUNK_SIZE):
                    _check_cancel(cancel, "文本写入已取消")
                    stream.write(piece[offset:offset + _FORMAT_CHUNK_SIZE])
            stream.flush()
            os.fsync(stream.fileno())
        _check_cancel(cancel, "文本写入已取消")
        os.replace(temporary, destination)
    except Exception:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise


def _hash_file(path: str, cancel=None) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as stream:
        while True:
            if cancel is not None and cancel.is_set():
                raise InterruptedError("哈希计算已取消")
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _copy_file_with_cancel(source: str, destination: str, cancel) -> dict[str, Any]:
    try:
        with open(source, "rb") as src, open(destination, "wb") as dst:
            before = os.fstat(src.fileno())
            copied_bytes = 0
            while True:
                if cancel.is_set():
                    raise InterruptedError("导出已取消")
                block = src.read(1024 * 1024)
                if not block:
                    break
                dst.write(block)
                copied_bytes += len(block)
            after = os.fstat(src.fileno())
            try:
                path_after = os.stat(source, follow_symlinks=False)
            except OSError:
                path_after = None
            before_identity = (before.st_dev, before.st_ino)
            after_identity = (after.st_dev, after.st_ino)
            path_identity = (path_after.st_dev, path_after.st_ino) if path_after else None
            changed = (
                before_identity != after_identity
                or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or copied_bytes != before.st_size
                or path_identity != before_identity
            )
            return {
                "copied_bytes": copied_bytes,
                "source_size_before": before.st_size,
                "source_size_after": after.st_size,
                "source_changed_during_copy": changed,
            }
    except Exception:
        try:
            os.remove(destination)
        except OSError:
            pass
        raise


def _validate_zip_archive(path: str, cancel=None) -> None:
    """Read every member to EOF so zipfile checks CRCs before publication."""
    with zipfile.ZipFile(path, "r") as archive:
        for member in archive.infolist():
            _check_cancel(cancel, "ZIP 校验已取消")
            with archive.open(member, "r") as stream:
                while True:
                    _check_cancel(cancel, "ZIP 校验已取消")
                    if not stream.read(1024 * 1024):
                        break
        _check_cancel(cancel, "ZIP 校验已取消")


def _write_zip_entry(archive: zipfile.ZipFile, path: str, arcname: str, cancel) -> None:
    info = zipfile.ZipInfo.from_file(path, arcname)
    info.compress_type = zipfile.ZIP_DEFLATED
    with archive.open(info, "w") as output, open(path, "rb") as source:
        while True:
            if cancel.is_set():
                raise InterruptedError("ZIP 打包已取消")
            block = source.read(1024 * 1024)
            if not block:
                break
            output.write(block)


def _unique_file(path: str) -> str:
    if not os.path.lexists(path):
        return path
    root, ext = os.path.splitext(path)
    index = 2
    while os.path.lexists(f"{root}_{index}{ext}"):
        index += 1
    return f"{root}_{index}{ext}"


def _publish_unique_file(source: str, desired_path: str) -> str:
    """Publish a completed same-directory temp file without replacing any existing path."""
    root, ext = os.path.splitext(desired_path)
    index = 1
    while True:
        destination = desired_path if index == 1 else f"{root}_{index}{ext}"
        try:
            # Linking is an atomic create-if-absent operation on the same volume.
            os.link(source, destination)
        except FileExistsError:
            index += 1
            continue
        except OSError:
            # Windows rename also fails when the destination already exists.
            # The temporary file is created in the same directory, so the move is atomic.
            if os.name != "nt":
                raise
            try:
                os.rename(source, destination)
            except FileExistsError:
                index += 1
                continue
            return destination
        try:
            os.remove(source)
        except OSError:
            try:
                os.remove(destination)
            except OSError:
                pass
            raise
        return destination


def _write_unique_json_file(directory: str, filename: str, value: Any) -> str:
    """Write JSON privately, then publish it without replacing a concurrent export."""
    fd, temporary = tempfile.mkstemp(prefix=f".{filename}.", suffix=".partial", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2))
            stream.flush()
            os.fsync(stream.fileno())
        return _publish_unique_file(temporary, os.path.join(directory, filename))
    except Exception:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise


def export_one(
    session: SessionInfo,
    catalog: Catalog,
    output_root: str,
    formats: list[str],
    mode: str = "full",
    redact: bool = False,
    include_raw: bool = False,
    cancel=None,
    progress: Callable[[str, int], None] | None = None,
) -> dict[str, Any]:
    import threading

    cancel = cancel or threading.Event()
    output_root = _validate_output_root(catalog, output_root)
    os.makedirs(output_root, exist_ok=True)
    redaction_counter = [0]
    if redact:
        try:
            safe_title, title_redaction_failed = _safe_redacted_text(session.title, cancel, "已脱敏会话", redaction_counter)
        except InterruptedError:
            safe_title, title_redaction_failed = "已脱敏会话", True
        try:
            safe_session_id, session_id_redaction_failed = _safe_redacted_text(session.session_id, cancel, "已脱敏ID")
        except InterruptedError:
            safe_session_id, session_id_redaction_failed = "已脱敏ID", True
    else:
        safe_title, safe_session_id = session.title, session.session_id
        title_redaction_failed = session_id_redaction_failed = False
    safe_session = replace(session, title=safe_title, session_id=safe_session_id)
    name = safe_name(safe_title) + "__" + safe_name(safe_session_id, 50)
    folder = unique_directory(output_root, name)
    marker = os.path.join(folder, "未完成.txt")
    try:
        Path(marker).write_text("导出尚未完成。请检查导出报告；此目录可能只包含部分文件。\n", encoding="utf-8")
    except Exception:
        # Do not leave an unmarked, empty session directory if the initial
        # failure marker itself cannot be written.
        try:
            os.remove(marker)
        except OSError:
            pass
        try:
            os.rmdir(folder)
        except OSError:
            pass
        raise
    issues = list(session.issues)
    if title_redaction_failed or session_id_redaction_failed:
        issues.append("会话标题或 ID 无法安全脱敏，已使用安全占位名。")

    generated_files: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    record_counts: dict[str, int] = {}
    cancel_stage = "初始化"

    def cancelled_result() -> dict[str, Any]:
        safe_issues = ["导出已取消；为保证脱敏安全，其他报告项已省略。"] if redact else issues
        try:
            Path(marker).write_text("导出已取消，目录内容可能不完整。\n" + "\n".join(safe_issues), encoding="utf-8")
        except OSError:
            pass

        known_files = {
            str(item.get("path")): item
            for item in generated_files
            if isinstance(item, dict) and isinstance(item.get("path"), str)
        }
        partial_files: list[dict[str, Any]] = []
        inventory_complete = True

        def note_inventory_error(_error: OSError) -> None:
            nonlocal inventory_complete
            inventory_complete = False

        try:
            for base, directories, filenames in os.walk(folder, onerror=note_inventory_error):
                directories.sort(key=str.casefold)
                filenames.sort(key=str.casefold)
                for filename in filenames:
                    path = os.path.join(base, filename)
                    if path in {marker, os.path.join(folder, "manifest.json")} or filename.endswith(".partial"):
                        continue
                    if len(partial_files) >= 10_000:
                        inventory_complete = False
                        break
                    relative = os.path.relpath(path, folder).replace(os.sep, "/")
                    try:
                        size = os.path.getsize(path)
                    except OSError:
                        inventory_complete = False
                        continue
                    existing = known_files.get(relative)
                    if existing and existing.get("size_bytes") == size and isinstance(existing.get("sha256"), str):
                        partial_files.append(dict(existing))
                    else:
                        partial_files.append({
                            "path": relative,
                            "sha256": None,
                            "size_bytes": size,
                            "hash_status": "因导出取消未计算",
                        })
                if not inventory_complete:
                    break
        except OSError:
            inventory_complete = False

        cancel_manifest = {
            "tool_version": APP_VERSION,
            "exported_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "session_id": safe_session_id,
            "title": safe_title,
            "platform": session.platform,
            "source_paths": [] if redact else list(session.paths),
            "reading_mode": mode,
            "sensitive_content_redacted": bool(redact),
            "status": "已取消",
            "complete": False,
            "cancelled_at_stage": cancel_stage,
            "file_inventory_complete": inventory_complete,
            "raw_source_included_without_redaction": raw_copied_count > 0,
            "record_counts": {
                "user_messages": session.user_count,
                "assistant_messages": session.assistant_count,
                "tool_calls": session.tool_count,
                "normalized_events": len(events),
                "source_types": dict(record_counts or session.type_counts),
                "duplicate_records_skipped_in_reading_view": session.duplicate_count,
            },
            "files": partial_files,
            "missing_content_and_errors": safe_issues,
            "manifest_hash_note": "为避免自引用，文件清单中的 SHA-256 不包含本 manifest。未计算的哈希为 null。",
        }
        manifest_path = os.path.join(folder, "manifest.json")
        temporary_manifest = ""
        try:
            fd, temporary_manifest = tempfile.mkstemp(prefix=".manifest.", suffix=".partial", dir=folder)
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                json.dump(cancel_manifest, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_manifest, manifest_path)
            temporary_manifest = ""
        except OSError:
            pass
        finally:
            if temporary_manifest:
                try:
                    os.remove(temporary_manifest)
                except OSError:
                    pass
        return {"session_id": safe_session_id, "title": safe_title, "platform": session.platform, "folder": folder, "zip": "", "status": "已取消", "issues": safe_issues, "files": partial_files}

    zip_path = ""
    partial_zip_path = ""
    zip_created = False
    raw_copied_count = 0
    try:
        selected_formats = {str(x).casefold() for x in formats}
        if "zip" in selected_formats:
            selected_formats.add("json")
        if progress:
            progress(f"读取会话：{safe_title[:60]}", -1)
        cancel_stage = "读取会话记录"
        events, load_issues, record_counts = load_session_events(
            session, catalog, cancel, include_raw_data=("json" in selected_formats)
        )
        issues.extend(x for x in load_issues if x not in issues)
        if cancel.is_set():
            return cancelled_result()

        cancel_stage = "复制关联附件"
        mapping = _copy_related_attachments(session, catalog, folder, events, cancel, issues, redact, redaction_counter)
        if not selected_formats.intersection({"txt", "md", "html", "json"}) and "zip" in selected_formats:
            selected_formats.add("json")
        if selected_formats.intersection({"txt", "md", "html"}):
            safe_events = []
            for event in _chat_events(events, mode):
                if cancel.is_set():
                    raise InterruptedError("导出已取消")
                mapped = _map_text_event(event, mapping, cancel)
                safe_events.append(_sanitize(mapped, redact, redaction_counter, cancel))
            render_session = _render_session(session, redact, redaction_counter, cancel, safe_title, safe_session_id)
        else:
            safe_events = []
            render_session = safe_session
        if "txt" in selected_formats:
            cancel_stage = "生成 TXT"
            _write_text_export(
                str(Path(folder, "聊天记录.txt")),
                # safe_events already has text and attachment references mapped
                # by _map_text_event; do not rescan the full attachment mapping
                # once per event while rendering the readable formats.
                _iter_txt_chunks(render_session, safe_events, mode, None, cancel),
                cancel,
            )
        if "md" in selected_formats:
            cancel_stage = "生成 Markdown"
            _write_text_export(
                str(Path(folder, "聊天记录.md")),
                _iter_markdown_chunks(render_session, safe_events, mode, None, cancel),
                cancel,
            )
        if "html" in selected_formats:
            cancel_stage = "生成 HTML"
            _write_text_export(
                str(Path(folder, "聊天记录.html")),
                _iter_html_chunks(render_session, safe_events, mode, None, cancel),
                cancel,
            )
        if "json" in selected_formats:
            cancel_stage = "生成 JSON"
            _write_json_export(
                str(Path(folder, "聊天记录.json")), safe_session, events, mode, redact, mapping, issues,
                redaction_counter, cancel,
            )
        if include_raw:
            cancel_stage = "复制原始记录"
            raw_root = Path(folder) / "原始文件"
            raw_root.mkdir(parents=True, exist_ok=True)
            rejected_sources = 0
            for source in session.paths:
                if cancel.is_set():
                    break
                if " :: " in source:
                    continue
                if not _is_raw_rollout_source(source, catalog.root, session.session_id):
                    rejected_sources += 1
                    continue
                if not os.path.isfile(source):
                    issues.append(f"原始 rollout 来源不存在或无法访问：{Path(source).name}")
                    continue
                if _has_link_or_junction_component(source, catalog.root):
                    issues.append(f"原始 rollout 来源包含符号链接或 junction，已跳过：{Path(source).name}")
                    continue
                destination = _unique_file(str(raw_root / Path(source).name))
                try:
                    copy_info = _copy_file_with_cancel(source, destination, cancel)
                except InterruptedError:
                    raise
                except OSError as exc:
                    issues.append(f"原始 rollout 读取失败，已继续其他来源：{Path(source).name}（{type(exc).__name__}）")
                    continue
                raw_copied_count += 1
                if copy_info["source_changed_during_copy"]:
                    source_label = "某个原始来源" if redact else Path(source).name
                    issues.append(
                        f"{source_label} 在复制期间发生变化：复制 {copy_info['copied_bytes']} 字节，"
                        f"打开时大小 {copy_info['source_size_before']} 字节，结束时大小 {copy_info['source_size_after']} 字节；副本已保留，但可能不是稳定快照。"
                    )
            if raw_copied_count == 0:
                issues.append("原始 rollout 文件未找到；该会话没有可复制的原始 JSONL")
            if rejected_sources:
                issues.append(f"忽略非 rollout 文件或会话 ID 不匹配的原始来源，共 {rejected_sources} 个")
        if cancel.is_set():
            return cancelled_result()

        cancel_stage = "计算文件清单哈希"
        for base, _, filenames in os.walk(folder, onerror=_raise_walk_error):
            if cancel.is_set():
                return cancelled_result()
            for filename in filenames:
                if cancel.is_set():
                    return cancelled_result()
                path = os.path.join(base, filename)
                if path == marker or path == os.path.join(folder, "manifest.json"):
                    continue
                digest, size = _hash_file(path, cancel)
                generated_files.append({"path": os.path.relpath(path, folder).replace(os.sep, "/"), "sha256": digest, "size_bytes": size})
        generated_files.sort(key=lambda item: item["path"].casefold())
        safe_source_paths = _safe_report_sanitize(session.paths, redact, cancel, redaction_counter)
        safe_issues = _safe_report_sanitize(issues, redact, cancel, redaction_counter)
        manifest = {
            "tool_version": APP_VERSION,
            "exported_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "session_id": safe_session_id,
            "title": safe_title,
            "platform": session.platform,
            "source_paths": safe_source_paths,
            "source": session.source,
            "reading_mode": mode,
            "sensitive_content_redacted": bool(redact),
            "status": "完成",
            "complete": True,
            "redacted_occurrences": redaction_counter[0],
            "raw_source_included_without_redaction": raw_copied_count > 0,
            "record_counts": {
                "user_messages": session.user_count,
                "assistant_messages": session.assistant_count,
                "tool_calls": session.tool_count,
                "normalized_events": len(events),
                "source_types": dict(record_counts or session.type_counts),
                "duplicate_records_skipped_in_reading_view": session.duplicate_count,
            },
            "files": generated_files,
            "missing_content_and_errors": safe_issues,
            "manifest_hash_note": "为避免自引用，文件清单中的 SHA-256 不包含本 manifest。",
        }
        _write_text_export(str(Path(folder, "manifest.json")), json.dumps(manifest, ensure_ascii=False, indent=2), cancel)
        zip_file = None
        if "zip" in selected_formats:
            cancel_stage = "打包 ZIP"
            zip_path = os.path.join(output_root, name + ".zip")
            zip_fd, partial_zip_path = tempfile.mkstemp(prefix=f".{name}.", suffix=".partial", dir=output_root)
            with os.fdopen(zip_fd, "w+b") as zip_stream:
                with zipfile.ZipFile(zip_stream, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                    for base, directories, filenames in os.walk(folder, onerror=_raise_walk_error):
                        directories.sort(key=str.casefold)
                        filenames.sort(key=str.casefold)
                        if cancel.is_set():
                            raise InterruptedError("ZIP 打包已取消")
                        for filename in filenames:
                            if cancel.is_set():
                                raise InterruptedError("ZIP 打包已取消")
                            path = os.path.join(base, filename)
                            if path == marker:
                                continue
                            arcname = os.path.join(os.path.basename(folder), os.path.relpath(path, folder))
                            _write_zip_entry(archive, path, arcname.replace(os.sep, "/"), cancel)
            cancel_stage = "校验 ZIP CRC"
            _validate_zip_archive(partial_zip_path, cancel)
            cancel_stage = "计算 ZIP 哈希"
            zip_hash, zip_size = _hash_file(partial_zip_path, cancel)
            if cancel.is_set():
                raise InterruptedError("ZIP 打包已取消")
            zip_path = _publish_unique_file(partial_zip_path, zip_path)
            zip_created = True
            partial_zip_path = ""
            zip_file = {"path": zip_path, "sha256": zip_hash, "size_bytes": zip_size}
        if cancel.is_set():
            raise InterruptedError("导出已取消")
        os.remove(marker)
        if progress:
            progress(f"完成：{safe_title[:60]}", -1)
        result = {"session_id": safe_session_id, "title": safe_title, "platform": session.platform, "folder": folder, "zip": zip_path, "zip_file": zip_file, "status": "完成" if not issues else "完成（有报告项）", "issues": safe_issues, "files": generated_files}
        return result
    except InterruptedError:
        for incomplete_path in (zip_path if zip_created else "", partial_zip_path):
            try:
                if incomplete_path:
                    os.remove(incomplete_path)
            except OSError:
                pass
        return cancelled_result()
    except Exception as exc:
        for incomplete_path in (zip_path if zip_created else "", partial_zip_path):
            try:
                if incomplete_path:
                    os.remove(incomplete_path)
            except OSError:
                pass
        issues.append(f"导出失败：{type(exc).__name__}: {exc}")
        safe_issues = _safe_report_sanitize(issues, redact, None)
        try:
            Path(marker).write_text("导出失败或未完成。\n" + "\n".join(safe_issues), encoding="utf-8")
        except OSError:
            pass
        manifest_path = Path(folder) / "manifest.json"
        if manifest_path.is_file():
            temporary_manifest = ""
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if isinstance(manifest, dict):
                    manifest["status"] = "失败"
                    manifest["complete"] = False
                    manifest["failure_stage"] = cancel_stage
                    manifest["missing_content_and_errors"] = safe_issues
                    fd, temporary_manifest = tempfile.mkstemp(prefix=".manifest.", suffix=".partial", dir=folder)
                    with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                        json.dump(manifest, stream, ensure_ascii=False, indent=2)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary_manifest, manifest_path)
                    temporary_manifest = ""
            except (OSError, ValueError):
                pass
            finally:
                if temporary_manifest:
                    try:
                        os.remove(temporary_manifest)
                    except OSError:
                        pass
        return {"session_id": safe_session_id, "title": safe_title, "platform": session.platform, "folder": folder, "zip": "", "status": "失败", "issues": safe_issues, "files": generated_files}


def export_batch(
    sessions: list[SessionInfo],
    catalog: Catalog,
    output_root: str,
    formats: list[str],
    mode: str,
    redact: bool,
    include_raw: bool,
    cancel,
    progress: Callable[[str, int], None] | None = None,
    result_callback: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    output_root = _validate_output_root(catalog, output_root)
    os.makedirs(output_root, exist_ok=True)
    def index_progress(message: str, _pct: int) -> None:
        if progress:
            progress(message, 0)

    snapshot_index_errors = _prepare_batch_snapshot_indexes(sessions, catalog, cancel, index_progress)
    results = []
    total = len(sessions)
    for index, session in enumerate(sessions, 1):
        if cancel.is_set():
            break

        def one_progress(message: str, _pct: int) -> None:
            if progress:
                progress(f"{index}/{total}：{message}", int((index - 1) * 100 / max(total, 1)))

        try:
            export_session = session
            session_issues = list(session.issues)
            for source_path, issue in snapshot_index_errors.items():
                if any(path.startswith(source_path + " :: ") for path in session.paths) and issue not in session_issues:
                    session_issues.append(issue)
            if session_issues != session.issues:
                export_session = replace(session, issues=session_issues)
            result = export_one(export_session, catalog, output_root, formats, mode, redact, include_raw, cancel, one_progress)
        except Exception as exc:
            title = session.title
            issue = f"导出失败：{type(exc).__name__}: {exc}"
            if redact:
                title = _safe_redacted_text(title, None, "已脱敏会话")[0]
                issue = _safe_redacted_text(issue, None, "导出失败：错误信息无法安全脱敏")[0]
            result = {
                "session_id": _safe_redacted_text(session.session_id, None, "已脱敏ID")[0] if redact else session.session_id,
                "title": title,
                "platform": session.platform,
                "folder": "",
                "zip": "",
                "status": "失败",
                "issues": [issue],
                "files": [],
            }
        results.append(result)
        if result_callback:
            result_callback(result)
        if progress:
            progress(f"已处理 {index}/{total} 个会话", int(index * 100 / max(total, 1)))
    try:
        safe_results = [_safe_batch_result(result, redact, cancel) for result in results]
    except InterruptedError:
        safe_results = [_safe_batch_result(result, redact, None) for result in results]
    not_started = []
    for session in sessions[len(results):]:
        if redact:
            report_cancel = None if cancel.is_set() else cancel
            try:
                safe_id = _safe_redacted_text(session.session_id, report_cancel, "已脱敏ID")[0]
                safe_title = _safe_redacted_text(session.title, report_cancel, "已脱敏会话")[0]
            except InterruptedError:
                safe_id = _safe_redacted_text(session.session_id, None, "已脱敏ID")[0]
                safe_title = _safe_redacted_text(session.title, None, "已脱敏会话")[0]
        else:
            safe_id, safe_title = session.session_id, session.title
        not_started.append({"session_id": safe_id, "title": safe_title, "platform": session.platform, "status": "未开始"})
    report = {
        "tool_version": APP_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "cancelled": cancel.is_set(),
        "requested_sessions": len(sessions),
        "processed_sessions": len(results),
        "results": safe_results,
        "not_started_sessions": not_started,
    }
    try:
        _write_unique_json_file(output_root, "导出报告.json", report)
    except OSError as exc:
        raise RuntimeError(
            f"批量报告写入失败（{type(exc).__name__}）；已处理 {len(results)} 个会话，"
            "请检查已生成会话目录中的 manifest.json。"
        ) from exc
    return results
