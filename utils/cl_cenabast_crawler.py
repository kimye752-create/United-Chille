"""CENABAST 칠레 국가중앙공급센터 약가 크롤러.

Ley 21.198(Ley CENABAST) 기반 소매 상한가(Precio Máximo de Venta al Público)
데이터를 cenabast.cl에서 주기적으로 파싱하여 DB에 적재합니다.

수집 데이터:
  - CENABAST 공급가 (민간 약국 매입가)
  - 소비자 판매 상한가 (정부 강제 상한)
"""
from __future__ import annotations

import asyncio
import re
from decimal import Decimal
from typing import Any

import httpx
from bs4 import BeautifulSoup


_BASE_URL = "https://www.cenabast.cl"
# CENABAST 약품 목록 페이지 (실제 URL은 변경될 수 있음 — 정기 점검 필요)
_PRICE_LIST_URL = f"{_BASE_URL}/medicamentos/precios-maximos"
_CATALOG_URL = f"{_BASE_URL}/catalogo"

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


async def fetch_max_prices(
    timeout: float = 25.0,
) -> list[dict[str, Any]]:
    """CENABAST 소매 상한가 목록 전체 파싱."""
    try:
        async with httpx.AsyncClient(headers=_HEADERS, timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(_PRICE_LIST_URL)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            return _parse_price_table(soup)
    except Exception as exc:
        return [{"error": str(exc), "source": "cenabast"}]


def _parse_price_table(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """CENABAST 가격표 HTML 테이블 파싱."""
    items: list[dict[str, Any]] = []

    for table in soup.select("table"):
        headers = [th.get_text(strip=True).lower() for th in table.select("th")]
        if not any("precio" in h or "medicamento" in h or "inn" in h for h in headers):
            continue

        for row in table.select("tr"):
            cells = [td.get_text(strip=True) for td in row.select("td")]
            if len(cells) < 3:
                continue

            # 헤더 인덱스 추정 (테이블 구조에 따라 조정 필요)
            inn_name = cells[0] if cells else ""
            supply_price = _parse_clp(cells[1] if len(cells) > 1 else "")
            max_retail   = _parse_clp(cells[2] if len(cells) > 2 else "")

            if not inn_name or (supply_price is None and max_retail is None):
                continue

            items.append({
                "source": "cenabast",
                "inn_name": inn_name,
                "cenabast_supply_price_clp": float(supply_price) if supply_price else None,
                "max_retail_price_clp": float(max_retail) if max_retail else None,
                "regulation_law": "Ley 21.198",
                "source_url": _PRICE_LIST_URL,
                "raw_text": " | ".join(cells)[:300],
            })

    return items


async def search_product(
    inn_name: str,
    timeout: float = 20.0,
) -> list[dict[str, Any]]:
    """특정 INN으로 CENABAST 카탈로그 검색."""
    url = f"{_CATALOG_URL}?q={inn_name.replace(' ', '+')}"
    try:
        async with httpx.AsyncClient(headers=_HEADERS, timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            all_prices = _parse_price_table(soup)
            # INN 필터링
            return [
                p for p in all_prices
                if inn_name.lower() in p.get("inn_name", "").lower()
            ]
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
            all_results.extend(filtered or [{"source": "cenabast", "inn_name": inn, "found": False}])
    else:
        # 전체 목록 실패 시 개별 검색
        for inn in inn_names:
            rows = await search_product(inn)
            all_results.extend(rows)
            await asyncio.sleep(1.0)

    return all_results
