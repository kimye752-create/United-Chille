"""
cl_outlier_detector.py — 칠레 약가(CLP) IQR 기반 이상치 탐지

Saudi pharma crawler outlier_detector.py 기반 — CLP 필드명 적용.

적용:
  - cl_pricing 테이블 INSERT 직전, 같은 inn_name 그룹 내 신규 가격 이상치 판정
  - 플래그(outlier_flagged=True)만 달고 INSERT는 수행 (삭제 안 함)

의존성: numpy
"""
from __future__ import annotations

from typing import NamedTuple

try:
    import numpy as np
    _NUMPY_OK = True
except ImportError:
    _NUMPY_OK = False


class OutlierResult(NamedTuple):
    flagged: bool
    method: str       # k_statistic | median_ratio | iqr_zero | skip | no_numpy
    k_value: float
    threshold: float
    reason: str


# ── 논문 Table 1 K 임계값 (정규분포) ──────────────────────
K_THRESHOLDS: dict[int, dict[float, float]] = {
    20:   {0.01: 6.341, 0.05: 4.934, 0.10: 4.376},
    30:   {0.01: 5.981, 0.05: 4.872, 0.10: 4.397},
    50:   {0.01: 5.663, 0.05: 4.842, 0.10: 4.468},
    75:   {0.01: 5.549, 0.05: 4.872, 0.10: 4.552},
    100:  {0.01: 5.543, 0.05: 4.914, 0.10: 4.622},
    250:  {0.01: 5.600, 0.05: 5.121, 0.10: 4.889},
    500:  {0.01: 5.732, 0.05: 5.318, 0.10: 5.117},
    1000: {0.01: 5.912, 0.05: 5.540, 0.10: 5.350},
}
_SORTED_N = sorted(K_THRESHOLDS.keys())


def _get_threshold(n: int, alpha: float = 0.05) -> float:
    for k in _SORTED_N:
        if k >= n:
            return K_THRESHOLDS[k][alpha]
    return K_THRESHOLDS[_SORTED_N[-1]][alpha]


