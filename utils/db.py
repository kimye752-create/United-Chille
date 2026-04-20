"""Supabase products 테이블 래퍼 (SQLite 폴백 없음).

환경변수:
  SUPABASE_URL  (기본값 하드코딩)
  SUPABASE_KEY  (기본값 하드코딩)
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

_DEFAULT_URL = "https://oynefikqoibwtfpjlizv.supabase.co"
_DEFAULT_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im95bmVmaWtxb2lid3RmcGpsaXp2Iiwicm9sZSI6"
    "InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3NjA1NzgwMywiZXhwIjoyMDkxNjMzODAzfQ"
    ".eCFcjx7gOhiv7mCyR2RiadndE9d6e6kVOWysHrarZTM"
)

_client_cache: Any = None


def get_client():
    """Supabase 클라이언트 싱글톤 반환."""
    global _client_cache
    if _client_cache is None:
        from supabase import create_client
        url = os.environ.get("SUPABASE_URL", _DEFAULT_URL)
        key = os.environ.get("SUPABASE_KEY", _DEFAULT_KEY)
        _client_cache = create_client(url, key)
    return _client_cache


get_supabase_client = get_client


def fetch_all_products(country: str = "SG") -> list[dict[str, Any]]:
    """products 테이블에서 해당 국가 전체 품목 조회 (deleted_at is null)."""
    sb = get_client()
    r = (
        sb.table("products")
        .select("*")
        .eq("country", country)
        .is_("deleted_at", "null")
        .order("crawled_at", desc=True)
        .execute()
    )
    return r.data or []


def fetch_kup_products(country: str = "SG") -> list[dict[str, Any]]:
    """KUP 파이프라인 품목만 조회 (source_name='{country}:kup_pipeline')."""
    sb = get_client()
    r = (
        sb.table("products")
        .select("*")
        .eq("country", country)
        .eq("source_name", f"{country}:kup_pipeline")
        .is_("deleted_at", "null")
        .execute()
    )
    return r.data or []


def upsert_product(row: dict[str, Any]) -> bool:
    """products 테이블에 upsert. 실패 시 False 반환."""
    sb = get_client()
    now = datetime.now(timezone.utc).isoformat()
    row.setdefault("crawled_at", now)
    row.setdefault("confidence", 0.5)
    try:
        sb.table("products").upsert(
            row,
            on_conflict="country,source_name,source_url",
        ).execute()
        return True
    except Exception:
        return False
