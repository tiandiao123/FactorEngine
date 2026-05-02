"""End-to-end tests for the OKX kline streaming pipeline.

Tests the full path WITHOUT a real WebSocket connection:
  KlineAdapter → KlinePublisher → bar_queue → Engine._runtime_loop()-style consumer

This validates that the bar format produced by the kline pipeline is
compatible with what Engine._runtime_loop() expects.
"""

import queue
import threading
import time

import numpy as np
import pytest

from dataflow.okx.kline.adapter import KlineAdapter
from dataflow.okx.kline.publisher import KlinePublisher


# ── Helpers ────────────────────────────────────────────────────

def _make_candle(ts_ms: int, o=100.0, h=105.0, l=95.0, c=102.0,
                 vol=10.0, vol_ccy=1020.0, vol_ccy_quote=1020.0,
                 confirm="1") -> list:
    return [
        str(ts_ms), str(o), str(h), str(l), str(c),
        str(vol), str(vol_ccy), str(vol_ccy_quote), confirm,
    ]


class FakeBarCache:
    def __init__(self):
        self.data: dict[str, list[np.ndarray]] = {}

    def append(self, symbol: str, bar: np.ndarray):
        self.data.setdefault(symbol, []).append(bar.copy())


# ── Full pipeline: adapter → publisher → queue ─────────────────

class TestAdapterToPublisherPipeline:
    """Simulate what KlineWorker._on_candle does internally."""

    def test_5s_bars_land_in_queue(self):
        bar_queue = queue.Queue(maxsize=16)
        cache = FakeBarCache()
        adapter = KlineAdapter(freq_seconds=5)
        publisher = KlinePublisher(
            bar_queue=bar_queue, debug_cache=cache, expected_interval_ms=5000,
        )

        base_ts = 1_700_000_000_000
        symbols = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
        bars_received = {s: 0 for s in symbols}

        # Feed 15 confirmed 1s candles per symbol → should produce 3 bars each
        for i in range(15):
            ts = base_ts + i * 1000
            for sym in symbols:
                price = 100.0 + i * 0.1 if sym == "BTC-USDT-SWAP" else 50.0 + i * 0.05
                raw = _make_candle(ts, o=price, h=price + 5, l=price - 5,
                                   c=price + 1, vol=10.0)
                bar = adapter.on_candle(sym, raw)
                if bar is not None:
                    publisher.publish(sym, bar)
                    bars_received[sym] += 1

        assert bars_received["BTC-USDT-SWAP"] == 3
        assert bars_received["ETH-USDT-SWAP"] == 3
        assert bar_queue.qsize() == 6
        assert publisher.bars_published == 6
        assert publisher.dup_count == 0

        # Verify cache also received bars
        assert len(cache.data["BTC-USDT-SWAP"]) == 3
        assert len(cache.data["ETH-USDT-SWAP"]) == 3

    def test_queue_format_matches_runtime_loop_expectations(self):
        """Verify round_bars format is dict[str, ndarray(8,)] — same as simulation."""
        bar_queue = queue.Queue(maxsize=16)
        adapter = KlineAdapter(freq_seconds=1)
        publisher = KlinePublisher(bar_queue=bar_queue, expected_interval_ms=1000)

        ts = 1_700_000_000_000
        raw = _make_candle(ts, o=100.0, h=110.0, l=90.0, c=105.0,
                           vol=50.0, vol_ccy=5250.0, vol_ccy_quote=5250.0)
        bar = adapter.on_candle("BTC-USDT-SWAP", raw)
        publisher.publish("BTC-USDT-SWAP", bar)

        round_bars = bar_queue.get_nowait()

        # Must be a dict
        assert isinstance(round_bars, dict)
        assert "BTC-USDT-SWAP" in round_bars

        bar = round_bars["BTC-USDT-SWAP"]
        assert isinstance(bar, np.ndarray)
        assert bar.shape == (8,)
        assert bar.dtype == np.float64

        # Column indices must match Engine._runtime_loop() expectations:
        #   bar[0] = ts, bar[1] = open, bar[2] = high, bar[3] = low,
        #   bar[4] = close, bar[5] = volume
        assert int(bar[0]) == ts
        assert bar[1] == 100.0   # open
        assert bar[2] == 110.0   # high
        assert bar[3] == 90.0    # low
        assert bar[4] == 105.0   # close
        assert bar[5] == 50.0    # volume

    def test_dedup_prevents_duplicate_push(self):
        bar_queue = queue.Queue(maxsize=16)
        adapter = KlineAdapter(freq_seconds=1)
        publisher = KlinePublisher(bar_queue=bar_queue, expected_interval_ms=1000)

        ts = 1_700_000_000_000
        raw = _make_candle(ts, c=100.0)

        bar = adapter.on_candle("BTC", raw)
        assert publisher.publish("BTC", bar) is True

        # Simulate duplicate candle (same ts, different close)
        raw2 = _make_candle(ts, c=999.0)
        bar2 = adapter.on_candle("BTC", raw2)
        assert publisher.publish("BTC", bar2) is False  # dedup

        assert bar_queue.qsize() == 1
        assert publisher.dup_count == 1