def check_outlier(
    existing_prices: list[float],
    new_price: float,
    alpha: float = 0.05,
) -> OutlierResult:
    """신규 CLP 가격이 기존 그룹 대비 이상치인지 판정.

    Args:
        existing_prices: 같은 inn_name 그룹의 기존 raw_price_clp 목록
        new_price: 신규 INSERT할 raw_price_clp
        alpha: 유의수준 (0.05 권장)
    """
    if new_price <= 0:
        return OutlierResult(True, "invalid", 0.0, 0.0,
                             f"price={new_price}: 0 이하 가격")

    if not _NUMPY_OK:
        # numpy 없을 때 단순 median 배수 룰
        if not existing_prices:
            return OutlierResult(False, "no_numpy", 0.0, 0.0, "numpy 없음, 첫 레코드")
        median = sorted(existing_prices)[len(existing_prices) // 2]
        if median > 0:
            ratio = new_price / median
            if ratio > 20.0 or ratio < 0.05:
                return OutlierResult(True, "no_numpy", 0.0, 20.0,
                                     f"median={median:.0f}, ratio={ratio:.1f}x (>20x or <5%)")
        return OutlierResult(False, "no_numpy", 0.0, 0.0, "numpy 없음 — median 룰 통과")

    import numpy as _np  # noqa: PLC0415

    all_prices = _np.array(existing_prices + [new_price], dtype=float)
    n = len(all_prices)

    MEDIAN_RATIO_THRESHOLD = 10.0

    # ── n < 5: median 배수 룰 ──
    if n < 5:
        if len(existing_prices) == 0:
            return OutlierResult(False, "skip", 0.0, 0.0, "첫 레코드, 비교 불가")
        median = float(_np.median(existing_prices))
        if median == 0:
            return OutlierResult(False, "skip", 0.0, 0.0, "median=0")
        ratio = new_price / median
        if ratio > MEDIAN_RATIO_THRESHOLD or ratio < (1.0 / MEDIAN_RATIO_THRESHOLD):
            return OutlierResult(True, "median_ratio", 0.0, MEDIAN_RATIO_THRESHOLD,
                                 f"n<5, median={median:.0f}CLP, ratio={ratio:.1f}x")
        return OutlierResult(False, "median_ratio", 0.0, MEDIAN_RATIO_THRESHOLD,
                             f"n<5, 정상 (ratio={ratio:.1f}x)")

    # ── 5 <= n < 20: 보수적 K + median 이중 검증 ──
    if n < 20:
        median = float(_np.median(existing_prices))
        if median == 0:
            return OutlierResult(False, "skip", 0.0, 0.0, "median=0")
        ratio = new_price / median
        q1 = float(_np.percentile(all_prices, 25))
        q3 = float(_np.percentile(all_prices, 75))
        iqr = q3 - q1
        k_val = (float(_np.max(all_prices)) - float(_np.min(all_prices))) / iqr if iqr > 0 else float("inf")
        thresh = _get_threshold(20, alpha)
        is_extreme = (new_price >= float(_np.max(all_prices)) or new_price <= float(_np.min(all_prices)))
        if (k_val > thresh and is_extreme and
                (ratio > 5.0 or ratio < 0.2)):
            return OutlierResult(True, "k_statistic+median", k_val, thresh,
                                 f"n<20, K={k_val:.2f}>{thresh:.2f}, ratio={ratio:.1f}x")
        return OutlierResult(False, "k_statistic+median", k_val, thresh,
                             f"n<20, K={k_val:.2f}, 정상")

    # ── n >= 20: K 통계량 ──
    q1    = float(_np.percentile(all_prices, 25))
    q3    = float(_np.percentile(all_prices, 75))
    iqr   = q3 - q1
    rng   = float(_np.max(all_prices)) - float(_np.min(all_prices))
    k_val = rng / iqr if iqr > 0 else float("inf")
    thresh = _get_threshold(n, alpha)

    if k_val == float("inf"):
        if len(existing_prices) > 0:
            ref = existing_prices[0]
            if ref != 0 and new_price != ref:
                ratio = new_price / ref
                if ratio >= MEDIAN_RATIO_THRESHOLD or ratio <= (1.0 / MEDIAN_RATIO_THRESHOLD):
                    return OutlierResult(True, "iqr_zero", k_val, 0.0,
                                         f"IQR=0, all={ref:.0f}CLP, new={new_price:.0f}CLP")
        return OutlierResult(False, "iqr_zero", k_val, 0.0, "IQR=0, 유사 가격")

    if k_val > thresh:
        is_extreme = (new_price >= float(_np.max(all_prices)) or new_price <= float(_np.min(all_prices)))
        if is_extreme:
            return OutlierResult(True, "k_statistic", k_val, thresh,
                                 f"K={k_val:.2f}>{thresh:.2f}, new={new_price:.0f}CLP 극단값")
        return OutlierResult(False, "k_statistic", k_val, thresh,
                             f"K={k_val:.2f}>{thresh:.2f}, 극단값 아님")

    # K 미초과 + 극단 median 비율 보조 검사
    if len(existing_prices) > 0:
        median = float(_np.median(existing_prices))
        if median > 0:
            ratio = new_price / median
            is_extreme = (new_price >= float(_np.max(all_prices)) or new_price <= float(_np.min(all_prices)))
            if is_extreme and (ratio >= 100.0 or ratio <= 0.01):
                return OutlierResult(True, "median_extreme", k_val, thresh,
                                     f"K={k_val:.2f}, median={median:.0f}CLP, ratio={ratio:.1f}x")

    return OutlierResult(False, "k_statistic", k_val, thresh,
                         f"K={k_val:.2f}<={thresh:.2f}, 정상")


def flag_record(
    record: dict,
    existing_prices: list[float],
    alpha: float = 0.05,
) -> dict:
    """크롤링 레코드에 이상치 플래그 추가.

    호출 순서:
      record = normalize_record(record)    # inn normalizer
      record = flag_record(record, prices)  # outlier detector ← 여기
      db.insert(record)

    추가 필드: outlier_flagged (bool), anomaly_reason (str|None)
    """
    out   = dict(record)
    price = out.get("raw_price_clp") or out.get("price_local") or out.get("price_clp")

    if price is None:
        out["outlier_flagged"] = False
        out["anomaly_reason"]  = None
        return out

    try:
        price_float = float(price)
    except (TypeError, ValueError):
        out["outlier_flagged"] = True
        out["anomaly_reason"]  = f"price not numeric: {price}"
        return out

    result = check_outlier(existing_prices, price_float, alpha)
    out["outlier_flagged"] = result.flagged
    out["anomaly_reason"]  = result.reason if result.flagged else None
    return out


def scan_group(
    prices: list[float],
    alpha: float = 0.05,
    max_iter: int = 5,
) -> list[tuple[float, OutlierResult]]:
    """기존 그룹 전체 스캔 → 이상치 목록 (배치 점검용)."""
    if not _NUMPY_OK or len(prices) < 5:
        return []

    import numpy as _np  # noqa: PLC0415

    data    = list(prices)
    outliers: list[tuple[float, OutlierResult]] = []

    for _ in range(max_iter):
        if len(data) < 5:
            break
        arr    = _np.array(data, dtype=float)
        q1     = float(_np.percentile(arr, 25))
        q3     = float(_np.percentile(arr, 75))
        iqr    = q3 - q1
        k_val  = (float(_np.max(arr)) - float(_np.min(arr))) / iqr if iqr > 0 else float("inf")
        thresh = _get_threshold(len(data), alpha) if len(data) >= 20 else _get_threshold(20, alpha)

        if k_val == float("inf") or k_val <= thresh:
            break

        median  = float(_np.median(arr))
        max_val = float(_np.max(arr))
        min_val = float(_np.min(arr))
        removed = max_val if abs(max_val - median) >= abs(min_val - median) else min_val

        result = OutlierResult(True, "k_statistic_scan", k_val, thresh,
                               f"scan: K={k_val:.2f}>{thresh:.2f}, removed={removed:.0f}CLP")
        outliers.append((removed, result))
        data = [x for x in data if x != removed]

    return outliers
