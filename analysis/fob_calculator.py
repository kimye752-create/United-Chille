"""FOB 역산기 (Engine 4 & 5) — Logic A (공공조달) / Logic B (민간소매).

핵심 방어:
  - VAT는 환경변수 UY_VAT_PHARMA_PCT에서 동적 로드 (하드코딩 절대 금지)
  - 3 시나리오 출력: conservative / base / optimistic
  - 인도네시아 및 우루과이 시장 모두 적용 가능한 범용 설계
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

ScenarioKey = Literal["conservative", "base", "optimistic"]


@dataclass(frozen=True)
class FobScenario:
    scenario: ScenarioKey
    het_usd: Decimal
    hna_usd: Decimal
    fob_usd: Decimal
    logic: str
    notes: str


@dataclass(frozen=True)
class FobResult:
    conservative: FobScenario
    base: FobScenario
    optimistic: FobScenario
    market_segment: str
    inn_name: str
    source_price_usd: Decimal
    source_label: str


def _env_rate(key: str, default: float) -> Decimal:
    try:
        return Decimal(str(float(os.environ.get(key, str(default)))))
    except Exception:
        return Decimal(str(default))


def _vat_rate() -> Decimal:
    return _env_rate("UY_VAT_PHARMA_PCT", 10.0) / Decimal("100")


# ── Logic A: 공공조달(SICE 낙찰가) 역산 ─────────────────────────────────────────

_PUBLIC_SCENARIOS: dict[ScenarioKey, dict[str, Decimal]] = {
    "conservative": {
        "logistics_rate":     Decimal("0.08"),
        "partner_margin":     Decimal("0.20"),
        "buffer":             Decimal("0.30"),
    },
    "base": {
        "logistics_rate":     Decimal("0.06"),
        "partner_margin":     Decimal("0.15"),
        "buffer":             Decimal("0.20"),
    },
    "optimistic": {
        "logistics_rate":     Decimal("0.04"),
        "partner_margin":     Decimal("0.10"),
        "buffer":             Decimal("0.10"),
    },
}


def calc_logic_a(
    award_price_usd: Decimal,
    import_duty_rate: Decimal | None = None,
    inn_name: str = "",
) -> FobResult:
    """공공조달 낙찰가 → FOB 역산 (Logic A).

    FOB_A = award_price × (1 - duty) × (1 - logistics) × (1 - margin) × (1 - buffer)
    """
    duty = import_duty_rate if import_duty_rate is not None else _env_rate("UY_IMPORT_DUTY_PCT", 0.0) / Decimal("100")
    one = Decimal("1")

    scenarios: dict[ScenarioKey, FobScenario] = {}
    for key, rates in _PUBLIC_SCENARIOS.items():
        fob = (
            award_price_usd
            * (one - duty)
            * (one - rates["logistics_rate"])
            * (one - rates["partner_margin"])
            * (one - rates["buffer"])
        )
        scenarios[key] = FobScenario(
            scenario=key,
            het_usd=award_price_usd,
            hna_usd=award_price_usd * (one - duty),
            fob_usd=max(fob, Decimal("0")),
            logic="A",
            notes=(
                f"duty={float(duty):.1%} logistics={float(rates['logistics_rate']):.1%} "
                f"margin={float(rates['partner_margin']):.1%} buffer={float(rates['buffer']):.1%}"
            ),
        )

    return FobResult(
        conservative=scenarios["conservative"],
        base=scenarios["base"],
        optimistic=scenarios["optimistic"],
        market_segment="public",
        inn_name=inn_name,
        source_price_usd=award_price_usd,
        source_label="SICE 낙찰가",
    )


# ── Logic B: 민간소매(HET) 역산 ──────────────────────────────────────────────────

_PRIVATE_SCENARIOS: dict[ScenarioKey, dict[str, Decimal]] = {
    "conservative": {
        "pharmacy_margin":    Decimal("0.30"),
        "distributor_margin": Decimal("0.20"),
        "import_costs":       Decimal("0.08"),
    },
    "base": {
        "pharmacy_margin":    Decimal("0.25"),
        "distributor_margin": Decimal("0.15"),
        "import_costs":       Decimal("0.06"),
    },
    "optimistic": {
        "pharmacy_margin":    Decimal("0.20"),
        "distributor_margin": Decimal("0.10"),
        "import_costs":       Decimal("0.04"),
    },
}


def calc_logic_b(
    het_usd: Decimal,
    inn_name: str = "",
    vat_rate_override: Decimal | None = None,
) -> FobResult:
    """민간소매 HET(소비자가) → FOB 역산 (Logic B).

    HNA = HET / (1 + VAT) / (1 + pharmacy_margin)
    FOB = HNA / (1 + distributor_margin) / (1 + import_costs)
    """
    vat = vat_rate_override if vat_rate_override is not None else _vat_rate()
    one = Decimal("1")

    scenarios: dict[ScenarioKey, FobScenario] = {}
    for key, rates in _PRIVATE_SCENARIOS.items():
        hna = het_usd / (one + vat) / (one + rates["pharmacy_margin"])
        fob = hna / (one + rates["distributor_margin"]) / (one + rates["import_costs"])
        scenarios[key] = FobScenario(
            scenario=key,
            het_usd=het_usd,
            hna_usd=hna,
            fob_usd=max(fob, Decimal("0")),
            logic="B",
            notes=(
                f"vat={float(vat):.1%} pharmacy={float(rates['pharmacy_margin']):.1%} "
                f"distributor={float(rates['distributor_margin']):.1%} "
                f"import={float(rates['import_costs']):.1%}"
            ),
        )

    return FobResult(
        conservative=scenarios["conservative"],
        base=scenarios["base"],
        optimistic=scenarios["optimistic"],
        market_segment="private",
        inn_name=inn_name,
        source_price_usd=het_usd,
        source_label="Farmashop/Farma.uy 소매가",
    )


def fob_result_to_dict(result: FobResult) -> dict:
    def scenario_dict(s: FobScenario) -> dict:
        return {
            "scenario": s.scenario,
            "het_usd": float(s.het_usd),
            "hna_usd": float(s.hna_usd),
            "fob_usd": float(s.fob_usd),
            "logic": s.logic,
            "notes": s.notes,
        }

    return {
        "inn_name": result.inn_name,
        "market_segment": result.market_segment,
        "source_price_usd": float(result.source_price_usd),
        "source_label": result.source_label,
        "conservative": scenario_dict(result.conservative),
        "base": scenario_dict(result.base),
        "optimistic": scenario_dict(result.optimistic),
    }


def msp_copayment_check(fob_usd: Decimal, uyu_per_usd: float = 40.3) -> dict:
    """산출된 FOB가 MSP 코페이먼트 상한선 제약 내에 있는지 검증."""
    ceiling_uyu = float(os.environ.get("UY_COPAYMENT_CEILING_UYU", "880"))
    ceiling_usd = ceiling_uyu / uyu_per_usd
    within = float(fob_usd) <= ceiling_usd
    return {
        "copayment_ceiling_uyu": ceiling_uyu,
        "copayment_ceiling_usd": round(ceiling_usd, 4),
        "fob_usd": float(fob_usd),
        "within_ceiling": within,
        "note": "MSP 2025.01 상한 기준" if within else "상한 초과 — 급여 등재 협상 필요",
    }
