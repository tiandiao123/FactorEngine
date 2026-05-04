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


# ═══════════════════════════════════════════════════════════════
#  T0019: Close RSI — classic momentum oscillator on trades-close
#    RSI near 70+ = overbought, 30- = oversold
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0019")
def build_close_rsi() -> rt.FactorGraph:
    """TsZscore(TsRsi(close, 14), 120)"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    rsi = g.add_rolling(Op.TS_RSI, c, 14)
    g.add_rolling(Op.TS_ZSCORE, rsi, 120)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0020: VWAP RSI — RSI on volume-weighted average price
#    Captures mean-reversion signal in execution price
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0020")
def build_vwap_rsi() -> rt.FactorGraph:
    """Neg(TsRank(TsRsi(vwap, 14), 120))"""
    g = rt.FactorGraph()
    v = g.add_input("vwap")
    rsi = g.add_rolling(Op.TS_RSI, v, 14)
    ranked = g.add_rolling(Op.TS_RANK, rsi, 120)
    g.add_unary(Op.NEG, ranked)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0021: Short-term RSI — fast RSI(5) captures rapid momentum
#    shifts at high frequency
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0021")
def build_short_rsi() -> rt.FactorGraph:
    """TsZscore(TsRsi(close, 5), 60)"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    rsi = g.add_rolling(Op.TS_RSI, c, 5)
    g.add_rolling(Op.TS_ZSCORE, rsi, 60)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0022: Volume Growth Momentum — rolling growth rate of volume
#    Sudden volume growth = event detection
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0022")
def build_volume_growth() -> rt.FactorGraph:
    """TsZscore(TsGrowth(SLog1p(volume), 5), 120)"""
    g = rt.FactorGraph()
    vol = g.add_input("volume")
    svol = g.add_unary(Op.SLOG1P, vol)
    growth = g.add_rolling(Op.TS_GROWTH, svol, 5)
    g.add_rolling(Op.TS_ZSCORE, growth, 120)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0023: VWAP Growth Acceleration — rate of change in VWAP
#    Positive acceleration = strengthening trend
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0023")
def build_vwap_growth() -> rt.FactorGraph:
    """Neg(TsRank(TsGrowth(vwap, 10), 120))"""
    g = rt.FactorGraph()
    v = g.add_input("vwap")
    growth = g.add_rolling(Op.TS_GROWTH, v, 10)
    ranked = g.add_rolling(Op.TS_RANK, growth, 120)
    g.add_unary(Op.NEG, ranked)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0024: Close MACD Histogram — classic MACD(12,26,9) on close
#    DIF = EMA(close,12) - EMA(close,26)
#    DEA = EMA(DIF, 9)
#    Histogram = DIF - DEA
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0024")
def build_close_macd_hist() -> rt.FactorGraph:
    """TsZscore(Sub(Sub(Ema(close,12),Ema(close,26)), Ema(Sub(Ema(close,12),Ema(close,26)),9)), 120)"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    ema_fast = g.add_rolling(Op.EMA, c, 12)
    ema_slow = g.add_rolling(Op.EMA, c, 26)
    dif = g.add_binary(Op.SUB, ema_fast, ema_slow)
    dea = g.add_rolling(Op.EMA, dif, 9)
    hist = g.add_binary(Op.SUB, dif, dea)
    g.add_rolling(Op.TS_ZSCORE, hist, 120)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0025: Close MACD DIF — trend direction signal
#    DIF > 0 = bullish, DIF < 0 = bearish
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0025")
def build_close_macd_dif() -> rt.FactorGraph:
    """TsRank(Sub(Ema(close,12), Ema(close,26)), 120)"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    ema_fast = g.add_rolling(Op.EMA, c, 12)
    ema_slow = g.add_rolling(Op.EMA, c, 26)
    dif = g.add_binary(Op.SUB, ema_fast, ema_slow)
    g.add_rolling(Op.TS_RANK, dif, 120)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0026: Close MACD DEA — smoothed signal line
