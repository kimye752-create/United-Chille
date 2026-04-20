"""싱가포르 1공정 수출 적합성 분석 엔진.

LLM 우선순위 (가이드라인 §1):
  1. Claude API (기본: claude-haiku-4-5-20251001) — 1차 분석·판단·근거 생성 (Primary)
  2. Perplexity API (sonar-pro)    — Claude 불확실 판정 시에만 보조 검색 후 재분석
  3. 정적 폴백                     — API 미설정 시

흐름:
  Claude 1차 분석 → verdict_confidence 낮으면 → Perplexity 보조 검색
  → Claude 2차 분석 (보강된 컨텍스트) → 최종 결과

출력 스키마 (품목별):
  product_id, trade_name, verdict(적합/부적합/조건부),
  rationale(근거 문단), key_factors, sources, analyzed_at

환경변수:
  CLAUDE_API_KEY 또는 ANTHROPIC_API_KEY
  CLAUDE_ANALYSIS_MODEL (선택, 기본 claude-haiku-4-5-20251001)
  PERPLEXITY_API_KEY  (선택)
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

from utils.pbs_pricing import fetch_pbs_pricing

# ── PRODUCT_META: Supabase products 테이블에서 동적 로드 ──────────────────────

_meta_cache: list[dict[str, Any]] | None = None

_FALLBACK_PRODUCT_META: list[dict[str, str]] = [
    # ── 우루과이(UY) 품목 ──────────────────────────────────────────────────
    {
        "product_id": "UY_cilostazol_cr_200",
        "trade_name": "Cilostazol CR",
        "inn": "Cilostazol 200mg SR (서방형)",
        "dosage_form": "SR Tab",
        "market_segment": "처방전 의약품",
        "product_type": "개량신약",
        "atc": "B01AC23",
        "therapeutic_area": "심혈관계 / 말초혈관질환",
        "hsa_reg": "해당없음 (MSP/ASSE 약가 등록 대상)",
        "key_risk": "IR Cilostazol 기존 경쟁(Pletal), 1일 1회 SR 차별화 입증, ARCE 낙찰가 리스크",
    },
    {
        "product_id": "UY_ciloduo_cilosta_rosuva",
        "trade_name": "Ciloduo",
        "inn": "Cilostazol + Rosuvastatin 복합제",
        "dosage_form": "Tab",
        "market_segment": "처방전 의약품",
        "product_type": "개량신약",
        "atc": "B01AC23 + C10AA07",
        "therapeutic_area": "심혈관계",
        "hsa_reg": "해당없음",
        "key_risk": "복합제 우루과이 등록 전례 부족, 성분별 별도 허가 요건 가능",
    },
    {
        "product_id": "UY_rosumeg_combigel",
        "trade_name": "Rosumeg Combigel",
        "inn": "Rosuvastatin + Omega-3",
        "dosage_form": "Cap",
        "market_segment": "처방전 의약품",
        "product_type": "개량신약",
        "atc": "C10AA07 + C10AX06",
        "therapeutic_area": "이상지질혈증",
        "hsa_reg": "해당없음",
        "key_risk": "오메가-3 복합제 현지 인식 부족, 독립 임상 데이터 요구 가능",
    },
    {
        "product_id": "UY_atmeg_combigel",
        "trade_name": "Atmeg Combigel",
        "inn": "Atorvastatin + Omega-3",
        "dosage_form": "Cap",
        "market_segment": "처방전 의약품",
        "product_type": "개량신약",
        "atc": "C10AA05 + C10AX06",
        "therapeutic_area": "이상지질혈증",
        "hsa_reg": "해당없음",
        "key_risk": "Atorvastatin 오리지널(Lipitor) 가격 경쟁, 복합제 별도 허가",
    },
    {
        "product_id": "UY_gastiin_cr_mosapride",
        "trade_name": "Gastiin CR",
        "inn": "Mosapride Citrate 15mg SR",
        "dosage_form": "SR Tab",
        "market_segment": "처방전 의약품",
        "product_type": "개량신약",
        "atc": "A03FA05",
        "therapeutic_area": "소화기계",
        "hsa_reg": "해당없음",
        "key_risk": "위장관 운동 촉진제 시장 내 경쟁, MSP 급여 목록 등재 가능성",
    },
    {
        "product_id": "UY_omethyl_omega3_2g",
        "trade_name": "Omethyl Cutielet",
        "inn": "Omega-3 Ethyl Esters 2g",
        "dosage_form": "Pouch",
        "market_segment": "처방전 의약품",
        "product_type": "개량신약",
        "atc": "C10AX06",
        "therapeutic_area": "고중성지방혈증",
        "hsa_reg": "해당없음",
        "key_risk": "Lovaza/Vascepa 경쟁, 우루과이 오메가-3 전문의약품 등록 경험 부족",
    },
    # ── 싱가포르(SG) 품목 ──────────────────────────────────────────────────
    {
        "product_id": "SG_sereterol_activair",
        "trade_name": "Sereterol Activair",
        "inn": "Fluticasone / Salmeterol",
        "dosage_form": "Inhaler",
        "market_segment": "처방전 의약품",
        "product_type": "일반제",
    },
    {
        "product_id": "SG_omethyl_omega3_2g",
        "trade_name": "Omethyl Cutielet",
        "inn": "Omega-3-Acid Ethyl Esters 90 2g",
        "dosage_form": "Pouch",
        "market_segment": "처방전 의약품",
        "product_type": "개량신약",
    },
    {
        "product_id": "SG_hydrine_hydroxyurea_500",
        "trade_name": "Hydrine",
        "inn": "Hydroxyurea 500mg",
        "dosage_form": "Cap",
        "market_segment": "항암제",
        "product_type": "항암제",
    },
    {
        "product_id": "SG_gadvoa_gadobutrol_604",
        "trade_name": "Gadvoa Inj.",
        "inn": "Gadobutrol 604.72mg",
        "dosage_form": "PFS",
        "market_segment": "처방전 의약품",
        "product_type": "일반제",
    },
    {
        "product_id": "SG_rosumeg_combigel",
        "trade_name": "Rosumeg Combigel",
        "inn": "Rosuvastatin + Omega-3",
        "dosage_form": "Cap",
        "market_segment": "처방전 의약품",
        "product_type": "개량신약",
    },
    {
        "product_id": "SG_atmeg_combigel",
        "trade_name": "Atmeg Combigel",
        "inn": "Atorvastatin + Omega-3",
        "dosage_form": "Cap",
        "market_segment": "처방전 의약품",
        "product_type": "개량신약",
    },
    {
        "product_id": "SG_ciloduo_cilosta_rosuva",
        "trade_name": "Ciloduo",
        "inn": "Cilostazol + Rosuvastatin",
        "dosage_form": "Tab",
        "market_segment": "처방전 의약품",
        "product_type": "개량신약",
    },
    {
        "product_id": "SG_gastiin_cr_mosapride",
        "trade_name": "Gastiin CR",
        "inn": "Mosapride Citrate 15mg",
        "dosage_form": "Tab",
        "market_segment": "처방전 의약품",
        "product_type": "개량신약",
    },
]


def _merge_with_fallback_meta(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Supabase 누락 품목을 기본 메타로 보완해 특정 품목 blank를 방지."""
    by_pid: dict[str, dict[str, Any]] = {}
    for row in rows:
        pid = str(row.get("product_id", "") or "").strip()
        if pid:
            by_pid[pid] = row

    for fallback in _FALLBACK_PRODUCT_META:
        pid = fallback["product_id"]
        if pid in by_pid:
            current = by_pid[pid]
            for key, value in fallback.items():
                if key == "product_id":
                    continue
                if not str(current.get(key, "") or "").strip():
                    current[key] = value
            current.setdefault("atc", "")
            current.setdefault("therapeutic_area", "")
            current.setdefault("hsa_reg", "")
            current.setdefault("key_risk", "")
            current.setdefault("manufacturer", "Korea United Pharm. Inc.")
            continue

        by_pid[pid] = {
            "product_id": pid,
            "trade_name": fallback["trade_name"],
            "inn": fallback["inn"],
            "atc": "",
            "dosage_form": fallback["dosage_form"],
            "market_segment": fallback["market_segment"],
            "therapeutic_area": "",
            "hsa_reg": "",
            "key_risk": "",
            "product_type": fallback["product_type"],
            "manufacturer": "Korea United Pharm. Inc.",
        }
    return list(by_pid.values())


