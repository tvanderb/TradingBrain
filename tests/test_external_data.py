"""Tests for Phase 5: External Market Data collection and storage."""

import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# --- Symbol Mapping ---

def test_symbol_mapping_all_pairs():
    from src.shell.symbol_mapping import KRAKEN_TO_BINANCE, to_binance_symbol, from_binance_symbol

    expected = {
        "BTC/USD": "BTCUSDT",
        "ETH/USD": "ETHUSDT",
        "SOL/USD": "SOLUSDT",
        "XRP/USD": "XRPUSDT",
        "DOGE/USD": "DOGEUSDT",
        "ADA/USD": "ADAUSDT",
        "LINK/USD": "LINKUSDT",
        "AVAX/USD": "AVAXUSDT",
        "DOT/USD": "DOTUSDT",
    }

    assert len(KRAKEN_TO_BINANCE) == 9
    for kraken, binance in expected.items():
        assert to_binance_symbol(kraken) == binance
        assert from_binance_symbol(binance) == kraken


def test_symbol_mapping_fallback():
    from src.shell.symbol_mapping import to_binance_symbol, from_binance_symbol

    # Fallback: strip /, replace USD with USDT
    assert to_binance_symbol("MATIC/USD") == "MATICUSDT"
    # Fallback reverse: strip USDT, insert /USD
    assert from_binance_symbol("MATICUSDT") == "MATIC/USD"


# --- BinanceFuturesClient ---

@pytest.mark.asyncio
async def test_binance_funding_rate_parsing():
    from src.shell.binance_futures import BinanceFuturesClient

    client = BinanceFuturesClient()
    mock_response = [
        {"symbol": "BTCUSDT", "fundingRate": "0.00010000", "fundingTime": 1709078400000}
    ]

    with patch.object(client, "_request", new_callable=AsyncMock, return_value=mock_response):
        result = await client.get_funding_rate("BTCUSDT")

    assert len(result) == 1
    assert result[0]["symbol"] == "BTCUSDT"
    assert result[0]["rate"] == 0.0001  # string -> float
    assert isinstance(result[0]["rate"], float)
    # Timestamp should be ISO format string (ms -> formatted)
    assert "2024" in result[0]["timestamp"]

    await client.close()


@pytest.mark.asyncio
async def test_binance_open_interest_parsing():
    from src.shell.binance_futures import BinanceFuturesClient

    client = BinanceFuturesClient()
    mock_response = {"symbol": "BTCUSDT", "openInterest": "12345.678", "time": 1709078400000}

    with patch.object(client, "_request", new_callable=AsyncMock, return_value=mock_response):
        result = await client.get_open_interest("BTCUSDT")

    assert result["symbol"] == "BTCUSDT"
    assert result["value"] == 12345.678  # string -> float
    assert isinstance(result["value"], float)

    await client.close()


@pytest.mark.asyncio
async def test_binance_funding_rate_history():
    from src.shell.binance_futures import BinanceFuturesClient

    client = BinanceFuturesClient()
    mock_response = [
        {"symbol": "ETHUSDT", "fundingRate": "0.00020000", "fundingTime": 1709078400000},
        {"symbol": "ETHUSDT", "fundingRate": "-0.00010000", "fundingTime": 1709107200000},
    ]

    with patch.object(client, "_request", new_callable=AsyncMock, return_value=mock_response):
        result = await client.get_funding_rate_history("ETHUSDT", limit=1000)

    assert len(result) == 2
    assert result[0]["rate"] == 0.0002
    assert result[1]["rate"] == -0.0001

    await client.close()


# --- FearGreedClient ---

@pytest.mark.asyncio
async def test_fear_greed_parsing():
    """Verify seconds timestamp gotcha and string->int value conversion."""
    from src.shell.fear_greed import FearGreedClient

    client = FearGreedClient()
    mock_response = {
        "data": [{"value": "73", "value_classification": "Greed", "timestamp": "1709078400"}]
    }

    with patch.object(client, "_request", new_callable=AsyncMock, return_value=mock_response):
        result = await client.get_current()

    assert result["value"] == 73  # string -> int
    assert isinstance(result["value"], int)
    assert result["classification"] == "Greed"
    # Timestamp is seconds, not ms — verify it's converted correctly
    assert "2024" in result["timestamp"]

    await client.close()


