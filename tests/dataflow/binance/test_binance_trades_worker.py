"""Tests for BinanceTradesWorker — flush, accumulation, and stats.

These tests exercise the Worker without opening actual WebSocket
connections by directly calling _on_trade() and flush().
"""

import math
from unittest.mock import patch

import numpy as np
import pytest

from dataflow.symbols import SymbolDiff
from dataflow.binance.trades.worker import BinanceTradesWorker
from dataflow.trades.accumulator import EventTimeTradesAccumulator


class TestBinanceTradesWorkerFlush:
    """Test BinanceTradesWorker.flush() returns correct (round_bars, round_trades)."""

    def test_flush_with_trades(self):
        worker = BinanceTradesWorker(symbols=["BTC-USDT", "ETH-USDT"])
        worker.set_freq(1.0)

        worker._on_trade("BTC-USDT", 97500.0, 0.5, "buy", 1714838400000)
        worker._on_trade("BTC-USDT", 97600.0, 0.3, "sell", 1714838400100)
        worker._on_trade("ETH-USDT", 3800.0, 2.0, "buy", 1714838400050)

        round_bars, round_trades = worker.flush()

        assert "BTC-USDT" in round_bars
        assert "ETH-USDT" in round_bars
        assert "BTC-USDT" in round_trades
        assert "ETH-USDT" in round_trades

        btc_bar = round_bars["BTC-USDT"]
        assert btc_bar.shape == (8,)
        assert btc_bar[1] == pytest.approx(97500.0)  # open
        assert btc_bar[2] == pytest.approx(97600.0)  # high
        assert btc_bar[3] == pytest.approx(97500.0)  # low
        assert btc_bar[4] == pytest.approx(97600.0)  # close (last trade)
        assert btc_bar[5] == pytest.approx(0.8)       # volume (0.5 + 0.3)

        btc_trades = round_trades["BTC-USDT"]
        assert btc_trades["buy_vol"] == pytest.approx(0.5)
        assert btc_trades["sell_vol"] == pytest.approx(0.3)
        assert btc_trades["trade_count"] == 2

    def test_flush_resets(self):
        worker = BinanceTradesWorker(symbols=["BTC-USDT"])
        worker.set_freq(1.0)

        worker._on_trade("BTC-USDT", 97500.0, 1.0, "buy", 1714838400000)
        _, trades1 = worker.flush()
        assert trades1["BTC-USDT"]["trade_count"] == 1

        _, trades2 = worker.flush()
        assert trades2["BTC-USDT"]["trade_count"] == 0
        assert trades2["BTC-USDT"]["vwap"] == pytest.approx(97500.0)

    def test_flush_no_trades_nan(self):
        worker = BinanceTradesWorker(symbols=["SOL-USDT"])
        round_bars, round_trades = worker.flush()

        assert round_trades["SOL-USDT"]["trade_count"] == 0
        assert math.isnan(round_trades["SOL-USDT"]["vwap"])
        assert math.isnan(round_trades["SOL-USDT"]["open"])
        assert math.isnan(float(round_bars["SOL-USDT"][1]))

    def test_trade_count_and_flush_count(self):
        worker = BinanceTradesWorker(symbols=["BTC-USDT"])
        assert worker.trade_count == 0
        assert worker.flush_count == 0

        worker._on_trade("BTC-USDT", 97500.0, 1.0, "buy", 0)
        worker._on_trade("BTC-USDT", 97600.0, 1.0, "sell", 0)
        assert worker.trade_count == 2

        worker.flush()
        worker.flush()
        assert worker.flush_count == 2

    def test_unknown_symbol_ignored(self):
        worker = BinanceTradesWorker(symbols=["BTC-USDT"])
        worker._on_trade("UNKNOWN-USDT", 100.0, 1.0, "buy", 0)
        assert worker.trade_count == 0

    def test_stats(self):
        worker = BinanceTradesWorker(symbols=["BTC-USDT", "ETH-USDT"])
        s = worker.stats()
        assert s["symbols"] == 2
        assert s["trade_count"] == 0
        assert s["flush_count"] == 0
        assert s["rebuild_count"] == 0

    def test_vwap_calculation(self):
        worker = BinanceTradesWorker(symbols=["BTC-USDT"])
        worker._on_trade("BTC-USDT", 100.0, 3.0, "buy", 0)
        worker._on_trade("BTC-USDT", 200.0, 1.0, "sell", 0)

        _, trades = worker.flush()
        expected_vwap = (100.0 * 3.0 + 200.0 * 1.0) / (3.0 + 1.0)
        assert trades["BTC-USDT"]["vwap"] == pytest.approx(expected_vwap)

    def test_exchange_time_accumulators_created(self):
        worker = BinanceTradesWorker(
            symbols=["BTC-USDT", "ETH-USDT"],
            trade_time_mode="exchange_time",
            freq_seconds=1.0,
            allowed_lateness_ms=500,
        )

        assert all(
            isinstance(acc, EventTimeTradesAccumulator)
            for acc in worker.accumulators.values()
        )

    def test_exchange_time_flush_finalizes_single_target_bucket(self):
        worker = BinanceTradesWorker(
            symbols=["BTC-USDT", "ETH-USDT"],
            trade_time_mode="exchange_time",
            freq_seconds=1.0,
            allowed_lateness_ms=500,
        )

        with patch("dataflow.binance.trades.worker.time.time", return_value=10_000.150):
            worker._on_trade("BTC-USDT", 100.0, 1.0, "buy", 10_000_100)
            worker._on_trade("ETH-USDT", 200.0, 2.0, "sell", 10_000_200)

        assert worker.flush(10_001_000) is None

        result = worker.flush(10_002_000)
        assert result is not None
        bucket_ts_ms, bars, trades = result
        assert bucket_ts_ms == 10_000_000
        assert set(bars) == {"BTC-USDT", "ETH-USDT"}
        assert bars["BTC-USDT"][0] == pytest.approx(10_000_000)
        assert bars["BTC-USDT"][4] == pytest.approx(100.0)
        assert bars["ETH-USDT"][4] == pytest.approx(200.0)
        assert trades["BTC-USDT"]["bucket_time_basis"] == "exchange_time"
        assert trades["BTC-USDT"]["allowed_lateness_ms"] == 500

        assert worker.flush(10_002_100) is None

    def test_exchange_time_worker_drops_trade_for_emitted_bucket(self):
        worker = BinanceTradesWorker(
            symbols=["BTC-USDT"],
            trade_time_mode="exchange_time",
            freq_seconds=1.0,
            allowed_lateness_ms=500,
        )

        with patch("dataflow.binance.trades.worker.time.time", return_value=10_000.150):
            worker._on_trade("BTC-USDT", 100.0, 1.0, "buy", 10_000_100)

        assert worker.flush(10_002_000) is not None

        with patch("dataflow.binance.trades.worker.time.time", return_value=10_002.100):
            worker._on_trade("BTC-USDT", 999.0, 1.0, "buy", 10_000_200)

        assert worker.trade_count == 1
        assert worker.stats()["dropped_late_trade_count"] == 1


