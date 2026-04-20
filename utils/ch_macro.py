"""칠레 거시지표 — Supabase cl_health_expenditure / cl_world_population 기반.

Supabase에 데이터가 없으면 IMF/World Bank 2024 기준 정적 값으로 폴백.
"""
from __future__ import annotations

from typing import Any

# 폴백용 정적 값 (Supabase 이관 전 또는 조회 실패 시)
# 출처: IMF WEO 2024, World Bank, IQVIA Chile Pharma Report 2024
_STATIC_MACRO: list[dict] = [
    {"label": "1인당 GDP",       "value": "$17,093",    "sub": "2024  ·  IMF WEO"},
    {"label": "인구",             "value": "1,982만 명", "sub": "2024  ·  INE Chile"},
    {"label": "의약품 시장 규모", "value": "USD 31억",   "sub": "2024  ·  IQVIA  ·  전년比 +6.2%"},
    {"label": "실질 성장률",      "value": "2.5%",       "sub": "2024  ·  Banco Central de Chile"},
]

_cache: list[dict] | None = None


def get_ch_macro() -> list[dict[str, Any]]:
    """Supabase에서 칠레 거시지표 조회. 실패 시 정적 폴백."""
    global _cache
    if _cache is not None:
        return _cache

    try:
        from utils.db import get_client
        sb = get_client()
        pop_row = (
            sb.table("sg_world_population")   # 공통 인구 테이블 재사용
            .select("population,year")
            .eq("country_code", "CHL")
            .order("year", desc=True)
            .limit(1)
            .execute()
            .data
        )
        exp_row = (
            sb.table("sg_health_expenditure")  # 공통 보건지출 테이블 재사용
            .select("value,year,series")
            .eq("country_or_area", "Chile")
            .ilike("series", "%per capita%")
            .order("year", desc=True)
            .limit(1)
            .execute()
            .data
        )

        result = list(_STATIC_MACRO)
        if pop_row:
            p = pop_row[0]
            result[1] = {"label": "인구", "value": f"{p['population']:,}명", "sub": f"{p['year']}  ·  World Bank"}
        if exp_row:
            e = exp_row[0]
            result[0] = {"label": "보건 지출/인구", "value": f"${e['value']:,.0f}", "sub": f"{e['year']}  ·  UN SYB67"}

        _cache = result
        return result
    except Exception:
        return _STATIC_MACRO


# 하위 호환 — server.py에서 `from utils.ch_macro import CH_MACRO` 사용
CH_MACRO = _STATIC_MACRO
