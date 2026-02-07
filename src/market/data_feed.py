"""Kraken WebSocket v2 market data feed.

Streams real-time OHLC bars and trades for configured symbols.
Also provides REST-based historical data for indicator initialization.
"""

from __future__ import annotations

import asyncio
import json
import hashlib
import hmac
import base64
import time
import urllib.parse
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Coroutine

import httpx
import pandas as pd

from src.core.config import Config, KrakenConfig
from src.core.logging import get_logger
from src.storage.models import FeeSchedule

log = get_logger("data_feed")


@dataclass
class OHLCBar:
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float
    trades: int
    interval_begin: str
    interval: int  # minutes


@dataclass
class LiveTrade:
    symbol: str
    price: float
    qty: float
    side: str  # "buy" or "sell"
    timestamp: str


class KrakenREST:
    """Kraken REST API client for historical data and account operations."""

    def __init__(self, config: KrakenConfig) -> None:
        self._base_url = config.rest_url
        self._api_key = config.api_key
        self._secret = config.secret_key
        self._client = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        await self._client.aclose()

    def _sign(self, urlpath: str, data: dict[str, Any]) -> dict[str, str]:
        """Generate Kraken API signature for private endpoints."""
        postdata = urllib.parse.urlencode(data)
        encoded = (str(data["nonce"]) + postdata).encode()
        message = urlpath.encode() + hashlib.sha256(encoded).digest()
        mac = hmac.new(base64.b64decode(self._secret), message, hashlib.sha512)
        return {
            "API-Key": self._api_key,
            "API-Sign": base64.b64encode(mac.digest()).decode(),
        }

    async def _public(self, endpoint: str, params: dict | None = None) -> dict:
        url = f"{self._base_url}/0/public/{endpoint}"
        resp = await self._client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        if data.get("error"):
            raise RuntimeError(f"Kraken API error: {data['error']}")
        return data["result"]

    async def _private(self, endpoint: str, data: dict | None = None) -> dict:
        urlpath = f"/0/private/{endpoint}"
        url = f"{self._base_url}{urlpath}"
        data = data or {}
        data["nonce"] = str(int(time.time() * 1000))
        headers = self._sign(urlpath, data)
        resp = await self._client.post(url, data=data, headers=headers)
        resp.raise_for_status()
        result = resp.json()
        if result.get("error"):
            raise RuntimeError(f"Kraken API error: {result['error']}")
        return result["result"]

    async def get_ohlc(
        self, pair: str, interval: int = 5, since: int | None = None
    ) -> pd.DataFrame:
        """Fetch historical OHLC data.

        Args:
            pair: Trading pair (e.g. "XBTUSD" for BTC/USD)
            interval: Candle interval in minutes
            since: Unix timestamp to fetch from
        """
        params: dict[str, Any] = {"pair": pair, "interval": interval}
        if since:
            params["since"] = since
        result = await self._public("OHLC", params)
        # Result has pair key and "last" key
        pair_key = [k for k in result if k != "last"][0]
        rows = result[pair_key]
        df = pd.DataFrame(
            rows,
            columns=["time", "open", "high", "low", "close", "vwap", "volume", "count"],
        )
        for col in ["open", "high", "low", "close", "vwap", "volume"]:
            df[col] = df[col].astype(float)
        df["count"] = df["count"].astype(int)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        df.set_index("time", inplace=True)
        return df

    async def get_ticker(self, pair: str) -> dict:
        """Get current ticker data for a pair."""
        result = await self._public("Ticker", {"pair": pair})
        pair_key = list(result.keys())[0]
        return result[pair_key]

    async def get_trade_volume(self, pairs: list[str] | None = None) -> FeeSchedule:
        """Get current fee tier based on 30-day volume."""
        data: dict[str, Any] = {}
        if pairs:
            data["pair"] = ",".join(pairs)

        result = await self._private("TradeVolume", data)
        volume = float(result.get("volume", 0))

        # Extract fees for the first pair if specified
        maker_fee = 0.16
        taker_fee = 0.26
        fee_tier = "unknown"

        if pairs and "fees" in result:
            first_pair = list(result["fees"].keys())[0] if result["fees"] else None
            if first_pair:
                taker_fee = float(result["fees"][first_pair].get("fee", 0.26))
                fee_tier = result["fees"][first_pair].get("tier", "unknown")
        if pairs and "fees_maker" in result:
            first_pair = list(result["fees_maker"].keys())[0] if result["fees_maker"] else None
            if first_pair:
                maker_fee = float(result["fees_maker"][first_pair].get("fee", 0.16))

        return FeeSchedule(
            maker_fee_pct=maker_fee,
            taker_fee_pct=taker_fee,
            volume_30d_usd=volume,
            fee_tier=str(fee_tier),
        )

    async def get_balance(self) -> dict[str, float]:
        """Get account balances."""
        result = await self._private("Balance")
        return {k: float(v) for k, v in result.items()}


# Mapping from standard pair format to Kraken REST pair format
PAIR_MAP = {
    "BTC/USD": "XBTUSD",
    "ETH/USD": "ETHUSD",
    "SOL/USD": "SOLUSD",
}


def to_kraken_pair(symbol: str) -> str:
    """Convert standard pair format to Kraken REST format."""
    return PAIR_MAP.get(symbol, symbol.replace("/", ""))