class TestBinanceTradesWorkerDynamicSymbols:
    def test_apply_symbol_diff_adds_and_removes_accumulators(self):
        worker = BinanceTradesWorker(symbols=["BTC-USDT", "ETH-USDT"])
        diff = SymbolDiff(
            version="v2",
            added=frozenset({"SOL-USDT"}),
            removed=frozenset({"ETH-USDT"}),
            unchanged=frozenset({"BTC-USDT"}),
            target=frozenset({"BTC-USDT", "SOL-USDT"}),
        )

        worker.apply_symbol_diff(diff)

        assert worker.active_symbols() == {"BTC-USDT", "SOL-USDT"}
        assert worker.symbols == ["BTC-USDT", "SOL-USDT"]
        assert "SOL-USDT" in worker.accumulators
        assert "ETH-USDT" not in worker.accumulators
        assert worker._rebuild_requested.is_set()

    def test_removed_symbol_late_trade_is_ignored(self):
        worker = BinanceTradesWorker(symbols=["BTC-USDT", "ETH-USDT"])
        diff = SymbolDiff(
            version="v2",
            added=frozenset(),
            removed=frozenset({"ETH-USDT"}),
            unchanged=frozenset({"BTC-USDT"}),
            target=frozenset({"BTC-USDT"}),
        )
        worker.apply_symbol_diff(diff)

        worker._on_trade("ETH-USDT", 3800.0, 1.0, "buy", 0)
        worker._on_trade("BTC-USDT", 97500.0, 1.0, "buy", 0)

        round_bars, round_trades = worker.flush()
        assert "ETH-USDT" not in round_bars
        assert "ETH-USDT" not in round_trades
        assert round_trades["BTC-USDT"]["trade_count"] == 1
        assert worker.trade_count == 1

    def test_flush_uses_active_symbol_snapshot_after_diff(self):
        worker = BinanceTradesWorker(symbols=["BTC-USDT", "ETH-USDT"])
        worker._on_trade("ETH-USDT", 3800.0, 1.0, "buy", 0)
        worker.apply_symbol_diff(
            SymbolDiff(
                version="v2",
                added=frozenset({"SOL-USDT"}),
                removed=frozenset({"ETH-USDT"}),
                unchanged=frozenset({"BTC-USDT"}),
                target=frozenset({"BTC-USDT", "SOL-USDT"}),
            )
        )

        worker._on_trade("SOL-USDT", 180.0, 2.0, "sell", 0)
        round_bars, round_trades = worker.flush()

        assert set(round_bars) == {"BTC-USDT", "SOL-USDT"}
        assert set(round_trades) == {"BTC-USDT", "SOL-USDT"}
        assert round_trades["SOL-USDT"]["trade_count"] == 1

    def test_apply_symbol_diff_requests_rebuild_without_running_loop(self):
        worker = BinanceTradesWorker(symbols=["BTC-USDT"])
        worker.apply_symbol_diff(
            SymbolDiff(
                version="v2",
                added=frozenset({"ETH-USDT"}),
                removed=frozenset(),
                unchanged=frozenset({"BTC-USDT"}),
                target=frozenset({"BTC-USDT", "ETH-USDT"}),
            )
        )

        assert worker._rebuild_requested.is_set()
        assert worker.stats()["symbols"] == 2


class TestBinanceTradesWorkerWithEngine:
    """Test that BinanceTradesWorker output is compatible with Engine._runtime_loop."""

    def test_flush_compatible_with_runtime_loop(self):
        """The (round_bars, round_trades) format must be consumable by Engine."""
        worker = BinanceTradesWorker(symbols=["BTC-USDT", "ETH-USDT"])
        worker.set_freq(1.0)

        worker._on_trade("BTC-USDT", 97500.0, 0.5, "buy", 1714838400000)
        worker._on_trade("ETH-USDT", 3800.0, 2.0, "sell", 1714838400050)

        round_bars, round_trades = worker.flush()

        for sym in ("BTC-USDT", "ETH-USDT"):
            bar = round_bars[sym]
            assert bar.dtype == np.float64
            assert bar.shape == (8,)
            ts = int(bar[0])
            assert ts > 0
            close = float(bar[4])
            assert close > 0

            trades = round_trades[sym]
            assert "vwap" in trades
            assert "buy_vol" in trades
            assert "sell_vol" in trades
            assert "trade_count" in trades
