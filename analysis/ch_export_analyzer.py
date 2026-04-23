"""칠레 1공정 수출 적합성 분석 엔진.

LLM 우선순위:
  1. Claude API (claude-haiku-4-5-20251001) — system prompt + user JSON 데이터 방식
  2. Perplexity API (sonar-pro)    — 불확실 판정 시 보조 검색 후 재분석
  3. 정적 폴백                     — API 미설정 시

칠레 규제 특수사항:
  - 규제 당국: ISP (Instituto de Salud Pública) — MINSAL 산하
  - 공공조달: CENABAST / Mercado Público (ChileCompra OCDS 표준)
  - 약가 통제: CENABAST Ley 21.198 소매 상한가
  - FONASA(공공) / ISAPRE(민간) 이중 보험 구조
  - VAT: 의약품 19% (환경변수 CL_VAT_PHARMA_PCT 우선)
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=True)
except ImportError:
    pass


# ── LLM 시스템 프롬프트 ───────────────────────────────────────────────────────
_CLAUDE_SYSTEM_PROMPT = (
    "당신은 한국유나이티드제약의 칠레 수출 전문 애널리스트입니다. "
    "주어진 크롤링 JSON(ISP·CENABAST·Market-Data)만 근거로, 아래 계층 필드를 "
    "한국어 존댓말('-합니다', '-습니다', '-해주시길 바랍니다')로 채웁니다.\n\n"
    "보고서 제목 형식은 「칠레 시장보고서 - {의약품명}」에 대응합니다.\n"
    "회사 표기는 '한국유나이티드제약'만 사용합니다.\n\n"
    "【데이터 원칙 — 최우선】\n"
    "- 입력 JSON은 Supabase DB(cl_products 등)에서 가져온 최신 요약값입니다. 절대 창작하지 않습니다.\n"
    "- ISP 등록번호(Registro), CENABAST 낙찰가, 경쟁사 등재 여부는 실제 데이터와 정합시킵니다.\n"
    "- 값이 없으면 '미확보(현지 파트너 협업 필요)'로 명시합니다.\n\n"
    "【양식 매핑 규칙】\n"
    "- market_overview.paragraph: 칠레 의약품 시장의 성장세와 공공-민간 이원화 구조(FONASA/ISAPRE) 맥락을 3문장 내외 요약.\n"
    "- market_overview.market_structure_tag: 공공조달(CENABAST) 중심|대형 약국체인 과점|제네릭 선호 중 해당 태그.\n"
    "- regulatory_risk.isp_paragraph: ISP 허가 요건, CPP(의약품수출증명서) 인정 여부.\n"
    "- regulatory_risk.fta_paragraph: 한국-칠레 FTA 관세 혜택(0%), HS코드 기준 적용 조건.\n"
    "- regulatory_risk.bioequivalence_paragraph: 생물학적 동등성(BE) 입증 필수 여부, 라벨링 규제.\n"
    "- operational_risk: 물류 리스크, 현지 유통망 구축 과제.\n"
    "- price_snapshot.cenabast_clp: 입력 JSON의 CENABAST 조달가를 그대로 표기 (없으면 '미확보').\n"
    "- price_snapshot.retail_clp: 입력 JSON의 소매 참고가를 그대로 표기 (없으면 '미확보').\n"
    "- price_snapshot.fta_status: FTA 관세혜택 적용 여부 한 줄 설명.\n"
    "- entry_pathway: ISP 등록 → FONASA/ISAPRE 급여 → 유통 전략 순서로 기술.\n"
    "- key_factors: 진출 성패를 좌우하는 핵심 요인 3~5개 (문자열 배열).\n"
    "- references: ISP Chile, CENABAST, ChileCompra 등 공신력 있는 출처 (문자열 배열).\n\n"
    "【출력 JSON 스키마 — 반드시 아래 형식만 출력】\n"
    "{\n"
    "  \"verdict\": {\"category\": \"가능|조건부|불가\", \"confidence\": 0.0},\n"
    "  \"market_overview\": {\"paragraph\": \"\", \"market_structure_tag\": \"\"},\n"
    "  \"regulatory_risk\": {\n"
    "    \"isp_paragraph\": \"\",\n"
    "    \"fta_paragraph\": \"\",\n"
    "    \"bioequivalence_paragraph\": \"\"\n"
    "  },\n"
    "  \"operational_risk\": \"\",\n"
    "  \"price_snapshot\": {\"cenabast_clp\": \"\", \"retail_clp\": \"\", \"fta_status\": \"\"},\n"
    "  \"entry_pathway\": \"\",\n"
    "  \"key_factors\": [],\n"
    "  \"references\": []\n"
    "}\n\n"
    "【칠레 특화 판정 기준】\n"
    "- verdict.category: 가능 = ISP 등록 경로 명확, 경쟁 우위 있음\n"
    "  조건부 = ISP 등록 가능하나 BE 시험/파트너 협업 등 전제 조건 있음\n"
    "  불가 = 규제 장벽 또는 시장 수요 부재로 진출 비권장\n\n"
    "【영어 약어 최초 노출 규칙】\n"
    "- ISP (Instituto de Salud Pública · 칠레 보건청)\n"
    "- CENABAST (Central de Abastecimiento · 칠레 공공조달청)\n"
    "- BE (Bioequivalence · 생물학적 동등성)\n"
    "- FONASA (Fondo Nacional de Salud · 칠레 국가건강보험공단)\n"
    "- ISAPRE (Instituciones de Salud Previsional · 칠레 민간건강보험)\n\n"
    "【환각 금지】 입력 JSON에 없는 숫자·코드·브랜드명·가격을 만들지 않습니다.\n"
    "【마크다운 금지】 **, #, 백틱, 링크 문법 사용하지 않습니다.\n"
    "【JSON만 출력】 코드블록 없이 순수 JSON 객체만 반환합니다.\n"
)


# ── 칠레 품목 메타데이터 ─────────────────────────────────────────────────────

_FALLBACK_PRODUCT_META: list[dict[str, str]] = [
    {
        "product_id": "CL_cilostazol_cr_200",
        "trade_name": "Cilostazol CR",
        "inn": "Cilostazol 200mg SR (서방형)",
        "dosage_form": "SR Tab",
        "market_segment": "처방전 의약품",
        "product_type": "개량신약",
        "atc": "B01AC23",
        "therapeutic_area": "심혈관계 / 말초혈관질환",
        "isp_reg": "등록 필요 — ISP 의약품 등록(RM) 절차",
        "key_risk": (
            "Pletaal(IR) 기존 경쟁, CENABAST 소매 상한가 적용 가능성, "
            "1일 1회 SR 차별성 임상 근거 ISP 제출 필수"
        ),
    },
    {
        "product_id": "CL_ciloduo_cilosta_rosuva",
        "trade_name": "Ciloduo",
        "inn": "Cilostazol 200mg SR + Rosuvastatin 10/20mg 복합제",
        "dosage_form": "SR Tab",
        "market_segment": "처방전 의약품",
        "product_type": "개량신약",
        "atc": "B01AC23 + C10AA07",
        "therapeutic_area": "심혈관계 (항혈전 + 이상지질혈증)",
        "isp_reg": "복합제 별도 ISP 등록 필요",
        "key_risk": (
            "칠레 복합제 등록 전례 소수, FONASA 급여 등재 협상 필요, "
            "Cruz Verde/Salcobrand 3대 체인 입점 협상"
        ),
    },
    {
        "product_id": "CL_rosumeg_combigel",
        "trade_name": "Rosumeg Combigel",
        "inn": "Rosuvastatin + Omega-3 에틸에스테르",
        "dosage_form": "Cap",
        "market_segment": "처방전 의약품",
        "product_type": "개량신약",
        "atc": "C10AA07 + C10AX06",
        "therapeutic_area": "이상지질혈증",
        "isp_reg": "등록 필요",
        "key_risk": "스타틴 제네릭 경쟁 심화, Omega-3 복합제 칠레 시장 인지도 미흡",
    },
    {
        "product_id": "CL_atmeg_combigel",
        "trade_name": "Atmeg Combigel",
        "inn": "Atorvastatin + Omega-3 에틸에스테르",
        "dosage_form": "Cap",
        "market_segment": "처방전 의약품",
        "product_type": "개량신약",
        "atc": "C10AA05 + C10AX06",
        "therapeutic_area": "이상지질혈증",
        "isp_reg": "등록 필요",
        "key_risk": "Lipitor 제네릭 대비 가격 경쟁력, 복합제 별도 허가 절차",
    },
    {
        "product_id": "CL_gastiin_cr_mosapride",
        "trade_name": "Gastiin CR",
        "inn": "Mosapride Citrate 15mg SR",
        "dosage_form": "SR Tab",
        "market_segment": "처방전 의약품",
        "product_type": "개량신약",
        "atc": "A03FA05",
        "therapeutic_area": "소화기계",
        "isp_reg": "등록 필요",
        "key_risk": "위장관 운동 촉진제 시장 내 Domperidone/Metoclopramide 경쟁",
    },
    {
        "product_id": "CL_omethyl_omega3_2g",
        "trade_name": "Omethyl Cutielet",
        "inn": "Omega-3 Ethyl Esters 2g",
        "dosage_form": "Pouch",
        "market_segment": "처방전 의약품",
        "product_type": "개량신약",
        "atc": "C10AX06",
        "therapeutic_area": "고중성지방혈증",
        "isp_reg": "등록 필요",
        "key_risk": "Vascepa/Lovaza 대비 포지셔닝, 처방전 Omega-3 전문약 인지도",
    },
    {
        "product_id": "CL_sereterol_activair",
        "trade_name": "Sereterol Activair",
        "inn": "Fluticasone 250/500μg + Salmeterol 50μg",
        "dosage_form": "Inhaler",
        "market_segment": "처방전 의약품",
        "product_type": "일반제",
        "atc": "R03AK06",
        "therapeutic_area": "호흡기계 / 천식·COPD",
        "isp_reg": "등록 필요",
        "key_risk": "Seretide(GSK) 오리지날 및 제네릭 경쟁, 흡입제 복약 지도 필요",
    },
    {
        "product_id": "CL_hydrine_hydroxyurea_500",
        "trade_name": "Hydrine",
        "inn": "Hydroxyurea 500mg",
        "dosage_form": "Cap",
        "market_segment": "처방전 의약품 (항암)",
        "product_type": "일반제",
        "atc": "L01XX05",
        "therapeutic_area": "혈액종양 / 겸상적혈구병",
        "isp_reg": "등록 필요 (항암제 특별 심사 경로)",
        "key_risk": "FONASA 항암제 급여 등재 필요, CENABAST 공공조달 경쟁 입찰",
    },
    {
        "product_id": "CL_gadvoa_gadobutrol_604",
        "trade_name": "Gadvoa Inj.",
        "inn": "Gadobutrol 604.72mg",
        "dosage_form": "PFS 5mL·7.5mL",
        "market_segment": "전문 의약품 (조영제)",
        "product_type": "일반제",
        "atc": "V08CA09",
        "therapeutic_area": "방사선과 / MRI 조영제",
        "isp_reg": "등록 필요 (조영제 별도 허가 경로)",
        "key_risk": "Gadovist(Bayer) 오리지날 독점, CENABAST 공공병원 입찰 경쟁",
    },
]

_meta_cache: list[dict[str, Any]] | None = None


def _get_product_meta() -> list[dict[str, Any]]:
    global _meta_cache
    if _meta_cache is not None:
        return _meta_cache
    try:
        from utils.db import fetch_kup_products
        db_rows = fetch_kup_products("CL")
        if db_rows:
            _meta_cache = db_rows
            return _meta_cache
    except Exception:
        pass
    _meta_cache = _FALLBACK_PRODUCT_META
    return _meta_cache


# ── Claude 분석 ──────────────────────────────────────────────────────────────

def _build_user_message(meta: dict[str, Any], pricing_context: str = "") -> str:
    """제품 메타 + ISP/CENABAST/Market 데이터를 JSON으로 직렬화해 user 메시지 생성.

    시스템 프롬프트가 역할·출력 형식을 정의하므로, user 메시지는 순수 데이터만 전달한다.
    """
    vat_pct = float(os.environ.get("CL_VAT_PHARMA_PCT", "19.0"))
    import_duty_pct = float(os.environ.get("CL_IMPORT_DUTY_PCT", "6.0"))

    payload: dict[str, Any] = {
        "product": {
            "product_id":       meta.get("product_id", ""),
            "trade_name":       meta.get("trade_name", ""),
            "inn":              meta.get("inn", ""),
            "dosage_form":      meta.get("dosage_form", ""),
            "atc":              meta.get("atc", ""),
            "therapeutic_area": meta.get("therapeutic_area", ""),
            "product_type":     meta.get("product_type", ""),
            "isp_reg":          meta.get("isp_reg", "등록 정보 없음"),
            "key_risk":         meta.get("key_risk", ""),
        },
        "market_context": {
            "regulator":          "ISP (Instituto de Salud Pública)",
            "price_control":      "CENABAST Ley 21.198 소매 상한가",
            "insurance_structure": "FONASA(공공, ~70%) / ISAPRE(민간, ~20%)",
            "retail_oligopoly":   "Cruz Verde / Salcobrand / Farmacias Ahumada (점유율 90%+)",
            "public_procurement": "Mercado Público (ChileCompra OCDS 표준)",
            "vat_pct":            vat_pct,
            "import_duty_pct":    import_duty_pct,
            "fta_korea_chile":    "한국-칠레 FTA: HS 3004·3006 의약품 관세 0% (FTA 적용 시)",
        },
        "pricing_data": pricing_context or "CENABAST·약국 크롤 데이터 없음 (크롤러 미실행 또는 해당 품목 미등재)",
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


async def analyze_product(
    product_id: str,
    db_row: dict[str, Any] | None = None,
    pricing_context: str = "",
) -> dict[str, Any]:
    """단일 품목 칠레 수출 적합성 분석."""
    meta_list = _get_product_meta()
    meta = next((m for m in meta_list if m.get("product_id") == product_id), None)
    if meta is None and db_row:
        meta = db_row
    if meta is None:
        meta = {"product_id": product_id, "trade_name": product_id}

    api_key = (
        os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY", "")
    ).strip()

    analyzed_at = datetime.now(timezone.utc).isoformat()

    if not api_key:
        return _static_fallback(meta, analyzed_at)

    try:
        import anthropic
        model = os.environ.get("CLAUDE_ANALYSIS_MODEL", "claude-haiku-4-5-20251001")
        client = anthropic.Anthropic(api_key=api_key)
        user_msg = _build_user_message(meta, pricing_context)

        resp = await asyncio.to_thread(
            lambda: client.messages.create(
                model=model,
                max_tokens=2000,
                system=_CLAUDE_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )
        )
        raw = resp.content[0].text.strip()
        normalized = _normalize_result(_parse_raw_json(raw), meta)
        normalized.update({
            "product_id":       product_id,
            "trade_name":       meta.get("trade_name", ""),
            "inn":              meta.get("inn", ""),
            "analyzed_at":      analyzed_at,
            "model":            model,
            "analysis_model":   model,
            "claude_model_id":  model,
        })
        return normalized

    except Exception as exc:
        result = _static_fallback(meta, analyzed_at)
        result["error"] = str(exc)
        result["claude_error_detail"] = str(exc)
        return result


def _parse_raw_json(raw: str) -> dict[str, Any]:
    """LLM 응답 텍스트에서 JSON 객체 추출."""
    import re
    # 코드블록 래퍼 제거 (```json ... ```)
    raw = re.sub(r"```(?:json)?\s*", "", raw).strip("`").strip()
    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return {}


def _normalize_result(llm: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    """새 계층형 LLM 스키마 → report_generator + server.py 기대 필드로 평탄화.

    보고서 렌더러(report_generator.py)가 사용하는 키:
      verdict, verdict_confidence, rationale,
      basis_market_medical, basis_regulatory, basis_trade,
      key_factors, entry_pathway, price_positioning_pbs,
      risks_conditions, isp_reg, sources, product_type
    서버(server.py)가 DB 저장 시 사용하는 추가 키:
      market_context, price_positioning, confidence
    """
    # ── 판정 ─────────────────────────────────────────────────────────────────
    verdict_block = llm.get("verdict") or {}
    if isinstance(verdict_block, dict):
        raw_cat    = str(verdict_block.get("category", "") or "").strip()
        confidence = float(verdict_block.get("confidence", 0.5) or 0.5)
    else:
        # 구버전 flat 스키마 폴백
        raw_cat    = str(verdict_block or llm.get("verdict", "") or "").strip()
        confidence = float(llm.get("verdict_confidence", 0.5) or 0.5)

    # 칠레 판정어 → 보고서 표준 판정어 매핑
    _VERDICT_MAP = {"가능": "적합", "조건부": "조건부", "불가": "부적합"}
    verdict = _VERDICT_MAP.get(raw_cat, raw_cat) or llm.get("verdict_flat", "조건부")
    if verdict not in ("적합", "조건부", "부적합"):
        verdict = "조건부"

    # ── 섹션 텍스트 ──────────────────────────────────────────────────────────
    mkt  = llm.get("market_overview") or {}
    reg  = llm.get("regulatory_risk") or {}
    snap = llm.get("price_snapshot") or {}

    mkt_para    = str(mkt.get("paragraph", "") or "").strip()
    mkt_tag     = str(mkt.get("market_structure_tag", "") or "").strip()
    isp_para    = str(reg.get("isp_paragraph", "") or "").strip()
    fta_para    = str(reg.get("fta_paragraph", "") or "").strip()
    be_para     = str(reg.get("bioequivalence_paragraph", "") or "").strip()
    op_risk     = str(llm.get("operational_risk", "") or "").strip()
    entry       = str(llm.get("entry_pathway", "") or "").strip()
    key_factors = list(llm.get("key_factors", []) or [])
    references  = list(llm.get("references", []) or [])

    # cenabast_clp를 숫자로 파싱 시도
    cenabast_raw  = str(snap.get("cenabast_clp", "") or "").strip()
    retail_raw    = str(snap.get("retail_clp", "") or "").strip()
    fta_status    = str(snap.get("fta_status", "") or "").strip()

    import re as _re
    def _extract_number(s: str) -> float | None:
        m = _re.search(r"[\d,]+(?:\.\d+)?", s.replace(",", ""))
        if m:
            try:
                return float(m.group(0).replace(",", ""))
            except Exception:
                pass
        return None

    cenabast_clp_val = _extract_number(cenabast_raw)
    retail_clp_val   = _extract_number(retail_raw)

    # 가격 포지셔닝 문자열 조합
    price_parts: list[str] = []
    if cenabast_raw and cenabast_raw not in ("미확보", ""):
        price_parts.append(f"CENABAST 조달가: {cenabast_raw}")
    if retail_raw and retail_raw not in ("미확보", ""):
        price_parts.append(f"소매 참고가: {retail_raw}")
    if fta_status:
        price_parts.append(f"FTA 관세: {fta_status}")
    price_positioning_str = " / ".join(price_parts) if price_parts else "CENABAST 참고가 미수집"

    # basis_regulatory 조합 (ISP + FTA)
    basis_reg_parts: list[str] = []
    if isp_para:
        basis_reg_parts.append(isp_para)
    if fta_para:
        basis_reg_parts.append(fta_para)
    basis_regulatory = " ".join(basis_reg_parts).strip() or "ISP 규제 정보 미확보"

    # basis_trade 조합 (BE + 운영 리스크)
    basis_trade_parts: list[str] = []
    if be_para:
        basis_trade_parts.append(be_para)
    if op_risk:
        basis_trade_parts.append(op_risk)
    basis_trade = " ".join(basis_trade_parts).strip() or "무역·운영 리스크 정보 미확보"

    # rationale: 판정 근거 종합 요약
    rationale = llm.get("rationale", "").strip()
    if not rationale:
        parts = [p for p in [mkt_para, isp_para, op_risk] if p]
        rationale = " ".join(parts[:3])[:600] or "AI 분석 결과 없음"

    # isp_reg: meta 우선, LLM isp_paragraph 보조
    isp_reg = (
        meta.get("isp_reg", "")
        or llm.get("isp_reg", "")
        or (isp_para[:120] if isp_para else "등록 정보 미확보")
    )

    return {
        # ── 판정 ────────────────────────────────────────────────────────────
        "verdict":              verdict,
        "verdict_confidence":   confidence,
        "confidence":           confidence,          # server.py DB 저장용 alias
        # ── 근거 텍스트 (report_generator 렌더러용) ───────────────────────
        "rationale":            rationale,
        "basis_market_medical": mkt_para,
        "basis_regulatory":     basis_regulatory,
        "basis_trade":          basis_trade,
        # ── 가격 (report_generator _pbs_one_line 대응) ───────────────────
        "price_positioning_pbs":    price_positioning_str,
        "price_positioning":        price_positioning_str,   # server.py DB alias
        "cenabast_max_price_clp":   cenabast_clp_val,
        "raw_price_clp":            retail_clp_val,
        # ── 전략 / 리스크 ────────────────────────────────────────────────
        "entry_pathway":        entry,
        "risks_conditions":     basis_trade,
        "key_factors":          key_factors,
        # ── 규제 / 구조 ──────────────────────────────────────────────────
        "isp_reg":              isp_reg,
        "hsa_reg":              isp_reg,             # report_generator legacy alias
        "market_context":       (f"{mkt_para} [{mkt_tag}]" if mkt_tag else mkt_para),
        "product_type":         meta.get("product_type", ""),
        # ── 출처 ─────────────────────────────────────────────────────────
        "sources":              references,
        "analysis_sources":     [{"name": r, "url": ""} for r in references],
        # ── 원본 LLM 출력 보존 (디버그용) ───────────────────────────────
        "_llm_raw": llm,
    }


def _static_fallback(meta: dict[str, Any], analyzed_at: str) -> dict[str, Any]:
    """API 키 없거나 오류 시 정적 결과 반환."""
    isp_reg = meta.get("isp_reg", "ISP 등록 정보 없음")
    key_risk = meta.get("key_risk", "")
    trade = meta.get("trade_name", meta.get("product_id", ""))
    price_str = "CENABAST 참고가 미수집 — 크롤러 재실행 필요"
    rationale = (
        f"API 키 미설정으로 정적 분석 결과를 반환합니다. "
        f"칠레 의약품 시장은 FONASA(공공, ~70%) / ISAPRE(민간, ~20%) 이원화 구조로 운영됩니다. "
        f"ISP 등록 상태: {isp_reg}. "
        f"주요 리스크: {key_risk or '미확인'}."
    )
    sources = ["ISP Chile (https://www.ispch.cl)",
               "CENABAST (https://www.cenabast.cl)",
               "Mercado Público (https://www.mercadopublico.cl)"]
    return {
        "product_id":           meta.get("product_id", ""),
        "trade_name":           trade,
        "inn":                  meta.get("inn", ""),
        "verdict":              "조건부",
        "verdict_confidence":   0.5,
        "confidence":           0.5,
        "rationale":            rationale,
        "basis_market_medical": (
            "칠레 의약품 시장은 연간 약 30억 달러 규모로, FONASA 공공보험이 전체 인구의 "
            "약 70%를 커버합니다. CENABAST를 통한 공공조달과 Cruz Verde·Salcobrand·"
            "Farmacias Ahumada 3대 체인의 소매 과점 구조가 특징입니다. "
            "한국-칠레 FTA로 HS 3004 의약품에 0% 관세가 적용됩니다."
        ),
        "basis_regulatory": (
            f"ISP(Instituto de Salud Pública) 의약품 등록(Registro Sanitario) 필요. "
            f"현황: {isp_reg}. CPP(의약품수출증명서) 제출 시 심사 기간 단축 가능."
        ),
        "basis_trade": (
            "BE(생물학적 동등성) 시험 요건 사전 확인 필요. "
            "현지 대리인(Representante Técnico) 지정 및 스페인어 라벨링 의무. "
            "콜드체인 여부, 칠레 통관 절차(SNA 신고) 확인 필요."
        ),
        "price_positioning_pbs":    price_str,
        "price_positioning":        price_str,
        "cenabast_max_price_clp":   None,
        "raw_price_clp":            None,
        "entry_pathway":            (
            "① ISP 의약품 등록(RM 취득) "
            "→ ② FONASA/ISAPRE 급여 등재 협상 "
            "→ ③ CENABAST 공공조달 입찰 또는 Cruz Verde/Salcobrand 입점 협상"
        ),
        "risks_conditions":         key_risk or "ISP 등록 요건 및 CENABAST 상한가 사전 검토 필요",
        "key_factors":              [
            "ISP 등록(Registro Sanitario) 취득 기간 및 비용",
            "CENABAST 소매 상한가 내 수익성 확보",
            "3대 약국 체인(Cruz Verde·Salcobrand·Ahumada) 입점 협상",
        ],
        "isp_reg":                  isp_reg,
        "hsa_reg":                  isp_reg,
        "market_context":           "공공조달(CENABAST) 중심, 대형 약국체인 과점, 제네릭 선호",
        "product_type":             meta.get("product_type", ""),
        "sources":                  sources,
        "analysis_sources":         [{"name": s, "url": ""} for s in sources],
        "analyzed_at":              analyzed_at,
        "model":                    "static_fallback",
        "analysis_model":           "static_fallback",
        "claude_model_id":          "static_fallback",
    }


async def analyze_all(use_perplexity: bool = True) -> list[dict[str, Any]]:
    """모든 칠레 품목 일괄 분석."""
    meta_list = _get_product_meta()
    cl_products = [m for m in meta_list if str(m.get("product_id", "")).startswith("CL_")]
    tasks = [analyze_product(m["product_id"]) for m in cl_products]
    return list(await asyncio.gather(*tasks))


async def analyze_custom_product(
    trade_name: str,
    inn: str,
    dosage_form: str = "",
) -> dict[str, Any]:
    """사용자가 직접 입력한 신약 분석 (칠레 시장)."""
    meta = {
        "product_id":   "CL_custom",
        "trade_name":   trade_name,
        "inn":          inn,
        "dosage_form":  dosage_form,
        "isp_reg":      "미확인 — 사용자 입력 품목",
        "key_risk":     "ISP 등록 여부 및 CENABAST 상한가 적용 여부 사전 조사 필요",
    }
    return await analyze_product("CL_custom", db_row=meta)
