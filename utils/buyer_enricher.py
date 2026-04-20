"""바이어 심층 조사 — 2차 수집.

CPHI 전시회 상세 페이지 전체 텍스트 → Claude Haiku 파싱.
Perplexity Sonar로 target_country 관련성 실시간 검증 후 Claude 컨텍스트에 주입.
국가 변수(target_country/target_region)로 전체 로직 제어.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any, Awaitable, Callable

CLAUDE_MODEL = "claude-haiku-4-5-20251001"

_SCHEMA_DESC = {
    "revenue":                   "연 매출 규모 (예: ~$50M, $200M+) — 불명확 시 '-'",
    "employees":                 "임직원 수 (예: 500+, 1200) — 불명확 시 '-'",
    "founded":                   "설립연도 (예: 1990) — 불명확 시 '-'",
    "territories":               "주요 영업 국가/지역 배열 (예: [\"Singapore\",\"Malaysia\"])",
    "has_target_country_presence": "target_country 시장 진출/영업 여부 (true/false/null)",
    "has_gmp":                   "GMP 인증 보유 여부 (true/false/null)",
    "import_history":            "수입 이력 여부 (true/false/null)",
    "procurement_history":       "공공조달 낙찰 이력 여부 (true/false/null)",
    "has_pharmacy_chain":        "약국 체인 보유 여부 (true/false/null)",
    "public_channel":            "공공 채널(병원/조달) 취급 여부 (true/false/null)",
    "private_channel":           "민간 채널(약국/도매) 취급 여부 (true/false/null)",
    "mah_capable":               "MAH(위생등록) 대행 가능 여부 (true/false/null)",
    "korea_experience":          "한국 기업 거래 경험 (예: '없음', '있음(미확인)') — 불명확 시 '-'",
    "certifications":            "보유 인증 목록 (예: [\"USFDA\",\"EU GMP\",\"KFDA\"])",
    "source_urls":               "참조 출처 URL 배열",
    "company_overview_kr":       "CPHI 페이지 기반 기업 개요 (한국어 2~3문장)",
    "recommendation_reason":     "파트너 후보 추천 이유 (한국어 3~5문장, 제품 연관성+강점+근거)",
}

_NULL_ENRICH: dict[str, Any] = {
    "revenue": "-",
    "employees": "-",
    "founded": "-",
    "territories": [],
    "has_target_country_presence": None,
    "has_gmp": None,
    "import_history": None,
    "procurement_history": None,
    "has_pharmacy_chain": None,
    "public_channel": None,
    "private_channel": None,
    "mah_capable": None,
    "korea_experience": "-",
    "certifications": [],
    "source_urls": [],
    "company_overview_kr": "-",
    "recommendation_reason": "-",
}


async def _claude_extract(
    company_name: str,
    country: str,
    full_page_text: str,
    product_label: str,
    target_country: str = "Singapore",
    target_region: str = "Asia",
    perplexity_text: str = "",
) -> dict[str, Any]:
    """CPHI 페이지 텍스트 + Perplexity 검증 결과를 Claude Haiku로 파싱하여 구조화."""
    api_key = os.environ.get("CLAUDE_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return dict(_NULL_ENRICH)

    schema_str = json.dumps(_SCHEMA_DESC, ensure_ascii=False, indent=2)

    if full_page_text:
        cphi_context = f"[CPHI 전시회 등록 페이지 전체 텍스트]\n{full_page_text}"
    else:
        cphi_context = f"회사명: {company_name}, 국가: {country} (CPHI 페이지 텍스트 없음)"

    pplx_context = ""
    if perplexity_text:
        pplx_context = (
            f"\n\n[Perplexity 실시간 웹 검색 결과 — {target_country} 관련성]\n"
            f"{perplexity_text}\n"
            f"※ 위 웹 검색 결과를 최우선 근거로 삼아 has_target_country_presence 및 "
            f"recommendation_reason을 작성하세요."
        )

    prompt = f"""아래 정보를 종합하여 제약 기업 정보를 추출하고 JSON으로 반환하세요.

분석 대상: {company_name} ({country})
탐색 목적 제품: {product_label}
타깃 시장: {target_country} / {target_region}

추출 항목 (키: 설명):
{schema_str}

{cphi_context}{pplx_context}

작성 규칙:
- CPHI 텍스트와 Perplexity 웹 검색 결과를 모두 참조하여 작성.
- territories: 언급된 영업 국가/지역 배열
- certifications: USFDA / EU GMP / KFDA / EDQM 등 언급된 인증 배열
- has_target_country_presence: Perplexity 결과에 {target_country} 진출 증거가 있으면 true,
  명시적으로 없다면 false, 불명확하면 null
