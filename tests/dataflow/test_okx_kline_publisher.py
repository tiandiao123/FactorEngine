"""Tests for dataflow.okx.kline.publisher.KlinePublisher (v3 ClockDriver).

Covers:
  - Round completion: all symbols arrive → ready via flush_for_clock()
  - Timeout + forward-fill: partial symbols + timeout → fill from last round
  - First round must wait for all symbols (no forward-fill source)
  - Round boundary: new ts triggers internal completion of previous round
  - Deduplication: same/old ts rejected
  - Gap detection and warning
  - Debug cache receives bars immediately
  - flush_for_clock() returns data for ClockDriver
  - Stats reporting (rounds_completed, forward_fill_count, etc.)
  - Thread safety
"""

import threading
import time

import numpy as np
import pytest

from dataflow.okx.kline.publisher import KlinePublisher


# ── Helpers ────────────────────────────────────────────────────

SYMBOLS_3 = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]
SYMBOLS_2 = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]


def _bar(ts_ms: int, close: float = 100.0) -> np.ndarray:
    return np.array(
        [ts_ms, 99.0, 105.0, 95.0, close, 10.0, 1000.0, 1000.0],
        dtype=np.float64,
    )


class FakeCache:
    def __init__(self):
        self.data: list[tuple[str, np.ndarray]] = []

    def append(self, symbol: str, bar: np.ndarray):
        self.data.append((symbol, bar.copy()))


def _pub(symbols=None, **kwargs):
    """Shorthand to create a publisher."""
    pub = KlinePublisher(
        symbols=symbols or SYMBOLS_3,
        expected_interval_ms=kwargs.pop("expected_interval_ms", 5000),
        **kwargs,
    )
    return pub


# ── Round completion: all symbols arrive ──────────────────────

class TestRoundCompletion:
    def test_ready_when_all_symbols_arrive(self):
        pub = _pub()

        pub.publish("BTC-USDT-SWAP", _bar(5000))
        pub.publish("ETH-USDT-SWAP", _bar(5000))
        pub.publish("SOL-USDT-SWAP", _bar(5000))

        result = pub.flush_for_clock()
        assert result is not None
        assert set(result.keys()) == set(SYMBOLS_3)
        assert pub.rounds_completed == 1

    def test_two_rounds(self):
        pub = _pub()

        for sym in SYMBOLS_3:
            pub.publish(sym, _bar(5000))
        for sym in SYMBOLS_3:
            pub.publish(sym, _bar(10000))

        assert pub.rounds_completed == 2

        r1 = pub.flush_for_clock()
        assert r1 is not None

    def test_order_independent(self):
        pub = _pub()

        pub.publish("SOL-USDT-SWAP", _bar(5000))
        pub.publish("BTC-USDT-SWAP", _bar(5000))
        pub.publish("ETH-USDT-SWAP", _bar(5000))

        result = pub.flush_for_clock()
        assert result is not None
        assert set(result.keys()) == set(SYMBOLS_3)

    def test_single_symbol(self):
        pub = _pub(symbols=["BTC-USDT-SWAP"])

        pub.publish("BTC-USDT-SWAP", _bar(5000))
        result = pub.flush_for_clock()
        assert result is not None
        assert "BTC-USDT-SWAP" in result

        pub.publish("BTC-USDT-SWAP", _bar(10000))
        result2 = pub.flush_for_clock()
        assert result2 is not None


# ── First round: must wait for all symbols ────────────────────

class TestFirstRound:
    def test_first_round_partial_no_flush(self):
        pub = _pub(batch_timeout_s=0.01)

        pub.publish("BTC-USDT-SWAP", _bar(5000))
        pub.publish("ETH-USDT-SWAP", _bar(5000))
        time.sleep(0.05)
        pub.publish("BTC-USDT-SWAP", _bar(10000))
        assert pub.rounds_completed == 0

    def test_first_round_complete_flushes(self):
        pub = _pub()

        for sym in SYMBOLS_3:
            pub.publish(sym, _bar(5000))

        assert pub.rounds_completed == 1


# ── Timeout + forward-fill ────────────────────────────────────