# --- CoinGeckoClient ---

@pytest.mark.asyncio
async def test_coingecko_global_parsing():
    from src.shell.coingecko import CoinGeckoClient

    client = CoinGeckoClient()
    mock_response = {
        "data": {
            "market_cap_percentage": {"btc": 52.5, "eth": 17.3},
            "total_market_cap": {"usd": 2500000000000},
        }
    }

    with patch.object(client, "_request", new_callable=AsyncMock, return_value=mock_response):
        result = await client.get_global()

    assert result["btc_dominance"] == 52.5
    assert result["eth_dominance"] == 17.3
    assert result["total_market_cap"] == 2500000000000
    assert "timestamp" in result

    await client.close()


@pytest.mark.asyncio
async def test_coingecko_api_key_header():
    """Verify optional API key is passed as header."""
    from src.shell.coingecko import CoinGeckoClient

    client_with_key = CoinGeckoClient(api_key="test-key-123")
    assert client_with_key._client.headers.get("x-cg-demo-api-key") == "test-key-123"

    client_no_key = CoinGeckoClient()
    assert "x-cg-demo-api-key" not in client_no_key._client.headers

    await client_with_key.close()
    await client_no_key.close()


# --- DataStore storage ---

@pytest.mark.asyncio
async def test_datastore_funding_rate_storage():
    from src.shell.database import Database
    from src.shell.data_store import DataStore
    from src.shell.config import DataConfig, ExternalDataConfig

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        db = Database(db_path)
        await db.connect()
        store = DataStore(db, DataConfig(), external_data_config=ExternalDataConfig())

        await store.store_funding_rate("BTCUSDT", "2024-02-28 08:00:00", 0.0001)
        await store.store_funding_rate("BTCUSDT", "2024-02-28 16:00:00", 0.00015)

        # Verify storage
        rows = await db.fetchall("SELECT * FROM funding_rates ORDER BY timestamp")
        assert len(rows) == 2
        assert rows[0]["rate"] == 0.0001
        assert rows[1]["rate"] == 0.00015

        # Verify deduplication (INSERT OR IGNORE)
        await store.store_funding_rate("BTCUSDT", "2024-02-28 08:00:00", 0.9999)
        rows = await db.fetchall("SELECT * FROM funding_rates")
        assert len(rows) == 2  # no new row
        assert rows[0]["rate"] == 0.0001  # original value preserved

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_datastore_funding_rate_batch():
    from src.shell.database import Database
    from src.shell.data_store import DataStore
    from src.shell.config import DataConfig, ExternalDataConfig

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        db = Database(db_path)
        await db.connect()
        store = DataStore(db, DataConfig(), external_data_config=ExternalDataConfig())

        batch = [
            ("BTCUSDT", "2024-02-28 00:00:00", 0.0001),
            ("BTCUSDT", "2024-02-28 08:00:00", 0.00015),
            ("ETHUSDT", "2024-02-28 00:00:00", 0.0002),
        ]
        await store.store_funding_rates_batch(batch)

        rows = await db.fetchall("SELECT * FROM funding_rates")
        assert len(rows) == 3

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_datastore_open_interest_storage():
    from src.shell.database import Database
    from src.shell.data_store import DataStore
    from src.shell.config import DataConfig, ExternalDataConfig

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        db = Database(db_path)
        await db.connect()
        store = DataStore(db, DataConfig(), external_data_config=ExternalDataConfig())

        await store.store_open_interest("BTCUSDT", "2024-02-28 12:00:00", 50000.5)

        rows = await db.fetchall("SELECT * FROM open_interest")
        assert len(rows) == 1
        assert rows[0]["value"] == 50000.5

        # Deduplication
        await store.store_open_interest("BTCUSDT", "2024-02-28 12:00:00", 99999.0)
        rows = await db.fetchall("SELECT * FROM open_interest")
        assert len(rows) == 1

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_datastore_index_value_storage():
    from src.shell.database import Database
    from src.shell.data_store import DataStore
    from src.shell.config import DataConfig, ExternalDataConfig

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        db = Database(db_path)
        await db.connect()
        store = DataStore(db, DataConfig(), external_data_config=ExternalDataConfig())

        await store.store_index_value("fear_greed", "2024-02-28 00:00:00", 73)
        await store.store_index_value("btc_dominance", "2024-02-28 12:00:00", 52.5)
        await store.store_index_value("eth_dominance", "2024-02-28 12:00:00", 17.3)
        await store.store_index_value("total_market_cap", "2024-02-28 12:00:00", 2.5e12)

        rows = await db.fetchall("SELECT * FROM index_values ORDER BY index_type")
        assert len(rows) == 4

        # Verify types are stored correctly
        fg = [r for r in rows if r["index_type"] == "fear_greed"][0]
        assert fg["value"] == 73

        await db.close()
    finally:
        os.unlink(db_path)


