"""ARCE/SICE 우루과이 공공조달 낙찰가 크롤러 — Playwright 기반.

타깃: https://comprasestatales.gub.uy/consultas/buscar
필터 조건:
  - Tipo de publicación: Adjudicaciones (낙찰 완료 건)
  - Organismo contratante: 29 = ASSE (보건서비스관리국) 또는 12 = MSP
  - Tipo de resolución: Adjudicada totalmente (전체 낙찰)
  - Catálogo: Familia 2 (Materiales y Suministros → 의약품)

환경변수 PLAYWRIGHT_LIVE=1 시 헤드풀 실행.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlencode

from utils.uy_parser import ParsedDrug, parse_drug_text

BASE_URL = "https://comprasestatales.gub.uy"
SEARCH_PATH = "/consultas/buscar"
RATE_LIMIT_DELAY = 2.0

LIVE = os.environ.get("PLAYWRIGHT_LIVE", "0") == "1"

# 조달 필터 파라미터 — URL 쿼리 인코딩용
DEFAULT_FILTERS: dict[str, str] = {
    "tipo_publicacion": "Adjudicaciones",
    "tipo_resolucion": "Adjudicada totalmente",
}

# 타깃 기관 코드
ORGANISMO_ASSE = "29"
ORGANISMO_MSP = "12"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-UY,es;q=0.9",
}


@dataclass
class SiceAward:
    organismo: str
    numero_licitacion: str
    descripcion: str
    proveedor: str
    monto_uyu: str
    fecha: str
    url: str
    raw_text: str
    parsed_drug: ParsedDrug | None = field(default=None, repr=False)


def _parse_sice_table(html: str, page_url: str) -> list[SiceAward]:
    from bs4 import BeautifulSoup  # type: ignore[import]

    soup = BeautifulSoup(html, "html.parser")
    awards: list[SiceAward] = []

    table = soup.select_one("table, .results-table, [class*='table']")
    if not table:
        return awards

    rows = table.select("tr")[1:]
    for row in rows:
        cells = [td.get_text(strip=True) for td in row.select("td")]
        if len(cells) < 4:
            continue

        link_el = row.select_one("a[href]")
        detail_url = (
            f"{BASE_URL}{link_el['href']}"
            if link_el and str(link_el["href"]).startswith("/")
            else page_url
        )

        organismo = cells[0] if len(cells) > 0 else ""
        numero = cells[1] if len(cells) > 1 else ""
        descripcion = cells[2] if len(cells) > 2 else ""
        proveedor = cells[3] if len(cells) > 3 else ""
        monto = cells[4] if len(cells) > 4 else ""
        fecha = cells[5] if len(cells) > 5 else ""

        raw_text = f"{descripcion} {monto} UYU {proveedor}"
        awards.append(
            SiceAward(
                organismo=organismo,
                numero_licitacion=numero,
                descripcion=descripcion,
                proveedor=proveedor,
                monto_uyu=re.sub(r"[^\d.,]", "", monto).replace(",", "."),
                fecha=fecha,
                url=detail_url,
                raw_text=raw_text,
            )
        )
    return awards


def _filter_by_keyword(awards: list[SiceAward], keyword: str) -> list[SiceAward]:
    kw = keyword.lower()
    return [
        a for a in awards
        if kw in a.descripcion.lower() or kw in a.raw_text.lower()
    ]


async def _fetch_with_playwright(url: str, fill_filters: bool = True) -> str:
    from playwright.async_api import async_playwright  # type: ignore[import]

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not LIVE)
        page = await browser.new_page(extra_http_headers=_HEADERS)
        await page.goto(f"{BASE_URL}{SEARCH_PATH}", wait_until="networkidle", timeout=30_000)

        if fill_filters:
            # Tipo de publicación → Adjudicaciones
            try:
                pub_sel = await page.query_selector(
                    "select[name*='tipo_publicacion'], select[id*='tipoPub'], select[name*='TipoPublicacion']"
                )
                if pub_sel:
                    await pub_sel.select_option(label="Adjudicaciones")
            except Exception:
                pass

            # Organismo → ASSE (29)
            try:
                org_sel = await page.query_selector(
                    "select[name*='organismo'], select[id*='organismo']"
                )
                if org_sel:
                    await org_sel.select_option(value=ORGANISMO_ASSE)
            except Exception:
                pass

            # Buscar 버튼
            try:
                btn = await page.query_selector(
                    "button[type='submit'], input[type='submit'], button:has-text('Buscar')"
                )
                if btn:
                    await btn.click()
                    await page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass

        await page.wait_for_timeout(2000)
        html = await page.content()
        await browser.close()
        return html


async def _fetch_html_fallback(keyword: str) -> str:
    import httpx

    params = {
        **DEFAULT_FILTERS,
        "organismo": ORGANISMO_ASSE,
        "descripcion": keyword,
    }
    url = f"{BASE_URL}{SEARCH_PATH}?{urlencode(params)}"
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(url, headers=_HEADERS, follow_redirects=True)
        resp.raise_for_status()
        return resp.text


async def crawl_sice(
    inn_name: str,
    max_pages: int = 3,
) -> list[SiceAward]:
    all_awards: list[SiceAward] = []

    try:
        html = await _fetch_with_playwright(f"{BASE_URL}{SEARCH_PATH}", fill_filters=True)
    except Exception:
        try:
            html = await _fetch_html_fallback(inn_name)
        except Exception:
            return []

    awards = _parse_sice_table(html, f"{BASE_URL}{SEARCH_PATH}")
    filtered = _filter_by_keyword(awards, inn_name)
    all_awards.extend(filtered)

    for award in all_awards:
        combined_text = f"{award.descripcion} {award.monto_uyu} UYU {award.proveedor}"
        drug = await parse_drug_text(
            raw_text=combined_text,
            source_site="sice",
            source_url=award.url,
        )
        if drug:
            drug.extra["organismo"] = award.organismo
            drug.extra["fecha"] = award.fecha
            drug.extra["numero_licitacion"] = award.numero_licitacion
        award.parsed_drug = drug
        await asyncio.sleep(RATE_LIMIT_DELAY)

    return all_awards


async def crawl_sice_to_parsed(inn_name: str) -> list[ParsedDrug | None]:
    awards = await crawl_sice(inn_name)
    return [a.parsed_drug for a in awards]
