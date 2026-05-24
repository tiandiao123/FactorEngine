"""Unit tests for ClockDriver (v4 — aggregation center)."""

from __future__ import annotations

import queue
import threading
import time

import numpy as np
import pytest

from dataflow.clock_driver import ClockDriver


class FakeWorker:
    """Minimal worker with a flush() that records calls."""

    def __init__(self, data=None):
        self._data = data
        self.flush_count = 0

    def flush(self):
        self.flush_count += 1
        return self._data


class TestClockDriverBasic:
    def test_register_and_tick(self):
        """Workers are flushed at least once within 2× freq."""
        q = queue.Queue(maxsize=64)
        cd = ClockDriver(freq_seconds=0.5, bar_queue=q)
        w = FakeWorker(data={"BTC": np.zeros(8)})
        cd.register("test", w)
        cd.start()
        time.sleep(1.5)
        cd.stop()
        assert w.flush_count >= 1

    def test_multiple_workers(self):
        """All registered workers are flushed each tick."""
        q = queue.Queue(maxsize=64)
        cd = ClockDriver(freq_seconds=0.5, bar_queue=q)
        w1 = FakeWorker(data={"BTC": np.zeros(8)})
        w2 = FakeWorker(data=None)
        cd.register("w1", w1)
        cd.register("w2", w2)
        cd.start()
        time.sleep(1.5)
        cd.stop()
        assert w1.flush_count >= 1
        assert w1.flush_count == w2.flush_count

    def test_tick_count(self):
        q = queue.Queue(maxsize=64)
        cd = ClockDriver(freq_seconds=0.5, bar_queue=q)
        cd.register("test", FakeWorker())
        cd.start()
        time.sleep(1.8)
        cd.stop()
        assert cd.tick_count >= 2

    def test_stop_is_idempotent(self):
        q = queue.Queue(maxsize=64)
        cd = ClockDriver(freq_seconds=1.0, bar_queue=q)
        cd.register("test", FakeWorker())
        cd.start()
        time.sleep(0.1)
        cd.stop()
        cd.stop()

    def test_no_workers_warning(self, caplog):
        q = queue.Queue(maxsize=64)
        cd = ClockDriver(freq_seconds=1.0, bar_queue=q)
        cd.start()
        time.sleep(0.1)
        cd.stop()
        assert "no registered workers" in caplog.text.lower()

    def test_worker_exception_does_not_crash(self):
        """A failing worker should not prevent others from being flushed."""
        q = queue.Queue(maxsize=64)

        class BadWorker:
            def flush(self):
                raise ValueError("boom")

        good = FakeWorker(data={"BTC": np.zeros(8)})

        cd = ClockDriver(freq_seconds=0.5, bar_queue=q)
        cd.register("bad", BadWorker())
        cd.register("good", good)
        cd.start()
        time.sleep(1.5)
        cd.stop()
        assert good.flush_count >= 1

    def test_freq_property(self):
        q = queue.Queue(maxsize=4)
        cd = ClockDriver(freq_seconds=3.0, bar_queue=q)
        assert cd.freq_seconds == 3.0

    def test_duplicate_worker_name_raises(self):
        q = queue.Queue(maxsize=4)
        cd = ClockDriver(freq_seconds=1.0, bar_queue=q)
        cd.register("kline", FakeWorker())
        with pytest.raises(ValueError, match="already registered"):
            cd.register("kline", FakeWorker())


class TestClockDriverAlignment:
    def test_ticks_are_roughly_aligned(self):
        """Tick timestamps should be close to wall-clock boundaries."""
        timestamps = []
        freq = 1.0

        class TimestampWorker:
            def flush(self):
                timestamps.append(time.time())
                return None

        q = queue.Queue(maxsize=64)
        cd = ClockDriver(freq_seconds=freq, bar_queue=q)
        cd.register("timer", TimestampWorker())
        cd.start()
        time.sleep(3.5)
        cd.stop()

        for ts in timestamps:
            remainder = ts % freq
            drift = min(remainder, freq - remainder)
            assert drift < 0.1, f"Tick at {ts} drifted {drift:.3f}s from boundary"


class TestClockDriverQueue:
    def test_pushes_to_bar_queue(self):
        """ClockDriver pushes aggregated data to bar_queue."""
        q = queue.Queue(maxsize=64)
        kline_data = {"BTC": np.array([5000, 100, 106, 98, 103, 80, 0, 0], dtype=np.float64)}
        cd = ClockDriver(freq_seconds=0.5, bar_queue=q)
        cd.register("kline", FakeWorker(data=kline_data))
        cd.start()
        time.sleep(1.5)
        cd.stop()

        assert q.qsize() >= 1
        round_bars, round_trades = q.get_nowait()
        assert "BTC" in round_bars

    def test_empty_flush_no_push(self):
        """If all workers return None/empty, nothing is pushed."""
        q = queue.Queue(maxsize=64)
        cd = ClockDriver(freq_seconds=0.5, bar_queue=q)
        cd.register("empty", FakeWorker(data=None))
        cd.start()
        time.sleep(1.5)
        cd.stop()
        assert q.qsize() == 0

    def test_drop_count_on_full_queue(self):
        q = queue.Queue(maxsize=1)
        kline_data = {"BTC": np.array([5000, 100, 106, 98, 103, 80, 0, 0], dtype=np.float64)}
        cd = ClockDriver(freq_seconds=0.3, bar_queue=q)
        cd.register("kline", FakeWorker(data=kline_data))
        cd.start()
        time.sleep(2.0)
        cd.stop()
        assert cd.drop_count >= 1 or q.qsize() >= 1

    def test_after_flush_commit_runs_after_queue_put(self):
        q = queue.Queue(maxsize=8)
        events = []
        kline_data = {
            "BTC": np.array([5000, 100, 106, 98, 103, 80, 0, 0], dtype=np.float64)
        }

        def after_flush(bucket_ts_ms):
            events.append(("hook", bucket_ts_ms, q.qsize()))
            q.put_nowait({"type": "symbol_diff", "version": "v2"})

        cd = ClockDriver(
            freq_seconds=0.5,
            bar_queue=q,
            after_flush_commit=after_flush,
        )
        cd.register("kline", FakeWorker(data=kline_data))
        cd._flush_and_aggregate()

        assert events == [("hook", 5000, 1)]
        first = q.get_nowait()
        second = q.get_nowait()
        assert isinstance(first, tuple)
        assert second["type"] == "symbol_diff"

    def test_structured_payload_includes_universe_metadata(self):
        q = queue.Queue(maxsize=8)
        kline_data = {
            "BTC": np.array([5000, 100, 106, 98, 103, 80, 0, 0], dtype=np.float64)
        }
        cd = ClockDriver(
            freq_seconds=0.5,
            bar_queue=q,
            structured_payload=True,
            symbol_version_getter=lambda: "v1",
            active_symbols_getter=lambda: {"BTC", "ETH"},
        )
        cd.register("kline", FakeWorker(data=kline_data))
        cd._flush_and_aggregate()

        payload = q.get_nowait()
        assert payload["type"] == "bars"
        assert payload["bucket_ts_ms"] == 5000
        assert payload["symbol_version"] == "v1"
        assert payload["active_symbols"] == {"BTC", "ETH"}
        assert "BTC" in payload["bars"]