def _load_product_meta() -> list[dict[str, Any]]:
    """Supabase products 테이블 (SG:kup_pipeline)에서 8품목 메타 로드.

    Supabase 미연결 또는 데이터 없을 시 빈 리스트 반환.
    """
    from utils.db import get_client
    sb = get_client()
    try:
        rows = (
            sb.table("products")
            .select("product_id,trade_name,active_ingredient,inn_name,strength,"
                    "dosage_form,market_segment,registration_number,"
                    "manufacturer,country_specific")
            .eq("country", "SG")
            .eq("source_name", "SG:kup_pipeline")
            .is_("deleted_at", "null")
            .execute()
            .data or []
        )
    except Exception:
        rows = []

    result = []
    for r in rows:
        cs = r.get("country_specific") or {}
        result.append({
            "product_id":       r.get("product_id", ""),
            "trade_name":       r.get("trade_name", ""),
            "inn":              r.get("inn_name") or r.get("active_ingredient", ""),
            "atc":              cs.get("atc", ""),
            "dosage_form":      r.get("dosage_form", ""),
            "market_segment":   r.get("market_segment", ""),
            "therapeutic_area": cs.get("therapeutic_area", ""),
            "hsa_reg":          cs.get("hsa_reg", ""),
            "key_risk":         cs.get("key_risk", ""),
            "product_type":     cs.get("product_type", "일반제"),
            "manufacturer":     r.get("manufacturer", "Korea United Pharm. Inc."),
        })
    return _merge_with_fallback_meta(result)


def _get_product_meta() -> list[dict[str, Any]]:
    global _meta_cache
    if _meta_cache is None:
        _meta_cache = _load_product_meta()
    return _meta_cache


def _get_meta_by_pid() -> dict[str, dict[str, Any]]:
    return {m["product_id"]: m for m in _get_product_meta()}


# 하위 호환 — tests/test_analysis.py 에서 PRODUCT_META 직접 import 대비
@property  # type: ignore[misc]
def PRODUCT_META() -> list[dict[str, Any]]:  # noqa: N802
    return _get_product_meta()


def _extract_assistant_text(message: object) -> str:
    """응답의 모든 `text` 블록만 이어 붙임 (thinking·tool_use 등은 건너뜀)."""
    parts: list[str] = []
    for block in getattr(message, "content", None) or ():
        if getattr(block, "type", None) == "text":
            t = getattr(block, "text", "") or ""
            if t:
                parts.append(t)
    return "\n".join(parts).strip()


def _read_env_secret(*names: str) -> str | None:
    for name in names:
        raw = os.environ.get(name)
        if raw is not None and (s := str(raw).strip()):
            return s
    return None


def _claude_analysis_model_id() -> str:
    """Anthropic Messages API에 넣는 모델 ID (Sonnet이 아니라 Haiku 기본)."""
    raw = os.environ.get("CLAUDE_ANALYSIS_MODEL", "")
    s = str(raw).strip()
    return s if s else "claude-haiku-4-5-20251001"


def _parse_claude_analysis_json(raw: str) -> dict[str, Any] | None:
    """모델 출력에서 분석 JSON만 추출 (서두 문장·코드펜스·토큰 절단 대비)."""
    text = (raw or "").strip()
    if not text:
        return None
    decoder = json.JSONDecoder()
    candidates: list[str] = [text]
    if "```" in text:
        for seg in text.split("```"):
            s = seg.strip()
            if s.lower().startswith("json"):
                s = s[4:].lstrip()
            if s.startswith("{"):
                candidates.append(s)

    for cand in candidates:
        start = 0
        while True:
            j = cand.find("{", start)
            if j < 0:
                break
            try:
                obj, _end = decoder.raw_decode(cand, j)
            except json.JSONDecodeError:
                start = j + 1
                continue
            coerced = _coerce_analysis_dict(obj)
            if coerced is not None:
                return coerced
            start = j + 1
    return None


def _coerce_analysis_dict(obj: object) -> dict[str, Any] | None:
    """모델이 Verdict 등 대소문자만 바꿔 보낸 경우에도 verdict 키를 맞춤."""
    if not isinstance(obj, dict):
        return None
    out: dict[str, Any] = dict(obj)
    if "verdict" not in out:
        for k, v in list(out.items()):
            if isinstance(k, str) and k.lower() == "verdict":
                out["verdict"] = v
                break
    return out if "verdict" in out else None


