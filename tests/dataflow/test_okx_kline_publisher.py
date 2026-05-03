"""Tests for dataflow.okx.kline.publisher.KlinePublisher (v2 round batching).

Covers:
  - Round batching: all symbols arrive → flush
  - Timeout + forward-fill: partial symbols + timeout → flush with fill
  - First round must wait for all symbols (no forward-fill source)
  - Round boundary: new ts triggers flush of previous round
  - Deduplication: same/old ts rejected
  - Gap detection and warning
  - Backpressure: queue full → drop
  - Debug cache receives bars immediately
  - Stats reporting (rounds_flushed, forward_fill_count, etc.)
  - Thread safety
"""

import queue
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


def _pub(symbols=None, maxsize=64, **kwargs):
    """Shorthand to create a publisher + queue pair."""
    q = queue.Queue(maxsize=maxsize)
    pub = KlinePublisher(
        bar_queue=q,
        symbols=symbols or SYMBOLS_3,
        expected_interval_ms=kwargs.pop("expected_interval_ms", 5000),
        **kwargs,
    )
    return pub, q


# ── Round batching: all symbols arrive ────────────────────────

class TestRoundBatching:
    def test_flush_when_all_symbols_arrive(self):
        pub, q = _pub()

        assert pub.publish("BTC-USDT-SWAP", _bar(5000)) is False  # 1/3
        assert pub.publish("ETH-USDT-SWAP", _bar(5000)) is False  # 2/3
        assert pub.publish("SOL-USDT-SWAP", _bar(5000)) is True   # 3/3 → flush

        assert q.qsize() == 1
        round_bars = q.get_nowait()
        assert set(round_bars.keys()) == set(SYMBOLS_3)
        assert pub.rounds_flushed == 1
        assert pub.bars_published == 3

    def test_two_rounds(self):
        pub, q = _pub()

        for sym in SYMBOLS_3:
            pub.publish(sym, _bar(5000))
        for sym in SYMBOLS_3:
            pub.publish(sym, _bar(10000))

        assert q.qsize() == 2
        assert pub.rounds_flushed == 2
        assert pub.bars_published == 6

    def test_order_independent(self):
        """Symbols can arrive in any order."""
        pub, q = _pub()

        pub.publish("SOL-USDT-SWAP", _bar(5000))
        pub.publish("BTC-USDT-SWAP", _bar(5000))
        pub.publish("ETH-USDT-SWAP", _bar(5000))

        assert q.qsize() == 1
        round_bars = q.get_nowait()
        assert set(round_bars.keys()) == set(SYMBOLS_3)

    def test_single_symbol(self):
        """With only 1 symbol, every publish triggers a flush."""
        pub, q = _pub(symbols=["BTC-USDT-SWAP"])

        assert pub.publish("BTC-USDT-SWAP", _bar(5000)) is True
        assert pub.publish("BTC-USDT-SWAP", _bar(10000)) is True

        assert q.qsize() == 2
        assert pub.rounds_flushed == 2


# ── First round: must wait for all symbols ────────────────────

class TestFirstRound:
    def test_first_round_partial_no_flush(self):
        """First round with missing symbols does NOT flush (no forward-fill)."""
        pub, q = _pub(batch_timeout_s=0.01)

        pub.publish("BTC-USDT-SWAP", _bar(5000))
        pub.publish("ETH-USDT-SWAP", _bar(5000))
        time.sleep(0.05)
        # Timeout has passed, but first round has no last_round → no flush
        # Next publish should check timeout but still not flush
        pub.publish("BTC-USDT-SWAP", _bar(10000))  # triggers ts boundary → tries flush old
        # The old round (ts=5000) is incomplete first round → not flushed
        assert pub.rounds_flushed == 0

    def test_first_round_complete_flushes(self):
        pub, q = _pub()

        for sym in SYMBOLS_3:
            pub.publish(sym, _bar(5000))

        assert q.qsize() == 1
        assert pub.rounds_flushed == 1


# ── Timeout + forward-fill ────────────────────────────────────

