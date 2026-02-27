"""Fear & Greed Index client — Alternative.me API.

No authentication required.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import httpx
import structlog

log = structlog.get_logger()

BASE_URL = "https://api.alternative.me/fng"


class FearGreedClient:
    """Alternative.me Fear & Greed Index client."""

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
        self._rate_lock = asyncio.Lock()
        self._last_call_time = 0.0
        self._min_call_interval = 1.0  # conservative — low-frequency API

    async def close(self) -> None:
        await self._client.aclose()

    async def _rate_limit(self) -> None:
        async with self._rate_lock:
            now = time.monotonic()
            elapsed = now - self._last_call_time
            if elapsed < self._min_call_interval:
                await asyncio.sleep(self._min_call_interval - elapsed)
            self._last_call_time = time.monotonic()

    async def _request(self, params: dict | None = None) -> dict:
        """Make a GET request with rate limiting and retry."""
        last_error: Exception | None = None
        for attempt in range(3):
            await self._rate_limit()
            try:
                resp = await self._client.get(BASE_URL, params=params)
                resp.raise_for_status()
                return resp.json()
            except (httpx.HTTPStatusError, httpx.RequestError) as e:
                last_error = e
                if attempt < 2:
                    delay = (2 ** attempt)
                    log.warning("fear_greed.retry", attempt=attempt + 1, delay=delay, error=str(e))
                    await asyncio.sleep(delay)
        raise last_error  # type: ignore[misc]

    async def get_current(self) -> dict:
        """Get current Fear & Greed Index value.

        Returns {value, classification, timestamp} with parsed values.
        GOTCHA: timestamp from this API is SECONDS, not milliseconds.
        """
        data = await self._request({"limit": "1"})
        entry = data["data"][0]
        # timestamp is seconds (not milliseconds)
        ts_seconds = int(entry["timestamp"])
        return {
            "value": int(entry["value"]),
            "classification": entry["value_classification"],
            "timestamp": datetime.fromtimestamp(ts_seconds, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        }
