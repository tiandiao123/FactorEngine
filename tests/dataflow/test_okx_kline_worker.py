"""Tests for dataflow.okx.kline.worker.KlineWorker lifecycle (v2).

Uses a monkeypatched OXKlineCollector to emit synthetic candles
without a real WebSocket connection.

Covers:
  - start() spins up daemon thread and bars flow into bar_queue
  - Rounds contain all symbols (batched by publisher v2)
  - stop() cleanly terminates the thread and flushes pending
  - stop() is idempotent (safe to call twice)
  - stats() reflects bars processed + round counts
  - Unconfirmed candles are filtered
"""

import asyncio
import queue
import time

import numpy as np
import pytest

from dataflow.okx.kline.worker import KlineWorker


# ── Helpers ────────────────────────────────────────────────────

def _make_candle(ts_ms: int, c=100.0, confirm="1") -> list:
    return [
        str(ts_ms), "99.0", "105.0", "95.0", str(c),
        "10.0", "1000.0", "1000.0", confirm,
    ]


class FakeCollector:
    """Replaces OXKlineCollector: emits a fixed set of candles then exits."""

    def __init__(self, candles: list[tuple[str, list]], on_candle):
        self._candles = candles
        self._on_candle = on_candle

    async def run(self):
        for symbol, raw in self._candles:
            self._on_candle(symbol, raw)
            await asyncio.sleep(0)

    def stop(self):
        pass


# ── Lifecycle tests ───────────────────────────────────────────

class TestKlineWorkerLifecycle:
    def _make_worker(self, candles, symbols=None, freq_seconds=1):
        """Create a KlineWorker with a patched collector."""
        if symbols is None:
            symbols = sorted(set(sym for sym, _ in candles)) or ["BTC-USDT-SWAP"]
        bar_queue = queue.Queue(maxsize=100)
        worker = KlineWorker(
            symbols=symbols,
            bar_queue=bar_queue,
            freq_seconds=freq_seconds,
        )

        async def patched_run():
            collector = FakeCollector(candles, worker._on_candle)
            worker._collector = collector
            await collector.run()

        worker._run = patched_run
        return worker, bar_queue

    def test_start_single_symbol_enqueues_bars(self):
        candles = [
            ("BTC-USDT-SWAP", _make_candle(1000, c=100.0)),
            ("BTC-USDT-SWAP", _make_candle(2000, c=101.0)),
            ("BTC-USDT-SWAP", _make_candle(3000, c=102.0)),
        ]
        worker, bar_queue = self._make_worker(candles)

        worker.start()
        time.sleep(0.5)
        worker.stop()

        assert worker.bar_count == 3
        assert bar_queue.qsize() == 3

    def test_multi_symbol_rounds_batched(self):
        """With 2 symbols, each ts produces 1 round with both symbols."""
        candles = [
            ("BTC-USDT-SWAP", _make_candle(1000, c=100.0)),
            ("ETH-USDT-SWAP", _make_candle(1000, c=200.0)),
            ("BTC-USDT-SWAP", _make_candle(2000, c=101.0)),
            ("ETH-USDT-SWAP", _make_candle(2000, c=201.0)),
        ]
        symbols = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
        worker, bar_queue = self._make_worker(candles, symbols=symbols)

        worker.start()
        time.sleep(0.5)
        worker.stop()

        assert worker.bar_count == 4
        assert bar_queue.qsize() == 2  # 2 complete rounds

        round1 = bar_queue.get_nowait()
        assert set(round1.keys()) == set(symbols)
        assert round1["BTC-USDT-SWAP"][4] == 100.0
        assert round1["ETH-USDT-SWAP"][4] == 200.0

        round2 = bar_queue.get_nowait()
        assert set(round2.keys()) == set(symbols)
        assert round2["BTC-USDT-SWAP"][4] == 101.0
        assert round2["ETH-USDT-SWAP"][4] == 201.0

    def test_aggregation_with_batching(self):
        """3s aggregation: 3 candles per symbol → 1 round with both symbols."""
        candles = [
            ("BTC-USDT-SWAP", _make_candle(1000, c=100.0)),
            ("ETH-USDT-SWAP", _make_candle(1000, c=200.0)),
            ("BTC-USDT-SWAP", _make_candle(2000, c=101.0)),
            ("ETH-USDT-SWAP", _make_candle(2000, c=201.0)),
            ("BTC-USDT-SWAP", _make_candle(3000, c=102.0)),
            ("ETH-USDT-SWAP", _make_candle(3000, c=202.0)),
        ]
        symbols = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
        worker, bar_queue = self._make_worker(candles, symbols=symbols, freq_seconds=3)

        worker.start()
        time.sleep(0.5)
        worker.stop()

        assert worker.bar_count == 2  # 1 aggregated bar per symbol
        assert bar_queue.qsize() == 1  # 1 complete round

        round_bars = bar_queue.get_nowait()
        assert set(round_bars.keys()) == set(symbols)
        assert round_bars["BTC-USDT-SWAP"][4] == 102.0  # close = last
        assert round_bars["ETH-USDT-SWAP"][4] == 202.0

    def test_stop_terminates_thread(self):
        candles = [("BTC-USDT-SWAP", _make_candle(1000))]
        worker, _ = self._make_worker(candles)

        worker.start()
        assert worker._thread is not None
        time.sleep(0.3)
        worker.stop()
        assert not worker._thread.is_alive()

    def test_stop_idempotent(self):
        candles = [("BTC-USDT-SWAP", _make_candle(1000))]
        worker, _ = self._make_worker(candles)

        worker.start()
        time.sleep(0.3)
        worker.stop()
        worker.stop()

    def test_stats_reflect_bars_and_rounds(self):
        candles = [
            ("BTC-USDT-SWAP", _make_candle(1000)),
            ("BTC-USDT-SWAP", _make_candle(2000)),
        ]
        worker, _ = self._make_worker(candles, freq_seconds=1)

        worker.start()
        time.sleep(0.5)
        worker.stop()

        stats = worker.stats()
        assert stats["adapter_bar_count"] == 2
        assert stats["bars_published"] == 2
        assert stats["rounds_flushed"] == 2
        assert stats["dup_count"] == 0
        assert stats["drop_count"] == 0

    def test_unconfirmed_candles_not_counted(self):
        candles = [
            ("BTC-USDT-SWAP", _make_candle(1000, confirm="1")),
            ("BTC-USDT-SWAP", _make_candle(2000, confirm="0")),
            ("BTC-USDT-SWAP", _make_candle(3000, confirm="1")),
        ]
        worker, bar_queue = self._make_worker(candles, freq_seconds=1)

        worker.start()
        time.sleep(0.5)
        worker.stop()

        assert worker.bar_count == 2
        assert bar_queue.qsize() == 2
