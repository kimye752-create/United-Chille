"""ReportLab 한글 폰트 등록 공유 모듈.

NanumGothic (Regular) + NanumGothicBold (Bold) 를 우선 사용.
프로젝트 fonts/ 폴더가 없으면 OS 시스템 폰트를 차례로 탐색.
"""
from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_FONT_DIR = _ROOT / "fonts"

_CACHE: tuple[str, str] | None = None   # (regular_name, bold_name)


def register() -> tuple[str, str]:
    """한글 폰트를 ReportLab에 등록하고 (regular_name, bold_name) 반환.

    이미 등록된 경우 캐시값 반환 (중복 등록 방지).
    """
    global _CACHE
    if _CACHE is not None:
        return _CACHE

    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    # (이름, 레귤러 경로, 볼드 경로) 우선순위 목록
    candidates: list[tuple[str, str, str]] = [
        # 프로젝트 fonts/ 폴더 (배포 환경 포함)
        ("NanumGothic",
         str(_FONT_DIR / "NanumGothic.ttf"),
         str(_FONT_DIR / "NanumGothicBold.ttf")),
        # Windows 시스템
        ("MalgunGothic",
         "C:/Windows/Fonts/malgun.ttf",
         "C:/Windows/Fonts/malgunbd.ttf"),
        # macOS 시스템
        ("AppleGothic",
         "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
         "/System/Library/Fonts/Supplemental/AppleGothic.ttf"),
        ("NanumGothic",
         "/Library/Fonts/NanumGothic.ttf",
         "/Library/Fonts/NanumGothicBold.ttf"),
    ]

    for name, reg_path, bold_path in candidates:
        if not Path(reg_path).is_file():
            continue
        try:
            reg_name  = name
            bold_name = f"{name}-Bold"
            pdfmetrics.registerFont(TTFont(reg_name,  reg_path))
            if Path(bold_path).is_file() and bold_path != reg_path:
                pdfmetrics.registerFont(TTFont(bold_name, bold_path))
            else:
                # Bold 파일 없으면 Regular 재사용
                pdfmetrics.registerFont(TTFont(bold_name, reg_path))
            _CACHE = (reg_name, bold_name)
            return _CACHE
        except Exception:
            continue

    # 최후 폴백: ReportLab CID 한글 폰트
    try:
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        pdfmetrics.registerFont(UnicodeCIDFont("HYSMyeongJo-Medium"))
        _CACHE = ("HYSMyeongJo-Medium", "HYSMyeongJo-Medium")
        return _CACHE
    except Exception:
        pass

    # ASCII 폴백
    _CACHE = ("Helvetica", "Helvetica-Bold")
    return _CACHE
