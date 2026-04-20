"""HSA 등재 의약품 조회 — Supabase 버전.

products 테이블 (country='SG', source_name='SG:hsa_registry')에서 읽어옴.
"""
from __future__ import annotations

from typing import Any

_cache: list[dict[str, Any]] | None = None


def load_registry() -> dict[str, dict[str, Any]]:
    """licence_no(registration_number) → row 매핑 반환."""
    global _cache
    if _cache is None:
        from utils.db import get_client
        sb = get_client()
        try:
            rows = (
                sb.table("products")
                .select(
                    "registration_number,trade_name,active_ingredient,"
                    "strength,dosage_form,country_specific"
                )
                .eq("country", "SG")
                .eq("source_name", "SG:hsa_registry")
                .execute()
                .data or []
            )
            _cache = rows
        except Exception:
            _cache = []

    return {
        r["registration_number"]: r
        for r in _cache
        if r.get("registration_number")
    }


def row_to_item(row: dict[str, Any]) -> dict[str, Any]:
    """map_to_schema 입력용 dict."""
    cs = row.get("country_specific") or {}
    return {
        "reg_no": (row.get("registration_number") or "").strip(),
        "product_name": (row.get("trade_name") or "").strip(),
        "trade_name": (row.get("trade_name") or "").strip(),
        "active_ingredient": (row.get("active_ingredient") or ""),
        "strength": (row.get("strength") or "").strip(),
        "dosage_form": (row.get("dosage_form") or "").strip().lower(),
        "atc_code": cs.get("atc_code", ""),
        "segment": "retail",
    }