# --- ExternalDataCollector ---

@pytest.mark.asyncio
async def test_collector_consecutive_failure_tracking():
    from src.shell.external_data import ExternalDataCollector, MAX_CONSECUTIVE_FAILURES
    from src.shell.config import Config
    from unittest.mock import AsyncMock

    config = Config()
    data_store = AsyncMock()

    collector = ExternalDataCollector(config, data_store)

    # Simulate failures below threshold
    for i in range(MAX_CONSECUTIVE_FAILURES - 1):
        await collector._record_failure("binance_funding", Exception("test"))
    assert collector._consecutive_failures["binance_funding"] == MAX_CONSECUTIVE_FAILURES - 1
    assert not collector._should_skip("binance_funding")

    # One more failure hits threshold
    await collector._record_failure("binance_funding", Exception("test"))
    assert collector._consecutive_failures["binance_funding"] == MAX_CONSECUTIVE_FAILURES
    # Should skip and reset counter
    assert collector._should_skip("binance_funding")
    assert collector._consecutive_failures["binance_funding"] == 0

    # After reset, should not skip
    assert not collector._should_skip("binance_funding")

    await collector.close()


@pytest.mark.asyncio
async def test_collector_error_isolation():
    """Verify that one API failure doesn't affect others."""
    from src.shell.external_data import ExternalDataCollector
    from src.shell.config import Config

    config = Config()
    data_store = AsyncMock()

    collector = ExternalDataCollector(config, data_store)

    # Make binance fail
    collector._binance.get_funding_rate = AsyncMock(side_effect=Exception("Binance down"))

    # Poll should catch the exception and record failure
    await collector.poll_funding_rates()
    assert collector._consecutive_failures["binance_funding"] == 1
    # Other sources unaffected
    assert collector._consecutive_failures["coingecko"] == 0
    assert collector._consecutive_failures["fear_greed"] == 0

    await collector.close()


@pytest.mark.asyncio
async def test_collector_success_resets_failures():
    from src.shell.external_data import ExternalDataCollector
    from src.shell.config import Config

    config = Config()
    data_store = AsyncMock()

    collector = ExternalDataCollector(config, data_store)

    # Record some failures
    collector._consecutive_failures["fear_greed"] = 2

    # Mock successful fear_greed poll
    collector._fear_greed.get_current = AsyncMock(return_value={
        "value": 50, "classification": "Neutral", "timestamp": "2024-02-28 00:00:00"
    })

    await collector.poll_fear_greed()
    assert collector._consecutive_failures["fear_greed"] == 0

    await collector.close()


# --- ms_to_iso helper ---

def test_ms_to_iso():
    from src.shell.binance_futures import _ms_to_iso

    # 1709078400000 ms = 2024-02-28 00:00:00 UTC
    result = _ms_to_iso(1709078400000)
    assert result == "2024-02-28 00:00:00"


# --- Schema description ---

def test_schema_description_includes_new_tables():
    from src.statistics.readonly_db import get_schema_description

    schema = get_schema_description()
    assert "funding_rates" in schema
    assert "open_interest" in schema
    assert "index_values" in schema

    # Verify key columns documented
    assert "rate" in schema["funding_rates"]["columns"]
    assert "value" in schema["open_interest"]["columns"]
    assert "index_type" in schema["index_values"]["columns"]

    # Verify descriptions mention what the data means
    assert "funding" in schema["funding_rates"]["description"].lower()
    assert "open interest" in schema["open_interest"]["description"].lower()
    assert "fear_greed" in schema["index_values"]["description"]


