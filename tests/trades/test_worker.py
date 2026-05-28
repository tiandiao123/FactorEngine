"""Tests for TradesWorker.

Unit tests mock the collector; integration test connects to live OKX WS.
"""

from __future__ import annotations

import math
import time
import threading
from unittest.mock import patch, MagicMock

import pytest

from dataflow.okx.trades.worker import TradesWorker
from dataflow.okx.trades.accumulator import TradesAccumulator
from dataflow.trades.accumulator import EventTimeTradesAccumulator


# ── unit tests (no network) ─────────────────────────────────────


class TestTradesWorkerUnit:
    def test_accumulators_created(self):
        w = TradesWorker(symbols=["BTC-USDT-SWAP", "ETH-USDT-SWAP"])
        assert set(w.accumulators.keys()) == {"BTC-USDT-SWAP", "ETH-USDT-SWAP"}
        assert all(isinstance(a, TradesAccumulator) for a in w.accumulators.values())

    def test_on_trade_routes_to_accumulator(self):
        w = TradesWorker(symbols=["BTC-USDT-SWAP", "ETH-USDT-SWAP"])
        w._on_trade("BTC-USDT-SWAP", 67000.0, 0.5, "buy", 1714704000000)
        w._on_trade("BTC-USDT-SWAP", 67100.0, 1.0, "sell", 1714704001000)
        w._on_trade("ETH-USDT-SWAP", 3500.0, 2.0, "buy", 1714704000000)

        btc_snap = w.accumulators["BTC-USDT-SWAP"].snapshot_and_reset()
        assert btc_snap["trade_count"] == 2
        assert btc_snap["buy_vol"] == pytest.approx(0.5)
        assert btc_snap["sell_vol"] == pytest.approx(1.0)
        # vwap = (67000*0.5 + 67100*1.0) / (0.5+1.0) = 100600/1.5 = 67066.67
        assert btc_snap["vwap"] == pytest.approx(100600.0 / 1.5)

        eth_snap = w.accumulators["ETH-USDT-SWAP"].snapshot_and_reset()
        assert eth_snap["trade_count"] == 1
        assert eth_snap["buy_vol"] == pytest.approx(2.0)

    def test_on_trade_unknown_symbol_ignored(self):
        w = TradesWorker(symbols=["BTC-USDT-SWAP"])
        w._on_trade("UNKNOWN-SWAP", 100.0, 1.0, "buy", 123)
        assert w.trade_count == 0
        btc_snap = w.accumulators["BTC-USDT-SWAP"].snapshot_and_reset()
        assert btc_snap["trade_count"] == 0

    def test_trade_count(self):
        w = TradesWorker(symbols=["BTC-USDT-SWAP"])
        assert w.trade_count == 0
        w._on_trade("BTC-USDT-SWAP", 100.0, 1.0, "buy", 123)
        w._on_trade("BTC-USDT-SWAP", 100.0, 1.0, "sell", 456)
        assert w.trade_count == 2

    def test_stats(self):
        w = TradesWorker(symbols=["A", "B", "C"])
        s = w.stats()
        assert s["trade_count"] == 0
        assert s["symbols"] == 3
        assert s["ws_msg_count"] == 0

    def test_snapshot_and_reset_independence(self):
        """After snapshot_and_reset, new trades go to fresh bucket."""
        w = TradesWorker(symbols=["BTC-USDT-SWAP"])
        w._on_trade("BTC-USDT-SWAP", 100.0, 1.0, "buy", 1000)
        snap1 = w.accumulators["BTC-USDT-SWAP"].snapshot_and_reset()
        assert snap1["trade_count"] == 1

        w._on_trade("BTC-USDT-SWAP", 200.0, 2.0, "sell", 2000)
        snap2 = w.accumulators["BTC-USDT-SWAP"].snapshot_and_reset()
        assert snap2["trade_count"] == 1
        assert snap2["vwap"] == pytest.approx(200.0)
        assert snap2["sell_vol"] == pytest.approx(2.0)

    def test_exchange_time_accumulators_created(self):
        w = TradesWorker(
            symbols=["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
            trade_time_mode="exchange_time",
            freq_seconds=1.0,
            allowed_lateness_ms=500,
        )

        assert all(
            isinstance(acc, EventTimeTradesAccumulator)
            for acc in w.accumulators.values()
        )

    def test_exchange_time_flush_finalizes_single_target_bucket(self):
        w = TradesWorker(
            symbols=["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
            trade_time_mode="exchange_time",
            freq_seconds=1.0,
            allowed_lateness_ms=500,
        )

        with patch("dataflow.okx.trades.worker.time.time", return_value=10_000.150):
            w._on_trade("BTC-USDT-SWAP", 100.0, 1.0, "buy", 10_000_100)
            w._on_trade("ETH-USDT-SWAP", 200.0, 2.0, "sell", 10_000_200)

        assert w.flush(10_001_000) is None

        result = w.flush(10_002_000)
        assert result is not None
        bucket_ts_ms, bars, trades = result
        assert bucket_ts_ms == 10_000_000
        assert set(bars) == {"BTC-USDT-SWAP", "ETH-USDT-SWAP"}
        assert bars["BTC-USDT-SWAP"][0] == pytest.approx(10_000_000)
        assert bars["BTC-USDT-SWAP"][4] == pytest.approx(100.0)
        assert bars["ETH-USDT-SWAP"][4] == pytest.approx(200.0)
        assert trades["BTC-USDT-SWAP"]["bucket_time_basis"] == "exchange_time"
        assert trades["BTC-USDT-SWAP"]["allowed_lateness_ms"] == 500

        assert w.flush(10_002_100) is None

    def test_exchange_time_worker_drops_trade_for_emitted_bucket(self):
        w = TradesWorker(
            symbols=["BTC-USDT-SWAP"],
            trade_time_mode="exchange_time",
            freq_seconds=1.0,
            allowed_lateness_ms=500,
        )

        with patch("dataflow.okx.trades.worker.time.time", return_value=10_000.150):
            w._on_trade("BTC-USDT-SWAP", 100.0, 1.0, "buy", 10_000_100)

        assert w.flush(10_002_000) is not None

        with patch("dataflow.okx.trades.worker.time.time", return_value=10_002.100):
            w._on_trade("BTC-USDT-SWAP", 999.0, 1.0, "buy", 10_000_200)

        assert w.trade_count == 1
        assert w.stats()["dropped_late_trade_count"] == 1


# ── integration test (live WS) ──────────────────────────────────


@pytest.mark.network
class TestTradesWorkerLive:
    def test_start_stop_collects_trades(self):
        """Start worker, let it run for a few seconds, verify trades arrived."""
        w = TradesWorker(symbols=["BTC-USDT-SWAP"])
        w.start()
        time.sleep(10)
        w.stop()

        assert w.trade_count > 0, "No trades collected — check network"
        snap = w.accumulators["BTC-USDT-SWAP"].snapshot_and_reset()
        assert snap["trade_count"] > 0
        assert not math.isnan(snap["vwap"])
        assert snap["vwap"] > 0
        assert snap["buy_vol"] + snap["sell_vol"] > 0
