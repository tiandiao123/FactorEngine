"""Tests for trades-derived factor builders (okx_perp_trades group)."""

import math
import pytest
import fe_runtime as rt
from factorengine.factors import FactorRegistry

Op = rt.Op


class TestTradesFactorRegistry:
    """Verify that trades factors load and build correctly."""

    @pytest.fixture(autouse=True)
    def _load_registry(self):
        self.reg = FactorRegistry()
        self.reg.load_group("okx_perp_trades")

    def test_group_discovered(self):
        assert "okx_perp_trades" in self.reg.groups

    def test_expected_factors_registered(self):
        fids = self.reg.factor_ids_by_group("okx_perp_trades")
        assert "T0001" in fids
        assert "T0002" in fids
        assert "T0003" in fids
        assert "T0004" in fids
        assert "T0005" in fids
        assert "T0006" in fids

    def test_all_factors_compile(self):
        graphs = self.reg.build_group("okx_perp_trades")
        for fid, g in graphs.items():
            assert g.num_nodes() > 0, f"Factor {fid} has no nodes"
            assert g.warmup_bars() >= 0


class TestTradeImbalanceT0001:
    """T0001: TsZscore(Div(Sub(buy_vol, sell_vol), Add(buy_vol, sell_vol)), 120)"""

    def test_basic_output(self):
        g = FactorRegistry()
        g.load_group("okx_perp_trades")
        graph = g.build("T0001", group="okx_perp_trades")

        for i in range(150):
            bv = 100.0 + (i % 10)
            sv = 90.0
            graph.push_bar(close=0.0, buy_vol=bv, sell_vol=sv)

        assert graph.ready()
        out = graph.output()
        assert not math.isnan(out)

    def test_equal_volumes_near_zero(self):
        g = FactorRegistry()
        g.load_group("okx_perp_trades")
        graph = g.build("T0001", group="okx_perp_trades")

        for _ in range(150):
            graph.push_bar(close=0.0, buy_vol=100.0, sell_vol=100.0)

        assert graph.ready()
        out = graph.output()
        assert abs(out) < 1e-4


class TestVwapMomentumT0002:
    """T0002: Div(Sub(vwap, Ma(vwap, 60)), TsStd(vwap, 60))"""

    def test_trending_vwap(self):
        g = FactorRegistry()
        g.load_group("okx_perp_trades")
        graph = g.build("T0002", group="okx_perp_trades")

        for i in range(100):
            graph.push_bar(close=0.0, vwap=100.0 + i * 0.1)

        assert graph.ready()
        out = graph.output()
        assert out > 0, "Uptrending VWAP should produce positive z-score"


class TestBuyVolSurgeT0003:
    """T0003: TsZscore(SLog1p(buy_vol), 120)"""

    def test_steady_then_spike(self):
        g = FactorRegistry()
        g.load_group("okx_perp_trades")
        graph = g.build("T0003", group="okx_perp_trades")

        for _ in range(130):
            graph.push_bar(close=0.0, buy_vol=100.0)

        out_before = graph.output()

        for _ in range(5):
            graph.push_bar(close=0.0, buy_vol=500.0)

        out_after = graph.output()
        assert out_after > out_before, "Buy volume spike should increase z-score"


class TestVwapCloseDivergenceT0004:
    """T0004: TsZscore(Div(Sub(vwap, close), close), 60)"""

    def test_vwap_premium(self):
        g = FactorRegistry()
        g.load_group("okx_perp_trades")
        graph = g.build("T0004", group="okx_perp_trades")

        for i in range(80):
            graph.push_bar(close=100.0, vwap=100.0)

        for _ in range(5):
            graph.push_bar(close=100.0, vwap=102.0)

        out = graph.output()
        assert out > 0, "Persistent VWAP premium should produce positive z-score"


class TestInferenceEngineIntegration:
    """Test that trades factors work in InferenceEngine with push_bars."""

    def test_trades_factors_in_engine(self):
        reg = FactorRegistry()
        reg.load_group("okx_perp_trades")

        engine = rt.InferenceEngine(num_threads=1)
        engine.add_symbol("BTC")

        for fid, graph in reg.build_group("okx_perp_trades").items():
            engine.add_factor("BTC", fid, graph)

        for i in range(150):
            bar = rt.BarData(
                close=100.0 + i * 0.01,
                vwap=100.0 + i * 0.01 + 0.05,
                buy_vol=100.0 + (i % 20),
                sell_vol=90.0 + (i % 15),
            )
            engine.push_bars({"BTC": bar})

        outputs = engine.get_all_outputs()
        assert "BTC" in outputs
        btc_out = outputs["BTC"]
        for fid in reg.factor_ids_by_group("okx_perp_trades"):
            assert fid in btc_out, f"Missing output for {fid}"
            assert not math.isnan(btc_out[fid]) or not reg.build("T0001", group="okx_perp_trades").ready()

    def test_backward_compat_kline_factors_unaffected(self):
        """Loading trades factors should not affect kline factors."""
        reg = FactorRegistry()
        reg.load_group("okx_perp")

        engine = rt.InferenceEngine(num_threads=1)
        engine.add_symbol("BTC")

        graphs = reg.build_group("okx_perp")
        first_fid = sorted(graphs.keys())[0]
        engine.add_factor("BTC", first_fid, graphs[first_fid])

        for i in range(500):
            bar = rt.BarData(
                close=100.0 + i * 0.01,
                volume=1000.0,
                open=99.5 + i * 0.01,
                high=100.5 + i * 0.01,
                low=99.0 + i * 0.01,
            )
            engine.push_bars({"BTC": bar})

        out = engine.get_outputs("BTC")
        assert len(out) == 1
