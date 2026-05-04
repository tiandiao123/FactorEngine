"""Tests for BinanceKlineCollector — kline message parsing.

Tests focus on the _dispatch() path (pure-Python, no WS needed).
"""

import json

import pytest

from dataflow.binance.kline.collector import (
    BinanceKlineCollector,
    resolve_binance_interval,
)


class TestDispatch:
    """Test BinanceKlineCollector._dispatch() with synthetic kline JSON."""

    def _make_collector(self, symbols=None):
        symbols = symbols or ["BTC-USDT", "ETH-USDT"]
        candles = []
        collector = BinanceKlineCollector(
            symbols=symbols,
            on_candle=lambda sym, k: candles.append((sym, k)),
            interval="1m",
        )
        return collector, candles

    def _kline_msg(
        self,
        symbol: str = "BTCUSDT",
        open_: str = "97500.00",
        high: str = "97600.00",
        low: str = "97400.00",
        close: str = "97550.00",
        volume: str = "12.345",
        quote_vol: str = "1203456.78",
        is_closed: bool = True,
        start_time: int = 1714838400000,
    ) -> str:
        return json.dumps({
            "stream": f"{symbol.lower()}@kline_1m",
            "data": {
                "e": "kline",
                "E": start_time + 59999,
                "s": symbol,
                "k": {
                    "t": start_time,
                    "T": start_time + 59999,
                    "s": symbol,
                    "i": "1m",
                    "f": 100,
                    "L": 200,
                    "o": open_,
                    "c": close,
                    "h": high,
                    "l": low,
                    "v": volume,
                    "n": 42,
                    "x": is_closed,
                    "q": quote_vol,
                    "V": "6.789",
                    "Q": "662345.12",
                    "B": "0",
                },
            },
        })

    def test_confirmed_kline(self):
        collector, candles = self._make_collector()
        msg = self._kline_msg(is_closed=True)
        collector._dispatch(msg)

        assert len(candles) == 1
        sym, k = candles[0]
        assert sym == "BTC-USDT"
        assert k["o"] == "97500.00"
        assert k["c"] == "97550.00"
        assert k["x"] is True
        assert collector.msg_count == 1

    def test_unconfirmed_kline_also_dispatched(self):
        """Collector dispatches ALL kline events; adapter filters confirmed."""
        collector, candles = self._make_collector()
        msg = self._kline_msg(is_closed=False)
        collector._dispatch(msg)

        assert len(candles) == 1
        assert candles[0][1]["x"] is False

    def test_eth_symbol_mapping(self):
        collector, candles = self._make_collector()
        msg = self._kline_msg(symbol="ETHUSDT", close="3800.50")
        collector._dispatch(msg)

        assert candles[0][0] == "ETH-USDT"

    def test_non_kline_event_ignored(self):
        collector, candles = self._make_collector()
        msg = json.dumps({"data": {"e": "aggTrade", "s": "BTCUSDT"}})
        collector._dispatch(msg)
        assert len(candles) == 0

    def test_subscription_response_ignored(self):
        collector, candles = self._make_collector()
        msg = json.dumps({"result": None, "id": 1})
        collector._dispatch(msg)
        assert len(candles) == 0

    def test_malformed_json_ignored(self):
        collector, candles = self._make_collector()
        collector._dispatch("not json")
        assert len(candles) == 0


class TestResolveBinanceInterval:

    @pytest.mark.parametrize(
        "secs, expected",
        [(60, "1m"), (180, "3m"), (300, "5m"), (900, "15m"),
         (1800, "30m"), (3600, "1h"), (7200, "2h"), (14400, "4h")],
    )
    def test_valid_intervals(self, secs: int, expected: str):
        assert resolve_binance_interval(secs) == expected

    def test_invalid_interval_raises(self):
        with pytest.raises(ValueError, match="No Binance kline interval"):
            resolve_binance_interval(45)

    def test_1s_raises(self):
        with pytest.raises(ValueError):
            resolve_binance_interval(1)
