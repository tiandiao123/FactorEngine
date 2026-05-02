"""Tests for dataflow.okx.kline.adapter.KlineAdapter.

Covers:
  - Confirmed vs unconfirmed candle filtering
  - Direct mode (freq >= 60s): pass-through
  - Aggregation mode (freq < 60s): N-candle merge
  - BarPoint shape, dtype, column order
  - Gap detection and buffer reset
  - Multiple symbols with independent buffers
"""

import numpy as np
import pytest

from dataflow.okx.kline.adapter import KlineAdapter, _parse_confirmed_bar


# ── Helpers ────────────────────────────────────────────────────

def _make_candle(ts_ms: int, o=100.0, h=105.0, l=95.0, c=102.0,
                 vol=10.0, vol_ccy=1020.0, vol_ccy_quote=1020.0,
                 confirm="1") -> list:
    """Build a synthetic OKX candle array."""
    return [
        str(ts_ms), str(o), str(h), str(l), str(c),
        str(vol), str(vol_ccy), str(vol_ccy_quote), confirm,
    ]


# ── _parse_confirmed_bar ──────────────────────────────────────

class TestParseConfirmedBar:
    def test_confirmed_returns_array(self):
        raw = _make_candle(1000, confirm="1")
        bar = _parse_confirmed_bar(raw)
        assert bar is not None
        assert bar.shape == (8,)
        assert bar.dtype == np.float64
        assert bar[0] == 1000.0
        assert bar[4] == 102.0

    def test_unconfirmed_returns_none(self):
        raw = _make_candle(1000, confirm="0")
        assert _parse_confirmed_bar(raw) is None

    def test_column_order(self):
        raw = _make_candle(5000, o=1.0, h=2.0, l=0.5, c=1.5,
                           vol=100.0, vol_ccy=150.0, vol_ccy_quote=150.0)
        bar = _parse_confirmed_bar(raw)
        assert bar[0] == 5000.0    # ts
        assert bar[1] == 1.0       # open
        assert bar[2] == 2.0       # high
        assert bar[3] == 0.5       # low
        assert bar[4] == 1.5       # close
        assert bar[5] == 100.0     # vol
        assert bar[6] == 150.0     # vol_ccy
        assert bar[7] == 150.0     # vol_ccy_quote


# ── Direct mode (freq >= 60s) ─────────────────────────────────

class TestAdapterDirect:
    def test_confirmed_candle_passes_through(self):
        adapter = KlineAdapter(freq_seconds=60)
        raw = _make_candle(60000, c=200.0, confirm="1")
        bar = adapter.on_candle("BTC", raw)
        assert bar is not None
        assert bar[4] == 200.0

    def test_unconfirmed_filtered(self):
        adapter = KlineAdapter(freq_seconds=60)
        raw = _make_candle(60000, confirm="0")
        assert adapter.on_candle("BTC", raw) is None

    def test_multiple_symbols_independent(self):
        adapter = KlineAdapter(freq_seconds=60)
        bar1 = adapter.on_candle("BTC", _make_candle(60000, c=100.0))
        bar2 = adapter.on_candle("ETH", _make_candle(60000, c=200.0))
        assert bar1 is not None and bar1[4] == 100.0
        assert bar2 is not None and bar2[4] == 200.0


# ── Aggregation mode (freq < 60s) ─────────────────────────────

