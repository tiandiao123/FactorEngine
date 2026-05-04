"""Unit tests for TradesAccumulator."""

from __future__ import annotations

import math
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from dataflow.okx.trades.accumulator import TradesAccumulator


# ── helpers ──────────────────────────────────────────────────────


def _approx(val: float, expected: float, rel: float = 1e-9) -> bool:
    if math.isnan(expected):
        return math.isnan(val)
    return abs(val - expected) <= rel * abs(expected) + 1e-15


# ── basic tests ──────────────────────────────────────────────────


class TestBasic:
    def test_empty_snapshot(self):
        acc = TradesAccumulator()
        snap = acc.snapshot_and_reset()
        assert math.isnan(snap["vwap"])
        assert snap["buy_vol"] == 0.0
        assert snap["sell_vol"] == 0.0
        assert snap["trade_count"] == 0
        assert math.isnan(snap["open"])
        assert math.isnan(snap["high"])
        assert math.isnan(snap["low"])
        assert math.isnan(snap["close"])
        assert snap["volume"] == 0.0

    def test_single_buy(self):
        acc = TradesAccumulator()
        acc.update(100.0, 2.0, "buy")
        snap = acc.snapshot_and_reset()
        assert snap["vwap"] == pytest.approx(100.0)
        assert snap["buy_vol"] == pytest.approx(2.0)
        assert snap["sell_vol"] == 0.0
        assert snap["trade_count"] == 1
        assert snap["open"] == pytest.approx(100.0)
        assert snap["high"] == pytest.approx(100.0)
        assert snap["low"] == pytest.approx(100.0)
        assert snap["close"] == pytest.approx(100.0)
        assert snap["volume"] == pytest.approx(2.0)

    def test_single_sell(self):
        acc = TradesAccumulator()
        acc.update(200.0, 5.0, "sell")
        snap = acc.snapshot_and_reset()
        assert snap["vwap"] == pytest.approx(200.0)
        assert snap["buy_vol"] == 0.0
        assert snap["sell_vol"] == pytest.approx(5.0)
        assert snap["trade_count"] == 1
        assert snap["open"] == pytest.approx(200.0)
        assert snap["close"] == pytest.approx(200.0)
        assert snap["volume"] == pytest.approx(5.0)

    def test_multiple_trades_vwap(self):
        acc = TradesAccumulator()
        # trade 1: px=100, sz=3 → pv=300
        # trade 2: px=200, sz=1 → pv=200
        # vwap = 500/4 = 125
        acc.update(100.0, 3.0, "buy")
        acc.update(200.0, 1.0, "sell")
        snap = acc.snapshot_and_reset()
        assert snap["vwap"] == pytest.approx(125.0)
        assert snap["buy_vol"] == pytest.approx(3.0)
        assert snap["sell_vol"] == pytest.approx(1.0)
        assert snap["trade_count"] == 2
        assert snap["open"] == pytest.approx(100.0)
        assert snap["high"] == pytest.approx(200.0)
        assert snap["low"] == pytest.approx(100.0)
        assert snap["close"] == pytest.approx(200.0)
        assert snap["volume"] == pytest.approx(4.0)

    def test_unknown_side_ignored(self):
        acc = TradesAccumulator()
        acc.update(100.0, 1.0, "buy")
        acc.update(100.0, 1.0, "unknown")
        snap = acc.snapshot_and_reset()
        assert snap["buy_vol"] == pytest.approx(1.0)
        assert snap["sell_vol"] == 0.0
        # unknown side still counted in vwap and trade_count
        assert snap["trade_count"] == 2
        assert snap["vwap"] == pytest.approx(100.0)


# ── reset semantics ─────────────────────────────────────────────


