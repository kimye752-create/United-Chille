"""칠레 거시지표 — Supabase cl_health_expenditure / cl_world_population 기반.

Supabase에 데이터가 없으면 아래 정적 값으로 폴백.

출처:
  - 국가 GDP:          World Bank 2024  ($330.3B)
  - 인구:              INE Chile 2024 인구주택총조사 (18,480,432명)
  - 의약품 시장 규모:  IQVIA / pharmatradz 2024  ($2.45B)
  - 의약품 수입 의존도: CEPAL / Salud y Fármacos 2024  (80.4% — 2018년 64.1% → 2024년 80.4%)
"""
from __future__ import annotations

from typing import Any

# ── 정적 폴백 (Supabase 미연결 또는 조회 실패 시) ─────────────────────────────
_STATIC: dict[str, Any] = {
    "gdp_usd_b":           330.3,      # 총 GDP USD billion
    "gdp_per_capita_usd":  17_220,     # 1인당 GDP USD (World Bank 2024)
    "population":          18_480_432, # 명 (2024 센서스 실측치)
    "pharma_market_usd_b":  2.45,      # USD 십억 (billion)
    "pharma_import_pct":   80.4,       # % — 수입 의존도
    "source": {
        "gdp":          "World Bank 2024",
        "population":   "INE Chile",
        "pharma_market":"IQVIA 2024",
        "pharma_import":"CEPAL 2024",
    },
}

# 하위 호환 — legacy list 형태 (일부 코드에서 직접 참조)
_STATIC_MACRO: list[dict] = [
    {"label": "1인당 GDP",          "value": "US$ 17,220", "sub": "2024  ·  World Bank"},
    {"label": "인구",              "value": "1,848만 명",  "sub": "2024  ·  INE Chile 센서스"},
    {"label": "의약품 시장 규모",  "value": "USD 24.5억",  "sub": "2024  ·  IQVIA  ·  CAGR +6.2%"},
    {"label": "의약품 수입 의존도","value": "80.4%",        "sub": "2024  ·  CEPAL / Salud y Fármacos"},
]

_cache: dict[str, Any] | None = None


def get_ch_macro() -> dict[str, Any]:
    """칠레 거시지표 dict 반환. Supabase 조회 실패 시 정적 폴백."""
    global _cache
    if _cache is not None:
        return _cache

    try:
        from utils.db import get_client
        sb = get_client()
        pop_row = (
            sb.table("sg_world_population")
            .select("population,year")
            .eq("country_code", "CHL")
            .order("year", desc=True)
            .limit(1)
            .execute()
            .data
        )

        result: dict[str, Any] = dict(_STATIC)
        if pop_row:
            p = pop_row[0]
            result["population"] = p["population"]
            result["source"] = dict(result["source"])
            result["source"]["population"] = f"World Bank {p['year']}"

        _cache = result
        return result
    except Exception:
        return _STATIC


# 하위 호환
CH_MACRO = _STATIC_MACRO
