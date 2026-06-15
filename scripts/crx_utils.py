"""解压浏览器扩展包（ZIP / CRX）。"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

ZIP_SIGNATURE = b"PK\x03\x04"
MIN_ARCHIVE_SIZE = 10_000


def crx_to_zip_bytes(crx_data: bytes) -> bytes:
    if crx_data[:4] == b"Cr24":
        zip_start = crx_data.find(ZIP_SIGNATURE)
        if zip_start < 0:
            raise ValueError("无效的 CRX 文件：未找到 ZIP 数据")
        return crx_data[zip_start:]

    if crx_data[:2] == ZIP_SIGNATURE[:2]:
        return crx_data

    raise ValueError("无法识别的扩展文件格式")


def _find_extension_root(dest_dir: Path) -> Path:
    if (dest_dir / "manifest.json").exists():
        return dest_dir

    for child in dest_dir.iterdir():
        if child.is_dir() and (child / "manifest.json").exists():
            return child

    raise ValueError(f"解压后未找到 manifest.json: {dest_dir}")


def extract_zip(archive_path: Path, dest_dir: Path) -> Path:
    data = archive_path.read_bytes()
    if len(data) < MIN_ARCHIVE_SIZE:
        preview = data[:200].decode("utf-8", errors="replace")
        raise ValueError(
            f"扩展包过小 ({len(data)} bytes)，下载可能失败。内容预览: {preview[:120]}"
        )

    dest_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(archive_path, "r") as archive:
        archive.extractall(dest_dir)

    root = _find_extension_root(dest_dir)
    if root != dest_dir:
        for item in root.iterdir():
            target = dest_dir / item.name
            if target.exists():
                continue
            item.rename(target)
        root.rmdir()

    if not (dest_dir / "manifest.json").exists():
        raise ValueError(f"扩展目录无效: {dest_dir}")

    return dest_dir


def extract_crx(crx_path: Path, dest_dir: Path) -> Path:
    crx_data = crx_path.read_bytes()
    if len(crx_data) < MIN_ARCHIVE_SIZE:
        preview = crx_data[:200].decode("utf-8", errors="replace")
        raise ValueError(
            f"CRX 文件过小 ({len(crx_data)} bytes)，下载可能失败。内容预览: {preview[:120]}"
        )

    zip_data = crx_to_zip_bytes(crx_data)
    dest_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(io.BytesIO(zip_data), "r") as archive:
        archive.extractall(dest_dir)

    return _find_extension_root(dest_dir)
