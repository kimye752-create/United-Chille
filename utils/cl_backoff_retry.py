"""
cl_backoff_retry.py — 칠레 크롤러용 비동기 HTTP 지수 백오프 + 지터 + 429 Retry-After 존중

Saudi pharma crawler backoff_retry.py 기반 — async/await 버전으로 재작성.
Chile 크롤러는 모두 async이므로 asyncio.sleep 사용.

사용법:
    from utils.cl_backoff_retry import async_with_backoff, RetryExhausted

    @async_with_backoff(max_attempts=3, base=3.0)
    async def fetch(url: str) -> httpx.Response:
        resp = await client.get(url, timeout=10)
        resp.raise_for_status()
        return resp
"""
from __future__ import annotations

import asyncio
import functools
import logging
import random
import time
from typing import Any, Callable, TypeVar

import httpx

try:
    from utils.cl_antibot import AntiBotType, detect as detect_antibot, get_countermeasure
except ImportError:
    from cl_antibot import AntiBotType, detect as detect_antibot, get_countermeasure  # type: ignore[no-redef]


logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


class RetryExhausted(Exception):
    """최대 재시도 횟수 초과"""


def _compute_wait(
    attempt: int,
    *,
    base: float,
    max_wait: float,
    jitter: float,
) -> float:
    """지수 백오프 + 지터."""
    exponential = base * (2 ** attempt)
    jittered    = exponential + random.uniform(0, jitter)
    return min(jittered, max_wait)


def _parse_retry_after(header_value: str | None) -> float | None:
    """Retry-After 헤더 파싱 — delta-seconds 또는 HTTP-date."""
    if not header_value:
        return None
    header_value = header_value.strip()
    try:
        return max(0.0, float(header_value))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(header_value)
        if dt is None:
            return None
        delta = dt.timestamp() - time.time()
        return max(0.0, delta)
    except (TypeError, ValueError):
        return None


def async_with_backoff(
    *,
    max_attempts: int = 3,
    base: float = 3.0,
    max_wait: float = 60.0,
    jitter: float = 2.0,
    retry_on_status: tuple[int, ...] = (429, 500, 502, 503, 504),
) -> Callable[[F], F]:
    """비동기 HTTP 함수를 감싸는 재시도 데코레이터.

    함수가 httpx.Response를 반환하거나 httpx.HTTPStatusError를 raise해야 한다.
    GET 및 idempotent 메서드에만 적용 (Chile 크롤러는 전부 GET).
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: Exception | None = None

            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)

                except httpx.HTTPStatusError as e:
                    status   = e.response.status_code
                    last_exc = e

                    # ── Anti-bot 탐지 ──
                    try:
                        resp_body = e.response.text[:2000]
                    except Exception:
                        resp_body = ""
                    resp_headers = dict(e.response.headers) if e.response else {}
                    ab_type = detect_antibot(status, resp_body, resp_headers)
                    cm      = get_countermeasure(ab_type)

                    # CAPTCHA / IP 차단 → 즉시 전파
                    if cm.should_circuit_break:
                        logger.warning(
                            "Anti-bot 탐지: %s → 즉시 중단 (circuit break)",
                            ab_type.value,
                        )
                        raise

                    # 재시도 대상이 아닌 4xx → 즉시 전파
                    if status not in retry_on_status:
                        raise

                    # 마지막 시도 → 전파
                    if attempt == max_attempts - 1:
                        break

                    # ── 대기 시간 산출 ──
                    if status == 429:
                        retry_after = _parse_retry_after(
                            e.response.headers.get("Retry-After")
                        )
                        if retry_after is not None:
                            wait = min(retry_after * cm.delay_multiplier, max_wait)
                        else:
                            wait = min(
                                _compute_wait(attempt, base=base, max_wait=max_wait, jitter=jitter)
                                * cm.delay_multiplier,
                                max_wait,
                            )
                        logger.warning(
                            "429 받음 [%s] (시도 %d/%d). %.1f초 대기",
                            ab_type.value, attempt + 1, max_attempts, wait,
                        )
                    else:
                        wait = min(
                            _compute_wait(attempt, base=base, max_wait=max_wait, jitter=jitter)
                            * cm.delay_multiplier,
                            max_wait,
                        )
                        logger.warning(
                            "%d 받음 [%s] (시도 %d/%d). %.1f초 대기",
                            status, ab_type.value, attempt + 1, max_attempts, wait,
                        )

                    await asyncio.sleep(wait)

                except (httpx.TimeoutException, httpx.NetworkError) as e:
                    last_exc = e
                    if attempt == max_attempts - 1:
                        break
                    wait = _compute_wait(
                        attempt, base=base, max_wait=max_wait, jitter=jitter
                    )
                    logger.warning(
                        "네트워크 오류: %s. %.1f초 대기 후 재시도", e, wait,
                    )
                    await asyncio.sleep(wait)

            raise RetryExhausted(
                f"{func.__name__} 최대 재시도 초과 (attempts={max_attempts})"
            ) from last_exc

        return wrapper  # type: ignore[return-value]

    return decorator
