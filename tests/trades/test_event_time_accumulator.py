"""Unit tests for EventTimeTradesAccumulator."""

from __future__ import annotations

import math

import pytest

from dataflow.trades.accumulator import EventTimeTradesAccumulator


def test_update_assigns_bucket_by_trade_ts_not_receive_ts():
    acc = EventTimeTradesAccumulator(freq_ms=1000, allowed_lateness_ms=500)

    accepted = acc.update(
        100.0,
        2.0,
        "buy",
        trade_ts_ms=10_000_990,
        receive_ts_ms=10_001_100,
    )

    assert accepted is True
    snaps = acc.flush_ready(10_002_000)
    assert len(snaps) == 1
    snap = snaps[0]
    assert snap.bucket_start_ms == 10_000_000
    assert snap.bucket_end_ms == 10_001_000
    assert snap.open == pytest.approx(100.0)
    assert snap.volume == pytest.approx(2.0)


def test_late_trade_within_allowed_lateness_is_accepted():
    acc = EventTimeTradesAccumulator(freq_ms=1000, allowed_lateness_ms=500)

    accepted = acc.update(
        101.0,
        1.0,
        "buy",
        trade_ts_ms=10_000_999,
        receive_ts_ms=10_001_499,
    )

    assert accepted is True
    assert acc.late_trade_count == 1
    assert acc.dropped_late_trade_count == 0
    snaps = acc.flush_ready(10_002_000)
    assert [snap.bucket_start_ms for snap in snaps] == [10_000_000]


def test_trade_after_allowed_lateness_is_dropped():
    acc = EventTimeTradesAccumulator(freq_ms=1000, allowed_lateness_ms=500)

    accepted = acc.update(
        101.0,
        1.0,
        "buy",
        trade_ts_ms=10_000_999,
        receive_ts_ms=10_001_501,
    )

    assert accepted is False
    assert acc.late_trade_count == 1
    assert acc.dropped_late_trade_count == 1
    assert acc.pending_bucket_count == 0
    assert acc.flush_ready(10_002_000) == []


def test_flush_ready_emits_only_mature_buckets():
    acc = EventTimeTradesAccumulator(freq_ms=1000, allowed_lateness_ms=500)
    acc.update(100.0, 1.0, "buy", trade_ts_ms=10_000_100, receive_ts_ms=10_000_150)
    acc.update(110.0, 1.0, "buy", trade_ts_ms=10_001_100, receive_ts_ms=10_001_150)

    assert acc.flush_ready(10_001_000) == []

    snaps = acc.flush_ready(10_002_000)
    assert [snap.bucket_start_ms for snap in snaps] == [10_000_000]
    assert snaps[0].close == pytest.approx(100.0)


def test_flush_ready_emits_multiple_buckets_in_order():
    acc = EventTimeTradesAccumulator(freq_ms=1000, allowed_lateness_ms=500)
    acc.update(100.0, 1.0, "buy", trade_ts_ms=10_000_100, receive_ts_ms=10_000_150)
    acc.update(101.0, 1.0, "buy", trade_ts_ms=10_001_100, receive_ts_ms=10_001_150)
    acc.update(102.0, 1.0, "buy", trade_ts_ms=10_002_100, receive_ts_ms=10_002_150)

    snaps = acc.flush_ready(10_004_000)

    assert [snap.bucket_start_ms for snap in snaps] == [
        10_000_000,
        10_001_000,
        10_002_000,
    ]
    assert [snap.close for snap in snaps] == pytest.approx([100.0, 101.0, 102.0])


def test_out_of_order_trades_fix_open_and_close_by_trade_ts():
    acc = EventTimeTradesAccumulator(freq_ms=1000, allowed_lateness_ms=500)

    acc.update(105.0, 1.0, "buy", trade_ts_ms=10_000_500, receive_ts_ms=10_000_510)
    acc.update(100.0, 1.0, "sell", trade_ts_ms=10_000_100, receive_ts_ms=10_000_520)
    acc.update(110.0, 1.0, "buy", trade_ts_ms=10_000_900, receive_ts_ms=10_000_530)

    snap = acc.flush_ready(10_002_000)[0]
    assert snap.open == pytest.approx(100.0)
    assert snap.high == pytest.approx(110.0)
    assert snap.low == pytest.approx(100.0)
    assert snap.close == pytest.approx(110.0)
    assert snap.first_trade_ts_ms == 10_000_100
    assert snap.last_trade_ts_ms == 10_000_900


