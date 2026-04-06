"""Core unit tests for the refactored dataflow layer."""

from __future__ import annotations

import numpy as np
import unittest
from unittest.mock import MagicMock

from dataflow.bars.aggregator import BarAggregator
from dataflow.cache import BarCache, BookCache, TradeCache
from dataflow.events import ASK_PX_SLICE, ASK_SZ_SLICE, BID_PX_SLICE, BID_SZ_SLICE, TRADE_SIDE_BUY, TRADE_SIDE_SELL
from dataflow.manager import DataflowManager


class BarAggregatorTest(unittest.TestCase):
    def test_ignores_unconfirmed_rows(self):
        agg = BarAggregator(agg_seconds=2)
        event = agg.on_candle1s(
            {
                "instId": "BTC-USDT-SWAP",
                "ts_recv": 1001,
                "raw": ["1000", "1", "2", "0.5", "1.5", "10", "0", "0", "0"],
            }
        )
        self.assertIsNone(event)

    def test_emits_aggregated_bar_after_target_count(self):
        agg = BarAggregator(agg_seconds=3)
        rows = [
            {
                "instId": "BTC-USDT-SWAP",
                "ts_recv": 1001,
                "raw": ["1000", "1", "3", "0.5", "2", "10", "0", "0", "1"],
            },
            {
                "instId": "BTC-USDT-SWAP",
                "ts_recv": 2001,
                "raw": ["2000", "2", "4", "1.5", "3", "11", "0", "0", "1"],
            },
            {
                "instId": "BTC-USDT-SWAP",
                "ts_recv": 3001,
                "raw": ["3000", "3", "5", "2.5", "4", "12", "0", "0", "1"],
            },
        ]

        out = None
        for row in rows:
            out = agg.on_candle1s(row)

        self.assertIsNotNone(out)
        self.assertEqual(out[0], 1000.0)
        self.assertEqual(out[1], 1.0)
        self.assertEqual(out[2], 5.0)
        self.assertEqual(out[3], 0.5)
        self.assertEqual(out[4], 4.0)
        self.assertEqual(out[5], 33.0)


class CacheTest(unittest.TestCase):
    def test_bar_cache_trims_and_returns_copies(self):
        cache = BarCache(window_length=2)
        cache.append("BTC-USDT-SWAP", np.array([1000, 1.0, 2.0, 0.5, 1.5, 10.0]))
        cache.append("BTC-USDT-SWAP", np.array([2000, 1.5, 2.5, 1.0, 2.0, 11.0]))
        cache.append("BTC-USDT-SWAP", np.array([3000, 2.0, 3.0, 1.5, 2.5, 12.0]))

        snapshot = cache.snapshot()
        self.assertEqual(snapshot["BTC-USDT-SWAP"].shape, (2, 6))
        self.assertEqual(snapshot["BTC-USDT-SWAP"][0][0], 2000.0)

        snapshot["BTC-USDT-SWAP"][0][0] = -1.0
        latest = cache.latest("BTC-USDT-SWAP")
        self.assertEqual(latest[0], 3000.0)

    def test_trade_cache_returns_deep_copies(self):
        cache = TradeCache(window_length=2)
        cache.extend("BTC-USDT-SWAP", np.array([[100.0, 1.0, TRADE_SIDE_BUY]]))
        cache.extend("BTC-USDT-SWAP", np.array([[101.0, 2.0, TRADE_SIDE_SELL]]))
        cache.extend("BTC-USDT-SWAP", np.array([[102.0, 3.0, TRADE_SIDE_BUY]]))

        rows = cache.get_window("BTC-USDT-SWAP")
        self.assertTrue(np.array_equal(rows[:, 0], np.array([101.0, 102.0])))
        rows[0, 0] = -1.0
        latest = cache.latest("BTC-USDT-SWAP")
        self.assertEqual(latest[0], 102.0)

    def test_book_cache_tracks_latest_and_history(self):
        cache = BookCache(history_length=2)
        row1 = np.full(20, np.nan)
        row1[BID_PX_SLICE.start] = 100.0
        row1[BID_SZ_SLICE.start] = 1.0
        row1[ASK_PX_SLICE.start] = 100.1
        row1[ASK_SZ_SLICE.start] = 2.0
        row2 = np.full(20, np.nan)
        row2[BID_PX_SLICE.start] = 101.0
        row2[BID_SZ_SLICE.start] = 3.0
        row2[ASK_PX_SLICE.start] = 101.1
        row2[ASK_SZ_SLICE.start] = 4.0
        row3 = np.full(20, np.nan)
        row3[BID_PX_SLICE.start] = 102.0
        row3[BID_SZ_SLICE.start] = 5.0
        row3[ASK_PX_SLICE.start] = 102.1
        row3[ASK_SZ_SLICE.start] = 6.0
        cache.extend("BTC-USDT-SWAP", np.vstack([row1, row2]))
        cache.extend("BTC-USDT-SWAP", row3.reshape(1, -1))

        latest = cache.latest("BTC-USDT-SWAP")
        self.assertEqual(latest[BID_PX_SLICE.start], 102.0)

        window = cache.get_window("BTC-USDT-SWAP")
        self.assertEqual(window.shape, (2, 20))
        self.assertEqual(window[0, BID_PX_SLICE.start], 101.0)
        self.assertEqual(window[1, BID_PX_SLICE.start], 102.0)

        snapshot = cache.snapshot()
        snapshot["BTC-USDT-SWAP"][0, BID_PX_SLICE.start] = -1.0
        latest_again = cache.latest("BTC-USDT-SWAP")
        self.assertEqual(latest_again[BID_PX_SLICE.start], 102.0)


class DataflowManagerTest(unittest.TestCase):
    def test_start_stop_respects_worker_enable_flags(self):
        manager = DataflowManager(
            symbols=["BTC-USDT-SWAP"],
            enable_trades=False,
            enable_books=False,
        )
        manager._bar_worker.start = MagicMock()
        manager._bar_worker.stop = MagicMock()
        manager._trade_worker.start = MagicMock()
        manager._trade_worker.stop = MagicMock()
        manager._book_worker.start = MagicMock()
        manager._book_worker.stop = MagicMock()

        manager.start()
        manager.stop()

        manager._bar_worker.start.assert_called_once()
        manager._bar_worker.stop.assert_called_once()
        manager._trade_worker.start.assert_not_called()
        manager._trade_worker.stop.assert_not_called()
        manager._book_worker.start.assert_not_called()
        manager._book_worker.stop.assert_not_called()

    def test_start_stop_runs_enabled_workers(self):
        manager = DataflowManager(
            symbols=["BTC-USDT-SWAP"],
            enable_trades=True,
            enable_books=True,
        )
        manager._bar_worker.start = MagicMock()
        manager._bar_worker.stop = MagicMock()
        manager._trade_worker.start = MagicMock()
        manager._trade_worker.stop = MagicMock()
        manager._book_worker.start = MagicMock()
        manager._book_worker.stop = MagicMock()

        manager.start()
        manager.stop()

        manager._trade_worker.start.assert_called_once()
        manager._trade_worker.stop.assert_called_once()
        manager._book_worker.start.assert_called_once()
        manager._book_worker.stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