class DataFeed:
    """Real-time market data via Kraken WebSocket v2 + REST historical data."""

    def __init__(self, config: Config) -> None:
        self._ws_url = config.kraken.ws_url
        self._rest = KrakenREST(config.kraken)
        self._symbols = config.markets.crypto_symbols
        self._bars: dict[str, list[OHLCBar]] = defaultdict(list)
        self._latest_prices: dict[str, float] = {}
        self._on_bar: Callable[[OHLCBar], Coroutine] | None = None
        self._on_trade: Callable[[LiveTrade], Coroutine] | None = None
        self._running = False

    @property
    def latest_prices(self) -> dict[str, float]:
        return dict(self._latest_prices)

    def get_latest_price(self, symbol: str) -> float | None:
        return self._latest_prices.get(symbol)

    async def load_historical(self, symbol: str, interval: int = 5) -> pd.DataFrame:
        """Load historical OHLC data for indicator initialization."""
        pair = to_kraken_pair(symbol)
        df = await self._rest.get_ohlc(pair, interval=interval)
        log.info("historical_loaded", symbol=symbol, rows=len(df), interval=interval)
        return df

    async def check_fees(self) -> FeeSchedule:
        """Check current fee schedule from Kraken."""
        pairs = [to_kraken_pair(s) for s in self._symbols]
        fees = await self._rest.get_trade_volume(pairs)
        log.info(
            "fee_check",
            maker=fees.maker_fee_pct,
            taker=fees.taker_fee_pct,
            volume_30d=fees.volume_30d_usd,
            tier=fees.fee_tier,
        )
        return fees

    async def stream(
        self,
        on_bar: Callable[[OHLCBar], Coroutine] | None = None,
        on_trade: Callable[[LiveTrade], Coroutine] | None = None,
    ) -> None:
        """Connect to WebSocket and stream market data.

        Reconnects automatically on disconnect.
        """
        self._on_bar = on_bar
        self._on_trade = on_trade
        self._running = True
        ws_failures = 0

        while self._running:
            try:
                await self._ws_loop()
                ws_failures = 0  # Reset on successful connection
            except Exception as e:
                ws_failures += 1
                log.error("ws_error", error=str(e), failures=ws_failures)
                if ws_failures >= 3:
                    log.warning("ws_giving_up", msg="Too many WebSocket failures, falling back to REST polling")
                    await self._poll_fallback()
                    return
                if self._running:
                    log.info("ws_reconnecting", delay=5)
                    await asyncio.sleep(5)

    async def _ws_loop(self) -> None:
        """Single WebSocket connection lifecycle."""
        try:
            import websockets
        except ImportError:
            # Fallback: use httpx websocket or just log
            log.warning("websockets_not_installed", msg="Install 'websockets' for live streaming. Using REST polling fallback.")
            await self._poll_fallback()
            return

        import ssl
        import certifi
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        async with websockets.connect(self._ws_url, ssl=ssl_ctx) as ws:
            log.info("ws_connected", url=self._ws_url)

            # Subscribe to OHLC and trades for all symbols
            await ws.send(json.dumps({
                "method": "subscribe",
                "params": {
                    "channel": "ohlc",
                    "symbol": self._symbols,
                    "interval": 5,
                },
            }))

            await ws.send(json.dumps({
                "method": "subscribe",
                "params": {
                    "channel": "trade",
                    "symbol": self._symbols,
                    "snapshot": False,
                },
            }))

            async for raw in ws:
                if not self._running:
                    break
                msg = json.loads(raw)
                await self._handle_message(msg)

    async def _poll_fallback(self) -> None:
        """REST polling fallback when websockets unavailable."""
        log.info("poll_fallback_started", interval=30)
        while self._running:
            for symbol in self._symbols:
                try:
                    pair = to_kraken_pair(symbol)
                    ticker = await self._rest.get_ticker(pair)
                    price = float(ticker["c"][0])  # Last trade price
                    self._latest_prices[symbol] = price
                except Exception as e:
                    log.error("poll_error", symbol=symbol, error=str(e))
            await asyncio.sleep(30)

    async def _handle_message(self, msg: dict) -> None:
        """Route incoming WebSocket messages."""
        channel = msg.get("channel")
        msg_type = msg.get("type")

        if channel == "ohlc" and msg_type in ("snapshot", "update"):
            for bar_data in msg.get("data", []):
                bar = OHLCBar(
                    symbol=bar_data["symbol"],
                    open=float(bar_data["open"]),
                    high=float(bar_data["high"]),
                    low=float(bar_data["low"]),
                    close=float(bar_data["close"]),
                    volume=float(bar_data["volume"]),
                    vwap=float(bar_data["vwap"]),
                    trades=int(bar_data["trades"]),
                    interval_begin=bar_data["interval_begin"],
                    interval=bar_data["interval"],
                )
                self._latest_prices[bar.symbol] = bar.close
                self._bars[bar.symbol].append(bar)

                if self._on_bar:
                    await self._on_bar(bar)

        elif channel == "trade" and msg_type in ("snapshot", "update"):
            for trade_data in msg.get("data", []):
                trade = LiveTrade(
                    symbol=trade_data["symbol"],
                    price=float(trade_data["price"]),
                    qty=float(trade_data["qty"]),
                    side=trade_data["side"],
                    timestamp=trade_data["timestamp"],
                )
                self._latest_prices[trade.symbol] = trade.price

                if self._on_trade:
                    await self._on_trade(trade)

    def stop(self) -> None:
        self._running = False
