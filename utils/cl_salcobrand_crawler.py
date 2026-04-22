"""Salcobrand 칠레 소매 약가 크롤러.

Salcobrand는 SPA(Ruby on Rails + React 하이브리드) 구조:
  - /search_result?query=... : JS로 렌더링되는 검색 결과 페이지
  - /api/v2/products : 인증 필요 REST API
  - /products/{slug} : 개별 제품 페이지

Playwright 설치 시 자동으로 헤드리스 렌더링 사용.
없으면 httpx 정적 파싱 시도(내용 없을 수 있음).
멤버십 할인가(Precio Socio)와 일반가(Precio Normal)를 모두 수집합니다.
"""
from __future__ import annotations

import asyncio
import re
from decimal import Decimal
from typing import Any

import httpx

try:
    from utils.cl_antibot import pick_ua
    from utils.cl_backoff_retry import async_with_backoff, RetryExhausted
except ImportError:
    def pick_ua() -> str:  # type: ignore[misc]
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    def async_with_backoff(**kw):  # type: ignore[misc]
        def deco(f): return f
        return deco
    class RetryExhausted(Exception): pass  # type: ignore[misc]


_API_BASE   = "https://salcobrand.cl"              # www 없음 (www.→ salcobrand.cl 리다이렉트)
# REST API 엔드포인트 (v2 인증 필요) — HTML 폴백 우선
_SEARCH_ENDPOINTS = [
    f"{_API_BASE}/api/2/json/search/product/search",   # v2 (인증 필요할 수 있음)
    f"{_API_BASE}/api/search",                          # v3 후보
]
_SEARCH_HTML_URL = f"{_API_BASE}/search_result"        # 실제 작동하는 검색 결과 URL


def _make_headers(accept: str = "application/json") -> dict[str, str]:
    return {
        "User-Agent":        pick_ua(),
        "Accept":            accept,
        "Accept-Language":   "es-CL,es;q=0.9",
        "Referer":           _API_BASE,
        "X-Requested-With":  "XMLHttpRequest",
    }


def _parse_clp(val: Any) -> Decimal | None:
    if val is None:
        return None
    cleaned = re.sub(r"[^\d]", "", str(val))
    return Decimal(cleaned) if cleaned else None


async def _try_api_endpoint(url: str, params: dict, headers: dict, timeout: float) -> httpx.Response | None:
    """단일 API 엔드포인트 시도. 실패 시 None 반환 (예외 미전파)."""
    try:
        async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            # JSON 응답인지 확인
            ct = resp.headers.get("content-type", "")
            if "json" not in ct and "javascript" not in ct:
                return None
            return resp
    except Exception:
        return None


async def search_product(
    inn_name: str,
    timeout: float = 20.0,
) -> list[dict[str, Any]]:
    """INN 성분명으로 Salcobrand 검색 → 가격 목록 반환.

    Playwright 설치 시 /search_result?query= 페이지 렌더링.
    없으면 REST API → httpx HTML 순서로 폴백.
    """
    # Playwright 자동 감지
    try:
        import playwright.async_api  # noqa: F401
        playwright_available = True
    except ImportError:
        playwright_available = False

    if playwright_available:
        items = await _search_playwright(inn_name, timeout)
        if items:
            return items

    # REST API 시도
    params_variants = [
        {"search": inn_name, "page": 1, "rows": 10},
        {"q": inn_name, "page": 1, "limit": 10},
    ]
    headers = _make_headers()
    for i, endpoint in enumerate(_SEARCH_ENDPOINTS):
        params = params_variants[i] if i < len(params_variants) else params_variants[0]
        resp = await _try_api_endpoint(endpoint, params, headers, timeout)
        if resp is not None:
            try:
                data = resp.json()
                items = _parse_api_response(data, inn_name)
                if items:
                    return items
            except Exception:
                pass

    # 최종 폴백: httpx 정적 파싱
    return await _search_html_fallback(inn_name, timeout, "모든 경로 실패")


