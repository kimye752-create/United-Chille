"""
cl_antibot.py — 칠레 크롤러용 Anti-bot 탐지 + UA 회전
Saudi pharma crawler antibot.py 기반 — Chile 사이트에 맞게 조정.

사용법:
    from utils.cl_antibot import pick_ua, detect, get_countermeasure, AntiBotType
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class AntiBotType(Enum):
    """Anti-bot 탐지 결과 분류."""
    CLOUDFLARE = "cloudflare"
    RECAPTCHA  = "recaptcha"
    RATE_LIMIT = "rate_limit"
    IP_BLOCK   = "ip_block"
    WAF_GENERIC = "waf_generic"
    NONE       = "none"


@dataclass(frozen=True)
class Countermeasure:
    """유형별 자동 대응 규칙."""
    action: str
    delay_multiplier: float
    extra_headers: dict | None
    should_circuit_break: bool


COUNTERMEASURES: dict[AntiBotType, Countermeasure] = {
    AntiBotType.CLOUDFLARE: Countermeasure(
        action="add_delay_and_headers",
        delay_multiplier=3.0,
        extra_headers={"Accept-Language": "es-CL,es;q=0.9,en;q=0.8", "Accept-Encoding": "gzip, deflate, br"},
        should_circuit_break=False,
    ),
    AntiBotType.RATE_LIMIT: Countermeasure(
        action="respect_retry_after",
        delay_multiplier=2.0,
        extra_headers=None,
        should_circuit_break=False,
    ),
    AntiBotType.IP_BLOCK: Countermeasure(
        action="circuit_break",
        delay_multiplier=0,
        extra_headers=None,
        should_circuit_break=True,
    ),
    AntiBotType.RECAPTCHA: Countermeasure(
        action="circuit_break",
        delay_multiplier=0,
        extra_headers=None,
        should_circuit_break=True,
    ),
    AntiBotType.WAF_GENERIC: Countermeasure(
        action="exponential_backoff",
        delay_multiplier=5.0,
        extra_headers=None,
        should_circuit_break=False,
    ),
    AntiBotType.NONE: Countermeasure(
        action="none",
        delay_multiplier=1.0,
        extra_headers=None,
        should_circuit_break=False,
    ),
}

# ─── User-Agent 회전 풀 ──────────────────────────────────
UA_POOL: list[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]


def pick_ua() -> str:
    """UA_POOL에서 랜덤 선택."""
    return random.choice(UA_POOL)


# ─── Anti-bot 탐지 패턴 ───────────────────────────────────
_CF_BODY_PATTERNS = (
    "cloudflare",
    "cf-challenge",
    "cf-browser-verification",
    "checking your browser",
    "ray id:",
    "performance & security by cloudflare",
    "just a moment",
    "please wait",
)

_CF_HEADER_PATTERNS = ("cloudflare", "cf-ray")

_CAPTCHA_PATTERNS = (
    "recaptcha",
    "g-recaptcha",
    "hcaptcha",
    "captcha-container",
    "captcha_challenge",
)

_WAF_STATUS_CODES = frozenset({520, 521, 522, 523, 524, 525, 526})


def detect(
    status_code: int,
    body: str = "",
    headers: Optional[dict[str, str]] = None,
) -> AntiBotType:
    """HTTP 응답에서 Anti-bot 유형 판별."""
    headers = headers or {}
    body_lower = body.lower()
    headers_lower = {k.lower(): v.lower() for k, v in headers.items()}

    # 1. Cloudflare (body + header)
    cf_in_body    = any(p in body_lower for p in _CF_BODY_PATTERNS)
    cf_in_headers = any(
        any(p in v for p in _CF_HEADER_PATTERNS)
        for v in headers_lower.values()
    )
    if cf_in_body or cf_in_headers:
        return AntiBotType.CLOUDFLARE

    # 2. Rate Limit
    if status_code == 429:
        return AntiBotType.RATE_LIMIT

    # 3. CAPTCHA / IP block
    if status_code == 403:
        if any(p in body_lower for p in _CAPTCHA_PATTERNS):
            return AntiBotType.RECAPTCHA
        return AntiBotType.IP_BLOCK

    # 4. WAF 특수 코드
    if status_code in _WAF_STATUS_CODES:
        return AntiBotType.WAF_GENERIC

    return AntiBotType.NONE


def get_countermeasure(antibot_type: AntiBotType) -> Countermeasure:
    """탐지 유형에 대한 대응 규칙 반환."""
    return COUNTERMEASURES.get(antibot_type, COUNTERMEASURES[AntiBotType.NONE])
