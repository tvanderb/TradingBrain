"""Kraken REST + WebSocket v2 client.

Handles all exchange communication. Part of the rigid shell.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import ssl
import time
import urllib.parse
from collections import defaultdict
from typing import Any, Callable, Coroutine

import certifi
import httpx
import pandas as pd
import structlog
import websockets

from src.shell.config import KrakenConfig

log = structlog.get_logger()

# Kraken pair mapping: user-friendly -> REST API format
PAIR_MAP = {
    "BTC/USD": "XBTUSD",
    "ETH/USD": "ETHUSD",
    "SOL/USD": "SOLUSD",
    "XRP/USD": "XRPUSD",
    "DOGE/USD": "XDGUSD",
    "ADA/USD": "ADAUSD",
    "LINK/USD": "LINKUSD",
    "AVAX/USD": "AVAXUSD",
    "DOT/USD": "DOTUSD",
}

PAIR_REVERSE = {v: k for k, v in PAIR_MAP.items()}
# WS v2 uses slash-separated format with Kraken's internal names
PAIR_REVERSE.update({
    "XBT/USD": "BTC/USD",
    "XDG/USD": "DOGE/USD",
})


def to_kraken_pair(symbol: str) -> str:
    return PAIR_MAP.get(symbol, symbol.replace("/", ""))


def from_kraken_pair(pair: str) -> str:
    return PAIR_REVERSE.get(pair, pair)


class KrakenREST:
    """Kraken REST API client."""

    def __init__(self, config: KrakenConfig) -> None:
        self._base_url = config.rest_url
        self._api_key = config.api_key
        self._secret = config.secret_key
        self._client = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        await self._client.aclose()

    def _sign(self, urlpath: str, data: dict[str, Any]) -> dict[str, str]:
        postdata = urllib.parse.urlencode(data)
        encoded = (str(data["nonce"]) + postdata).encode()
        message = urlpath.encode() + hashlib.sha256(encoded).digest()
        mac = hmac.new(base64.b64decode(self._secret), message, hashlib.sha512)
        return {
            "API-Key": self._api_key,
            "API-Sign": base64.b64encode(mac.digest()).decode(),
        }

    async def public(self, endpoint: str, params: dict | None = None) -> dict:
        url = f"{self._base_url}/0/public/{endpoint}"
        resp = await self._client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        if data.get("error"):
            raise RuntimeError(f"Kraken API error: {data['error']}")
        return data["result"]

    async def private(self, endpoint: str, data: dict | None = None) -> dict:
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

    async def get_ohlc(self, symbol: str, interval: int = 5, since: int | None = None) -> pd.DataFrame:
        """Fetch OHLC candles for a symbol."""
        pair = to_kraken_pair(symbol)
        params: dict[str, Any] = {"pair": pair, "interval": interval}
        if since:
            params["since"] = since

        result = await self.public("OHLC", params)
        pair_keys = [k for k in result if k != "last"]
        if not pair_keys:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        pair_key = pair_keys[0]
        rows = result[pair_key]

        if not rows:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        df = pd.DataFrame(
            rows,
            columns=["time", "open", "high", "low", "close", "vwap", "volume", "count"],
        )
        for col in ["open", "high", "low", "close", "vwap", "volume"]:
            df[col] = df[col].astype(float)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        df.set_index("time", inplace=True)
        return df[["open", "high", "low", "close", "volume"]]

    async def get_ticker(self, symbol: str) -> dict:
        pair = to_kraken_pair(symbol)
        result = await self.public("Ticker", {"pair": pair})
        if not result:
            raise RuntimeError(f"Kraken returned empty ticker for {symbol}")
        pair_key = list(result.keys())[0]
        return result[pair_key]

    async def get_spread(self, symbol: str) -> float:
        """Get current bid-ask spread as a percentage."""
        ticker = await self.get_ticker(symbol)
        ask = float(ticker["a"][0])
        bid = float(ticker["b"][0])
        if bid == 0:
            return 0.0
        return (ask - bid) / bid

    async def get_trade_volume(self) -> dict:
        """Get fee tier from 30-day trade volume."""
        result = await self.private("TradeVolume")
        return {
            "volume": float(result.get("volume", 0)),
            "currency": result.get("currency", "USD"),
        }

    async def get_fee_schedule(self, symbol: str) -> tuple[float, float]:
        """Get maker/taker fees for a pair. Returns (maker_pct, taker_pct)."""
        pair = to_kraken_pair(symbol)
        result = await self.private("TradeVolume", {"pair": pair})

        maker_fee = 0.25
        taker_fee = 0.40

        if "fees" in result:
            fee_key = list(result["fees"].keys())[0] if result["fees"] else None
            if fee_key:
                taker_fee = float(result["fees"][fee_key].get("fee", 0.40))

        if "fees_maker" in result:
            maker_key = list(result["fees_maker"].keys())[0] if result["fees_maker"] else None
            if maker_key:
                maker_fee = float(result["fees_maker"][maker_key].get("fee", 0.25))

        return maker_fee, taker_fee

    async def get_balance(self) -> dict[str, float]:
        """Get account balances."""
        result = await self.private("Balance")
        return {k: float(v) for k, v in result.items()}

    async def get_open_orders(self) -> dict:
        return await self.private("OpenOrders")

    async def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        volume: float,
        price: float | None = None,
    ) -> dict:
        """Place an order on Kraken."""
        pair = to_kraken_pair(symbol)
        data: dict[str, Any] = {
            "pair": pair,
            "type": side,       # "buy" or "sell"
            "ordertype": order_type,  # "market" or "limit"
            "volume": str(volume),
        }
        if price is not None:
            data["price"] = str(price)
        return await self.private("AddOrder", data)

    async def cancel_order(self, txid: str) -> dict:
        return await self.private("CancelOrder", {"txid": txid})

    async def cancel_all_orders(self) -> dict:
        return await self.private("CancelAll")


class KrakenWebSocket:
    """Kraken WebSocket v2 client for real-time data."""

    def __init__(self, ws_url: str, symbols: list[str]) -> None:
        self._url = ws_url
        self._symbols = symbols
        self._ws = None
        self._running = False
        self._callbacks: dict[str, list[Callable]] = defaultdict(list)
        self._prices: dict[str, float] = {}
        self._retry_count = 0
        self._max_retries = 5
        self._on_failure: Callable[[], Coroutine] | None = None

    def set_on_failure(self, callback: Callable[[], Coroutine]) -> None:
        """Set callback for when WebSocket permanently fails after max retries."""
        self._on_failure = callback

    @property
    def prices(self) -> dict[str, float]:
        return dict(self._prices)

    def on_ticker(self, callback: Callable) -> None:
        self._callbacks["ticker"].append(callback)

    def on_ohlc(self, callback: Callable) -> None:
        self._callbacks["ohlc"].append(callback)

    async def connect(self) -> None:
        self._running = True
        while self._running and self._retry_count < self._max_retries:
            try:
                ssl_ctx = ssl.create_default_context(cafile=certifi.where())
                async with websockets.connect(self._url, ssl=ssl_ctx) as ws:
                    self._ws = ws
                    self._retry_count = 0
                    log.info("websocket.connected", url=self._url)
                    await self._subscribe(ws)
                    await self._listen(ws)
            except Exception as e:
                self._retry_count += 1
                wait = min(2 ** self._retry_count, 30)
                log.warning("websocket.reconnecting", error=str(e), retry=self._retry_count, wait=wait)
                await asyncio.sleep(wait)

        if self._retry_count >= self._max_retries:
            log.error("websocket.max_retries", retries=self._max_retries)
            if self._on_failure:
                await self._on_failure()

    async def _subscribe(self, ws) -> None:
        pairs = [to_kraken_pair(s) for s in self._symbols]
        # Subscribe to ticker
        await ws.send(json.dumps({
            "method": "subscribe",
            "params": {"channel": "ticker", "symbol": pairs},
        }))
        # Subscribe to OHLC (5-min)
        await ws.send(json.dumps({
            "method": "subscribe",
            "params": {"channel": "ohlc", "symbol": pairs, "interval": 5},
        }))

    async def _listen(self, ws) -> None:
        async for msg_raw in ws:
            try:
                msg = json.loads(msg_raw)
            except json.JSONDecodeError:
                continue

            try:
                channel = msg.get("channel")

                if channel == "ticker":
                    for item in msg.get("data", []):
                        symbol = from_kraken_pair(item.get("symbol", ""))
                        price = float(item.get("last", 0))
                        if symbol and price:
                            self._prices[symbol] = price
                            for cb in self._callbacks.get("ticker", []):
                                try:
                                    await cb(symbol, price) if asyncio.iscoroutinefunction(cb) else cb(symbol, price)
                                except Exception as e:
                                    log.error("websocket.callback_error", channel="ticker", error=str(e))

                elif channel == "ohlc":
                    for item in msg.get("data", []):
                        symbol = from_kraken_pair(item.get("symbol", ""))
                        for cb in self._callbacks.get("ohlc", []):
                            try:
                                await cb(symbol, item) if asyncio.iscoroutinefunction(cb) else cb(symbol, item)
                            except Exception as e:
                                log.error("websocket.callback_error", channel="ohlc", error=str(e))

            except (KeyError, ValueError) as e:
                log.debug("websocket.msg_parse_error", error=str(e))
                continue

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()