class TestReset:
    def test_reset_clears_state(self):
        acc = TradesAccumulator()
        acc.update(50.0, 10.0, "buy")
        _ = acc.snapshot_and_reset()
        snap2 = acc.snapshot_and_reset()
        assert snap2["buy_vol"] == 0.0
        assert snap2["sell_vol"] == 0.0
        assert snap2["trade_count"] == 0
        # OHLC + vwap forward-filled from last known close (50.0)
        assert snap2["open"] == 50.0
        assert snap2["high"] == 50.0
        assert snap2["low"] == 50.0
        assert snap2["close"] == 50.0
        assert snap2["vwap"] == 50.0
        assert snap2["volume"] == 0.0

    def test_forward_fill_no_prior_trades(self):
        """First-ever snapshot with no trades returns NaN (no last_close)."""
        acc = TradesAccumulator()
        snap = acc.snapshot_and_reset()
        assert math.isnan(snap["open"])
        assert math.isnan(snap["close"])
        assert math.isnan(snap["vwap"])
        assert snap["trade_count"] == 0

    def test_forward_fill_multiple_empty_buckets(self):
        """Consecutive empty buckets keep forward-filling the same close."""
        acc = TradesAccumulator()
        acc.update(100.0, 1.0, "buy")
        _ = acc.snapshot_and_reset()
        # Two consecutive empty buckets
        snap2 = acc.snapshot_and_reset()
        snap3 = acc.snapshot_and_reset()
        assert snap2["close"] == 100.0
        assert snap3["close"] == 100.0
        assert snap2["vwap"] == 100.0
        assert snap3["vwap"] == 100.0
        assert snap2["trade_count"] == 0
        assert snap3["trade_count"] == 0

    def test_accumulate_after_reset(self):
        acc = TradesAccumulator()
        acc.update(100.0, 1.0, "buy")
        _ = acc.snapshot_and_reset()

        acc.update(200.0, 2.0, "sell")
        acc.update(300.0, 3.0, "buy")
        snap = acc.snapshot_and_reset()
        # vwap = (200*2 + 300*3) / (2+3) = 1300/5 = 260
        assert snap["vwap"] == pytest.approx(260.0)
        assert snap["buy_vol"] == pytest.approx(3.0)
        assert snap["sell_vol"] == pytest.approx(2.0)
        assert snap["trade_count"] == 2
        assert snap["open"] == pytest.approx(200.0)
        assert snap["high"] == pytest.approx(300.0)
        assert snap["low"] == pytest.approx(200.0)
        assert snap["close"] == pytest.approx(300.0)
        assert snap["volume"] == pytest.approx(5.0)

    def test_multiple_resets(self):
        acc = TradesAccumulator()
        for _ in range(5):
            snap = acc.snapshot_and_reset()
            assert snap["trade_count"] == 0
            assert math.isnan(snap["vwap"])


# ── edge cases ───────────────────────────────────────────────────


class TestEdgeCases:
    def test_zero_size_trade(self):
        acc = TradesAccumulator()
        acc.update(100.0, 0.0, "buy")
        snap = acc.snapshot_and_reset()
        # sum_v == 0 → vwap = NaN
        assert math.isnan(snap["vwap"])
        assert snap["buy_vol"] == 0.0
        assert snap["trade_count"] == 1

    def test_very_small_size(self):
        acc = TradesAccumulator()
        acc.update(67000.0, 1e-10, "sell")
        snap = acc.snapshot_and_reset()
        assert snap["vwap"] == pytest.approx(67000.0)
        assert snap["sell_vol"] == pytest.approx(1e-10)

    def test_large_volume(self):
        acc = TradesAccumulator()
        for _ in range(10000):
            acc.update(50000.0, 1.0, "buy")
        snap = acc.snapshot_and_reset()
        assert snap["vwap"] == pytest.approx(50000.0)
        assert snap["buy_vol"] == pytest.approx(10000.0)
        assert snap["trade_count"] == 10000

    def test_all_buys(self):
        acc = TradesAccumulator()
        acc.update(100.0, 1.0, "buy")
        acc.update(100.0, 2.0, "buy")
        snap = acc.snapshot_and_reset()
        assert snap["buy_vol"] == pytest.approx(3.0)
        assert snap["sell_vol"] == 0.0

    def test_all_sells(self):
        acc = TradesAccumulator()
        acc.update(100.0, 1.0, "sell")
        acc.update(100.0, 2.0, "sell")
        snap = acc.snapshot_and_reset()
        assert snap["buy_vol"] == 0.0
        assert snap["sell_vol"] == pytest.approx(3.0)


# ── thread safety ────────────────────────────────────────────────


