"""우루과이(UY) 거시 경제 및 의약품 시장 지표.

수치 출처:
  GDP/capita: IMF World Economic Outlook 2024
  인구: UN World Population Prospects 2024
  의약품 시장: IMS Health / IQVIA 남미 보고서 추정
  성장률: IMF 실질 GDP 성장률 2024
  보건지출: WHO GHED 2022
  부가세·코페이먼트: MSP 우루과이 공중보건부 2025.01
"""

from __future__ import annotations

import os
from typing import Any

UY_MACRO: dict[str, Any] = {
    "country": "UY",
    "country_name": "우루과이",
    "currency": "UYU",
    "gdp_per_capita_usd": 20_045,
    "population_m": 3.6,
    "pharma_market_usd_m": 850,
    "real_growth_pct": 3.2,
    "healthcare_pct_gdp": 9.4,
    "vat_pharma_pct": float(os.environ.get("UY_VAT_PHARMA_PCT", "10.0")),
    "copayment_ceiling_uyu": float(os.environ.get("UY_COPAYMENT_CEILING_UYU", "880.0")),
    "pharmacy_margin_pct": 25.0,
    "distributor_margin_pct": 15.0,
    "import_duty_pct": 0.0,
    "source": {
        "gdp": "IMF WEO 2024",
        "population": "UN WPP 2024",
        "pharma_market": "IQVIA 남미 추정 2024",
        "growth": "IMF 실질 GDP 2024",
        "healthcare": "WHO GHED 2022",
        "tax": "MSP 우루과이 2025.01",
    },
}


def get_uy_macro() -> dict[str, Any]:
    """우루과이 거시지표 반환. 환경변수로 VAT·코페이먼트 동적 갱신."""
    data = dict(UY_MACRO)
    data["vat_pharma_pct"] = float(os.environ.get("UY_VAT_PHARMA_PCT", "10.0"))
    data["copayment_ceiling_uyu"] = float(
        os.environ.get("UY_COPAYMENT_CEILING_UYU", "880.0")
    )
    return data