# --- ExternalDataConfig ---

def test_external_data_config_defaults():
    from src.shell.config import ExternalDataConfig

    edc = ExternalDataConfig()
    assert edc.coingecko_api_key == ""
    assert edc.funding_rate_retention_days == 90
    assert edc.open_interest_retention_days == 30
    assert edc.index_value_retention_days == 90


def test_config_has_external_data():
    from src.shell.config import Config

    config = Config()
    assert hasattr(config, "external_data")
    assert config.external_data.funding_rate_retention_days == 90


# --- Observability ---

@pytest.mark.asyncio
async def test_collector_notifies_on_degraded_state():
    """Hitting MAX_CONSECUTIVE_FAILURES should fire a system_error notification."""
    from src.shell.external_data import ExternalDataCollector, MAX_CONSECUTIVE_FAILURES
    from src.shell.config import Config

    config = Config()
    data_store = AsyncMock()
    notifier = AsyncMock()

    collector = ExternalDataCollector(config, data_store)
    collector.set_notifier(notifier)

    # Record exactly MAX_CONSECUTIVE_FAILURES
    for _ in range(MAX_CONSECUTIVE_FAILURES):
        await collector._record_failure("coingecko", Exception("timeout"))

    # Notifier should have been called with system_error on the last one
    notifier.system_error.assert_called_once()
    call_args = notifier.system_error.call_args[0][0]
    assert "CoinGecko" in call_args
    assert "failed" in call_args

    await collector.close()


@pytest.mark.asyncio
async def test_collector_no_notification_below_threshold():
    """Failures below threshold should not trigger Telegram notification."""
    from src.shell.external_data import ExternalDataCollector, MAX_CONSECUTIVE_FAILURES
    from src.shell.config import Config

    config = Config()
    data_store = AsyncMock()
    notifier = AsyncMock()

    collector = ExternalDataCollector(config, data_store)
    collector.set_notifier(notifier)

    for _ in range(MAX_CONSECUTIVE_FAILURES - 1):
        await collector._record_failure("fear_greed", Exception("test"))

    notifier.system_error.assert_not_called()

    await collector.close()


@pytest.mark.asyncio
async def test_collector_activity_logging_on_failure():
    """Failures should log to activity timeline."""
    from src.shell.external_data import ExternalDataCollector
    from src.shell.config import Config

    config = Config()
    data_store = AsyncMock()
    activity = AsyncMock()

    collector = ExternalDataCollector(config, data_store)
    collector.set_activity_logger(activity)

    await collector._record_failure("binance_oi", Exception("connection refused"))

    activity.data.assert_called_once()
    call_args = activity.data.call_args
    assert "Binance Open Interest" in call_args[0][0]
    assert call_args[1]["severity"] == "warning"

    await collector.close()


@pytest.mark.asyncio
async def test_collector_activity_logging_on_backfill():
    """Backfill completion should log to activity timeline."""
    from src.shell.external_data import ExternalDataCollector
    from src.shell.config import Config

    config = Config()
    config.symbols = ["BTC/USD"]  # minimal for speed
    data_store = AsyncMock()
    activity = AsyncMock()

    collector = ExternalDataCollector(config, data_store)
    collector.set_activity_logger(activity)

    # Mock all API clients to return data
    collector._binance.get_funding_rate_history = AsyncMock(return_value=[
        {"symbol": "BTCUSDT", "timestamp": "2024-02-28 00:00:00", "rate": 0.0001}
    ])
    collector._binance.get_open_interest = AsyncMock(return_value={
        "symbol": "BTCUSDT", "timestamp": "2024-02-28 00:00:00", "value": 50000.0
    })
    collector._fear_greed.get_current = AsyncMock(return_value={
        "value": 50, "classification": "Neutral", "timestamp": "2024-02-28 00:00:00"
    })
    collector._coingecko.get_global = AsyncMock(return_value={
        "btc_dominance": 52.5, "eth_dominance": 17.3,
        "total_market_cap": 2.5e12, "timestamp": "2024-02-28 00:00:00"
    })

    await collector.backfill_on_startup()

    activity.data.assert_called_once()
    summary = activity.data.call_args[0][0]
    assert "backfill" in summary.lower()
    assert "funding" in summary.lower()

    await collector.close()
