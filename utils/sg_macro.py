"""싱가포르 거시지표 — Supabase sg_health_expenditure / sg_world_population 기반.

Supabase에 데이터가 없으면 KOTRA 2026 PDF 기준 정적 값으로 폴백.
"""
from __future__ import annotations

from typing import Any

# 폴백용 정적 값 (Supabase 이관 전 또는 조회 실패 시)
_STATIC_MACRO: list[dict] = [
    {"label": "1인당 GDP",   "value": "$90,674",    "sub": "2024  ·  IMF / Singstat"},
    {"label": "인구",        "value": "604만 명",    "sub": "2024  ·  Singstat"},
    {"label": "의약품 투자",  "value": "S$22.15억",  "sub": "2024  ·  EDB  ·  전년比 +146%"},
    {"label": "실질 성장률",  "value": "4.4%",       "sub": "2024  ·  MTI"},
]

_cache: list[dict] | None = None


def get_sg_macro() -> list[dict[str, Any]]:
    """Supabase에서 싱가포르 거시지표 조회. 실패 시 정적 폴백."""
    global _cache
    if _cache is not None:
        return _cache

    try:
        from utils.db import get_client
        sb = get_client()
        pop_row = (
            sb.table("sg_world_population")
            .select("population,year")
            .eq("country_code", "SGP")
            .order("year", desc=True)
            .limit(1)
            .execute()
            .data
        )
        exp_row = (
            sb.table("sg_health_expenditure")
            .select("value,year,series")
            .eq("country_or_area", "Singapore")
            .ilike("series", "%per capita%")
            .order("year", desc=True)
            .limit(1)
            .execute()
            .data
        )

        result = list(_STATIC_MACRO)  # 기본값 복사
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


# 하위 호환 — server.py에서 `from utils.sg_macro import SG_MACRO` 사용
SG_MACRO = _STATIC_MACRO
