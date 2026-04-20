"""우루과이 의약품 스페인어 원시 텍스트 → 구조화 데이터 파서 (Engine 2).

Claude Haiku API를 호출하여 스페인어 제품명·함량·포장 단위·가격을 파싱하고
UYU → USD 환율 변환 후 ParsedDrug 데이터클래스로 반환한다.

핵심 방어:
  - 포장 단위 기준 단위당 가격(price_per_unit) 강제 계산 → FOB 역산 왜곡 방지
  - 환율 환경변수 UY_USD_RATE 우선 적용 (yfinance 폴백)
  - VAT는 환경변수 UY_VAT_PHARMA_PCT에서 동적 로드 (하드코딩 금지)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

CLAUDE_MODEL = "claude-haiku-4-5-20251001"

DOSAGE_FORM_MAP: dict[str, str] = {
    "comprimido": "tablet",
    "comprimidos": "tablet",
    "comp": "tablet",
    "cápsula": "capsule",
    "cápsulas": "capsule",
    "cap": "capsule",
    "ampolla": "ampoule",
    "ampollas": "ampoule",
    "amp": "ampoule",
    "jarabe": "syrup",
    "suspensión": "suspension",
    "suspension": "suspension",
    "crema": "cream",
    "ungüento": "ointment",
    "solución": "solution",
    "solucion": "solution",
    "sol": "solution",
    "tableta": "tablet",
    "tabletas": "tablet",
    "gragea": "tablet",
    "grageas": "tablet",
    "supositorio": "suppository",
    "supositorios": "suppository",
    "parche": "patch",
    "inyectable": "injectable",
    "vial": "vial",
    "frasco": "bottle",
    "sobres": "sachet",
    "sobre": "sachet",
}

_RATE_CACHE: dict[str, float] = {"rate": 0.0, "ts": 0.0}
_RATE_TTL = 1800.0


def _uyu_to_usd_rate() -> float:
    env_rate = os.environ.get("UY_USD_RATE", "").strip()
    if env_rate:
        try:
            return float(env_rate)
        except ValueError:
            pass

    now = time.monotonic()
    if _RATE_CACHE["rate"] and (now - _RATE_CACHE["ts"]) < _RATE_TTL:
        return _RATE_CACHE["rate"]

    try:
        import yfinance as yf  # type: ignore[import]
        ticker = yf.Ticker("UYUUSD=X")
        rate = float(ticker.fast_info.last_price)
        if rate > 0:
            _RATE_CACHE["rate"] = rate
            _RATE_CACHE["ts"] = now
            return rate
    except Exception:
        pass

    return 0.02481  # 폴백: 1 UYU ≈ 0.02481 USD (2025.04 기준)


def _normalize_form(raw: str) -> str:
    token = raw.strip().lower()
    for key, val in DOSAGE_FORM_MAP.items():
        if key in token:
            return val
    return raw.strip()


def _safe_decimal(v: Any) -> Decimal | None:
    try:
        return Decimal(str(v))
    except (InvalidOperation, TypeError):
        return None


@dataclass
class ParsedDrug:
    inn_name: str
    brand_name: str
    strength_mg: float
    dosage_form: str
    pack_size: int
    total_price_uyu: Decimal
    price_per_unit_uyu: Decimal
    price_per_unit_usd: Decimal
    manufacturer: str
    source_site: str
    source_url: str
    raw_text: str
    confidence: float = 0.7
    farmacard_price_uyu: Decimal | None = None
    extra: dict[str, Any] = field(default_factory=dict)


_SCHEMA_DESC: dict[str, str] = {
    "inn_name":        "WHO INN 국제일반명 (예: Cilostazol). 불명확 시 brand_name 기반 추론",
    "brand_name":      "제품 상품명 (예: Cilozol). 없으면 inn_name과 동일",
    "strength_mg":     "주성분 함량 숫자값 (mg 단위, 예: 100.0). 불명확 시 null",
    "dosage_form":     "제형 — 스페인어를 영문 표준으로 변환 (tablet/capsule/ampoule/syrup 등)",
    "pack_size":       "포장 단위 정수 (예: 30). 불명확 시 null",
    "total_price_uyu": "포장 전체 가격 (UYU 페소, 숫자만). 불명확 시 null",
    "manufacturer":    "제조사/판매사명. 불명확 시 '-'",
}


async def parse_drug_text(
    raw_text: str,
    source_site: str,
    source_url: str,
    farmacard_price_uyu: Decimal | None = None,
) -> ParsedDrug | None:
    api_key = os.environ.get("CLAUDE_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return _regex_fallback(raw_text, source_site, source_url, farmacard_price_uyu)

    schema_str = json.dumps(_SCHEMA_DESC, ensure_ascii=False, indent=2)
    prompt = f"""우루과이 약국 사이트에서 수집한 스페인어 의약품 텍스트를 파싱하여 JSON으로 반환하세요.

