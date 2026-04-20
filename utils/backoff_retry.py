"""HTTP 재시도 래퍼 (tenacity 기반).

보고서 §9: backoff_retry.py — HTTP 재시도 (tenacity), 나라 무관
"""

from __future__ import annotations

from typing import Any, Callable

from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


def make_retry(
    *,
    attempts: int = 3,
    min_wait: float = 2.0,
    max_wait: float = 30.0,
) -> Callable:
    """지정 파라미터로 tenacity retry 데코레이터를 반환한다."""
    return retry(
        wait=wait_exponential(multiplier=1, min=min_wait, max=max_wait),
        stop=stop_after_attempt(attempts),
        reraise=True,
    )


# 기본 설정 데코레이터 (3회, 2~30초 지수 대기)
default_retry = make_retry()


async def fetch_with_retry(
    url: str,
    *,
    attempts: int = 3,
    timeout: float = 20.0,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
) -> tuple[int, str]:
    """httpx 비동기 GET + tenacity 재시도.

    Returns:
        (status_code, response_text)

    Raises:
        RetryError: 최대 재시도 초과
        httpx.HTTPError: 네트워크 오류
    """
    import httpx

    @retry(
        wait=wait_exponential(min=2, max=30),
        stop=stop_after_attempt(attempts),
        retry=retry_if_exception_type(httpx.HTTPError),
        reraise=True,
    )
    async def _get() -> tuple[int, str]:
        async with httpx.AsyncClient(
            http2=True,
            timeout=timeout,
            follow_redirects=True,
            headers=headers or {},
        ) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            return r.status_code, r.text

    return await _get()


async def with_retry(
    coro_fn,
    *,
    attempts: int = 3,
    min_wait: float = 2.0,
    max_wait: float = 30.0,
):
    """비동기 코루틴 함수를 tenacity 재시도로 래핑하여 실행한다.

    Usage:
        async def _do(): ...
        result = await with_retry(_do)
    """
    import asyncio

    @retry(
        wait=wait_exponential(min=min_wait, max=max_wait),
        stop=stop_after_attempt(attempts),
        reraise=True,
    )
    async def _wrapped():
        return await coro_fn()

    return await _wrapped()
