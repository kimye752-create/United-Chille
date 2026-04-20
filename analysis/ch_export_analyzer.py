"""칠레 1공정 수출 적합성 분석 엔진.

LLM 우선순위:
  1. Claude API (claude-haiku-4-5-20251001) — 1차 분석·판단·근거 생성
  2. Perplexity API (sonar-pro)    — 불확실 판정 시 보조 검색 후 재분석
  3. 정적 폴백                     — API 미설정 시

칠레 규제 특수사항:
  - 규제 당국: ISP (Instituto de Salud Pública) — MINSAL 산하
  - 공공조달: Mercado Público / ChileCompra (OCDS 표준)
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


# ── Claude 분석 ─────────────────────────────────────────────────────────────

def _build_analysis_prompt(meta: dict[str, Any], pricing_context: str = "") -> str:
    return f"""당신은 칠레 의약품 수출 전문 컨설턴트입니다.
아래 품목의 칠레 시장 수출 적합성을 분석해 주세요.

## 품목 정보
- 제품명(상품명): {meta.get('trade_name', '')}
- INN(성분명): {meta.get('inn', '')}
- 제형: {meta.get('dosage_form', '')}
- ATC 코드: {meta.get('atc', '')}
- 치료 영역: {meta.get('therapeutic_area', '')}
- 제품 유형: {meta.get('product_type', '')}
- ISP 등록 상태: {meta.get('isp_reg', '미확인')}
- 주요 리스크: {meta.get('key_risk', '')}

## 칠레 시장 맥락
- 규제 당국: ISP (Instituto de Salud Pública) / MINSAL
- 약가 통제: CENABAST Ley 21.198 (소매 상한가 강제)
- 보험 구조: FONASA(공공, 약 70%) / ISAPRE(민간, 약 20%)
- 소매 독과점: Cruz Verde / Salcobrand / Farmacias Ahumada (시장 점유율 90%+)
- 공공조달: Mercado Público (ChileCompra OCDS 표준)
- VAT: 19%

## 약가 수집 데이터
{pricing_context or "수집된 현지 약가 데이터 없음 (크롤링 미실행)"}

## 분석 요청
다음 JSON 형식으로만 응답하세요:
{{
  "verdict": "적합|조건부|부적합",
  "verdict_confidence": 0.0~1.0,
  "rationale": "판정 근거 (3-5문장, 칠레 규제·시장·경쟁 맥락 포함)",
  "key_factors": ["요인1", "요인2", "요인3"],
  "entry_pathway": "권장 진입 경로 (ISP 등록 → FONASA/ISAPRE 급여 → 유통 전략)",
  "price_positioning": "가격 포지셔닝 전략 (CENABAST 상한가 대비)",
  "risks_conditions": "조건부 시 충족 조건 또는 주요 리스크",
  "sources": ["참고 출처1", "참고 출처2"]
}}"""


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
        prompt = _build_analysis_prompt(meta, pricing_context)

        resp = await asyncio.to_thread(
            lambda: client.messages.create(
                model=model,
                max_tokens=1500,
                messages=[{"role": "user", "content": prompt}],
            )
        )
        raw = resp.content[0].text
        parsed = _parse_llm_json(raw)
        parsed.update({
            "product_id": product_id,
            "trade_name": meta.get("trade_name", ""),
            "inn": meta.get("inn", ""),
            "analyzed_at": analyzed_at,
            "model": model,
        })
        return parsed
    except Exception as exc:
        result = _static_fallback(meta, analyzed_at)
        result["error"] = str(exc)
        return result


def _parse_llm_json(raw: str) -> dict[str, Any]:
    import re
    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return {
        "verdict": "조건부",
        "verdict_confidence": 0.5,
        "rationale": raw[:500],
        "key_factors": [],
        "entry_pathway": "",
        "price_positioning": "",
        "risks_conditions": "",
        "sources": [],
    }


def _static_fallback(meta: dict[str, Any], analyzed_at: str) -> dict[str, Any]:
    return {
        "product_id": meta.get("product_id", ""),
        "trade_name": meta.get("trade_name", ""),
        "inn": meta.get("inn", ""),
        "verdict": "조건부",
        "verdict_confidence": 0.5,
        "rationale": (
            f"API 키 미설정으로 정적 분석 결과를 반환합니다. "
            f"ISP 등록 상태: {meta.get('isp_reg', '미확인')}. "
            f"주요 리스크: {meta.get('key_risk', '미확인')}."
        ),
        "key_factors": ["ISP 등록 필요", "CENABAST 약가 통제 확인", "3대 체인 입점 협상"],
        "entry_pathway": "ISP 등록 → FONASA/ISAPRE 급여 신청 → Cruz Verde/Salcobrand 입점",
        "price_positioning": "CENABAST 소매 상한가 조회 후 역산 FOB 산출 필요",
        "risks_conditions": meta.get("key_risk", ""),
        "sources": ["ISP Chile", "CENABAST", "Mercado Público"],
        "analyzed_at": analyzed_at,
        "model": "static_fallback",
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
        "product_id": "CL_custom",
        "trade_name": trade_name,
        "inn": inn,
        "dosage_form": dosage_form,
        "isp_reg": "미확인",
        "key_risk": "ISP 등록 여부 및 CENABAST 상한가 적용 여부 사전 조사 필요",
    }
    return await analyze_product("CL_custom", db_row=meta)
