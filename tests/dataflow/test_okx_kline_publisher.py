"""Tests for dataflow.okx.kline.publisher.KlinePublisher.

Covers:
  - Normal publish: bar placed in queue
  - Deduplication: same ts rejected
  - Old ts rejected
  - Gap detection and warning
  - Backpressure: queue full → drop
  - Debug cache receives published bars
  - Thread safety of counters
  - Stats reporting
"""

import queue
import threading

import numpy as np
import pytest

from dataflow.okx.kline.publisher import KlinePublisher


# ── Helpers ────────────────────────────────────────────────────

def _bar(ts_ms: int, close: float = 100.0) -> np.ndarray:
    """Create a minimal BarPoint array."""
    return np.array(
        [ts_ms, 99.0, 105.0, 95.0, close, 10.0, 1000.0, 1000.0],
        dtype=np.float64,
    )


class FakeCache:
    """Minimal mock for debug BarCache."""
    def __init__(self):
        self.data: list[tuple[str, np.ndarray]] = []

    def append(self, symbol: str, bar: np.ndarray):
        self.data.append((symbol, bar.copy()))


# ── Normal publish ─────────────────────────────────────────────

class TestPublishNormal:
    def test_first_bar_published(self):
        q = queue.Queue(maxsize=16)
        pub = KlinePublisher(bar_queue=q, expected_interval_ms=5000)

        assert pub.publish("BTC", _bar(5000)) is True
        assert q.qsize() == 1

        round_bars = q.get_nowait()
        assert "BTC" in round_bars
        np.testing.assert_array_equal(round_bars["BTC"], _bar(5000))

    def test_sequential_bars_published(self):
        q = queue.Queue(maxsize=16)
        pub = KlinePublisher(bar_queue=q, expected_interval_ms=5000)

        pub.publish("BTC", _bar(5000))
        pub.publish("BTC", _bar(10000))
        pub.publish("BTC", _bar(15000))

        assert q.qsize() == 3
        assert pub.bars_published == 3

    def test_multiple_symbols(self):
        q = queue.Queue(maxsize=16)
        pub = KlinePublisher(bar_queue=q, expected_interval_ms=5000)

        pub.publish("BTC", _bar(5000, close=100))
        pub.publish("ETH", _bar(5000, close=200))

        assert q.qsize() == 2
        r1 = q.get_nowait()
        r2 = q.get_nowait()
        symbols = set(list(r1.keys()) + list(r2.keys()))
        assert symbols == {"BTC", "ETH"}


# ── Deduplication ──────────────────────────────────────────────

class TestPublishDedup:
    def test_same_ts_rejected(self):
        q = queue.Queue(maxsize=16)
        pub = KlinePublisher(bar_queue=q, expected_interval_ms=5000)

        assert pub.publish("BTC", _bar(5000)) is True
        assert pub.publish("BTC", _bar(5000)) is False
        assert q.qsize() == 1
        assert pub.dup_count == 1

    def test_old_ts_rejected(self):
        q = queue.Queue(maxsize=16)
        pub = KlinePublisher(bar_queue=q, expected_interval_ms=5000)

        pub.publish("BTC", _bar(10000))
        assert pub.publish("BTC", _bar(5000)) is False
        assert pub.dup_count == 1

    def test_dedup_per_symbol(self):
        q = queue.Queue(maxsize=16)
        pub = KlinePublisher(bar_queue=q, expected_interval_ms=5000)

        pub.publish("BTC", _bar(5000))
        # Same ts for a different symbol is OK
        assert pub.publish("ETH", _bar(5000)) is True
        assert pub.dup_count == 0


# ── Gap detection ──────────────────────────────────────────────

class TestPublishGap:
    def test_gap_detected(self):
        q = queue.Queue(maxsize=16)
        pub = KlinePublisher(bar_queue=q, expected_interval_ms=5000)

        pub.publish("BTC", _bar(5000))
        pub.publish("BTC", _bar(20000))  # gap: 15000ms > 10000ms threshold

        assert pub.gap_count == 1

    def test_no_gap_within_threshold(self):
        q = queue.Queue(maxsize=16)
        pub = KlinePublisher(bar_queue=q, expected_interval_ms=5000)

        pub.publish("BTC", _bar(5000))
        pub.publish("BTC", _bar(10000))  # exactly on time

        assert pub.gap_count == 0

    def test_gap_on_first_bar_not_counted(self):
        q = queue.Queue(maxsize=16)
        pub = KlinePublisher(bar_queue=q, expected_interval_ms=5000)

        pub.publish("BTC", _bar(100000))
        assert pub.gap_count == 0


# ── Backpressure ───────────────────────────────────────────────

class TestPublishBackpressure:
    def test_queue_full_drops(self):
        q = queue.Queue(maxsize=2)
        pub = KlinePublisher(bar_queue=q, expected_interval_ms=1000)

        assert pub.publish("BTC", _bar(1000)) is True
        assert pub.publish("BTC", _bar(2000)) is True
        assert pub.publish("BTC", _bar(3000)) is False  # queue full

        assert pub.drop_count == 1
        assert pub.bars_published == 2


# ── Debug cache ────────────────────────────────────────────────

class TestPublishDebugCache:
    def test_cache_receives_bars(self):
        q = queue.Queue(maxsize=16)
        cache = FakeCache()
        pub = KlinePublisher(bar_queue=q, debug_cache=cache, expected_interval_ms=5000)

        pub.publish("BTC", _bar(5000, close=100))
        pub.publish("ETH", _bar(5000, close=200))

        assert len(cache.data) == 2
        assert cache.data[0][0] == "BTC"
        assert cache.data[0][1][4] == 100.0
        assert cache.data[1][0] == "ETH"
        assert cache.data[1][1][4] == 200.0

    def test_no_cache_no_error(self):
        q = queue.Queue(maxsize=16)
        pub = KlinePublisher(bar_queue=q, debug_cache=None)
        pub.publish("BTC", _bar(5000))
        assert pub.bars_published == 1


# ── Stats ──────────────────────────────────────────────────────

class TestPublishStats:
    def test_stats_dict(self):
        q = queue.Queue(maxsize=16)
        pub = KlinePublisher(bar_queue=q, expected_interval_ms=5000)

        pub.publish("BTC", _bar(5000))
        pub.publish("BTC", _bar(5000))  # dup

        s = pub.stats()
        assert s["bars_published"] == 1
        assert s["dup_count"] == 1
        assert s["gap_count"] == 0
        assert s["drop_count"] == 0


# ── Thread safety ──────────────────────────────────────────────

class TestPublishThreadSafety:
    def test_concurrent_publish(self):
        q = queue.Queue(maxsize=10000)
        pub = KlinePublisher(bar_queue=q, expected_interval_ms=1000)

        errors = []

        def publisher(symbol: str, start_ts: int, count: int):
            try:
                for i in range(count):
                    pub.publish(symbol, _bar(start_ts + i * 1000))
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=publisher, args=("BTC", 0, 500)),
            threading.Thread(target=publisher, args=("ETH", 0, 500)),
            threading.Thread(target=publisher, args=("SOL", 0, 500)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors
        assert pub.bars_published == 1500
        assert q.qsize() == 1500