#    DEA crossing zero = major trend reversal
#    Negated so lower rank = stronger bullish signal
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0026")
def build_close_macd_dea() -> rt.FactorGraph:
    """Neg(TsRank(Ema(Sub(Ema(close,12), Ema(close,26)), 9), 120))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    ema_fast = g.add_rolling(Op.EMA, c, 12)
    ema_slow = g.add_rolling(Op.EMA, c, 26)
    dif = g.add_binary(Op.SUB, ema_fast, ema_slow)
    dea = g.add_rolling(Op.EMA, dif, 9)
    ranked = g.add_rolling(Op.TS_RANK, dea, 120)
    g.add_unary(Op.NEG, ranked)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0027: VWAP MACD Histogram — MACD on execution price
#    VWAP MACD captures institutional flow momentum
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0027")
def build_vwap_macd_hist() -> rt.FactorGraph:
    """TsZscore(Sub(Sub(Ema(vwap,12),Ema(vwap,26)), Ema(Sub(Ema(vwap,12),Ema(vwap,26)),9)), 120)"""
    g = rt.FactorGraph()
    v = g.add_input("vwap")
    ema_fast = g.add_rolling(Op.EMA, v, 12)
    ema_slow = g.add_rolling(Op.EMA, v, 26)
    dif = g.add_binary(Op.SUB, ema_fast, ema_slow)
    dea = g.add_rolling(Op.EMA, dif, 9)
    hist = g.add_binary(Op.SUB, dif, dea)
    g.add_rolling(Op.TS_ZSCORE, hist, 120)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0028: Net Volume MACD — MACD on (buy_vol - sell_vol) flow
#    Captures shifts in directional order flow momentum
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0028")
def build_net_vol_macd() -> rt.FactorGraph:
    """TsZscore(Sub(Sub(Ema(Sub(bv,sv),12),Ema(Sub(bv,sv),26)), Ema(Sub(Ema(Sub(bv,sv),12),Ema(Sub(bv,sv),26)),9)), 60)"""
    g = rt.FactorGraph()
    bv = g.add_input("buy_vol")
    sv = g.add_input("sell_vol")
    net = g.add_binary(Op.SUB, bv, sv)
    ema_fast = g.add_rolling(Op.EMA, net, 12)
    ema_slow = g.add_rolling(Op.EMA, net, 26)
    dif = g.add_binary(Op.SUB, ema_fast, ema_slow)
    dea = g.add_rolling(Op.EMA, dif, 9)
    hist = g.add_binary(Op.SUB, dif, dea)
    g.add_rolling(Op.TS_ZSCORE, hist, 60)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0029: Close CCI-14 — classic overbought/oversold indicator
#    CCI > +100 → overbought; CCI < -100 → oversold
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0029")
def build_close_cci_14() -> rt.FactorGraph:
    """TsZscore(TsCci(close, 14), 120)"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    cci = g.add_rolling(Op.TS_CCI, c, 14)
    g.add_rolling(Op.TS_ZSCORE, cci, 120)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0030: VWAP CCI-14 — CCI on execution price captures
#    institutional-driven overbought/oversold
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0030")
def build_vwap_cci_14() -> rt.FactorGraph:
    """Neg(TsRank(TsCci(vwap, 14), 120))"""
    g = rt.FactorGraph()
    vw = g.add_input("vwap")
    cci = g.add_rolling(Op.TS_CCI, vw, 14)
    ranked = g.add_rolling(Op.TS_RANK, cci, 120)
    g.add_unary(Op.NEG, ranked)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0031: Short CCI-5 — fast mean-reversion signal
#    Short window captures rapid price deviations from recent mean
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0031")
def build_close_cci_5() -> rt.FactorGraph:
    """TsZscore(TsCci(close, 5), 60)"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    cci = g.add_rolling(Op.TS_CCI, c, 5)
    g.add_rolling(Op.TS_ZSCORE, cci, 60)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0032: CCI Divergence — spread between close CCI and VWAP CCI
#    Divergence signals institutional vs retail positioning
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0032")
def build_cci_divergence() -> rt.FactorGraph:
    """TsZscore(Sub(TsCci(close, 14), TsCci(vwap, 14)), 120)"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    vw = g.add_input("vwap")
    cci_close = g.add_rolling(Op.TS_CCI, c, 14)
    cci_vwap = g.add_rolling(Op.TS_CCI, vw, 14)
    div = g.add_binary(Op.SUB, cci_close, cci_vwap)
    g.add_rolling(Op.TS_ZSCORE, div, 120)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0033: Upside Return Sum — sum of positive returns over 30 bars
#    Measures accumulated upside momentum
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0033")
def build_sumpos_ret_30() -> rt.FactorGraph:
    """TsZscore(TsSumPos(ret, 30), 120)"""
    g = rt.FactorGraph()
    r = g.add_input("ret")
    sp = g.add_rolling(Op.TS_SUM_POS, r, 30)
    g.add_rolling(Op.TS_ZSCORE, sp, 120)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0034: Upside Volume Sum — sum of positive net-volume changes
