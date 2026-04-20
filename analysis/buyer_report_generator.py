"""바이어 발굴 보고서 PDF 생성기.

구조:
  표지: 제품명 + 분석일
  요약 테이블: Top 10 기업 한눈에 보기
  기업별 상세 페이지:
    기업 개요 / 추천 이유 / 기본 정보 / 기업 규모 / 역량·실적 / 채널·파트너십 / 출처
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

# ── 한글 폰트 등록 ────────────────────────────────────────────────────────────
_FONT_DIR = Path(__file__).resolve().parents[1] / "fonts"
_FONT_REGULAR = _FONT_DIR / "NanumGothic.ttf"
_FONT_BOLD    = _FONT_DIR / "NanumGothicBold.ttf"

def _register_fonts() -> tuple[str, str]:
    """한글 폰트 등록. 폰트 파일 없으면 Helvetica 폴백."""
    if _FONT_REGULAR.is_file() and _FONT_BOLD.is_file():
        try:
            pdfmetrics.registerFont(TTFont("Korean",      str(_FONT_REGULAR)))
            pdfmetrics.registerFont(TTFont("Korean-Bold", str(_FONT_BOLD)))
            return "Korean", "Korean-Bold"
        except Exception:
            pass
    return "Helvetica", "Helvetica-Bold"

_FONT, _FONT_BOLD_NAME = _register_fonts()

# ── 색상 ──────────────────────────────────────────────────────────────────────
_NAVY   = colors.Color(23/255, 63/255, 120/255)
_GREEN  = colors.Color(39/255, 174/255, 96/255)
_ORANGE = colors.Color(230/255, 126/255, 34/255)
_LIGHT  = colors.Color(245/255, 247/255, 250/255)
_MUTED  = colors.Color(120/255, 130/255, 150/255)
_REASON = colors.Color(235/255, 245/255, 255/255)  # 추천이유 배경
_WHITE  = colors.white

W, H = A4


def _styles() -> dict:
    base = getSampleStyleSheet()

    def _s(name, parent="Normal", **kw) -> ParagraphStyle:
        return ParagraphStyle(name, parent=base[parent], **kw)

    return {
        "cover_title": _s("cover_title", fontSize=22, leading=30, textColor=_NAVY,
                          fontName=_FONT_BOLD_NAME, spaceAfter=4),
        "cover_sub":   _s("cover_sub",   fontSize=13, leading=18, textColor=_MUTED,
                          fontName=_FONT, spaceAfter=12),
        "section":     _s("section",     fontSize=10, leading=14, textColor=_NAVY,
                          fontName=_FONT_BOLD_NAME, spaceBefore=8, spaceAfter=3),
        "body":        _s("body",        fontSize=9,  leading=14, textColor=colors.black,
                          fontName=_FONT, spaceAfter=2),
        "small":       _s("small",       fontSize=8,  leading=12, textColor=_MUTED,
                          fontName=_FONT),
        "reason":      _s("reason",      fontSize=9,  leading=15, textColor=colors.black,
                          fontName=_FONT, spaceAfter=2,
                          backColor=_REASON, borderPadding=(6, 8, 6, 8)),
        "overview":    _s("overview",    fontSize=9,  leading=14, textColor=colors.black,
                          fontName=_FONT, spaceAfter=2),
        "link":        _s("link",        fontSize=8,  leading=12, textColor=colors.blue,
                          fontName=_FONT),
    }


def _yn(val: Any) -> str:
    if val is True:  return "있음"
    if val is False: return "없음"
    return "-"


def _dash(val: Any) -> str:
    if val is None or str(val).strip() in ("", "None", "null", "-"):
        return "-"
    return str(val)


def _build_cover(product_label: str, company_count: int, styles: dict) -> list:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return [
        Spacer(1, 30*mm),
        Paragraph("바이어 발굴 보고서", styles["cover_title"]),
        Paragraph(f"제품: {product_label}", styles["cover_sub"]),
        Paragraph(f"발굴 기업 수: {company_count}개  |  분석일: {now}", styles["small"]),
        Spacer(1, 6*mm),
        HRFlowable(width="100%", thickness=1.5, color=_NAVY),
        Spacer(1, 4*mm),
        Paragraph(
            "본 보고서는 CPHI Japan 전시회 참가 기업 크롤링 및 Claude AI 심층조사를 통해 "
            "자동 생성된 바이어 발굴 분석 결과입니다. "
            "성분/치료군 일치 기업 및 싱가포르·ASEAN 대상 사업자를 대상으로 수집하였습니다.",
            styles["body"],
        ),
        PageBreak(),
    ]


def _build_summary_table(companies: list[dict], styles: dict) -> list:
    elems: list = [
        Paragraph("바이어 후보 요약", styles["cover_title"]),
        Spacer(1, 4*mm),
    ]
    header = ["#", "기업명", "국가", "카테고리", "이메일"]
    rows   = [header]
    for i, c in enumerate(companies, 1):
        rows.append([
            str(i),
            (c.get("company_name") or "-")[:28],
            (c.get("country") or "-"),
            (c.get("category") or "-")[:20],
            (c.get("email") or "-")[:30],
        ])

    col_w = [10*mm, 58*mm, 28*mm, 40*mm, 50*mm]
    tbl = Table(rows, colWidths=col_w, repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), _NAVY),
        ("TEXTCOLOR",     (0, 0), (-1, 0), _WHITE),
        ("FONTNAME",      (0, 0), (-1, 0), _FONT_BOLD_NAME),
        ("FONTNAME",      (0, 1), (-1, -1), _FONT),
        ("FONTSIZE",      (0, 0), (-1, -1), 8),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [_LIGHT, _WHITE]),
        ("GRID",          (0, 0), (-1, -1), 0.3, _MUTED),
        ("ALIGN",         (0, 0), (0, -1), "CENTER"),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    elems += [tbl, PageBreak()]
    return elems


def _build_company_page(c: dict, idx: int, styles: dict) -> list:
    elems: list = []
    name    = _dash(c.get("company_name"))
    country = _dash(c.get("country"))
    e       = c.get("enriched", {})

    # ── 헤더 ──────────────────────────────────────────────────────────────
    hdr_data = [[
        Paragraph(
            f"{idx}.  {name}",
            ParagraphStyle("hdr", fontSize=14, textColor=_NAVY,
                           fontName=_FONT_BOLD_NAME, leading=18),
        ),
        Paragraph(
            f"{country}  ·  {_dash(c.get('category'))}",
            ParagraphStyle("hdr_r", fontSize=9, textColor=_MUTED,
                           fontName=_FONT, leading=12),
        ),
    ]]
    hdr_tbl = Table(hdr_data, colWidths=[120*mm, 65*mm])
    hdr_tbl.setStyle(TableStyle([
        ("VALIGN",        (0, 0), (-1, -1), "BOTTOM"),
        ("LINEBELOW",     (0, 0), (-1, 0), 1.5, _NAVY),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 4),
    ]))
    elems += [hdr_tbl, Spacer(1, 3*mm)]

    # ── 기업 개요 ─────────────────────────────────────────────────────────
    overview = _dash(e.get("company_overview_kr"))
    if overview != "-":
        elems.append(Paragraph("기업 개요", styles["section"]))
        elems.append(Paragraph(overview, styles["overview"]))
        elems.append(Spacer(1, 2*mm))

    # ── 추천 이유 (강조 박스) ─────────────────────────────────────────────
    reason = _dash(e.get("recommendation_reason"))
    if reason != "-":
        elems.append(Paragraph("추천 이유", styles["section"]))
        elems.append(Paragraph(reason, styles["reason"]))
        elems.append(Spacer(1, 3*mm))

    # ── 기본 정보 ─────────────────────────────────────────────────────────
    elems.append(Paragraph("기본 정보", styles["section"]))

    website_val = _dash(c.get("website"))
    if website_val != "-":
        website_cell = Paragraph(
            f'<a href="{website_val}"><u>{website_val}</u></a>',
            styles["link"],
        )
    else:
        website_cell = Paragraph("-", styles["body"])

    info_rows = [
        [Paragraph("주소",     styles["small"]), Paragraph(_dash(c.get("address")),  styles["body"]),
         Paragraph("부스",     styles["small"]), Paragraph(_dash(c.get("booth")),    styles["body"])],
        [Paragraph("전화",     styles["small"]), Paragraph(_dash(c.get("phone")),    styles["body"]),
         Paragraph("팩스",     styles["small"]), Paragraph(_dash(c.get("fax")),      styles["body"])],
        [Paragraph("이메일",   styles["small"]), Paragraph(_dash(c.get("email")),    styles["body"]),
         Paragraph("설립연도", styles["small"]), Paragraph(_dash(e.get("founded")), styles["body"])],
        [Paragraph("웹사이트", styles["small"]), website_cell,
         Paragraph("",         styles["small"]), Paragraph("",                       styles["body"])],
    ]
    info_tbl = Table(info_rows, colWidths=[22*mm, 68*mm, 22*mm, 68*mm])
    info_tbl.setStyle(TableStyle([
        ("FONTSIZE",      (0, 0), (-1, -1), 8),
        ("ROWBACKGROUNDS",(0, 0), (-1, -1), [_LIGHT, _WHITE]),
        ("GRID",          (0, 0), (-1, -1), 0.2, _MUTED),
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
    ]))
    elems += [info_tbl, Spacer(1, 3*mm)]

    # ── 기업 규모 ─────────────────────────────────────────────────────────
    territories = ", ".join(e.get("territories", [])) or "-"
    elems.append(Paragraph("기업 규모", styles["section"]))
    size_rows = [
        [Paragraph("연 매출",   styles["small"]), Paragraph(_dash(e.get("revenue")),   styles["body"]),
         Paragraph("임직원 수", styles["small"]), Paragraph(_dash(e.get("employees")), styles["body"])],
        [Paragraph("사업 지역", styles["small"]), Paragraph(territories,               styles["body"]),
         Paragraph("",          styles["small"]), Paragraph("",                         styles["body"])],
    ]
    size_tbl = Table(size_rows, colWidths=[22*mm, 68*mm, 22*mm, 68*mm])
    size_tbl.setStyle(TableStyle([
        ("FONTSIZE",      (0, 0), (-1, -1), 8),
        ("ROWBACKGROUNDS",(0, 0), (-1, -1), [_LIGHT, _WHITE]),
        ("GRID",          (0, 0), (-1, -1), 0.2, _MUTED),
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
    ]))
    elems += [size_tbl, Spacer(1, 3*mm)]

    # ── 역량 · 실적 / 채널 · 파트너십 ────────────────────────────────────
    elems.append(Paragraph("역량 · 실적 · 채널", styles["section"]))
    cap_rows = [
        ["GMP 인증",     _yn(e.get("has_gmp")),
         "수입 이력",    _yn(e.get("import_history"))],
        ["공공조달 이력", _yn(e.get("procurement_history")),
         "공공 채널",    _yn(e.get("public_channel"))],
        ["민간 채널",    _yn(e.get("private_channel")),
         "약국 체인",    _yn(e.get("has_pharmacy_chain"))],
        ["MAH 대행",     _yn(e.get("mah_capable")),
         "한국 거래 경험", _dash(e.get("korea_experience"))],
    ]
    cap_data = [
        [Paragraph(r[0], styles["small"]), Paragraph(r[1], styles["body"]),
         Paragraph(r[2], styles["small"]), Paragraph(r[3], styles["body"])]
        for r in cap_rows
    ]
    cap_tbl = Table(cap_data, colWidths=[28*mm, 62*mm, 28*mm, 62*mm])
    cap_tbl.setStyle(TableStyle([
        ("FONTSIZE",      (0, 0), (-1, -1), 8),
        ("ROWBACKGROUNDS",(0, 0), (-1, -1), [_LIGHT, _WHITE]),
        ("GRID",          (0, 0), (-1, -1), 0.2, _MUTED),
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    elems += [cap_tbl, Spacer(1, 3*mm)]

    # ── CPHI 등록 제품 ────────────────────────────────────────────────────
    cphi_prods = c.get("products_cphi", [])
    if cphi_prods:
        elems.append(Paragraph("CPHI 등록 제품", styles["section"]))
        elems.append(Paragraph(" / ".join(cphi_prods[:15]), styles["small"]))
        elems.append(Spacer(1, 2*mm))

    # ── 참조 출처 ─────────────────────────────────────────────────────────
    src_urls = e.get("source_urls", [])
    if src_urls:
        elems.append(Paragraph("참조 출처", styles["section"]))
        for url in src_urls[:5]:
            elems.append(Paragraph(
                f'• <a href="{url}"><u>{url}</u></a>',
                styles["link"],
            ))

    elems.append(PageBreak())
    return elems


def build_buyer_pdf(
    companies: list[dict[str, Any]],
    product_label: str,
    out_path: Path,
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=18*mm,
        rightMargin=18*mm,
        topMargin=16*mm,
        bottomMargin=16*mm,
    )
    styles = _styles()
    elems: list = []
    elems += _build_cover(product_label, len(companies), styles)
    if companies:
        elems += _build_summary_table(companies, styles)
        for i, c in enumerate(companies, 1):
            elems += _build_company_page(c, i, styles)
    doc.build(elems)
