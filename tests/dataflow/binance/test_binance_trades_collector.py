"""Tests for BinanceTradesCollector — aggTrade message parsing.

Tests focus on the _dispatch() path (pure-Python, no WS needed).
"""

import json

import pytest

from dataflow.binance.trades.collector import BinanceTradesCollector


class TestDispatch:
    """Test BinanceTradesCollector._dispatch() with synthetic aggTrade JSON."""

    def _make_collector(self, symbols=None):
        symbols = symbols or ["BTC-USDT", "ETH-USDT"]
        trades = []
        collector = BinanceTradesCollector(
            symbols=symbols,
            on_trade=lambda sym, px, sz, side, ts: trades.append(
                (sym, px, sz, side, ts)
            ),
        )
        return collector, trades

    def _aggtrade_msg(
        self,
        symbol: str = "BTCUSDT",
        price: str = "97500.50",
        qty: str = "0.123",
        is_buyer_maker: bool = False,
        trade_time: int = 1714838400000,
    ) -> str:
        """Build a combined-stream aggTrade JSON string."""
        return json.dumps({
            "stream": f"{symbol.lower()}@aggTrade",
            "data": {
                "e": "aggTrade",
                "E": trade_time,
                "s": symbol,
                "a": 123456,
                "p": price,
                "q": qty,
                "f": 100,
                "l": 100,
                "T": trade_time,
                "m": is_buyer_maker,
            },
        })

    def test_basic_buy(self):
        collector, trades = self._make_collector()
        msg = self._aggtrade_msg(
            symbol="BTCUSDT", price="97500.50", qty="0.123",
            is_buyer_maker=False, trade_time=1714838400000,
        )
        collector._dispatch(msg)

        assert len(trades) == 1
        sym, px, sz, side, ts = trades[0]
        assert sym == "BTC-USDT"
        assert px == pytest.approx(97500.50)
        assert sz == pytest.approx(0.123)
        assert side == "buy"
        assert ts == 1714838400000
        assert collector.trade_count == 1
        assert collector.msg_count == 1

    def test_basic_sell(self):
        """m=true → buyer is maker → taker is seller → side='sell'."""
        collector, trades = self._make_collector()
        msg = self._aggtrade_msg(is_buyer_maker=True)
        collector._dispatch(msg)

        assert trades[0][3] == "sell"

    def test_eth_symbol_mapping(self):
        collector, trades = self._make_collector()
        msg = self._aggtrade_msg(symbol="ETHUSDT", price="3800.25", qty="1.5")
        collector._dispatch(msg)

        assert trades[0][0] == "ETH-USDT"
        assert trades[0][1] == pytest.approx(3800.25)

    def test_unknown_event_ignored(self):
        collector, trades = self._make_collector()
        msg = json.dumps({"stream": "btcusdt@kline_1m", "data": {"e": "kline"}})
        collector._dispatch(msg)
        assert len(trades) == 0
        assert collector.msg_count == 0

    def test_malformed_json_ignored(self):
        collector, trades = self._make_collector()
        collector._dispatch("not json at all")
        assert len(trades) == 0

    def test_missing_fields_ignored(self):
        collector, trades = self._make_collector()
        msg = json.dumps({
            "data": {"e": "aggTrade", "s": "BTCUSDT"},
        })
        collector._dispatch(msg)
        assert len(trades) == 0

    def test_multiple_trades_accumulate(self):
        collector, trades = self._make_collector()
        for i in range(5):
            msg = self._aggtrade_msg(
                price=str(97500 + i), qty="0.1", trade_time=1714838400000 + i,
            )
            collector._dispatch(msg)

        assert len(trades) == 5
        assert collector.trade_count == 5
        assert collector.msg_count == 5

    def test_symbol_not_in_subscription(self):
        """aggTrade for unknown symbol still dispatches (with from_binance_symbol fallback)."""
        collector, trades = self._make_collector(symbols=["BTC-USDT"])
        msg = self._aggtrade_msg(symbol="SOLUSDT", price="170.0", qty="10")
        collector._dispatch(msg)

        assert len(trades) == 1
        assert trades[0][0] == "SOL-USDT"