#    Positive buy-sell imbalance accumulation
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0034")
def build_sumpos_netvol_30() -> rt.FactorGraph:
    """TsZscore(TsSumPos(Sub(buy_vol, sell_vol), 30), 120)"""
    g = rt.FactorGraph()
    bv = g.add_input("buy_vol")
    sv = g.add_input("sell_vol")
    net = g.add_binary(Op.SUB, bv, sv)
    sp = g.add_rolling(Op.TS_SUM_POS, net, 30)
    g.add_rolling(Op.TS_ZSCORE, sp, 120)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0035: Positive Return Ratio — fraction of positive returns
#    High ratio = consistent uptrend; low ratio = downtrend
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0035")
def build_posratio_ret_30() -> rt.FactorGraph:
    """TsZscore(TsPosRatio(ret, 30), 120)"""
    g = rt.FactorGraph()
    r = g.add_input("ret")
    pr = g.add_rolling(Op.TS_POS_RATIO, r, 30)
    g.add_rolling(Op.TS_ZSCORE, pr, 120)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0036: Buy Dominance Ratio — fraction of bars where buy_vol > sell_vol
#    Measures consistency of buy-side pressure
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0036")
def build_posratio_netvol_60() -> rt.FactorGraph:
    """TsZscore(TsPosRatio(Sub(buy_vol, sell_vol), 60), 120)"""
    g = rt.FactorGraph()
    bv = g.add_input("buy_vol")
    sv = g.add_input("sell_vol")
    net = g.add_binary(Op.SUB, bv, sv)
    pr = g.add_rolling(Op.TS_POS_RATIO, net, 60)
    g.add_rolling(Op.TS_ZSCORE, pr, 120)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0037: Return Up/Down Ratio — Σ(pos ret) / Σ(|neg ret|) over 30 bars
#    ratio > 1 = upside dominates; ratio < 1 = downside dominates
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0037")
def build_updown_ret_30() -> rt.FactorGraph:
    """TsZscore(TsUpDownRatio(ret, 30), 120)"""
    g = rt.FactorGraph()
    r = g.add_input("ret")
    ud = g.add_rolling(Op.TS_UP_DOWN_RATIO, r, 30)
    g.add_rolling(Op.TS_ZSCORE, ud, 120)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0038: Volume Up/Down Ratio — Σ(buy_vol - sell_vol when >0)
#    / Σ(|buy_vol - sell_vol when <0|), microstructure pressure
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0038")
def build_updown_netvol_60() -> rt.FactorGraph:
    """Neg(TsRank(TsUpDownRatio(Sub(buy_vol, sell_vol), 60), 120))"""
    g = rt.FactorGraph()
    bv = g.add_input("buy_vol")
    sv = g.add_input("sell_vol")
    net = g.add_binary(Op.SUB, bv, sv)
    ud = g.add_rolling(Op.TS_UP_DOWN_RATIO, net, 60)
    ranked = g.add_rolling(Op.TS_RANK, ud, 120)
    g.add_unary(Op.NEG, ranked)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0039: De-correlated VWAP — remove close-correlated component