# ── Perplexity 보조 검색 ──────────────────────────────────────────────────────

async def _perplexity_search(query: str, api_key: str) -> str | None:
    """Perplexity sonar-pro로 규제·시장 정보 검색. 실패 시 None."""
    try:
        import httpx
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": "sonar-pro",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a pharmaceutical regulatory expert. "
                        "Provide factual, concise information about drug regulatory status "
                        "and market conditions in Singapore and Southeast Asia. "
                        "Always cite sources when available."
                    ),
                },
                {"role": "user", "content": query},
            ],
            "max_tokens": 512,
            "return_citations": True,
        }
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                "https://api.perplexity.ai/chat/completions",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]
    except Exception:
        return None


# ── Claude 분석 (Primary) ────────────────────────────────────────────────────

def _build_uy_analysis_prompt(
    meta: dict[str, Any],
    db_row: dict[str, Any] | None,
    perplexity_context: str | None,
    static_context_text: str | None = None,
) -> str:
    """Uruguay 시장 전용 Claude 분석 프롬프트."""
    reg_context = perplexity_context or "미수행"
    static_section = (
        f"\n## 시장 조사 데이터\n{static_context_text}\n"
        if static_context_text else ""
    )
    product_type = meta.get("product_type", "개량신약")

    return f"""당신은 우루과이(Uruguay) 의약품 수출 가능성을 분석하는 전문 컨설턴트입니다.
아래 품목에 대해 우루과이 1공정(규제 적합성·시장 진입 가능성) 관점에서 수출 적합성을 판단하세요.
사실 우선순위:
1) MSP/ASSE/ARCE 공식 자료 및 ORPM 벤치마크
2) 현지 약국 실세가 (Farmashop, Farma.uy, San Roque, Rex 수집가)
3) Perplexity 실시간 컨텍스트 (있을 때만 보강)
4) 일반 추론

## 품목 정보
- 브랜드명: {meta['trade_name']}
- INN(성분): {meta['inn']}
- ATC 코드: {meta.get('atc', '미확인')}
- 제형: {meta['dosage_form']}
- 제품 유형: {product_type}
- 시장 세그먼트: {meta['market_segment']}
- 치료 영역: {meta.get('therapeutic_area', '미지정')}
- 규제 상태: {meta.get('hsa_reg', '미확인')}
- 주요 리스크: {meta.get('key_risk', '미확인')}
{static_section}
## 실시간 규제·시장 정보 (Perplexity)
{reg_context}

## 분석 과제
1. 우루과이 MSP(공중보건부) 약가 등록 경로 및 ASSE 공공조달(ARCE/SICE) 진입 가능성
2. SICE 낙찰가 기준 공공 수요 존재 여부 및 경쟁 환경 (IR 오리지널 vs SR 개량신약)
3. 현지 약국 체인(Farmashop·San Roque·Rex) 민간 시장 접근 전략
4. UYU 약가 수준 및 FOB 역산 기반 수익성 평가 (VAT 10% 감안)
5. 코페이먼트 상한선(880 UYU) 대비 제품 가격 포지셔닝
6. 최종 판정: 적합(공공/민간 동시 진입) / 조건부(등록 선결 후 가능) / 부적합

▶ 출력 형식 규칙:
- basis_market_medical, basis_regulatory, basis_trade, risks_conditions, price_positioning_pbs 필드는 자연스러운 산문으로 작성
- 줄바꿈, 불릿 기호, 번호 목록 사용 금지
- 각 필드는 2~3문장으로만 구성
- "불가능", "확인 불가" 같은 단정적 결핍 표현 대신 "현재 확보된 데이터 기준" 등 실행 가능한 제안형 표현 사용

반드시 아래 JSON 형식으로만 응답하세요 (다른 텍스트 없이):
{{
  "verdict": "적합" | "부적합" | "조건부",
  "verdict_en": "SUITABLE" | "UNSUITABLE" | "CONDITIONAL",
  "rationale": "<한 문단 요약. 우루과이 시장 맥락 포함. 최대 320자>",
  "basis_market_medical": "<시장/의료 근거 2~3문장>",
  "basis_regulatory": "<MSP·ASSE 규제 근거 2~3문장>",
  "basis_trade": "<무역/유통 근거. UYU 가격·FOB 언급 포함>",
  "key_factors": ["<요인1>", "<요인2>", "<요인3>"],
  "entry_pathway": "<권장 진입 경로: MSP 약가 등록 / ASSE 공공조달 / 민간약국 유통>",
  "price_positioning_pbs": "<가격 포지셔닝 2~3문장. 현지 약국 실세가 또는 ORPM 벤치마크 언급>",
  "risks_conditions": "<진입 시 리스크/조건 2~3문장>",
  "sources": [
    {{"name": "<출처명>", "url": "<URL 또는 '내부 데이터'>"}}
  ],
  "confidence_note": "<판단 근거의 신뢰도 설명>"
}}"""


