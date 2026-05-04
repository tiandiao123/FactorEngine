"""Tests for BinanceKlineWorker — flush and publisher integration."""

import numpy as np
import pytest

from dataflow.binance.kline.worker import BinanceKlineWorker


class TestBinanceKlineWorkerFlush:
    """Test BinanceKlineWorker._on_candle() → publisher → flush()."""

    def _make_worker(self, symbols=None, freq=60):
        symbols = symbols or ["BTC-USDT", "ETH-USDT"]
        return BinanceKlineWorker(symbols=symbols, freq_seconds=freq)

    def _confirmed_kline(self, ts: int, o: float, h: float, l: float,
                         c: float, v: float, q: float) -> dict:
        return {
            "t": ts, "o": str(o), "h": str(h), "l": str(l),
            "c": str(c), "v": str(v), "q": str(q), "x": True,
        }

    def test_flush_returns_none_before_data(self):
        worker = self._make_worker()
        result = worker.flush()
        assert result is None

    def test_flush_returns_round_after_all_symbols(self):
        worker = self._make_worker(["BTC-USDT", "ETH-USDT"])
        ts = 1714838400000

        worker._on_candle("BTC-USDT", self._confirmed_kline(
            ts, 97500, 97600, 97400, 97550, 12.0, 1170000,
        ))
        worker._on_candle("ETH-USDT", self._confirmed_kline(
            ts, 3800, 3850, 3750, 3820, 100, 382000,
        ))

        result = worker.flush()
        assert result is not None
        assert "BTC-USDT" in result
        assert "ETH-USDT" in result

        btc = result["BTC-USDT"]
        assert btc.shape == (8,)
        assert btc[0] == ts
        assert btc[4] == pytest.approx(97550.0)

    def test_unconfirmed_kline_ignored(self):
        worker = self._make_worker(["BTC-USDT"])
        k = {"t": 1714838400000, "o": "97500", "h": "97600", "l": "97400",
             "c": "97550", "v": "12", "q": "1170000", "x": False}
        worker._on_candle("BTC-USDT", k)
        assert worker.bar_count == 0

    def test_bar_count_increments(self):
        worker = self._make_worker(["BTC-USDT"])
        ts = 1714838400000
        worker._on_candle("BTC-USDT", self._confirmed_kline(
            ts, 97500, 97600, 97400, 97550, 12, 1170000,
        ))
        assert worker.bar_count == 1

    def test_stats(self):
        worker = self._make_worker(["BTC-USDT"])
        s = worker.stats()
        assert "bars_received" in s
        assert "rounds_completed" in s
        assert "adapter_bar_count" in s


class TestBinanceKlineWorkerEngine:
    """Test that BinanceKlineWorker flush output is compatible with Engine."""

    def test_flush_output_format(self):
        worker = BinanceKlineWorker(
            symbols=["BTC-USDT", "ETH-USDT"], freq_seconds=60,
        )
        ts = 1714838400000

        for sym, px in [("BTC-USDT", 97500), ("ETH-USDT", 3800)]:
            worker._on_candle(sym, {
                "t": ts, "o": str(px), "h": str(px + 50),
                "l": str(px - 50), "c": str(px + 20),
                "v": "10.0", "q": str(px * 10), "x": True,
            })

        result = worker.flush()
        assert result is not None
        for sym in ("BTC-USDT", "ETH-USDT"):
            bar = result[sym]
            assert bar.dtype == np.float64
            assert bar.shape == (8,)
            assert int(bar[0]) == ts
