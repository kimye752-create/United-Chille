"""호주 PBS API v3로 참고 가격(DPMQ)을 구조화 추출.

PBS Management API v3 공개 키 (Unregistered Public Users):
  Subscription-Key: 2384af7c667342ceb5a736fe29f1dc6b

환경변수 PBS_SUBSCRIPTION_KEY 로 덮어쓸 수 있습니다.
싱가포르 약가가 아닌, HTA·스케줄 참고용 방법론적 추산 라벨과 함께 사용합니다.
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import quote

import httpx

_BASE: Final[str] = "https://data-api.health.gov.au/pbs/api/v3"
_PUBLIC_KEY: Final[str] = "2384af7c667342ceb5a736fe29f1dc6b"
_MAX_FALLBACK_PAGES = 5  # rate limit(5/min) 고려해 제한

PBS_METHODOLOGY_LABEL_KO: Final[str] = "(PBS, 방법론적 추산)"
_DEFAULT_AUD_SGD: Final[float] = 0.87
# API 호출 간 대기(초). 환경변수 PBS_API_SLEEP_SEC 으로 조정 가능.
_API_SLEEP_SEC: float = float(os.environ.get("PBS_API_SLEEP_SEC", "3"))

# 전역 Lock: asyncio.gather로 병렬 실행될 때 PBS API 호출을 직렬화
# rate limit = 5회/분 → 12초 간격이면 안전
_api_lock = threading.Lock()
_last_api_call_ts: float = 0.0

# schedule_code 세션 캐시 (매 호출마다 /schedules 조회 방지)
_cached_schedule: str | None = None
_cached_schedule_ts: float = 0.0
_SCHEDULE_CACHE_TTL: float = 3600.0  # 1시간

# 호주 PBS INN 동의어 매핑 (검색어 → PBS 등재명)
# 일반 INN과 호주 PBS 표기가 다른 경우
_PBS_INN_SYNONYMS: dict[str, list[str]] = {
    "hydroxyurea": ["hydroxycarbamide"],
    "hydroxycarbamide": ["hydroxyurea"],
    "fluticasone": ["fluticasone propionate"],
    "adrenaline": ["epinephrine"],
    "epinephrine": ["adrenaline"],
    "paracetamol": ["acetaminophen"],
    "acetaminophen": ["paracetamol"],
    "salbutamol": ["albuterol"],
    "albuterol": ["salbutamol"],
    "frusemide": ["furosemide"],
    "furosemide": ["frusemide"],
}


def _subscription_key() -> str:
    return os.environ.get("PBS_SUBSCRIPTION_KEY", _PUBLIC_KEY)


def _api_headers() -> dict[str, str]:
    return {"Subscription-Key": _subscription_key()}


def _pbs_public_url(pbs_code: str | None = None) -> str:
    if pbs_code:
        return f"https://www.pbs.gov.au/browse/medicine?search={quote(str(pbs_code))}"
    return "https://www.pbs.gov.au/browse/medicine"


# ---------------------------------------------------------------------------
# 결과 dataclass (기존 인터페이스 유지 + API 필드 확장)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PbsPricingResult:
    """PBS 스케줄에서 추출한 참고 가격(방법론적 추산)."""

    product_id: str
    search_terms_tried: tuple[str, ...] = ()
    search_hit: bool = False
    listing_url: str = ""
    schedule_drug_name: str = ""
    pack_description: str = ""
    dpmq_aud: float | None = None
    dpmq_label_en: str = "DPMQ (dispensed price for maximum quantity)"
    aud_to_sgd_rate: float | None = None
    dpmq_sgd_hint: float | None = None
    methodology_label_ko: str = PBS_METHODOLOGY_LABEL_KO
    fetch_error: str = ""
    # API 전용 확장 필드
    pbs_item_code: str | None = None
    pbs_determined_price: float | None = None
    pbs_pack_size: Any = None
    pbs_benefit_type: str | None = None
    pbs_program_code: str | None = None
    pbs_brand_name: str | None = None
    pbs_innovator: str | None = None
    pbs_first_listed_date: Any = None
    pbs_repeats: Any = None
    pbs_restriction: bool = False
    pbs_total_brands: int = 0
    pbs_brands: tuple[dict[str, Any], ...] = ()

    def to_prompt_block(self) -> str:
        lines: list[str] = [
            "### PBS 참고 가격 (호주 공개 스케줄, 방법론적 추산)",
            f"- 라벨: {self.methodology_label_ko}",
            "- 주의: 싱가포르 약가·ERP 직접 벤치마크가 아님. 국제 HTA·스케줄 참고용.",
        ]
        if self.fetch_error:
            lines.append(f"- 수집 상태: {self.fetch_error}")
        if self.listing_url:
            lines.append(f"- PBS 페이지: {self.listing_url}")
        if self.pbs_item_code:
            lines.append(f"- PBS 품목 코드: {self.pbs_item_code}")
        if self.schedule_drug_name:
            lines.append(f"- 스케줄 표기: {self.schedule_drug_name}")
        if self.pack_description:
            lines.append(f"- 제형·규격: {self.pack_description}")
        if self.dpmq_aud is not None:
            lines.append(f"- DPMQ (AUD): ${self.dpmq_aud:.2f}")
        if self.pbs_determined_price is not None:
            lines.append(f"- Determined Price (AUD): ${self.pbs_determined_price:.2f}")
        if self.dpmq_sgd_hint is not None and self.aud_to_sgd_rate is not None:
            lines.append(
                f"- 참고 SGD 환산(대략, 환율 {self.aud_to_sgd_rate:.4f}): "
                f"약 SGD {self.dpmq_sgd_hint:.2f}"
            )
        if self.pbs_brand_name:
            lines.append(f"- 브랜드명: {self.pbs_brand_name}")
        if self.pbs_total_brands > 1:
            lines.append(f"- 등재 브랜드 수: {self.pbs_total_brands}")
        if self.search_terms_tried:
            lines.append(f"- 시도 검색어: {', '.join(self.search_terms_tried)}")
        return "\n".join(lines)

    def to_flat_dict(self) -> dict[str, Any]:
        return {
            "pbs_listing_url": self.listing_url or None,
            "pbs_schedule_drug_name": self.schedule_drug_name or None,
            "pbs_pack_description": self.pack_description or None,
            "pbs_dpmq_aud": self.dpmq_aud,
            "pbs_dpmq_label_en": self.dpmq_label_en,
            "pbs_aud_to_sgd_rate": self.aud_to_sgd_rate,
            "pbs_dpmq_sgd_hint": self.dpmq_sgd_hint,
            "pbs_methodology_label_ko": self.methodology_label_ko,
            "pbs_search_terms_tried": ", ".join(self.search_terms_tried) if self.search_terms_tried else None,
            "pbs_search_hit": self.search_hit,
            "pbs_fetch_error": self.fetch_error or None,
            "pbs_item_code": self.pbs_item_code,
            "pbs_determined_price": self.pbs_determined_price,
            "pbs_pack_size": self.pbs_pack_size,
            "pbs_benefit_type": self.pbs_benefit_type,
            "pbs_brand_name": self.pbs_brand_name,
            "pbs_innovator": self.pbs_innovator,
            "pbs_restriction": self.pbs_restriction,
            "pbs_total_brands": self.pbs_total_brands,
        }


# ---------------------------------------------------------------------------
# API 유틸리티
# ---------------------------------------------------------------------------

def _api_sleep() -> None:
    """Lock 획득 후 마지막 호출로부터 최소 _API_SLEEP_SEC 보장."""
    # Lock 없이 단독 호출 시에도 동작하도록 fallback
    if _API_SLEEP_SEC > 0:
        time.sleep(_API_SLEEP_SEC)


def _price_from_row(row: dict[str, Any]) -> float | None:
    for key in ("claimed_price", "determined_price"):
        v = row.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    return None


def _restriction_from_row(row: dict[str, Any]) -> str | None:
    for key in ("restriction_text", "note_text", "caution_text"):
        v = row.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _row_matches_ingredient(row: dict[str, Any], needles: list[str]) -> bool:
    """drug_name / li_drug_name / generic_name / product_name 에 부분일치."""
    needles = [n.strip().lower() for n in needles if n and str(n).strip()]
    if not needles:
        return False
    parts: list[str] = []
    for key in ("drug_name", "li_drug_name", "generic_name", "product_name"):
        v = row.get(key)
        if isinstance(v, str):
            parts.append(v.lower())
    blob = " ".join(parts)
    return any(n in blob for n in needles)


def _fetch_schedule_code() -> str | None:
    """최신 schedule_code 반환 (세션 내 1시간 캐시, Lock 포함)."""
    global _cached_schedule, _cached_schedule_ts
    now = time.monotonic()
    if _cached_schedule and (now - _cached_schedule_ts) < _SCHEDULE_CACHE_TTL:
        return _cached_schedule
    r = _api_get(f"{_BASE}/schedules", {})
    if r is None or r.status_code != 200 or not r.content:
        return _cached_schedule
    try:
        data = r.json().get("data")
    except Exception:
        return _cached_schedule
    if not isinstance(data, list) or not data:
        return _cached_schedule
    code = data[0].get("schedule_code")
    if code is not None:
        _cached_schedule = str(code)
        _cached_schedule_ts = time.monotonic()
    return _cached_schedule


def _expand_synonyms(term: str) -> list[str]:
    """PBS INN 동의어 확장: 원문 + 호주 PBS 표기."""
    t = term.strip().lower()
    extras = _PBS_INN_SYNONYMS.get(t, [])
    seen: set[str] = set()
    out: list[str] = []
    for s in [t] + extras:
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _build_needles(inn: str) -> list[str]:
    """INN 문자열 → 부분일치 검색 후보 리스트.

    복합제 구분자 +, / 모두 처리 + PBS INN 동의어 확장.
    예) "hydroxyurea" → ["hydroxyurea", "hydroxycarbamide"]
    예) "cilostazol/rosuvastatin" → ["cilostazol", "rosuvastatin", ...]
    """
    raw = (inn or "").strip().lower()
    if not raw:
        return []
    parts = [p.strip() for p in re.split(r"[+/]", raw) if p.strip()]
    needles: list[str] = []
    for p in parts:
        needles.extend(_expand_synonyms(p))
    if raw not in needles:
        needles.append(raw)
    # 중복 제거, 순서 유지
    seen: set[str] = set()
    out: list[str] = []
    for n in needles:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _search_terms_for_meta(meta: dict[str, str]) -> list[str]:
    """meta(product_id, inn, trade_name)에서 API drug_name 검색어 후보.

    복합제(+, / 구분자) → 성분 개별로 분리해 각각 시도.
    """
    inn = str(meta.get("inn", "") or "").strip()
    trade = str(meta.get("trade_name", "") or "").strip()

    terms: list[str] = []
    if inn:
        # 복합제 성분 개별 추가 + 동의어 확장
        parts = [p.strip().lower() for p in re.split(r"[+/]", inn) if p.strip()]
        for p in parts:
            terms.extend(_expand_synonyms(p))
        # 전체 INN도 추가
        full = re.sub(r"\s*[+/]\s*", " ", inn).strip().lower()
        if full not in terms:
            terms.append(full)
    if trade and trade.lower() not in terms:
        terms.append(trade.lower())

    # 중복 제거
    seen: set[str] = set()
    out: list[str] = []
    for t in terms:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _api_get(url: str, params: dict[str, Any]) -> httpx.Response | None:
    """API GET: 전역 Lock으로 직렬화 + 호출 간격 보장 + 429 재시도."""
    global _last_api_call_ts
    with _api_lock:
        # 마지막 호출 이후 _API_SLEEP_SEC 보장
        elapsed = time.monotonic() - _last_api_call_ts
        wait = _API_SLEEP_SEC - elapsed
        if wait > 0:
            time.sleep(wait)
        try:
            r = httpx.get(url, params=params, headers=_api_headers(), timeout=15)
            _last_api_call_ts = time.monotonic()
            if r.status_code == 429:
                retry_after = int(r.headers.get("retry-after", "60"))
                time.sleep(max(retry_after, 60))
                r = httpx.get(url, params=params, headers=_api_headers(), timeout=15)
                _last_api_call_ts = time.monotonic()
            return r
        except Exception:
            _last_api_call_ts = time.monotonic()
            return None


def _query_items_primary(
    schedule: str, drug_name: str, needles: list[str]
) -> tuple[list[dict[str, Any]], bool]:
    """drug_name 파라미터로 API 조회. 반환: (매칭 행 리스트, PBS미등재여부).

    204 → PBS 미등재 확정(not_listed=True), fallback 불필요.
    """
    r = _api_get(
        f"{_BASE}/items",
        {"schedule_code": schedule, "drug_name": drug_name, "page": 1, "limit": 10},
    )
    if r is None:
        return [], False
    if r.status_code == 204:
        return [], True  # PBS 미등재
    if r.status_code != 200 or not r.content:
        return [], False
    try:
        rows = r.json().get("data")
    except Exception:
        return [], False
    matched = [row for row in (rows or []) if isinstance(row, dict) and _row_matches_ingredient(row, needles)]
    return matched, False


def _query_items_fallback(
    schedule: str, needles: list[str]
) -> list[dict[str, Any]]:
    """페이지 순회 fallback (limit=100, 최대 _MAX_FALLBACK_PAGES 페이지)."""
    matched: list[dict[str, Any]] = []
    for page in range(1, _MAX_FALLBACK_PAGES + 1):
        r = _api_get(
            f"{_BASE}/items",
            {"schedule_code": schedule, "page": page, "limit": 100},
        )
        if r is None or r.status_code != 200 or not r.content:
            break
        try:
            payload = r.json()
        except Exception:
            break
        rows = payload.get("data")
        if not isinstance(rows, list) or not rows:
            break
        for row in rows:
            if isinstance(row, dict) and _row_matches_ingredient(row, needles):
                matched.append(row)
        meta_block = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
        total = meta_block.get("total_records")
        if isinstance(total, int) and page * 100 >= total:
            break
    return matched


def _select_best_row(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """여러 브랜드 중 1행 선택: innovator=Y 우선, 없으면 최저가 제네릭."""
    originals = [r for r in rows if r.get("innovator_indicator") == "Y"]
    if originals:
        return originals[0]
    generics = [r for r in rows if r.get("innovator_indicator") == "N"]
    pool = generics if generics else rows

    def price_key(row: dict[str, Any]) -> float:
        v = _price_from_row(row)
        return v if v is not None else float("inf")

    return min(pool, key=price_key)


def _resolve_aud_sgd_rate() -> tuple[float, str]:
    raw = os.environ.get("PBS_AUD_TO_SGD", "").strip()
    if raw:
        try:
            return float(raw), "env PBS_AUD_TO_SGD"
        except ValueError:
            pass
    try:
        with httpx.Client(timeout=8.0) as client:
            r = client.get("https://api.frankfurter.app/latest?from=AUD&to=SGD")
            r.raise_for_status()
            rate = float(r.json()["rates"]["SGD"])
            return rate, "api.frankfurter.app"
    except Exception:
        return _DEFAULT_AUD_SGD, f"default({_DEFAULT_AUD_SGD})"


# ---------------------------------------------------------------------------
# 메인 공개 함수
# ---------------------------------------------------------------------------

def fetch_pbs_pricing_sync(meta: dict[str, str]) -> PbsPricingResult:
    """PBS API v3로 성분 검색 → DPMQ 추출. 동기."""
    pid = str(meta.get("product_id", "") or "")

    if os.environ.get("PBS_FETCH", "").strip().lower() in ("0", "false", "skip", "off"):
        return PbsPricingResult(
            product_id=pid,
            fetch_error="PBS_FETCH 비활성(테스트/오프라인)",
        )

    terms = _search_terms_for_meta(meta)
    inn = str(meta.get("inn", "") or "").strip()
    needles = _build_needles(inn) if inn else []
    if not needles and terms:
        needles = list(terms)

    if not terms:
        return PbsPricingResult(product_id=pid, fetch_error="검색어 없음(INN/trade_name 미입력)")

    # 1. 최신 schedule_code 조회
    schedule = _fetch_schedule_code()
    if not schedule:
        return PbsPricingResult(product_id=pid, search_terms_tried=tuple(terms), fetch_error="schedule_code 조회 실패")

    # 2. drug_name 파라미터 직접 조회 (각 검색어 순차 시도)
    matched_rows: list[dict[str, Any]] = []
    not_listed = False
    for term in terms:
        term_needles = _build_needles(term) if term else needles
        rows, term_not_listed = _query_items_primary(schedule, term, term_needles or [term])
        if rows:
            matched_rows = rows
            break
        if term_not_listed:
            not_listed = True  # 이 검색어는 PBS 미등재 확정, 다음 성분 시도

    # 3. fallback: 페이지 순회 (모든 검색어가 204 미등재면 스킵)
    if not matched_rows and needles and not not_listed:
        matched_rows = _query_items_fallback(schedule, needles)

    if not matched_rows:
        reason = "PBS 미등재(204)" if not_listed else "PBS 매칭 품목 없음"
        return PbsPricingResult(
            product_id=pid,
            search_terms_tried=tuple(terms),
            fetch_error=reason,
        )

    best = _select_best_row(matched_rows)

    # 브랜드 목록
    brand_set: dict[str, dict[str, Any]] = {}
    for row in matched_rows:
        bn = row.get("brand_name") or ""
        if bn and bn not in brand_set:
            brand_set[bn] = {
                "brand_name": bn,
                "pbs_price_aud": _price_from_row(row),
                "pbs_innovator": row.get("innovator_indicator"),
                "pbs_item_code": str(row["pbs_code"]) if row.get("pbs_code") else None,
            }
    pbs_brands = tuple(brand_set.values())

    raw_code = best.get("pbs_code")
    item_code = str(raw_code) if raw_code is not None else None
    dpmq = _price_from_row(best)
    determined = float(best["determined_price"]) if best.get("determined_price") else None

    rate, _rate_src = _resolve_aud_sgd_rate()
    sgd_hint = round(dpmq * rate, 2) if dpmq is not None else None

    return PbsPricingResult(
        product_id=pid,
        search_terms_tried=tuple(terms),
        search_hit=True,
        listing_url=_pbs_public_url(item_code),
        schedule_drug_name=str(best.get("drug_name") or best.get("li_drug_name") or ""),
        pack_description=str(best.get("pack_size") or ""),
        dpmq_aud=dpmq,
        aud_to_sgd_rate=rate,
        dpmq_sgd_hint=sgd_hint,
        fetch_error="" if dpmq is not None else "DPMQ(claimed_price) 없음",
        pbs_item_code=item_code,
        pbs_determined_price=determined,
        pbs_pack_size=best.get("pack_size"),
        pbs_benefit_type=best.get("benefit_type_code"),
        pbs_program_code=best.get("program_code"),
        pbs_brand_name=best.get("brand_name"),
        pbs_innovator=best.get("innovator_indicator"),
        pbs_first_listed_date=best.get("first_listed_date"),
        pbs_repeats=best.get("number_of_repeats"),
        pbs_restriction=best.get("benefit_type_code") in ("R", "S"),
        pbs_total_brands=len(brand_set),
        pbs_brands=pbs_brands,
    )


async def fetch_pbs_pricing(meta: dict[str, str]) -> PbsPricingResult:
    """비동기 래퍼(네트워크는 스레드에서 실행)."""
    return await asyncio.to_thread(fetch_pbs_pricing_sync, meta)


# ---------------------------------------------------------------------------
# 하위 호환: 테스트에서 직접 사용하는 HTML 파싱 함수 유지
# ---------------------------------------------------------------------------

_DPMQ_AFTER_REPEATS_RE = re.compile(
    r'<td class="align-top" rowspan="\d+">(\d+)</td>\s*'
    r'<td class="align-top" rowspan="\d+">(\d+)</td>\s*'
    r'<td class="align-top" rowspan="\d+">(\d+)</td>\s*'
    r'<td class="align-top" rowspan="\d+">\$([0-9]+(?:\.[0-9]+)?)</td>',
    re.IGNORECASE | re.DOTALL,
)
_FORM_STRENGTH_RE = re.compile(
    r'<span class="form-strength">([^<]{1,500})</span>',
    re.IGNORECASE,
)
_DRUG_H1_RE = re.compile(
    r'<h1 class="drug-name">([^<]{1,200})</h1>',
    re.IGNORECASE,
)


def _first_medicine_item_block(html: str) -> str | None:
    idx = html.lower().find('id="medicine-item"')
    if idx < 0:
        return None
    end = html.find("</table>", idx)
    if end < 0:
        return None
    return html[idx : end + len("</table>")]


def _parse_item_page(html: str) -> tuple[float | None, str, str]:
    """HTML 파싱(테스트·레거시 호환용). 실제 경로는 API v3 사용."""
    block = _first_medicine_item_block(html) or html
    m = _DPMQ_AFTER_REPEATS_RE.search(block)
    dpmq: float | None = None
    if m:
        try:
            dpmq = float(m.group(4))
        except ValueError:
            pass
    fs = _FORM_STRENGTH_RE.search(block)
    pack = fs.group(1).strip() if fs else ""
    h1 = _DRUG_H1_RE.search(html)
    drug = h1.group(1).strip() if h1 else ""
    return dpmq, drug, pack
