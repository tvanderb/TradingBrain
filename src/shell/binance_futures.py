"""Binance Futures client — funding rates and open interest.

No authentication required. Uses public fapi endpoints.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import httpx
import structlog

log = structlog.get_logger()

BASE_URL = "https://fapi.binance.com"


class BinanceFuturesClient:
    """Binance Futures public API client for funding rates and open interest."""

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
        self._rate_lock = asyncio.Lock()
        self._last_call_time = 0.0
        self._min_call_interval = 0.2  # ~5 req/sec

    async def close(self) -> None:
        await self._client.aclose()

    async def _rate_limit(self) -> None:
        async with self._rate_lock:
            now = time.monotonic()
            elapsed = now - self._last_call_time
            if elapsed < self._min_call_interval:
                await asyncio.sleep(self._min_call_interval - elapsed)
            self._last_call_time = time.monotonic()

    async def _request(self, endpoint: str, params: dict | None = None) -> dict | list:
        """Make a GET request with rate limiting and retry."""
        last_error: Exception | None = None
        for attempt in range(3):
            await self._rate_limit()
            try:
                resp = await self._client.get(f"{BASE_URL}{endpoint}", params=params)
                resp.raise_for_status()
                return resp.json()
            except (httpx.HTTPStatusError, httpx.RequestError) as e:
                last_error = e
                if attempt < 2:
                    delay = (2 ** attempt)  # 1s, 2s
                    log.warning("binance.retry", endpoint=endpoint, attempt=attempt + 1, delay=delay, error=str(e))
                    await asyncio.sleep(delay)
        raise last_error  # type: ignore[misc]

    async def get_funding_rate(self, symbol: str, limit: int = 1) -> list[dict]:
        """Get recent funding rate(s) for a symbol.

        Returns list of {symbol, rate, timestamp} dicts with parsed values.
        """
        data = await self._request("/fapi/v1/fundingRate", {"symbol": symbol, "limit": limit})
        return [
            {
                "symbol": row["symbol"],
                "rate": float(row["fundingRate"]),
                "timestamp": _ms_to_iso(row["fundingTime"]),
            }
            for row in data
        ]

    async def get_funding_rate_history(self, symbol: str, limit: int = 1000) -> list[dict]:
        """Get funding rate history for backfill. Same endpoint, larger limit."""
        return await self.get_funding_rate(symbol, limit=limit)

    async def get_open_interest(self, symbol: str) -> dict:
        """Get current open interest for a symbol.

        Returns {symbol, value, timestamp} with parsed values.
        """
        data = await self._request("/fapi/v1/openInterest", {"symbol": symbol})
        # time field may be absent in some responses — use current time explicitly rather than
        # silently pretending a made-up timestamp is real exchange data
        ts_ms = data.get("time")
        if ts_ms is None:
            log.warning("binance.oi_no_timestamp", symbol=symbol)
            ts_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        return {
            "symbol": data["symbol"],
            "value": float(data["openInterest"]),
            "timestamp": _ms_to_iso(ts_ms),
        }


def _ms_to_iso(ms: int) -> str:
    """Convert milliseconds timestamp to ISO 8601 UTC string."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
