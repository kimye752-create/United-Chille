#!/usr/bin/env python3
"""싱가포르 시장 분석 보고서 생성기 (Supabase 기반).

출력 형식:
  reports/sg_report_YYYYMMDD_HHMMSS.json  — 전체 데이터 (기계 판독용)
  reports/sg_report_YYYYMMDD_HHMMSS.pdf   — 양식 기준 보고서 (사람 판독용)

PDF 구조 (품목별 2페이지):
  페이지1: 회사명·제목·제품 바·1 판정·2 근거(시장/규제/무역+PBS 참고가)·3 전략(채널·가격·리스크)
  페이지2: 4 근거·출처(논문·출처 요약 표·DB/기관)

실행:
  python report_generator.py
  python report_generator.py --out reports/
  python report_generator.py --analysis-json path/to/analysis.json  (분석 결과 주입)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=False)
except ImportError:
    pass

# ── 8개 품목 기대 product_id ──────────────────────────────────────────────────

_EXPECTED_PRODUCTS = [
    "SG_omethyl_omega3_2g",
    "SG_sereterol_activair",
    "SG_hydrine_hydroxyurea_500",
    "SG_gadvoa_gadobutrol_604",
    "SG_rosumeg_combigel",
    "SG_atmeg_combigel",
    "SG_ciloduo_cilosta_rosuva",
    "SG_gastiin_cr_mosapride",
]

_TRADE_NAMES = {
    "SG_hydrine_hydroxyurea_500": "Hydrine",
    "SG_gadvoa_gadobutrol_604": "Gadvoa Inj.",
    "SG_sereterol_activair": "Sereterol Activair",
    "SG_omethyl_omega3_2g": "Omethyl",
    "SG_rosumeg_combigel": "Rosumeg Combigel",
    "SG_atmeg_combigel": "Atmeg Combigel",
    "SG_ciloduo_cilosta_rosuva": "Ciloduo",
    "SG_gastiin_cr_mosapride": "Gastiin CR",
}

_INN_NAMES = {
    "SG_hydrine_hydroxyurea_500": "Hydroxyurea 500mg",
    "SG_gadvoa_gadobutrol_604": "Gadobutrol 604.72mg",
    "SG_sereterol_activair": "Fluticasone / Salmeterol",
    "SG_omethyl_omega3_2g": "Omega-3-Acid Ethyl Esters 90 2g",
    "SG_rosumeg_combigel": "Rosuvastatin + Omega-3-EE90",
    "SG_atmeg_combigel": "Atorvastatin + Omega-3-EE90",
    "SG_ciloduo_cilosta_rosuva": "Cilostazol + Rosuvastatin",
    "SG_gastiin_cr_mosapride": "Mosapride Citrate",
}

# ── HS 코드 및 패키징 정보 ─────────────────────────────────────────────────────

_HS_CODES: dict[str, str] = {
    "SG_omethyl_omega3_2g":       "3004.90",  # 개량신약
    "SG_sereterol_activair":      "3004.90",  # 일반제 (흡입제)
    "SG_hydrine_hydroxyurea_500": "3004.90",  # 항암제
    "SG_gadvoa_gadobutrol_604":   "3006.30",  # 조영제
    "SG_rosumeg_combigel":        "3004.90",  # 개량신약
    "SG_atmeg_combigel":          "3004.90",  # 개량신약
    "SG_ciloduo_cilosta_rosuva":  "3004.90",  # 개량신약
    "SG_gastiin_cr_mosapride":    "3004.90",  # 개량신약
}

_PACKAGING: dict[str, str] = {
    "SG_omethyl_omega3_2g":       "Omega-3-Acid Ethyl Esters 90 / 2g / Pouch",
    "SG_sereterol_activair":      "Fluticasone 250μg·500μg + Salmeterol 50μg / Inhaler",
    "SG_hydrine_hydroxyurea_500": "Hydroxyurea 500mg / Cap.",
    "SG_gadvoa_gadobutrol_604":   "Gadobutrol 604.72mg / PFS 5mL·7.5mL",
    "SG_rosumeg_combigel":        "Rosuvastatin 5·10mg + Omega-3-EE90 1g / Cap.",
    "SG_atmeg_combigel":          "Atorvastatin 10mg + Omega-3-EE90 1g / Cap.",
    "SG_ciloduo_cilosta_rosuva":  "Cilostazol 200mg + Rosuvastatin 10·20mg / Tab.",
    "SG_gastiin_cr_mosapride":    "Mosapride Citrate 15mg / Tab.",
}

# verdict 기반 확률 매핑 — 하드코딩 수치 제거
_VERDICT_TO_PROB: dict[str | None, float] = {
    "적합":   0.80,
    "조건부": 0.50,
    "부적합": 0.15,
    None:     0.00,
}

def _get_success_prob(verdict: str | None) -> float:
    return _VERDICT_TO_PROB.get(verdict, 0.00)

# 품목별 관련 사이트 (양식 §1) — 가격/GeBIZ 제거
_RELATED_SITES: dict[str, dict[str, list[tuple[str, str]]]] = {
    pid: {
        "public": [
            ("HSA eService Portal", "https://www.hsa.gov.sg/e-services"),
            ("MOH Singapore", "https://www.moh.gov.sg"),
            ("data.gov.sg", "https://data.gov.sg"),
        ],
        "private": [],
        "papers": [
            ("PubMed Central", "https://www.ncbi.nlm.nih.gov/pmc"),
            ("싱가포르 보건부 Clinical Practice Guidelines",
             "https://www.moh.gov.sg/resources-statistics/guidelines-and-guidelines"),
        ],
    }
    for pid in _EXPECTED_PRODUCTS
}


# ── 데이터 로드 ───────────────────────────────────────────────────────────────

def load_products() -> list[dict]:
    """Supabase products 테이블에서 KUP 싱가포르 품목을 조회."""
    from utils.db import fetch_kup_products
    return fetch_kup_products("SG")


# ── 보고서 데이터 조합 ────────────────────────────────────────────────────────

def build_report(
    products: list[dict],
    generated_at: str,
    analysis: list[dict] | None = None,
    references: dict[str, list[dict[str, str]]] | None = None,
) -> dict:
    # product_key(사람이 읽는 식별자)로 인덱싱 — _EXPECTED_PRODUCTS와 동일한 키 체계
    by_pid: dict[str, dict] = {p.get("product_key") or p["product_id"]: p for p in products}
    analysis_by_pid: dict[str, dict] = (
        {a["product_id"]: a for a in analysis} if analysis else {}
    )
    refs_by_pid: dict[str, list] = references or {}

    items = []
    if analysis:
        ordered = [a.get("product_id", "") for a in analysis if a.get("product_id")]
        target_pids = [pid for pid in _EXPECTED_PRODUCTS if pid in ordered]
        for pid in ordered:
            if pid not in target_pids:
                target_pids.append(pid)
    else:
        target_pids = list(_EXPECTED_PRODUCTS)
    total = len(target_pids)

    for pid in target_pids:
        row = by_pid.get(pid)
        trade = _TRADE_NAMES.get(pid, pid)
        inn = _INN_NAMES.get(pid, "")
        ana = analysis_by_pid.get(pid, {})

        if row:
            item: dict[str, Any] = {
                "product_id": pid,
                "trade_name": row.get("trade_name") or trade,
                "inn_label": inn,
                "market_segment": row.get("market_segment"),
                "regulatory_id": row.get("regulatory_id"),
                "db_confidence": row.get("confidence"),
                "status": "loaded",
            }
        else:
            item = {
                "product_id": pid,
                "trade_name": trade,
                "inn_label": inn,
                "market_segment": None,
                "regulatory_id": None,
                "db_confidence": None,
                "status": "not_loaded",
            }

        # 분석 결과 병합
        verdict = ana.get("verdict")
        item["verdict"] = verdict                      # None = API 미설정
        item["verdict_en"] = ana.get("verdict_en")
        item["rationale"] = ana.get("rationale", "")
        item["basis_market_medical"] = ana.get("basis_market_medical", "")
        item["basis_regulatory"] = ana.get("basis_regulatory", "")
        item["basis_trade"] = ana.get("basis_trade", "")
        item["key_factors"] = ana.get("key_factors", [])
        item["entry_pathway"] = ana.get("entry_pathway", "")
        item["price_positioning_pbs"] = ana.get("price_positioning_pbs", "")
        item["pbs_listing_url"] = ana.get("pbs_listing_url")
        item["pbs_schedule_drug_name"] = ana.get("pbs_schedule_drug_name")
        item["pbs_pack_description"] = ana.get("pbs_pack_description")
        item["pbs_dpmq_aud"] = ana.get("pbs_dpmq_aud")
        item["pbs_dpmq_sgd_hint"] = ana.get("pbs_dpmq_sgd_hint")
        item["pbs_methodology_label_ko"] = ana.get("pbs_methodology_label_ko") or "(PBS, 방법론적 추산)"
        item["pbs_search_hit"] = ana.get("pbs_search_hit")
        item["pbs_fetch_error"] = ana.get("pbs_fetch_error")
        item["risks_conditions"] = ana.get("risks_conditions", "")
        item["hsa_reg"] = ana.get("hsa_reg", "")
        item["product_type"] = ana.get("product_type", "")
        item["analysis_sources"] = ana.get("sources", [])
        item["analysis_model"] = ana.get("analysis_model", "")
        item["analysis_error"] = ana.get("analysis_error")
        item["claude_model_id"] = ana.get("claude_model_id", "")
        item["claude_error_detail"] = ana.get("claude_error_detail")
        item["success_prob"] = _get_success_prob(verdict)

        # ── 관련 사이트 — DB 소스 + Perplexity 논문 ────────────────────────────
        base_sites = _RELATED_SITES.get(pid, {"public": [], "private": [], "papers": []})

        # Perplexity 논문 결과가 있으면 우선 사용, 없으면 기본값 유지
        paper_refs = refs_by_pid.get(pid, [])
        if paper_refs:
            papers_list = [
                {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "summary_ko": r.get("reason", ""),
                    "source": r.get("source", ""),
                }
                for r in paper_refs
                if r.get("title") and r.get("url")
            ]
        else:
            papers_list = [
                {"title": name, "url": url, "summary_ko": "기본 참고 출처"}
                for name, url in base_sites.get("papers", [])
            ]

        # DB에서 수집된 소스 URL로 공공/민간 사이트 보강
        public_extra: list[tuple[str, str]] = []
        private_extra: list[tuple[str, str]] = []
        if row:
            src_name = row.get("source_name", "")
            src_url = row.get("source_url", "")
            src_tier = row.get("source_tier", 4)
            if src_name and src_url and src_url not in ("", "—"):
                label = src_name.replace("_", " ").title()
                if src_tier <= 2:
                    public_extra.append((label, src_url))
                else:
                    private_extra.append((label, src_url))

        item["related_sites"] = {
            "public":  base_sites.get("public", []) + public_extra,
            "private": base_sites.get("private", []) + private_extra,
            "papers":  papers_list,
        }

        # DB/기관별 정적 설명 매핑 — 이름 키워드 기반으로 적절한 설명 선택
        _DB_DESC_MAP: dict[str, str] = {
            "SG:kup_pipeline":          "KU Pharma 내부 파이프라인 DB — 제품 식별자·시장 세그먼트·규제 ID·신뢰도 점수 보유",
            "Supabase Database":        "KU Pharma 내부 Supabase DB — 제품별 시장 세그먼트·규제 식별자·신뢰도 점수 관리",
            "KU Pharma Pipeline":       "KU Pharma 내부 Supabase DB — 제품별 시장 세그먼트·규제 식별자·신뢰도 점수 관리",
            "HSA Therapeutic":          "싱가포르 HSA 공식 의약품 등록 DB — 등록 번호·승인일·성분명·레퍼런스 제품 정보 조회",
            "HSA Singapore":            "싱가포르 HSA 공식 의약품 등록 DB — 등록 번호·승인일·성분명·레퍼런스 제품 정보 조회",
            "PBS Public Schedule":      "호주 PBS 공개 스케줄 — DPMQ 기준 약가 및 등재 여부(미등재 204 포함). 싱가포르 약가와 직접 동일시 불가",
            "PBS Australia":            "호주 PBS 공개 스케줄 — DPMQ 기준 약가 및 등재 여부(미등재 204 포함). 싱가포르 약가와 직접 동일시 불가",
            "GeBIZ":                    "싱가포르 정부 공공조달 플랫폼(GeBIZ) — 발주기관별 tender 이력·낙찰 품목·의약품 수요 규모 조회",
            "Perplexity":               "Perplexity 실시간 규제 검색 — HSA 최신 공지·임상 가이드라인·학술 논문 링크 실시간 보완",
            "NDF":                      "싱가포르 국가 약제 포뮬러리(NDF) — 공립 병원 처방 허용 목록·포뮬러리 등재 현황 조회",
        }

        def _resolve_db_desc(name: str) -> str:
            for keyword, desc in _DB_DESC_MAP.items():
                if keyword.lower() in name.lower():
                    return desc
            return "분석에 참조된 데이터 출처"

        used_data_sources: list[dict[str, str]] = []
        if row:
            src_name = str(row.get("source_name", "") or "")
            src_url = str(row.get("source_url", "") or "")
            if src_name:
                used_data_sources.append(
                    {
                        "name": src_name,
                        "description": _resolve_db_desc(src_name),
                        "url": src_url,
                    }
                )
        for s in item.get("analysis_sources", []) or []:
            if not isinstance(s, dict):
                continue
            name = str(s.get("name", "") or "").strip()
            url = str(s.get("url", "") or "").strip()
            if not name:
                continue
            if any(d["name"] == name and d.get("url", "") == url for d in used_data_sources):
                continue
            if "korea united" in name.lower():
                continue
            used_data_sources.append(
                {
                    "name": name,
                    "description": _resolve_db_desc(name),
                    "url": url,
                }
            )
        pbs_url = item.get("pbs_listing_url")
        if isinstance(pbs_url, str) and pbs_url.strip():
            if not any(
                d.get("url", "") == pbs_url.strip() for d in used_data_sources
            ):
                used_data_sources.append(
                    {
                        "name": "PBS Australia",
                        "description": _resolve_db_desc("PBS Australia"),
                        "url": pbs_url.strip(),
                    }
                )
        item["used_data_sources"] = used_data_sources

        items.append(item)

    verdict_counts = {
        "적합": sum(1 for it in items if it.get("verdict") == "적합"),
        "조건부": sum(1 for it in items if it.get("verdict") == "조건부"),
        "부적합": sum(1 for it in items if it.get("verdict") == "부적합"),
        "미분석": sum(1 for it in items if it.get("verdict") is None),
    }

    return {
        "meta": {
            "generated_at": generated_at,
            "country": "SG",
            "currency": "SGD",
            "total_products": total,
            "verdict_summary": verdict_counts,
            "data_sources": [
                "HSA (Supabase)",
                "WHO EML (Supabase)",
                "GLOBOCAN (Supabase)",
                "규제 PDF",
                "Perplexity API",
                "PBS Australia (공개 스케줄, 방법론적 추산)",
            ],
            "reference_pricing": {
                "primary_label": "(PBS, 방법론적 추산)",
                "aud_field": "pbs_dpmq_aud (DPMQ)",
                "sgd_note": "pbs_dpmq_sgd_hint 는 참고 환산(환율 변동)",
            },
            "note": (
                "싱가포르 공개 병원·소매 약가는 본 파이프라인에서 직접 수집하지 않습니다. "
                "호주 PBS 공개 스케줄의 DPMQ를 (PBS, 방법론적 추산)으로 표기해 참고합니다."
            ),
        },
        "products": items,
    }


# ── PDF 렌더링 ────────────────────────────────────────────────────────────────

_FONT_CACHE: str | None = None


def _register_korean_font() -> str:
    """한글 지원 폰트를 등록하고 폰트명을 반환. 등록 실패 시 Helvetica 반환.

    결과를 모듈 레벨에 캐싱하므로 여러 번 호출해도 파일시스템 탐색은 최초 1회만 수행.
    """
    global _FONT_CACHE
    if _FONT_CACHE is not None:
        return _FONT_CACHE

    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfbase.ttfonts import TTFont

    candidates = [
        # Render/Linux 배포환경 — download_fonts.py 가 빌드 시 받아놓은 파일
        ("NanumGothic",  str(ROOT / "fonts" / "NanumGothic.ttf")),
        # macOS 시스템 폰트
        ("AppleGothic",  "/System/Library/Fonts/Supplemental/AppleGothic.ttf"),
        ("AppleGothic",  "/Library/Fonts/AppleGothic.ttf"),
        ("NanumGothic",  "/Library/Fonts/NanumGothic.ttf"),
        # Windows
        ("MalgunGothic", "C:/Windows/Fonts/malgun.ttf"),
    ]
    for name, path in candidates:
        if Path(path).is_file():
            try:
                pdfmetrics.registerFont(TTFont(name, path))
                pdfmetrics.registerFont(TTFont(f"{name}-Bold", path))
                _FONT_CACHE = name
                return name
            except Exception:
                continue
    try:
        # ReportLab 내장 CID 폰트 폴백(시스템 TTF 없어도 한글 표시 가능)
        pdfmetrics.registerFont(UnicodeCIDFont("HYSMyeongJo-Medium"))
        _FONT_CACHE = "HYSMyeongJo-Medium"
        return "HYSMyeongJo-Medium"
    except Exception:
        pass
    _FONT_CACHE = "Helvetica"
    return "Helvetica"


def render_pdf(report: dict, out_path: Path) -> None:
    """보고서 데이터를 2페이지 템플릿(회사 헤더·제품 바·번호 섹션·PBS 가격)으로 PDF 저장."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    W, _H = A4
    MARGIN = 20 * mm
    CONTENT_W = W - 2 * MARGIN

    base_font = _register_korean_font()
    bold_font = f"{base_font}-Bold"
    if base_font == "HYSMyeongJo-Medium":
        bold_font = base_font

    # ── 템플릿 색상 ────────────────────────────────────────────────────────────
    C_NAVY   = colors.HexColor("#1B2A4A")
    C_GREEN  = colors.HexColor("#27AE60")
    C_BODY   = colors.HexColor("#1A1A1A")
    C_BORDER = colors.HexColor("#D0D7E3")
    C_ALT    = colors.HexColor("#F4F6F9")

    COL1 = CONTENT_W * 0.26
    COL2 = CONTENT_W * 0.74

    def ps(name: str, **kw) -> ParagraphStyle:
        return ParagraphStyle(name, **kw)

    s_title = ps(
        "Title",
        fontName=bold_font,
        fontSize=18,
        leading=24,
        alignment=TA_CENTER,
        textColor=C_NAVY,
        spaceAfter=4,
    )
    s_date = ps(
        "Date",
        fontName=base_font,
        fontSize=10,
        leading=13,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#6B7280"),
    )
    s_section = ps(
        "Section",
        fontName=bold_font,
        fontSize=11,
        textColor=C_NAVY,
        leading=15,
        spaceBefore=8,
        spaceAfter=4,
    )
    s_cell_h = ps("CellH", fontName=bold_font, fontSize=9, textColor=C_NAVY, leading=13, wordWrap="CJK")
    s_cell = ps("Cell", fontName=base_font, fontSize=9, textColor=C_BODY, leading=14, wordWrap="CJK")
    s_bar = ps(
        "Bar",
        fontName=bold_font,
        fontSize=9,
        textColor=colors.white,
        leading=13,
        wordWrap="CJK",
    )
    s_hdr = ps(
        "HdrWhite",
        fontName=bold_font,
        fontSize=9,
        textColor=colors.white,
        leading=13,
        wordWrap="CJK",
    )
    s_cell_sm = ps(
        "CellSm",
        fontName=base_font,
        fontSize=7,
        textColor=colors.HexColor("#6B7280"),
        leading=10,
        wordWrap="CJK",
    )

    def _rx(text: str) -> str:
        return (
            (text or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    def _trunc(text: str, limit: int = 800) -> str:
        """텍스트를 limit자로 잘라 ReportLab 레이아웃 무한루프를 방지."""
        s = (text or "").strip()
        return s if len(s) <= limit else s[:limit] + "…"

    def _clean_prose(text: str) -> str:
        """AI 생성 텍스트에서 불릿/줄바꿈 아티팩트를 제거해 깔끔한 산문으로 변환."""
        import re
        s = (text or "").strip()
        if not s:
            return s
        # 줄 단위로 쪼개서 각 줄의 앞 불릿 마커 제거
        lines = s.splitlines()
        cleaned: list[str] = []
        for line in lines:
            line = line.strip()
            # "- ", "• ", "* ", "· " 등 앞부분 불릿 마커 제거
            line = re.sub(r'^[\-\•\*\·]\s+', '', line)
            # "1. ", "2. " 등 번호 목록 마커 제거
            line = re.sub(r'^\d+[\.\)]\s+', '', line)
            if line:
                cleaned.append(line)
        # 문장이 이미 마침표로 끝나면 그냥 공백으로 이어 붙임
        # 마침표 없이 끊긴 줄은 콤마+공백으로 이어 자연스러운 문장 유지
        result_parts: list[str] = []
        for part in cleaned:
            if result_parts and not result_parts[-1].rstrip().endswith(('.', '!', '?', '다', '음', '임')):
                result_parts.append(', ' + part)
            else:
                result_parts.append((' ' if result_parts else '') + part)
        joined = ''.join(result_parts).strip()
        # 이중 공백 정리
        joined = re.sub(r'  +', ' ', joined)
        return joined

    def _para(text: str, style) -> "Paragraph":
        """텍스트를 정리한 뒤 Paragraph 객체로 반환. \n → <br/> 변환 포함."""
        cleaned = _clean_prose(text)
        escaped = _rx(cleaned)
        return Paragraph(escaped, style)

    def _pbs_one_line(p: dict[str, Any]) -> str:
        aud = p.get("pbs_dpmq_aud")
        sgd = p.get("pbs_dpmq_sgd_hint")
        if isinstance(aud, (int, float)):
            line = f"DPMQ AUD {aud:.2f}"
            if isinstance(sgd, (int, float)):
                line += f", 참고 SGD {sgd:.2f}"
            line += " (PBS, 방법론적 추산 — 싱가포르 약가 아님)"
            return line
        haiku = str(p.get("pbs_haiku_estimate") or "").strip()
        if haiku:
            return haiku
        return "PBS 미등재 — 국제 가격 벤치마크 수집 후 산출 예정"

    def _triple_table(rows: list[tuple[str, str, str]]) -> Table:
        w1, w2, w3 = CONTENT_W * 0.28, CONTENT_W * 0.14, CONTENT_W * 0.58
        pdata = [
            [
                Paragraph(_rx(a), s_cell_h),
                Paragraph(_rx(b), s_cell),
                Paragraph(_rx(c), s_cell),
            ]
            for a, b, c in rows
        ]
        t = Table(pdata, colWidths=[w1, w2, w3])
        t.setStyle(TableStyle(_base_style()))
        return t

    def _base_style(extra: list | None = None) -> list:
        cmds = [
            ("GRID",   (0, 0), (-1, -1), 0.5, C_BORDER),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING",    (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING",   (0, 0), (-1, -1), 8),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 8),
        ]
        if extra:
            cmds.extend(extra)
        return cmds

    def _simple_table(rows: list[list[str]], *, shade_alt: bool = True) -> Table:
        pdata = [
            [Paragraph(_rx(r[0]), s_cell_h), Paragraph(_rx(r[1]), s_cell)]
            for r in rows
        ]
        t = Table(pdata, colWidths=[COL1, COL2])
        extras: list[tuple[Any, ...]] = []
        if shade_alt:
            for i in range(len(rows)):
                if i % 2 == 1:
                    extras.append(("BACKGROUND", (0, i), (-1, i), C_ALT))
        t.setStyle(TableStyle(_base_style(extras)))
        return t

    def _fmt_date(raw: str) -> str:
        try:
            return datetime.fromisoformat(raw).strftime("%Y-%m-%d")
        except Exception:
            return raw[:10]

    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN,  bottomMargin=MARGIN,
        title="싱가포르 1공정 시장조사 보고서",
    )

    story: list = []

    for idx, product in enumerate(report["products"]):
        generated_date = _fmt_date(report.get("meta", {}).get("generated_at", ""))
        trade = str(product.get("trade_name", "") or "—")
        inn = str(product.get("inn_label", "") or "—")
        verdict = str(product.get("verdict", "") or "미분석")

        # 1페이지 — 제목 + 제품 바
        story.append(Paragraph(_rx("싱가포르 시장 분석 보고서"), s_title))
        story.append(Paragraph(_rx(generated_date), s_date))
        story.append(Spacer(1, 6))

        pid = str(product.get("product_id", ""))
        hs_code = _HS_CODES.get(pid, "3004.90")
        bar_txt = f"{trade} — {inn}  |  HS CODE: {hs_code}"
        bar_tbl = Table([[Paragraph(_rx(bar_txt), s_bar)]], colWidths=[CONTENT_W])
        bar_tbl.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#4B5563")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 10),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ]
            )
        )
        story.append(bar_tbl)
        story.append(Spacer(1, 10))

        story.append(Paragraph(_rx("1. 진출 적합 판정"), s_section))
        story.append(
            _simple_table([["판정", verdict]], shade_alt=False),
        )
        story.append(Spacer(1, 6))

        story.append(Paragraph(_rx("2. 판정 근거"), s_section))
        pbs_line = _pbs_one_line(product)

        def _prose_row(label: str, field: str, fallback: str = "—") -> list:
            raw = str(product.get(field, "") or "").strip() or fallback
            return [Paragraph(_rx(label), s_cell_h), _para(_trunc(raw), s_cell)]

        basis_tbl_data = [
            _prose_row("시장 / 의료", "basis_market_medical"),
            _prose_row("규제",       "basis_regulatory"),
            _prose_row("무역",       "basis_trade"),
            [Paragraph(_rx("참고 가격"), s_cell_h), _para(pbs_line, s_cell)],
        ]
        extras_b: list[tuple] = []
        for i in range(len(basis_tbl_data)):
            if i % 2 == 1:
                extras_b.append(("BACKGROUND", (0, i), (-1, i), C_ALT))
        bt = Table(basis_tbl_data, colWidths=[COL1, COL2])
        bt.setStyle(TableStyle(_base_style(extras_b)))
        story.append(bt)
        story.append(Spacer(1, 6))

        story.append(Paragraph(_rx("3. 시장 진출 전략"), s_section))
        price_txt = str(product.get("price_positioning_pbs", "") or "").strip() or pbs_line
        strategy_tbl_data = [
            _prose_row("진입 채널 권고", "entry_pathway"),
            [Paragraph(_rx("가격 포지셔닝"), s_cell_h), _para(price_txt, s_cell)],
            _prose_row("리스크 + 조건",  "risks_conditions"),
        ]
        extras_s: list[tuple] = []
        for i in range(len(strategy_tbl_data)):
            if i % 2 == 1:
                extras_s.append(("BACKGROUND", (0, i), (-1, i), C_ALT))
        st = Table(strategy_tbl_data, colWidths=[COL1, COL2])
        st.setStyle(TableStyle(_base_style(extras_s)))
        story.append(st)

        story.append(PageBreak())

        # 2페이지
        story.append(Paragraph(_rx("4. 근거 및 출처"), s_section))

        # ── 4-1. Perplexity 추천 논문 (표 형식) ────────────────────────────────
        story.append(Paragraph(_rx("4-1. Perplexity 추천 논문"), s_section))
        papers = product.get("related_sites", {}).get("papers", []) or []
        valid_papers = [p for p in papers if isinstance(p, dict) and (p.get("title") or p.get("url"))]

        if valid_papers:
            w_no    = CONTENT_W * 0.05
            w_title = CONTENT_W * 0.56
            w_sum   = CONTENT_W * 0.39

            paper_tbl: list[list] = [[
                Paragraph("No.", s_hdr),
                Paragraph("논문 제목 / 출처", s_hdr),
                Paragraph("한국어 요약", s_hdr),
            ]]
            extras_p: list[tuple] = [
                ("BACKGROUND", (0, 0), (-1, 0), C_NAVY),
            ]
            for i, p in enumerate(valid_papers, 1):
                title   = _trunc(str(p.get("title",     "") or ""), 200)
                url     = str(p.get("url",         "") or "")
                source  = str(p.get("source",      "") or "")
                summary = _trunc(str(p.get("summary_ko", "") or "관련성 설명 없음"), 400)

                title_lines = _rx(title)
                if source:
                    title_lines += f"<br/>[{_rx(source)}]"
                if url:
                    short_url = url[:75] + ("…" if len(url) > 75 else "")
                    title_lines += f"<br/>{_rx(short_url)}"

                paper_tbl.append([
                    Paragraph(str(i), s_cell),
                    Paragraph(title_lines, s_cell),
                    Paragraph(_rx(summary), s_cell),
                ])
                if i % 2 == 0:
                    extras_p.append(("BACKGROUND", (0, i), (-1, i), C_ALT))

            pt = Table(paper_tbl, colWidths=[w_no, w_title, w_sum])
            pt.setStyle(TableStyle(_base_style(extras_p)))
            story.append(pt)
        else:
            story.append(_simple_table([["Perplexity 논문", "사용된 논문 링크 없음"]], shade_alt=False))

        story.append(Spacer(1, 8))

        # ── 4-2. 사용된 DB/기관 (3컬럼 표) ────────────────────────────────────
        story.append(Paragraph(_rx("4-2. 사용된 DB/기관"), s_section))
        db_sources = [
            src for src in (product.get("used_data_sources", []) or [])
            if isinstance(src, dict) and src.get("name")
        ]
        if db_sources:
            w_name = CONTENT_W * 0.25
            w_desc = CONTENT_W * 0.45
            w_link = CONTENT_W * 0.30

            db_tbl: list[list] = [[
                Paragraph("DB/기관명", s_hdr),
                Paragraph("설명", s_hdr),
                Paragraph("링크", s_hdr),
            ]]
            extras_d: list[tuple] = [
                ("BACKGROUND", (0, 0), (-1, 0), C_NAVY),
            ]
            for i, src in enumerate(db_sources, 1):
                name = str(src.get("name",        "") or "")
                desc = str(src.get("description", "") or "")
                url  = str(src.get("url",         "") or "")
                short_url = (url[:55] + "…" if len(url) > 55 else url) if url else "—"
                db_tbl.append([
                    Paragraph(_rx(name),      s_cell),
                    Paragraph(_rx(desc),      s_cell),
                    Paragraph(_rx(short_url), s_cell_sm),
                ])
                if i % 2 == 0:
                    extras_d.append(("BACKGROUND", (0, i), (-1, i), C_ALT))

            dt = Table(db_tbl, colWidths=[w_name, w_desc, w_link])
            dt.setStyle(TableStyle(_base_style(extras_d)))
            story.append(dt)
        else:
            story.append(_simple_table([["사용된 DB/기관", "이번 분석에서 확인된 DB 출처 정보 없음"]], shade_alt=False))

        if idx < len(report["products"]) - 1:
            story.append(PageBreak())

    doc.build(story)


