"""CoinGecko client — BTC/ETH dominance and total market cap.

Optional API key via COINGECKO_API_KEY env var (x-cg-demo-api-key header).
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import httpx
import structlog

log = structlog.get_logger()

BASE_URL = "https://api.coingecko.com/api/v3"


class CoinGeckoClient:
    """CoinGecko public API client for global market data."""

    def __init__(self, api_key: str = "") -> None:
        headers = {}
        if api_key:
            headers["x-cg-demo-api-key"] = api_key
        self._client = httpx.AsyncClient(timeout=30.0, headers=headers, follow_redirects=True)
        self._rate_lock = asyncio.Lock()
        self._last_call_time = 0.0
        # Free tier: 10-30 calls/min depending on key. 2s interval is safe.
        self._min_call_interval = 2.0

    async def close(self) -> None:
        await self._client.aclose()

    async def _rate_limit(self) -> None:
        async with self._rate_lock:
            now = time.monotonic()
            elapsed = now - self._last_call_time
            if elapsed < self._min_call_interval:
                await asyncio.sleep(self._min_call_interval - elapsed)
            self._last_call_time = time.monotonic()

    async def _request(self, endpoint: str, params: dict | None = None) -> dict:
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
                    delay = (2 ** attempt)
                    log.warning("coingecko.retry", endpoint=endpoint, attempt=attempt + 1, delay=delay, error=str(e))
                    await asyncio.sleep(delay)
        raise last_error  # type: ignore[misc]

    async def get_global(self) -> dict:
        """Get global market data: BTC/ETH dominance and total market cap.

        Returns {btc_dominance, eth_dominance, total_market_cap, timestamp}.
        """
        data = await self._request("/global")
        market_data = data["data"]
        cap_pct = market_data["market_cap_percentage"]
        total_cap = market_data["total_market_cap"]
        # Access keys directly — KeyError on missing is better than storing 0.0 as real data
        return {
            "btc_dominance": float(cap_pct["btc"]),
            "eth_dominance": float(cap_pct["eth"]),
            "total_market_cap": float(total_cap["usd"]),
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        }