def _build_analysis_prompt(
    meta: dict[str, Any],
    db_row: dict[str, Any] | None,
    perplexity_context: str | None,
    static_context_text: str | None = None,
    pbs_context_block: str | None = None,
) -> str:
    reg_context = perplexity_context or "미수행"

    static_section = ""
    if static_context_text:
        static_section = (
            f"\n## 시장 조사 데이터 (HSA CSV + GeBIZ CSV + 브로슈어)\n"
            f"{static_context_text}\n"
        )

    pbs_section = ""
    if pbs_context_block:
        pbs_section = f"\n{pbs_context_block}\n"

    product_type = meta.get("product_type", "일반제")
    db_facts = _build_db_facts(db_row)

    return f"""당신은 싱가포르 의약품 수출 가능성을 분석하는 전문 컨설턴트입니다.
아래 품목에 대해 싱가포르 1공정(규제 적합성·시장 진입 가능성) 관점에서 수출 적합성을 판단하세요.
PBS 공개 스케줄에서 추출한 참고 가격(DPMQ)이 있으면 반드시 활용하되,
반드시 "(PBS, 방법론적 추산)" 라벨을 붙이고 싱가포르 약가·ERP 직접 벤치마크가 아님을 분명히 하세요.
사실 우선순위:
1) 기관 원문 데이터(HSA/MOH/PBS/가이드라인)와 정적 컨텍스트
2) PBS 참고 가격 블록(있을 때)
3) Perplexity 실시간 컨텍스트(있을 때만 보강)
4) 일반 추론
근거가 불충분하면 단정하지 말고 조건부/리스크로 명시하세요.

## 품목 정보
- 브랜드명: {meta['trade_name']}
- INN(성분): {meta['inn']}
- ATC 코드: {meta['atc']}
- 제형: {meta['dosage_form']}
- 제품 유형: {product_type}
- 시장 세그먼트: {meta['market_segment']}
- 치료 영역: {meta['therapeutic_area']}
- HSA 등재 상태: {meta['hsa_reg']}
- 주요 리스크: {meta['key_risk']}

## 내부 저장 데이터 (원문 출처 메타 포함)
{db_facts}
{static_section}{pbs_section}
## 실시간 규제·시장 정보 (Perplexity)
{reg_context}

## 분석 과제
1. HSA 등재 상태 및 진입 경로 (신규 NDA Full / 동등성 심사 / 복합제 별도 등록)
2. GeBIZ 조달 이력 기반 공공 수요 존재 여부 및 발주기관 특성
3. 경쟁품 수 및 처방 분류에 따른 시장 접근 전략
4. 주요 규제 장벽 및 예상 등록 타임라인
5. PBS 참고가(있을 때)를 시장·무역 논의에 1문장 이상 반영
6. 최종 판정: 적합(등재·채널 확보) / 조건부(등록 선결 후 가능) / 부적합

▶ 출력 형식 규칙 (반드시 준수):
- basis_market_medical, basis_regulatory, basis_trade, risks_conditions, price_positioning_pbs 필드는
  반드시 자연스러운 산문(연속 문장)으로 작성하세요.
- 줄바꿈(\n), 불릿 기호(-, •, *), 번호 목록(1. 2. 등), 소제목을 절대 사용하지 마세요.
- 각 필드는 2~3개의 연속된 문장으로만 구성하세요.
- 제조사명을 본문에 언급하지 마세요.
- "Supabase에 따르면", "DB에 따르면", "내부 저장소 기준" 같은 표현을 본문에 쓰지 마세요.
- 두괄식 판정 근거 및 시장 진출 전략 문장에는 반드시 기관+자료명을 명시하세요.
  예: "HSA Therapeutic Products Register(조회일: YYYY-MM-DD)에 따르면 ...", "MOH 통계 연보(연도)에 따르면 ..."
- 각 핵심 근거 문장에는 가능하면 수치/상태값(예: 등재 여부, 경쟁품 수, 등록 유형)을 1개 이상 포함하세요.

문장 톤 규칙:
- "불가능", "확인 불가", "제공되지 않아" 같은 단정적 결핍 표현은 쓰지 마세요.
- 대신 "현재 확보된 데이터 기준", "현 시점에서 확인된 범위", "추가 데이터 확보 시 정밀화 가능"처럼
  실행 가능한 제안형 표현으로 바꾸세요.

반드시 아래 JSON 형식으로만 응답하세요 (다른 텍스트 없이):
{{
  "verdict": "적합" | "부적합" | "조건부",
  "verdict_en": "SUITABLE" | "UNSUITABLE" | "CONDITIONAL",
  "rationale": "<한 문단 요약. PBS 참고가가 있으면 한 문장 안에 (PBS, 방법론적 추산) 포함. 최대 320자>",
  "basis_market_medical": "<시장/의료 근거 2~3문장>",
  "basis_regulatory": "<규제 근거 2~3문장>",
  "basis_trade": "<무역/유통 근거 2~3문장. PBS DPMQ·환율 참고 시 무역/가격 맥락에 반영>",
  "key_factors": ["<요인1>", "<요인2>", "<요인3>"],
  "entry_pathway": "<권장 진입 경로: NDA Full / 동등성(Abridged) / 복합제 별도 등록 / 브랜드 등록>",
  "price_positioning_pbs": "<가격 포지셔닝 2~3문장. 반드시 완전한 문장으로만 작성. PBS DPMQ가 확보된 경우 'DPMQ AUD X.XX, 참고 SGD Y.YY 수준(PBS, 방법론적 추산)'을 포함. PBS 미등재(204) 또는 데이터 없는 경우 '단일 가격이 공개되어 있으나 대상 품목 미등재로 직접 약가 벤치마크 산출이 제한적이며, [레퍼런스 경쟁품명] 기존 약가를 벤치마크로 한 상대적 가격 전략 수립이 필요합니다.'와 같이 서술. '제시할 수 없다' 같은 불완전 표현 사용 금지.>",
  "risks_conditions": "<진입 시 리스크/조건 2~3문장>",
  "sources": [
    {{"name": "<출처명>", "url": "<URL 또는 '내부 데이터'>"}}
  ],
  "confidence_note": "<판단 근거의 신뢰도 설명>"
}}"""


def _build_db_facts(db_row: dict[str, Any] | None) -> str:
    if not db_row:
        return "- DB 행 없음"
    facts: list[str] = []
    for key in (
        "product_key",
        "trade_name",
        "market_segment",
        "regulatory_id",
        "source_name",
        "source_url",
        "confidence",
    ):
        val = db_row.get(key)
        if val not in (None, ""):
            facts.append(f"- {key}: {val}")
    raw = db_row.get("raw_payload")
    if isinstance(raw, dict):
        for rk in (
            "sg_source_type",
            "sg_ndf_listed",
            "sg_gebiz_award",
            "moh_news_url",
            "moh_news_year",
            "moh_news_title",
        ):
            if rk in raw and raw.get(rk) not in (None, "", []):
                facts.append(f"- raw_payload.{rk}: {raw.get(rk)}")
    if not facts:
        return "- DB 주요 필드 없음"
    return "\n".join(facts[:20])


def _soften_limit_phrase(text: str) -> str:
    """결핍/불가 단정 문구를 제안형 문구로 완화."""
    s = (text or "").strip()
    if not s:
        return s
    repl = [
        ("제공되지 않아", "현재 확보된 범위에서"),
        ("확인이 불가능", "현 시점에서는 추가 확인이 필요"),
        ("확인 불가", "추가 확인 필요"),
        ("불가능합니다", "제한적입니다"),
        ("불가능", "제한적"),
        ("없어", "제한적이어서"),
        ("없습니다.", "제한적입니다."),
    ]
    out = s
    for a, b in repl:
        out = out.replace(a, b)
    return out