async def _search_playwright(inn_name: str, timeout: float) -> list[dict[str, Any]]:
    """Playwright로 Salcobrand /search_result?query= 렌더링 후 파싱."""
    from playwright.async_api import async_playwright
    from bs4 import BeautifulSoup

    url = f"{_SEARCH_HTML_URL}?query={inn_name.replace(' ', '+')}"
    headers = _make_headers(accept="text/html,*/*")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=headers["User-Agent"],
            locale="es-CL",
        )
        page = await context.new_page()
        try:
            await page.goto(url, timeout=int(timeout * 1000), wait_until="networkidle")
        except Exception:
            pass
        await page.wait_for_timeout(3000)

        html = await page.content()
        body_text = await page.evaluate("document.body.innerText || ''")
        await context.close()
        await browser.close()

    if not body_text or len(body_text) < 100:
        return []

    soup = BeautifulSoup(html, "html.parser")
    return _parse_product_cards_sb(soup, inn_name, url)


def _parse_product_cards_sb(soup: Any, inn_name: str, page_url: str) -> list[dict[str, Any]]:
    """Salcobrand 상품 카드 파싱 (Playwright 렌더링 또는 정적 HTML 공용)."""
    from bs4 import BeautifulSoup
    items: list[dict[str, Any]] = []

    card_selectors = [
        ".product-card", "[class*='product-item']", "[class*='product-tile']",
        ".product", "article.product", "li[class*='product']",
        "[data-product-id]", "[class*='item']",
    ]
    cards: list = []
    for sel in card_selectors:
        cards = soup.select(sel)
        if len(cards) >= 2:  # 의미 있는 개수
            break

    for card in cards[:10]:
        price_el = card.select_one(
            ".precio-normal, .price-normal, [class*='precio-normal'], "
            "[class*='price-normal'], .price, [class*='precio']:not([class*='descuento'])"
        )
        if not price_el:
            for candidate in card.select("span, p, div"):
                txt = candidate.get_text(strip=True)
                if re.match(r'^\$?\s*[\d.]{4,}$', txt.replace(",", "")):
                    price_el = candidate
                    break
        if not price_el:
            continue

        price_clp = _parse_clp(re.sub(r"[^\d]", "", price_el.get_text()))
        if not price_clp or int(price_clp) < 100:
            continue

        name_el   = card.select_one(".product-name, .name, [class*='product-name'], [class*='nombre'], h2, h3, h4")
        link_el   = card.select_one("a[href]")
        href      = link_el.get("href", "") if link_el else ""
        full_url  = href if href.startswith("http") else (_API_BASE + href if href else page_url)
        member_el = card.select_one(".precio-socio, .price-member, [class*='precio-socio'], [class*='price-member'], [class*='socio']")
        member_price = _parse_clp(re.sub(r"[^\d]", "", member_el.get_text())) if member_el else None

        items.append({
            "source":           "salcobrand",
            "inn_name":         inn_name,
            "brand_name":       name_el.get_text(strip=True)[:100] if name_el else "",
            "raw_price_clp":    float(price_clp),
            "member_price_clp": float(member_price) if member_price else None,
            "source_url":       full_url,
            "raw_text":         card.get_text(" ", strip=True)[:200],
        })
    return items


@async_with_backoff(max_attempts=2, base=4.0, max_wait=20.0)
async def _fetch_salcobrand_html(url: str, headers: dict, timeout: float) -> httpx.Response:
    async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp


async def _search_html_fallback(inn_name: str, timeout: float, prev_error: str) -> list[dict[str, Any]]:
    """최종 폴백: httpx 정적 파싱 (SPA이므로 대부분 비어있음)."""
    from bs4 import BeautifulSoup
    url     = f"{_SEARCH_HTML_URL}?query={inn_name.replace(' ', '+')}"
    headers = _make_headers(accept="text/html")
    try:
        resp  = await _fetch_salcobrand_html(url, headers, timeout)
        soup  = BeautifulSoup(resp.text, "html.parser")
        items = _parse_product_cards_sb(soup, inn_name, url)
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
            "source":           "salcobrand",
            "inn_name":         inn_name,
            "brand_name":       prod.get("name") or prod.get("nombre") or "",
            "raw_price_clp":    float(normal_price),
            "member_price_clp": float(member_price) if member_price else None,
            "source_url":       (
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
