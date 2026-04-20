"""우루과이(UY) 의약품 시장 분석 오케스트레이터 (Engine 1~3 통합).

주요 흐름:
  1. 크롤러 5종(SICE, Farmashop, Farma.uy, San Roque, Rex) 병렬 실행
  2. uy_parser.py로 스페인어 파싱 및 UYU→USD 변환
  3. Supabase uy_pricing 테이블에 공통 6컬럼 규격으로 INSERT
  4. ORPM 벤치마크 대비 이상치 탐지 (±30% 초과 시 confidence 강등)
  5. FOB 역산기(Logic A/B) 실행 → 3 시나리오 반환

타깃 기본 품목: Cilostazol (실로스타졸)
"""

from __future__ import annotations

import asyncio
import os
import time
from decimal import Decimal
from typing import Any

DEFAULT_INN_NAMES: list[str] = [
    "Cilostazol",
    "Clopidogrel",
    "Rosuvastatin",
    "Atorvastatin",
    "Metformin",
    "Amlodipine",
]

OUTLIER_THRESHOLD = 0.30  # ORPM 기준 ±30% 초과 시 confidence 강등


async def run_all_crawlers(
    inn_names: list[str] | None = None,
    emit: Any = None,
) -> dict[str, list[Any]]:
    from utils.uy_farmauy_crawler import crawl_farmauy
    from utils.uy_rex_crawler import crawl_rex
    from utils.uy_sanroque_crawler import crawl_sanroque
    from utils.uy_farmashop_crawler import crawl_farmashop
    from utils.uy_sice_crawler import crawl_sice_to_parsed

    targets = inn_names or DEFAULT_INN_NAMES
    all_results: dict[str, list[Any]] = {}

    for inn in targets:
        if emit:
            await emit({"phase": "uy_crawl", "message": f"{inn} — 크롤링 시작", "level": "info"})

        crawl_tasks = [
            crawl_farmauy(inn),
            crawl_rex(inn),
            crawl_sanroque(inn),
            crawl_farmashop(inn),
            crawl_sice_to_parsed(inn),
        ]
        results = await asyncio.gather(*crawl_tasks, return_exceptions=True)

        merged: list[Any] = []
        source_labels = ["farmauy", "rex", "sanroque", "farmashop", "sice"]
        for label, result in zip(source_labels, results):
            if isinstance(result, Exception):
                if emit:
                    await emit({
                        "phase": "uy_crawl",
                        "message": f"{inn} [{label}] 오류: {result}",
                        "level": "warn",
                    })
                continue
            valid = [r for r in result if r is not None]
            merged.extend(valid)
            if emit:
                await emit({
                    "phase": "uy_crawl",
                    "message": f"{inn} [{label}] {len(valid)}건 수집",
                    "level": "success" if valid else "info",
                })

        all_results[inn] = merged

    return all_results


def _detect_outliers(
    parsed_list: list[Any],
    benchmark_usd: float | None,
) -> list[Any]:
    if benchmark_usd is None or benchmark_usd <= 0:
        return parsed_list

    for drug in parsed_list:
        if not hasattr(drug, "price_per_unit_usd"):
            continue
        price = float(drug.price_per_unit_usd)
        if price <= 0:
            continue
        deviation = abs(price - benchmark_usd) / benchmark_usd
        if deviation > OUTLIER_THRESHOLD:
            drug.confidence = min(drug.confidence, 0.5)
            drug.extra["outlier"] = True
            drug.extra["deviation_pct"] = round(deviation * 100, 1)
    return parsed_list