class TestTimeoutForwardFill:
    def test_timeout_triggers_forward_fill(self):
        pub, q = _pub(batch_timeout_s=0.05)

        # First round: all arrive
        for sym in SYMBOLS_3:
            pub.publish(sym, _bar(5000))
        assert pub.rounds_flushed == 1

        # Second round: only 2/3 arrive
        pub.publish("BTC-USDT-SWAP", _bar(10000))
        pub.publish("ETH-USDT-SWAP", _bar(10000))
        time.sleep(0.1)

        # Next publish triggers timeout check
        pub.publish("BTC-USDT-SWAP", _bar(15000))

        # The ts=10000 round should have been flushed with SOL forward-filled
        assert pub.rounds_flushed == 2
        assert pub.forward_fill_count == 1

        # Verify the flushed round has all 3 symbols
        # First round was at q position 0, second at position 1
        q.get_nowait()  # discard first round
        round2 = q.get_nowait()
        assert set(round2.keys()) == set(SYMBOLS_3)
        assert int(round2["SOL-USDT-SWAP"][0]) == 5000  # forward-filled from round 1

    def test_forward_fill_value_is_last_round(self):
        pub, q = _pub(symbols=SYMBOLS_2, batch_timeout_s=0.05)

        # Round 1: all arrive
        pub.publish("BTC-USDT-SWAP", _bar(5000, close=100.0))
        pub.publish("ETH-USDT-SWAP", _bar(5000, close=200.0))
        assert pub.rounds_flushed == 1

        # Round 2: only BTC
        pub.publish("BTC-USDT-SWAP", _bar(10000, close=101.0))
        time.sleep(0.1)

        # Trigger timeout check
        pub.publish("BTC-USDT-SWAP", _bar(15000, close=102.0))

        assert pub.rounds_flushed == 2
        q.get_nowait()
        round2 = q.get_nowait()
        assert round2["ETH-USDT-SWAP"][4] == 200.0  # close from round 1


# ── Round boundary: ts mismatch ───────────────────────────────

class TestRoundBoundary:
    def test_new_ts_flushes_previous_round(self):
        """When a bar with a new ts arrives, the old round is flushed first."""
        pub, q = _pub(symbols=SYMBOLS_2)

        # Round 1: both arrive
        pub.publish("BTC-USDT-SWAP", _bar(5000))
        pub.publish("ETH-USDT-SWAP", _bar(5000))
        assert pub.rounds_flushed == 1

        # Round 2: BTC arrives, then BTC arrives again with ts=15000
        pub.publish("BTC-USDT-SWAP", _bar(10000))
        # Now ETH hasn't arrived yet for ts=10000. BTC with ts=15000 arrives:
        pub.publish("BTC-USDT-SWAP", _bar(15000))

        # ts=10000 round should be flushed (ETH forward-filled)
        assert pub.rounds_flushed == 2
        assert pub.forward_fill_count == 1


# ── Deduplication ─────────────────────────────────────────────

class TestDedup:
    def test_same_ts_rejected(self):
        pub, q = _pub(symbols=["BTC-USDT-SWAP"])

        pub.publish("BTC-USDT-SWAP", _bar(5000))
        assert pub.publish("BTC-USDT-SWAP", _bar(5000)) is False
        assert pub.dup_count == 1

    def test_old_ts_rejected(self):
        pub, q = _pub(symbols=["BTC-USDT-SWAP"])

        pub.publish("BTC-USDT-SWAP", _bar(10000))
        assert pub.publish("BTC-USDT-SWAP", _bar(5000)) is False
        assert pub.dup_count == 1

    def test_dedup_per_symbol(self):
        pub, q = _pub()

        pub.publish("BTC-USDT-SWAP", _bar(5000))
        # Same ts for a different symbol is OK
        result = pub.publish("ETH-USDT-SWAP", _bar(5000))
        assert result is False  # not flushed yet (2/3)
        assert pub.dup_count == 0


# ── Gap detection ─────────────────────────────────────────────

class TestGap:
    def test_gap_detected(self):
        pub, q = _pub(symbols=["BTC-USDT-SWAP"])

        pub.publish("BTC-USDT-SWAP", _bar(5000))
        pub.publish("BTC-USDT-SWAP", _bar(20000))
        assert pub.gap_count == 1

    def test_no_gap_within_threshold(self):
        pub, q = _pub(symbols=["BTC-USDT-SWAP"])

        pub.publish("BTC-USDT-SWAP", _bar(5000))
        pub.publish("BTC-USDT-SWAP", _bar(10000))
        assert pub.gap_count == 0

    def test_gap_on_first_bar_not_counted(self):
        pub, q = _pub(symbols=["BTC-USDT-SWAP"])

        pub.publish("BTC-USDT-SWAP", _bar(100000))
        assert pub.gap_count == 0

    def test_gap_boundary_exact_2x_not_counted(self):
        pub, q = _pub(symbols=["BTC-USDT-SWAP"])

        pub.publish("BTC-USDT-SWAP", _bar(5000))
        pub.publish("BTC-USDT-SWAP", _bar(15000))
        assert pub.gap_count == 0

    def test_gap_boundary_just_over_2x(self):
        pub, q = _pub(symbols=["BTC-USDT-SWAP"])

        pub.publish("BTC-USDT-SWAP", _bar(5000))
        pub.publish("BTC-USDT-SWAP", _bar(15001))
        assert pub.gap_count == 1

    def test_gap_multi_symbol_independent(self):
        pub, q = _pub()

        pub.publish("BTC-USDT-SWAP", _bar(5000))
        pub.publish("ETH-USDT-SWAP", _bar(5000))
        pub.publish("SOL-USDT-SWAP", _bar(5000))  # flush round 1

        pub.publish("BTC-USDT-SWAP", _bar(20000))  # BTC gap
        pub.publish("ETH-USDT-SWAP", _bar(10000))  # ETH normal
        pub.publish("SOL-USDT-SWAP", _bar(10000))  # SOL normal — won't flush (ts mismatch)

        assert pub.gap_count == 1


