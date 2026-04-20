#!/usr/bin/env python3
"""Render 배포 시 한글 폰트 자동 다운로드.

buildCommand에서 호출:
  python scripts/download_fonts.py
"""
from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FONTS_DIR = ROOT / "fonts"

# 다운로드할 폰트 목록: (저장파일명, 다운로드 URL)
FONTS = [
    (
        "NanumGothic.ttf",
        "https://github.com/google/fonts/raw/main/ofl/nanumgothic/NanumGothic-Regular.ttf",
    ),
]


def main() -> int:
    FONTS_DIR.mkdir(exist_ok=True)
    success = True
    for filename, url in FONTS:
        dest = FONTS_DIR / filename
        if dest.exists() and dest.stat().st_size > 10_000:
            print(f"[fonts] Already exists: {dest} ({dest.stat().st_size:,} bytes)")
            continue
        print(f"[fonts] Downloading {filename} from {url} ...")
        try:
            urllib.request.urlretrieve(url, str(dest))
            size = dest.stat().st_size
            print(f"[fonts] OK: {dest} ({size:,} bytes)")
            if size < 10_000:
                print(f"[fonts] WARN: {filename} seems too small ({size} bytes), may be corrupt", file=sys.stderr)
                success = False
        except Exception as exc:
            print(f"[fonts] WARN: {filename} download failed: {exc}", file=sys.stderr)
            success = False
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