def _soften_analysis_language(result: dict[str, Any]) -> dict[str, Any]:
    """사용자 가이드에 맞춰 부정 단정 표현을 완화."""
    out = dict(result)
    for k in (
        "rationale",
        "basis_market_medical",
        "basis_regulatory",
        "basis_trade",
        "entry_pathway",
        "price_positioning_pbs",
        "risks_conditions",
        "confidence_note",
    ):
        if k in out and isinstance(out.get(k), str):
            out[k] = _soften_limit_phrase(out[k])
    if isinstance(out.get("key_factors"), list):
        out["key_factors"] = [
            _soften_limit_phrase(str(x)) for x in out["key_factors"]
        ]
    return out


def _sanitize_source_attribution_phrase(text: str) -> str:
    """저장소 중심 표현을 기관/자료 중심 표현으로 정리."""
    import re

    s = (text or "").strip()
    if not s:
        return s

    rules: list[tuple[str, str]] = [
        (
            r"Supabase\s*(및|와)?\s*HSA\s*(시장\s*조사\s*)?데이터에\s*따르면",
            "HSA 공개 자료에 따르면",
        ),
        (r"Supabase\s*데이터에\s*따르면", "공개 기관 자료에 따르면"),
        (r"DB\s*데이터에\s*따르면", "공개 기관 자료에 따르면"),
        (r"내부\s*(DB|데이터베이스|저장소)\s*기준", "현재 확보된 공개 기관 자료 기준"),
        (r"Supabase\s*기준", "현재 확보된 공개 기관 자료 기준"),
        (r"호주\s*PBS", "PBS"),
        (r"PBS\s*\(호주[^\)]*\)", "PBS"),
        (r"호주\s*공개\s*스케줄", "공개 스케줄"),
        (r"호주", ""),
    ]
    out = s
    for pat, repl in rules:
        out = re.sub(pat, repl, out, flags=re.IGNORECASE)
    return out


def _infer_source_name_from_url(url: str) -> str:
    u = (url or "").lower()
    if "hsa.gov.sg" in u:
        return "HSA Singapore"
    if "moh.gov.sg" in u:
        return "MOH Singapore"
    if "healthhub.sg" in u:
        return "HealthHub Singapore"
    if "pharmaceutical-benefits-scheme" in u or "pbs.gov.au" in u:
        return "PBS Public Schedule"
    if "data.gov.sg" in u:
        return "data.gov.sg"
    if "who.int" in u:
        return "WHO"
    if "ncbi.nlm.nih.gov" in u or "pubmed" in u:
        return "PubMed"
    return "공개 출처"


def _normalize_sources(result: dict[str, Any]) -> dict[str, Any]:
    """sources를 기관/자료 중심으로 정리하고 내부 저장소 표기를 제거."""
    out = dict(result)
    src_raw = out.get("sources")
    if not isinstance(src_raw, list):
        out["sources"] = []
        return out

    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for s in src_raw:
        if not isinstance(s, dict):
            continue
        name = str(s.get("name", "") or "").strip()
        url = str(s.get("url", "") or "").strip()
        if name and "supabase" in name.lower():
            continue
        if name:
            name = name.replace("PBS Australia", "PBS Public Schedule")
            name = name.replace("호주 PBS", "PBS")
            name = name.replace("호주", "").strip()
        if not name and url:
            name = _infer_source_name_from_url(url)
        if not name:
            continue
        key = (name.lower(), url.lower())
        if key in seen:
            continue
        seen.add(key)
        normalized.append({"name": name, "url": url})
    out["sources"] = normalized
    return out


def _polish_evidence_texts(result: dict[str, Any]) -> dict[str, Any]:
    """근거 문장에서 저장소 기반 표현 제거."""
    out = dict(result)
    for k in (
        "rationale",
        "basis_market_medical",
        "basis_regulatory",
        "basis_trade",
        "entry_pathway",
        "price_positioning_pbs",
        "risks_conditions",
        "confidence_note",
    ):
        v = out.get(k)
        if isinstance(v, str):
            out[k] = _sanitize_source_attribution_phrase(v)
    return out


def _normalize_price_positioning_pbs(
    result: dict[str, Any],
    pbs_res: Any,
) -> dict[str, Any]:
    """가격 포지셔닝 문장을 사용자 가이드 문맥으로 정리."""
    out = dict(result)
    default_line = (
        "단일 가격이 공개되어 있으나, 대상 품목 미등재로 인해 직접 약가 벤치마크를 "
        "산출하기엔 제한적입니다. 싱가포르에서는 기존 경쟁품의 병원·약국 공급가와 "
        "처방 채널 가격대를 기준으로 상대 가격 전략을 수립하는 접근이 적절합니다."
    )

    current = str(out.get("price_positioning_pbs", "") or "").strip()
    if current:
        current = _sanitize_source_attribution_phrase(current)

    if getattr(pbs_res, "dpmq_aud", None) is not None:
        aud = float(getattr(pbs_res, "dpmq_aud"))
        sgd = getattr(pbs_res, "dpmq_sgd_hint", None)
        if sgd is not None:
            ref = f"DPMQ AUD {aud:.2f}(참고 SGD {float(sgd):.2f} 수준, PBS 방법론적 추산)"
        else:
            ref = f"DPMQ AUD {aud:.2f}(PBS 방법론적 추산)"
        out["price_positioning_pbs"] = (
            f"{ref}이 공개되어 있으나, 직접 약가 벤치마크로 단정하기에는 제한적입니다. "
            "싱가포르에서는 기존 경쟁품의 병원·약국 공급가를 기준으로 상대 가격 전략을 수립하는 접근이 적절합니다."
        )
        return out

    fetch_error = str(getattr(pbs_res, "fetch_error", "") or "").strip()
    if "미등재" in fetch_error:
        out["price_positioning_pbs"] = default_line
        return out

    if not current:
        out["price_positioning_pbs"] = default_line
        return out

    out["price_positioning_pbs"] = current
    return out


