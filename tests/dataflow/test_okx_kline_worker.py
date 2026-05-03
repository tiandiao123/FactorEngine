"""Tests for dataflow.okx.kline.worker.KlineWorker lifecycle.

Uses a monkeypatched OKXKlineCollector to emit synthetic candles
without a real WebSocket connection.

Covers:
  - start() spins up daemon thread and bars flow into bar_queue
  - stop() cleanly terminates the thread
  - stop() is idempotent (safe to call twice)
  - stats() reflects bars processed
  - bar_count matches expectations
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
    """Replaces OKXKlineCollector: emits a fixed set of candles then exits."""

    def __init__(self, candles: list[tuple[str, list]], on_candle):
        self._candles = candles
        self._on_candle = on_candle

    async def run(self):
        for symbol, raw in self._candles:
            self._on_candle(symbol, raw)
            await asyncio.sleep(0)

    def stop(self):
        pass


# ── Lifecycle tests ────────────────────────────────────────────

class TestKlineWorkerLifecycle:
    def _make_worker(self, candles, freq_seconds=1):
        """Create a KlineWorker with a patched collector."""
        bar_queue = queue.Queue(maxsize=100)
        worker = KlineWorker(
            symbols=["BTC-USDT-SWAP"],
            bar_queue=bar_queue,
            freq_seconds=freq_seconds,
        )

        original_run = worker._run

        async def patched_run():
            collector = FakeCollector(candles, worker._on_candle)
            worker._collector = collector
            await collector.run()

        worker._run = patched_run
        return worker, bar_queue

    def test_start_enqueues_bars(self):
        candles = [
            ("BTC-USDT-SWAP", _make_candle(1000, c=100.0)),
            ("BTC-USDT-SWAP", _make_candle(2000, c=101.0)),
            ("BTC-USDT-SWAP", _make_candle(3000, c=102.0)),
        ]
        worker, bar_queue = self._make_worker(candles, freq_seconds=1)

        worker.start()
        time.sleep(0.5)
        worker.stop()

        assert worker.bar_count == 3
        assert bar_queue.qsize() == 3

        bars = [bar_queue.get_nowait() for _ in range(3)]
        closes = [list(b.values())[0][4] for b in bars]
        assert closes == [100.0, 101.0, 102.0]

    def test_start_with_aggregation(self):
        candles = [
            ("BTC-USDT-SWAP", _make_candle(1000, c=100.0)),
            ("BTC-USDT-SWAP", _make_candle(2000, c=101.0)),
            ("BTC-USDT-SWAP", _make_candle(3000, c=102.0)),
        ]
        worker, bar_queue = self._make_worker(candles, freq_seconds=3)

        worker.start()
        time.sleep(0.5)
        worker.stop()

        assert worker.bar_count == 1
        assert bar_queue.qsize() == 1

        round_bars = bar_queue.get_nowait()
        bar = round_bars["BTC-USDT-SWAP"]
        assert bar[4] == 102.0  # close = last

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
        worker.stop()  # second call should not raise

    def test_stats_reflect_bars(self):
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
        assert stats["dup_count"] == 0
        assert stats["drop_count"] == 0

    def test_unconfirmed_candles_not_counted(self):
        candles = [
            ("BTC-USDT-SWAP", _make_candle(1000, confirm="1")),
            ("BTC-USDT-SWAP", _make_candle(2000, confirm="0")),  # unconfirmed
            ("BTC-USDT-SWAP", _make_candle(3000, confirm="1")),
        ]
        worker, bar_queue = self._make_worker(candles, freq_seconds=1)

        worker.start()
        time.sleep(0.5)
        worker.stop()

        assert worker.bar_count == 2
        assert bar_queue.qsize() == 2
