"""LLM API 呼び出し用の指数バックオフリトライ。

cron 中に発生する一時的な APIConnectionError / 503 / 529 / 過渡レート制限を吸収する。
"""

from __future__ import annotations

import logging
import random
import time
from typing import Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_BASE_DELAY = 2.0
DEFAULT_MAX_DELAY = 30.0


def _is_retryable(exc: BaseException) -> bool:
    name = type(exc).__name__
    if name in {"APIConnectionError", "APITimeoutError", "InternalServerError", "ServiceUnavailableError"}:
        return True
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and status in {408, 429, 500, 502, 503, 504, 529}:
        return True
    msg = str(exc).lower()
    if any(s in msg for s in ("connection", "timeout", "temporarily", "overloaded", "deadline")):
        return True
    return False


def call_with_retry(
    fn: Callable[[], T],
    *,
    label: str = "llm",
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
) -> T:
    last: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt >= max_attempts or not _is_retryable(exc):
                raise
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            delay += random.uniform(0, delay * 0.3)
            logger.warning(
                "  ⚠ %s API呼び出し失敗 (%s/%s, %s) → %.1fs後に再試行",
                label, attempt, max_attempts, type(exc).__name__, delay,
            )
            time.sleep(delay)
    assert last is not None
    raise last
