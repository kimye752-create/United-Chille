"""인도네시아 실로스타졸(Cilostazol) CR 파트너 AHP 매칭 엔진 (Engine 6).

가중치:
  심혈관 포트폴리오 시너지: 40%
  시장 지배력·매출 규모:   30%
  다국적 협력·인허가 역량:  30%

조건부 분기:
  IR(속방형) 제제 보유사 → 직접 감점이 아닌 'line_extension' 전략으로 분기
  (Citaz→Citaz CR, Aggravan→Aggravan XR 라인 익스텐션 제안)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

PitchStrategy = Literal["direct", "line_extension"]


@dataclass
class CandidateProfile:
    company_name: str
    market_rank: int
    cardio_portfolio: list[str]
    has_ir_cilostazol: bool
    has_sr_cilostazol: bool
    intl_experience: bool
    gmp_certified: bool
    mah_capable: bool
    email: str
    phone: str
    headquarters: str
    notes: str = ""


@dataclass
class AhpResult:
    company_name: str
    rank: int
    psi_score: float
    cardio_score: float
    market_score: float
    intl_score: float
    pitch_strategy: PitchStrategy
    email: str
    phone: str
    headquarters: str
    key_products: list[str]
    notes: str
    pitch_memo: str = ""


WEIGHTS: dict[str, float] = {
    "cardio": 0.40,
    "market": 0.30,
    "intl":   0.30,
}

INDONESIA_CANDIDATES: list[CandidateProfile] = [
    CandidateProfile(
        company_name="PT Phapros Tbk",
        market_rank=5,
        cardio_portfolio=[
            "Cardismo XR (Isosorbide mononitrate SR)",
            "Letonal (Spironolactone)",
        ],
        has_ir_cilostazol=False,
        has_sr_cilostazol=False,
        intl_experience=True,
        gmp_certified=True,
        mah_capable=True,
        email="corporate@phapros.co.id",
        phone="+62-21-576-2709",
        headquarters="Menara Rajawali 17F, Jakarta",
        notes="Kimia Farma 그룹 계열사. Cardismo XR 론칭 성공으로 서방형 제제 상업화 역량 검증.",
    ),
    CandidateProfile(
        company_name="PT Tempo Scan Pacific Tbk",
        market_rank=4,
        cardio_portfolio=[
            "Lopigard (Clopidogrel 75mg)",
            "Hyslo (Atenolol)",
        ],
        has_ir_cilostazol=False,
        has_sr_cilostazol=False,
        intl_experience=True,
        gmp_certified=True,
        mah_capable=True,
        email="info@pttempo.com",
        phone="+62-21-2921-8888",
        headquarters="Tempo Scan Tower 16F, Jakarta",
        notes="전국 46개 지사, 10만 유통 거점. Lopigard(클로피도그렐) 영업망과 크로스셀링 최적.",
    ),
    CandidateProfile(
        company_name="PT Darya-Varia Laboratoria Tbk",
        market_rank=7,
        cardio_portfolio=[
            "Atofar (Atorvastatin)",
            "Cedocard (Isosorbide Dinitrate)",
            "Clopigard (Clopidogrel)",
        ],
        has_ir_cilostazol=False,
        has_sr_cilostazol=False,
        intl_experience=True,
        gmp_certified=True,
        mah_capable=True,
        email="corporate@darya-varia.com",
        phone="+62-21-2276-8000",
        headquarters="South Quarter Tower, Jakarta",
        notes="고부가가치 전문의약품 중심 전략. 글로벌 파트너십 경험 풍부. 심장내과 네트워크 강함.",
    ),
    CandidateProfile(
        company_name="PT Kalbe Farma",
        market_rank=1,
        cardio_portfolio=[
            "Citaz 50mg (Cilostazol IR)",
            "Citaz 100mg (Cilostazol IR)",
            "Lasix (Furosemide)",
        ],
        has_ir_cilostazol=True,
        has_sr_cilostazol=False,
        intl_experience=True,
        gmp_certified=True,
        mah_capable=True,
        email="info@kalbe.co.id",
        phone="+62-21-4287-3888",
        headquarters="Gedung KALBE, Jakarta",
        notes="인도네시아 제약 1위. Citaz IR 보유 → CR 라인 익스텐션 전략 적용.",
    ),
    CandidateProfile(
        company_name="PT Dexa Medica",
        market_rank=2,
        cardio_portfolio=[
            "Aggravan 50mg (Cilostazol IR)",
            "Clopidogrel Dexa (Clopidogrel)",
        ],
        has_ir_cilostazol=True,
        has_sr_cilostazol=False,
        intl_experience=True,
        gmp_certified=True,
        mah_capable=True,
        email="info@dexa-medica.com",
        phone="+62-711-537-5859",
        headquarters="Palembang / Jakarta",
        notes="인도네시아 제약 2위. Aggravan IR 보유 → CR 라인 익스텐션(Aggravan XR) 전략.",
    ),
]


def _score_cardio(profile: CandidateProfile) -> float:
    score = 0.0
    cardio_count = len(profile.cardio_portfolio)
    if cardio_count >= 3:
        score += 0.5
    elif cardio_count >= 1:
        score += 0.3

    has_sr_cardio = any(
        "xr" in p.lower() or "sr " in p.lower() or "retard" in p.lower()
        for p in profile.cardio_portfolio
    )
    if has_sr_cardio:
        score += 0.3

    has_antiplatelet = any(
        any(kw in p.lower() for kw in ("clopidogrel", "aspirin", "ticagrelor", "prasugrel"))
        for p in profile.cardio_portfolio
    )
    if has_antiplatelet:
        score += 0.2

    return min(score, 1.0)


def _score_market(profile: CandidateProfile) -> float:
    rank = profile.market_rank
    if rank <= 2:
        return 1.0
    if rank <= 5:
        return 0.8
    if rank <= 10:
        return 0.6
    return 0.4


def _score_intl(profile: CandidateProfile) -> float:
    score = 0.0
    if profile.intl_experience:
        score += 0.5
    if profile.gmp_certified:
        score += 0.3
    if profile.mah_capable:
        score += 0.2
    return min(score, 1.0)


def _build_pitch_memo(profile: CandidateProfile, strategy: PitchStrategy) -> str:
    if strategy == "line_extension":
        ir_products = [p for p in profile.cardio_portfolio if "IR" in p or "cilostazol" in p.lower()]
        ir_name = ir_products[0].split("(")[0].strip() if ir_products else "기존 실로스타졸 제품"
        brand_base = ir_name.split()[0]
        return (
            f"귀사의 {ir_name}은 우수한 브랜드이나, 1일 2회 복용과 두통 부작용 한계가 있습니다. "
            f"BILDAS 특허 기술 기반 CR 200mg 1일 1회 제제를 라이선스 인하여 "
            f"'{brand_base} CR' 또는 '{brand_base} XR'로 출시하면 "
            f"오츠카(Pletaal SR)의 프리미엄 시장을 선점하고 기존 브랜드 가치를 업그레이드할 수 있습니다."
        )
    else:
        cardio = ", ".join(profile.cardio_portfolio[:2])
        return (
            f"귀사의 심혈관 포트폴리오({cardio})는 당사 실로스타졸 CR과 완벽한 시너지를 냅니다. "
            f"기존 IR 제제 없이 CR 200mg 1일 1회 제제를 통해 "
            f"오츠카(Pletaal SR) 대비 비용 경쟁력 있는 개량신약을 처음으로 선보일 수 있습니다. "
            f"BPOM 등재 경험과 e-Katalog JKN 가격 협상을 함께 지원합니다."
        )


def score_all_candidates(
    candidates: list[CandidateProfile] | None = None,
) -> list[AhpResult]:
    pool = candidates if candidates is not None else INDONESIA_CANDIDATES

    results: list[AhpResult] = []
    for profile in pool:
        if not profile.gmp_certified or not profile.mah_capable:
            continue

        cardio_s = _score_cardio(profile)
        market_s = _score_market(profile)
        intl_s = _score_intl(profile)

        psi = (
            cardio_s * WEIGHTS["cardio"]
            + market_s * WEIGHTS["market"]
            + intl_s   * WEIGHTS["intl"]
        )

        strategy: PitchStrategy = (
            "line_extension" if profile.has_ir_cilostazol else "direct"
        )

        results.append(
            AhpResult(
                company_name=profile.company_name,
                rank=0,
                psi_score=round(psi, 3),
                cardio_score=round(cardio_s, 3),
                market_score=round(market_s, 3),
                intl_score=round(intl_s, 3),
                pitch_strategy=strategy,
                email=profile.email,
                phone=profile.phone,
                headquarters=profile.headquarters,
                key_products=profile.cardio_portfolio,
                notes=profile.notes,
                pitch_memo=_build_pitch_memo(profile, strategy),
            )
        )

    # direct 전략 업체 먼저, 같은 전략 내에서는 PSI 내림차순
    results.sort(key=lambda r: (r.pitch_strategy != "direct", -r.psi_score))
    for i, r in enumerate(results, 1):
        r.rank = i

    return results


def ahp_results_to_dicts(results: list[AhpResult]) -> list[dict[str, Any]]:
    return [
        {
            "company_name": r.company_name,
            "rank": r.rank,
            "psi_score": r.psi_score,
            "cardio_score": r.cardio_score,
            "market_score": r.market_score,
            "intl_score": r.intl_score,
            "pitch_strategy": r.pitch_strategy,
            "email": r.email,
            "phone": r.phone,
            "headquarters": r.headquarters,
            "key_products": r.key_products,
            "notes": r.notes,
            "pitch_memo": r.pitch_memo,
        }
        for r in results
    ]
