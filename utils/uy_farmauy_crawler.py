"""Farma.uy 소매 약가 크롤러 — 쿼리 스트링 INN 타기팅.

수집 전략:
  GET https://farma.uy/search?q={inn_name}
  정적 HTML → BeautifulSoup 파싱 → uy_parser.py 전달
  품절(out of stock) 상태까지 수집하여 재고 메타 저장
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup  # type: ignore[import]

from utils.backoff_retry import with_retry
from utils.uy_parser import ParsedDrug, parse_drug_text

BASE_URL = "https://farma.uy"
SEARCH_URL = f"{BASE_URL}/search"
RATE_LIMIT_DELAY = 1.0

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-UY,es;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


@dataclass
class FarmaUyRaw:
    product_name: str
    price_uyu: str
    discount_price_uyu: str | None
    manufacturer: str
    in_stock: bool
    url: str
    raw_text: str


def _parse_search_results(html: str, base_url: str) -> list[FarmaUyRaw]:
    soup = BeautifulSoup(html, "html.parser")
    results: list[FarmaUyRaw] = []

    for card in soup.select(".product-item, .product-card, article.product, .item-product"):
        name_el = card.select_one(
            ".product-name, .product-title, h2, h3, .name, [class*='title']"
        )
        if not name_el:
            continue
        name = name_el.get_text(strip=True)

        price_el = card.select_one(
            ".price, .product-price, [class*='price']:not([class*='old']):not([class*='regular'])"
        )
        price_str = price_el.get_text(strip=True) if price_el else ""
        price_clean = re.sub(r"[^\d.,]", "", price_str).replace(",", ".")
        if not price_clean:
            continue

        discount_el = card.select_one(".price-sale, .price-discount, .special-price, .old-price")
        discount_str = discount_el.get_text(strip=True) if discount_el else None
        discount_clean = (
            re.sub(r"[^\d.,]", "", discount_str).replace(",", ".") if discount_str else None
        )

        mfr_el = card.select_one(".brand, .manufacturer, .lab, [class*='brand']")
        manufacturer = mfr_el.get_text(strip=True) if mfr_el else "-"

        link_el = card.select_one("a[href]")
        product_url = (
            f"{base_url}{link_el['href']}"
            if link_el and str(link_el["href"]).startswith("/")
            else (str(link_el["href"]) if link_el else "")
        )

        stock_text = card.get_text(strip=True).lower()
        in_stock = "sin stock" not in stock_text and "agotado" not in stock_text

        raw_text = f"{name} {price_str} {manufacturer}"
        results.append(
            FarmaUyRaw(
                product_name=name,
                price_uyu=price_clean,
                discount_price_uyu=discount_clean,
                manufacturer=manufacturer,
                in_stock=in_stock,
                url=product_url,
                raw_text=raw_text,
            )
        )
    return results


async def _fetch_html(client: httpx.AsyncClient, url: str) -> str:
    async def _do() -> str:
        resp = await client.get(url, headers=_HEADERS, follow_redirects=True)
        resp.raise_for_status()
        return resp.text

    return await with_retry(_do)


async def crawl_farmauy(
    inn_name: str,
    max_pages: int = 3,
) -> list[ParsedDrug | None]:
    results: list[FarmaUyRaw] = []
    async with httpx.AsyncClient(timeout=20.0, http2=True) as client:
        for page in range(1, max_pages + 1):
            params = f"?q={quote(inn_name)}&page={page}"
            url = f"{SEARCH_URL}{params}"
            html = await _fetch_html(client, url)
            page_results = _parse_search_results(html, BASE_URL)
            if not page_results:
                break
            results.extend(page_results)
            await asyncio.sleep(RATE_LIMIT_DELAY)

    parsed: list[ParsedDrug | None] = []
    for raw in results:
        combined_text = f"{raw.product_name} {raw.price_uyu} UYU {raw.manufacturer}"
        drug = await parse_drug_text(
            raw_text=combined_text,
            source_site="farmauy",
            source_url=raw.url,
        )
        if drug:
            drug.extra["in_stock"] = raw.in_stock
        parsed.append(drug)

    return parsed


async def crawl_farmauy_multi(
    inn_names: list[str],
) -> dict[str, list[ParsedDrug | None]]:
    results: dict[str, list[ParsedDrug | None]] = {}
    for inn in inn_names:
        results[inn] = await crawl_farmauy(inn)
        await asyncio.sleep(RATE_LIMIT_DELAY)
    return results