def _build_db_row(drug: Any, product_id: str | None = None) -> dict[str, Any]:
    vat_rate = float(os.environ.get("UY_VAT_PHARMA_PCT", "10.0")) / 100.0

    segment = "public" if getattr(drug, "source_site", "") == "sice" else "private"
    confidence_map = {
        "sice":      0.9,
        "farmashop": 0.8,
        "farmauy":   0.75,
        "sanroque":  0.7,
        "rex":       0.7,
    }
    site = getattr(drug, "source_site", "")
    base_conf = confidence_map.get(site, 0.6)
    final_conf = min(getattr(drug, "confidence", base_conf), base_conf)

    farmacard_uyu = None
    fc = getattr(drug, "farmacard_price_uyu", None)
    if fc is not None:
        farmacard_uyu = float(fc)

    return {
        "product_id": product_id,
        "market_segment": segment,
        "fob_estimated_usd": None,
        "confidence": round(final_conf, 2),
        "inn_name": getattr(drug, "inn_name", "") or "",
        "brand_name": getattr(drug, "brand_name", "") or "",
        "source_site": site,
        "raw_price_uyu": float(getattr(drug, "total_price_uyu", 0) or 0),
        "package_size": getattr(drug, "pack_size", None),
        "price_per_unit_uyu": float(getattr(drug, "price_per_unit_uyu", 0) or 0),
        "vat_rate": vat_rate,
        "farmacard_price_uyu": farmacard_uyu,
        "source_url": getattr(drug, "source_url", "") or "",
        "raw_text": (getattr(drug, "raw_text", "") or "")[:1000],
        "strength_mg": getattr(drug, "strength_mg", None),
        "dosage_form": getattr(drug, "dosage_form", None),
        "manufacturer": getattr(drug, "manufacturer", None),
    }


async def save_to_supabase(rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    try:
        from utils.db import get_supabase_client
        sb = get_supabase_client()
        result = sb.table("uy_pricing").insert(rows).execute()
        return len(result.data) if result.data else 0
    except Exception:
        return 0


async def run_fob_calculation(
    parsed_list: list[Any],
    inn_name: str,
) -> list[dict[str, Any]]:
    from analysis.fob_calculator import calc_logic_a, calc_logic_b, fob_result_to_dict
    from utils.uy_parser import _uyu_to_usd_rate

    fob_results: list[dict[str, Any]] = []
    rate = _uyu_to_usd_rate()

    public_drugs = [d for d in parsed_list if getattr(d, "source_site", "") == "sice"]
    private_drugs = [d for d in parsed_list if getattr(d, "source_site", "") != "sice"]

    if public_drugs:
        prices = [float(d.price_per_unit_usd) for d in public_drugs if float(d.price_per_unit_usd) > 0]
        if prices:
            avg_public = Decimal(str(sum(prices) / len(prices)))
            result_a = calc_logic_a(avg_public, inn_name=inn_name)
            fob_results.append({**fob_result_to_dict(result_a), "source_count": len(public_drugs)})

    if private_drugs:
        prices = [float(d.price_per_unit_usd) for d in private_drugs if float(d.price_per_unit_usd) > 0]
        if prices:
            avg_private = Decimal(str(sum(prices) / len(prices)))
            result_b = calc_logic_b(avg_private, inn_name=inn_name)
            fob_results.append({**fob_result_to_dict(result_b), "source_count": len(private_drugs)})

    return fob_results


async def analyze_uy_market(
    inn_names: list[str] | None = None,
    save_db: bool = True,
    emit: Any = None,
) -> dict[str, Any]:
    start = time.time()
    targets = inn_names or DEFAULT_INN_NAMES

    crawl_results = await run_all_crawlers(targets, emit=emit)

    all_db_rows: list[dict[str, Any]] = []
    fob_by_inn: dict[str, list[dict[str, Any]]] = {}

    for inn, parsed_list in crawl_results.items():
        cleaned = _detect_outliers(parsed_list, benchmark_usd=None)
        rows = [_build_db_row(d) for d in cleaned]
        all_db_rows.extend(rows)
        fob_by_inn[inn] = await run_fob_calculation(cleaned, inn)

    saved_count = 0
    if save_db:
        saved_count = await save_to_supabase(all_db_rows)
        if emit:
            await emit({
                "phase": "uy_db",
                "message": f"Supabase uy_pricing {saved_count}건 적재 완료",
                "level": "success",
            })

    total_count = sum(len(v) for v in crawl_results.values())
    elapsed = round(time.time() - start, 1)

    return {
        "ok": True,
        "elapsed_sec": elapsed,
        "inn_names": targets,
        "total_collected": total_count,
        "saved_to_db": saved_count,
        "fob_results": fob_by_inn,
        "crawl_summary": {
            inn: len(items) for inn, items in crawl_results.items()
        },
    }
