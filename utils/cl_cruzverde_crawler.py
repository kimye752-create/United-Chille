"""Cruz Verde 칠레 소매 약가 크롤러 (SPA 동적 렌더링 대응).

Cruz Verde는 SPA(Single Page App) 구조이므로 Playwright로 DOM 렌더링을
완전히 대기한 후 가격 태그를 파싱합니다.

환경변수:
  PLAYWRIGHT_LIVE=1  : 헤드풀 브라우저 (디버깅용)
  PLAYWRIGHT_LIVE=0  : 헤드리스 (기본, 운영)

개선 사항 (v2):
  - cl_antibot: UA 회전
  - cl_backoff_retry: 비동기 지수 백오프 재시도
"""
from __future__ import annotations

import asyncio
import os
import re
from decimal import Decimal
from typing import Any

import httpx
from bs4 import BeautifulSoup

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


_BASE_URL   = "https://www.cruzverde.cl"
_SEARCH_URL = f"{_BASE_URL}/buscar?query="

# Cruz Verde는 Incapsula WAF로 헤드리스 브라우저 차단 → ScraperAPI 필요
# SCRAPERAPI_KEY 환경변수 설정 시 ScraperAPI 칠레 IP + JS 렌더링 사용


def _scraperapi_url(target: str) -> str | None:
    """ScraperAPI URL 반환. 키 없으면 None."""
    key = os.environ.get("SCRAPERAPI_KEY", "").strip()
    if not key:
        return None
    return (
        f"http://api.scraperapi.com?api_key={key}"
        f"&url={target}&country_code=cl&render=true&wait=4000"
    )


def _make_headers() -> dict[str, str]:
    return {
        "User-Agent":      pick_ua(),
        "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
        "Referer":         _BASE_URL,
    }


def _parse_clp(text: str) -> Decimal | None:
    """'$12.345' 또는 '12345' → Decimal 변환."""
    cleaned = re.sub(r"[^\d]", "", text or "")
    return Decimal(cleaned) if cleaned else None


async def search_product(
    inn_name: str,
    timeout: float = 20.0,
) -> list[dict[str, Any]]:
    """INN 성분명으로 Cruz Verde 검색 → 가격 목록 반환.

    우선순위:
      1. ScraperAPI (SCRAPERAPI_KEY 설정 시) — Incapsula 우회 + JS 렌더링
      2. Playwright + Stealth (설치 시) — 헤드리스, Incapsula에 막힐 수 있음
      3. httpx 정적 폴백 — SPA라 결과 없을 가능성 높음
    """
    target_url = _SEARCH_URL + inn_name.replace(" ", "+")

    # 1. ScraperAPI 경로 (가장 신뢰도 높음)
    proxy = _scraperapi_url(target_url)
    if proxy:
        return await _search_via_scraperapi(proxy, inn_name, timeout)

    # 2. Playwright + Stealth
    try:
        import playwright.async_api  # noqa: F401
        return await _search_playwright(inn_name, timeout)
    except ImportError:
        pass

    # 3. httpx 폴백
    return await _search_httpx(inn_name, timeout)


async def _search_via_scraperapi(proxy_url: str, inn_name: str, timeout: float) -> list[dict[str, Any]]:
    """ScraperAPI를 통해 Cruz Verde 검색 결과 파싱."""
    try:
        async with httpx.AsyncClient(timeout=timeout + 30, follow_redirects=True) as client:
            resp = await client.get(proxy_url)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            items = _parse_product_cards(soup, inn_name)
            return items or [{"error": "ScraperAPI: 결과 없음", "source": "cruzverde", "inn_name": inn_name}]
    except Exception as exc:
        return [{"error": f"ScraperAPI 오류: {exc}", "source": "cruzverde", "inn_name": inn_name}]


@async_with_backoff(max_attempts=3, base=3.0, max_wait=30.0)
async def _fetch_cruzverde(url: str, headers: dict, timeout: float) -> httpx.Response:
    """백오프 재시도 포함 단일 GET."""
    async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp


async def _search_httpx(inn_name: str, timeout: float) -> list[dict[str, Any]]:
    """정적 HTML 폴백 — Playwright 미사용 시."""
    url     = _SEARCH_URL + inn_name.replace(" ", "+")
    headers = _make_headers()
    try:
        resp = await _fetch_cruzverde(url, headers, timeout)
        soup = BeautifulSoup(resp.text, "html.parser")
        return _parse_product_cards(soup, inn_name)
    except RetryExhausted as exc:
        return [{"error": f"재시도 초과: {exc}", "source": "cruzverde", "inn_name": inn_name}]
    except Exception as exc:
        return [{"error": str(exc), "source": "cruzverde", "inn_name": inn_name}]


async def _search_playwright(inn_name: str, timeout: float) -> list[dict[str, Any]]:
    """Playwright + Stealth SPA 렌더링 후 파싱.

    playwright-stealth: Incapsula/Cloudflare 헤드리스 탐지 우회.
    """
    try:
        from playwright.async_api import async_playwright  # type: ignore[import]
    except ImportError:
        return await _search_httpx(inn_name, timeout)

    url = _SEARCH_URL + inn_name.replace(" ", "+")
    headers = _make_headers()

    # playwright-stealth 선택적 import (Stealth 클래스 방식)
    _stealth_obj = None
    try:
        from playwright_stealth.stealth import Stealth  # type: ignore[import]
        _stealth_obj = Stealth()
    except Exception:
        pass

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = await browser.new_context(
            user_agent=headers["User-Agent"],
            locale="es-CL",
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()
        if _stealth_obj:
            await _stealth_obj.apply_stealth_async(page)
        try:
            await page.goto(url, timeout=int(timeout * 1000), wait_until="networkidle")
            await page.wait_for_timeout(4000)  # Angular 렌더링 대기

            body_text = await page.evaluate("document.body.innerText || ''")
            if len(body_text) < 50:
                # 한 번 더 대기
                await page.wait_for_timeout(5000)
                body_text = await page.evaluate("document.body.innerText || ''")

            html  = await page.content()
            soup  = BeautifulSoup(html, "html.parser")
            items = _parse_product_cards(soup, inn_name)
            return items
        except Exception as exc:
            return [{"error": str(exc), "source": "cruzverde", "inn_name": inn_name}]
        finally:
            await context.close()
            await browser.close()


def _parse_product_cards(soup: BeautifulSoup, inn_name: str) -> list[dict[str, Any]]:
    """BeautifulSoup에서 상품 카드 파싱."""
    items: list[dict[str, Any]] = []

    cards = soup.select(".product-card, article[class*='product'], .product-item")
    if not cards:
        cards = soup.select("[class*='product']")

    for card in cards[:10]:
        name_el  = card.select_one("[class*='name'], [class*='title'], h2, h3")
        price_el = card.select_one("[class*='price'], [class*='precio']")
        if not price_el:
            continue

        brand     = name_el.get_text(strip=True) if name_el else ""
        price_clp = _parse_clp(price_el.get_text(strip=True))
        if price_clp is None:
            continue

        link_el  = card.select_one("a[href]")
        href     = link_el.get("href", "") if link_el else ""
        full_url = href if href.startswith("http") else (_BASE_URL + href if href else _BASE_URL)

        items.append({
            "source":        "cruzverde",
            "inn_name":      inn_name,
            "brand_name":    brand,
            "raw_price_clp": float(price_clp),
            "source_url":    full_url,
            "raw_text":      card.get_text(" ", strip=True)[:200],
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
