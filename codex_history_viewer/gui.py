from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import traceback
from datetime import date
from pathlib import Path
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QDate, QEvent, QModelIndex, QSettings, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QAbstractItemView,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStyledItemDelegate,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from .exporter import export_batch
from .importer import import_export_package
from .reader import APP_VERSION, Catalog, SessionInfo, _local_display, load_session_events, scan_codex_home

PREVIEW_CHARACTER_LIMIT = 1_000_000
PREVIEW_TEXT_CHARACTER_LIMIT = PREVIEW_CHARACTER_LIMIT - 16_384
PREVIEW_RECORD_LIMIT = 20_000


def _platform_label(platform: str) -> str:
    return {"codex": "Codex", "chatgpt": "ChatGPT 网页", "unknown": "来源未知"}.get(platform, "来源未知")


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TiB"


def _filter_sessions(
    sessions: list[SessionInfo],
    needle: str,
    archive_mode: str,
    use_date: bool,
    date_from: str,
    date_to: str,
    cancel_event: threading.Event,
    platform_mode: str = "all",
) -> list[SessionInfo] | None:
    """Filter an immutable catalog snapshot without rebuilding long search text."""
    needle = needle.casefold()
    result: list[SessionInfo] = []
    try:
        lower_day = date.fromisoformat(date_from) if use_date else None
        upper_day = date.fromisoformat(date_to) if use_date else None
    except ValueError:
        return result
    for session in sessions:
        if cancel_event.is_set():
            return None
        if platform_mode != "all" and session.platform != platform_mode:
            continue
        if archive_mode == "active" and session.archived or archive_mode == "archived" and not session.archived:
            continue
        if use_date:
            if not session.created_at:
                continue
            try:
                session_day = date.fromisoformat(session.created_at[:10])
                if not (lower_day <= session_day <= upper_day):
                    continue
            except ValueError:
                continue
        if needle:
            if (
                needle not in session.title.casefold()
                and needle not in session.session_id.casefold()
                and needle not in session.cwd.casefold()
                and needle not in session.search_text
            ):
                continue
        result.append(session)
    return None if cancel_event.is_set() else result


