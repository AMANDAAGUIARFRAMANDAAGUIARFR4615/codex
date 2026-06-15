#!/usr/bin/env python3
"""下载并安装 Cookie-Editor 扩展。"""

from __future__ import annotations

import sys
from pathlib import Path

import requests

from crx_utils import extract_zip
from io_utils import setup_utf8_stdio

setup_utf8_stdio()

# 官方 GitHub Release（Chrome Web Store 在 CI 中常返回无效 CRX）
GITHUB_RELEASE_URL = (
    "https://github.com/Moustachauve/cookie-editor/releases/download/"
    "v1.13.0/cookie-editor-chrome-1.13.0.zip"
)


def install_cookie_editor(dest_dir: Path, cache_zip: Path | None = None) -> Path:
    if dest_dir.exists() and (dest_dir / "manifest.json").exists():
        print(f"[extension] Cookie-Editor 已存在: {dest_dir}")
        return dest_dir

    zip_path = cache_zip or (dest_dir.parent / "cookie-editor-chrome.zip")
    zip_path.parent.mkdir(parents=True, exist_ok=True)

    print("[extension] 从 GitHub Release 下载 Cookie-Editor...")
    response = requests.get(GITHUB_RELEASE_URL, timeout=120)
    response.raise_for_status()
    zip_path.write_bytes(response.content)
    print(f"[extension] 已下载 {len(response.content)} bytes -> {zip_path}")

    extract_zip(zip_path, dest_dir)
    print(f"[extension] 已安装到 {dest_dir}")
    return dest_dir


def main() -> None:
    dest_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("extensions/cookie-editor")
    cache_zip = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    install_cookie_editor(dest_dir, cache_zip)


if __name__ == "__main__":
    main()
