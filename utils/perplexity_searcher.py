"""Perplexity Sonar API — 실시간 웹서칭.

두 가지 쿼리 유형:
  1. product_query  : 특정 성분/치료군을 취급하는 기업을 TARGET_COUNTRY에서 탐색
  2. company_query  : CPHI에서 찾은 기업이 TARGET_COUNTRY와 관련 있는지 검증

반환값: {"text": str, "citations": list[str]}
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Awaitable, Callable

import httpx

PPLX_ENDPOINT = "https://api.perplexity.ai/chat/completions"
PPLX_MODEL    = "sonar"
_TIMEOUT      = 30.0
_DELAY        = 1.2  # 연속 호출 간격(초)


def _api_key() -> str:
    key = os.environ.get("PERPLEXITY_API_KEY", "")
    if not key:
        raise RuntimeError("PERPLEXITY_API_KEY 환경변수가 설정되지 않았습니다.")
    return key


async def _pplx_query(prompt: str) -> dict[str, Any]:
    """Perplexity Sonar 단일 쿼리 → {text, citations}."""
    headers = {
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type": "application/json",
    }
    body = {
        "model": PPLX_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a pharmaceutical market research assistant. "
                    "Answer factually and concisely. "
                    "Focus on company names, locations, products, and market presence."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 1024,
        "return_citations": True,
        "search_recency_filter": "month",
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.post(PPLX_ENDPOINT, headers=headers, json=body)
        r.raise_for_status()
        data = r.json()

    text      = data["choices"][0]["message"]["content"]
    citations = data.get("citations", [])
    return {"text": text, "citations": citations}


# ── 쿼리 템플릿 ────────────────────────────────────────────────────────────────

def _product_queries(
    ingredient: str,
    therapeutic: str,
    target_country: str,
    target_region: str,
) -> list[str]:
    """성분/치료군 기반 — 해당 국가/지역에서 취급 기업 탐색."""
    return [
        (
            f"List pharmaceutical companies or distributors that supply or market "
            f"{ingredient} ({therapeutic}) in {target_country} or {target_region}. "
            f"Include company name, country, website if available."
        ),
        (
            f"Who are the importers, distributors, or licensees of {ingredient} "
            f"pharmaceutical products targeting the {target_country} market? "
            f"Provide company names and any available contact or web information."
        ),
    ]


def _company_query(
    company_name: str,
    products_hint: str,
    target_country: str,
    target_region: str,
) -> str:
    """기업 단건 — Singapore/Asia 시장 관련성 검증."""
    return (
        f"Does {company_name} have business presence, distribution, or partnerships "
        f"in {target_country} or {target_region}? "
        f"What pharmaceutical products do they sell or distribute there? "
        f"Products context: {products_hint or 'general pharmaceutical'}. "
        f"Provide factual details only."
    )


# ── 공개 API ──────────────────────────────────────────────────────────────────

async def search_by_product(
    ingredient: str,
    therapeutic: str,
    target_country: str = "Singapore",
    target_region: str = "Asia",
    emit: Callable[[str], Awaitable[None]] | None = None,
) -> list[dict[str, Any]]:
    """
    성분/치료군 키워드로 Perplexity 검색 → 쿼리당 결과 반환.

    반환: [{"query": str, "text": str, "citations": list[str]}, ...]
    """
    queries = _product_queries(ingredient, therapeutic, target_country, target_region)
    results: list[dict[str, Any]] = []

    for q in queries:
        if emit:
            await emit(f"  Perplexity 검색: {q[:80]}…")
        try:
            res = await _pplx_query(q)
            results.append({"query": q, **res})
        except Exception as e:
            if emit:
                await emit(f"  Perplexity 오류: {e}")
            results.append({"query": q, "text": "", "citations": []})
        await asyncio.sleep(_DELAY)

    return results


async def verify_company(
    company_name: str,
    products_hint: str = "",
    target_country: str = "Singapore",
    target_region: str = "Asia",
    emit: Callable[[str], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """
    단일 기업 → Perplexity로 TARGET_COUNTRY 관련성 검증.

    반환: {"text": str, "citations": list[str]}
    """
    q = _company_query(company_name, products_hint, target_country, target_region)
    if emit:
        await emit(f"  [{company_name}] Perplexity 검증 중…")
    try:
        res = await _pplx_query(q)
        await asyncio.sleep(_DELAY)
        return res
    except Exception as e:
        if emit:
            await emit(f"  [{company_name}] Perplexity 오류: {e}")
        return {"text": "", "citations": []}


async def batch_verify_companies(
    companies: list[dict[str, Any]],
    target_country: str = "Singapore",
    target_region: str = "Asia",
    emit: Callable[[str], Awaitable[None]] | None = None,
) -> list[dict[str, Any]]:
    """
    기업 목록 전체 검증 — 각 기업에 verify_company 실행 후 perplexity_text 필드 추가.

    반환: company dict + {"perplexity_text": str, "perplexity_citations": list[str]}
    """
    results: list[dict[str, Any]] = []
    total = len(companies)

    for i, company in enumerate(companies, 1):
        name = company.get("company_name", f"#{i}")
        products = ", ".join(company.get("products_cphi", [])[:5])
        overview = company.get("overview_text", "")
        products_hint = products or overview[:200]

        if emit:
            await emit(f"Perplexity 검증 [{i}/{total}] {name}")

        pplx = await verify_company(
            name, products_hint, target_country, target_region, emit
        )
        results.append({
            **company,
            "perplexity_text":      pplx["text"],
            "perplexity_citations": pplx["citations"],
        })

    return results
