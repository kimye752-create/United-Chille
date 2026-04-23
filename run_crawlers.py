"""칠레 크롤러 일괄 실행 스크립트.

실행:
  python run_crawlers.py

수집 대상 (8개 성분):
  cilostazol, atorvastatin, rosuvastatin, omega-3,
  fluticasone, hydroxyurea, gadobutrol, mosapride

저장 테이블:
  - cl_pricing        : 소매 약가 (Salcobrand, Cruz Verde, Ahumada)
  - cl_cenabast_prices: CENABAST 공급가 / 상한가
  - cl_procurement    : Mercado Público 낙찰 데이터
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from typing import Any

# 프로젝트 루트를 sys.path에 추가
sys.path.insert(0, os.path.dirname(__file__))

# .env 로드
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── 대상 INN 성분명 ───────────────────────────────────────────────────────────
INN_NAMES = [
    "cilostazol",
    "atorvastatin",
    "rosuvastatin",
    "omega-3",
    "fluticasone",
    "hydroxyurea",
    "gadobutrol",
    "mosapride",
]

# ── Supabase 클라이언트 ────────────────────────────────────────────────────────
def _get_sb():
    from utils.db import get_client
    return get_client()


def _save_cl_pricing(row: dict) -> bool:
    from utils.db import upsert_cl_pricing
    return upsert_cl_pricing(row)


def _save_cenabast(rows: list[dict]) -> int:
    """cl_cenabast_prices 테이블에 저장."""
    sb = _get_sb()
    saved = 0
    now = datetime.now(timezone.utc).isoformat()
    for row in rows:
        if "error" in row:
            continue
        clean = {
            "inn_name":                  row.get("inn_name"),
            "brand_name":                row.get("brand_name"),
            "strength":                  row.get("strength"),
            "dosage_form":               row.get("dosage_form"),
            "package_size":              row.get("package_size"),
            "cenabast_supply_price_clp": row.get("cenabast_supply_price_clp"),
            "max_retail_price_clp":      row.get("max_retail_price_clp"),
            "regulation_law":            row.get("regulation_law", "Ley 21.198"),
            "source_url":                row.get("source_url"),
            "scraped_at":                now,
        }
        clean = {k: v for k, v in clean.items() if v is not None}
        try:
            sb.table("cl_cenabast_prices").insert(clean).execute()
            saved += 1
        except Exception as e:
            print(f"  [WARN] cl_cenabast_prices insert 실패: {e}")
    return saved


def _save_procurement(rows: list[dict]) -> int:
    """cl_procurement 테이블에 저장."""
    sb = _get_sb()
    saved = 0
    now = datetime.now(timezone.utc).isoformat()
    for row in rows:
        if "error" in row:
            continue
        clean = {
            "tender_id":          row.get("tender_id"),
            "buyer_org":          row.get("buyer_org"),
            "inn_name":           row.get("inn_name"),
            "brand_name":         row.get("brand_name"),
            "quantity":           row.get("quantity"),
            "unit":               row.get("unit"),
            "awarded_price_clp":  row.get("awarded_price_clp"),
            "award_date":         row.get("award_date"),
            "tender_status":      row.get("tender_status", "awarded"),
            "source_url":         row.get("source_url"),
            "crawled_at":         now,
        }
        # ocds_data는 jsonb — dict이면 그대로, 아니면 제외
        if isinstance(row.get("ocds_data"), dict):
            clean["ocds_data"] = row["ocds_data"]
        clean = {k: v for k, v in clean.items() if v is not None}
        try:
            sb.table("cl_procurement").insert(clean).execute()
            saved += 1
        except Exception as e:
            print(f"  [WARN] cl_procurement insert 실패: {e}")
    return saved


# ── 크롤러 개별 실행 ──────────────────────────────────────────────────────────

async def run_salcobrand(inn_names: list[str]) -> tuple[int, int]:
    print("\n[1/5] Salcobrand 소매가 크롤링...")
    from utils.cl_salcobrand_crawler import crawl
    rows = await crawl(inn_names)
    ok = err = 0
    for row in rows:
        if "error" in row:
            print(f"  [ERR] {row}")
            err += 1
        else:
            _save_cl_pricing(row)
            ok += 1
    print(f"  → 저장: {ok}건, 오류: {err}건")
    return ok, err


async def run_cruzverde(inn_names: list[str]) -> tuple[int, int]:
    print("\n[2/5] Cruz Verde 소매가 크롤링...")
    try:
        from utils.cl_cruzverde_crawler import crawl
        rows = await crawl(inn_names)
        ok = err = 0
        for row in rows:
            if "error" in row:
                print(f"  [ERR] {row.get('error', '')[:100]}")
                err += 1
            else:
                _save_cl_pricing(row)
                ok += 1
        print(f"  → 저장: {ok}건, 오류: {err}건")
        return ok, err
    except Exception as e:
        print(f"  [SKIP] Cruz Verde 실패: {e}")
        return 0, 1


async def run_ahumada(inn_names: list[str]) -> tuple[int, int]:
    print("\n[3/5] Farmacias Ahumada 소매가 크롤링...")
    try:
        from utils.cl_ahumada_crawler import crawl
        rows = await crawl(inn_names)
        ok = err = 0
        for row in rows:
            if "error" in row:
                print(f"  [ERR] {row.get('error', '')[:100]}")
                err += 1
            else:
                _save_cl_pricing(row)
                ok += 1
        print(f"  → 저장: {ok}건, 오류: {err}건")
        return ok, err
    except Exception as e:
        print(f"  [SKIP] Ahumada 실패: {e}")
        return 0, 1


async def run_cenabast(inn_names: list[str]) -> tuple[int, int]:
    print("\n[4/5] CENABAST 공급가 / 상한가 크롤링...")
    try:
        from utils.cl_cenabast_crawler import crawl
        rows = await crawl(inn_names)
        # cl_pricing에도 cenabast_max_price_clp로 업서트
        ok_pricing = 0
        cenabast_rows = []
        for row in rows:
            if "error" in row:
                print(f"  [ERR] {row.get('error', '')[:100]}")
                continue
            if row.get("found") is False:
                print(f"  [미발견] {row.get('inn_name')}")
                continue
            # cl_pricing upsert (cenabast_max_price_clp 채우기)
            pricing_row = {
                "source_site":             "cenabast",
                "inn_name":                row.get("inn_name"),
                "source_url":              row.get("source_url", "https://www.cenabast.cl"),
                "cenabast_max_price_clp":  row.get("max_retail_price_clp"),
                "raw_price_clp":           row.get("cenabast_supply_price_clp"),
                "raw_text":                row.get("raw_text", ""),
            }
            _save_cl_pricing(pricing_row)
            ok_pricing += 1
            cenabast_rows.append(row)
        ok_cenabast = _save_cenabast(cenabast_rows)
        print(f"  → cl_pricing 저장: {ok_pricing}건, cl_cenabast_prices 저장: {ok_cenabast}건")
        return ok_cenabast, 0
    except Exception as e:
        print(f"  [SKIP] CENABAST 실패: {e}")
        return 0, 1


async def run_mercadopublico(inn_names: list[str]) -> tuple[int, int]:
    print("\n[5/5] Mercado Público 공공조달 크롤링...")
    try:
        from utils.cl_mercadopublico_crawler import crawl
        rows = await crawl(inn_names)
        ok = err = 0
        for row in rows:
            if "error" in row:
                print(f"  [ERR] {row.get('error', '')[:100]}")
                err += 1
        ok = _save_procurement([r for r in rows if "error" not in r])
        print(f"  → cl_procurement 저장: {ok}건, 오류: {err}건")
        return ok, err
    except Exception as e:
        print(f"  [SKIP] Mercado Público 실패: {e}")
        return 0, 1


# ── 메인 ──────────────────────────────────────────────────────────────────────

async def main():
    start = datetime.now()
    print("=" * 60)
    print(f"칠레 크롤러 일괄 실행 시작 — {start.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"대상 INN: {', '.join(INN_NAMES)}")
    print("=" * 60)

    # 크롤러 순차 실행 (사이트 부하 분산)
    await run_salcobrand(INN_NAMES)
    await asyncio.sleep(2)
    await run_cruzverde(INN_NAMES)
    await asyncio.sleep(2)
    await run_ahumada(INN_NAMES)
    await asyncio.sleep(2)
    await run_cenabast(INN_NAMES)
    await asyncio.sleep(2)
    await run_mercadopublico(INN_NAMES)

    # 결과 요약
    print("\n" + "=" * 60)
    print("크롤링 완료. DB 현황:")
    sb = _get_sb()
    for tbl in ["cl_pricing", "cl_cenabast_prices", "cl_procurement"]:
        r = sb.table(tbl).select("id", count="exact").execute()
        print(f"  {tbl}: {r.count}건")
    elapsed = (datetime.now() - start).seconds
    print(f"소요 시간: {elapsed}초")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