def _root_key(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def _clear_selection_for_root_change(old_catalog: Catalog | None, new_catalog: Catalog, selected_ids: set[str]) -> bool:
    if old_catalog and _root_key(old_catalog.root) != _root_key(new_catalog.root):
        selected_ids.clear()
        return True
    return False


def _prune_selection_for_catalog(catalog: Catalog, selected_ids: set[str]) -> bool:
    """Drop IDs that disappeared during a refresh of the same Codex root."""
    before = set(selected_ids)
    selected_ids.intersection_update(session.session_id for session in catalog.sessions)
    return selected_ids != before


def _preview_target(
    visible_sessions: list[SessionInfo],
    catalog_sessions: list[SessionInfo],
    selected_ids: set[str],
    current_row: int,
) -> SessionInfo | None:
    # Preview follows the focused row. Falling back to a different checked row
    # made the preview appear to ignore the user's click.
    if 0 <= current_row < len(visible_sessions):
        return visible_sessions[current_row]
    selected = [session for session in catalog_sessions if session.session_id in selected_ids]
    return selected[0] if len(selected) == 1 else None


def _export_scope_sessions(
    catalog_sessions: list[SessionInfo],
    visible_sessions: list[SessionInfo],
    selected_ids: set[str],
    visible_only: bool,
) -> list[SessionInfo]:
    """Resolve an export scope without leaking checked rows outside current filters."""
    if visible_only:
        return list(visible_sessions)
    return [session for session in catalog_sessions if session.session_id in selected_ids]


class FilterWorker(QThread):
    completed = Signal(int, object)

    def __init__(
        self,
        generation: int,
        sessions: list[SessionInfo],
        needle: str,
        archive_mode: str,
        use_date: bool,
        date_from: str,
        date_to: str,
        platform_mode: str = "all",
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.generation = generation
        self.sessions = sessions
        self.needle = needle
        self.archive_mode = archive_mode
        self.use_date = use_date
        self.date_from = date_from
        self.date_to = date_to
        self.platform_mode = platform_mode
        self.cancel_event = threading.Event()

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        result = _filter_sessions(
            self.sessions,
            self.needle,
            self.archive_mode,
            self.use_date,
            self.date_from,
            self.date_to,
            self.cancel_event,
            self.platform_mode,
        )
        if result is not None:
            self.completed.emit(self.generation, result)


class SessionTableModel(QAbstractTableModel):
    """A lightweight, on-demand table adapter for the session catalog."""

    selection_changed = Signal()

    def __init__(self, selected_ids: set[str], parent: QWidget | None = None):
        super().__init__(parent)
        self.selected_ids = selected_ids
        self.sessions: list[SessionInfo] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.sessions)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(MainWindow.COLUMNS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            if 0 <= section < len(MainWindow.COLUMNS):
                return MainWindow.COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not (0 <= index.row() < len(self.sessions)):
            return None
        session = self.sessions[index.row()]
        column = index.column()
        if column == 0:
            if role == Qt.ItemDataRole.CheckStateRole:
                return Qt.CheckState.Checked if session.session_id in self.selected_ids else Qt.CheckState.Unchecked
            if role == Qt.ItemDataRole.TextAlignmentRole:
                return Qt.AlignmentFlag.AlignCenter
            return None
        value = self._value(session, column)
        if role == Qt.ItemDataRole.ToolTipRole and column == 5:
            return f"会话类别：{_platform_label(session.platform)}\n数据来源：{session.source or '未知'}\n读取状态：{session.status or '未知'}"
        if role == Qt.ItemDataRole.ToolTipRole and column == 3:
            return str(session.created_at or "未找到")
        if role == Qt.ItemDataRole.ToolTipRole and column == 4:
            return str(session.updated_at or "未找到")
        if role == Qt.ItemDataRole.ToolTipRole and column == 11:
            if session.disk_size_bytes is None:
                if session.disk_size_status == "共享数据库 / 索引":
                    reason = "SQLite、session_index.jsonl 等共享文件无法准确拆分到单条会话，因此不计入会话文件大小。"
                elif session.disk_size_status == "未能完整统计":
                    reason = "本地文件统计中断或遇到读取问题，无法确认完整大小；请查看读取报告。"
                elif session.disk_size_status == "未找到本地记录文件":
                    reason = "没有找到可归属到该会话的本地 rollout 或附件文件。"
                else:
                    reason = "未能确认该会话的本地文件大小，请查看读取报告。"
                return (
                    f"大小状态：{session.disk_size_status}\n"
                    + reason
                )
            details = "仅统计该会话 rollout 文件和明确关联附件的逻辑文件大小；不含共享数据库或索引。"
            details += "同一附件被多个会话引用时，当前结果合计会按会话重复计入；数值不是磁盘分配簇占用。"
            if session.disk_size_status == "部分统计":
                details += "部分目录项未能完成统计，显示值是已扫描部分的下限。"
            return f"已统计：{_format_bytes(session.disk_size_bytes)}（{session.disk_size_bytes:,} 字节）\n{details}"
        if role == Qt.ItemDataRole.ToolTipRole and column in {2, 3, 4, 6}:
            return str(value)
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return value
        if role == Qt.ItemDataRole.TextAlignmentRole and column in {7, 8, 9, 10, 11}:
            return Qt.AlignmentFlag.AlignCenter
        return None

    @staticmethod
    def _value(session: SessionInfo, column: int) -> str:
        if column == 1:
            value = session.title
        elif column == 2:
            value = session.session_id
        elif column == 3:
            value = str(session.created_at or "")[:16]
        elif column == 4:
            value = str(session.updated_at or "")[:16]
        elif column == 5:
            full_source = session.source or "来源未知"
            source_lower = full_source.casefold()
            categories = [
                label
                for label, token in (("rollout", "rollout"), ("SQLite", "sqlite"), ("索引", "session_index"), ("历史索引", "history.jsonl"))
                if token in source_lower
            ]
            source = " + ".join(categories[:2]) if categories else full_source[:14]
            if len(categories) > 2:
                source += f" +{len(categories) - 2}"
            status = session.status or "状态未知"
            value = f"{_platform_label(session.platform)} · {source} · {status}"
        elif column == 6:
            value = session.cwd
        elif column == 7:
            value = "是" if session.archived else "否"
        elif column == 8:
            value = session.user_count
        elif column == 9:
            value = session.assistant_count
        elif column == 10:
            value = session.tool_count
        elif column == 11:
            if session.disk_size_bytes is None:
                value = session.disk_size_status
            else:
                prefix = "约 " if session.disk_size_status == "部分统计" else ""
                value = prefix + _format_bytes(session.disk_size_bytes)
        else:
            value = ""
        return "" if value is None else str(value)

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.column() == 0:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def setData(self, index: QModelIndex, value: Any, role: int = Qt.ItemDataRole.EditRole) -> bool:
        if not index.isValid() or index.column() != 0 or role != Qt.ItemDataRole.CheckStateRole:
            return False
        session_id = self.sessions[index.row()].session_id
        checked = value == Qt.CheckState.Checked or value == Qt.CheckState.Checked.value
        previously_checked = session_id in self.selected_ids
        if checked == previously_checked:
            return True
        if checked:
            self.selected_ids.add(session_id)
        else:
            self.selected_ids.discard(session_id)
        self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
        self.selection_changed.emit()
        return True

    def set_sessions(self, sessions: list[SessionInfo]) -> None:
        self.beginResetModel()
        self.sessions = sessions
        self.endResetModel()

    def refresh_checks(self) -> None:
        if self.sessions:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(len(self.sessions) - 1, 0),
                [Qt.ItemDataRole.CheckStateRole],
            )


class SessionCheckDelegate(QStyledItemDelegate):
    """Make the whole checkbox cell toggle reliably, including on narrow DPI-scaled targets."""

    def editorEvent(self, event, model, option, index) -> bool:
        if index.isValid() and index.column() == 0:
            if event.type() == QEvent.Type.MouseButtonDblClick and event.button() == Qt.MouseButton.LeftButton:
                # A double-click is two clicks; the first press already toggled.
                return True
            if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                if option.rect.contains(event.position().toPoint()):
                    self._toggle(model, index)
                return True
            if event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
                return True
            key_toggle = event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Space
            if key_toggle:
                self._toggle(model, index)
                return True
            if event.type() == QEvent.Type.KeyRelease and event.key() == Qt.Key.Key_Space:
                return True
        return super().editorEvent(event, model, option, index)

    @staticmethod
    def _toggle(model, index) -> None:
        current = model.data(index, Qt.ItemDataRole.CheckStateRole)
        new_state = Qt.CheckState.Unchecked if current == Qt.CheckState.Checked else Qt.CheckState.Checked
        model.setData(index, new_state, Qt.ItemDataRole.CheckStateRole)


def default_codex_home() -> str:
    return os.path.abspath(os.path.expandvars(os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")))


class ScanWorker(QThread):
    progress = Signal(str, int)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, root: str):
        super().__init__()
        self.root = root
        self.catalog: Catalog | None = None
        self.cancel_event = threading.Event()

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        try:
            self.catalog = scan_codex_home(self.root, self.cancel_event, lambda text, value: self.progress.emit(text, value))
            self.completed.emit(self.catalog)
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class ImportWorker(QThread):
    completed = Signal(str, object)
    progress = Signal(str, int)
    failed = Signal(str)

    def __init__(self, source: str, destination: str):
        super().__init__()
        self.source = source
        self.destination = destination
        self.cancel_event = threading.Event()

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        try:
            root, report = import_export_package(
                self.source,
                self.destination,
                self.cancel_event,
                lambda text, value: self.progress.emit(text, value),
            )
            self.completed.emit(root, report)
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class ExportWorker(QThread):
    progress = Signal(str, int)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, sessions: list[SessionInfo], catalog: Catalog, options: dict[str, Any]):
        super().__init__()
        self.sessions = sessions
        self.catalog = catalog
        self.options = options
        self.cancel_event = threading.Event()

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        try:
            results = export_batch(
                self.sessions,
                self.catalog,
                self.options["output_root"],
                self.options["formats"],
                self.options["mode"],
                self.options["redact"],
                self.options["include_raw"],
                self.cancel_event,
                lambda text, value: self.progress.emit(text, value),
            )
            self.completed.emit(results)
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class PreviewWorker(QThread):
    completed = Signal(str, object)
    failed = Signal(str)

    def __init__(self, session: SessionInfo, catalog: Catalog):
        super().__init__()
        self.session = session
        self.catalog = catalog
        self.cancel_event = threading.Event()

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        try:
            events, issues, _ = load_session_events(
                self.session,
                self.catalog,
                self.cancel_event,
                include_raw_data=False,
                max_records=PREVIEW_RECORD_LIMIT,
                max_text_chars=PREVIEW_TEXT_CHARACTER_LIMIT,
            )
            lines: list[str] = []
            characters = 0
            truncated = False

            def append(*values: Any) -> bool:
                nonlocal characters, truncated
                for value in values:
                    if characters >= PREVIEW_CHARACTER_LIMIT:
                        truncated = True
                        return False
                    text = str(value)
                    remaining = PREVIEW_CHARACTER_LIMIT - characters
                    if len(text) > remaining:
                        lines.append(text[:remaining])
                        characters += remaining
                        truncated = True
                        return False
                    lines.append(text)
                    characters += len(text) + 1
                return True

            append(
                f"会话：{self.session.title}",
                f"会话 ID：{self.session.session_id}",
                f"来源类别：{_platform_label(self.session.platform)}",
                f"创建时间（本机）：{self.session.created_at or '未找到'}",
                f"创建时间（原始）：{self.session.raw_created_at or '未找到'}",
                f"工作目录：{self.session.cwd or '未找到'}",
                f"来源：{self.session.source or '未知'}",
                f"状态：{self.session.status}",
                "",
            )
            cancelled = False
            record_limited = any("预览已达到" in issue for issue in issues)
            text_limited = any("预览文本已达到" in issue for issue in issues)
            for event in events:
                if self.cancel_event.is_set():
                    cancelled = True
                    break
                kind = event.get("kind")
                timestamp = _local_display(str(event.get("raw_timestamp", "")))
                if kind == "message":
                    who = "用户" if event.get("role") == "user" else "助手"
                    phase = f"（{event.get('phase')}）" if event.get("phase") else ""
                    keep_going = append(f"[{timestamp}] {who}{phase}", event.get("text", ""), "")
                elif kind == "tool_call":
                    keep_going = append(f"[{timestamp}] 工具调用：{event.get('name')}  call_id={event.get('call_id') or '未找到'}", event.get("text", ""), "")
                elif kind == "tool_result":
                    keep_going = append(f"[{timestamp}] 工具结果：{event.get('name')}  call_id={event.get('call_id') or '未找到'}", event.get("text", ""), "")
                elif kind in {"status", "unknown"}:
                    keep_going = append(f"[{timestamp}] {event.get('name', '事件')}", event.get("text", ""), "")
                else:
                    keep_going = True
                if not keep_going:
                    break
                for attachment in event.get("attachments", []):
                    reference = attachment.get("ref", "")
                    if not append(f"[附件 {attachment.get('status', '状态未知')}] {reference or '未找到'}"):
                        break
            if issues:
                append("读取报告：")
                for item in issues:
                    if not append(f"- {item}"):
                        break
            if cancelled:
                lines.append("\n预览已取消。当前内容不完整；需要完整内容时请重新预览或导出。")
            if record_limited:
                lines.append(f"\n预览为控制读取占用，最多处理 {PREVIEW_RECORD_LIMIT:,} 条源记录。完整导出不受此限制。")
            if text_limited:
                lines.append("\n预览文本已达到字符预算，当前消息或后续内容可能未显示。完整导出不受此限制。")
            if truncated:
                lines.append(f"\n预览已截断，最多显示约 {PREVIEW_CHARACTER_LIMIT:,} 个字符。导出文件仍会包含完整记录。")
            self.completed.emit(self.session.session_id, "\n".join(lines))
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class ExportOptions(QDialog):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("导出设置")
        self.setMinimumWidth(600)
        outer = QVBoxLayout(self)

        location = QGroupBox("保存位置")
        loc_layout = QHBoxLayout(location)
        self.output_edit = QLineEdit(os.path.join(os.path.expanduser("~"), "Documents", "CodexHistoryExports"))
        browse = QPushButton("选择…")
        browse.clicked.connect(self._choose_folder)
        loc_layout.addWidget(self.output_edit, 1)
        loc_layout.addWidget(browse)
        outer.addWidget(location)

        fmt = QGroupBox("导出格式")
        grid = QGridLayout(fmt)
        self.format_checks = {
            "txt": QCheckBox("TXT"),
            "md": QCheckBox("Markdown"),
            "html": QCheckBox("HTML"),
            "json": QCheckBox("JSON"),
            "raw": QCheckBox("原始 JSONL / ZST"),
            "zip": QCheckBox("ZIP 打包"),
        }
        for index, (key, check) in enumerate(self.format_checks.items()):
            grid.addWidget(check, index // 3, index % 3)
        self.format_checks["md"].setChecked(True)
        self.format_checks["json"].setChecked(True)
        outer.addWidget(fmt)

        reading = QGroupBox("阅读版与隐私")
        form = QFormLayout(reading)
        self.mode = QComboBox()
        self.mode.addItem("完整版：聊天、工具和状态事件", "full")
        self.mode.addItem("纯聊天版：用户与助手消息", "chat")
        self.redact = QCheckBox("脱敏常见密码、令牌和本机路径")
        self.redact.setChecked(False)
        self.include_raw_confirm = QCheckBox("我确认原始文件可能包含密码、令牌等敏感内容")
        self.include_raw_confirm.setEnabled(False)
        self.format_checks["raw"].toggled.connect(self.include_raw_confirm.setEnabled)
        self.format_checks["raw"].toggled.connect(lambda checked: self.include_raw_confirm.setChecked(False) if not checked else None)
        form.addRow("阅读内容", self.mode)
        form.addRow("", self.redact)
        form.addRow("", self.include_raw_confirm)
        outer.addWidget(reading)
        warning = QLabel("聊天文本默认按记录原样导出；脱敏会替换常见密码、令牌和绝对本机路径，仍不能保证识别所有敏感信息。原始文件仅在勾选并确认后复制，且不应用脱敏。")
        warning.setWordWrap(True)
        outer.addWidget(warning)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self.cancel_button = QPushButton("取消")
        self.export_button = QPushButton("开始导出")
        self.cancel_button.clicked.connect(self.close)
        self.export_button.clicked.connect(self._accept)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.export_button)
        outer.addLayout(actions)
        self.options: dict[str, Any] | None = None

    def _choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择导出文件夹", self.output_edit.text())
        if folder:
            self.output_edit.setText(folder)

    def _accept(self) -> None:
        if not self.output_edit.text().strip():
            QMessageBox.warning(self, "缺少保存位置", "请选择导出文件夹。")
            return
        formats = [key for key in ("txt", "md", "html", "json", "zip") if self.format_checks[key].isChecked()]
        include_raw = self.format_checks["raw"].isChecked()
        if not formats and not include_raw:
            QMessageBox.warning(self, "未选择格式", "至少选择一种导出格式。")
            return
        if include_raw and not self.include_raw_confirm.isChecked():
            QMessageBox.warning(self, "需要确认", "请确认原始文件可能包含敏感内容后再导出。")
            return
        if include_raw:
            formats.append("raw")
        self.options = {
            "output_root": os.path.abspath(os.path.expandvars(self.output_edit.text().strip())),
            "formats": formats,
            "mode": self.mode.currentData(),
            "redact": self.redact.isChecked(),
            "include_raw": include_raw,
        }
        self.accept()


class MainWindow(QMainWindow):
    COLUMNS = ["选择", "会话标题", "会话 ID", "创建时间（本机）", "最后更新时间（本机）", "类别 / 来源 / 状态", "工作目录", "归档", "用户", "助手", "工具", "本地文件大小"]

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Codex 与 ChatGPT 历史记录浏览与导出")
        self.resize(1500, 900)
        self.setMinimumSize(960, 640)
        self.settings = QSettings("Local Codex Tools", "Codex History Viewer")
        saved_geometry = self.settings.value("ui/geometry")
        self.setStyleSheet("""
            QMainWindow, QWidget { background: #f4f6fa; color: #202938; }
            QLabel { background: transparent; }
            QLineEdit, QComboBox, QDateEdit, QPlainTextEdit {
                background: #ffffff; border: 1px solid #cbd3df; border-radius: 6px;
                padding: 6px 8px; selection-background-color: #2563eb;
            }
            QLineEdit:focus, QComboBox:focus, QDateEdit:focus, QPlainTextEdit:focus {
                border: 1px solid #3b82f6;
            }
            QPushButton {
                background: #ffffff; border: 1px solid #cbd3df; border-radius: 6px;
                padding: 7px 12px; min-height: 20px;
            }
            QPushButton:hover { background: #eef4ff; border-color: #93b4ed; }
            QPushButton:pressed { background: #dce9ff; }
            QPushButton:disabled { color: #98a2b3; background: #f0f2f5; }
            QPushButton[primary="true"] {
                color: #ffffff; background: #2563eb; border-color: #2563eb; font-weight: 600;
            }
            QPushButton[primary="true"]:hover { background: #1d4ed8; }
            QPushButton[primary="true"]:disabled { color: #ffffff; background: #9bb8ef; border-color: #9bb8ef; }
            QTableView {
                background: #ffffff; alternate-background-color: #f8fafc;
                border: 1px solid #d8dee8; border-radius: 7px; gridline-color: #edf0f5;
                selection-background-color: #dbeafe; selection-color: #172554;
            }
            QHeaderView::section {
                background: #eef2f7; color: #344054; border: 0; border-bottom: 1px solid #d8dee8;
                padding: 7px 5px; font-weight: 600;
            }
            QProgressBar { background: #e7ebf2; border: 0; border-radius: 4px; min-height: 8px; text-align: center; }
            QProgressBar::chunk { background: #3478e5; border-radius: 4px; }
            QStatusBar { background: #eef2f7; color: #526071; }
        """)
        self.catalog: Catalog | None = None
        self.sessions_by_id: dict[str, SessionInfo] = {}
        self.visible_sessions: list[SessionInfo] = []
        self.selected_ids: set[str] = set()
        self.scan_worker: ScanWorker | None = None
        self.import_worker: ImportWorker | None = None
        self.export_worker: ExportWorker | None = None
        self.preview_worker: PreviewWorker | None = None
        self.filter_worker: FilterWorker | None = None
        self._pending_import_root = ""
        self._filter_generation = 0
        self._filter_applied_generation = 0
        self._task_busy = False
        self._close_pending = False
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(160)
        self.search_timer.timeout.connect(self.apply_filters)

        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(12, 10, 12, 8)
        root_layout.setSpacing(8)

        source_group = QGroupBox("Codex 数据目录")
        source_row = QHBoxLayout(source_group)
        source_row.setContentsMargins(10, 12, 10, 10)
        saved_source = str(self.settings.value("source/root", "") or "")
        initial_source = saved_source if os.path.isdir(os.path.expandvars(saved_source)) else default_codex_home()
        self.path_edit = QLineEdit(initial_source)
        self.path_edit.setClearButtonEnabled(False)
        self.path_edit.setToolTip("默认使用 CODEX_HOME；未设置时为 %USERPROFILE%\\.codex")
        self.choose_path_button = QPushButton("浏览…")
        self.import_button = QPushButton("导入包…")
        self.import_button.setToolTip("导入另一台电脑导出的 ZIP，会解压到本机导入库并显示在列表中")
        self.scan_button = QPushButton("扫描 / 刷新")
        self.import_button.clicked.connect(self._import_package)
        self.choose_path_button.clicked.connect(self._choose_codex_home)
        self.scan_button.clicked.connect(self.start_scan)
        source_row.addWidget(self.path_edit, 1)
        source_row.addWidget(self.import_button)
        source_row.addWidget(self.choose_path_button)
        source_row.addWidget(self.scan_button)
        root_layout.addWidget(source_group)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("搜索"))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("标题、会话 ID、聊天内容或工作目录")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(lambda _text: self.search_timer.start())
        filter_row.addWidget(self.search_edit, 1)
        self.platform_filter = QComboBox()
        self.platform_filter.addItem("全部来源", "all")
        self.platform_filter.addItem("Codex", "codex")
        self.platform_filter.addItem("ChatGPT 网页", "chatgpt")
        self.platform_filter.addItem("来源未知", "unknown")
        self.platform_filter.setToolTip("按数据库明确记录的来源类别筛选；不会根据模型名称猜来源")
        self.platform_filter.currentIndexChanged.connect(self.apply_filters)
        filter_row.addWidget(self.platform_filter)
        self.archive_filter = QComboBox()
        self.archive_filter.addItem("全部会话", "all")
        self.archive_filter.addItem("未归档", "active")
        self.archive_filter.addItem("已归档", "archived")
        self.archive_filter.currentIndexChanged.connect(self.apply_filters)
        filter_row.addWidget(self.archive_filter)
        self.date_enabled = QCheckBox("日期范围")
        self.date_enabled.setToolTip("按会话创建日期筛选；创建时间未找到或无法识别的会话不会匹配")
        self.date_enabled.toggled.connect(self.apply_filters)
        filter_row.addWidget(self.date_enabled)
        self.date_from = QDateEdit(QDate.currentDate().addMonths(-1))
        self.date_from.setCalendarPopup(True)
        self.date_from.setDisplayFormat("yyyy-MM-dd")
        self.date_from.setToolTip("按会话创建日期筛选；没有可识别创建日期的会话不会显示")
        self.date_to = QDateEdit(QDate.currentDate())
        self.date_to.setCalendarPopup(True)
        self.date_to.setDisplayFormat("yyyy-MM-dd")
        self.date_to.setToolTip("按会话创建日期筛选；没有可识别创建日期的会话不会显示")
        self.date_from.dateChanged.connect(self.apply_filters)
        self.date_to.dateChanged.connect(self.apply_filters)
        self.date_from.setEnabled(False)
        self.date_to.setEnabled(False)
        self.date_enabled.toggled.connect(self.date_from.setEnabled)
        self.date_enabled.toggled.connect(self.date_to.setEnabled)
        filter_row.addWidget(self.date_from)
        filter_row.addWidget(QLabel("至"))
        filter_row.addWidget(self.date_to)
        root_layout.addLayout(filter_row)

        self.source_info_label = QLabel(
            "来源说明：Codex 会话即使使用 GPT 模型也仍属于 Codex；ChatGPT 网页按明确来源标记识别。"
            "具体记录是否有正文，请看列表中的读取状态。"
        )
        self.source_info_label.setWordWrap(True)
        self.source_info_label.setTextFormat(Qt.TextFormat.PlainText)
        self.source_info_label.setStyleSheet(
            "QLabel { background: #edf4ff; color: #344b6b; border-left: 3px solid #5b8def; "
            "border-radius: 4px; padding: 6px 9px; }"
        )
        self.source_info_label.setToolTip(
            "来源类别表示记录来自 Codex 应用还是 ChatGPT 网页，不表示所用模型。"
            "来源未知表示本机记录没有可确认的平台来源标记。"
        )
        root_layout.addWidget(self.source_info_label)
        self.disk_usage_label = QLabel("当前结果的本地文件大小：扫描后显示。")
        self.disk_usage_label.setStyleSheet("color: #526071; padding: 0 3px;")
        self.disk_usage_label.setToolTip(
            "按会话统计 rollout 文件和明确关联附件的逻辑文件大小；同一附件被多个会话引用时合计会重复计入。"
            "共享 SQLite 或索引文件无法准确分摊到单条会话；数值不代表磁盘分配簇占用。"
        )
        root_layout.addWidget(self.disk_usage_label)

        action_row = QHBoxLayout()
        self.select_visible_button = QPushButton("将当前结果加入选择")
        self.select_visible_button.setToolTip("把当前筛选显示的会话加入选择；筛选范围外已有的选择会保留。")
        self.clear_selection_button = QPushButton("清除选择")
        self.clear_selection_button.setToolTip("清除全部已选会话，包括当前筛选范围以外的会话。")
        self.preview_button = QPushButton("预览当前行")
        self.export_button = QPushButton("导出所选")
        self.export_button.setProperty("primary", True)
        self.export_visible_button = QPushButton("导出当前结果")
        self.export_visible_button.setToolTip("只导出当前筛选显示的会话，不会混入筛选范围外的已勾选会话。")
        self.report_button = QPushButton("读取报告")
        self.select_visible_button.clicked.connect(self.select_visible)
        self.clear_selection_button.clicked.connect(self.clear_selection)
        self.preview_button.clicked.connect(self.preview_selected)
        self.export_button.clicked.connect(self.start_export)
        self.export_visible_button.clicked.connect(self.start_export_visible)
        self.report_button.clicked.connect(self.show_scan_report)
        self.result_label = QLabel("匹配 0 / 0 条")
        self.result_label.setStyleSheet("color: #526071; font-weight: 600;")
        self.selection_label = QLabel("已选 0 条（当前结果 0 条）")
        self.selection_label.setStyleSheet("color: #344054; font-weight: 600;")
        self.select_visible_button.setEnabled(False)
        self.clear_selection_button.setEnabled(False)
        self.preview_button.setEnabled(False)
        self.export_button.setEnabled(False)
        self.export_visible_button.setEnabled(False)
        action_row.addWidget(self.select_visible_button)
        action_row.addWidget(self.clear_selection_button)
        action_row.addWidget(self.preview_button)
        action_row.addWidget(self.export_button)
        action_row.addWidget(self.export_visible_button)
        action_row.addWidget(self.report_button)
        action_row.addSpacing(10)
        action_row.addWidget(self.result_label)
        action_row.addSpacing(8)
        action_row.addWidget(self.selection_label)
        action_row.addStretch(1)
        root_layout.addLayout(action_row)

        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.table_model = SessionTableModel(self.selected_ids, self)
        self.table_model.selection_changed.connect(self._update_selection_label)
        self.table = QTableView()
        self.table.setModel(self.table_model)
        self.table.selectionModel().currentChanged.connect(lambda *_: self._update_selection_label())
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.verticalHeader().setVisible(False)
        self.table.setItemDelegateForColumn(0, SessionCheckDelegate(self.table))
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.resizeSection(0, 48)
        header.resizeSection(1, 150)
        header.resizeSection(2, 125)
        header.resizeSection(3, 105)
        header.resizeSection(4, 105)
        header.resizeSection(5, 210)
        header.setSectionResizeMode(6, header.ResizeMode.Stretch)
        for col, width in ((7, 42), (8, 34), (9, 34), (10, 34), (11, 112)):
            header.resizeSection(col, width)
        self.splitter.addWidget(self.table)

        preview_panel = QWidget()
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(0, 6, 0, 0)
        self.preview_title = QLabel("选择会话后预览记录")
        self.preview_title.setStyleSheet("font-weight: 600; color: #26364b; padding: 3px 2px;")
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        preview_layout.addWidget(self.preview_title)
        preview_layout.addWidget(self.preview, 1)
        self.splitter.addWidget(preview_panel)
        saved_splitter = self.settings.value("ui/splitter")
        if saved_splitter is not None and not self.splitter.restoreState(saved_splitter):
            self.splitter.setSizes([560, 280])
        elif saved_splitter is None:
            self.splitter.setSizes([560, 280])
        root_layout.addWidget(self.splitter, 1)
        saved_header = self.settings.value("ui/table_header")
        if saved_header is not None:
            header.restoreState(saved_header)

        footer = QHBoxLayout()
        self.status_label = QLabel("就绪")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setMaximumWidth(260)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_current)
        self.help_button = QPushButton("使用说明")
        self.help_button.clicked.connect(self.show_help)
        footer.addWidget(self.status_label, 1)
        footer.addWidget(self.progress_bar)
        footer.addWidget(self.cancel_button)
        footer.addWidget(self.help_button)
        root_layout.addLayout(footer)
        self.statusBar().showMessage(f"版本 {APP_VERSION} · 数据仅在本机处理")

        if saved_geometry is not None:
            self.restoreGeometry(saved_geometry)
        self.start_scan()

    def _choose_codex_home(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择 Codex 数据目录", self.path_edit.text())
        if folder:
            self.path_edit.setText(folder)
            self.start_scan()

    def _import_package(self) -> None:
        if self._task_busy or any(worker and worker.isRunning() for worker in (self.scan_worker, self.import_worker, self.export_worker, self.preview_worker)):
            return
        source, _ = QFileDialog.getOpenFileName(
            self,
            "选择 CodexHistoryViewer 导出 ZIP",
            os.path.join(os.path.expanduser("~"), "Documents"),
            "导出 ZIP (*.zip);;所有文件 (*.*)",
        )
        if not source:
            return
        default_destination = os.path.join(os.path.expanduser("~"), "Documents", "CodexHistoryImports")
        destination = QFileDialog.getExistingDirectory(self, "选择导入库保存位置", default_destination)
        if not destination:
            return
        self._task_busy = True
        self._refresh_controls()
        self.cancel_button.setEnabled(True)
        self.progress_bar.setRange(0, 0)
        self.status_label.setText("正在导入导出包…")
        self.import_worker = ImportWorker(source, destination)
        self.import_worker.progress.connect(self._progress)
        self.import_worker.completed.connect(self._import_completed)
        self.import_worker.failed.connect(self._import_failed)
        self.import_worker.finished.connect(self._import_finished)
        self.import_worker.start()

    @Slot(str, object)
    def _import_completed(self, root: str, report: dict[str, Any]) -> None:
        self._pending_import_root = root
        message = (
            f"导入完成：{report.get('imported_sessions', 0):,} 个会话，"
            f"已校验 {report.get('integrity_files_checked', 0):,} 个文件"
        )
        if report.get("reconstructed_sessions"):
            message += f"，其中 {report['reconstructed_sessions']:,} 个由阅读版重建"
        self.status_label.setText(message + "；正在建立列表…")

    @Slot(str)
    def _import_failed(self, message: str) -> None:
        self._pending_import_root = ""
        self._task_busy = False
        self._refresh_controls()
        self.cancel_button.setEnabled(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.status_label.setText("导入失败")
        QMessageBox.critical(self, "导入失败", message)

    @Slot()
    def _import_finished(self) -> None:
        worker = self.import_worker
        if worker:
            worker.deleteLater()
        self.import_worker = None
        root = getattr(self, "_pending_import_root", "")
        self._pending_import_root = ""
        self._task_busy = False
        if root and os.path.isdir(root):
            self.path_edit.setText(root)
            QTimer.singleShot(0, self.start_scan)
        else:
            self._refresh_controls()
            self.cancel_button.setEnabled(False)

    def start_scan(self) -> None:
        if self.scan_worker and self.scan_worker.isRunning() or self.import_worker and self.import_worker.isRunning() or self.export_worker and self.export_worker.isRunning() or self.preview_worker and self.preview_worker.isRunning():
            return
        root = os.path.abspath(os.path.expandvars(self.path_edit.text().strip()))
        if not os.path.isdir(root):
            QMessageBox.warning(self, "目录不存在", "请选择有效的 Codex 数据目录。")
            return
        self.settings.setValue("source/root", root)
        self._task_busy = True
        self._refresh_controls()
        self.cancel_button.setEnabled(True)
        self.status_label.setText("正在扫描…")
        self.progress_bar.setRange(0, 0)
        self.scan_worker = ScanWorker(root)
        self.scan_worker.progress.connect(self._progress)
        self.scan_worker.completed.connect(self._scan_completed)
        self.scan_worker.failed.connect(self._scan_failed)
        self.scan_worker.finished.connect(self._background_task_finished)
        self.scan_worker.start()

    @Slot(str, int)
    def _progress(self, message: str, value: int) -> None:
        self.status_label.setText(message)
        if value < 0:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(value)

    @Slot(object)
    def _scan_completed(self, catalog: Catalog) -> None:
        old = self.catalog
        selection_changed = _clear_selection_for_root_change(old, catalog, self.selected_ids)
        self.catalog = catalog
        self._task_busy = False
        self.sessions_by_id = {session.session_id: session for session in catalog.sessions}
        if _prune_selection_for_catalog(catalog, self.selected_ids):
            selection_changed = True
        if selection_changed:
            self.table_model.refresh_checks()
            self._update_selection_label()
        self._update_source_summary()
        self.cancel_button.setEnabled(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        self.apply_filters()
        size_mb = catalog.bytes_scanned / (1024 * 1024)
        if catalog.cancelled:
            scan_label = "扫描已取消（结果可能不完整）"
        elif not catalog.root_valid:
            scan_label = "数据目录无效，扫描未完成"
        else:
            scan_label = "扫描完成"
        self.status_label.setText(f"{scan_label}：{len(catalog.sessions):,} 个会话，{catalog.files_scanned} 个记录文件，{size_mb:.1f} MiB")
        if catalog.issues:
            self.statusBar().showMessage(f"扫描报告有 {len(catalog.issues)} 项；会话行的读取状态与预览中可查看详情", 10000)
        if old and old.temp_dir and old.temp_dir != catalog.temp_dir:
            shutil.rmtree(old.temp_dir, ignore_errors=True)

    @Slot(str)
    def _scan_failed(self, message: str) -> None:
        self._task_busy = False
        self._refresh_controls()
        self.cancel_button.setEnabled(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.status_label.setText("扫描失败")
        QMessageBox.critical(self, "扫描失败", message)

    def apply_filters(self, *_args) -> None:
        if not self.catalog or self._close_pending:
            return
        self._filter_generation += 1
        self.result_label.setText("正在筛选…")
        self.result_label.setToolTip("后台正在应用当前搜索和筛选条件。")
        self.disk_usage_label.setText("正在更新当前结果的本地文件大小…")
        self._refresh_controls()
        if self.filter_worker and self.filter_worker.isRunning():
            self.filter_worker.cancel()
            return
        self._start_filter(self._filter_generation)

    def _start_filter(self, generation: int) -> None:
        if not self.catalog or self._close_pending:
            return
        if generation == self._filter_generation:
            self.result_label.setText("正在筛选…")
            self.result_label.setToolTip("后台正在应用当前搜索和筛选条件。")
            self._refresh_controls()
        worker = FilterWorker(
            generation,
            self.catalog.sessions,
            self.search_edit.text().strip().casefold(),
            self.archive_filter.currentData(),
            self.date_enabled.isChecked(),
            self.date_from.date().toString("yyyy-MM-dd"),
            self.date_to.date().toString("yyyy-MM-dd"),
            self.platform_filter.currentData(),
            self,
        )
        worker.completed.connect(self._filters_completed)
        worker.finished.connect(lambda worker=worker: self._filter_finished(worker))
        self.filter_worker = worker
        worker.start()

    @Slot(int, object)
    def _filters_completed(self, generation: int, sessions: list[SessionInfo]) -> None:
        if generation != self._filter_generation or not self.catalog:
            return
        self.visible_sessions = sessions
        self._filter_applied_generation = generation
        self._render_rows()

    @Slot(object)
    def _filter_finished(self, worker: FilterWorker | None = None) -> None:
        if worker is None:
            worker = self.filter_worker
        if worker:
            worker.deleteLater()
        # A previous worker can finish after a newer filter worker has been
        # installed. Its callback must not clear or delete the newer worker.
        if worker is not self.filter_worker:
            return
        self.filter_worker = None
        if not self._close_pending and self.catalog and self._filter_applied_generation != self._filter_generation:
            self._start_filter(self._filter_generation)
        else:
            self._refresh_controls()

    def _render_rows(self) -> None:
        self.table_model.set_sessions(self.visible_sessions)
        self._update_selection_label()
        total = len(self.catalog.sessions) if self.catalog else 0
        if self.visible_sessions:
            self.result_label.setText(f"匹配 {len(self.visible_sessions):,} / {total:,} 条")
            self.result_label.setToolTip("当前筛选匹配的会话数 / 扫描到的会话总数")
            if self.preview_title.text() == "当前筛选没有匹配会话":
                self.preview_title.setText("选择会话后预览记录")
                self.preview.clear()
        else:
            self.result_label.setText(f"没有匹配会话（共 {total:,} 条）")
            self.result_label.setToolTip("请尝试清除搜索关键词、切换归档筛选，或关闭日期范围。")
            self.preview_title.setText("当前筛选没有匹配会话")
            self.preview.setPlainText("请尝试清除搜索关键词、切换归档筛选，或关闭日期范围。\n\n扫描结果和原始记录不会因此改变。")
        self._update_disk_usage_summary()
        self._refresh_controls()

    def _update_disk_usage_summary(self) -> None:
        if not self.visible_sessions:
            self.disk_usage_label.setText("当前结果没有会话；本地文件大小为 0。")
            return
        known = [session for session in self.visible_sessions if session.disk_size_bytes is not None]
        total_bytes = sum(session.disk_size_bytes or 0 for session in known)
        complete = sum(session.disk_size_status == "已统计" for session in known)
        partial = sum(session.disk_size_status == "部分统计" for session in known)
        shared = sum(session.disk_size_status == "共享数据库 / 索引" for session in self.visible_sessions)
        incomplete = sum(session.disk_size_status == "未能完整统计" for session in self.visible_sessions)
        missing = sum(session.disk_size_status == "未找到本地记录文件" for session in self.visible_sessions)
        classified = len(known) + shared + incomplete + missing
        unknown = max(0, len(self.visible_sessions) - classified)
        parts = [f"按会话归属累计文件大小：{_format_bytes(total_bytes)}（共享附件可能重复计入）"]
        if known:
            parts.append(f"已统计 {complete} 条")
            if partial:
                parts.append(f"部分统计 {partial} 条")
        if shared:
            parts.append(f"共享数据库 / 索引未分摊 {shared} 条")
        if incomplete:
            parts.append(f"未能完整统计 {incomplete} 条")
        if missing:
            parts.append(f"未找到本地记录文件 {missing} 条")
        if unknown:
            parts.append(f"大小状态未知 {unknown} 条")
        self.disk_usage_label.setText(" · ".join(parts) + "。")

    def _update_source_summary(self) -> None:
        if not self.catalog:
            return
        sessions = self.catalog.sessions
        codex_count = sum(session.platform == "codex" for session in sessions)
        chatgpt_sessions = [session for session in sessions if session.platform == "chatgpt"]
        chatgpt_metadata = sum(session.status == "仅目录元数据" for session in chatgpt_sessions)
        unknown_count = sum(session.platform == "unknown" for session in sessions)
        self.source_info_label.setText(
            f"本目录：Codex {codex_count:,} 条 · ChatGPT 网页 {len(chatgpt_sessions):,} 条 "
            f"（仅目录元数据 {chatgpt_metadata:,} 条）· 来源未知 {unknown_count:,} 条。"
            "Codex 会话即使使用 GPT 模型也仍属于 Codex；“仅目录元数据”表示未找到聊天正文。"
        )

    def _refresh_controls(self) -> None:
        """Keep actions aligned with the current catalog, filter result and background task."""
        busy = self._task_busy or any(
            worker and worker.isRunning()
            for worker in (self.scan_worker, self.import_worker, self.export_worker, self.preview_worker)
        )
        filter_ready = bool(self.catalog) and self._filter_applied_generation == self._filter_generation
        self.path_edit.setEnabled(not busy)
        self.import_button.setEnabled(not busy)
        self.choose_path_button.setEnabled(not busy)
        self.scan_button.setEnabled(not busy)
        self.search_edit.setEnabled(not busy)
        self.platform_filter.setEnabled(not busy)
        self.archive_filter.setEnabled(not busy)
        self.date_enabled.setEnabled(not busy)
        self.date_from.setEnabled(not busy and self.date_enabled.isChecked())
        self.date_to.setEnabled(not busy and self.date_enabled.isChecked())
        self.table.setEnabled(not busy and filter_ready)
        self.select_visible_button.setEnabled(not busy and filter_ready and bool(self.visible_sessions))
        self.clear_selection_button.setEnabled(not busy and bool(self.selected_ids))
        current_row = self.table.currentIndex().row()
        has_current = 0 <= current_row < len(self.visible_sessions)
        self.preview_button.setEnabled(not busy and filter_ready and (has_current or len(self.selected_ids) == 1))
        self.export_button.setEnabled(not busy and bool(self.selected_ids))
        self.export_visible_button.setEnabled(not busy and filter_ready and bool(self.visible_sessions))
        self.report_button.setEnabled(not busy and self.catalog is not None)
        self.cancel_button.setEnabled(busy)

    @Slot()
    def _background_task_finished(self) -> None:
        self._task_busy = False
        self._refresh_controls()

    def _update_selection_label(self) -> None:
        visible_selected = sum(session.session_id in self.selected_ids for session in self.visible_sessions)
        self.selection_label.setText(
            f"已选 {len(self.selected_ids):,} 条（当前结果 {visible_selected:,} 条）"
        )
        self._refresh_controls()

    def select_visible(self) -> None:
        self.selected_ids.update(session.session_id for session in self.visible_sessions)
        self.table_model.refresh_checks()
        self._update_selection_label()

    def clear_selection(self) -> None:
        self.selected_ids.clear()
        self.table_model.refresh_checks()
        self._update_selection_label()

    def _selected_sessions(self) -> list[SessionInfo]:
        if not self.catalog:
            return []
        return _export_scope_sessions(self.catalog.sessions, self.visible_sessions, self.selected_ids, False)

    def preview_selected(self) -> None:
        if any(worker and worker.isRunning() for worker in (self.scan_worker, self.import_worker, self.export_worker, self.preview_worker)):
            return
        if not self.catalog or self._filter_applied_generation != self._filter_generation:
            QMessageBox.information(self, "预览当前行", "筛选正在更新，请稍后再预览。")
            return
        current_row = self.table.currentIndex().row()
        target = _preview_target(self.visible_sessions, self.catalog.sessions, self.selected_ids, current_row) if self.catalog else None
        if not target:
            if len(self.selected_ids) > 1:
                detail = "已勾选多个会话。请先点选列表中的一行，再预览当前行。"
            else:
                detail = "请先点选列表中的一行，或只勾选一个会话。"
            QMessageBox.information(self, "预览当前行", detail)
            return
        session = target
        self.preview_title.setText(f"正在读取：{_platform_label(session.platform)} · {session.title} · {session.session_id[:8]}")
        self.preview.setPlainText("正在读取会话记录…")
        self.progress_bar.setRange(0, 0)
        self._task_busy = True
        self._refresh_controls()
        self.status_label.setText("正在读取预览…")
        self._preview_terminal_status = "预览完成"
        self.preview_worker = PreviewWorker(session, self.catalog)
        self.preview_worker.completed.connect(self._preview_completed)
        self.preview_worker.failed.connect(self._preview_failed)
        self.preview_worker.finished.connect(self._preview_finished)
        self.preview_worker.start()

    @Slot(str, object)
    def _preview_completed(self, session_id: str, text: str) -> None:
        if session_id not in self.sessions_by_id:
            return
        session = self.sessions_by_id[session_id]
        self.preview_title.setText(f"预览：{_platform_label(session.platform)} · {session.title} · {session_id[:8]}")
        self.preview.setPlainText(text)
        self._preview_terminal_status = "预览已取消" if self.preview_worker and self.preview_worker.cancel_event.is_set() else "预览完成"
        cursor = self.preview.textCursor()
        cursor.movePosition(cursor.MoveOperation.Start)
        self.preview.setTextCursor(cursor)

    @Slot(str)
    def _preview_failed(self, error: str) -> None:
        self.preview.setPlainText("读取失败：" + error)
        self._preview_terminal_status = "预览读取失败"

    @Slot()
    def _preview_finished(self) -> None:
        if not (self.scan_worker and self.scan_worker.isRunning()) and not (self.export_worker and self.export_worker.isRunning()):
            self._task_busy = False
            self._refresh_controls()
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(0)
            self.status_label.setText(self._preview_terminal_status)

    def start_export(self) -> None:
        self._start_export(visible_only=False)

    def start_export_visible(self) -> None:
        self._start_export(visible_only=True)

    def _start_export(self, visible_only: bool) -> None:
        if any(worker and worker.isRunning() for worker in (
            self.scan_worker,
            getattr(self, "import_worker", None),
            self.preview_worker,
            self.export_worker,
        )):
            return
        if not self.catalog:
            return
        if visible_only and self._filter_applied_generation != self._filter_generation:
            QMessageBox.information(self, "导出当前结果", "筛选正在更新，请稍后再导出，避免导出上一批结果。")
            return
        selected = _export_scope_sessions(
            self.catalog.sessions,
            self.visible_sessions,
            self.selected_ids,
            visible_only,
        )
        if not selected:
            message = "当前筛选没有可导出的会话。" if visible_only else "请先勾选一个或多个会话。"
            QMessageBox.information(self, "导出", message)
            return
        dialog = ExportOptions(self)
        if dialog.exec() != ExportOptions.DialogCode.Accepted or not dialog.options:
            return
        options = dialog.options
        chatgpt_count = sum(session.platform == "chatgpt" for session in selected)
        if chatgpt_count:
            answer = QMessageBox.warning(
                self,
                "ChatGPT 网页记录只有目录信息",
                f"本批次包含 {chatgpt_count:,} 条 ChatGPT 网页记录。目前本机只找到会话标题和时间等目录元数据，未找到聊天正文；导出不会包含完整对话。仍要继续导出？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        if options["include_raw"]:
            answer = QMessageBox.warning(
                self,
                "原始内容可能含敏感信息",
                f"将复制 {len(selected)} 个会话的原始记录文件。原始文件不应用脱敏。确认继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._task_busy = True
        self._refresh_controls()
        self.progress_bar.setRange(0, 0)
        self.status_label.setText("正在导出…")
        self.export_worker = ExportWorker(selected, self.catalog, options)
        self.export_worker.progress.connect(self._progress)
        self.export_worker.completed.connect(self._export_completed)
        self.export_worker.failed.connect(self._export_failed)
        self.export_worker.finished.connect(self._background_task_finished)
        self.export_worker.start()

    @Slot(object)
    def _export_completed(self, results: list[dict[str, Any]]) -> None:
        self._task_busy = False
        self._refresh_controls()
        self.progress_bar.setRange(0, 100)
        requested = len(self.export_worker.sessions) if self.export_worker else len(results)
        cancelled = bool(self.export_worker and self.export_worker.cancel_event.is_set())
        completed = sum(1 for result in results if result.get("status", "").startswith("完成"))
        failed = len(results) - completed
        not_started = max(0, requested - len(results))
        summary = f"导出已取消：成功 {completed}，失败或取消 {failed}，未开始 {not_started}" if cancelled else f"导出结束：成功 {completed}，失败 {failed}"
        self.progress_bar.setValue(0 if cancelled else 100)
        self.status_label.setText(summary)
        lines = [summary, ""]
        if not_started:
            lines.append(f"[未开始] 有 {not_started} 个会话在取消时尚未处理。")
        detail_limit = 50
        for result in results[:detail_limit]:
            lines.append(f"[{result.get('status')}] [{_platform_label(str(result.get('platform', 'unknown')))}] {result.get('title') or result.get('session_id')}")
            lines.append(f"  目录：{result.get('folder', '')}")
            if result.get("zip"):
                lines.append(f"  ZIP：{result['zip']}")
            for issue in result.get("issues", [])[:5]:
                lines.append(f"  报告：{issue}")
        if len(results) > detail_limit:
            lines.extend(["", f"界面仅显示前 {detail_limit} 条结果；完整列表见输出目录中的 导出报告.json。"])
        self.preview_title.setText("最近一次导出结果")
        self.preview.setPlainText("\n".join(lines))
        if results:
            folder = results[0].get("folder")
            if folder and os.path.isdir(folder):
                open_button = QMessageBox(self)
                if cancelled:
                    open_button.setWindowTitle("导出已取消")
                    open_button.setText(f"导出已取消。已生成的结果可能不完整，请检查此目录中的 manifest.json 和未完成.txt：\n{folder}")
                else:
                    open_button.setWindowTitle("导出完成")
                    open_button.setText(f"导出已完成。结果保存在：\n{folder}")
                open_button.setStandardButtons(QMessageBox.StandardButton.Ok)
                open_button.setInformativeText("详细错误和文件哈希位于每个会话目录的 manifest.json。")
                open_button.exec()

    @Slot(str)
    def _export_failed(self, message: str) -> None:
        self._task_busy = False
        self._refresh_controls()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.status_label.setText("导出失败")
        QMessageBox.critical(self, "导出失败", message)

    def cancel_current(self) -> None:
        if self.scan_worker and self.scan_worker.isRunning():
            self.scan_worker.cancel()
            self.status_label.setText("正在取消扫描…")
        elif self.import_worker and self.import_worker.isRunning():
            self.import_worker.cancel()
            self.status_label.setText("正在取消导入…")
        elif self.export_worker and self.export_worker.isRunning():
            self.export_worker.cancel()
            self.status_label.setText("正在取消导出…已写出的部分目录会标记为未完成")
        elif self.preview_worker and self.preview_worker.isRunning():
            self.preview_worker.cancel()
            self.status_label.setText("正在取消预览…")

    def show_help(self) -> None:
        QMessageBox.information(
            self,
            "本地数据说明",
            "工具只读扫描所选 Codex 目录，并按明确来源字段分开标记 Codex 与 ChatGPT 网页会话。聊天正文以 Codex rollout JSONL 为主，数据库用于会话元数据与缺少 rollout 时的条目回退；session_index.jsonl 和 history.jsonl 不当作完整聊天记录。\n\n"
            "ChatGPT 网页会话目录当前可能只有标题和时间等元数据。若状态显示“仅目录元数据”，表示本机没有找到聊天正文；导出只包含可用元数据和缺失说明，不是完整对话。\n\n"
            "归档 ZST 需要 zstandard 依赖。SQLite 先以只读方式建立临时一致性快照，从而读取 WAL 中已提交内容。附件仅复制 attachments\\<会话 ID> 或记录明确引用的文件。\n\n"
            "数据库投影可能晚于正在写入的 rollout，或者仅保留部分条目。读取报告、损坏行、缺失附件和未知事件类型会写入预览及 manifest.json。\n\n"
            "列表中的本地文件大小按会话统计 rollout 文件和明确关联附件的逻辑文件大小；同一附件被多个会话引用时会在汇总里重复计入。共享 SQLite 和索引文件无法准确拆分到单条会话，会显示为未分摊。统计结果不是 NTFS 实际分配簇占用；无法确认完整性的记录会单独计数。\n\n"
            "原始导出可能含敏感内容，默认关闭；常见格式脱敏是启发式处理，不能保证识别所有秘密。HTML 为静态离线文件，记录内容经过转义。",
        )

    def show_scan_report(self) -> None:
        if not self.catalog:
            QMessageBox.information(self, "读取报告", "请先扫描 Codex 数据目录。")
            return
        lines = [
            f"扫描目录：{self.catalog.root}",
            f"rollout 文件：{self.catalog.files_scanned}",
            f"读取字节：{self.catalog.bytes_scanned:,}",
            f"发现会话：{len(self.catalog.sessions):,}",
            "",
            "辅助数据源：",
            *[f"- {note}" for note in self.catalog.notes],
            "",
            "SQLite 结构检测：",
        ]
        for database in self.catalog.databases:
            recognized = [table.name for table in database.tables if table.metadata_id or table.item_json]
            lines.append(f"- {database.source_path}")
            lines.append(f"  已识别表：{', '.join(recognized) if recognized else '无会话条目表'}")
            if database.error:
                lines.append(f"  状态：{database.error}")
        if self.catalog.issues:
            lines.extend(["", f"扫描问题（{len(self.catalog.issues)} 项）：", *[f"- {item}" for item in self.catalog.issues]])
        session_issue_count = sum(len(session.issues) for session in self.catalog.sessions)
        if session_issue_count:
            lines.extend(["", f"会话问题（{session_issue_count} 项）："])
            for session in self.catalog.sessions:
                for issue in session.issues:
                    lines.append(f"- {session.session_id}：{issue}")
        dialog = QDialog(self)
        dialog.setWindowTitle("读取报告")
        dialog.resize(860, 620)
        layout = QVBoxLayout(dialog)
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setPlainText("\n".join(lines))
        layout.addWidget(view, 1)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(dialog.accept)
        layout.addWidget(close_button, 0, Qt.AlignmentFlag.AlignRight)
        dialog.exec()

    def closeEvent(self, event) -> None:
        if self._close_pending:
            event.ignore()
            return
        active = False
        for worker in (self.scan_worker, self.import_worker, self.export_worker, self.preview_worker, self.filter_worker):
            if worker:
                worker.finished.connect(self._finish_close_after_workers)
                if worker.isRunning():
                    active = True
                    worker.cancel()
        if active:
            self._close_pending = True
            self.status_label.setText("正在安全关闭，等待后台读取或导出停止…")
            event.ignore()
            return
        self.settings.setValue("ui/geometry", self.saveGeometry())
        self.settings.setValue("ui/splitter", self.splitter.saveState())
        self.settings.setValue("ui/table_header", self.table.horizontalHeader().saveState())
        self.settings.sync()
        self._cleanup_temporary_catalogs()
        event.accept()

    @Slot()
    def _finish_close_after_workers(self) -> None:
        if self._close_pending and not any(worker and worker.isRunning() for worker in (self.scan_worker, self.import_worker, self.export_worker, self.preview_worker, self.filter_worker)):
            self._close_pending = False
            self.close()

    def _cleanup_temporary_catalogs(self) -> None:
        directories = {catalog.temp_dir for catalog in (self.catalog, getattr(self.scan_worker, "catalog", None)) if catalog and catalog.temp_dir}
        for directory in directories:
            shutil.rmtree(directory, ignore_errors=True)


def run() -> int:
    app: QApplication | None = None
    try:
        app = QApplication(sys.argv)
        app.setApplicationName("Codex 历史记录浏览与导出")
        app.setOrganizationName("Local Codex Tools")
        app.setFont(QFont("Microsoft YaHei UI", 9))
        window = MainWindow()
        window.show()
        return app.exec()
    except Exception:
        details = traceback.format_exc()
        log_dir = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "CodexHistoryViewer"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / "启动错误.log").write_text(details, encoding="utf-8")
        except OSError:
            pass
        if app is not None:
            try:
                QMessageBox.critical(None, "Codex 历史记录工具启动失败", f"程序启动时发生错误。\n\n已保存诊断日志：{log_dir / '启动错误.log'}\n\n{details[-2000:]}")
            except Exception:
                pass
        return 1