class TestTimeoutForwardFill:
    def test_timeout_triggers_forward_fill(self):
        pub = _pub(batch_timeout_s=0.05)

        for sym in SYMBOLS_3:
            pub.publish(sym, _bar(5000))
        assert pub.rounds_completed == 1

        pub.publish("BTC-USDT-SWAP", _bar(10000))
        pub.publish("ETH-USDT-SWAP", _bar(10000))
        time.sleep(0.1)
        pub.publish("BTC-USDT-SWAP", _bar(15000))

        assert pub.rounds_completed == 2
        assert pub.forward_fill_count == 1

    def test_forward_fill_value_is_last_round(self):
        pub = _pub(symbols=SYMBOLS_2, batch_timeout_s=0.05)

        pub.publish("BTC-USDT-SWAP", _bar(5000, close=100.0))
        pub.publish("ETH-USDT-SWAP", _bar(5000, close=200.0))
        assert pub.rounds_completed == 1

        pub.publish("BTC-USDT-SWAP", _bar(10000, close=101.0))
        time.sleep(0.1)
        pub.publish("BTC-USDT-SWAP", _bar(15000, close=102.0))

        assert pub.rounds_completed == 2


# ── Round boundary: ts mismatch ───────────────────────────────

class TestRoundBoundary:
    def test_new_ts_flushes_previous_round(self):
        pub = _pub(symbols=SYMBOLS_2)

        pub.publish("BTC-USDT-SWAP", _bar(5000))
        pub.publish("ETH-USDT-SWAP", _bar(5000))
        assert pub.rounds_completed == 1

        pub.publish("BTC-USDT-SWAP", _bar(10000))
        pub.publish("BTC-USDT-SWAP", _bar(15000))

        assert pub.rounds_completed == 2
        assert pub.forward_fill_count == 1


# ── Deduplication ─────────────────────────────────────────────

class TestDedup:
    def test_same_ts_rejected(self):
        pub = _pub(symbols=["BTC-USDT-SWAP"])

        pub.publish("BTC-USDT-SWAP", _bar(5000))
        pub.publish("BTC-USDT-SWAP", _bar(5000))
        assert pub.dup_count == 1

    def test_old_ts_rejected(self):
        pub = _pub(symbols=["BTC-USDT-SWAP"])

        pub.publish("BTC-USDT-SWAP", _bar(10000))
        pub.publish("BTC-USDT-SWAP", _bar(5000))
        assert pub.dup_count == 1

    def test_dedup_per_symbol(self):
        pub = _pub()

        pub.publish("BTC-USDT-SWAP", _bar(5000))
        pub.publish("ETH-USDT-SWAP", _bar(5000))
        assert pub.dup_count == 0


# ── Gap detection ─────────────────────────────────────────────

class TestGap:
    def test_gap_detected(self):
        pub = _pub(symbols=["BTC-USDT-SWAP"])

        pub.publish("BTC-USDT-SWAP", _bar(5000))
        pub.publish("BTC-USDT-SWAP", _bar(20000))
        assert pub.gap_count == 1

    def test_no_gap_within_threshold(self):
        pub = _pub(symbols=["BTC-USDT-SWAP"])

        pub.publish("BTC-USDT-SWAP", _bar(5000))
        pub.publish("BTC-USDT-SWAP", _bar(10000))
        assert pub.gap_count == 0

    def test_gap_on_first_bar_not_counted(self):
        pub = _pub(symbols=["BTC-USDT-SWAP"])

        pub.publish("BTC-USDT-SWAP", _bar(100000))
        assert pub.gap_count == 0

    def test_gap_boundary_exact_2x_not_counted(self):
        pub = _pub(symbols=["BTC-USDT-SWAP"])

        pub.publish("BTC-USDT-SWAP", _bar(5000))
        pub.publish("BTC-USDT-SWAP", _bar(15000))
        assert pub.gap_count == 0

    def test_gap_boundary_just_over_2x(self):
        pub = _pub(symbols=["BTC-USDT-SWAP"])

        pub.publish("BTC-USDT-SWAP", _bar(5000))
        pub.publish("BTC-USDT-SWAP", _bar(15001))
        assert pub.gap_count == 1

    def test_gap_multi_symbol_independent(self):
        pub = _pub()

        pub.publish("BTC-USDT-SWAP", _bar(5000))
        pub.publish("ETH-USDT-SWAP", _bar(5000))
        pub.publish("SOL-USDT-SWAP", _bar(5000))

        pub.publish("BTC-USDT-SWAP", _bar(20000))
        pub.publish("ETH-USDT-SWAP", _bar(10000))
        pub.publish("SOL-USDT-SWAP", _bar(10000))

        assert pub.gap_count == 1