def test_same_millisecond_close_uses_later_arrival():
    acc = EventTimeTradesAccumulator(freq_ms=1000, allowed_lateness_ms=500)

    acc.update(100.0, 1.0, "buy", trade_ts_ms=10_000_100, receive_ts_ms=10_000_101)
    acc.update(101.0, 1.0, "buy", trade_ts_ms=10_000_100, receive_ts_ms=10_000_102)

    snap = acc.flush_ready(10_002_000)[0]
    assert snap.open == pytest.approx(100.0)
    assert snap.close == pytest.approx(101.0)


def test_forward_fill_empty_bucket_after_last_close():
    acc = EventTimeTradesAccumulator(freq_ms=1000, allowed_lateness_ms=500)
    acc.update(100.0, 1.0, "buy", trade_ts_ms=10_000_100, receive_ts_ms=10_000_150)
    acc.update(103.0, 1.0, "buy", trade_ts_ms=10_002_100, receive_ts_ms=10_002_150)

    snaps = acc.flush_ready(10_004_000)

    assert [snap.bucket_start_ms for snap in snaps] == [
        10_000_000,
        10_001_000,
        10_002_000,
    ]
    assert snaps[0].trade_count == 1
    assert snaps[1].trade_count == 0
    assert snaps[1].open == pytest.approx(100.0)
    assert snaps[1].high == pytest.approx(100.0)
    assert snaps[1].low == pytest.approx(100.0)
    assert snaps[1].close == pytest.approx(100.0)
    assert snaps[1].vwap == pytest.approx(100.0)
    assert snaps[1].volume == 0.0
    assert snaps[2].close == pytest.approx(103.0)


def test_no_empty_bucket_before_first_trade():
    acc = EventTimeTradesAccumulator(freq_ms=1000, allowed_lateness_ms=500)

    assert acc.flush_ready(10_004_000) == []


def test_pop_ready_bucket_returns_none_before_first_trade():
    acc = EventTimeTradesAccumulator(freq_ms=1000, allowed_lateness_ms=500)

    assert acc.pop_ready_bucket(10_000_000, 10_002_000) is None


def test_pop_ready_bucket_returns_none_when_target_not_mature():
    acc = EventTimeTradesAccumulator(freq_ms=1000, allowed_lateness_ms=500)
    acc.update(100.0, 1.0, "buy", trade_ts_ms=10_000_100, receive_ts_ms=10_000_150)

    assert acc.pop_ready_bucket(10_000_000, 10_001_000) is None
    assert acc.pending_bucket_count == 1


def test_pop_ready_bucket_pops_only_requested_queue_head():
    acc = EventTimeTradesAccumulator(freq_ms=1000, allowed_lateness_ms=500)
    acc.update(100.0, 1.0, "buy", trade_ts_ms=10_000_100, receive_ts_ms=10_000_150)
    acc.update(101.0, 1.0, "buy", trade_ts_ms=10_001_100, receive_ts_ms=10_001_150)

    assert acc.pop_ready_bucket(10_001_000, 10_003_000) is None
    assert acc.pending_bucket_count == 2

    snap = acc.pop_ready_bucket(10_000_000, 10_003_000)
    assert snap is not None
    assert snap.bucket_start_ms == 10_000_000
    assert snap.close == pytest.approx(100.0)
    assert acc.pending_bucket_count == 1