def _extract_price_from_text(text: str) -> str | None:
    """모델 응답에서 $숫자 패턴만 추출. 없으면 None."""
    import re
    # $10, $10.50, $10-50, $10–50, $10~50, USD 10 등 다양한 형태 커버
    pattern = r'\$[\d,]+(?:\.\d+)?(?:\s*[-–~]\s*\$?[\d,]+(?:\.\d+)?)?|USD\s*[\d,]+(?:\.\d+)?(?:\s*[-–~]\s*(?:USD\s*)?[\d,]+(?:\.\d+)?)?'
    m = re.search(pattern, text)
    if m:
        return m.group(0).strip()
    return None


async def _haiku_price_estimate(meta: dict[str, Any], api_key: str) -> str | None:
    """PBS 가격 없는 품목에 대해 Claude Haiku로 참고 가격 조사."""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)

        def _call() -> str | None:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=64,
                timeout=30.0,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"성분: {meta.get('inn')}, 제형: {meta.get('dosage_form')}. "
                            "국제 참고 가격을 $숫자 또는 $숫자-숫자 형식으로만 답하라. "
                            "설명, 문장, 면책 문구 없이 금액만."
                        ),
                    },
                    {
                        "role": "assistant",
                        "content": "$",
                    },
                ],
            )
            texts = [b.text for b in resp.content if hasattr(b, "text")]
            raw = texts[0].strip() if texts else None
            if not raw:
                return None
            # assistant prefill "$" + 모델 응답 합치기
            full = "$" + raw
            return full

        raw_text = await asyncio.to_thread(_call)
        if not raw_text:
            return None
        price = _extract_price_from_text(raw_text)
        if not price:
            return None
        return f"{price}, 추정값이니 참고용으로만 사용하세요."
    except Exception:
        return None


