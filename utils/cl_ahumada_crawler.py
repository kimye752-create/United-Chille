"""Farmacias Ahumada(FASA) 칠레 소매 약가 크롤러.

FASA는 Cloudflare 보호 및 CAPTCHA가 적용될 가능성이 높습니다.
환경변수 SCRAPERAPI_KEY가 설정되어 있으면 ScraperAPI 프록시를 사용하고,
없으면 직접 요청(Cloudflare 탐지 시 명확한 오류 메시지 반환)합니다.

개선 사항 (v2):
  - cl_antibot: Cloudflare / IP 차단 탐지 + UA 회전
  - cl_backoff_retry: 비동기 지수 백오프 재시도 (Cloudflare 감지 시 3배 지연)
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
    from utils.cl_antibot import pick_ua, detect as detect_antibot, get_countermeasure, AntiBotType
    from utils.cl_backoff_retry import async_with_backoff, RetryExhausted
except ImportError:
    def pick_ua() -> str:  # type: ignore[misc]
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    def detect_antibot(status, body="", headers=None):  # type: ignore[misc]
        return None
    def get_countermeasure(ab_type):  # type: ignore[misc]
        class _CM:
            should_circuit_break = False
        return _CM()
    def async_with_backoff(**kw):  # type: ignore[misc]
        def deco(f): return f
        return deco
    class RetryExhausted(Exception): pass  # type: ignore[misc]
    class AntiBotType:  # type: ignore[misc]
        CLOUDFLARE = "cloudflare"
        NONE = "none"


_BASE_URL   = "https://www.farmaciasahumada.cl"
# SFCC(Salesforce Commerce Cloud) 올바른 Site/Locale 경로 (Sites-ahumada-cl-Site/default)
_SEARCH_URL = f"{_BASE_URL}/on/demandware.store/Sites-ahumada-cl-Site/default/Search-Show"


def _make_headers() -> dict[str, str]:
    return {
        "User-Agent":      pick_ua(),
        "Accept-Language": "es-CL,es;q=0.9",
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer":         _BASE_URL,
    }


def _parse_clp(text: str) -> Decimal | None:
    cleaned = re.sub(r"[^\d]", "", text or "")
    return Decimal(cleaned) if cleaned else None


def _build_url(inn_name: str) -> str:
    """ScraperAPI 프록시 URL 또는 직접 URL 반환."""
    scraperapi_key = os.environ.get("SCRAPERAPI_KEY", "").strip()
    target = f"{_SEARCH_URL}?q={inn_name.replace(' ', '+')}"
    if scraperapi_key:
        return (
            f"http://api.scraperapi.com?api_key={scraperapi_key}"
            f"&url={target}&country_code=cl&render=true"
        )
    return target


@async_with_backoff(max_attempts=3, base=4.0, max_wait=45.0)
async def _fetch_ahumada(url: str, headers: dict, timeout: float) -> httpx.Response:
    """백오프 재시도 포함 GET. Cloudflare 탐지 시 더 긴 지연(antibot 배수 적용)."""
    async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp


async def search_product(
    inn_name: str,
    timeout: float = 20.0,
) -> list[dict[str, Any]]:
    """INN 성분명으로 FASA 검색 → 가격 목록 반환."""
    url     = _build_url(inn_name)
    headers = _make_headers()

    try:
        resp = await _fetch_ahumada(url, headers, timeout)

        # 200이지만 Cloudflare challenge 페이지 여부 확인
        ab_type = detect_antibot(resp.status_code, resp.text[:2000], dict(resp.headers))
        if hasattr(ab_type, 'value'):
            ab_val = ab_type.value
        else:
            ab_val = str(ab_type)

        if ab_val in ("cloudflare", "recaptcha", "ip_block"):
            scraperapi_key = os.environ.get("SCRAPERAPI_KEY", "").strip()
            hint = "SCRAPERAPI_KEY 설정 권장" if not scraperapi_key else "ScraperAPI 연결 확인 요망"
            return [{
                "error": f"Cloudflare/Anti-bot 탐지 ({ab_val}). {hint}",
                "source":   "ahumada",
                "inn_name": inn_name,
                "antibot":  ab_val,
            }]

        soup = BeautifulSoup(resp.text, "html.parser")
        return _parse_product_cards(soup, inn_name)

    except RetryExhausted as exc:
        return [{
            "error":    f"재시도 초과 (FASA Cloudflare 차단 의심): {exc}",
            "source":   "ahumada",
            "inn_name": inn_name,
        }]
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        body   = ""
        try:
            body = exc.response.text[:2000]
        except Exception:
            pass
        ab_type  = detect_antibot(status, body, dict(exc.response.headers))
        ab_val   = ab_type.value if hasattr(ab_type, 'value') else str(ab_type)
        return [{
            "error":    f"HTTP {status} ({ab_val}) — Cloudflare/ScraperAPI 확인 요망",
            "source":   "ahumada",
            "inn_name": inn_name,
            "antibot":  ab_val,
        }]
    except Exception as exc:
        return [{"error": str(exc), "source": "ahumada", "inn_name": inn_name}]


def _parse_product_cards(soup: BeautifulSoup, inn_name: str) -> list[dict[str, Any]]:
    """FASA SFCC(Demandware) 구조 파싱.

    SFCC는 가격 정보를 세 군데에 저장합니다:
      1. <script> 태그 내 JSON 데이터 레이어 (window.dataLayer, productData 등)
      2. 상품 카드 data-price / data-gtm-price 속성
      3. .price-sales / .price-standard span 텍스트

    세 경로를 모두 시도하여 최대한 수집합니다.
    """
    import json

    items: list[dict[str, Any]] = []
    seen: set[str] = set()  # 중복 제거

    # ── 경로 1: <script> 태그 내 SFCC JSON 데이터 레이어 ─────────────────
    _price_patterns = [
        re.compile(r'"salesPrice"\s*:\s*\{[^}]*"value"\s*:\s*(\d[\d.,]*)'),
        re.compile(r'"price"\s*:\s*\{[^}]*"sales"\s*:\s*\{[^}]*"value"\s*:\s*(\d[\d.,]*)'),
        re.compile(r'"listPrice"\s*:\s*(\d{4,8})'),
        re.compile(r'"salePrice"\s*:\s*(\d{4,8})'),
        re.compile(r'"price"\s*:\s*(\d{4,8})'),
        re.compile(r'"pricing"\s*:\s*\{\s*"standard"\s*:\s*(\d{4,8})'),
    ]
    _name_patterns = [
        re.compile(r'"productName"\s*:\s*"([^"]{3,80})"'),
        re.compile(r'"name"\s*:\s*"([^"]{3,80})"'),
    ]
    _url_patterns = [
        re.compile(r'"productUrl"\s*:\s*"([^"]{5,200})"'),
        re.compile(r'"url"\s*:\s*"(/[^"]{5,200})"'),
    ]

    for script_tag in soup.select("script"):
        raw = script_tag.string or ""
        if not raw or len(raw) < 100:
            continue
        # 상품 JSON 블록인지 빠르게 확인
        if "price" not in raw.lower() and "producto" not in raw.lower():
            continue

        # JSON 배열/객체 블록 추출 시도
        try:
            # window.dataLayer = [...]; 또는 유사 패턴
            m = re.search(r'(?:dataLayer|productData|products)\s*=\s*(\[.*?\]);', raw, re.S)
            if m:
                data = json.loads(m.group(1))
                if not isinstance(data, list):
                    data = [data]
                for item in data[:20]:
                    if not isinstance(item, dict):
                        continue
                    # 중첩 순회
                    flat = {}
                    def _flatten(d: dict, prefix: str = "") -> None:
                        for k, v in d.items():
                            if isinstance(v, dict):
                                _flatten(v, k)
                            else:
                                flat[k] = v
                    _flatten(item)
                    price = _parse_clp(str(flat.get("price") or flat.get("salePrice") or flat.get("listPrice") or ""))
                    name  = str(flat.get("name") or flat.get("productName") or "")
                    url   = str(flat.get("url") or flat.get("productUrl") or "")
                    if price and name and name not in seen:
                        seen.add(name)
                        items.append({
                            "source":        "ahumada",
                            "inn_name":      inn_name,
                            "brand_name":    name[:100],
                            "raw_price_clp": float(price),
                            "source_url":    url if url.startswith("http") else (_BASE_URL + url if url else _BASE_URL),
                            "raw_text":      f"{name} | {price}",
                        })
        except Exception:
            pass

        # 정규식 패턴으로 가격 추출 (JSON 파싱 실패 시 보완)
        for pat in _price_patterns:
            for m in pat.finditer(raw):
                price_str = m.group(1).replace(".", "").replace(",", "")
                price = _parse_clp(price_str)
                if price and 1000 <= int(price) <= 99_999_999:
                    # 인근에서 이름 찾기
                    start = max(0, m.start() - 500)
                    end   = min(len(raw), m.end() + 500)
                    chunk = raw[start:end]
                    name_m = _name_patterns[0].search(chunk) or _name_patterns[1].search(chunk)
                    url_m  = _url_patterns[0].search(chunk) or _url_patterns[1].search(chunk)
                    name   = name_m.group(1) if name_m else inn_name
                    url_s  = url_m.group(1) if url_m else ""
                    if name not in seen:
                        seen.add(name)
                        items.append({
                            "source":        "ahumada",
                            "inn_name":      inn_name,
                            "brand_name":    name[:100],
                            "raw_price_clp": float(price),
                            "source_url":    url_s if url_s.startswith("http") else (_BASE_URL + url_s if url_s else _BASE_URL),
                            "raw_text":      chunk[:200],
                        })
                    if len(items) >= 10:
                        break
            if len(items) >= 10:
                break

    if len(items) >= 10:
        return items[:10]

    # ── 경로 2: data-price / data-gtm-price 속성 ─────────────────────────
    for el in soup.select("[data-price],[data-gtm-price],[data-sale-price]")[:10]:
        raw_price = el.get("data-sale-price") or el.get("data-price") or el.get("data-gtm-price") or ""
        price_clp = _parse_clp(raw_price)
        if not price_clp:
            continue
        name_el = el.select_one("[class*='name'],[class*='title'],h2,h3") or el
        name = name_el.get_text(strip=True)[:100]
        link_el = el.select_one("a[href]") or el.find_parent("a")
        href = (link_el.get("href","") if link_el else "")
        full_url = href if href.startswith("http") else (_BASE_URL + href if href else _BASE_URL)
        if name not in seen:
            seen.add(name)
            items.append({
                "source":        "ahumada",
                "inn_name":      inn_name,
                "brand_name":    name,
                "raw_price_clp": float(price_clp),
                "source_url":    full_url,
                "raw_text":      el.get_text(" ", strip=True)[:200],
            })

    if len(items) >= 1:
        return items[:10]

    # ── 경로 3: CSS 클래스 기반 상품 카드 (기존 방식 + 개선) ──────────────
    selectors = [
        ".product-tile", ".product-info", "[class*='product-card']",
        "[class*='product-tile']", "[class*='product-item']",
        "article.product", "li.product",
    ]
    for sel in selectors:
        cards = soup.select(sel)
        if cards:
            for card in cards[:10]:
                # SFCC 가격 클래스들
                price_el = card.select_one(
                    ".price-sales, .price .value, [class*='sales-price'], "
                    "[class*='sale-price'], [class*='precio-venta']"
                )
                if not price_el:
                    # 숫자가 4자리 이상인 span/div 찾기 (CLP는 최소 수천 단위)
                    for candidate in card.select("span,div"):
                        txt = candidate.get_text(strip=True)
                        if re.match(r'^\$?\s*[\d.]{4,}$', txt):
                            price_el = candidate
                            break
                if not price_el:
                    continue
                price_clp = _parse_clp(price_el.get_text(strip=True))
                if price_clp is None:
                    continue
                name_el  = card.select_one(".product-name, .pdp-link a, [class*='name']")
                link_el  = card.select_one("a[href]")
                href     = link_el.get("href", "") if link_el else ""
                full_url = href if href.startswith("http") else (_BASE_URL + href if href else _BASE_URL)
                name = name_el.get_text(strip=True)[:100] if name_el else inn_name
                if name not in seen:
                    seen.add(name)
                    items.append({
                        "source":        "ahumada",
                        "inn_name":      inn_name,
                        "brand_name":    name,
                        "raw_price_clp": float(price_clp),
                        "source_url":    full_url,
                        "raw_text":      card.get_text(" ", strip=True)[:200],
                    })
            if items:
                break

    return items[:10]


async def crawl(inn_names: list[str]) -> list[dict[str, Any]]:
    """여러 INN 성분을 순차 검색 (지연 시간 증가 — FASA 차단 방지)."""
    all_results: list[dict[str, Any]] = []
    for inn in inn_names:
        rows = await search_product(inn)
        all_results.extend(rows)
        await asyncio.sleep(3.0)  # FASA는 더 긴 지연 (Cloudflare 감지 완화)
    return all_results
