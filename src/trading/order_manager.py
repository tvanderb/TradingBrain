"""Order management — Kraken live trading and paper trading simulator.

Handles order placement, tracking, and fill simulation.
In paper mode, simulates fills against real market prices.
In live mode, places orders via Kraken REST API.
"""

from __future__ import annotations

import time
import urllib.parse
import hashlib
import hmac
import base64
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from src.core.config import Config, KrakenConfig
from src.core.logging import get_logger
from src.market.data_feed import to_kraken_pair
from src.storage.database import Database
from src.storage.models import FeeSchedule, Trade
from src.storage import queries

log = get_logger("orders")


@dataclass
class OrderResult:
    success: bool
    trade_id: int | None
    exchange_order_id: str | None
    filled_price: float | None
    commission: float
    message: str


class PaperTrader:
    """Simulated trading against real market prices.

    Tracks virtual balances and simulates fills with realistic fees and slippage.
    """

    def __init__(
        self,
        initial_balance_usd: float = 1000.0,
        fees: FeeSchedule | None = None,
    ) -> None:
        self._balance_usd = initial_balance_usd
        self._holdings: dict[str, float] = {}  # symbol -> qty
        self._fees = fees or FeeSchedule(maker_fee_pct=0.16, taker_fee_pct=0.26)
        self._order_counter = 0

    @property
    def balance_usd(self) -> float:
        return self._balance_usd

    @property
    def holdings(self) -> dict[str, float]:
        return dict(self._holdings)

    def update_fees(self, fees: FeeSchedule) -> None:
        self._fees = fees

    def portfolio_value(self, prices: dict[str, float]) -> float:
        """Calculate total portfolio value at current prices."""
        total = self._balance_usd
        for symbol, qty in self._holdings.items():
            price = prices.get(symbol, 0)
            total += qty * price
        return total

    def execute(
        self,
        symbol: str,
        side: str,
        qty: float,
        market_price: float,
        order_type: str = "market",
    ) -> OrderResult:
        """Simulate a trade execution."""
        self._order_counter += 1
        order_id = f"paper-{self._order_counter}-{int(time.time())}"

        # Simulate slippage (0.05% for market orders)
        slippage = 0.0005 if order_type == "market" else 0.0
        if side == "buy":
            filled_price = market_price * (1 + slippage)
        else:
            filled_price = market_price * (1 - slippage)

        trade_value = qty * filled_price
        commission = self._fees.estimate_cost(trade_value, is_maker=(order_type == "limit"))

        if side == "buy":
            cost = trade_value + commission
            if cost > self._balance_usd:
                return OrderResult(
                    success=False,
                    trade_id=None,
                    exchange_order_id=None,
                    filled_price=None,
                    commission=0,
                    message=f"Insufficient balance: need ${cost:.2f}, have ${self._balance_usd:.2f}",
                )
            self._balance_usd -= cost
            self._holdings[symbol] = self._holdings.get(symbol, 0) + qty
        else:
            current_qty = self._holdings.get(symbol, 0)
            if qty > current_qty:
                return OrderResult(
                    success=False,
                    trade_id=None,
                    exchange_order_id=None,
                    filled_price=None,
                    commission=0,
                    message=f"Insufficient holdings: need {qty}, have {current_qty}",
                )
            proceeds = trade_value - commission
            self._balance_usd += proceeds
            self._holdings[symbol] = current_qty - qty
            if self._holdings[symbol] <= 0:
                del self._holdings[symbol]

        log.info(
            "paper_fill",
            symbol=symbol,
            side=side,
            qty=qty,
            price=round(filled_price, 2),
            commission=round(commission, 4),
            balance=round(self._balance_usd, 2),
        )

        return OrderResult(
            success=True,
            trade_id=None,  # Set by OrderManager after DB insert
            exchange_order_id=order_id,
            filled_price=filled_price,
            commission=commission,
            message="Paper trade filled",
        )


class KrakenTrader:
    """Live trading via Kraken REST API."""

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

    async def place_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        order_type: str = "market",
        price: float | None = None,
    ) -> OrderResult:
        """Place an order on Kraken."""
        pair = to_kraken_pair(symbol)
        urlpath = "/0/private/AddOrder"
        url = f"{self._base_url}{urlpath}"

        data: dict[str, Any] = {
            "nonce": str(int(time.time() * 1000)),
            "ordertype": order_type,
            "type": side,
            "volume": str(qty),
            "pair": pair,
        }
        if price is not None:
            data["price"] = str(price)

        headers = self._sign(urlpath, data)
        resp = await self._client.post(url, data=data, headers=headers)
        resp.raise_for_status()
        result = resp.json()

        if result.get("error"):
            return OrderResult(
                success=False,
                trade_id=None,
                exchange_order_id=None,
                filled_price=None,
                commission=0,
                message=f"Kraken error: {result['error']}",
            )

        txid = result["result"]["txid"][0] if result["result"].get("txid") else None

        log.info("kraken_order_placed", symbol=symbol, side=side, qty=qty, txid=txid)

        return OrderResult(
            success=True,
            trade_id=None,
            exchange_order_id=txid,
            filled_price=None,  # Will be updated on fill
            commission=0,
            message=f"Order placed: {txid}",
        )


class OrderManager:
    """Unified order management — routes to paper or live trading."""

    def __init__(self, config: Config, db: Database) -> None:
        self._config = config
        self._db = db
        self._paper_mode = config.mode == "paper"

        if self._paper_mode:
            self._paper = PaperTrader(initial_balance_usd=config.paper_balance_usd)
            self._live: KrakenTrader | None = None
        else:
            self._paper = None
            self._live = KrakenTrader(config.kraken)

    def update_fees(self, fees: FeeSchedule) -> None:
        if self._paper:
            self._paper.update_fees(fees)

    async def place_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        order_type: str = "market",
        signal_id: int | None = None,
        notes: str | None = None,
    ) -> OrderResult:
        """Place a trade and record it in the database."""
        # Insert trade record first
        trade = Trade(
            symbol=symbol,
            side=side,
            qty=qty,
            price=price,
            order_type=order_type,
            signal_id=signal_id,
            notes=notes,
        )
        trade_id = await queries.insert_trade(self._db, trade)

        # Execute
        if self._paper_mode and self._paper:
            result = self._paper.execute(symbol, side, qty, price, order_type)
        elif self._live:
            result = await self._live.place_order(symbol, side, qty, order_type, price if order_type != "market" else None)
        else:
            result = OrderResult(False, None, None, None, 0, "No trader configured")

        result.trade_id = trade_id

        # Update trade record with fill info
        if result.success and result.filled_price:
            await queries.update_trade_fill(
                self._db,
                trade_id,
                result.filled_price,
                result.exchange_order_id or "",
                result.commission,
            )

        return result

    def get_portfolio_value(self, prices: dict[str, float]) -> float:
        """Get total portfolio value."""
        if self._paper:
            return self._paper.portfolio_value(prices)
        return 0.0  # Live mode: fetched from Kraken

    def get_balance(self) -> float:
        if self._paper:
            return self._paper.balance_usd
        return 0.0

    def get_holdings(self) -> dict[str, float]:
        if self._paper:
            return self._paper.holdings
        return {}

    async def close(self) -> None:
        if self._live:
            await self._live.close()