# ── 2공정 PDF 렌더링 ──────────────────────────────────────────────────────────

def render_p2_pdf(p2_data: dict, out_path: Path) -> None:
    """2공정 수출 가격 전략 PDF 생성.

    p2_data 필드:
      product_name  : str
      verdict       : str  (적합/조건부/부적합/—)
      seg_label     : str  (공공시장/민간시장)
      base_price    : float | None  (SGD)
      formula_str   : str  (공식 텍스트)
      mode_label    : str  (직접 입력 / AI 분석)
      scenarios     : list[{label, price, reason}]
      ai_rationale  : list[str]  (AI 모드에서만 채워짐)
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    W, _H = A4
    MARGIN = 20 * mm
    CONTENT_W = W - 2 * MARGIN

    base_font = _register_korean_font()
    bold_font = f"{base_font}-Bold"
    if base_font == "HYSMyeongJo-Medium":
        bold_font = base_font

    C_NAVY   = colors.HexColor("#1B2A4A")
    C_BODY   = colors.HexColor("#1A1A1A")
    C_BORDER = colors.HexColor("#D0D7E3")
    C_ALT    = colors.HexColor("#F4F6F9")

    COL1 = CONTENT_W * 0.30
    COL2 = CONTENT_W * 0.70

    def ps(name: str, **kw) -> ParagraphStyle:
        return ParagraphStyle(name, **kw)

    def _rx(text: str) -> str:
        return (
            (text or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    s_title    = ps("P2Title",   fontName=bold_font, fontSize=18, leading=24,
                    alignment=TA_CENTER, textColor=C_NAVY, spaceAfter=4)
    s_subtitle = ps("P2Sub",     fontName=base_font, fontSize=10, leading=13,
                    alignment=TA_CENTER, textColor=colors.HexColor("#6B7280"))
    s_section  = ps("P2Section", fontName=bold_font, fontSize=11, textColor=C_NAVY,
                    leading=15, spaceBefore=10, spaceAfter=4)
    s_cell_h   = ps("P2CellH",   fontName=bold_font, fontSize=9, textColor=C_NAVY,
                    leading=13, wordWrap="CJK")
    s_cell     = ps("P2Cell",    fontName=base_font, fontSize=9, textColor=C_BODY,
                    leading=14, wordWrap="CJK")
    s_bar      = ps("P2Bar",     fontName=bold_font, fontSize=9, textColor=colors.white,
                    leading=13, wordWrap="CJK")
    s_mono     = ps("P2Mono",    fontName=base_font, fontSize=9, textColor=C_BODY,
                    leading=14, wordWrap="CJK")
    s_reason   = ps("P2Reason",  fontName=base_font, fontSize=8,
                    textColor=colors.HexColor("#374151"), leading=12, wordWrap="CJK")

    def _base_style(extra: list | None = None) -> list:
        cmds = [
            ("GRID",            (0, 0), (-1, -1), 0.5, C_BORDER),
            ("VALIGN",          (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING",      (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING",   (0, 0), (-1, -1), 5),
            ("LEFTPADDING",     (0, 0), (-1, -1), 8),
            ("RIGHTPADDING",    (0, 0), (-1, -1), 8),
        ]
        if extra:
            cmds.extend(extra)
        return cmds

    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN,  bottomMargin=MARGIN,
        title="싱가포르 2공정 수출 가격 전략 보고서",
    )

    product_name = str(p2_data.get("product_name", "") or "제품명 없음")
    verdict      = str(p2_data.get("verdict",      "") or "—")
    seg_label    = str(p2_data.get("seg_label",    "") or "—")
    base_price   = p2_data.get("base_price")
    formula_str  = str(p2_data.get("formula_str",  "") or "—")
    mode_label   = str(p2_data.get("mode_label",   "") or "—")
    scenarios    = p2_data.get("scenarios",    []) or []
    ai_rationale = p2_data.get("ai_rationale", []) or []

    from datetime import datetime, timezone as _tz_p2
    generated_date = datetime.now(_tz_p2.utc).strftime("%Y-%m-%d")
    base_str = f"SGD {base_price:,.4f}" if isinstance(base_price, (int, float)) else "—"

    story: list = []

    # ── 제목 + 제품 바 ────────────────────────────────────────────────────────
    story.append(Paragraph(_rx("싱가포르 수출 가격 전략 보고서 (2공정)"), s_title))
    story.append(Paragraph(_rx(generated_date), s_subtitle))
    story.append(Spacer(1, 6))

    bar_txt = f"{product_name}  |  판정: {verdict}  |  시장: {seg_label}"
    bar_tbl = Table([[Paragraph(_rx(bar_txt), s_bar)]], colWidths=[CONTENT_W])
    bar_tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), colors.HexColor("#4B5563")),
        ("LEFTPADDING",   (0, 0), (-1, -1), 10),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 10),
        ("TOPPADDING",    (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(bar_tbl)
    story.append(Spacer(1, 10))

    # ── 1. 원 가격 ────────────────────────────────────────────────────────────
    story.append(Paragraph(_rx("1. 원 가격 (기준 가격)"), s_section))
    base_tbl = Table([
        [Paragraph(_rx("기준 가격"), s_cell_h), Paragraph(_rx(base_str),    s_cell)],
        [Paragraph(_rx("산정 방식"), s_cell_h), Paragraph(_rx(mode_label),  s_cell)],
        [Paragraph(_rx("시장 구분"), s_cell_h), Paragraph(_rx(seg_label),   s_cell)],
    ], colWidths=[COL1, COL2])
    base_tbl.setStyle(TableStyle(_base_style([
        ("BACKGROUND", (0, 1), (-1, 1), C_ALT),
    ])))
    story.append(base_tbl)
    story.append(Spacer(1, 6))

    # ── 2. 적용한 계산 공식 ────────────────────────────────────────────────────
    story.append(Paragraph(_rx("2. 적용한 계산 공식"), s_section))
    formula_tbl = Table([
        [Paragraph(_rx("공식"), s_cell_h), Paragraph(_rx(formula_str), s_mono)],
    ], colWidths=[COL1, COL2])
    formula_tbl.setStyle(TableStyle(_base_style()))
    story.append(formula_tbl)
    story.append(Spacer(1, 6))

    # ── AI 분석 근거 (AI 모드 전용) ────────────────────────────────────────────
    if ai_rationale:
        story.append(Paragraph(_rx("AI 분석 근거"), s_section))
        rat_rows = [
            [Paragraph(_rx(f"• {line}"), s_cell)]
            for line in ai_rationale
            if str(line).strip()
        ]
        if rat_rows:
            rat_tbl = Table(rat_rows, colWidths=[CONTENT_W])
            rat_tbl.setStyle(TableStyle([
                ("GRID",          (0, 0), (-1, -1), 0.5, C_BORDER),
                ("VALIGN",        (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING",    (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING",   (0, 0), (-1, -1), 10),
                ("RIGHTPADDING",  (0, 0), (-1, -1), 10),
                ("BACKGROUND",    (0, 0), (-1, -1), C_ALT),
            ]))
            story.append(rat_tbl)
        story.append(Spacer(1, 6))

    # ── 3. 가격 시나리오 ──────────────────────────────────────────────────────
    story.append(Paragraph(_rx("3. 가격 시나리오"), s_section))

    # 시나리오 레이블 정규화 (구버전 "공격적인 시나리오" → "공격" 등 모두 처리)
    def _sc_key(lbl: str) -> str:
        lbl = str(lbl or "")
        if "공격" in lbl: return "공격"
        if "보수" in lbl: return "보수"
        return "평균"

    _SC_BG: dict[str, Any] = {
        "공격": colors.HexColor("#FEF2F2"),
        "평균": colors.HexColor("#EFF6FF"),
        "보수": colors.HexColor("#F0FDF4"),
    }
    _SC_LC: dict[str, Any] = {
        "공격": colors.HexColor("#DC2626"),
        "평균": colors.HexColor("#2563EB"),
        "보수": colors.HexColor("#16A34A"),
    }

    for sc in scenarios:
        raw_label = str(sc.get("label", sc.get("name", "")) or "")
        key       = _sc_key(raw_label)
        label     = raw_label or key
        price_val = sc.get("price") if sc.get("price") is not None else sc.get("price_sgd")
        reason    = str(sc.get("reason", "") or "—")
        formula   = str(sc.get("formula", "") or "").strip()
        price_str = (
            f"SGD {float(price_val):,.2f}" if isinstance(price_val, (int, float)) else "—"
        )
        bg = _SC_BG.get(key, C_ALT)
        lc = _SC_LC.get(key, C_NAVY)

        uid = f"{key}_{id(sc)}"
        s_sc_label = ps(f"ScL_{uid}", fontName=bold_font, fontSize=10,
                        textColor=lc, leading=14, wordWrap="CJK")
        s_sc_price = ps(f"ScP_{uid}", fontName=bold_font, fontSize=12,
                        textColor=C_NAVY, leading=16, wordWrap="CJK")
        s_sc_formula = ps(f"ScF_{uid}", fontName=bold_font,
                          fontSize=8.5, textColor=C_NAVY, leading=12, wordWrap="CJK")

        rows = [
            [Paragraph(_rx(label),     s_sc_label),
             Paragraph(_rx(price_str), s_sc_price)],
            [Paragraph(_rx("근거"),    s_cell_h),
             Paragraph(_rx(reason),    s_reason)],
        ]
        if formula:
            rows.append([
                Paragraph(_rx("계산식"), s_cell_h),
                Paragraph(_rx(formula),  s_sc_formula),
            ])

        sc_tbl = Table(rows, colWidths=[COL1, COL2])
        sc_tbl.setStyle(TableStyle([
            ("GRID",          (0, 0), (-1, -1), 0.5, C_BORDER),
            ("VALIGN",        (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING",    (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING",   (0, 0), (-1, -1), 8),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 8),
            ("BACKGROUND",    (0, 0), (-1, -1), bg),
        ]))
        story.append(sc_tbl)
        story.append(Spacer(1, 4))

    doc.build(story)


# ── 메인 ─────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="싱가포르 시장 분석 보고서 생성 (Supabase 기반)")
    parser.add_argument("--out", default=str(ROOT / "reports"))
    parser.add_argument(
        "--analysis-json",
        default=None,
        help="기존 분석 결과 JSON 파일 경로 (없으면 Claude API로 실행)",
    )
    parser.add_argument(
        "--no-perplexity",
        action="store_true",
        help="Perplexity 논문 검색 건너뜀",
    )
    args = parser.parse_args(argv)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%d_%H%M%S")
    generated_at = now.isoformat()

    # 분석 결과 로드
    analysis: list[dict] | None = None

    if args.analysis_json:
        analysis_path = Path(args.analysis_json)
        if analysis_path.exists():
            analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
            print(f"[report] 분석 결과 로드: {analysis_path} ({len(analysis)}건)")
        else:
            print(f"[report] 경고: {analysis_path} 없음 — Claude API로 실행")

    if analysis is None:
        print("[report] Claude API로 분석 실행 중... (API 키 없으면 미실행 메시지 표시)")
        from analysis.sg_export_analyzer import analyze_all
        analysis = asyncio.run(analyze_all(use_perplexity=not args.no_perplexity))
        # 분석 결과 JSON 저장
        ana_path = out_dir / f"sg_analysis_{ts}.json"
        ana_path.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[report] 분석 JSON → {ana_path}")

    # Perplexity 논문 검색
    references: dict[str, list] = {}
    if not args.no_perplexity:
        print("[report] Perplexity 논문 검색 중... (API 키 없으면 기본 사이트 사용)")
        from analysis.perplexity_references import fetch_all_references
        references = asyncio.run(fetch_all_references())
        ref_count = sum(len(v) for v in references.values())
        print(f"[report] 논문 검색 완료: {ref_count}건")

    # Supabase에서 KUP 제품 로드
    print("[report] Supabase에서 품목 데이터 로드 중...")
    products = load_products()
    print(f"[report] 품목 로드 완료: {len(products)}건")

    report = build_report(products, generated_at, analysis, references=references)

    # JSON 저장
    json_path = out_dir / f"sg_report_{ts}.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[report] JSON → {json_path}")

    # PDF 저장
    pdf_path = out_dir / f"sg_report_{ts}.pdf"
    render_pdf(report, pdf_path)
    print(f"[report] PDF  → {pdf_path}")

    meta = report["meta"]
    vs = meta.get("verdict_summary", {})
    print(
        f"\n[report] 판정 결과 — "
        f"적합: {vs.get('적합', 0)}건 / "
        f"조건부: {vs.get('조건부', 0)}건 / "
        f"부적합: {vs.get('부적합', 0)}건 / "
        f"미분석: {vs.get('미분석', 0)}건 "
        f"(총 {meta['total_products']}품목)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