#    TsDeCor(close, vwap, w) = vwap - corr(close, vwap, w) * vwap
#    Residual captures execution-price alpha not explained by price
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0039")
def build_decor_vwap() -> rt.FactorGraph:
    """TsZscore(Sub(vwap, Mul(Corr(close, vwap, 60), vwap)), 120)"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    vw = g.add_input("vwap")
    corr = g.add_bivariate(Op.CORR, c, vw, 60)
    scaled = g.add_binary(Op.MUL, corr, vw)
    resid = g.add_binary(Op.SUB, vw, scaled)
    g.add_rolling(Op.TS_ZSCORE, resid, 120)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0040: De-correlated Volume — volume residual after removing
#    price-driven component; captures independent volume surges
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0040")
def build_decor_volume() -> rt.FactorGraph:
    """TsZscore(Sub(SLog1p(vol), Mul(Corr(ret, SLog1p(vol), 60), SLog1p(vol))), 120)"""
    g = rt.FactorGraph()
    r = g.add_input("ret")
    vol = g.add_input("volume")
    svol = g.add_unary(Op.SLOG1P, vol)
    corr = g.add_bivariate(Op.CORR, r, svol, 60)
    scaled = g.add_binary(Op.MUL, corr, svol)
    resid = g.add_binary(Op.SUB, svol, scaled)
    g.add_rolling(Op.TS_ZSCORE, resid, 120)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0041: Price Path Modulus — L2 norm of returns: sqrt(Σret²)
#    High modulus = volatile path; measures "energy" of price path
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0041")
def build_modulus_ret_30() -> rt.FactorGraph:
    """Neg(TsRank(Sqrt(TsSum(Sqr(ret), 30)), 120))"""
    g = rt.FactorGraph()
    r = g.add_input("ret")
    r2 = g.add_unary(Op.SQR, r)
    sum_r2 = g.add_rolling(Op.TS_SUM, r2, 30)
    mod = g.add_unary(Op.SQRT, sum_r2)
    ranked = g.add_rolling(Op.TS_RANK, mod, 120)
    g.add_unary(Op.NEG, ranked)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0042: Volume Path Modulus — L2 norm of log-volume changes
#    Captures bursts of volume activity
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0042")
def build_modulus_vol_30() -> rt.FactorGraph:
    """TsZscore(Sqrt(TsSum(Sqr(TsPct(SLog1p(volume), 1)), 30)), 120)"""
    g = rt.FactorGraph()
    vol = g.add_input("volume")
    svol = g.add_unary(Op.SLOG1P, vol)
    vol_chg = g.add_rolling(Op.TS_PCT, svol, 1)
    chg2 = g.add_unary(Op.SQR, vol_chg)
    sum_chg2 = g.add_rolling(Op.TS_SUM, chg2, 30)
    mod = g.add_unary(Op.SQRT, sum_chg2)
    g.add_rolling(Op.TS_ZSCORE, mod, 120)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0043: SignSqrt Return — sign(ret) * sqrt(|ret|)
#    Compresses extreme returns symmetrically, z-scored
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0043")
def build_sign_sqrt_ret() -> rt.FactorGraph:
    """TsZscore(SignSqrt(ret), 120)"""
    g = rt.FactorGraph()
    r = g.add_input("ret")
    ss = g.add_unary(Op.SIGN_SQRT, r)
    g.add_rolling(Op.TS_ZSCORE, ss, 120)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0044: SignSqrt Imbalance — compressed trade imbalance
#    SignSqrt on (buy_vol - sell_vol), more robust to outliers
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0044")
def build_sign_sqrt_imbalance() -> rt.FactorGraph:
    """TsZscore(SignSqrt(Sub(buy_vol, sell_vol)), 120)"""
    g = rt.FactorGraph()
    bv = g.add_input("buy_vol")
    sv = g.add_input("sell_vol")
    net = g.add_binary(Op.SUB, bv, sv)
    ss = g.add_unary(Op.SIGN_SQRT, net)
    g.add_rolling(Op.TS_ZSCORE, ss, 120)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0045: Saturated Imbalance — Tanh(imbalance) smoothly clips to [-1, 1]
#    Robust to extreme imbalance spikes, then ranked
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0045")
def build_saturated_imbalance() -> rt.FactorGraph:
    """Neg(TsRank(Tanh(Div(Sub(bv,sv),Add(bv,sv))), 120))"""
    g = rt.FactorGraph()
    bv = g.add_input("buy_vol")
    sv = g.add_input("sell_vol")
    diff = g.add_binary(Op.SUB, bv, sv)
    total = g.add_binary(Op.ADD, bv, sv)
    imb = g.add_binary(Op.DIV, diff, total)
    clipped = g.add_unary(Op.TANH, imb)
    ranked = g.add_rolling(Op.TS_RANK, clipped, 120)
    g.add_unary(Op.NEG, ranked)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0046: FMax(ret, volume_chg) — captures whichever signal is
#    stronger: price move or volume surge
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0046")
def build_fmax_ret_vol() -> rt.FactorGraph:
    """TsZscore(FMax(ret, TsPct(SLog1p(volume), 1)), 120)"""
    g = rt.FactorGraph()
    r = g.add_input("ret")
    vol = g.add_input("volume")
    svol = g.add_unary(Op.SLOG1P, vol)
    vol_chg = g.add_rolling(Op.TS_PCT, svol, 1)
    mx = g.add_binary(Op.FMAX, r, vol_chg)
    g.add_rolling(Op.TS_ZSCORE, mx, 120)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0047: Imbalance Skewness — tail asymmetry of trade flow
#    Persistent positive skew = buy bursts; negative = sell bursts
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0047")
def build_imbalance_skew() -> rt.FactorGraph:
    """Neg(TsRank(TsSkew(Div(Sub(buy_vol, sell_vol), Add(buy_vol, sell_vol)), 30), 120))"""
    g = rt.FactorGraph()
    bv = g.add_input("buy_vol")
    sv = g.add_input("sell_vol")
    diff = g.add_binary(Op.SUB, bv, sv)
    total = g.add_binary(Op.ADD, bv, sv)
    imbalance = g.add_binary(Op.DIV, diff, total)
    skew = g.add_rolling(Op.TS_SKEW, imbalance, 30)
    ranked = g.add_rolling(Op.TS_RANK, skew, 120)
    g.add_unary(Op.NEG, ranked)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0048: VWAP Median Reversion — VWAP vs rolling median
#    Robust alternative to MA-based VWAP momentum
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0048")
def build_vwap_median_dev() -> rt.FactorGraph:
    """TsZscore(Div(Sub(vwap, TsMed(vwap, 60)), TsMad(vwap, 60)), 120)"""
    g = rt.FactorGraph()
    vw = g.add_input("vwap")
    med = g.add_rolling(Op.TS_MED, vw, 60)
    mad = g.add_rolling(Op.TS_MAD, vw, 60)
    dev = g.add_binary(Op.SUB, vw, med)
    norm = g.add_binary(Op.DIV, dev, mad)
    g.add_rolling(Op.TS_ZSCORE, norm, 120)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0049: WMA Volume Trend — linearly-weighted volume trend
#    Recent volume bars weighted more; captures acceleration
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0049")
def build_wma_vol_trend() -> rt.FactorGraph:
    """Neg(TsRank(Div(Sub(SLog1p(volume), TsWma(SLog1p(volume), 30)), TsStd(SLog1p(volume), 30)), 120))"""
    g = rt.FactorGraph()
    vol = g.add_input("volume")
    svol = g.add_unary(Op.SLOG1P, vol)
    wma = g.add_rolling(Op.TS_WMA, svol, 30)
    dev = g.add_binary(Op.SUB, svol, wma)
    std = g.add_rolling(Op.TS_STD, svol, 30)
    norm = g.add_binary(Op.DIV, dev, std)
    ranked = g.add_rolling(Op.TS_RANK, norm, 120)
    g.add_unary(Op.NEG, ranked)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0050: Close Drawdown — how far close is from its recent peak
#    TsMaxDiff = close - max(close, w) ≤ 0
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0050")
def build_close_drawdown() -> rt.FactorGraph:
    """TsRank(TsMaxDiff(close, 30), 120)"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    dd = g.add_rolling(Op.TS_MAX_DIFF, c, 30)
    g.add_rolling(Op.TS_RANK, dd, 120)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0051: VWAP Breakout — how far VWAP is from its recent trough
