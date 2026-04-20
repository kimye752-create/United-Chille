"""Salcobrand 칠레 소매 약가 크롤러 (JSON API 역추적 방식).

Salcobrand 프론트엔드는 내부 REST API를 호출하므로,
브라우저 네트워크 탭에서 캡처한 JSON 엔드포인트를 직접 Fetch합니다.
멤버십 할인가(Precio Socio)와 일반가(Precio Normal)를 모두 수집합니다.
"""
from __future__ import annotations

import asyncio
import re
from decimal import Decimal
from typing import Any

import httpx


_API_BASE = "https://www.salcobrand.cl"
# Salcobrand 검색 API 엔드포인트 (역추적 확인 필요 — 변경 시 업데이트)
_SEARCH_API = f"{_API_BASE}/api/2/json/search/product/search"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-CL,es;q=0.9",
    "Referer": _API_BASE,
    "X-Requested-With": "XMLHttpRequest",
}


def _parse_clp(val: Any) -> Decimal | None:
    if val is None:
        return None
    cleaned = re.sub(r"[^\d]", "", str(val))
    return Decimal(cleaned) if cleaned else None


async def search_product(
    inn_name: str,
    timeout: float = 15.0,
) -> list[dict[str, Any]]:
    """INN 성분명으로 Salcobrand API 검색 → 가격 목록 반환."""
    params = {
        "search": inn_name,
        "page": 1,
        "rows": 10,
    }
    try:
        async with httpx.AsyncClient(headers=_HEADERS, timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(_SEARCH_API, params=params)
            resp.raise_for_status()
            data = resp.json()
            return _parse_api_response(data, inn_name)
    except Exception as exc:
        # API 구조 변경 시 HTML 폴백
        return await _search_html_fallback(inn_name, timeout, str(exc))


async def _search_html_fallback(inn_name: str, timeout: float, prev_error: str) -> list[dict[str, Any]]:
    """API 실패 시 HTML 검색 결과 파싱."""
    from bs4 import BeautifulSoup
    url = f"{_API_BASE}/buscar?q={inn_name.replace(' ', '+')}"
    try:
        async with httpx.AsyncClient(headers={**_HEADERS, "Accept": "text/html"}, timeout=timeout) as client:
            resp = await client.get(url)
            soup = BeautifulSoup(resp.text, "html.parser")
            items: list[dict[str, Any]] = []
            for card in soup.select(".product-card, [class*='product-item']")[:10]:
                price_el = card.select_one("[class*='price'], [class*='precio']")
                if not price_el:
                    continue
                price_clp = _parse_clp(re.sub(r"[^\d]", "", price_el.get_text()))
                if price_clp:
                    name_el = card.select_one("[class*='name'], h2, h3")
                    items.append({
                        "source": "salcobrand",
                        "inn_name": inn_name,
                        "brand_name": name_el.get_text(strip=True) if name_el else "",
                        "raw_price_clp": float(price_clp),
                        "source_url": url,
                        "raw_text": card.get_text(" ", strip=True)[:200],
                    })
            return items or [{"error": prev_error, "source": "salcobrand", "inn_name": inn_name}]
    except Exception as exc2:
        return [{"error": str(exc2), "source": "salcobrand", "inn_name": inn_name}]


def _parse_api_response(data: Any, inn_name: str) -> list[dict[str, Any]]:
    """Salcobrand JSON API 응답 파싱."""
    items: list[dict[str, Any]] = []
    products = (
        data.get("products")
        or data.get("results")
        or data.get("items")
        or (data if isinstance(data, list) else [])
    )
    for prod in products[:10]:
        if not isinstance(prod, dict):
            continue
        normal_price = _parse_clp(prod.get("price") or prod.get("normalPrice") or prod.get("precio"))
        member_price = _parse_clp(prod.get("memberPrice") or prod.get("precioSocio") or prod.get("discountPrice"))
        if normal_price is None:
            continue

        items.append({
            "source": "salcobrand",
            "inn_name": inn_name,
            "brand_name": prod.get("name") or prod.get("nombre") or "",
            "raw_price_clp": float(normal_price),
            "member_price_clp": float(member_price) if member_price else None,
            "source_url": (
                f"{_API_BASE}{prod.get('url', '')}" if prod.get("url") else _API_BASE
            ),
            "raw_text": str(prod)[:200],
        })
    return items


async def crawl(inn_names: list[str]) -> list[dict[str, Any]]:
    """여러 INN 성분을 순차 검색하여 결과 합산."""
    all_results: list[dict[str, Any]] = []
    for inn in inn_names:
        rows = await search_product(inn)
        all_results.extend(rows)
        await asyncio.sleep(1.5)
    return all_results
