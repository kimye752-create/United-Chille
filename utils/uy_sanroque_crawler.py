"""San Roque (sanroque.com.uy) 소매 크롤러 — 카테고리 ID 직접 타기팅.

수집 전략:
  카테고리 엔드포인트를 직접 호출하여 의약품 목록 수집 (전체 스파이더링 금지)
  INN 키워드로 제품명 필터링 후 uy_parser.py 전달
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup  # type: ignore[import]

from utils.backoff_retry import with_retry
from utils.uy_parser import ParsedDrug, parse_drug_text

BASE_URL = "https://sanroque.com.uy"
RATE_LIMIT_DELAY = 2.0

CATEGORY_ENDPOINTS: dict[str, str] = {
    "cardiovascular": f"{BASE_URL}/medicamentos/cardiovascular",
    "medicamentos": f"{BASE_URL}/medicamentos",
    "search": f"{BASE_URL}/buscar",
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-UY,es;q=0.9",
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
}


@dataclass
class SanRoqueRaw:
    product_name: str
    price_uyu: str
    promo_price_uyu: str | None
    category: str
    url: str
    raw_text: str


def _parse_sanroque_listing(html: str, category: str) -> list[SanRoqueRaw]:
    soup = BeautifulSoup(html, "html.parser")
    items: list[SanRoqueRaw] = []

    for card in soup.select(
        ".product, .product-item, .product-card, [class*='product-'], article"
    ):
        name_el = card.select_one(
            ".product-name, .product-title, h2, h3, [class*='name'], [class*='title']"
        )
        if not name_el:
            continue
        name = name_el.get_text(strip=True)
        if not name:
            continue

        price_el = card.select_one(
            ".price:not(.price-old):not(.price-before), "
            ".product-price, [class*='price-current'], [class*='price-main']"
        )
        if not price_el:
            continue
        price_raw = price_el.get_text(strip=True)
        price_clean = re.sub(r"[^\d.,]", "", price_raw).replace(",", ".")
        if not price_clean:
            continue

        promo_el = card.select_one(
            ".price-promo, .special-price, [class*='promo'], [class*='oferta']"
        )
        promo_str: str | None = None
        if promo_el:
            promo_raw = promo_el.get_text(strip=True)
            promo_clean = re.sub(r"[^\d.,]", "", promo_raw).replace(",", ".")
            if promo_clean:
                promo_str = promo_clean

        link_el = card.select_one("a[href]")
        product_url = (
            f"{BASE_URL}{link_el['href']}"
            if link_el and str(link_el["href"]).startswith("/")
            else (str(link_el["href"]) if link_el else "")
        )

        raw_text = f"{name} {price_raw}"
        items.append(
            SanRoqueRaw(
                product_name=name,
                price_uyu=price_clean,
                promo_price_uyu=promo_str,
                category=category,
                url=product_url,
                raw_text=raw_text,
            )
        )
    return items


def _filter_by_inn(items: list[SanRoqueRaw], inn_name: str) -> list[SanRoqueRaw]:
    keyword = inn_name.lower()
    return [i for i in items if keyword in i.product_name.lower() or keyword in i.raw_text.lower()]


async def _fetch_html(client: httpx.AsyncClient, url: str) -> str:
    async def _do() -> str:
        resp = await client.get(url, headers=_HEADERS, follow_redirects=True)
        resp.raise_for_status()
        return resp.text

    return await with_retry(_do)


async def crawl_sanroque(
    inn_name: str,
    use_search: bool = True,
    max_category_pages: int = 2,
) -> list[ParsedDrug | None]:
    raw_items: list[SanRoqueRaw] = []

    async with httpx.AsyncClient(timeout=25.0) as client:
        if use_search:
            search_url = f"{CATEGORY_ENDPOINTS['search']}?q={quote(inn_name)}"
            html = await _fetch_html(client, search_url)
            raw_items.extend(_parse_sanroque_listing(html, "search"))
            await asyncio.sleep(RATE_LIMIT_DELAY)

        if not raw_items:
            for cat_name, cat_url in CATEGORY_ENDPOINTS.items():
                if cat_name == "search":
                    continue
                for page in range(1, max_category_pages + 1):
                    url = f"{cat_url}?page={page}"
                    html = await _fetch_html(client, url)
                    page_items = _parse_sanroque_listing(html, cat_name)
                    if not page_items:
                        break
                    raw_items.extend(page_items)
                    await asyncio.sleep(RATE_LIMIT_DELAY)

    filtered = _filter_by_inn(raw_items, inn_name)
    if not filtered:
        filtered = raw_items[:20]

    parsed: list[ParsedDrug | None] = []
    for raw in filtered:
        drug = await parse_drug_text(
            raw_text=raw.raw_text,
            source_site="sanroque",
            source_url=raw.url,
        )
        if drug and raw.promo_price_uyu:
            drug.extra["promo_price_uyu"] = raw.promo_price_uyu
        parsed.append(drug)
    return parsed