#    TsMinDiff = vwap - min(vwap, w) ≥ 0
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0051")
def build_vwap_breakout() -> rt.FactorGraph:
    """Neg(TsRank(TsMinDiff(vwap, 30), 120))"""
    g = rt.FactorGraph()
    vw = g.add_input("vwap")
    bu = g.add_rolling(Op.TS_MIN_DIFF, vw, 30)
    ranked = g.add_rolling(Op.TS_RANK, bu, 120)
    g.add_unary(Op.NEG, ranked)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0052: Close Range Oscillator — normalized range width
#    TsMinmaxDiff / MA captures relative spread
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0052")
def build_range_oscillator() -> rt.FactorGraph:
    """TsZscore(Div(TsMinmaxDiff(close, 30), Ma(close, 30)), 120)"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    rng = g.add_rolling(Op.TS_MINMAX_DIFF, c, 30)
    ma = g.add_rolling(Op.MA, c, 30)
    norm = g.add_binary(Op.DIV, rng, ma)
    g.add_rolling(Op.TS_ZSCORE, norm, 120)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  T0053: Imbalance Variance Regime — high variance = unstable flow
#    TsVari on buy-sell imbalance as regime indicator
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp_trades", "T0053")
def build_imbalance_vari() -> rt.FactorGraph:
    """Neg(TsRank(TsVari(Div(Sub(buy_vol, sell_vol), Add(buy_vol, sell_vol)), 30), 120))"""
    g = rt.FactorGraph()
    bv = g.add_input("buy_vol")
    sv = g.add_input("sell_vol")
    diff = g.add_binary(Op.SUB, bv, sv)
    total = g.add_binary(Op.ADD, bv, sv)
    imbalance = g.add_binary(Op.DIV, diff, total)
    var = g.add_rolling(Op.TS_VARI, imbalance, 30)
    ranked = g.add_rolling(Op.TS_RANK, var, 120)
    g.add_unary(Op.NEG, ranked)
    g.compile()
    return g
