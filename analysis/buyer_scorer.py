"""바이어 평가 점수 계산 + 랭킹.

점수는 내부 정렬용으로만 사용 — 외부(프론트/PDF)에 노출하지 않음.
criteria=None: 성분 매칭 우선 + enrichment 완성도 순 정렬
criteria 있음: 선택 항목 점수 합산 → 성분 매칭 tie-break
전체 candidate 풀(20개) 대상으로 상위 top_n(10) 선택.
"""

from __future__ import annotations

import re
from typing import Any

SCORE_CRITERIA = [
    {"key": "기업규모",     "label": "기업 규모"},
    {"key": "유통실적",     "label": "유통 실적"},
    {"key": "GMP보유",      "label": "GMP 보유"},
    {"key": "공공채널",     "label": "공공 채널"},
    {"key": "민간채널",     "label": "민간 채널"},
    {"key": "파트너적합성", "label": "파트너 적합성"},
    {"key": "한국거래",     "label": "한국 거래 경험"},
    {"key": "MAH가능",      "label": "MAH 가능"},
]


def _bool_score(val: Any) -> int:
    return 100 if val is True else 0


def _revenue_score(revenue: str) -> int:
    if not revenue or revenue == "-":
        return 0
    r = revenue.upper()
    for marker, score in [
        ("$10B", 100), ("$5B", 100), ("$2B", 95), ("$1B", 90),
        ("$500M", 80), ("$300M", 75), ("$200M", 70),
        ("$100M", 60), ("$50M", 50), ("$20M", 40), ("$10M", 30),
    ]:
        if marker in r:
            return score
    return 20 if len(r) > 1 else 0


def _employee_score(employees: str) -> int:
    if not employees or employees == "-":
        return 0
    nums = [int(x.replace(",", "")) for x in re.findall(r"[\d,]+", employees)]
    if not nums:
        return 20
    n = max(nums)
    if n >= 10000: return 100
    if n >= 5000:  return 90
    if n >= 1000:  return 75
    if n >= 500:   return 60
    if n >= 100:   return 40
    return 20


def _korea_score(val: Any) -> int:
    if not val or val in ("-", "없음", "None"):
        return 0
    s = str(val)
    try:
        n = int(re.search(r"\d+", s).group())
        if n >= 5: return 100
        if n >= 3: return 80
        if n >= 1: return 60
    except Exception:
        pass
    if "있음" in s or "경험" in s:
        return 50
    return 0


def _enrichment_completeness(company: dict[str, Any]) -> int:
    """enrichment 완성도 점수 (정렬 보조용)."""
    e = company.get("enriched", {})
    score = 0
    if e.get("company_overview_kr", "-") not in ("-", ""):
        score += 30
    if e.get("recommendation_reason", "-") not in ("-", ""):
        score += 30
    if company.get("website", "-") != "-":
        score += 20
    if e.get("territories"):
        score += 10
    if e.get("revenue", "-") != "-":
        score += 10
    return score


def compute_scores(company: dict[str, Any]) -> dict[str, int]:
    """기업 1개 → 항목별 점수 dict (내부 정렬용)."""
    e = company.get("enriched", {})

    rev_s = _revenue_score(str(e.get("revenue", "-")))
    emp_s = _employee_score(str(e.get("employees", "-")))
    size_score = (rev_s + emp_s) // 2 if (rev_s or emp_s) else 0

    imp_s = _bool_score(e.get("import_history"))
    pro_s = _bool_score(e.get("procurement_history"))
    dist_score = (imp_s + pro_s) // 2

    partner_s = max(
        _bool_score(e.get("mah_capable")),
        _korea_score(e.get("korea_experience")),
    )

    return {
        "기업규모":     size_score,
        "유통실적":     dist_score,
        "GMP보유":      _bool_score(e.get("has_gmp")),
        "공공채널":     _bool_score(e.get("public_channel")),
        "민간채널":     _bool_score(e.get("private_channel")),
        "파트너적합성": partner_s,
        "한국거래":     _korea_score(e.get("korea_experience")),
        "MAH가능":      _bool_score(e.get("mah_capable")),
        "타깃국가진출": _bool_score(e.get("has_target_country_presence")),
    }


def rank_companies(
    all_candidates: list[dict[str, Any]],
    active_criteria: list[str] | None = None,
    top_n: int = 10,
) -> list[dict[str, Any]]:
    """
    전체 후보 풀(all_candidates)에서 criteria 기준으로 상위 top_n 선택.
    composite_score는 내부 정렬용 — 반환 딕셔너리에서 제거.
    """
    scored: list[dict[str, Any]] = []
    for c in all_candidates:
        scores = compute_scores(c)
        ingredient_match = c.get("ingredient_match", False)
        completeness    = _enrichment_completeness(c)

        target_presence = scores.get("타깃국가진출", 0)

        if active_criteria:
            # criteria 선택 시: 선택 항목 점수 합산
            criteria_avg = sum(scores.get(k, 0) for k in active_criteria) / len(active_criteria)
            # tie-break: 타깃국가 진출 여부 → 성분 매칭 → 완성도
            sort_key = (criteria_avg, target_presence, 10 if ingredient_match else 0, completeness)
        else:
            # criteria 없음: 타깃국가 진출 → 성분 매칭 → enrichment 완성도 순
            sort_key = (target_presence, 100 if ingredient_match else 0, completeness, 0)

        scored.append({
            **c,
            "scores": scores,
            "_sort_key": sort_key,
        })

    scored.sort(key=lambda x: x["_sort_key"], reverse=True)

    result = []
    for item in scored[:top_n]:
        item.pop("_sort_key", None)
        # composite_score 제거 (외부 노출 금지)
        item.pop("composite_score", None)
        result.append(item)

    return result