# ── Debug cache ───────────────────────────────────────────────

class TestDebugCache:
    def test_cache_receives_bars_immediately(self):
        cache = FakeCache()
        pub = _pub(debug_cache=cache)

        pub.publish("BTC-USDT-SWAP", _bar(5000, close=100.0))
        assert len(cache.data) == 1
        assert cache.data[0][0] == "BTC-USDT-SWAP"
        assert cache.data[0][1][4] == 100.0

    def test_no_cache_no_error(self):
        pub = _pub(debug_cache=None, symbols=["BTC-USDT-SWAP"])
        pub.publish("BTC-USDT-SWAP", _bar(5000))
        assert pub.bars_received == 1


# ── flush_for_clock ───────────────────────────────────────────

class TestFlushForClock:
    def test_returns_none_before_first_complete(self):
        pub = _pub()
        pub.publish("BTC-USDT-SWAP", _bar(5000))
        result = pub.flush_for_clock()
        assert result is None

    def test_returns_complete_round(self):
        pub = _pub(symbols=["BTC-USDT-SWAP"])

        pub.publish("BTC-USDT-SWAP", _bar(5000))

        result = pub.flush_for_clock()
        assert result is not None
        assert "BTC-USDT-SWAP" in result
        assert result["BTC-USDT-SWAP"][4] == 100.0

    def test_second_call_returns_fallback(self):
        """Second call returns pending/last_round data."""
        pub = _pub(symbols=["BTC-USDT-SWAP"])

        pub.publish("BTC-USDT-SWAP", _bar(5000))

        r1 = pub.flush_for_clock()
        assert r1 is not None

        r2 = pub.flush_for_clock()
        assert r2 is not None

    def test_returns_latest_completed_round(self):
        pub = _pub(symbols=SYMBOLS_2)

        pub.publish("BTC-USDT-SWAP", _bar(5000, close=100.0))
        pub.publish("ETH-USDT-SWAP", _bar(5000, close=200.0))

        pub.publish("BTC-USDT-SWAP", _bar(10000, close=101.0))
        pub.publish("ETH-USDT-SWAP", _bar(10000, close=201.0))

        result = pub.flush_for_clock()
        assert result is not None
        assert set(result.keys()) == set(SYMBOLS_2)


# ── Stats ─────────────────────────────────────────────────────

class TestStats:
    def test_stats_dict(self):
        pub = _pub()

        for sym in SYMBOLS_3:
            pub.publish(sym, _bar(5000))

        s = pub.stats()
        assert s["bars_received"] == 3
        assert s["rounds_completed"] == 1
        assert s["forward_fill_count"] == 0
        assert s["dup_count"] == 0
        assert s["gap_count"] == 0

    def test_stats_with_forward_fill(self):
        pub = _pub(symbols=SYMBOLS_2, batch_timeout_s=0.01)

        pub.publish("BTC-USDT-SWAP", _bar(5000))
        pub.publish("ETH-USDT-SWAP", _bar(5000))

        pub.publish("BTC-USDT-SWAP", _bar(10000))
        time.sleep(0.05)
        pub.publish("BTC-USDT-SWAP", _bar(15000))

        s = pub.stats()
        assert s["rounds_completed"] == 2
        assert s["forward_fill_count"] == 1


# ── Thread safety ─────────────────────────────────────────────

class TestThreadSafety:
    def test_concurrent_publish_single_symbol(self):
        pub = _pub(symbols=["BTC-USDT-SWAP"])
        errors = []

        def publisher(start_ts, count):
            try:
                for i in range(count):
                    pub.publish("BTC-USDT-SWAP", _bar(start_ts + i * 5000))
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=publisher, args=(0, 500)),
            threading.Thread(target=publisher, args=(500 * 5000, 500)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors
        assert pub.bars_received == 1000
        assert pub.rounds_completed == 1000