class TestThreadSafety:
    def test_concurrent_updates(self):
        """Many threads calling update() concurrently."""
        acc = TradesAccumulator()
        n_threads = 8
        n_per_thread = 5000

        def worker(side: str):
            for _ in range(n_per_thread):
                acc.update(100.0, 1.0, side)

        threads = []
        for i in range(n_threads):
            side = "buy" if i % 2 == 0 else "sell"
            t = threading.Thread(target=worker, args=(side,))
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        snap = acc.snapshot_and_reset()
        total = n_threads * n_per_thread
        assert snap["trade_count"] == total
        assert snap["vwap"] == pytest.approx(100.0)
        buy_threads = n_threads // 2
        sell_threads = n_threads - buy_threads
        assert snap["buy_vol"] == pytest.approx(buy_threads * n_per_thread)
        assert snap["sell_vol"] == pytest.approx(sell_threads * n_per_thread)

    def test_concurrent_update_and_snapshot(self):
        """One thread updates, another snapshots periodically."""
        acc = TradesAccumulator()
        n_updates = 20000
        snapshots: list[dict] = []
        stop = threading.Event()

        def updater():
            for _ in range(n_updates):
                acc.update(100.0, 1.0, "buy")
            stop.set()

        def snapshotter():
            while not stop.is_set():
                snap = acc.snapshot_and_reset()
                snapshots.append(snap)

        t_update = threading.Thread(target=updater)
        t_snap = threading.Thread(target=snapshotter)
        t_update.start()
        t_snap.start()
        t_update.join()
        stop.set()
        t_snap.join()

        # Final snapshot to catch remaining
        snapshots.append(acc.snapshot_and_reset())

        total_count = sum(s["trade_count"] for s in snapshots)
        total_buy = sum(s["buy_vol"] for s in snapshots)
        assert total_count == n_updates
        assert total_buy == pytest.approx(n_updates)


# ── repr / count property ───────────────────────────────────────


class TestIntrospection:
    def test_count_property(self):
        acc = TradesAccumulator()
        assert acc.count == 0
        acc.update(1.0, 1.0, "buy")
        assert acc.count == 1
        acc.update(1.0, 1.0, "sell")
        assert acc.count == 2

    def test_repr(self):
        acc = TradesAccumulator()
        acc.update(100.0, 2.0, "buy")
        r = repr(acc)
        assert "TradesAccumulator" in r
        assert "count=1" in r


# ── OHLCV from trades ───────────────────────────────────────────


class TestOHLCV:
    def test_ohlcv_basic_sequence(self):
        """open=first, high=max, low=min, close=last."""
        acc = TradesAccumulator()
        acc.update(100.0, 1.0, "buy")   # open
        acc.update(105.0, 1.0, "buy")   # high
        acc.update(95.0, 1.0, "sell")   # low
        acc.update(102.0, 1.0, "buy")   # close
        snap = acc.snapshot_and_reset()
        assert snap["open"] == pytest.approx(100.0)
        assert snap["high"] == pytest.approx(105.0)
        assert snap["low"] == pytest.approx(95.0)
        assert snap["close"] == pytest.approx(102.0)
        assert snap["volume"] == pytest.approx(4.0)

    def test_ohlcv_monotonic_up(self):
        acc = TradesAccumulator()
        for px in [100, 101, 102, 103]:
            acc.update(float(px), 1.0, "buy")
        snap = acc.snapshot_and_reset()
        assert snap["open"] == pytest.approx(100.0)
        assert snap["high"] == pytest.approx(103.0)
        assert snap["low"] == pytest.approx(100.0)
        assert snap["close"] == pytest.approx(103.0)

    def test_ohlcv_monotonic_down(self):
        acc = TradesAccumulator()
        for px in [200, 190, 180, 170]:
            acc.update(float(px), 2.0, "sell")
        snap = acc.snapshot_and_reset()
        assert snap["open"] == pytest.approx(200.0)
        assert snap["high"] == pytest.approx(200.0)
        assert snap["low"] == pytest.approx(170.0)
        assert snap["close"] == pytest.approx(170.0)
        assert snap["volume"] == pytest.approx(8.0)

    def test_ohlcv_single_trade(self):
        acc = TradesAccumulator()
        acc.update(50000.0, 0.5, "buy")
        snap = acc.snapshot_and_reset()
        assert snap["open"] == snap["high"] == snap["low"] == snap["close"]
        assert snap["open"] == pytest.approx(50000.0)

    def test_ohlcv_reset_between_buckets(self):
        acc = TradesAccumulator()
        acc.update(100.0, 1.0, "buy")
        acc.update(110.0, 1.0, "buy")
        snap1 = acc.snapshot_and_reset()
        assert snap1["open"] == pytest.approx(100.0)
        assert snap1["close"] == pytest.approx(110.0)

        acc.update(200.0, 1.0, "sell")
        acc.update(190.0, 1.0, "sell")
        snap2 = acc.snapshot_and_reset()
        assert snap2["open"] == pytest.approx(200.0)
        assert snap2["high"] == pytest.approx(200.0)
        assert snap2["low"] == pytest.approx(190.0)
        assert snap2["close"] == pytest.approx(190.0)