- has_gmp: GMP 관련 인증 텍스트 있으면 true
- company_overview_kr: 기업 소개 한국어 2~3문장 요약
- recommendation_reason:
    첫 문장: "{product_label}"과의 성분/치료군 연관성
    이후: {target_country} 시장 진출 여부(Perplexity 근거 포함)·인증·규모·강점을
    근거로 3~5문장 한국어 작성
- JSON만 반환 (```json 마크다운 없이)
"""

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1800,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            parsed = json.loads(m.group(0))
            for k, v in _NULL_ENRICH.items():
                if k not in parsed or parsed[k] == "":
                    parsed[k] = v
            return parsed
    except Exception:
        pass
    return dict(_NULL_ENRICH)


async def enrich_company(
    company: dict[str, Any],
    product_label: str = "",
    target_country: str = "Singapore",
    target_region: str = "Asia",
    emit: Callable[[str], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """단일 기업 심층조사 — CPHI 텍스트 + Perplexity 검증 → Claude Haiku."""
    name    = company.get("company_name", "-")
    country = company.get("country", "-")
    website = company.get("website", "-")

    # full_page_text 우선, 없으면 overview_text, 그 다음 알려진 필드로 컨텍스트 구성
    full_page_text = company.get("full_page_text", "") or company.get("overview_text", "")
    if not full_page_text:
        parts: list[str] = []
        if company.get("address") and company["address"] != "-":
            parts.append(f"주소: {company['address']}")
        if company.get("email") and company["email"] != "-":
            parts.append(f"이메일: {company['email']}")
        if company.get("category") and company["category"] != "-":
            parts.append(f"카테고리: {company['category']}")
        if company.get("products_cphi"):
            parts.append(f"제품 목록: {', '.join(company['products_cphi'][:15])}")
        if country and country != "-":
            parts.append(f"국가: {country}")
        full_page_text = "\n".join(parts)

    # ── Perplexity 실시간 검증 ───────────────────────────────────────────────
    perplexity_text = ""
    perplexity_citations: list[str] = []
    px_key = os.environ.get("PERPLEXITY_API_KEY", "").strip()

    # CF-prefixed ID는 실제 기업명이 아니므로 검색 스킵
    is_real_name = bool(name) and name != "-" and not re.match(r"^CF\w+$", name)

    if px_key and is_real_name:
        try:
            from utils.perplexity_searcher import verify_company as pplx_verify
            products_hint = ", ".join(company.get("products_cphi", [])[:5])
            if emit:
                await emit(f"    ↳ Perplexity 검증: {name}")
            pplx = await pplx_verify(
                name, products_hint, target_country, target_region
            )
            perplexity_text     = pplx.get("text", "")
            perplexity_citations = pplx.get("citations", [])
        except Exception as e:
            if emit:
                await emit(f"    ↳ Perplexity 오류: {e}")

    enriched = await _claude_extract(
        name, country, full_page_text,
        product_label, target_country, target_region,
        perplexity_text=perplexity_text,
    )

    # 웹사이트 + Perplexity 인용 출처를 source_urls에 추가
    existing_urls: list[str] = enriched.get("source_urls", [])
    for url in perplexity_citations:
        if url and url not in existing_urls:
            existing_urls.append(url)
    if website and website != "-" and website not in existing_urls:
        existing_urls.insert(0, website)
    enriched["source_urls"] = existing_urls

    for k, v in _NULL_ENRICH.items():
        if k not in enriched:
            enriched[k] = v

    return {**company, "enriched": enriched}


async def enrich_all(
    companies: list[dict[str, Any]],
    product_label: str = "",
    target_country: str = "Singapore",
    target_region: str = "Asia",
    emit: Callable[[str], Awaitable[None]] | None = None,
) -> list[dict[str, Any]]:
    """전체 기업 심층조사 (순차 — API 부하 조절)."""
    results: list[dict[str, Any]] = []
    total = len(companies)

    px_available = bool(os.environ.get("PERPLEXITY_API_KEY", "").strip())
    model_info = "Claude Haiku + Perplexity" if px_available else "Claude Haiku"
    if emit:
        await emit(f"심층조사 시작 / 모델: {model_info} / 타깃: {target_country} ({target_region})")

    for i, company in enumerate(companies, 1):
        name = company.get("company_name", company.get("exid", f"#{i}"))
        if emit:
            await emit(f"  [{i}/{total}] {name} 분석 중…")
        try:
            enriched = await enrich_company(
                company, product_label, target_country, target_region, emit
            )
        except Exception as e:
            if emit:
                await emit(f"  [{i}/{total}] {name} 오류: {e} → 폴백")
            enriched = {**company, "enriched": dict(_NULL_ENRICH)}
        results.append(enriched)
        await asyncio.sleep(0.8)

    return results
