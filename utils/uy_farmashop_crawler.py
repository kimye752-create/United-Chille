"""Farmashop (tienda.farmashop.com.uy) Playwright 크롤러 — 이중 가격 DOM 분리.

핵심 방어:
  Farmacard 할인가와 일반 정가를 DOM 클래스로 엄격히 분리.
  regular_price → raw_price (기본값 / FOB 역산 입력)
  farmacard_price → 별도 메타 컬럼 저장 (프로모션 분석용)

환경변수 PLAYWRIGHT_LIVE=1 시 실제 브라우저 실행.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import quote

from utils.uy_parser import ParsedDrug, _safe_decimal, parse_drug_text

BASE_URL = "https://tienda.farmashop.com.uy"
SEARCH_URL = f"{BASE_URL}/buscar"
CARDIO_CATEGORY_URL = f"{BASE_URL}/medicamentos/cardiovasculares"
RATE_LIMIT_DELAY = 2.0

LIVE = os.environ.get("PLAYWRIGHT_LIVE", "0") == "1"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-UY,es;q=0.9",
}

# DOM 셀렉터 — Farmashop 실제 구조에 맞게 우선순위 정렬
# regular_price 셀렉터: 일반 정가 (기본값)
_REGULAR_PRICE_SELECTORS = [
    "[data-price-type='regular']",
    ".price-regular",
    ".price-normal",
    ".vtex-product-price-1-x-sellingPrice",
    ".vtex-product-price-1-x-listPrice",
    "span.price:not([class*='member']):not([class*='farmacard'])",
    ".product-price:not([class*='farmacard'])",
]

# farmacard_price 셀렉터: 회원 할인가 (별도 저장)
_FARMACARD_PRICE_SELECTORS = [
    "[data-price-type='member']",
    "[data-price-type='farmacard']",
    ".price-farmacard",
    ".price-member",
    "[class*='farmacard']",
    "[class*='member-price']",
]


@dataclass
class FarmashopRaw:
    product_name: str
    regular_price_uyu: str
    farmacard_price_uyu: str | None
    url: str
    raw_text: str


def _extract_price(el_text: str) -> str:
    return re.sub(r"[^\d.,]", "", el_text).replace(",", ".")


def _parse_playwright_html(html: str) -> list[FarmashopRaw]:
    from bs4 import BeautifulSoup  # type: ignore[import]

    soup = BeautifulSoup(html, "html.parser")
    items: list[FarmashopRaw] = []

    product_cards = soup.select(
        ".vtex-product-summary-2-x-element, "
        ".product-item, .product-card, "
        "article[class*='product'], "
        "[class*='productSummary']"
    )

    for card in product_cards:
        name_el = card.select_one(
            ".vtex-product-summary-2-x-productNameContainer, "
            ".product-name, h2, h3, [class*='productName']"
        )
        if not name_el:
            continue
        name = name_el.get_text(strip=True)
        if not name:
            continue

        regular_price_str = ""
        for sel in _REGULAR_PRICE_SELECTORS:
            el = card.select_one(sel)
            if el:
                raw = el.get_text(strip=True)
                clean = _extract_price(raw)
                if clean:
                    regular_price_str = clean
                    break
        if not regular_price_str:
            continue

        farmacard_price_str: str | None = None
        for sel in _FARMACARD_PRICE_SELECTORS:
            el = card.select_one(sel)
            if el:
                raw = el.get_text(strip=True)
                clean = _extract_price(raw)
                if clean and clean != regular_price_str:
                    farmacard_price_str = clean
                    break

        link_el = card.select_one("a[href]")
        product_url = (
            f"{BASE_URL}{link_el['href']}"
            if link_el and str(link_el["href"]).startswith("/")
            else (str(link_el["href"]) if link_el else "")
        )

        raw_text = f"{name} {regular_price_str} UYU"
        items.append(
            FarmashopRaw(
                product_name=name,
                regular_price_uyu=regular_price_str,
                farmacard_price_uyu=farmacard_price_str,
                url=product_url,
                raw_text=raw_text,
            )
        )
    return items


async def _fetch_with_playwright(url: str) -> str:
    from playwright.async_api import async_playwright  # type: ignore[import]

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not LIVE)
        page = await browser.new_page(extra_http_headers=_HEADERS)
        await page.goto(url, wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(2000)

        for _ in range(3):
            await page.evaluate("window.scrollBy(0, 800)")
            await page.wait_for_timeout(500)

        html = await page.content()
        await browser.close()
        return html


async def _fetch_html_safe(url: str) -> str:
    if not LIVE:
        try:
            import httpx

            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(url, headers=_HEADERS, follow_redirects=True)
                resp.raise_for_status()
                return resp.text
        except Exception:
            pass

    return await _fetch_with_playwright(url)


async def crawl_farmashop(
    inn_name: str,
    max_pages: int = 3,
) -> list[ParsedDrug | None]:
    raw_items: list[FarmashopRaw] = []

    for page in range(1, max_pages + 1):
        url = f"{SEARCH_URL}?q={quote(inn_name)}&page={page}"
        html = await _fetch_html_safe(url)
        page_items = _parse_playwright_html(html)
        if not page_items:
            break
        raw_items.extend(page_items)
        await asyncio.sleep(RATE_LIMIT_DELAY)

    if not raw_items:
        html = await _fetch_html_safe(CARDIO_CATEGORY_URL)
        cat_items = _parse_playwright_html(html)
        keyword = inn_name.lower()
        raw_items = [
            i for i in cat_items if keyword in i.product_name.lower()
        ]

    parsed: list[ParsedDrug | None] = []
    for raw in raw_items:
        farmacard_dec = _safe_decimal(raw.farmacard_price_uyu)
        drug = await parse_drug_text(
            raw_text=raw.raw_text,
            source_site="farmashop",
            source_url=raw.url,
            farmacard_price_uyu=farmacard_dec,
        )
        parsed.append(drug)

    return parsed
