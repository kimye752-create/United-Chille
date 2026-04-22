"""칠레 1공정 시장보고서 PDF 생성기 — SG_01 양식 기준.

구조 (SG_01_시장보고서 양식 준수):
  제목: 칠레 시장보고서 — [제품명]
  헤더 바: [제품명] | HS CODE: XXXX | Chile | 날짜

  1. 의료 거시환경 파악
     - 인구, 1인당 GDP, 의약품 시장 규모, 보건 지출, 적응증 유병률, 적응증 시장 규모
     - 서술 단락 (Claude 분석 market_context)

  2. 무역/규제 환경
     ▸ ISP 등록 현황
     ▸ 진입 채널 권고 (entry_pathway)
     ▸ 관세 및 무역 (의약품 0% 관세, IVA 19%, Mercado Público)

  3. 참고 가격
     - cl_pricing DB 크롤 실측 데이터 (Cruz Verde / Salcobrand / CENABAST 등)

  4. 리스크 / 조건
     ▸ 규제 심사 소요 기간 (ISP)
     ▸ 경쟁 강도
     ▸ 포뮬러리 / 급여 등재 (FONASA · ISAPRE)

  5. 근거 및 출처
     ▸ 5-1. Perplexity 추천 논문 (No.1~3)
     ▸ 5-2. 사용된 DB/기관
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ── 칠레 거시 고정 지표 (폴백) ─────────────────────────────────────────────────
_CL_MACRO = {
    "population":  "약 1,926만 명 (INE Chile · 2024 센서스)",
    "gdp_pc":      "USD 16,785 (IMF 2024)",
    "pharma_mkt":  "USD 24.5억 (2024 추산, IQVIA)",
    "health_exp":  "GDP 대비 약 9.7% (FONASA · MINSAL 2023)",
    "import_rate": "수입 의존도 약 80.4% (CEPAL · Salud y Fármacos 2024)",
}

# ── HS 코드 매핑 ────────────────────────────────────────────────────────────────
_HS_MAP: dict[str, str] = {
    "CL_cilostazol_cr_200":      "3004.90",
    "CL_ciloduo_cilosta_rosuva": "3004.90",
    "CL_rosumeg_combigel":       "3004.90",
    "CL_atmeg_combigel":         "3004.90",
    "CL_gastiin_cr_mosapride":   "3004.90",
    "CL_omethyl_omega3_2g":      "3004.90",
}


def _font_pair() -> tuple[str, str]:
    try:
        from utils.pdf_fonts import register
        return register()
    except Exception:
        return "Helvetica", "Helvetica-Bold"


def _rx(text: str) -> str:
    """ReportLab XML 특수문자 이스케이프."""
    return (str(text or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


def render_cl_p1_pdf(
    report_data: dict[str, Any],
    out_path: Path,
    *,
    refs: list[dict[str, Any]] | None = None,
    macro_override: dict[str, str] | None = None,
) -> None:
    """SG_01 양식 기준 칠레 P1 시장보고서 PDF 생성.

    report_data 필드:
        product_id   str   (CL_xxx)
        trade_name   str
        inn          str
        verdict      str   (적합/조건부/부적합)
        rationale    str
        key_factors  list[str]
        entry_pathway str
        price_positioning str
        risks_conditions  str
        sources      list[str]
        isp_reg      str    (ISP 등록 현황 메모)
        pricing_rows list[dict]  (cl_pricing 크롤 데이터)
        hs_code      str
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        HRFlowable,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
        PageBreak,
    )

    W, _H = A4
    MARGIN = 20 * mm
    CW = W - 2 * MARGIN

    base_font, bold_font = _font_pair()

    # 색상
    C_NAVY    = colors.HexColor("#1B2A4A")
    C_BODY    = colors.HexColor("#1A1A1A")
    C_MUTED   = colors.HexColor("#6B7280")
    C_BORDER  = colors.HexColor("#D0D7E3")
    C_ALT     = colors.HexColor("#F4F6F9")
    C_BLUE_LT = colors.HexColor("#EFF6FF")
    C_GREEN   = colors.HexColor("#16A34A")
    C_RED     = colors.HexColor("#DC2626")
    C_ORANGE  = colors.HexColor("#D97706")
    C_WHITE   = colors.white

    COL1 = CW * 0.28
    COL2 = CW * 0.72

    def ps(name: str, **kw) -> ParagraphStyle:
        return ParagraphStyle(name, **kw)

    s_doc_title = ps("ClTitle",  fontName=bold_font,  fontSize=17, leading=22,
                     alignment=TA_CENTER, textColor=C_NAVY, spaceAfter=4)
    s_sub       = ps("ClSub",    fontName=base_font,  fontSize=10, leading=13,
                     alignment=TA_CENTER, textColor=C_MUTED, spaceAfter=6)
    s_bar       = ps("ClBar",    fontName=bold_font,  fontSize=9,  textColor=C_WHITE,
                     leading=13, wordWrap="CJK")
    s_section   = ps("ClSec",   fontName=bold_font,  fontSize=11, textColor=C_NAVY,
                     leading=15, spaceBefore=10, spaceAfter=4)
    s_subsec    = ps("ClSub2",   fontName=bold_font,  fontSize=9.5,textColor=C_NAVY,
                     leading=14, spaceBefore=5, spaceAfter=2, wordWrap="CJK")
    s_cell_h    = ps("ClCH",     fontName=bold_font,  fontSize=9,  textColor=C_NAVY,
                     leading=13, wordWrap="CJK")
    s_cell      = ps("ClC",      fontName=base_font,  fontSize=9,  textColor=C_BODY,
                     leading=14, wordWrap="CJK")
    s_body      = ps("ClBody",   fontName=base_font,  fontSize=9.5,textColor=C_BODY,
                     leading=15, wordWrap="CJK", spaceAfter=3)
    s_muted     = ps("ClMuted",  fontName=base_font,  fontSize=8,  textColor=C_MUTED,
                     leading=12, wordWrap="CJK")
    s_disc      = ps("ClDisc",   fontName=base_font,  fontSize=8,  textColor=C_MUTED,
                     leading=12, wordWrap="CJK", spaceAfter=4)

    def _bar(txt: str, bg=C_NAVY) -> Table:
        t = Table([[Paragraph(_rx(txt), s_bar)]], colWidths=[CW])
        t.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), bg),
            ("LEFTPADDING",   (0, 0), (-1, -1), 10),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 10),
            ("TOPPADDING",    (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        return t

    def _kv_table(rows: list[tuple[str, str]], col1=COL1, col2=COL2) -> Table:
        tbl_rows = [
            [Paragraph(_rx(k), s_cell_h), Paragraph(_rx(v), s_cell)]
            for k, v in rows
        ]
        t = Table(tbl_rows, colWidths=[col1, col2])
        cmds = [
            ("GRID",        (0, 0), (-1, -1), 0.5, C_BORDER),
            ("VALIGN",      (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING",  (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING",(0,0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING",(0, 0), (-1, -1), 8),
        ]
        for i in range(0, len(tbl_rows), 2):
            cmds.append(("BACKGROUND", (0, i), (-1, i), C_ALT))
        t.setStyle(TableStyle(cmds))
        return t

    def _hr() -> HRFlowable:
        return HRFlowable(width=CW, thickness=0.5, color=C_BORDER, spaceAfter=4)

    # 데이터 추출
    product_id   = str(report_data.get("product_id", "") or "")
    trade_name   = str(report_data.get("trade_name", "") or "제품명 없음")
    inn          = str(report_data.get("inn", "") or "")
    verdict      = str(report_data.get("verdict", "미상") or "미상")
    rationale    = str(report_data.get("rationale", "") or "")
    key_factors  = report_data.get("key_factors", []) or []
    entry_path   = str(report_data.get("entry_pathway", "") or "")
    price_pos    = str(report_data.get("price_positioning", "") or "")
    risks        = str(report_data.get("risks_conditions", "") or "")
    sources      = report_data.get("sources", []) or []
    isp_reg      = str(report_data.get("isp_reg", "") or "ISP 등록 필요 (RM 절차)")
    pricing_rows = report_data.get("pricing_rows", []) or []
    hs_code      = str(report_data.get("hs_code", "") or _HS_MAP.get(product_id, "3004.90"))

    macro = macro_override or _CL_MACRO
    gen_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # 판정 색상
    v_color = C_GREEN if verdict == "적합" else (C_RED if verdict == "부적합" else C_ORANGE)

    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN,  bottomMargin=MARGIN,
        title=f"칠레 시장보고서 — {trade_name}",
    )

    story: list = []

    # ── 제목 ─────────────────────────────────────────────────────────────────
    story.append(Paragraph(_rx(f"칠레 시장보고서 — {trade_name}"), s_doc_title))
    inn_display = f"({inn})" if inn else ""
    story.append(Paragraph(
        _rx(f"{trade_name} {inn_display}  |  HS CODE: {hs_code}  |  Chile  |  {gen_date}"),
        s_sub,
    ))
    story.append(Spacer(1, 4))

    # 제품 바 + 판정
    v_label = f"판정: {verdict}"
    story.append(_bar(f"{trade_name}  |  {gen_date}  |  {v_label}"))
    story.append(Spacer(1, 10))

    # ── 1. 의료 거시환경 파악 ────────────────────────────────────────────────
    story.append(Paragraph(_rx("1. 의료 거시환경 파악"), s_section))
    story.append(_kv_table([
        ("인구",               macro.get("population",  _CL_MACRO["population"])),
        ("1인당 GDP",          macro.get("gdp_pc",      _CL_MACRO["gdp_pc"])),
        ("의약품 시장 규모",    macro.get("pharma_mkt",  _CL_MACRO["pharma_mkt"])),
        ("보건 지출",          macro.get("health_exp",  _CL_MACRO["health_exp"])),
        ("수입 의존도",        macro.get("import_rate", _CL_MACRO["import_rate"])),
    ]))
    story.append(Spacer(1, 6))

    # 시장 서술
    market_ctx = str(report_data.get("market_context", "") or "")
    if not market_ctx:
        market_ctx = (
            f"칠레는 남미 최고 수준의 의약품 규제 인프라를 갖춘 고소득 중견국으로, "
            f"ISP(Instituto de Salud Pública) 주관 하에 의약품 등록·허가가 이루어집니다. "
            f"FONASA(공공)와 ISAPRE(민간) 이중 보험 구조를 통해 의약품이 급여되며, "
            f"공공 조달은 CENABAST 및 Mercado Público ChileCompra를 경유합니다. "
            f"3대 약국 체인(Cruz Verde · Salcobrand · Farmacias Ahumada)이 민간 유통의 약 90%를 점유합니다."
        )
    story.append(Paragraph(_rx(market_ctx), s_body))
    story.append(Spacer(1, 6))

    # ── 2. 무역/규제 환경 ───────────────────────────────────────────────────
    story.append(Paragraph(_rx("2. 무역/규제 환경"), s_section))

    story.append(Paragraph(_rx("▸ ISP 등록 현황"), s_subsec))
    story.append(Paragraph(_rx(isp_reg or "ISP 등록 이력 없음 — 신규 RM(Registro de Medicamentos) 절차 필요"), s_body))
    story.append(Spacer(1, 4))

    story.append(Paragraph(_rx("▸ 진입 채널 권고"), s_subsec))
    story.append(Paragraph(_rx(entry_path or
        "① ISP RM 등록 → ② FONASA 급여 협상(공공) 또는 ISAPRE 등재(민간) → "
        "③ CENABAST/Mercado Público 입찰(공공) 또는 Cruz Verde·Salcobrand 입점(민간)"), s_body))
    story.append(Spacer(1, 4))

    story.append(Paragraph(_rx("▸ 관세 및 무역"), s_subsec))
    story.append(_kv_table([
        ("의약품 수입 관세",  "HS 3004 기준 0% (한-칠레 FTA 적용 — Decree No. 312/2004)"),
        ("IVA (부가가치세)", "19% (의약품 포함 · CENABAST 공급분 면제 가능)"),
        ("공공 조달 플랫폼", "Mercado Público ChileCompra (www.mercadopublico.cl) — OCDS 표준"),
        ("CENABAST",         "Ley 21.198 소매 상한가 등재 품목 대상 — 공공 의료기관 의무 적용"),
    ]))
    story.append(Spacer(1, 8))

    # ── 3. 참고 가격 ────────────────────────────────────────────────────────
    story.append(Paragraph(_rx("3. 참고 가격"), s_section))
    story.append(Paragraph(
        _rx("▸ DB 크롤 실측 데이터 (Cruz Verde · Salcobrand · Farmacias Ahumada · CENABAST · Mercado Público)"),
        s_subsec,
    ))

    if pricing_rows:
        w_src  = CW * 0.20
        w_brd  = CW * 0.22
        w_inn2 = CW * 0.28
        w_pri  = CW * 0.30
        s_hdr  = ps("PrHdr", fontName=bold_font, fontSize=8.5, textColor=C_WHITE,
                    leading=12, wordWrap="CJK")
        pr_hdr = [
            Paragraph(_rx("출처"), s_hdr),
            Paragraph(_rx("브랜드명"), s_hdr),
            Paragraph(_rx("성분 (INN)"), s_hdr),
            Paragraph(_rx("가격 (CLP)"), s_hdr),
        ]
        pr_rows: list[list] = [pr_hdr]
        pr_cmds = [("BACKGROUND", (0, 0), (-1, 0), C_NAVY)]
        for i, row in enumerate(pricing_rows[:15], 1):
            retail  = row.get("raw_price_clp") or row.get("max_retail_price_clp")
            supply  = row.get("cenabast_supply_price_clp")
            awarded = row.get("awarded_price_clp")
            parts: list[str] = []
            if retail:   parts.append(f"소매 CLP {int(retail):,}")
            if supply:   parts.append(f"CENABAST CLP {int(supply):,}")
            if awarded:  parts.append(f"낙찰 CLP {int(awarded):,}")
            price_str = " / ".join(parts) if parts else "—"
            pr_rows.append([
                Paragraph(_rx(str(row.get("source", "—"))), s_cell),
                Paragraph(_rx(str(row.get("brand_name", "—") or "—")), s_cell),
                Paragraph(_rx(str(row.get("inn_name", "—")[:40])), s_cell),
                Paragraph(_rx(price_str), s_cell),
            ])
            if i % 2 == 0:
                pr_cmds.append(("BACKGROUND", (0, i), (-1, i), C_ALT))
        pr_tbl = Table(pr_rows, colWidths=[w_src, w_brd, w_inn2, w_pri])
        pr_tbl.setStyle(TableStyle([
            ("GRID",        (0, 0), (-1, -1), 0.5, C_BORDER),
            ("VALIGN",      (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING",  (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING",(0,0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING",(0, 0), (-1, -1), 6),
        ] + pr_cmds))
        story.append(pr_tbl)
    else:
        story.append(Paragraph(
            _rx("크롤 가격 데이터 없음 — 가격 수집 버튼을 눌러 실측 데이터를 수집하세요."),
            s_cell,
        ))

    story.append(Spacer(1, 4))
    story.append(Paragraph(_rx(
        "※ 칠레는 의약품 공식 약가 데이터를 CENABAST 및 Mercado Público를 통해 공개합니다. "
        "민간 소매가(Cruz Verde 등)는 CENABAST Ley 21.198 소매 상한가를 초과할 수 없습니다."
        + (f"  가격 포지셔닝 전략: {price_pos}" if price_pos else "")
    ), s_muted))
    story.append(Spacer(1, 8))

    # ── 4. 리스크 / 조건 ────────────────────────────────────────────────────
    story.append(Paragraph(_rx("4. 리스크 / 조건"), s_section))

    story.append(Paragraph(_rx("▸ 규제 심사 소요 기간"), s_subsec))
    story.append(Paragraph(
        _rx("ISP RM(신규 등록) 심사 소요 기간은 통상 12~24개월이 예상됩니다. "
            "사전 Pre-submission 미팅을 통해 필요 자료(생동성·안정성·임상)를 사전 확정하면 "
            "지연 리스크를 줄일 수 있습니다."),
        s_body,
    ))
    story.append(Spacer(1, 4))

    story.append(Paragraph(_rx("▸ 경쟁 강도"), s_subsec))
    kf_txt = ("  |  ".join(str(f) for f in key_factors[:5]) if key_factors
               else rationale[:250] if rationale else "경쟁 분석 결과 없음")
    story.append(Paragraph(_rx(kf_txt), s_body))
    story.append(Spacer(1, 4))

    story.append(Paragraph(_rx("▸ 급여 등재 (FONASA · ISAPRE)"), s_subsec))
    story.append(Paragraph(
        _rx(risks or
            "FONASA 급여 목록 포함 여부 및 ISAPRE 보험 적용 요건에 대한 추가 협의가 필요합니다. "
            "급여 등재 전까지는 자부담(out-of-pocket) 또는 민간 병원·약국 채널 접근이 현실적입니다."),
        s_body,
    ))
    story.append(Spacer(1, 8))

    # ── 5. 근거 및 출처 ─────────────────────────────────────────────────────
    story.append(Paragraph(_rx("5. 근거 및 출처"), s_section))

    # 5-1. Perplexity 논문
    story.append(Paragraph(_rx("▸ 5-1. Perplexity 추천 논문"), s_subsec))
    if refs:
        for i, ref in enumerate(refs[:3], 1):
            title_txt = str(ref.get("title", "") or ref.get("query", "") or "")
            body_txt  = str(ref.get("body",  "") or ref.get("snippet", "") or "")
            url_txt   = str(ref.get("url",   "") or "")
            story.append(Paragraph(_rx(f"No.{i}   {title_txt}"), s_cell_h))
            if body_txt:
                story.append(Paragraph(_rx(body_txt[:400]), s_body))
            if url_txt:
                story.append(Paragraph(_rx(f"출처: {url_txt}"), s_muted))
            story.append(Spacer(1, 4))
    else:
        story.append(Paragraph(_rx("논문 검색 결과 없음 (Perplexity API 미설정 또는 검색 실패)"), s_cell))

    story.append(Spacer(1, 6))

    # 5-2. DB/기관
    story.append(Paragraph(_rx("▸ 5-2. 사용된 DB/기관"), s_subsec))
    db_sources = sources or []
    default_db = [
        "ISP Chile — Instituto de Salud Pública (의약품 등록·허가 공식 DB)",
        "CENABAST — Ley 21.198 소매 상한가 및 공공 공급가 DB",
        "Mercado Público ChileCompra — 공공 조달 낙찰가 OCDS 데이터",
        "Cruz Verde / Salcobrand / Farmacias Ahumada — 민간 소매가 크롤 데이터",
        "FONASA — 공공보험 급여 목록",
        "MINSAL Chile — 의약품 정책 및 규제 지침",
    ]
    all_srcs = list(dict.fromkeys(db_sources + default_db))  # 중복 제거
    for src in all_srcs[:8]:
        story.append(Paragraph(_rx(f"•  {src}"), s_muted))

    # ── 면책 조항 ────────────────────────────────────────────────────────────
    story.append(Spacer(1, 10))
    story.append(_hr())
    story.append(Paragraph(
        _rx("※ 본 보고서는 Claude AI 분석 및 공개 DB 크롤 데이터에 기반한 참고 자료이며, "
            "최종 의사결정 전 담당자의 검토 및 현지 전문가 확인이 반드시 필요합니다."),
        s_disc,
    ))

    doc.build(story)
