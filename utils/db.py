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


def upsert_cl_pricing(row: dict[str, Any]) -> bool:
    """cl_pricing 테이블에 칠레 약가 데이터 upsert.

    cl_pricing 스키마 핵심 컬럼:
      source_site (text), inn_name (text), raw_price_clp (decimal),
      brand_name, source_url, crawled_at, cenabast_max_price_clp 등
    NOTE: 크롤러에서 "source" 키로 보내도 "source_site" 로 자동 변환.
    """
    sb = get_client()
    now = datetime.now(timezone.utc).isoformat()
    row = {k: v for k, v in row.items() if k not in ("ocds_data", "raw_text")}
    row.setdefault("crawled_at", now)

    # 크롤러 호환: "source" → "source_site" 자동 변환
    if "source" in row and "source_site" not in row:
        row["source_site"] = row.pop("source")
    elif "source" in row:
        row.pop("source")

    # 추가 필드명 통일 (구버전 크롤러 호환)
    if "cenabast_supply_price_clp" in row:
        row.setdefault("cenabast_max_price_clp", row.pop("cenabast_supply_price_clp"))
    if "max_retail_price_clp" in row:
        row.setdefault("raw_price_clp", row.pop("max_retail_price_clp"))
    if "awarded_price_clp" in row:
        row.pop("awarded_price_clp", None)  # 별도 procurement 테이블에 적재

    # 알 수 없는 컬럼 제거 (스키마 외 컬럼이 있으면 insert 실패)
    # member_price_clp 등 크롤러 전용 필드는 raw_text에 기록 후 제거
    if "member_price_clp" in row and row["member_price_clp"]:
        row["raw_text"] = (row.get("raw_text") or "") + f" | 소시오CLP={row['member_price_clp']}"
    _KNOWN_COLS = {
        "id", "product_id", "market_segment", "fob_estimated_usd", "confidence",
        "crawled_at", "inn_name", "brand_name", "source_site", "raw_price_clp",
        "package_size", "price_per_unit_clp", "vat_rate", "pharmacy_margin",
        "source_url", "raw_text", "strength_mg", "dosage_form", "manufacturer",
        "cenabast_max_price_clp", "is_cenabast_regulated",
    }
    row = {k: v for k, v in row.items() if k in _KNOWN_COLS}

    try:
        # unique constraint가 없으므로 단순 insert (중복은 inn_name+source_url 기준 허용)
        sb.table("cl_pricing").insert(row).execute()
        return True
    except Exception:
        return False


def upsert_cl_analysis_p1(row: dict[str, Any]) -> bool:
    """cl_analysis_p1 테이블에 P1 시장 적합성 분석 결과 upsert.

    필수: product_id
    도메인 칼럼:
      거시환경: market_context
      규제:     isp_reg
      가격:     price_positioning
      판정:     verdict, verdict_confidence, rationale, entry_pathway
      리스크:   risks_conditions
      구조화:   key_factors (list), sources (list)
    """
    sb = get_client()
    now = datetime.now(timezone.utc).isoformat()
    row = dict(row)
    row.setdefault("analyzed_at", now)

    # jsonb 필드: list/dict → 그대로 전달 (Supabase 클라이언트가 직렬화)
    # None 값 칼럼 제거 (불필요한 null 덮어쓰기 방지)
    clean = {k: v for k, v in row.items() if v is not None and v != ""}
    try:
        sb.table("cl_analysis_p1").upsert(
            clean,
            on_conflict="product_id",
        ).execute()
        return True
    except Exception:
        return False


def upsert_cl_analysis_p2(row: dict[str, Any]) -> bool:
    """cl_analysis_p2 테이블에 P2 가격 전략 분석 결과 upsert.

    필수: product_name, market_segment ('public' | 'private')
    도메인 칼럼:
      가격:   final_price_clp, final_price_usd, ref_price_clp, ref_price_usd,
              rationale, scenarios (list), formula_elements (list)
      거시:   market_summary
      환율:   exchange_usd_clp, exchange_usd_krw
    """
    sb = get_client()
    now = datetime.now(timezone.utc).isoformat()
    row = dict(row)
    row.setdefault("analyzed_at", now)
    clean = {k: v for k, v in row.items() if v is not None and v != ""}
    try:
        sb.table("cl_analysis_p2").upsert(
            clean,
            on_conflict="product_name,market_segment",
        ).execute()
        return True
    except Exception:
        return False


def upsert_cl_analysis_p3(row: dict[str, Any]) -> bool:
    """cl_analysis_p3 테이블에 P3 바이어 발굴 분석 결과 upsert.

    필수: company_name, product_label
    도메인 칼럼:
      기업개요: company_overview_kr, revenue, employees, founded,
               territories (list), certifications (list)
      자격검증: has_gmp, import_history, has_pharmacy_chain, public_channel,
               private_channel, mah_capable, procurement_history,
               korea_experience, has_target_country_presence
      추천근거: recommendation_reason, score, rank_position
      출처:    source_urls (list)
    """
    sb = get_client()
    now = datetime.now(timezone.utc).isoformat()
    row = dict(row)
    row.setdefault("enriched_at", now)
    clean = {k: v for k, v in row.items() if v is not None and v != ""}
    try:
        sb.table("cl_analysis_p3").upsert(
            clean,
            on_conflict="company_name,product_label",
        ).execute()
        return True
    except Exception:
        return False


def fetch_cl_pricing_context(inn_name: str, limit: int = 10) -> str:
    """INN 성분명 기준 cl_pricing 데이터를 가져와 AI 분석용 문자열 반환."""
    try:
        sb = get_client()
        r = (
            sb.table("cl_pricing")
            .select("source_site,inn_name,brand_name,raw_price_clp,cenabast_max_price_clp,crawled_at")
            .ilike("inn_name", f"%{inn_name}%")
            .order("crawled_at", desc=True)
            .limit(limit)
            .execute()
        )
        rows = r.data or []
        if not rows:
            return ""
        lines = [f"[DB 크롤 데이터 — {inn_name}]"]
        for row in rows:
            src = row.get("source_site", "?")
            brand = row.get("brand_name", "")
            retail = row.get("raw_price_clp")
            supply = row.get("cenabast_max_price_clp")
            awarded = None
            parts = [f"{src}"]
            if brand:
                parts.append(f"브랜드={brand}")
            if retail:
                parts.append(f"소매CLP={retail:,.0f}")
            if supply:
                parts.append(f"CENABAST공급CLP={supply:,.0f}")
            if awarded:
                parts.append(f"낙찰CLP={awarded:,.0f}")
            lines.append("  " + " | ".join(parts))
        return "\n".join(lines)
    except Exception:
        return ""