def test_pop_ready_bucket_forward_fills_missing_target_after_last_close():
    acc = EventTimeTradesAccumulator(freq_ms=1000, allowed_lateness_ms=500)
    acc.update(100.0, 1.0, "buy", trade_ts_ms=10_000_100, receive_ts_ms=10_000_150)
    acc.update(103.0, 1.0, "buy", trade_ts_ms=10_002_100, receive_ts_ms=10_002_150)

    first = acc.pop_ready_bucket(10_000_000, 10_004_000)
    missing = acc.pop_ready_bucket(10_001_000, 10_004_000)
    third = acc.pop_ready_bucket(10_002_000, 10_004_000)

    assert first is not None
    assert missing is not None
    assert third is not None
    assert missing.trade_count == 0
    assert missing.close == pytest.approx(100.0)
    assert third.close == pytest.approx(103.0)


def test_pop_ready_bucket_skips_inactive_missing_target():
    acc = EventTimeTradesAccumulator(freq_ms=1000, allowed_lateness_ms=500)
    acc.update(100.0, 1.0, "buy", trade_ts_ms=10_000_100, receive_ts_ms=10_000_150)
    acc.update(103.0, 1.0, "buy", trade_ts_ms=10_002_100, receive_ts_ms=10_002_150)

    first = acc.pop_ready_bucket(10_000_000, 10_004_000)
    missing = acc.pop_ready_bucket(10_001_000, 10_004_000, active=False)
    third = acc.pop_ready_bucket(10_002_000, 10_004_000)

    assert first is not None
    assert missing is None
    assert third is not None
    assert third.close == pytest.approx(103.0)


def test_inactive_empty_bucket_is_not_forward_filled():
    acc = EventTimeTradesAccumulator(freq_ms=1000, allowed_lateness_ms=500)
    acc.update(100.0, 1.0, "buy", trade_ts_ms=10_000_100, receive_ts_ms=10_000_150)
    acc.update(103.0, 1.0, "buy", trade_ts_ms=10_002_100, receive_ts_ms=10_002_150)

    snaps = acc.flush_ready(10_004_000, active=False)

    assert [snap.bucket_start_ms for snap in snaps] == [10_000_000, 10_002_000]


def test_emitted_bucket_is_deleted_and_late_trade_does_not_recreate_it():
    acc = EventTimeTradesAccumulator(freq_ms=1000, allowed_lateness_ms=500)
    acc.update(100.0, 1.0, "buy", trade_ts_ms=10_000_100, receive_ts_ms=10_000_150)

    first = acc.flush_ready(10_002_000)
    assert [snap.bucket_start_ms for snap in first] == [10_000_000]
    assert acc.pending_bucket_count == 0

    accepted = acc.update(
        999.0,
        1.0,
        "buy",
        trade_ts_ms=10_000_200,
        receive_ts_ms=10_002_100,
    )

    assert accepted is False
    assert acc.dropped_late_trade_count == 1
    assert acc.pending_bucket_count == 0
    followup = acc.flush_ready(10_003_000)
    assert [snap.bucket_start_ms for snap in followup] == [10_001_000]
    assert followup[0].trade_count == 0
    assert followup[0].close == pytest.approx(100.0)


def test_snapshot_to_dict_preserves_runtime_fields():
    acc = EventTimeTradesAccumulator(freq_ms=1000, allowed_lateness_ms=500)
    acc.update(100.0, 2.0, "buy", trade_ts_ms=10_000_100, receive_ts_ms=10_000_150)

    snap_dict = acc.flush_ready(10_002_000)[0].to_dict()

    for key in (
        "vwap",
        "buy_vol",
        "sell_vol",
        "trade_count",
        "open",
        "high",
        "low",
        "close",
        "volume",
    ):
        assert key in snap_dict
    assert snap_dict["vwap"] == pytest.approx(100.0)


def test_zero_size_trade_has_nan_vwap_but_keeps_ohlc():
    acc = EventTimeTradesAccumulator(freq_ms=1000, allowed_lateness_ms=500)
    acc.update(100.0, 0.0, "buy", trade_ts_ms=10_000_100, receive_ts_ms=10_000_150)

    snap = acc.flush_ready(10_002_000)[0]

    assert math.isnan(snap.vwap)
    assert snap.open == pytest.approx(100.0)
    assert snap.close == pytest.approx(100.0)
    assert snap.volume == 0.0