# ── Consumer thread simulation ─────────────────────────────────

class TestConsumerThread:
    """Simulate Engine._runtime_loop() consuming from bar_queue."""

    def test_consumer_receives_all_bars(self):
        bar_queue = queue.Queue(maxsize=64)
        adapter = KlineAdapter(freq_seconds=1)
        publisher = KlinePublisher(bar_queue=bar_queue, expected_interval_ms=1000)
        consumed = []
        stop = threading.Event()

        def consumer():
            while not stop.is_set():
                try:
                    round_bars = bar_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                for sym, bar in round_bars.items():
                    close = float(bar[4])
                    prev_close = close  # simplified
                    consumed.append((sym, close))

        t = threading.Thread(target=consumer, daemon=True)
        t.start()

        # Push 10 bars
        for i in range(10):
            ts = 1_000_000 + i * 1000
            raw = _make_candle(ts, c=100.0 + i)
            bar = adapter.on_candle("BTC", raw)
            publisher.publish("BTC", bar)

        time.sleep(0.5)
        stop.set()
        t.join(timeout=2)

        assert len(consumed) == 10
        closes = [c for _, c in consumed]
        assert closes == [100.0 + i for i in range(10)]


# ── Stress test ────────────────────────────────────────────────

class TestPipelineStress:
    def test_high_throughput(self):
        """Push 10000 bars through the pipeline and verify no data loss."""
        bar_queue = queue.Queue(maxsize=20000)
        adapter = KlineAdapter(freq_seconds=1)
        publisher = KlinePublisher(bar_queue=bar_queue, expected_interval_ms=1000)

        n_bars = 10000
        for i in range(n_bars):
            ts = i * 1000
            raw = _make_candle(ts, c=float(i))
            bar = adapter.on_candle("BTC", raw)
            publisher.publish("BTC", bar)

        assert bar_queue.qsize() == n_bars
        assert publisher.bars_published == n_bars
        assert publisher.dup_count == 0
        assert publisher.drop_count == 0

    def test_multi_symbol_high_throughput(self):
        """20 symbols × 1000 bars each."""
        bar_queue = queue.Queue(maxsize=100000)
        adapter = KlineAdapter(freq_seconds=1)
        publisher = KlinePublisher(bar_queue=bar_queue, expected_interval_ms=1000)

        symbols = [f"SYM-{i}-USDT-SWAP" for i in range(20)]
        n_bars_per_sym = 1000

        for i in range(n_bars_per_sym):
            ts = i * 1000
            for sym in symbols:
                raw = _make_candle(ts, c=float(i))
                bar = adapter.on_candle(sym, raw)
                publisher.publish(sym, bar)

        expected = len(symbols) * n_bars_per_sym
        assert publisher.bars_published == expected
        assert bar_queue.qsize() == expected
