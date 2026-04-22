"""칠레 약가 크롤링 파이프라인 오케스트레이터.

Cruz Verde · Salcobrand · Farmacias Ahumada · CENABAST · Mercado Público
5개 소스를 순차 실행하고 결과를 병합합니다.
"""
from __future__ import annotations

import asyncio
from typing import Any


async def run_all_crawlers(inn_names: list[str]) -> list[dict[str, Any]]:
    """모든 칠레 약가 소스를 순차 크롤링하여 결과 병합.

    Returns:
        모든 소스의 수집 결과 목록 (오류 항목 포함)
    """
    from utils.cl_cruzverde_crawler   import crawl as crawl_cv
    from utils.cl_salcobrand_crawler  import crawl as crawl_sb
    from utils.cl_ahumada_crawler     import crawl as crawl_ah
    from utils.cl_cenabast_crawler    import crawl as crawl_cn
    from utils.cl_mercadopublico_crawler import crawl as crawl_mp

    all_rows: list[dict[str, Any]] = []

    # 소매 약국 3개 병렬 실행
    retail_results = await asyncio.gather(
        crawl_cv(inn_names),
        crawl_sb(inn_names),
        crawl_ah(inn_names),
        return_exceptions=True,
    )
    for result in retail_results:
        if isinstance(result, list):
            all_rows.extend(result)

    # CENABAST (전체 목록 파싱 → 순차)
    try:
        cenabast_rows = await crawl_cn(inn_names)
        all_rows.extend(cenabast_rows)
    except Exception as exc:
        all_rows.append({"error": str(exc), "source": "cenabast"})

    # Mercado Público (공공 API)
    try:
        mp_rows = await crawl_mp(inn_names)
        all_rows.extend(mp_rows)
    except Exception as exc:
        all_rows.append({"error": str(exc), "source": "mercadopublico"})

    return all_rows
