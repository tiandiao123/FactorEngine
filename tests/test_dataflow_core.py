"""Core unit tests for the refactored dataflow layer."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from dataflow.bars.aggregator import BarAggregator
from dataflow.cache import BarCache, BookCache, TradeCache
from dataflow.events import BarEvent, BookEvent, BookLevel, TradeEvent
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
        self.assertEqual(out.symbol, "BTC-USDT-SWAP")
        self.assertEqual(out.channel, "bar_3s")
        self.assertEqual(out.ts_event, 1000)
        self.assertEqual(out.ts_recv, 3001)
        self.assertEqual(out.open, 1.0)
        self.assertEqual(out.high, 5.0)
        self.assertEqual(out.low, 0.5)
        self.assertEqual(out.close, 4.0)
        self.assertEqual(out.vol, 33.0)


class CacheTest(unittest.TestCase):
    def test_bar_cache_trims_and_returns_copies(self):
        cache = BarCache(window_length=2)
        cache.append(
            BarEvent("BTC-USDT-SWAP", "bar_5s", 1000, 1001, 1.0, 2.0, 0.5, 1.5, 10.0)
        )
        cache.append(
            BarEvent("BTC-USDT-SWAP", "bar_5s", 2000, 2001, 1.5, 2.5, 1.0, 2.0, 11.0)
        )
        cache.append(
            BarEvent("BTC-USDT-SWAP", "bar_5s", 3000, 3001, 2.0, 3.0, 1.5, 2.5, 12.0)
        )

        snapshot = cache.snapshot()
        self.assertEqual(snapshot["BTC-USDT-SWAP"].shape, (2, 6))
        self.assertEqual(snapshot["BTC-USDT-SWAP"][0][0], 2000.0)

        snapshot["BTC-USDT-SWAP"][0][0] = -1.0
        latest = cache.latest("BTC-USDT-SWAP")
        self.assertEqual(latest[0], 3000.0)

    def test_trade_cache_returns_deep_copies(self):
        cache = TradeCache(window_length=2)
        first = TradeEvent(
            symbol="BTC-USDT-SWAP",
            channel="trades-all",
            ts_event=1000,
            ts_recv=1001,
            trade_id="t1",
            px=100.0,
            sz=1.0,
            side="buy",
        )
        second = TradeEvent(
            symbol="BTC-USDT-SWAP",
            channel="trades-all",
            ts_event=2000,
            ts_recv=2001,
            trade_id="t2",
            px=101.0,
            sz=2.0,
            side="sell",
        )
        third = TradeEvent(
            symbol="BTC-USDT-SWAP",
            channel="trades-all",
            ts_event=3000,
            ts_recv=3001,
            trade_id="t3",
            px=102.0,
            sz=3.0,
            side="buy",
        )
        cache.append(first)
        cache.append(second)
        cache.append(third)

        rows = cache.get_window("BTC-USDT-SWAP")
        self.assertEqual([row.trade_id for row in rows], ["t2", "t3"])
        rows[0].trade_id = "mutated"
        latest = cache.latest("BTC-USDT-SWAP")
        self.assertEqual(latest.trade_id, "t3")

    def test_book_cache_tracks_latest_and_history(self):
        cache = BookCache(history_length=2)
        b1 = BookEvent(
            symbol="BTC-USDT-SWAP",
            channel="books5",
            ts_event=1000,
            ts_recv=1001,
            best_bid_px=100.0,
            best_bid_sz=1.0,
            best_ask_px=100.1,
            best_ask_sz=2.0,
            bids=[BookLevel(100.0, 1.0)],
            asks=[BookLevel(100.1, 2.0)],
        )
        b2 = BookEvent(
            symbol="BTC-USDT-SWAP",
            channel="books5",
            ts_event=2000,
            ts_recv=2001,
            best_bid_px=101.0,
            best_bid_sz=3.0,
            best_ask_px=101.1,
            best_ask_sz=4.0,
            bids=[BookLevel(101.0, 3.0)],
            asks=[BookLevel(101.1, 4.0)],
        )
        b3 = BookEvent(
            symbol="BTC-USDT-SWAP",
            channel="books5",
            ts_event=3000,
            ts_recv=3001,
            best_bid_px=102.0,
            best_bid_sz=5.0,
            best_ask_px=102.1,
            best_ask_sz=6.0,
            bids=[BookLevel(102.0, 5.0)],
            asks=[BookLevel(102.1, 6.0)],
        )
        cache.update(b1)
        cache.update(b2)
        cache.update(b3)

        latest = cache.latest("BTC-USDT-SWAP")
        self.assertEqual(latest.best_bid_px, 102.0)

        window = cache.get_window("BTC-USDT-SWAP")
        self.assertEqual([row.ts_event for row in window], [2000, 3000])

        snapshot = cache.latest_snapshot()
        snapshot["BTC-USDT-SWAP"].best_bid_px = -1.0
        latest_again = cache.latest("BTC-USDT-SWAP")
        self.assertEqual(latest_again.best_bid_px, 102.0)


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