class TestAdapterAggregation:
    def test_3s_aggregation(self):
        adapter = KlineAdapter(freq_seconds=3)

        # Feed 3 confirmed 1s candles
        r1 = adapter.on_candle("BTC", _make_candle(1000, o=100, h=105, l=95, c=102, vol=10))
        r2 = adapter.on_candle("BTC", _make_candle(2000, o=102, h=108, l=99, c=106, vol=20))
        r3 = adapter.on_candle("BTC", _make_candle(3000, o=106, h=110, l=97, c=104, vol=15))

        assert r1 is None
        assert r2 is None
        assert r3 is not None

        bar = r3
        assert bar[0] == 1000.0      # ts = first
        assert bar[1] == 100.0       # open = first
        assert bar[2] == 110.0       # high = max(105, 108, 110)
        assert bar[3] == 95.0        # low = min(95, 99, 97)
        assert bar[4] == 104.0       # close = last
        assert bar[5] == 45.0        # vol = sum(10, 20, 15)

    def test_5s_aggregation(self):
        adapter = KlineAdapter(freq_seconds=5)
        for i in range(4):
            result = adapter.on_candle("BTC", _make_candle(1000 * (i + 1), vol=10))
            assert result is None

        result = adapter.on_candle("BTC", _make_candle(5000, vol=10))
        assert result is not None
        assert result[5] == 50.0  # 5 × 10

    def test_multiple_symbols_independent_buffers(self):
        adapter = KlineAdapter(freq_seconds=2)

        # BTC: 1 candle (not enough)
        assert adapter.on_candle("BTC", _make_candle(1000, c=100)) is None

        # ETH: 2 candles (enough)
        assert adapter.on_candle("ETH", _make_candle(1000, c=200)) is None
        bar_eth = adapter.on_candle("ETH", _make_candle(2000, c=210))
        assert bar_eth is not None
        assert bar_eth[4] == 210.0

        # BTC still needs 1 more
        bar_btc = adapter.on_candle("BTC", _make_candle(2000, c=105))
        assert bar_btc is not None
        assert bar_btc[4] == 105.0

    def test_buffer_resets_after_output(self):
        adapter = KlineAdapter(freq_seconds=2)

        # First round
        adapter.on_candle("BTC", _make_candle(1000, c=100))
        bar1 = adapter.on_candle("BTC", _make_candle(2000, c=105))
        assert bar1 is not None

        # Second round starts fresh
        assert adapter.on_candle("BTC", _make_candle(3000, c=110)) is None
        bar2 = adapter.on_candle("BTC", _make_candle(4000, c=115))
        assert bar2 is not None
        assert bar2[4] == 115.0

    def test_unconfirmed_ignored_in_aggregation(self):
        adapter = KlineAdapter(freq_seconds=2)
        adapter.on_candle("BTC", _make_candle(1000, confirm="1"))
        # Unconfirmed should not count toward the buffer
        assert adapter.on_candle("BTC", _make_candle(2000, confirm="0")) is None
        # Still need one more confirmed
        bar = adapter.on_candle("BTC", _make_candle(2000, confirm="1"))
        assert bar is not None


# ── Gap detection ──────────────────────────────────────────────

class TestAdapterGapDetection:
    def test_gap_resets_buffer(self):
        adapter = KlineAdapter(freq_seconds=3)

        # Buffer 2 candles
        adapter.on_candle("BTC", _make_candle(1000, c=100))
        adapter.on_candle("BTC", _make_candle(2000, c=101))

        # Big gap (> 2s): buffer should be cleared
        # The new candle starts a fresh buffer of 1
        result = adapter.on_candle("BTC", _make_candle(10000, c=200))
        assert result is None  # only 1 in buffer now

        # Feed 2 more to complete
        adapter.on_candle("BTC", _make_candle(11000, c=201))
        bar = adapter.on_candle("BTC", _make_candle(12000, c=202))
        assert bar is not None
        assert bar[0] == 10000.0   # ts = start of new buffer
        assert bar[4] == 202.0

    def test_no_gap_does_not_reset(self):
        adapter = KlineAdapter(freq_seconds=3)
        adapter.on_candle("BTC", _make_candle(1000))
        adapter.on_candle("BTC", _make_candle(2000))
        bar = adapter.on_candle("BTC", _make_candle(3000, c=999))
        assert bar is not None
        assert bar[4] == 999.0


# ── Edge cases ─────────────────────────────────────────────────

class TestAdapterEdgeCases:
    def test_freq_1s_immediate_output(self):
        adapter = KlineAdapter(freq_seconds=1)
        bar = adapter.on_candle("BTC", _make_candle(1000, c=42.0))
        assert bar is not None
        assert bar[4] == 42.0

    def test_invalid_freq_raises(self):
        with pytest.raises(ValueError):
            KlineAdapter(freq_seconds=0)

    def test_reset_symbol(self):
        adapter = KlineAdapter(freq_seconds=3)
        adapter.on_candle("BTC", _make_candle(1000))
        adapter.on_candle("BTC", _make_candle(2000))
        adapter.reset_symbol("BTC")
        # Buffer cleared — need 3 fresh candles
        assert adapter.on_candle("BTC", _make_candle(3000)) is None
