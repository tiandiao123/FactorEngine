"""Tests for BinanceKlineAdapter — kline → BarPoint conversion."""

import math

import numpy as np
import pytest

from dataflow.binance.kline.adapter import BinanceKlineAdapter


class TestBinanceKlineAdapter:

    def _make_kline(self, *, x: bool = True, **overrides) -> dict:
        k = {
            "t": 1714838400000,
            "T": 1714838459999,
            "s": "BTCUSDT",
            "i": "1m",
            "o": "97500.00",
            "h": "97600.00",
            "l": "97400.00",
            "c": "97550.00",
            "v": "12.345",
            "q": "1203456.78",
            "n": 42,
            "x": x,
        }
        k.update(overrides)
        return k

    def test_confirmed_kline(self):
        adapter = BinanceKlineAdapter()
        k = self._make_kline(x=True)
        bar = adapter.on_candle("BTC-USDT", k)

        assert bar is not None
        assert bar.shape == (8,)
        assert bar.dtype == np.float64
        assert bar[0] == 1714838400000     # ts
        assert bar[1] == pytest.approx(97500.00)  # open
        assert bar[2] == pytest.approx(97600.00)  # high
        assert bar[3] == pytest.approx(97400.00)  # low
        assert bar[4] == pytest.approx(97550.00)  # close
        assert bar[5] == pytest.approx(12.345)     # volume (base)
        assert bar[6] == pytest.approx(1203456.78) # quote volume
        assert bar[7] == 0.0                       # placeholder

    def test_unconfirmed_kline_returns_none(self):
        adapter = BinanceKlineAdapter()
        k = self._make_kline(x=False)
        bar = adapter.on_candle("BTC-USDT", k)
        assert bar is None

    def test_missing_x_defaults_to_false(self):
        adapter = BinanceKlineAdapter()
        k = self._make_kline()
        del k["x"]
        bar = adapter.on_candle("BTC-USDT", k)
        assert bar is None

    def test_multiple_symbols(self):
        adapter = BinanceKlineAdapter()

        btc = self._make_kline(o="97500", c="97600")
        eth = self._make_kline(o="3800", c="3850", v="100.5", q="381900.0")

        bar_btc = adapter.on_candle("BTC-USDT", btc)
        bar_eth = adapter.on_candle("ETH-USDT", eth)

        assert bar_btc is not None
        assert bar_eth is not None
        assert bar_btc[1] == pytest.approx(97500.0)
        assert bar_eth[1] == pytest.approx(3800.0)
