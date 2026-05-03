"""Unit tests for ClockDriver."""

from __future__ import annotations

import threading
import time

import pytest

from dataflow.clock_driver import ClockDriver


class TestClockDriverBasic:
    def test_register_and_tick(self):
        """Callbacks fire at least once within 2× freq."""
        results = []
        cd = ClockDriver(freq_seconds=0.5)
        cd.register(lambda: results.append(time.time()))
        cd.start()
        time.sleep(1.5)
        cd.stop()
        assert len(results) >= 1

    def test_multiple_callbacks(self):
        """All registered callbacks fire each tick."""
        counts = {"a": 0, "b": 0}
        cd = ClockDriver(freq_seconds=0.5)
        cd.register(lambda: counts.__setitem__("a", counts["a"] + 1))
        cd.register(lambda: counts.__setitem__("b", counts["b"] + 1))
        cd.start()
        time.sleep(1.5)
        cd.stop()
        assert counts["a"] >= 1
        assert counts["a"] == counts["b"]

    def test_tick_count(self):
        cd = ClockDriver(freq_seconds=0.5)
        cd.register(lambda: None)
        cd.start()
        time.sleep(1.8)
        cd.stop()
        assert cd.tick_count >= 2

    def test_stop_is_idempotent(self):
        cd = ClockDriver(freq_seconds=1.0)
        cd.register(lambda: None)
        cd.start()
        time.sleep(0.1)
        cd.stop()
        cd.stop()

    def test_no_callbacks_warning(self, caplog):
        cd = ClockDriver(freq_seconds=1.0)
        cd.start()
        time.sleep(0.1)
        cd.stop()
        assert "no registered callbacks" in caplog.text.lower()

    def test_callback_exception_does_not_crash(self):
        """A failing callback should not prevent others from firing."""
        results = []

        def bad():
            raise ValueError("boom")

        def good():
            results.append(1)

        cd = ClockDriver(freq_seconds=0.5)
        cd.register(bad)
        cd.register(good)
        cd.start()
        time.sleep(1.5)
        cd.stop()
        assert len(results) >= 1

    def test_freq_property(self):
        cd = ClockDriver(freq_seconds=3.0)
        assert cd.freq_seconds == 3.0


class TestClockDriverAlignment:
    def test_ticks_are_roughly_aligned(self):
        """Tick timestamps should be close to wall-clock boundaries."""
        timestamps = []
        freq = 1.0

        def record():
            timestamps.append(time.time())

        cd = ClockDriver(freq_seconds=freq)
        cd.register(record)
        cd.start()
        time.sleep(3.5)
        cd.stop()

        for ts in timestamps:
            remainder = ts % freq
            drift = min(remainder, freq - remainder)
            assert drift < 0.1, f"Tick at {ts} drifted {drift:.3f}s from boundary"
