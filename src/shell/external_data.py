"""External Data Collector — orchestrates Binance, Fear & Greed, and CoinGecko polling.

Manages backfill on startup, scheduled polling, and per-source fault tolerance.
Participates in all observability layers: structlog, activity log, Prometheus, Telegram.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from src.shell.binance_futures import BinanceFuturesClient
from src.shell.coingecko import CoinGeckoClient
from src.shell.config import Config
from src.shell.data_store import DataStore
from src.shell.fear_greed import FearGreedClient
from src.shell.symbol_mapping import to_binance_symbol

if TYPE_CHECKING:
    from src.shell.activity import ActivityLogger
    from src.telegram.notifications import Notifier

log = structlog.get_logger()

# Max consecutive failures before skipping one interval
MAX_CONSECUTIVE_FAILURES = 3

# Human-readable source names for notifications
_SOURCE_LABELS = {
    "binance_funding": "Binance Funding Rates",
    "binance_oi": "Binance Open Interest",
    "fear_greed": "Fear & Greed Index",
    "coingecko": "CoinGecko Global Data",
}


class ExternalDataCollector:
    """Orchestrates collection and storage of external market data."""

    def __init__(self, config: Config, data_store: DataStore) -> None:
        self._config = config
        self._data_store = data_store
        self._binance = BinanceFuturesClient()
        self._fear_greed = FearGreedClient()
        self._coingecko = CoinGeckoClient(api_key=config.external_data.coingecko_api_key)
        self._activity: ActivityLogger | None = None
        self._notifier: Notifier | None = None
        self._consecutive_failures: dict[str, int] = {
            "binance_funding": 0,
            "binance_oi": 0,
            "fear_greed": 0,
            "coingecko": 0,
        }

    def set_activity_logger(self, activity: ActivityLogger) -> None:
        self._activity = activity

    def set_notifier(self, notifier: Notifier) -> None:
        self._notifier = notifier

    async def close(self) -> None:
        await self._binance.close()
        await self._fear_greed.close()
        await self._coingecko.close()

    def _should_skip(self, source: str) -> bool:
        """Check if a source should be skipped due to consecutive failures."""
        if self._consecutive_failures[source] >= MAX_CONSECUTIVE_FAILURES:
            log.warning("external_data.skip_interval", source=source,
                        failures=self._consecutive_failures[source])
            self._consecutive_failures[source] = 0  # Reset and try again next time
            return True
        return False

    def _record_success(self, source: str) -> None:
        self._consecutive_failures[source] = 0

    async def _record_failure(self, source: str, error: Exception) -> None:
        self._consecutive_failures[source] += 1
        count = self._consecutive_failures[source]
        label = _SOURCE_LABELS.get(source, source)
        log.error("external_data.poll_failed", source=source, error=str(error),
                  consecutive=count)

        if self._activity:
            await self._activity.data(
                f"{label} poll failed ({count}/{MAX_CONSECUTIVE_FAILURES}): {error}",
                severity="warning",
            )

        # Alert on degraded state — investor should know
        if count >= MAX_CONSECUTIVE_FAILURES and self._notifier:
            await self._notifier.system_error(
                f"External data degraded: {label} failed {count}x consecutively, skipping next interval"
            )

    async def backfill_on_startup(self) -> None:
        """Run once at boot — backfill historical data where available."""
        log.info("external_data.backfill_start")
        backfill_stats: dict[str, int] = {}
        backfill_errors: list[str] = []

        # Funding rates: 30-day history per symbol
        total_funding = 0
        for symbol in self._config.symbols:
            binance_sym = to_binance_symbol(symbol)
            try:
                records = await self._binance.get_funding_rate_history(binance_sym, limit=1000)
                if records:
                    rows = [(r["symbol"], r["timestamp"], r["rate"]) for r in records]
                    await self._data_store.store_funding_rates_batch(rows)
                    total_funding += len(records)
            except Exception as e:
                log.warning("external_data.backfill_funding_failed", symbol=binance_sym, error=str(e))
                backfill_errors.append(f"funding:{binance_sym}")
        if total_funding:
            backfill_stats["funding_rates"] = total_funding

        # Open interest: current snapshot per symbol (no history endpoint)
        oi_count = 0
        for symbol in self._config.symbols:
            binance_sym = to_binance_symbol(symbol)
            try:
                oi = await self._binance.get_open_interest(binance_sym)
                await self._data_store.store_open_interest(oi["symbol"], oi["timestamp"], oi["value"])
                oi_count += 1
            except Exception as e:
                log.warning("external_data.backfill_oi_failed", symbol=binance_sym, error=str(e))
                backfill_errors.append(f"oi:{binance_sym}")
        if oi_count:
            backfill_stats["open_interest"] = oi_count

        # Fear & Greed: current value
        try:
            fg = await self._fear_greed.get_current()
            await self._data_store.store_index_value("fear_greed", fg["timestamp"], fg["value"])
            backfill_stats["fear_greed"] = fg["value"]
        except Exception as e:
            log.warning("external_data.backfill_fear_greed_failed", error=str(e))
            backfill_errors.append("fear_greed")

        # CoinGecko: current values (3 indices)
        try:
            cg = await self._coingecko.get_global()
            ts = cg["timestamp"]
            await self._data_store.store_index_value("btc_dominance", ts, cg["btc_dominance"])
            await self._data_store.store_index_value("eth_dominance", ts, cg["eth_dominance"])
            await self._data_store.store_index_value("total_market_cap", ts, cg["total_market_cap"])
            backfill_stats["btc_dominance"] = round(cg["btc_dominance"], 1)
        except Exception as e:
            log.warning("external_data.backfill_coingecko_failed", error=str(e))
            backfill_errors.append("coingecko")

        log.info("external_data.backfill_complete", stats=backfill_stats, errors=backfill_errors)

        if self._activity:
            parts = []
            if "funding_rates" in backfill_stats:
                parts.append(f"{backfill_stats['funding_rates']} funding rates")
            if "open_interest" in backfill_stats:
                parts.append(f"{backfill_stats['open_interest']} OI snapshots")
            if "fear_greed" in backfill_stats:
                parts.append(f"F&G={backfill_stats['fear_greed']}")
            if "btc_dominance" in backfill_stats:
                parts.append(f"BTC dom={backfill_stats['btc_dominance']}%")

            summary = f"External data backfill: {', '.join(parts)}" if parts else "External data backfill: no data"
            if backfill_errors:
                summary += f" | failed: {', '.join(backfill_errors)}"
                await self._activity.data(summary, severity="warning", detail={"errors": backfill_errors})
            else:
                await self._activity.data(summary, detail=backfill_stats)

    async def poll_funding_rates(self) -> None:
        """Poll latest funding rate for all symbols. Runs every 8h."""
        if self._should_skip("binance_funding"):
            return

        try:
            for symbol in self._config.symbols:
                binance_sym = to_binance_symbol(symbol)
                records = await self._binance.get_funding_rate(binance_sym, limit=1)
                for r in records:
                    await self._data_store.store_funding_rate(r["symbol"], r["timestamp"], r["rate"])
            self._record_success("binance_funding")
            log.info("external_data.poll_funding_rates_complete")
        except Exception as e:
            await self._record_failure("binance_funding", e)

    async def poll_open_interest(self) -> None:
        """Poll current open interest for all symbols. Runs every 1h."""
        if self._should_skip("binance_oi"):
            return

        try:
            for symbol in self._config.symbols:
                binance_sym = to_binance_symbol(symbol)
                oi = await self._binance.get_open_interest(binance_sym)
                await self._data_store.store_open_interest(oi["symbol"], oi["timestamp"], oi["value"])
            self._record_success("binance_oi")
            log.info("external_data.poll_oi_complete")
        except Exception as e:
            await self._record_failure("binance_oi", e)

    async def poll_fear_greed(self) -> None:
        """Poll Fear & Greed Index. Runs daily."""
        if self._should_skip("fear_greed"):
            return

        try:
            fg = await self._fear_greed.get_current()
            await self._data_store.store_index_value("fear_greed", fg["timestamp"], fg["value"])
            self._record_success("fear_greed")
            log.info("external_data.poll_fear_greed_complete", value=fg["value"])
            if self._activity:
                await self._activity.data(f"Fear & Greed Index: {fg['value']} ({fg['classification']})")
        except Exception as e:
            await self._record_failure("fear_greed", e)

    async def poll_coingecko(self) -> None:
        """Poll CoinGecko global data. Runs every 1h."""
        if self._should_skip("coingecko"):
            return

        try:
            cg = await self._coingecko.get_global()
            ts = cg["timestamp"]
            await self._data_store.store_index_value("btc_dominance", ts, cg["btc_dominance"])
            await self._data_store.store_index_value("eth_dominance", ts, cg["eth_dominance"])
            await self._data_store.store_index_value("total_market_cap", ts, cg["total_market_cap"])
            self._record_success("coingecko")
            log.info("external_data.poll_coingecko_complete")
        except Exception as e:
            await self._record_failure("coingecko", e)
