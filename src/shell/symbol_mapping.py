"""Symbol mapping — Kraken pairs to Binance Futures symbols."""

from __future__ import annotations

KRAKEN_TO_BINANCE: dict[str, str] = {
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

BINANCE_TO_KRAKEN: dict[str, str] = {v: k for k, v in KRAKEN_TO_BINANCE.items()}


def to_binance_symbol(kraken_symbol: str) -> str:
    """Convert a Kraken pair to a Binance Futures symbol.

    Falls back to stripping '/' and replacing 'USD' with 'USDT'.
    """
    if kraken_symbol in KRAKEN_TO_BINANCE:
        return KRAKEN_TO_BINANCE[kraken_symbol]
    return kraken_symbol.replace("/", "").replace("USD", "USDT")


def from_binance_symbol(binance_symbol: str) -> str:
    """Convert a Binance Futures symbol back to a Kraken pair.

    Falls back to removing 'USDT', inserting '/' before 'USD'.
    """
    if binance_symbol in BINANCE_TO_KRAKEN:
        return BINANCE_TO_KRAKEN[binance_symbol]
    base = binance_symbol.replace("USDT", "")
    return f"{base}/USD"
