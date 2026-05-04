"""Tests for dataflow.okx.kline.worker.KlineWorker lifecycle (v3 ClockDriver).

Uses a monkeypatched OKXKlineCollector to emit synthetic candles
without a real WebSocket connection.

Covers:
  - start() spins up daemon thread and bars flow into publisher buffer
  - flush() returns complete rounds for ClockDriver
  - Rounds contain all symbols (batched by publisher)
  - stop() cleanly terminates the thread
  - stop() is idempotent (safe to call twice)
  - stats() reflects bars processed + round counts
  - Unconfirmed candles are filtered
"""

import asyncio
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


# ── Lifecycle tests ───────────────────────────────────────────

class TestKlineWorkerLifecycle:
    def _make_worker(self, candles, symbols=None, freq_seconds=1):
        """Create a KlineWorker with a patched collector."""
        if symbols is None:
            symbols = sorted(set(sym for sym, _ in candles)) or ["BTC-USDT-SWAP"]
        worker = KlineWorker(
            symbols=symbols,
            freq_seconds=freq_seconds,
        )

        async def patched_run():
            collector = FakeCollector(candles, worker._on_candle)
            worker._collector = collector
            await collector.run()

        worker._run = patched_run
        return worker

    def test_start_single_symbol_produces_bars(self):
        candles = [
            ("BTC-USDT-SWAP", _make_candle(1000, c=100.0)),
            ("BTC-USDT-SWAP", _make_candle(2000, c=101.0)),
            ("BTC-USDT-SWAP", _make_candle(3000, c=102.0)),
        ]
        worker = self._make_worker(candles)

        worker.start()
        time.sleep(0.5)
        worker.stop()

        assert worker.bar_count == 3

    def test_flush_returns_round(self):
        candles = [
            ("BTC-USDT-SWAP", _make_candle(1000, c=100.0)),
            ("ETH-USDT-SWAP", _make_candle(1000, c=200.0)),
        ]
        symbols = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
        worker = self._make_worker(candles, symbols=symbols)

        worker.start()
        time.sleep(0.5)
        worker.stop()

        assert worker.bar_count == 2
        result = worker.flush()
        assert result is not None
        assert set(result.keys()) == set(symbols)
        assert result["BTC-USDT-SWAP"][4] == 100.0
        assert result["ETH-USDT-SWAP"][4] == 200.0

    def test_multi_symbol_rounds_batched(self):
        """With 2 symbols, each ts produces 1 round retrievable via flush."""
        candles = [
            ("BTC-USDT-SWAP", _make_candle(1000, c=100.0)),
            ("ETH-USDT-SWAP", _make_candle(1000, c=200.0)),
            ("BTC-USDT-SWAP", _make_candle(2000, c=101.0)),
            ("ETH-USDT-SWAP", _make_candle(2000, c=201.0)),
        ]
        symbols = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
        worker = self._make_worker(candles, symbols=symbols)

        worker.start()
        time.sleep(0.5)
        worker.stop()

        assert worker.bar_count == 4

        result = worker.flush()
        assert result is not None
        assert set(result.keys()) == set(symbols)

    def test_aggregation_with_flush(self):
        """3s aggregation: 3 candles per symbol in window [0,3000) → 1 round."""
        candles = [
            ("BTC-USDT-SWAP", _make_candle(0, c=100.0)),
            ("ETH-USDT-SWAP", _make_candle(0, c=200.0)),
            ("BTC-USDT-SWAP", _make_candle(1000, c=101.0)),
            ("ETH-USDT-SWAP", _make_candle(1000, c=201.0)),
            ("BTC-USDT-SWAP", _make_candle(2000, c=102.0)),
            ("ETH-USDT-SWAP", _make_candle(2000, c=202.0)),
        ]
        symbols = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
        worker = self._make_worker(candles, symbols=symbols, freq_seconds=3)

        worker.start()
        time.sleep(0.5)
        worker.stop()

        assert worker.bar_count == 2

        result = worker.flush()
        assert result is not None
        assert set(result.keys()) == set(symbols)
        assert result["BTC-USDT-SWAP"][4] == 102.0
        assert result["ETH-USDT-SWAP"][4] == 202.0
        assert result["BTC-USDT-SWAP"][0] == 0.0
        assert result["ETH-USDT-SWAP"][0] == 0.0

    def test_stop_terminates_thread(self):
        candles = [("BTC-USDT-SWAP", _make_candle(1000))]
        worker = self._make_worker(candles)

        worker.start()
        assert worker._thread is not None
        time.sleep(0.3)
        worker.stop()
        assert not worker._thread.is_alive()

    def test_stop_idempotent(self):
        candles = [("BTC-USDT-SWAP", _make_candle(1000))]
        worker = self._make_worker(candles)

        worker.start()
        time.sleep(0.3)
        worker.stop()
        worker.stop()

    def test_stats_reflect_bars(self):
        candles = [
            ("BTC-USDT-SWAP", _make_candle(1000)),
            ("BTC-USDT-SWAP", _make_candle(2000)),
        ]
        worker = self._make_worker(candles, freq_seconds=1)

        worker.start()
        time.sleep(0.5)
        worker.stop()

        stats = worker.stats()
        assert stats["adapter_bar_count"] == 2
        assert stats["bars_received"] == 2
        assert stats["rounds_completed"] == 2
        assert stats["dup_count"] == 0

    def test_unconfirmed_candles_not_counted(self):
        candles = [
            ("BTC-USDT-SWAP", _make_candle(1000, confirm="1")),
            ("BTC-USDT-SWAP", _make_candle(2000, confirm="0")),
            ("BTC-USDT-SWAP", _make_candle(3000, confirm="1")),
        ]
        worker = self._make_worker(candles, freq_seconds=1)

        worker.start()
        time.sleep(0.5)
        worker.stop()

        assert worker.bar_count == 2
