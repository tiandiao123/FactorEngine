from __future__ import annotations

from dataflow.okx.trades.worker import TradesWorker
from dataflow.symbols import SymbolDiff


def test_okx_trades_worker_apply_symbol_diff_adds_and_removes_accumulators():
    worker = TradesWorker(symbols=["BTC-USDT-SWAP", "ETH-USDT-SWAP"])
    diff = SymbolDiff(
        version="v2",
        added=frozenset({"SOL-USDT-SWAP"}),
        removed=frozenset({"ETH-USDT-SWAP"}),
        unchanged=frozenset({"BTC-USDT-SWAP"}),
        target=frozenset({"BTC-USDT-SWAP", "SOL-USDT-SWAP"}),
    )

    worker.apply_symbol_diff(diff)

    assert worker.active_symbols() == {"BTC-USDT-SWAP", "SOL-USDT-SWAP"}
    assert worker.symbols == ["BTC-USDT-SWAP", "SOL-USDT-SWAP"]
    assert "SOL-USDT-SWAP" in worker.accumulators
    assert "ETH-USDT-SWAP" not in worker.accumulators
    assert worker._rebuild_requested.is_set()


def test_okx_trades_worker_removed_symbol_late_trade_is_ignored():
    worker = TradesWorker(symbols=["BTC-USDT-SWAP", "ETH-USDT-SWAP"])
    worker.apply_symbol_diff(
        SymbolDiff(
            version="v2",
            added=frozenset(),
            removed=frozenset({"ETH-USDT-SWAP"}),
            unchanged=frozenset({"BTC-USDT-SWAP"}),
            target=frozenset({"BTC-USDT-SWAP"}),
        )
    )

    worker._on_trade("ETH-USDT-SWAP", 3800.0, 1.0, "buy", 0)
    worker._on_trade("BTC-USDT-SWAP", 97500.0, 1.0, "buy", 0)

    round_bars, round_trades = worker.flush()
    assert "ETH-USDT-SWAP" not in round_bars
    assert "ETH-USDT-SWAP" not in round_trades
    assert round_trades["BTC-USDT-SWAP"]["trade_count"] == 1
    assert worker.trade_count == 1


def test_okx_trades_worker_flush_uses_active_symbol_snapshot_after_diff():
    worker = TradesWorker(symbols=["BTC-USDT-SWAP", "ETH-USDT-SWAP"])
    worker._on_trade("ETH-USDT-SWAP", 3800.0, 1.0, "buy", 0)
    worker.apply_symbol_diff(
        SymbolDiff(
            version="v2",
            added=frozenset({"SOL-USDT-SWAP"}),
            removed=frozenset({"ETH-USDT-SWAP"}),
            unchanged=frozenset({"BTC-USDT-SWAP"}),
            target=frozenset({"BTC-USDT-SWAP", "SOL-USDT-SWAP"}),
        )
    )

    worker._on_trade("SOL-USDT-SWAP", 180.0, 2.0, "sell", 0)
    round_bars, round_trades = worker.flush()

    assert set(round_bars) == {"BTC-USDT-SWAP", "SOL-USDT-SWAP"}
    assert set(round_trades) == {"BTC-USDT-SWAP", "SOL-USDT-SWAP"}
    assert round_trades["SOL-USDT-SWAP"]["trade_count"] == 1
    assert worker.stats()["symbols"] == 2
    assert worker.stats()["rebuild_count"] == 0
