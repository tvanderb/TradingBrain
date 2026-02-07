"""Integration test: Full pipeline from signal to paper trade."""

import asyncio
import os
from pathlib import Path

import numpy as np
import pandas as pd

from src.core.config import Config
from src.core.logging import setup_logging
from src.market.regime import classify_regime
from src.market.signals import RawSignal, SignalGenerator
from src.storage.database import Database
from src.storage.models import FeeSchedule
from src.storage.models import Signal as DbSignal
from src.storage.models import Trade
from src.storage import queries
from src.trading.order_manager import PaperTrader
from src.trading.risk_manager import RiskManager


async def run_test():
    config = Config.load()
    setup_logging("WARNING")  # Quiet for test output

    test_db_path = Path("/tmp/test_brain.db")
    if test_db_path.exists():
        test_db_path.unlink()

    db = Database(test_db_path)
    await db.connect()

    print("=== Integration Test: Full Pipeline ===\n")

    # 1. Synthetic market data
    np.random.seed(42)
    n = 200
    base_price = 50000
    returns = np.random.randn(n) * 0.002
    prices = base_price * np.cumprod(1 + returns)

    df = pd.DataFrame({
        "open": prices * (1 + np.random.randn(n) * 0.0005),
        "high": prices * (1 + abs(np.random.randn(n) * 0.001)),
        "low": prices * (1 - abs(np.random.randn(n) * 0.001)),
        "close": prices,
        "volume": np.random.rand(n) * 10 + 1,
    })
    print(f"1. Synthetic data: {n} bars, price ${prices.min():.0f}-${prices.max():.0f}")

    # 2. Regime classification
    regime = classify_regime(df)
    print(f"2. Regime: {regime.regime.value} (confidence={regime.confidence:.2f})")

    # 3. Signal generation
    signal_gen = SignalGenerator(config.strategy)
    signal = signal_gen.generate(df, "BTC/USD")
    if signal:
        print(f"3. Signal: {signal.direction} (strength={signal.strength:.3f})")
        print(f"   Reason: {signal.reasoning}")
    else:
        signal = RawSignal("BTC/USD", "trend", 0.75, "long", "Forced test signal")
        print(f"3. No natural signal - using test signal (strength=0.75)")

    # 4. Risk check
    fees = FeeSchedule(maker_fee_pct=0.16, taker_fee_pct=0.26)
    risk = RiskManager(config.risk, fees)
    portfolio = 1000.0
    trade_usd = risk.calculate_position_size(portfolio, signal.strength)
    check = risk.check_trade("BTC/USD", "buy", trade_usd, portfolio, [])
    print(f"4. Trade size: ${trade_usd:.2f} | Risk: {'PASS' if check.passed else 'FAIL'}")

    # 5. Paper trade: buy
    paper = PaperTrader(initial_balance_usd=1000.0, fees=fees)
    price = prices[-1]
    qty = trade_usd / price
    buy = paper.execute("BTC/USD", "buy", qty, price)
    print(f"5. BUY  @ ${buy.filled_price:.2f} | Fee: ${buy.commission:.4f} | Balance: ${paper.balance_usd:.2f}")

    # 6. Paper trade: sell at +2%
    exit_price = price * 1.02
    sell = paper.execute("BTC/USD", "sell", qty, exit_price)
    gross = (sell.filled_price - buy.filled_price) * qty
    total_fees = buy.commission + sell.commission
    net = gross - total_fees
    print(f"6. SELL @ ${sell.filled_price:.2f} | Fee: ${sell.commission:.4f} | Balance: ${paper.balance_usd:.2f}")
    print(f"   Gross: ${gross:.4f} | Fees: ${total_fees:.4f} ({total_fees/gross*100:.1f}% of gross) | Net: ${net:.4f}")

    # 7. Database operations
    sig_id = await queries.insert_signal(db, DbSignal(
        symbol="BTC/USD", signal_type="trend", strength=0.75,
        direction="long", reasoning="Test",
    ))
    trade = Trade(symbol="BTC/USD", side="buy", qty=qty, price=price, order_type="market")
    trade_id = await queries.insert_trade(db, trade)
    await queries.update_trade_fill(db, trade_id, buy.filled_price, buy.exchange_order_id, buy.commission)
    await queries.update_trade_pnl(db, trade_id, net)
    recent = await queries.get_recent_trades(db, limit=5)
    print(f"7. DB: {len(recent)} trade(s) stored, P&L: ${recent[0].pnl:.4f}")

    # 8. Fee storage
    await queries.insert_fee_check(db, fees)
    latest = await queries.get_latest_fees(db)
    print(f"8. Fees stored: maker={latest.maker_fee_pct}%, taker={latest.taker_fee_pct}%")

    # 9. Stop loss / take profit calculation
    sl, tp = risk.get_stop_take_profit(buy.filled_price, "long")
    print(f"9. SL: ${sl:.2f} | TP: ${tp:.2f}")

    # 10. Edge cases
    # Try to buy more than balance
    huge_buy = paper.execute("BTC/USD", "buy", 1.0, 50000.0)
    assert not huge_buy.success, "Should fail on insufficient balance"
    print(f"10. Overdraft blocked: {huge_buy.message}")

    # Try to sell more than holdings
    bad_sell = paper.execute("ETH/USD", "sell", 1.0, 3000.0)
    assert not bad_sell.success, "Should fail on no holdings"
    print(f"    No-holdings blocked: {bad_sell.message}")

    # Risk: exceed daily trade limit
    for _ in range(20):
        risk.record_trade_result(0.5)
    check = risk.check_trade("BTC/USD", "buy", 20.0, 1000.0, [])
    assert not check.passed, "Should fail on daily trade limit"
    print(f"    Daily limit blocked: {check.reason}")

    print("\n=== ALL TESTS PASSED ===")
    await db.close()


if __name__ == "__main__":
    asyncio.run(run_test())
