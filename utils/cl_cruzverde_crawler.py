"""Cruz Verde 칠레 소매 약가 크롤러 (SPA 동적 렌더링 대응).

Cruz Verde는 SPA(Single Page App) 구조이므로 Playwright로 DOM 렌더링을
완전히 대기한 후 가격 태그를 파싱합니다.

환경변수:
  PLAYWRIGHT_LIVE=1  : 헤드풀 브라우저 (디버깅용)
  PLAYWRIGHT_LIVE=0  : 헤드리스 (기본, 운영)
"""
from __future__ import annotations

import asyncio
import os
import re
from decimal import Decimal
from typing import Any

import httpx
from bs4 import BeautifulSoup


_BASE_URL = "https://www.cruzverde.cl"
_SEARCH_URL = f"{_BASE_URL}/buscar?query="

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
    "Referer": _BASE_URL,
}


def _parse_clp(text: str) -> Decimal | None:
    """'$12.345' 또는 '12345' → Decimal 변환."""
    cleaned = re.sub(r"[^\d]", "", text or "")
    return Decimal(cleaned) if cleaned else None


async def search_product(
    inn_name: str,
    timeout: float = 15.0,
) -> list[dict[str, Any]]:
    """INN 성분명으로 Cruz Verde 검색 → 가격 목록 반환."""
    results: list[dict[str, Any]] = []

    playwright_live = os.environ.get("PLAYWRIGHT_LIVE", "0").strip() == "1"
    if playwright_live:
        results = await _search_playwright(inn_name, timeout)
    else:
        results = await _search_httpx(inn_name, timeout)

    return results


async def _search_httpx(inn_name: str, timeout: float) -> list[dict[str, Any]]:
    """정적 HTML 폴백 — Playwright 미사용 시."""
    url = _SEARCH_URL + inn_name.replace(" ", "+")
    try:
        async with httpx.AsyncClient(headers=_HEADERS, timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            return _parse_product_cards(soup, inn_name)
    except Exception as exc:
        return [{"error": str(exc), "source": "cruzverde", "inn_name": inn_name}]


async def _search_playwright(inn_name: str, timeout: float) -> list[dict[str, Any]]:
    """Playwright SPA 렌더링 후 파싱."""
    try:
        from playwright.async_api import async_playwright  # type: ignore[import]
    except ImportError:
        return await _search_httpx(inn_name, timeout)

    url = _SEARCH_URL + inn_name.replace(" ", "+")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_extra_http_headers(_HEADERS)
        try:
            await page.goto(url, timeout=int(timeout * 1000))
            # 가격 요소가 렌더링될 때까지 대기
            await page.wait_for_selector("[class*='price'], [class*='precio']", timeout=8000)
            html = await page.content()
            soup = BeautifulSoup(html, "html.parser")
            return _parse_product_cards(soup, inn_name)
        except Exception as exc:
            return [{"error": str(exc), "source": "cruzverde", "inn_name": inn_name}]
        finally:
            await browser.close()


def _parse_product_cards(soup: BeautifulSoup, inn_name: str) -> list[dict[str, Any]]:
    """BeautifulSoup에서 상품 카드 파싱."""
    items: list[dict[str, Any]] = []

    # Cruz Verde DOM 패턴: .product-card, [data-product-name], span[class*=price]
    cards = soup.select(".product-card, article[class*='product'], .product-item")
    if not cards:
        # 폴백: 가격 포함된 모든 요소 스캔
        cards = soup.select("[class*='product']")

    for card in cards[:10]:
        name_el = card.select_one("[class*='name'], [class*='title'], h2, h3")
        price_el = card.select_one("[class*='price'], [class*='precio']")
        if not price_el:
            continue

        brand = name_el.get_text(strip=True) if name_el else ""
        price_text = price_el.get_text(strip=True)
        price_clp = _parse_clp(price_text)
        if price_clp is None:
            continue

        link_el = card.select_one("a[href]")
        url = (_BASE_URL + link_el["href"]) if link_el else ""

        items.append({
            "source": "cruzverde",
            "inn_name": inn_name,
            "brand_name": brand,
            "raw_price_clp": float(price_clp),
            "source_url": url,
            "raw_text": card.get_text(" ", strip=True)[:200],
        })

    return items


async def crawl(inn_names: list[str]) -> list[dict[str, Any]]:
    """여러 INN 성분을 순차 검색하여 결과 합산."""
    all_results: list[dict[str, Any]] = []
    for inn in inn_names:
        rows = await search_product(inn)
        all_results.extend(rows)
        await asyncio.sleep(1.5)  # 요청 간 지연 (IP 차단 방지)
    return all_results
