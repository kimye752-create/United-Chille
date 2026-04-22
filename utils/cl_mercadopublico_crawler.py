"""Mercado Público 칠레 공공조달 크롤러 (OCDS Open Data API 연동).

OCDS(Open Contracting Data Standard) 포맷을 지원하므로
브라우저 자동화 없이 REST API로 직접 데이터를 수집합니다.

API 문서: https://api.mercadopublico.cl/
주요 엔드포인트:
  GET /servicios/v1/publico/licitaciones.json  — 입찰 목록
  GET /servicios/v1/publico/licitaciones/{codigo}  — 입찰 상세

개선 사항 (v2):
  - cl_backoff_retry: 비동기 지수 백오프 재시도
"""
from __future__ import annotations

import asyncio
import os
import re
from decimal import Decimal
from typing import Any

import httpx

try:
    from utils.cl_backoff_retry import async_with_backoff, RetryExhausted
except ImportError:
    def async_with_backoff(**kw):  # type: ignore[misc]
        def deco(f): return f
        return deco
    class RetryExhausted(Exception): pass  # type: ignore[misc]


_API_BASE          = "https://api.mercadopublico.cl/servicios/v1/publico"
_LICITACIONES_URL  = f"{_API_BASE}/licitaciones.json"

_HEADERS = {
    "User-Agent": "UPharmaExportAI/2.0 (research; contact: kup@example.com)",
    "Accept":     "application/json",
}

# 의약품 HS 코드 관련 키워드
_PHARMA_KEYWORDS = [
    "medicamento", "fármaco", "farmaco", "comprimido",
    "cápsula", "capsula", "inyectable", "jarabe", "tableta",
]


def _get_ticket_key() -> str:
    """환경변수 MERCADOPUBLICO_API_KEY (미설정 시 공개 엔드포인트 사용)."""
    return os.environ.get("MERCADOPUBLICO_API_KEY", "").strip()


@async_with_backoff(max_attempts=3, base=2.0, max_wait=30.0)
async def _fetch_licitaciones(params: dict, timeout: float) -> httpx.Response:
    """백오프 재시도 포함 공공 API GET."""
    async with httpx.AsyncClient(headers=_HEADERS, timeout=timeout) as client:
        resp = await client.get(_LICITACIONES_URL, params=params)
        resp.raise_for_status()
        return resp


async def search_tenders(
    inn_name: str,
    limit: int = 20,
    timeout: float = 20.0,
) -> list[dict[str, Any]]:
    """INN 성분명으로 공공조달 입찰 검색."""
    params: dict[str, Any] = {
        "nombre":   inn_name,
        "estado":   "adjudicada",   # 낙찰 완료된 건만
        "cantidad": limit,
        "pagina":   1,
    }
    ticket = _get_ticket_key()
    if ticket:
        params["ticket"] = ticket

    try:
        resp = await _fetch_licitaciones(params, timeout)
        data = resp.json()
        return _parse_licitaciones(data, inn_name)
    except RetryExhausted as exc:
        return [{"error": f"재시도 초과: {exc}", "source": "mercadopublico", "inn_name": inn_name}]
    except Exception as exc:
        return [{"error": str(exc), "source": "mercadopublico", "inn_name": inn_name}]


def _parse_clp(val: Any) -> Decimal | None:
    if val is None:
        return None
    cleaned = re.sub(r"[^\d.]", "", str(val))
    try:
        return Decimal(cleaned) if cleaned else None
    except Exception:
        return None


def _parse_licitaciones(data: Any, inn_name: str) -> list[dict[str, Any]]:
    """OCDS 표준 응답 파싱."""
    items: list[dict[str, Any]] = []
    licitaciones = (
        data.get("Listado")
        or data.get("data")
        or data.get("licitaciones")
        or (data if isinstance(data, list) else [])
    )
    for lic in licitaciones[:20]:
        if not isinstance(lic, dict):
            continue
        nombre = lic.get("Nombre") or lic.get("nombre") or ""
        codigo = lic.get("CodigoExterno") or lic.get("codigo") or ""
        org    = lic.get("NombreOrganismo") or lic.get("organismo") or ""

        # 의약품 관련 여부 확인
        nombre_lower = nombre.lower()
        is_pharma = (
            any(kw in nombre_lower for kw in _PHARMA_KEYWORDS)
            or inn_name.lower() in nombre_lower
        )
        if not is_pharma:
            continue

        monto = _parse_clp(lic.get("MontoEstimado") or lic.get("monto"))
        fecha = str(lic.get("FechaAdjudicacion") or lic.get("fecha") or "")

        items.append({
            "source":             "mercadopublico",
            "inn_name":           inn_name,
            "tender_id":          codigo,
            "buyer_org":          org,
            "tender_name":        nombre,
            "awarded_price_clp":  float(monto) if monto else None,
            "award_date":         fecha[:10] if fecha else None,
            "tender_status":      "awarded",
            "source_url": (
                f"https://www.mercadopublico.cl/Procurement/Modules/RFB/"
                f"DetailsAcquisition.aspx?idlicitacion={codigo}"
            ),
            "ocds_data": lic,
        })

    return items


async def crawl(inn_names: list[str]) -> list[dict[str, Any]]:
    """여러 INN 성분의 공공조달 낙찰 데이터 수집."""
    all_results: list[dict[str, Any]] = []
    for inn in inn_names:
        rows = await search_tenders(inn)
        all_results.extend(rows)
        await asyncio.sleep(1.0)
    return all_results