async def _claude_analyze(
    meta: dict[str, Any],
    db_row: dict[str, Any] | None,
    api_key: str,
    *,
    perplexity_context: str | None = None,
    static_context_text: str | None = None,
    pbs_context_block: str | None = None,
    model: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Claude API로 수출 적합성 분석. (파싱된 dict 또는 None, 사람이 읽을 오류 요약 또는 None)."""
    try:
        import anthropic
    except ImportError:
        return None, "anthropic 패키지 미설치"

    resolved_model = (model or "").strip() or _claude_analysis_model_id()
    pid = str(meta.get("product_id", ""))
    if pid.startswith("UY_"):
        prompt = _build_uy_analysis_prompt(
            meta, db_row, perplexity_context, static_context_text
        )
    else:
        prompt = _build_analysis_prompt(
            meta, db_row, perplexity_context, static_context_text, pbs_context_block
        )

    def _sync_call() -> tuple[dict[str, Any] | None, str | None]:
        try:
            client = anthropic.Anthropic(api_key=api_key, timeout=60.0)
            response = client.messages.create(
                model=resolved_model,
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = _extract_assistant_text(response)
            if not raw:
                return None, "empty_model_text"
            parsed = _parse_claude_analysis_json(raw)
            if parsed is None:
                head = raw[:160].replace("\n", " ")
                return None, f"json_parse_failed(len={len(raw)} head={head!r})"
            return parsed, None
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"[:400]

    return await asyncio.wait_for(asyncio.to_thread(_sync_call), timeout=90.0)


# ── 단일 품목 분석 ─────────────────────────────────────────────────────────────

async def analyze_product(
    product_id: str,
    db_row: dict[str, Any] | None = None,
    *,
    use_perplexity: bool = True,
) -> dict[str, Any]:
    """단일 품목 수출 적합성 분석.

    Returns:
        분석 결과 dict (verdict, rationale, key_factors, sources, analyzed_at 포함)
    """
    meta = _get_meta_by_pid().get(product_id)
    if meta is None:
        return {
            "product_id": product_id,
            "error": f"알 수 없는 product_id: {product_id}",
            "analyzed_at": datetime.now(timezone.utc).isoformat(),
        }

    claude_key = _read_env_secret("CLAUDE_API_KEY", "ANTHROPIC_API_KEY")
    perplexity_key = _read_env_secret("PERPLEXITY_API_KEY") if use_perplexity else None
    claude_model_id = _claude_analysis_model_id()
    claude_error_detail: str | None = None

    # 정적 데이터 컨텍스트 로드 (HSA CSV + PDF 스니펫)
    static_context_text: str | None = None
    try:
        from utils.static_data import get_product_context, context_to_prompt_text
        ctx = get_product_context(product_id)
        if ctx:
            static_context_text = context_to_prompt_text(ctx)
    except Exception:
        pass

    meta_for_pbs: dict[str, str] = {
        "product_id": product_id,
        "trade_name": str(meta.get("trade_name", "") or ""),
        "inn": str(meta.get("inn", "") or ""),
        "dosage_form": str(meta.get("dosage_form", "") or ""),
    }
    pbs_res = await fetch_pbs_pricing(meta_for_pbs)
    pbs_block = pbs_res.to_prompt_block()
    pbs_flat = pbs_res.to_flat_dict()

    result: dict[str, Any] | None = None
    analysis_model = "static_fallback"
    analysis_error: str | None = None

    # Step 1: Claude 1차 분석 (HSA DB + PDF + PBS 컨텍스트 포함)
    if claude_key:
        result, claude_error_detail = await _claude_analyze(
            meta, db_row, claude_key,
            perplexity_context=None,
            static_context_text=static_context_text,
            pbs_context_block=pbs_block,
            model=claude_model_id,
        )
        if result:
            analysis_model = claude_model_id

    # Step 2: 조건부 판정 시 Perplexity 보조 검색 후 재분석
    if (
        result is not None
        and perplexity_key
        and result.get("verdict") == "조건부"
        and claude_key
    ):
        if product_id.startswith("UY_"):
            query = (
                f"Uruguay MSP/ASSE regulatory status and SICE procurement history for "
                f"{meta['trade_name']} ({meta['inn']}). "
                f"Include drug pricing policy, ARCE awards, MSP copayment ceiling updates."
            )
        else:
            query = (
                f"Singapore HSA regulatory status and formulary context for "
                f"{meta['trade_name']} ({meta['inn']}). "
                f"Recent regulatory or clinical guideline updates only — no retail prices."
            )
        perplexity_context = await _perplexity_search(query, perplexity_key)
        if perplexity_context:
            result2, err2 = await _claude_analyze(
                meta, db_row, claude_key,
                perplexity_context=perplexity_context,
                static_context_text=static_context_text,
                pbs_context_block=pbs_block,
                model=claude_model_id,
            )
            if result2:
                result = result2
                analysis_model = f"{claude_model_id}+perplexity"
            elif err2:
                claude_error_detail = err2

    if result is not None and pbs_flat.get("pbs_listing_url"):
        src_list: list[Any] = list(result.get("sources") or [])
        pbs_u = str(pbs_flat["pbs_listing_url"])
        if pbs_u and not any(
            isinstance(x, dict) and str(x.get("url", "")) == pbs_u for x in src_list
        ):
            src_list.insert(0, {"name": "PBS Public Schedule", "url": pbs_u})
        result["sources"] = src_list

    # API 미설정 또는 분석 실패 시 — 보고서에 명확히 표시
    if result is None:
        no_api = not bool(claude_key)
        analysis_error = "no_api_key" if no_api else "claude_failed"
        pbs_fallback_price = ""
        if pbs_res.dpmq_aud is not None:
            sgd_part = (
                f"(참고 SGD 약 {pbs_res.dpmq_sgd_hint:.2f} 가정) "
                if pbs_res.dpmq_sgd_hint is not None
                else ""
            )
            pbs_fallback_price = (
                f"PBS DPMQ 약 AUD {pbs_res.dpmq_aud:.2f} {sgd_part}"
                f"{pbs_res.methodology_label_ko}."
            )
        elif pbs_res.listing_url:
            pbs_fallback_price = (
                f"PBS 스케줄 페이지는 확보되었으나 DPMQ 파싱에 한계가 있어 "
                f"추가 확인이 필요합니다. {pbs_res.methodology_label_ko}"
            )
        result = {
            "verdict": None,
            "verdict_en": None,
            "rationale": (
                "Claude API 키 미설정 — CLAUDE_API_KEY 또는 ANTHROPIC_API_KEY "
                "환경변수를 설정하면 실제 분석이 실행됩니다."
                if no_api else
                "Claude API 분석 실패 — API 키를 확인하거나 잠시 후 다시 시도하세요."
            ),
            "basis_market_medical": pbs_fallback_price,
            "basis_regulatory": "",
            "basis_trade": pbs_fallback_price,
            "key_factors": [],
            "entry_pathway": "",
            "price_positioning_pbs": pbs_fallback_price or (
                "현재 확보된 PBS 페이지 기준으로 참고 가격 문구를 구성하려면 "
                "네트워크·파싱 결과가 필요합니다."
            ),
            "risks_conditions": "",
            "sources": (
                [{"name": "PBS Public Schedule", "url": pbs_res.listing_url}]
                if pbs_res.listing_url else []
            ),
            "confidence_note": "API 미설정" if no_api else "분석 실패",
        }

    result = _soften_analysis_language(result)
    result = _polish_evidence_texts(result)
    result = _normalize_price_positioning_pbs(result, pbs_res)
    result = _normalize_sources(result)

    # PBS 가격 없으면 Haiku로 참고 가격 보완
    haiku_estimate: str | None = None
    if pbs_res.dpmq_aud is None and claude_key:
        haiku_estimate = await _haiku_price_estimate(meta_for_pbs, claude_key)
    result["pbs_haiku_estimate"] = haiku_estimate or ""

    if not (result.get("price_positioning_pbs") or "").strip():
        if pbs_res.dpmq_aud is not None:
            result["price_positioning_pbs"] = (
                f"PBS DPMQ 약 AUD {pbs_res.dpmq_aud:.2f}, "
                f"참고 SGD 약 {pbs_res.dpmq_sgd_hint} 수준(환율 변동). "
                "싱가포르 소매가와 동일시하지 않습니다."
            )
        elif haiku_estimate:
            result["price_positioning_pbs"] = haiku_estimate
        elif pbs_res.fetch_error:
            result["price_positioning_pbs"] = (
                "PBS 미등재 또는 조회 오류로 DPMQ 참고가를 직접 산출하기엔 제한적입니다. "
                "싱가포르 약가 포지셔닝은 동일 성분 경쟁 제품의 기존 약가를 벤치마크로 하여 "
                "tender 입찰 경쟁력을 고려한 상대적 가격 전략 수립이 필요합니다."
            )

    return {
        "product_id": product_id,
        "trade_name": meta["trade_name"],
        "inn": meta["inn"],
        "market_segment": meta["market_segment"],
        "product_type": meta.get("product_type", ""),
        "hsa_reg": meta.get("hsa_reg", ""),
        "verdict": result.get("verdict"),          # None = API 미설정
        "verdict_en": result.get("verdict_en"),
        "rationale": result.get("rationale", ""),
        "basis_market_medical": result.get("basis_market_medical", ""),
        "basis_regulatory": result.get("basis_regulatory", ""),
        "basis_trade": result.get("basis_trade", ""),
        "key_factors": result.get("key_factors", []),
        "entry_pathway": result.get("entry_pathway", ""),
        "price_positioning_pbs": result.get("price_positioning_pbs", ""),
        "risks_conditions": result.get("risks_conditions", ""),
        "section_source_map": {
            "제품 식별": "supabase.products (SG:kup_pipeline)",
            "핵심 판정": (
                f"Claude Haiku ({claude_model_id})"
                if analysis_error is None else "fallback (API 미설정/실패)"
            ),
            "두괄식 근거 - 시장/의료": (
                "Claude Haiku + HSA/MOH/공개기관 컨텍스트"
                if analysis_error is None else "fallback"
            ),
            "두괄식 근거 - 규제": (
                "Claude Haiku + HSA/MOH/공개기관 컨텍스트"
                if analysis_error is None else "fallback"
            ),
            "두괄식 근거 - 무역": (
                "Claude Haiku + HSA/MOH/공개기관 컨텍스트"
                if analysis_error is None else "fallback"
            ),
            "시장 진출 전략 - 진입 채널 권고": (
                "Claude Haiku"
                if analysis_error is None else "fallback"
            ),
            "시장 진출 전략 - 리스크/조건": (
                "Claude Haiku + HSA/MOH/공개기관 컨텍스트"
                if analysis_error is None else "fallback"
            ),
        },
        "sources": result.get("sources", []),
        "confidence_note": result.get("confidence_note", ""),
        "analysis_model": analysis_model,
        "analysis_error": analysis_error,
        "claude_model_id": claude_model_id,
        "claude_error_detail": claude_error_detail if analysis_error == "claude_failed" else None,
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
        **pbs_flat,
        "pbs_haiku_estimate": result.get("pbs_haiku_estimate", ""),
    }


# ── 신약(커스텀) 분석 ────────────────────────────────────────────────────────

async def analyze_custom_product(
    trade_name: str,
    inn: str,
    dosage_form: str = "",
) -> dict[str, Any]:
    """사용자 입력 신약에 대한 싱가포르 수출 적합성 분석.

    Supabase DB 행 없이 입력 정보 + PBS 참고가 + Claude로만 실행.
    """
    meta: dict[str, Any] = {
        "product_id":       "custom",
        "trade_name":       trade_name,
        "inn":              inn,
        "atc":              "",
        "dosage_form":      dosage_form,
        "market_segment":   "처방전 의약품",
        "therapeutic_area": "",
        "hsa_reg":          "미등재(신약)",
        "key_risk":         "",
        "manufacturer":     "",
        "product_type":     "신약",
        "regulatory_id":    "",
        "db_confidence":    None,
    }

    claude_key = _read_env_secret("CLAUDE_API_KEY", "ANTHROPIC_API_KEY")
    claude_model_id = _claude_analysis_model_id()

    meta_for_pbs: dict[str, str] = {
        "product_id": "custom",
        "trade_name": trade_name,
        "inn": inn,
        "dosage_form": dosage_form,
    }
    pbs_res = await fetch_pbs_pricing(meta_for_pbs)
    pbs_block = pbs_res.to_prompt_block()
    pbs_flat  = pbs_res.to_flat_dict()

    result: dict[str, Any] | None = None
    analysis_error: str | None = None

    if claude_key:
        result, analysis_error = await _claude_analyze(
            meta, None, claude_key,
            pbs_context_block=pbs_block,
            model=claude_model_id,
        )

    if result is not None:
        result = _soften_analysis_language(result)
        result = _polish_evidence_texts(result)
        result = _normalize_price_positioning_pbs(result, pbs_res)
        result = _normalize_sources(result)

    haiku_estimate: str | None = None
    if pbs_res.dpmq_aud is None and claude_key:
        haiku_estimate = await _haiku_price_estimate(meta_for_pbs, claude_key)

    if result is None:
        result = {
            "verdict": None,
            "verdict_en": None,
            "rationale": "Claude API 키 미설정 또는 분석 실패." if not claude_key else f"분석 오류: {analysis_error}",
            "basis_market_medical": "",
            "basis_regulatory": "",
            "basis_trade": "",
            "key_factors": [],
            "entry_pathway": "",
            "price_positioning_pbs": haiku_estimate or "",
            "risks_conditions": "",
            "sources": [],
            "confidence_note": "미분석",
        }

    result["pbs_haiku_estimate"] = haiku_estimate or ""
    if not (result.get("price_positioning_pbs") or "").strip():
        if pbs_res.dpmq_aud is not None:
            result["price_positioning_pbs"] = (
                f"PBS DPMQ 약 AUD {pbs_res.dpmq_aud:.2f}, "
                f"참고 SGD 약 {pbs_res.dpmq_sgd_hint} 수준(환율 변동)."
            )
        elif haiku_estimate:
            result["price_positioning_pbs"] = haiku_estimate

    return {
        "product_id":            "custom",
        "trade_name":            trade_name,
        "inn":                   inn,
        "inn_label":             f"{inn} {dosage_form}".strip(),
        "market_segment":        "처방전 의약품",
        "product_type":          "신약",
        "hsa_reg":               "미등재(신약)",
        "verdict":               result.get("verdict"),
        "verdict_en":            result.get("verdict_en"),
        "rationale":             result.get("rationale", ""),
        "basis_market_medical":  result.get("basis_market_medical", ""),
        "basis_regulatory":      result.get("basis_regulatory", ""),
        "basis_trade":           result.get("basis_trade", ""),
        "key_factors":           result.get("key_factors", []),
        "entry_pathway":         result.get("entry_pathway", ""),
        "price_positioning_pbs": result.get("price_positioning_pbs", ""),
        "risks_conditions":      result.get("risks_conditions", ""),
        "pbs_haiku_estimate":    result.get("pbs_haiku_estimate", ""),
        "pbs_dpmq_aud":          pbs_flat.get("pbs_dpmq_aud"),
        "pbs_dpmq_sgd_hint":     pbs_flat.get("pbs_dpmq_sgd_hint"),
        "pbs_methodology_label_ko": pbs_flat.get("pbs_methodology_label_ko"),
        "pbs_fetch_error":       pbs_flat.get("pbs_fetch_error"),
        "pbs_listing_url":       pbs_flat.get("pbs_listing_url"),
        "pbs_schedule_drug_name": pbs_flat.get("pbs_schedule_drug_name"),
        "pbs_pack_description":  pbs_flat.get("pbs_pack_description"),
        "pbs_search_hit":        pbs_flat.get("pbs_search_hit"),
        "sources":               result.get("sources", []),
        "confidence_note":       result.get("confidence_note", ""),
        "regulatory_id":         "",
        "db_confidence":         None,
        "analysis_model":        claude_model_id if claude_key else "미설정",
        "analysis_error":        analysis_error,
        "analyzed_at":           datetime.now(timezone.utc).isoformat(),
    }


# ── 전체 8품목 배치 분석 ──────────────────────────────────────────────────────

async def analyze_all(
    *,
    use_perplexity: bool = True,
) -> list[dict[str, Any]]:
    """8품목 전체 수출 적합성 분석 실행 (Supabase 기반).

    Args:
        use_perplexity: Perplexity 보조 검색 활성화 여부

    Returns:
        품목별 분석 결과 리스트
    """
    from utils.db import fetch_kup_products
    kup_rows = fetch_kup_products("SG")
    db_rows = {r["product_id"]: r for r in kup_rows}

    tasks = [
        analyze_product(
            meta["product_id"],
            db_rows.get(meta["product_id"]),
            use_perplexity=use_perplexity,
        )
        for meta in _get_product_meta()
    ]
    return list(await asyncio.gather(*tasks))
