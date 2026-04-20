"""Farmacias Ahumada(FASA) 칠레 소매 약가 크롤러.

FASA는 Cloudflare 보호 및 CAPTCHA가 적용될 가능성이 높습니다.
환경변수 SCRAPERAPI_KEY가 설정되어 있으면 ScraperAPI 프록시를 사용하고,
없으면 직접 요청(차단 시 빈 결과 반환)합니다.
"""
from __future__ import annotations

import asyncio
import os
import re
from decimal import Decimal
from typing import Any

import httpx
from bs4 import BeautifulSoup


_BASE_URL = "https://www.farmaciasahumada.cl"
_SEARCH_URL = f"{_BASE_URL}/on/demandware.store/Sites-ahumada-Site/es_CL/Search-Show"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-CL,es;q=0.9",
    "Referer": _BASE_URL,
}


def _parse_clp(text: str) -> Decimal | None:
    cleaned = re.sub(r"[^\d]", "", text or "")
    return Decimal(cleaned) if cleaned else None


def _build_url(inn_name: str) -> str:
    """ScraperAPI 프록시 URL 또는 직접 URL 반환."""
    scraperapi_key = os.environ.get("SCRAPERAPI_KEY", "").strip()
    target = f"{_SEARCH_URL}?q={inn_name.replace(' ', '+')}&format=page-element"
    if scraperapi_key:
        return (
            f"http://api.scraperapi.com?api_key={scraperapi_key}"
            f"&url={target}&country_code=cl&render=true"
        )
    return target


async def search_product(
    inn_name: str,
    timeout: float = 20.0,
) -> list[dict[str, Any]]:
    """INN 성분명으로 FASA 검색 → 가격 목록 반환."""
    url = _build_url(inn_name)
    try:
        async with httpx.AsyncClient(headers=_HEADERS, timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url)
            if resp.status_code in (403, 429, 503):
                return [{
                    "error": f"HTTP {resp.status_code} — Cloudflare 차단 의심. SCRAPERAPI_KEY 확인 요망",
                    "source": "ahumada",
                    "inn_name": inn_name,
                }]
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            return _parse_product_cards(soup, inn_name)
    except Exception as exc:
        return [{"error": str(exc), "source": "ahumada", "inn_name": inn_name}]


def _parse_product_cards(soup: BeautifulSoup, inn_name: str) -> list[dict[str, Any]]:
    """FASA Demandware SPA 구조 파싱."""
    items: list[dict[str, Any]] = []

    # Demandware/Salesforce Commerce Cloud 패턴
    for card in soup.select(".product-tile, .product-info, [class*='product-card']")[:10]:
        name_el = card.select_one(".product-name, .pdp-link a, [class*='name']")
        price_el = card.select_one(".price .value, [class*='price']:not([class*='old'])")
        if not price_el:
            continue
        price_clp = _parse_clp(price_el.get_text())
        if price_clp is None:
            continue

        link_el = card.select_one("a[href]")
        href = link_el.get("href", "") if link_el else ""
        full_url = href if href.startswith("http") else (_BASE_URL + href if href else _BASE_URL)

        items.append({
            "source": "ahumada",
            "inn_name": inn_name,
            "brand_name": name_el.get_text(strip=True) if name_el else "",
            "raw_price_clp": float(price_clp),
            "source_url": full_url,
            "raw_text": card.get_text(" ", strip=True)[:200],
        })

    return items


async def crawl(inn_names: list[str]) -> list[dict[str, Any]]:
    """여러 INN 성분을 순차 검색 (지연 시간 증가 — FASA 차단 방지)."""
    all_results: list[dict[str, Any]] = []
    for inn in inn_names:
        rows = await search_product(inn)
        all_results.extend(rows)
        await asyncio.sleep(2.5)  # FASA는 더 긴 지연
    return all_results