# ── Backpressure ──────────────────────────────────────────────

class TestBackpressure:
    def test_queue_full_drops_round(self):
        pub, q = _pub(symbols=["BTC-USDT-SWAP"], maxsize=1)

        assert pub.publish("BTC-USDT-SWAP", _bar(5000)) is True   # round 1 → queue
        assert pub.publish("BTC-USDT-SWAP", _bar(10000)) is False  # round 2 → full

        assert pub.drop_count == 1
        assert pub.rounds_flushed == 1


# ── Debug cache ───────────────────────────────────────────────

class TestDebugCache:
    def test_cache_receives_bars_immediately(self):
        cache = FakeCache()
        pub, q = _pub(debug_cache=cache)

        pub.publish("BTC-USDT-SWAP", _bar(5000, close=100.0))
        # Cache receives bar immediately, even before round is flushed
        assert len(cache.data) == 1
        assert cache.data[0][0] == "BTC-USDT-SWAP"
        assert cache.data[0][1][4] == 100.0

    def test_no_cache_no_error(self):
        pub, q = _pub(debug_cache=None, symbols=["BTC-USDT-SWAP"])
        pub.publish("BTC-USDT-SWAP", _bar(5000))
        assert pub.bars_published == 1


# ── Stats ─────────────────────────────────────────────────────

class TestStats:
    def test_stats_dict(self):
        pub, q = _pub()

        for sym in SYMBOLS_3:
            pub.publish(sym, _bar(5000))

        s = pub.stats()
        assert s["bars_published"] == 3
        assert s["rounds_flushed"] == 1
        assert s["forward_fill_count"] == 0
        assert s["dup_count"] == 0
        assert s["gap_count"] == 0
        assert s["drop_count"] == 0

    def test_stats_with_forward_fill(self):
        pub, q = _pub(symbols=SYMBOLS_2, batch_timeout_s=0.01)

        # Round 1: complete
        pub.publish("BTC-USDT-SWAP", _bar(5000))
        pub.publish("ETH-USDT-SWAP", _bar(5000))

        # Round 2: only BTC
        pub.publish("BTC-USDT-SWAP", _bar(10000))
        time.sleep(0.05)
        # Trigger timeout via next publish
        pub.publish("BTC-USDT-SWAP", _bar(15000))

        s = pub.stats()
        assert s["rounds_flushed"] == 2
        assert s["forward_fill_count"] == 1


# ── Flush on stop ─────────────────────────────────────────────

class TestFlush:
    def test_flush_pending_round(self):
        """flush() pushes incomplete round with forward-fill."""
        pub, q = _pub(symbols=SYMBOLS_2)

        # Round 1: complete
        pub.publish("BTC-USDT-SWAP", _bar(5000))
        pub.publish("ETH-USDT-SWAP", _bar(5000))

        # Round 2: partial
        pub.publish("BTC-USDT-SWAP", _bar(10000))

        assert pub.flush() is True
        assert pub.rounds_flushed == 2
        assert pub.forward_fill_count == 1

    def test_flush_empty_noop(self):
        pub, q = _pub()
        assert pub.flush() is False

    def test_flush_first_round_incomplete(self):
        """flush() on first incomplete round does nothing (no fill source)."""
        pub, q = _pub()
        pub.publish("BTC-USDT-SWAP", _bar(5000))
        assert pub.flush() is False
        assert pub.rounds_flushed == 0


# ── Thread safety ─────────────────────────────────────────────

class TestThreadSafety:
    def test_concurrent_publish_single_symbol(self):
        """Multiple threads publishing for the same single-symbol publisher."""
        pub, q = _pub(symbols=["BTC-USDT-SWAP"], maxsize=10000)
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
        assert pub.bars_published == 1000
        assert pub.rounds_flushed == 1000
