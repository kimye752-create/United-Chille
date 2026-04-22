"""CENABAST 칠레 국가중앙공급센터 약가 크롤러.

Ley 21.198(Ley CENABAST) 기반 소매 상한가(Precio Máximo de Venta al Público)
데이터를 cenabast.cl에서 주기적으로 파싱하여 DB에 적재합니다.

수집 데이터:
  - CENABAST 공급가 (민간 약국 매입가)
  - 소비자 판매 상한가 (정부 강제 상한)

개선 사항 (v2):
  - cl_antibot: UA 회전
  - cl_backoff_retry: 비동기 지수 백오프 재시도
  - 테이블 파싱 검증 강화
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


_BASE_URL        = "https://www.cenabast.cl"
_PRICE_LIST_URL  = f"{_BASE_URL}/medicamentos/precios"         # 올바른 현재 URL
_CONTRACT_URL    = f"{_BASE_URL}/precios-vigentes-en-contratos/"
_CATALOG_URL     = f"{_BASE_URL}/catalogo"
_ISP_SEARCH_URL  = "https://registros.ispch.gob.cl/api/v1/medicamentos"  # ISP 대체 소스


def _make_headers() -> dict[str, str]:
    return {
        "User-Agent":      pick_ua(),
        "Accept-Language": "es-CL,es;q=0.9",
        "Referer":         _BASE_URL,
    }


def _parse_clp(text: str) -> Decimal | None:
    cleaned = re.sub(r"[^\d]", "", text or "")
    return Decimal(cleaned) if cleaned else None


def _build_url(target: str) -> str:
    """ScraperAPI 칠레 IP 프록시 URL 또는 직접 URL 반환.

    CENABAST는 해외 IP를 WAF로 차단 → ScraperAPI country_code=cl 필요.
    SCRAPERAPI_KEY 없으면 직접 접속 시도(국내 서버에서는 성공).
    """
    scraperapi_key = os.environ.get("SCRAPERAPI_KEY", "").strip()
    if scraperapi_key:
        return (
            f"http://api.scraperapi.com?api_key={scraperapi_key}"
            f"&url={target}&country_code=cl&render=false"
        )
    return target


@async_with_backoff(max_attempts=3, base=3.0, max_wait=40.0)
async def _fetch_cenabast(url: str, headers: dict, timeout: float) -> httpx.Response:
    """백오프 재시도 포함 GET.

    CENABAST WAF 우회:
      - ScraperAPI 있으면 칠레 IP로 프록시
      - 없으면 verify=False 직접 요청 (칠레 서버 IP에서 구동 시 성공)
    """
    proxy_url = _build_url(url)
    scraperapi_key = os.environ.get("SCRAPERAPI_KEY", "").strip()

    # ScraperAPI 경유 시 SSL 검증 정상, 직접 접속 시 verify=False
    verify = True if scraperapi_key else False
    async with httpx.AsyncClient(
        headers=headers, timeout=timeout, follow_redirects=True, verify=verify
    ) as client:
        resp = await client.get(proxy_url)
        resp.raise_for_status()
        return resp


async def fetch_max_prices(timeout: float = 25.0) -> list[dict[str, Any]]:
    """CENABAST 소매 상한가 목록 전체 파싱."""
    headers = _make_headers()
    try:
        resp = await _fetch_cenabast(_PRICE_LIST_URL, headers, timeout)
        soup = BeautifulSoup(resp.text, "html.parser")
        return _parse_price_table(soup)
    except RetryExhausted as exc:
        return [{"error": f"재시도 초과: {exc}", "source": "cenabast"}]
    except Exception as exc:
        return [{"error": str(exc), "source": "cenabast"}]


def _parse_price_table(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """CENABAST 가격표 HTML 테이블 파싱.

    v2: 헤더 분석으로 컬럼 인덱스 자동 탐지 (테이블 구조 변경 대응).
    """
    items: list[dict[str, Any]] = []

    for table in soup.select("table"):
        headers = [th.get_text(strip=True).lower() for th in table.select("th")]

        # 의약품 관련 테이블인지 확인
        if not any("precio" in h or "medicamento" in h or "inn" in h or "producto" in h
                   for h in headers):
            continue

        # 컬럼 인덱스 추정 (fallback: 0=INN, 1=공급가, 2=소매상한가)
        def _find_col(keywords: list[str]) -> int:
            for kw in keywords:
                for i, h in enumerate(headers):
                    if kw in h:
                        return i
            return -1

        idx_inn    = _find_col(["inn", "principio", "medicamento", "nombre", "producto"])
        idx_supply = _find_col(["cenabast", "suministro", "compra", "supply"])
        idx_retail = _find_col(["maximo", "max", "venta", "pvp", "precio"])

        # 헤더 미발견 시 위치 기반 기본값
        if idx_inn    < 0: idx_inn    = 0
        if idx_supply < 0: idx_supply = 1
        if idx_retail < 0: idx_retail = 2

        for row in table.select("tr"):
            cells = [td.get_text(strip=True) for td in row.select("td")]
            if len(cells) < 3:
                continue

            def _safe_cell(i: int) -> str:
                return cells[i] if 0 <= i < len(cells) else ""

            inn_name     = _safe_cell(idx_inn)
            supply_price = _parse_clp(_safe_cell(idx_supply))
            max_retail   = _parse_clp(_safe_cell(idx_retail))

            if not inn_name or (supply_price is None and max_retail is None):
                continue
            # 빈 문자열/헤더 행 건너뜀
            if inn_name.lower() in ("inn", "nombre", "medicamento", "producto", "principio"):
                continue

            items.append({
                "source":                   "cenabast",
                "inn_name":                 inn_name,
                "cenabast_supply_price_clp": float(supply_price) if supply_price else None,
                "max_retail_price_clp":      float(max_retail) if max_retail else None,
                "regulation_law":           "Ley 21.198",
                "source_url":               _PRICE_LIST_URL,
                "raw_text":                 " | ".join(cells)[:300],
            })

    return items


async def search_product(
    inn_name: str,
    timeout: float = 20.0,
) -> list[dict[str, Any]]:
    """특정 INN으로 CENABAST 카탈로그 검색."""
    url     = f"{_CATALOG_URL}?q={inn_name.replace(' ', '+')}"
    headers = _make_headers()
    try:
        resp       = await _fetch_cenabast(url, headers, timeout)
        soup       = BeautifulSoup(resp.text, "html.parser")
        all_prices = _parse_price_table(soup)
        return [
            p for p in all_prices
            if inn_name.lower() in p.get("inn_name", "").lower()
        ]
    except RetryExhausted as exc:
        return [{"error": f"재시도 초과: {exc}", "source": "cenabast", "inn_name": inn_name}]
    except Exception as exc:
        return [{"error": str(exc), "source": "cenabast", "inn_name": inn_name}]


async def crawl(inn_names: list[str]) -> list[dict[str, Any]]:
    """여러 INN 성분의 CENABAST 상한가 데이터 수집."""
    all_results: list[dict[str, Any]] = []

    # 전체 목록 1회 파싱 후 필터링 (효율적)
    full_list = await fetch_max_prices()
    if full_list and "error" not in full_list[0]:
        for inn in inn_names:
            filtered = [
                p for p in full_list
                if inn.lower() in p.get("inn_name", "").lower()
            ]
            all_results.extend(
                filtered or [{"source": "cenabast", "inn_name": inn, "found": False}]
            )
    else:
        # 전체 목록 실패 시 개별 검색
        for inn in inn_names:
            rows = await search_product(inn)
            all_results.extend(rows)
            await asyncio.sleep(1.0)

    return all_results
