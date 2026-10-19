"""Shared HTTP client: timeouts, a descriptive User-Agent, retries for transient failures only."""

from __future__ import annotations

import logging
import time
from typing import Any

import requests
from tenacity import (
    Retrying,
    before_sleep_log,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)
from tenacity.wait import wait_base

log = logging.getLogger(__name__)

USER_AGENT = "gb-grid-carbon/0.1 (portfolio data pipeline)"
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class RetryableHTTPError(Exception):
    """Transient upstream failure (rate limit / 5xx) worth retrying."""


class ApiError(Exception):
    """Permanent failure (e.g. 400 for an invalid date range): retrying will not help."""


class ApiClient:
    def __init__(
        self,
        *,
        timeout_s: float = 60.0,
        max_attempts: int = 6,
        wait: wait_base | None = None,
        pause_s: float = 0.0,
        session: requests.Session | None = None,
    ) -> None:
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.timeout_s = timeout_s
        self.max_attempts = max_attempts
        # 2+4+8+16+32 s = ~62 s of backoff: enough to outlast Open-Meteo's per-minute 429 quota.
        self.wait = wait if wait is not None else wait_exponential(multiplier=2, min=2, max=60)
        self.pause_s = pause_s

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        retrying = Retrying(
            retry=retry_if_exception_type(
                (RetryableHTTPError, requests.ConnectionError, requests.Timeout)
            ),
            stop=stop_after_attempt(self.max_attempts),
            wait=self.wait,
            reraise=True,
            before_sleep=before_sleep_log(log, logging.WARNING),
        )
        for attempt in retrying:
            with attempt:
                data = self._get_once(url, params)
        if self.pause_s:
            time.sleep(self.pause_s)
        return data

    def _get_once(self, url: str, params: dict[str, Any] | None) -> Any:
        resp = self.session.get(url, params=params, timeout=self.timeout_s)
        if resp.status_code in RETRYABLE_STATUS:
            raise RetryableHTTPError(f"HTTP {resp.status_code} for {resp.url}")
        if not resp.ok:
            raise ApiError(f"HTTP {resp.status_code} for {resp.url}: {resp.text[:300]}")
        try:
            return resp.json()
        except requests.JSONDecodeError as e:  # a blip (empty or HTML body) behind a 200
            raise RetryableHTTPError(f"HTTP {resp.status_code} but not JSON for {resp.url}: "
                                     f"{resp.text[:100]!r}") from e  # fmt: skip
