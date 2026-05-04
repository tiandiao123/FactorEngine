"""
OKX Perpetual trades-derived factor builders.

Factors built from trades-aggregated inputs: vwap, buy_vol, sell_vol.
These require trades data (data_source="trades" or "kline_trades").

Add new factors via @register_factor("okx_perp_trades", "TXXXX").
"""

from factorengine.factors.registry import register_factor
import fe_runtime as rt

Op = rt.Op


# ═══════════════════════════════════════════════════════════════
#  T0001: Trade Imbalance Z-Score
#    (buy_vol - sell_vol) / (buy_vol + sell_vol), then z-scored
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0001")
def build_trade_imbalance() -> rt.FactorGraph:
    """TsZscore(Div(Sub(buy_vol, sell_vol), Add(buy_vol, sell_vol)), 120)"""
    g = rt.FactorGraph()
    bv = g.add_input("buy_vol")
    sv = g.add_input("sell_vol")
    diff = g.add_binary(Op.SUB, bv, sv)
    total = g.add_binary(Op.ADD, bv, sv)
    imbalance = g.add_binary(Op.DIV, diff, total)
    g.add_rolling(Op.TS_ZSCORE, imbalance, 120)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0002: VWAP Momentum — deviation of VWAP from its MA
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0002")
def build_vwap_momentum() -> rt.FactorGraph:
    """Div(Sub(vwap, Ma(vwap, 60)), TsStd(vwap, 60))"""
    g = rt.FactorGraph()
    v = g.add_input("vwap")
    ma = g.add_rolling(Op.MA, v, 60)
    dev = g.add_binary(Op.SUB, v, ma)
    vol = g.add_rolling(Op.TS_STD, v, 60)
    g.add_binary(Op.DIV, dev, vol)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0003: Buy Volume Surge — buy volume relative to its history
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0003")
def build_buy_vol_surge() -> rt.FactorGraph:
    """TsZscore(SLog1p(buy_vol), 120)"""
    g = rt.FactorGraph()
    bv = g.add_input("buy_vol")
    sbv = g.add_unary(Op.SLOG1P, bv)
    g.add_rolling(Op.TS_ZSCORE, sbv, 120)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0004: VWAP-Close Divergence — premium of VWAP over close
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0004")
def build_vwap_close_divergence() -> rt.FactorGraph:
    """TsZscore(Div(Sub(vwap, close), close), 60)"""
    g = rt.FactorGraph()
    vw = g.add_input("vwap")
    c = g.add_input("close")
    premium = g.add_binary(Op.DIV, g.add_binary(Op.SUB, vw, c), c)
    g.add_rolling(Op.TS_ZSCORE, premium, 60)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0005: Net Volume Trend — smoothed buy-sell imbalance
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0005")
def build_net_volume_trend() -> rt.FactorGraph:
    """TsRank(Ema(Sub(buy_vol, sell_vol), 30), 120)"""
    g = rt.FactorGraph()
    bv = g.add_input("buy_vol")
    sv = g.add_input("sell_vol")
    net = g.add_binary(Op.SUB, bv, sv)
    smooth = g.add_rolling(Op.EMA, net, 30)
    g.add_rolling(Op.TS_RANK, smooth, 120)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0006: VWAP Mean Reversion — EMA crossover on VWAP
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0006")
def build_vwap_mean_reversion() -> rt.FactorGraph:
    """Neg(Div(Sub(Ema(vwap, 10), Ema(vwap, 60)), TsStd(vwap, 60)))"""
    g = rt.FactorGraph()
    v = g.add_input("vwap")
    ema_fast = g.add_rolling(Op.EMA, v, 10)
    ema_slow = g.add_rolling(Op.EMA, v, 60)
    diff = g.add_binary(Op.SUB, ema_fast, ema_slow)
    vol = g.add_rolling(Op.TS_STD, v, 60)
    g.add_unary(Op.NEG, g.add_binary(Op.DIV, diff, vol))
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0007: Imbalance Kurtosis — tail risk in trade imbalance
#    High kurtosis = extreme buy/sell bursts (informed trading)
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0007")
def build_imbalance_kurtosis() -> rt.FactorGraph:
    """TsZscore(TsKurt(Div(Sub(buy_vol, sell_vol), Add(buy_vol, sell_vol)), 60), 120)"""
    g = rt.FactorGraph()
    bv = g.add_input("buy_vol")
    sv = g.add_input("sell_vol")
    diff = g.add_binary(Op.SUB, bv, sv)
    total = g.add_binary(Op.ADD, bv, sv)
    imbalance = g.add_binary(Op.DIV, diff, total)
    kurt = g.add_rolling(Op.TS_KURT, imbalance, 60)
    g.add_rolling(Op.TS_ZSCORE, kurt, 120)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0008: Buy Volume Kurtosis — tail detection in buy pressure
#    Sudden spikes in buy volume create heavy tails
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0008")
def build_buy_vol_kurtosis() -> rt.FactorGraph:
    """Neg(TsRank(TsKurt(SLog1p(buy_vol), 30), 120))"""
    g = rt.FactorGraph()
    bv = g.add_input("buy_vol")
    sbv = g.add_unary(Op.SLOG1P, bv)
    kurt = g.add_rolling(Op.TS_KURT, sbv, 30)
    ranked = g.add_rolling(Op.TS_RANK, kurt, 120)
    g.add_unary(Op.NEG, ranked)
    g.compile()
    return g
