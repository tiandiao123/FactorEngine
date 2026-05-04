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


# ═══════════════════════════════════════════════════════════════
#  T0009: Volume-Price Beta — sensitivity of returns to volume
#    High beta = price driven by volume surges (liquidity events)
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0009")
def build_volume_price_beta() -> rt.FactorGraph:
    """TsZscore(TsBeta(SLog1p(volume), ret, 60), 120)"""
    g = rt.FactorGraph()
    vol = g.add_input("volume")
    ret = g.add_input("ret")
    svol = g.add_unary(Op.SLOG1P, vol)
    beta = g.add_bivariate(Op.TS_BETA, svol, ret, 60)
    g.add_rolling(Op.TS_ZSCORE, beta, 120)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0010: Buy-Sell Imbalance Beta on Close — how close reacts
#    to trade imbalance shifts (informed flow → price impact)
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0010")
def build_imbalance_close_beta() -> rt.FactorGraph:
    """Neg(TsRank(TsBeta(Div(Sub(buy_vol,sell_vol),Add(buy_vol,sell_vol)), TsPct(close,1), 30), 120))"""
    g = rt.FactorGraph()
    bv = g.add_input("buy_vol")
    sv = g.add_input("sell_vol")
    close = g.add_input("close")
    diff = g.add_binary(Op.SUB, bv, sv)
    total = g.add_binary(Op.ADD, bv, sv)
    imbalance = g.add_binary(Op.DIV, diff, total)
    pct = g.add_rolling(Op.TS_PCT, close, 1)
    beta = g.add_bivariate(Op.TS_BETA, imbalance, pct, 30)
    ranked = g.add_rolling(Op.TS_RANK, beta, 120)
    g.add_unary(Op.NEG, ranked)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0011: Volume-Return Alpha — excess return unexplained by volume
#    Intercept of ret regressed on SLog1p(vol): positive = hidden alpha
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0011")
def build_volume_return_alpha() -> rt.FactorGraph:
    """TsZscore(TsIntercept(SLog1p(volume), ret, 60), 120)"""
    g = rt.FactorGraph()
    vol = g.add_input("volume")
    ret = g.add_input("ret")
    svol = g.add_unary(Op.SLOG1P, vol)
    alpha = g.add_bivariate(Op.TS_INTERCEPT, svol, ret, 60)
    g.add_rolling(Op.TS_ZSCORE, alpha, 120)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0012: Imbalance-Return Residual — return not explained by imbalance
#    Baseline shift in price movement independent of trade flow
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0012")
def build_imbalance_return_residual() -> rt.FactorGraph:
    """Neg(TsRank(TsIntercept(Div(Sub(buy_vol,sell_vol),Add(buy_vol,sell_vol)), TsPct(close,1), 30), 120))"""
    g = rt.FactorGraph()
    bv = g.add_input("buy_vol")
    sv = g.add_input("sell_vol")
    close = g.add_input("close")
    diff = g.add_binary(Op.SUB, bv, sv)
    total = g.add_binary(Op.ADD, bv, sv)
    imbalance = g.add_binary(Op.DIV, diff, total)
    pct = g.add_rolling(Op.TS_PCT, close, 1)
    alpha = g.add_bivariate(Op.TS_INTERCEPT, imbalance, pct, 30)
    ranked = g.add_rolling(Op.TS_RANK, alpha, 120)
    g.add_unary(Op.NEG, ranked)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0013: Volume CV — stability of trading volume over time
#    High CV = erratic volume (potential manipulation or events)
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0013")
def build_volume_cv() -> rt.FactorGraph:
    """Neg(TsRank(TsCv(SLog1p(volume), 60), 120))"""
    g = rt.FactorGraph()
    vol = g.add_input("volume")
    svol = g.add_unary(Op.SLOG1P, vol)
    cv = g.add_rolling(Op.TS_CV, svol, 60)
    ranked = g.add_rolling(Op.TS_RANK, cv, 120)
    g.add_unary(Op.NEG, ranked)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0014: Imbalance CV — variability of buy-sell imbalance
#    Stable imbalance = persistent pressure; variable = noise
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0014")
def build_imbalance_cv() -> rt.FactorGraph:
    """TsZscore(TsCv(Div(Sub(buy_vol, sell_vol), Add(buy_vol, sell_vol)), 30), 120)"""
    g = rt.FactorGraph()
    bv = g.add_input("buy_vol")
    sv = g.add_input("sell_vol")
    diff = g.add_binary(Op.SUB, bv, sv)
    total = g.add_binary(Op.ADD, bv, sv)
    imbalance = g.add_binary(Op.DIV, diff, total)
    cv = g.add_rolling(Op.TS_CV, imbalance, 30)
    g.add_rolling(Op.TS_ZSCORE, cv, 120)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0015: Volume-Return Cosine — directional alignment between
#    volume and returns (without mean-centering)
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0015")
def build_vol_return_cos() -> rt.FactorGraph:
    """TsZscore(TsCos(SLog1p(volume), ret, 60), 120)"""
    g = rt.FactorGraph()
    vol = g.add_input("volume")
    ret = g.add_input("ret")
    svol = g.add_unary(Op.SLOG1P, vol)
    cos = g.add_bivariate(Op.TS_COS, svol, ret, 60)
    g.add_rolling(Op.TS_ZSCORE, cos, 120)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0016: Imbalance-Return Cosine — raw directional coherence
#    between trade flow imbalance and price returns
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0016")
def build_imbalance_return_cos() -> rt.FactorGraph:
    """Neg(TsRank(TsCos(Div(Sub(buy_vol,sell_vol),Add(buy_vol,sell_vol)), TsPct(close,1), 30), 120))"""
    g = rt.FactorGraph()
    bv = g.add_input("buy_vol")
    sv = g.add_input("sell_vol")
    close = g.add_input("close")
    diff = g.add_binary(Op.SUB, bv, sv)
    total = g.add_binary(Op.ADD, bv, sv)
    imbalance = g.add_binary(Op.DIV, diff, total)
    pct = g.add_rolling(Op.TS_PCT, close, 1)
    cos = g.add_bivariate(Op.TS_COS, imbalance, pct, 30)
    ranked = g.add_rolling(Op.TS_RANK, cos, 120)
    g.add_unary(Op.NEG, ranked)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0017: Normalized Close Position — where close sits in recent
#    range [0=at min, 1=at max], captures relative price level
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0017")
def build_close_norm_position() -> rt.FactorGraph:
    """TsZscore(TsNorm(close, 60), 120)"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    norm = g.add_rolling(Op.TS_NORM, c, 60)
    g.add_rolling(Op.TS_ZSCORE, norm, 120)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0018: Normalized Volume — volume position in recent range
#    Low norm = quiet; high norm = active, potential reversal signal
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0018")
def build_volume_norm() -> rt.FactorGraph:
    """Neg(TsRank(TsNorm(SLog1p(volume), 30), 120))"""
    g = rt.FactorGraph()
    vol = g.add_input("volume")
    svol = g.add_unary(Op.SLOG1P, vol)
    norm = g.add_rolling(Op.TS_NORM, svol, 30)
    ranked = g.add_rolling(Op.TS_RANK, norm, 120)
    g.add_unary(Op.NEG, ranked)
    g.compile()
    return g
