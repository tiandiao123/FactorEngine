"""End-to-end tests for the OKX kline streaming pipeline (v3 ClockDriver).

Tests the full path WITHOUT a real WebSocket connection:
  KlineAdapter → KlinePublisher → flush_for_clock() → consumer

Validates that:
  - Rounds contain ALL symbols (not partial)
  - Bar format is compatible with Engine._runtime_loop()
  - Dedup prevents duplicate pushes
  - Multi-symbol high throughput works correctly
"""

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


# ── Full pipeline: adapter → publisher → flush_for_clock ──────

class TestAdapterToPublisherPipeline:
    """Simulate what KlineWorker._on_candle does internally."""

    def test_5s_bars_batched_in_rounds(self):
        """3 rounds of 5s bars for 2 symbols → 3 complete rounds."""
        symbols = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
        cache = FakeBarCache()
        adapter = KlineAdapter(freq_seconds=5)
        publisher = KlinePublisher(
            symbols=symbols,
            debug_cache=cache, expected_interval_ms=5000,
        )

        base_ts = 1_700_000_000_000

        for i in range(15):
            ts = base_ts + i * 1000
            for sym in symbols:
                price = 100.0 + i * 0.1 if sym == "BTC-USDT-SWAP" else 50.0 + i * 0.05
                raw = _make_candle(ts, o=price, h=price + 5, l=price - 5,
                                   c=price + 1, vol=10.0)
                bar = adapter.on_candle(sym, raw)
                if bar is not None:
                    publisher.publish(sym, bar)

        assert publisher.rounds_completed == 3
        assert publisher.bars_received == 6

        # flush_for_clock should return data.
        result = publisher.flush_for_clock()
        assert result is not None
        assert set(result.keys()) == set(symbols)

        # Cache receives bars immediately (not batched).
        assert len(cache.data["BTC-USDT-SWAP"]) == 3
        assert len(cache.data["ETH-USDT-SWAP"]) == 3

    def test_bar_format_matches_runtime_loop(self):
        """Each round is dict[str, ndarray(8,)] with correct column order."""
        symbols = ["BTC-USDT-SWAP"]
        adapter = KlineAdapter(freq_seconds=1)
        publisher = KlinePublisher(
            symbols=symbols, expected_interval_ms=1000,
        )

        ts = 1_700_000_000_000
        raw = _make_candle(ts, o=100.0, h=110.0, l=90.0, c=105.0,
                           vol=50.0, vol_ccy=5250.0, vol_ccy_quote=5250.0)
        bar = adapter.on_candle("BTC-USDT-SWAP", raw)
        publisher.publish("BTC-USDT-SWAP", bar)

        result = publisher.flush_for_clock()
        assert isinstance(result, dict)
        assert "BTC-USDT-SWAP" in result

        bar = result["BTC-USDT-SWAP"]
        assert isinstance(bar, np.ndarray)
        assert bar.shape == (8,)
        assert bar.dtype == np.float64

        assert int(bar[0]) == ts
        assert bar[1] == 100.0   # open
        assert bar[2] == 110.0   # high
        assert bar[3] == 90.0    # low
        assert bar[4] == 105.0   # close
        assert bar[5] == 50.0    # volume

    def test_dedup_prevents_duplicate(self):
        symbols = ["BTC-USDT-SWAP"]
        adapter = KlineAdapter(freq_seconds=1)
        publisher = KlinePublisher(
            symbols=symbols, expected_interval_ms=1000,
        )

        ts = 1_700_000_000_000
        raw = _make_candle(ts, c=100.0)
        bar = adapter.on_candle("BTC-USDT-SWAP", raw)
        publisher.publish("BTC-USDT-SWAP", bar)

        raw2 = _make_candle(ts, c=999.0)
        bar2 = adapter.on_candle("BTC-USDT-SWAP", raw2)
        publisher.publish("BTC-USDT-SWAP", bar2)

        assert publisher.rounds_completed == 1
        assert publisher.dup_count == 1


# ── Consumer thread simulation ────────────────────────────────

class TestConsumerThread:
    """Simulate ClockDriver polling flush_for_clock()."""

    def test_consumer_receives_full_rounds(self):
        symbols = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
        adapter = KlineAdapter(freq_seconds=1)
        publisher = KlinePublisher(
            symbols=symbols, expected_interval_ms=1000,
        )
        consumed_rounds = []
        stop = threading.Event()

        def consumer():
            while not stop.is_set():
                result = publisher.flush_for_clock()
                if result is not None:
                    consumed_rounds.append(result)
                time.sleep(0.05)

        t = threading.Thread(target=consumer, daemon=True)
        t.start()

        for i in range(10):
            ts = 1_000_000 + i * 1000
            for sym in symbols:
                raw = _make_candle(ts, c=100.0 + i)
                bar = adapter.on_candle(sym, raw)
                publisher.publish(sym, bar)
            time.sleep(0.02)

        time.sleep(0.5)
        stop.set()
        t.join(timeout=2)

        assert len(consumed_rounds) > 0
        for round_bars in consumed_rounds:
            assert set(round_bars.keys()) == set(symbols)


# ── Stress test ───────────────────────────────────────────────

class TestPipelineStress:
    def test_high_throughput_single_symbol(self):
        symbols = ["BTC-USDT-SWAP"]
        adapter = KlineAdapter(freq_seconds=1)
        publisher = KlinePublisher(
            symbols=symbols, expected_interval_ms=1000,
        )

        n_bars = 10000
        for i in range(n_bars):
            ts = i * 1000
            raw = _make_candle(ts, c=float(i))
            bar = adapter.on_candle("BTC-USDT-SWAP", raw)
            publisher.publish("BTC-USDT-SWAP", bar)

        assert publisher.rounds_completed == n_bars
        assert publisher.bars_received == n_bars
        assert publisher.dup_count == 0

    def test_multi_symbol_high_throughput(self):
        """20 symbols × 1000 bars: each round must contain all 20 symbols."""
        symbols = [f"SYM-{i}-USDT-SWAP" for i in range(20)]
        adapter = KlineAdapter(freq_seconds=1)
        publisher = KlinePublisher(
            symbols=symbols, expected_interval_ms=1000,
        )

        n_rounds = 1000
        for i in range(n_rounds):
            ts = i * 1000
            for sym in symbols:
                raw = _make_candle(ts, c=float(i))
                bar = adapter.on_candle(sym, raw)
                publisher.publish(sym, bar)

        assert publisher.rounds_completed == n_rounds
        assert publisher.bars_received == n_rounds * len(symbols)

        sample = publisher.flush_for_clock()
        assert sample is not None
        assert set(sample.keys()) == set(symbols)