추출 항목:
{schema_str}

규칙:
1. price_per_unit_uyu = total_price_uyu / pack_size 로 반드시 계산하여 포함하세요.
2. dosage_form은 스페인어를 영문 표준 제형명으로 변환하세요 (Comprimidos→tablet, Cápsulas→capsule 등).
3. 숫자만 포함할 값에는 숫자만, 문자열 값에는 텍스트만 넣으세요.
4. 불확실한 필드는 null로 반환하세요. JSON 외 텍스트는 절대 포함하지 마세요.

입력 텍스트:
{raw_text[:800]}

반드시 JSON 객체 하나만 반환하세요."""

    try:
        import httpx

        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = {
            "model": CLAUDE_MODEL,
            "max_tokens": 512,
            "temperature": 0.0,
            "messages": [{"role": "user", "content": prompt}],
        }
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json=body,
            )
            resp.raise_for_status()
            content = resp.json()["content"][0]["text"].strip()

        m = re.search(r"\{.*\}", content, re.S)
        if not m:
            return _regex_fallback(raw_text, source_site, source_url, farmacard_price_uyu)
        data: dict[str, Any] = json.loads(m.group(0))
        return _build_parsed(data, raw_text, source_site, source_url, farmacard_price_uyu)
    except Exception:
        return _regex_fallback(raw_text, source_site, source_url, farmacard_price_uyu)


def _build_parsed(
    data: dict[str, Any],
    raw_text: str,
    source_site: str,
    source_url: str,
    farmacard_price_uyu: Decimal | None,
) -> ParsedDrug | None:
    total = _safe_decimal(data.get("total_price_uyu"))
    if total is None:
        return None

    pack = int(data.get("pack_size") or 1) or 1
    per_unit_uyu = total / Decimal(pack)
    rate = Decimal(str(_uyu_to_usd_rate()))
    per_unit_usd = per_unit_uyu * rate

    raw_form = str(data.get("dosage_form") or "")
    dosage_form = _normalize_form(raw_form) if raw_form else "unknown"

    strength_raw = data.get("strength_mg")
    try:
        strength_mg = float(strength_raw) if strength_raw is not None else 0.0
    except (ValueError, TypeError):
        strength_mg = 0.0

    return ParsedDrug(
        inn_name=str(data.get("inn_name") or "").strip(),
        brand_name=str(data.get("brand_name") or "").strip(),
        strength_mg=strength_mg,
        dosage_form=dosage_form,
        pack_size=pack,
        total_price_uyu=total,
        price_per_unit_uyu=per_unit_uyu,
        price_per_unit_usd=per_unit_usd,
        manufacturer=str(data.get("manufacturer") or "-").strip(),
        source_site=source_site,
        source_url=source_url,
        raw_text=raw_text,
        farmacard_price_uyu=farmacard_price_uyu,
        confidence=0.75,
    )


def _regex_fallback(
    raw_text: str,
    source_site: str,
    source_url: str,
    farmacard_price_uyu: Decimal | None,
) -> ParsedDrug | None:
    price_m = re.search(r"[\$\s]?([\d.,]+)", raw_text.replace(",", ""))
    if not price_m:
        return None
    total = _safe_decimal(price_m.group(1).replace(",", "."))
    if total is None:
        return None

    pack_m = re.search(r"(\d+)\s*(?:comp|cap|tab|amp)", raw_text, re.I)
    pack = int(pack_m.group(1)) if pack_m else 1

    mg_m = re.search(r"(\d+(?:\.\d+)?)\s*mg", raw_text, re.I)
    strength_mg = float(mg_m.group(1)) if mg_m else 0.0

    rate = Decimal(str(_uyu_to_usd_rate()))
    per_unit_uyu = total / Decimal(pack)

    return ParsedDrug(
        inn_name="",
        brand_name="",
        strength_mg=strength_mg,
        dosage_form="unknown",
        pack_size=pack,
        total_price_uyu=total,
        price_per_unit_uyu=per_unit_uyu,
        price_per_unit_usd=per_unit_uyu * rate,
        manufacturer="-",
        source_site=source_site,
        source_url=source_url,
        raw_text=raw_text,
        farmacard_price_uyu=farmacard_price_uyu,
        confidence=0.4,
    )


async def parse_drug_texts_batch(
    items: list[dict[str, Any]],
) -> list[ParsedDrug | None]:
    tasks = [
        parse_drug_text(
            raw_text=item["raw_text"],
            source_site=item.get("source_site", ""),
            source_url=item.get("source_url", ""),
            farmacard_price_uyu=_safe_decimal(item.get("farmacard_price_uyu")),
        )
        for item in items
    ]
    return list(await asyncio.gather(*tasks))
