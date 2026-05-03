"""Integration test for OXKTradesCollector — connects to live OKX WS.

This test requires network access to OKX.  It subscribes to BTC-USDT-SWAP
trades for a few seconds and verifies that trades are received and parsed.

Run with:
    python -m pytest tests/trades/test_collector.py -v -s
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from dataflow.okx.trades.collector import OKXTradesCollector


@pytest.fixture()
def collected_trades():
    """Run collector for a short window, return list of received trades."""
    trades: list[tuple] = []

    def on_trade(symbol: str, px: float, sz: float, side: str, ts_ms: int):
        trades.append((symbol, px, sz, side, ts_ms))

    collector = OKXTradesCollector(
        symbols=["BTC-USDT-SWAP"],
        on_trade=on_trade,
    )

    async def _run_for(seconds: float):
        task = asyncio.create_task(collector.run())
        await asyncio.sleep(seconds)
        collector.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run_for(8))
    return trades, collector


@pytest.mark.network
class TestOKXTradesCollector:
    def test_receives_trades(self, collected_trades):
        trades, collector = collected_trades
        assert len(trades) > 0, "No trades received — check network / OKX availability"
        assert collector.trade_count == len(trades)

    def test_trade_fields(self, collected_trades):
        trades, _ = collected_trades
        assert len(trades) > 0
        symbol, px, sz, side, ts_ms = trades[0]
        assert symbol == "BTC-USDT-SWAP"
        assert isinstance(px, float) and px > 0
        assert isinstance(sz, float) and sz > 0
        assert side in ("buy", "sell")
        assert isinstance(ts_ms, int) and ts_ms > 1_000_000_000_000

    def test_msg_count(self, collected_trades):
        _, collector = collected_trades
        assert collector.msg_count > 0


class TestCollectorUnit:
    """Unit tests that don't require network."""

    def test_dispatch_valid(self):
        trades = []
        collector = OKXTradesCollector(
            symbols=["BTC-USDT-SWAP"],
            on_trade=lambda sym, px, sz, side, ts: trades.append(
                (sym, px, sz, side, ts)
            ),
        )
        import json

        payload = json.dumps({
            "arg": {"channel": "trades", "instId": "BTC-USDT-SWAP"},
            "data": [
                {"px": "67000.5", "sz": "0.1", "side": "buy", "ts": "1714704000123"},
                {"px": "67001.0", "sz": "0.2", "side": "sell", "ts": "1714704000456"},
            ],
        })
        collector._dispatch(payload)
        assert len(trades) == 2
        assert trades[0] == ("BTC-USDT-SWAP", 67000.5, 0.1, "buy", 1714704000123)
        assert trades[1] == ("BTC-USDT-SWAP", 67001.0, 0.2, "sell", 1714704000456)
        assert collector.trade_count == 2
        assert collector.msg_count == 1

    def test_dispatch_event_ignored(self):
        trades = []
        collector = OKXTradesCollector(
            symbols=["BTC-USDT-SWAP"],
            on_trade=lambda *a: trades.append(a),
        )
        import json

        collector._dispatch(json.dumps({"event": "subscribe", "arg": {}}))
        assert len(trades) == 0
        assert collector.msg_count == 0

    def test_dispatch_malformed_record(self):
        trades = []
        collector = OKXTradesCollector(
            symbols=["BTC-USDT-SWAP"],
            on_trade=lambda *a: trades.append(a),
        )
        import json

        payload = json.dumps({
            "arg": {"channel": "trades", "instId": "BTC-USDT-SWAP"},
            "data": [
                {"px": "bad", "sz": "0.1", "side": "buy", "ts": "123"},
                {"px": "67000.0", "sz": "0.1", "side": "buy", "ts": "123"},
            ],
        })
        collector._dispatch(payload)
        # "bad" can be parsed as float? No it can't. Wait...
        # Actually float("bad") raises ValueError, so first record skipped
        # Actually let me check: float("bad") → ValueError. Good.
        assert len(trades) == 1
        assert collector.trade_count == 1

    def test_dispatch_missing_instId(self):
        trades = []
        collector = OKXTradesCollector(
            symbols=["BTC-USDT-SWAP"],
            on_trade=lambda *a: trades.append(a),
        )
        import json

        payload = json.dumps({
            "arg": {"channel": "trades"},
            "data": [{"px": "100", "sz": "1", "side": "buy", "ts": "123"}],
        })
        collector._dispatch(payload)
        assert len(trades) == 0

    def test_dispatch_invalid_json(self):
        collector = OKXTradesCollector(
            symbols=["BTC-USDT-SWAP"],
            on_trade=lambda *a: None,
        )
        collector._dispatch("not json at all {{{")
        assert collector.msg_count == 0

    def test_stop_flag(self):
        collector = OKXTradesCollector(
            symbols=["BTC-USDT-SWAP"],
            on_trade=lambda *a: None,
        )
        assert collector._running is True
        collector.stop()
        assert collector._running is False
