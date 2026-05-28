"""Tests for Engine with exchange='binance'.

Covers:
  - exchange parameter validation
  - Binance kline below 1m raises ValueError
  - Binance trades mode wires up BinanceTradesWorker
  - End-to-end factor output via queue injection
"""

import math
import threading
import time

import numpy as np
import pytest

from factorengine.engine import Engine


class TestEngineExchangeParam:
    """Test exchange parameter validation and defaults."""

    def test_default_exchange_is_okx(self):
        engine = Engine(
            symbols=["BTC-USDT-SWAP"],
            mode="simulation",
            sim_seed=42,
        )
        assert engine.exchange == "okx"
        engine.stop()

    def test_valid_exchanges(self):
        for ex in ("okx", "binance"):
            engine = Engine(
                symbols=["BTC-USDT"],
                mode="simulation",
                sim_seed=42,
                exchange=ex,
            )
            assert engine.exchange == ex
            engine.stop()

    def test_invalid_exchange_raises(self):
        with pytest.raises(ValueError, match="Invalid exchange"):
            Engine(
                symbols=["BTC-USDT"],
                mode="simulation",
                sim_seed=42,
                exchange="bybit",
            )


class TestBinanceKlineValidation:
    """Binance Futures does not support kline below 1m."""

    def test_binance_kline_1s_raises(self):
        with pytest.raises(ValueError, match="does not support kline below 1m"):
            Engine(
                symbols=["BTC-USDT"],
                mode="live",
                exchange="binance",
                data_source="kline",
                data_freq="1s",
            )

    def test_binance_kline_trades_5s_raises(self):
        with pytest.raises(ValueError, match="does not support kline below 1m"):
            Engine(
                symbols=["BTC-USDT"],
                mode="live",
                exchange="binance",
                data_source="kline_trades",
                data_freq="5s",
            )

    def test_binance_trades_1s_ok(self):
        """trades-only mode should work at 1s for Binance."""
        engine = Engine(
            symbols=["BTC-USDT"],
            mode="live",
            exchange="binance",
            data_source="trades",
            data_freq="1s",
            factor_group="okx_perp_trades",
            num_threads=1,
        )
        assert engine.exchange == "binance"
        assert engine.data_source == "trades"
        engine.stop()

    def test_binance_trades_exchange_time_wires_worker_config(self):
        engine = Engine(
            symbols=["BTC-USDT"],
            mode="live",
            exchange="binance",
            data_source="trades",
            data_freq="1s",
            factor_group="okx_perp_trades",
            num_threads=1,
            trade_time_mode="exchange_time",
            allowed_lateness_ms=750,
        )

        try:
            assert engine.exchange == "binance"
            assert engine.data_source == "trades"
            assert engine._trades_worker._trade_time_mode == "exchange_time"
            assert engine._trades_worker._allowed_lateness_ms == 750
            assert engine._trades_worker._freq_ms == pytest.approx(1000.0)
        finally:
            engine.stop()

    def test_binance_kline_1m_ok(self):
        """kline mode at 1m should work for Binance."""
        engine = Engine(
            symbols=["BTC-USDT"],
            mode="live",
            exchange="binance",
            data_source="kline",
            data_freq="1min",
            factor_group="okx_perp",
            num_threads=1,
        )
        assert engine.exchange == "binance"
        assert engine.data_source == "kline"
        engine.stop()

    def test_binance_kline_trades_1m_ok(self):
        """kline_trades mode at 1m should work for Binance."""
        engine = Engine(
            symbols=["BTC-USDT"],
            mode="live",
            exchange="binance",
            data_source="kline_trades",
            data_freq="1min",
            factor_group=["okx_perp", "okx_perp_trades"],
            num_threads=1,
        )
        assert engine.exchange == "binance"
        assert engine.data_source == "kline_trades"
        engine.stop()


class TestBinanceTradesRuntimeLoop:
    """Test Engine._runtime_loop with Binance-style (round_bars, round_trades) payloads."""

    def test_binance_trades_factor_output(self):
        """Simulate Binance trades-only: inject payloads via queue, verify factors."""
        engine = Engine(
            symbols=["BTC-USDT"],
            mode="live",
            exchange="binance",
            data_source="trades",
            data_freq="1s",
            factor_group="okx_perp_trades",
            num_threads=1,
        )
        sym = "BTC-USDT"

        engine._stop_event.clear()
        t = threading.Thread(target=engine._runtime_loop, daemon=True)
        t.start()

        try:
            for i in range(120):
                bar = np.array([
                    float(i * 1000),
                    97500.0 + i * 0.5,
                    97510.0 + i * 0.5,
                    97490.0 + i * 0.5,
                    97505.0 + i * 0.5,
                    0.5 + i * 0.01,
                    float("nan"),
                    float("nan"),
                ])
                trades = {
                    "vwap": 97502.0 + i * 0.5,
                    "buy_vol": 0.3 + (i % 10) * 0.01,
                    "sell_vol": 0.2 + (i % 8) * 0.01,
                    "trade_count": 15 + i,
                }
                engine._bar_queue.put(({sym: bar}, {sym: trades}))
                time.sleep(0.003)

            time.sleep(0.5)
        finally:
            engine._stop_event.set()
            t.join(timeout=3)

        assert engine.bars_pushed == 120
        factors = engine.get_factor_outputs()
        assert sym in factors
        fvals = factors[sym]
        assert len(fvals) > 0

        non_nan_count = sum(1 for v in fvals.values() if not math.isnan(v))
        assert non_nan_count > 0

    def test_binance_multi_symbol(self):
        """Multiple Binance symbols through the runtime loop."""
        symbols = ["BTC-USDT", "ETH-USDT", "SOL-USDT"]
        engine = Engine(
            symbols=symbols,
            mode="live",
            exchange="binance",
            data_source="trades",
            data_freq="1s",
            factor_group="okx_perp_trades",
            num_threads=1,
        )

        engine._stop_event.clear()
        t = threading.Thread(target=engine._runtime_loop, daemon=True)
        t.start()

        try:
            for i in range(60):
                round_bars = {}
                round_trades = {}
                for sym in symbols:
                    base_px = {"BTC-USDT": 97500, "ETH-USDT": 3800, "SOL-USDT": 170}[sym]
                    bar = np.array([
                        float(i * 1000),
                        base_px + i * 0.1,
                        base_px + i * 0.1 + 5,
                        base_px + i * 0.1 - 5,
                        base_px + i * 0.1 + 2,
                        1.0 + i * 0.01,
                        float("nan"),
                        float("nan"),
                    ])
                    trades = {
                        "vwap": base_px + i * 0.1 + 1,
                        "buy_vol": 0.5,
                        "sell_vol": 0.3,
                        "trade_count": 10,
                    }
                    round_bars[sym] = bar
                    round_trades[sym] = trades
                engine._bar_queue.put((round_bars, round_trades))
                time.sleep(0.005)

            time.sleep(0.5)
        finally:
            engine._stop_event.set()
            t.join(timeout=3)

        factors = engine.get_factor_outputs()
        for sym in symbols:
            assert sym in factors
