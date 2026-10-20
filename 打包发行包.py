from __future__ import annotations

import hashlib
import os
import re
import sys
import uuid
import zipfile
from pathlib import Path


PROJECT = Path(__file__).resolve().parent
OUTPUT = PROJECT.parent
ROOT_FILES = {
    "README.md",
    "启动工具.bat",
    "构建EXE.bat",
    "main.py",
    "pyside_bootstrap.py",
    "requirements.txt",
    "requirements-build.txt",
    "CodexHistoryViewer.spec",
    "打包发行包.py",
}
WINDOWS_ROOT_FILES = {"README.md", "启动工具.bat"}
PACKAGE_DIRS = {"codex_history_viewer", "tests", "dist"}
SKIP_DIRS = {"__pycache__", ".venv", "build"}


def app_version() -> str:
    source = (PROJECT / "codex_history_viewer" / "reader.py").read_text(encoding="utf-8")
    match = re.search(r'^APP_VERSION\s*=\s*["\']([^"\']+)["\']', source, re.MULTILINE)
    if not match:
        raise RuntimeError("无法从 reader.py 读取工具版本")
    return match.group(1)


def project_files(include_source: bool) -> list[Path]:
    selected: list[Path] = []
    for name in sorted(ROOT_FILES if include_source else WINDOWS_ROOT_FILES):
        path = PROJECT / name
        if path.is_file() and not path.is_symlink():
            selected.append(path)

    wanted = PACKAGE_DIRS if include_source else {"dist"}
    for directory in sorted(wanted):
        root = PROJECT / directory
        if not root.is_dir() or root.is_symlink():
            continue
        for base, directories, files in os.walk(root, followlinks=False):
            current = Path(base)
            directories[:] = [
                name
                for name in directories
                if name not in SKIP_DIRS
                and not (current / name).is_symlink()
                and not getattr(current / name, "is_junction", lambda: False)()
            ]
            for name in files:
                path = current / name
                if path.is_file() and not path.is_symlink():
                    selected.append(path)
    return sorted(set(selected), key=lambda path: path.relative_to(PROJECT).as_posix().casefold())


def build_archive(destination: Path, files: list[Path]) -> str:
    temporary = destination.with_name(f".{destination.stem}.{uuid.uuid4().hex}.partial")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in files:
                archive.write(path, path.relative_to(PROJECT).as_posix())
        with zipfile.ZipFile(temporary, "r") as archive:
            broken = archive.testzip()
            if broken is not None:
                raise RuntimeError(f"ZIP CRC 校验失败：{broken}")
            names = set(archive.namelist())
            if "启动工具.bat" not in names or "dist/CodexHistoryViewer/CodexHistoryViewer.exe" not in names:
                raise RuntimeError("发行包缺少启动脚本或 EXE")
            if any(info.flag_bits & 0x800 == 0 and any(ord(char) > 127 for char in info.filename) for info in archive.infolist()):
                raise RuntimeError("ZIP 中文文件名未启用 UTF-8 标记")
        os.replace(temporary, destination)
        digest = hashlib.sha256()
        with destination.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    executable = PROJECT / "dist" / "CodexHistoryViewer" / "CodexHistoryViewer.exe"
    if not executable.is_file():
        raise SystemExit("找不到 dist\\CodexHistoryViewer\\CodexHistoryViewer.exe，请先运行 构建EXE.bat")
    version = app_version()
    packages = (
        (f"CodexHistoryViewer-{version}-Windows.zip", project_files(include_source=False)),
        (f"CodexHistoryViewer-{version}-Full.zip", project_files(include_source=True)),
    )
    for filename, files in packages:
        destination = OUTPUT / filename
        digest = build_archive(destination, files)
        print(f"已生成：{destination}")
        print(f"文件数：{len(files)}  大小：{destination.stat().st_size:,} 字节")
        print(f"SHA-256：{digest}")


if __name__ == "__main__":
    main()
