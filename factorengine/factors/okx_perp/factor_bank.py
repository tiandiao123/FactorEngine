"""
OKX Perpetual factor builders.

Factors derived from crypto kbar data (close, volume, open, high, low).
Add new factors by adding @register_factor("okx_perp", "XXXX") functions.

Source: skydiscover/skygen/factorgen/cryptokbar/crypto_factor_inference/
        rewritten_factor_bank/factors/
"""

from factorengine.factors.registry import register_factor
import fe_runtime as rt

Op = rt.Op


# ═══════════════════════════════════════════════════════════════
#  0001: Price deviation from MA, normalized by volatility
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0001")
def build_factor_0001() -> rt.FactorGraph:
    """Div(Sub(close, Ma(close, 120)), TsStd(close, 60))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    ma120 = g.add_rolling(Op.MA, c, 120)
    dev = g.add_binary(Op.SUB, c, ma120)
    vol = g.add_rolling(Op.TS_STD, c, 60)
    g.add_binary(Op.DIV, dev, vol)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0002: Volatility-Normalized Reversal with Volume Confirmation
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0002")
def build_factor_0002() -> rt.FactorGraph:
    """Div(Div(TsDiff(Log(close),30), TsStd(TsDiff(Log(close),1),120)),
           Neg(TsRank(volume,240)))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    v = g.add_input("volume")
    logc = g.add_unary(Op.LOG, c)
    ret30 = g.add_rolling(Op.TS_DIFF, logc, 30)
    ret1 = g.add_rolling(Op.TS_DIFF, logc, 1)
    vol120 = g.add_rolling(Op.TS_STD, ret1, 120)
    zscore_ret = g.add_binary(Op.DIV, ret30, vol120)
    vol_rank = g.add_rolling(Op.TS_RANK, v, 240)
    neg_vr = g.add_unary(Op.NEG, vol_rank)
    g.add_binary(Op.DIV, zscore_ret, neg_vr)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0003: Volatility-Adjusted Velocity Divergence
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0003")
def build_factor_0003() -> rt.FactorGraph:
    """Mul(TsRank(Div(TsDiff(Log(close),15), TsStd(TsDiff(Log(close),1),60)), 240),
           TsRank(Div(Abs(TsDiff(Log(close),15)), SLog1p(Ma(volume,30))), 240))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    v = g.add_input("volume")
    logc = g.add_unary(Op.LOG, c)
    ret1 = g.add_rolling(Op.TS_DIFF, logc, 1)
    velocity = g.add_rolling(Op.TS_DIFF, logc, 15)
    rvol = g.add_rolling(Op.TS_STD, ret1, 60)
    move_eff = g.add_binary(Op.DIV, velocity, rvol)
    avg_vol = g.add_rolling(Op.MA, v, 30)
    slog_vol = g.add_unary(Op.SLOG1P, avg_vol)
    abs_vel = g.add_unary(Op.ABS, velocity)
    price_impact = g.add_binary(Op.DIV, abs_vel, slog_vol)
    eff_rank = g.add_rolling(Op.TS_RANK, move_eff, 240)
    impact_rank = g.add_rolling(Op.TS_RANK, price_impact, 240)
    g.add_binary(Op.MUL, eff_rank, impact_rank)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0004: Volatility-Adjusted RSI variant
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0004")
def build_factor_0004() -> rt.FactorGraph:
    """Neg(TsRank(Mul(Div(TsDiff(Log(close),5), TsStd(TsDiff(Log(close),5),60)),
                      Div(Abs(TsDiff(Log(close),30)),
                          TsSum(Abs(TsDiff(Log(close),5)),6))), 180))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    logc = g.add_unary(Op.LOG, c)
    ret5 = g.add_rolling(Op.TS_DIFF, logc, 5)
    vol60 = g.add_rolling(Op.TS_STD, ret5, 60)
    vol_adj_ret = g.add_binary(Op.DIV, ret5, vol60)
    disp = g.add_unary(Op.ABS, g.add_rolling(Op.TS_DIFF, logc, 30))
    abs_ret5 = g.add_unary(Op.ABS, ret5)
    path_len = g.add_rolling(Op.TS_SUM, abs_ret5, 6)
    efficiency = g.add_binary(Op.DIV, disp, path_len)
    weighted_mom = g.add_binary(Op.MUL, vol_adj_ret, efficiency)
    ranked = g.add_rolling(Op.TS_RANK, weighted_mom, 180)
    g.add_unary(Op.NEG, ranked)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0005: Volume-Momentum Divergence with Volatility Regime
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0005")
def build_factor_0005() -> rt.FactorGraph:
    """TsRank(Mul(Neg(Corr(TsDiff(Log(close),1),
         TsDiff(Ema(Log(volume),5),1),120)),
         Abs(TsZscore(TsStd(TsDiff(Log(close),1),30),360))), 240)"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    v = g.add_input("volume")
    logc = g.add_unary(Op.LOG, c)
    ret = g.add_rolling(Op.TS_DIFF, logc, 1)
    logv = g.add_unary(Op.LOG, v)
    ema_logv = g.add_rolling(Op.EMA, logv, 5)
    vol_change = g.add_rolling(Op.TS_DIFF, ema_logv, 1)
    pv_corr = g.add_bivariate(Op.CORR, ret, vol_change, 120)
    rvol = g.add_rolling(Op.TS_STD, ret, 30)
    vol_regime = g.add_rolling(Op.TS_ZSCORE, rvol, 360)
    abs_regime = g.add_unary(Op.ABS, vol_regime)
    divergence = g.add_binary(Op.MUL, g.add_unary(Op.NEG, pv_corr), abs_regime)
    g.add_rolling(Op.TS_RANK, divergence, 240)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0007: Volume-Price Divergence Reversal (VPDR)
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0007")
def build_factor_0007() -> rt.FactorGraph:
    """Neg(Mul(Sub(TsRank(TsDiff(Log(close),30),240),
                   TsRank(Div(Ma(volume,30),Ma(volume,120)),240)),
               TsRank(TsStd(TsDiff(Log(close),1),60),360)))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    v = g.add_input("volume")
    logc = g.add_unary(Op.LOG, c)
    price_mom = g.add_rolling(Op.TS_DIFF, logc, 30)
    vol_short = g.add_rolling(Op.MA, v, 30)
    vol_med = g.add_rolling(Op.MA, v, 120)
    vol_ratio = g.add_binary(Op.DIV, vol_short, vol_med)
    mom_rank = g.add_rolling(Op.TS_RANK, price_mom, 240)
    vol_rank = g.add_rolling(Op.TS_RANK, vol_ratio, 240)
    divergence = g.add_binary(Op.SUB, mom_rank, vol_rank)
    ret1 = g.add_rolling(Op.TS_DIFF, logc, 1)
    rvol = g.add_rolling(Op.TS_STD, ret1, 60)
    rvol_rank = g.add_rolling(Op.TS_RANK, rvol, 360)
    signal = g.add_binary(Op.MUL, divergence, rvol_rank)
    g.add_unary(Op.NEG, signal)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0009: Intraday Range Compression Breakout
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0009")
def build_factor_0009() -> rt.FactorGraph:
    """Tanh(Div(TsZscore(Mul(Sub(Div(Sub(close,TsMin(low,30)),
       Sub(TsMax(high,30),TsMin(low,30))),0.5),
       Sub(1,TsRank(Div(Div(Sub(TsMax(high,30),TsMin(low,30)),close),1),360))),120),2))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    h = g.add_input("high")
    lo = g.add_input("low")
    rh = g.add_rolling(Op.TS_MAX, h, 30)
    rl = g.add_rolling(Op.TS_MIN, lo, 30)
    price_range = g.add_binary(Op.SUB, rh, rl)
    norm_range = g.add_binary(Op.DIV, price_range, c)
    range_rank = g.add_rolling(Op.TS_RANK, norm_range, 360)
    close_pos = g.add_binary(Op.DIV, g.add_binary(Op.SUB, c, rl), price_range)
    centered_pos = g.add_scalar_op(Op.SUB_SCALAR, close_pos, 0.5)
    comp_weight = g.add_scalar_op(Op.SCALAR_SUB, range_rank, 1.0)
    raw_signal = g.add_binary(Op.MUL, centered_pos, comp_weight)
    signal_z = g.add_rolling(Op.TS_ZSCORE, raw_signal, 120)
    g.add_unary(Op.TANH, g.add_scalar_op(Op.DIV_SCALAR, signal_z, 2.0))
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0010: Volume-Price Convexity Divergence
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0010")
def build_factor_0010() -> rt.FactorGraph:
    """Neg(TsRank(Div(price_convexity, SLog1p(vol_surge)), 180))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    v = g.add_input("volume")
    ma10 = g.add_rolling(Op.MA, c, 10)
    dev = g.add_binary(Op.SUB, c, ma10)
    vol60 = g.add_rolling(Op.TS_STD, c, 60)
    price_conv = g.add_binary(Op.DIV, dev, vol60)
    vol_short = g.add_rolling(Op.MA, v, 5)
    vol_long = g.add_rolling(Op.MA, v, 120)
    vol_ratio = g.add_binary(Op.DIV, vol_short, vol_long)
    vol_slog = g.add_unary(Op.SLOG1P, vol_ratio)
    raw = g.add_binary(Op.DIV, price_conv, vol_slog)
    ranked = g.add_rolling(Op.TS_RANK, raw, 180)
    g.add_unary(Op.NEG, ranked)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0011: Price-Volume Correlation Divergence
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0011")
def build_factor_0011() -> rt.FactorGraph:
    """Mul(Neg(TsRank(Corr(TsDiff(Log(close),5),TsDiff(SLog1p(volume),5),60),120)),
           TsRank(TsStd(TsDiff(Log(close),1),15),120))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    v = g.add_input("volume")
    logc = g.add_unary(Op.LOG, c)
    ret5 = g.add_rolling(Op.TS_DIFF, logc, 5)
    slogv = g.add_unary(Op.SLOG1P, v)
    vol_change5 = g.add_rolling(Op.TS_DIFF, slogv, 5)
    pv_corr = g.add_bivariate(Op.CORR, ret5, vol_change5, 60)
    divergence = g.add_unary(Op.NEG, g.add_rolling(Op.TS_RANK, pv_corr, 120))
    ret1 = g.add_rolling(Op.TS_DIFF, logc, 1)
    recent_vol = g.add_rolling(Op.TS_STD, ret1, 15)
    vol_rank = g.add_rolling(Op.TS_RANK, recent_vol, 120)
    g.add_binary(Op.MUL, divergence, vol_rank)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0012: Liquidity-Adjusted Volatility Compression
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0012")
def build_factor_0012() -> rt.FactorGraph:
    """Neg(Mul(TsDiff(Log(close),60),
              TsRank(Div(Div(TsStd(close,15),TsStd(close,240)),
                         SLog1p(Div(Ma(volume,15),Ma(volume,240)))),480)))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    v = g.add_input("volume")
    std15 = g.add_rolling(Op.TS_STD, c, 15)
    std240 = g.add_rolling(Op.TS_STD, c, 240)
    rel_vol = g.add_binary(Op.DIV, std15, std240)
    vol15 = g.add_rolling(Op.MA, v, 15)
    vol240 = g.add_rolling(Op.MA, v, 240)
    rel_vccy = g.add_binary(Op.DIV, vol15, vol240)
    slog_vccy = g.add_unary(Op.SLOG1P, rel_vccy)
    lac = g.add_binary(Op.DIV, rel_vol, slog_vccy)
    lac_rank = g.add_rolling(Op.TS_RANK, lac, 480)
    logc = g.add_unary(Op.LOG, c)
    mom60 = g.add_rolling(Op.TS_DIFF, logc, 60)
    signal = g.add_binary(Op.MUL, mom60, lac_rank)
    g.add_unary(Op.NEG, signal)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0013: Volatility-Adjusted Relative Strength Efficiency
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0013")
def build_factor_0013() -> rt.FactorGraph:
    """TsZscore(Mul(Div(TsDiff(Log(close),60),TsStd(TsDiff(Log(close),1),60)),
                    TsRank(Div(TsStd(TsDiff(Log(close),1),30),
                               TsStd(TsDiff(Log(close),1),240)),120)),180)"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    logc = g.add_unary(Op.LOG, c)
    ret1 = g.add_rolling(Op.TS_DIFF, logc, 1)
    displacement = g.add_rolling(Op.TS_DIFF, logc, 60)
    path_vol = g.add_rolling(Op.TS_STD, ret1, 60)
    eff_ratio = g.add_binary(Op.DIV, displacement, path_vol)
    vol_short = g.add_rolling(Op.TS_STD, ret1, 30)
    vol_long = g.add_rolling(Op.TS_STD, ret1, 240)
    vol_regime = g.add_binary(Op.DIV, vol_short, vol_long)
    vol_regime_rank = g.add_rolling(Op.TS_RANK, vol_regime, 120)
    signal = g.add_binary(Op.MUL, eff_ratio, vol_regime_rank)
    g.add_rolling(Op.TS_ZSCORE, signal, 180)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0014: Volume-Weighted Price Volatility Asymmetry
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0014")
def build_factor_0014() -> rt.FactorGraph:
    """Neg(Mul(TsZscore(TsDiff(close,60),120),
              TsRank(Div(TsStd(TsDiff(Log(close),1),30),
                         Ma(SLog1p(volume),30)),240)))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    v = g.add_input("volume")
    logc = g.add_unary(Op.LOG, c)
    ret1 = g.add_rolling(Op.TS_DIFF, logc, 1)
    volatility = g.add_rolling(Op.TS_STD, ret1, 30)
    slogv = g.add_unary(Op.SLOG1P, v)
    smoothed_vol = g.add_rolling(Op.MA, slogv, 30)
    vol_eff = g.add_binary(Op.DIV, volatility, smoothed_vol)
    eff_rank = g.add_rolling(Op.TS_RANK, vol_eff, 240)
    trend_dir = g.add_rolling(Op.TS_ZSCORE, g.add_rolling(Op.TS_DIFF, c, 60), 120)
    signal = g.add_binary(Op.MUL, trend_dir, eff_rank)
    g.add_unary(Op.NEG, signal)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0015: Liquidity-Adjusted Price Efficiency
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0015")
def build_factor_0015() -> rt.FactorGraph:
    """Neg(Mul(Sub(TsRank(Ma(Div(Div(Sub(high,low),close),SLog1p(volume)),20),240),0.5),
              Sub(TsRank(TsDiff(Log(close),60),360),0.5)))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    h = g.add_input("high")
    lo = g.add_input("low")
    v = g.add_input("volume")
    price_range = g.add_binary(Op.DIV, g.add_binary(Op.SUB, h, lo), c)
    log_vol = g.add_unary(Op.SLOG1P, v)
    illiq = g.add_binary(Op.DIV, price_range, log_vol)
    illiq_smooth = g.add_rolling(Op.MA, illiq, 20)
    illiq_rank = g.add_rolling(Op.TS_RANK, illiq_smooth, 240)
    logc = g.add_unary(Op.LOG, c)
    ret60 = g.add_rolling(Op.TS_DIFF, logc, 60)
    ret_rank = g.add_rolling(Op.TS_RANK, ret60, 360)
    illiq_c = g.add_scalar_op(Op.SUB_SCALAR, illiq_rank, 0.5)
    ret_c = g.add_scalar_op(Op.SUB_SCALAR, ret_rank, 0.5)
    raw = g.add_binary(Op.MUL, illiq_c, ret_c)
    g.add_unary(Op.NEG, raw)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0016: Liquidity-Adjusted Volatility Compression (LAVC)
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0016")
def build_factor_0016() -> rt.FactorGraph:
    """Neg(Mul(Div(TsRank(Div(Ma(volume,60),Ma(volume,360)),360),
                   SLog1p(Div(Ma(Div(Sub(high,low),close),30),
                              Ma(Div(Sub(high,low),close),240)))),
              TsDiff(Log(close),60)))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    h = g.add_input("high")
    lo = g.add_input("low")
    v = g.add_input("volume")
    range_pct = g.add_binary(Op.DIV, g.add_binary(Op.SUB, h, lo), c)
    vol_comp = g.add_binary(Op.DIV, g.add_rolling(Op.MA, range_pct, 30),
                            g.add_rolling(Op.MA, range_pct, 240))
    rel_vol = g.add_rolling(Op.TS_RANK,
                            g.add_binary(Op.DIV, g.add_rolling(Op.MA, v, 60),
                                         g.add_rolling(Op.MA, v, 360)), 360)
    absorption = g.add_binary(Op.DIV, rel_vol, g.add_unary(Op.SLOG1P, vol_comp))
    logc = g.add_unary(Op.LOG, c)
    recent_ret = g.add_rolling(Op.TS_DIFF, logc, 60)
    signal = g.add_binary(Op.MUL, absorption, recent_ret)
    g.add_unary(Op.NEG, signal)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0018: Intraday Momentum Decay
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0018")
def build_factor_0018() -> rt.FactorGraph:
    """Neg(TsZscore(TsDiff(Ema(Div(Ema(TsDiff(Log(close),1),20),
                                    Ema(SLog1p(volume),20)),15),45),360))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    v = g.add_input("volume")
    logc = g.add_unary(Op.LOG, c)
    log_ret = g.add_rolling(Op.TS_DIFF, logc, 1)
    slogv = g.add_unary(Op.SLOG1P, v)
    smooth_ret = g.add_rolling(Op.EMA, log_ret, 20)
    smooth_vol = g.add_rolling(Op.EMA, slogv, 20)
    efficiency = g.add_binary(Op.DIV, smooth_ret, smooth_vol)
    eff_ema = g.add_rolling(Op.EMA, efficiency, 15)
    eff_accel = g.add_rolling(Op.TS_DIFF, eff_ema, 45)
    signal_z = g.add_rolling(Op.TS_ZSCORE, eff_accel, 360)
    g.add_unary(Op.NEG, signal_z)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0019: Volume-Price Convexity Divergence (VPCD)
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0019")
def build_factor_0019() -> rt.FactorGraph:
    """Neg(Mul(TsRank(Sub(Ma(TsDiff(Log(close),1),10),
                          Ma(TsDiff(Log(close),1),60)),240),
              TsRank(Div(volume,Ma(volume,120)),240)))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    v = g.add_input("volume")
    logc = g.add_unary(Op.LOG, c)
    ret1 = g.add_rolling(Op.TS_DIFF, logc, 1)
    fast_ma = g.add_rolling(Op.MA, ret1, 10)
    slow_ma = g.add_rolling(Op.MA, ret1, 60)
    price_accel = g.add_binary(Op.SUB, fast_ma, slow_ma)
    vol_ma = g.add_rolling(Op.MA, v, 120)
    vol_intensity = g.add_binary(Op.DIV, v, vol_ma)
    accel_rank = g.add_rolling(Op.TS_RANK, price_accel, 240)
    vol_rank = g.add_rolling(Op.TS_RANK, vol_intensity, 240)
    overheating = g.add_binary(Op.MUL, accel_rank, vol_rank)
    g.add_unary(Op.NEG, overheating)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0020: Intraday Range Efficiency with Volume Decay
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0020")
def build_factor_0020() -> rt.FactorGraph:
    """Neg(TsZscore(Mul(Sub(range_pos, 0.5), vol_ratio), 240))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    h = g.add_input("high")
    lo = g.add_input("low")
    v = g.add_input("volume")
    rh = g.add_rolling(Op.TS_MAX, h, 120)
    rl = g.add_rolling(Op.TS_MIN, lo, 120)
    rng = g.add_binary(Op.SUB, rh, rl)
    pos = g.add_binary(Op.DIV, g.add_binary(Op.SUB, c, rl), rng)
    centered = g.add_scalar_op(Op.SUB_SCALAR, pos, 0.5)
    vs = g.add_rolling(Op.MA, v, 15)
    vl = g.add_rolling(Op.MA, v, 120)
    vr = g.add_binary(Op.DIV, vs, vl)
    raw = g.add_binary(Op.MUL, centered, vr)
    zs = g.add_rolling(Op.TS_ZSCORE, raw, 240)
    g.add_unary(Op.NEG, zs)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0022: Relative Volatility Compression and Expansion
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0022")
def build_factor_0022() -> rt.FactorGraph:
    """Mul(TsRank(Div(Sub(TsMax(high,15),TsMin(low,15)),
                      Mul(close,TsStd(TsDiff(Log(close),1),120))),120),
           Mul(TsRank(TsZscore(TsDiff(Log(close),30),180),120),
               TsRank(Div(Ma(volume,10),Ma(volume,60)),120)))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    h = g.add_input("high")
    lo = g.add_input("low")
    v = g.add_input("volume")
    logc = g.add_unary(Op.LOG, c)
    ret1 = g.add_rolling(Op.TS_DIFF, logc, 1)
    short_range = g.add_binary(Op.SUB, g.add_rolling(Op.TS_MAX, h, 15),
                               g.add_rolling(Op.TS_MIN, lo, 15))
    med_vol = g.add_rolling(Op.TS_STD, ret1, 120)
    denom = g.add_binary(Op.MUL, c, med_vol)
    vol_ratio = g.add_binary(Op.DIV, short_range, denom)
    ret30 = g.add_rolling(Op.TS_DIFF, logc, 30)
    z_ret30 = g.add_rolling(Op.TS_ZSCORE, ret30, 180)
    rel_v = g.add_binary(Op.DIV, g.add_rolling(Op.MA, v, 10),
                         g.add_rolling(Op.MA, v, 60))
    rank_exp = g.add_rolling(Op.TS_RANK, vol_ratio, 120)
    rank_dir = g.add_rolling(Op.TS_RANK, z_ret30, 120)
    rank_conv = g.add_rolling(Op.TS_RANK, rel_v, 120)
    g.add_binary(Op.MUL, rank_exp, g.add_binary(Op.MUL, rank_dir, rank_conv))
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0023: Signed Volume Impulse Decay
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0023")
def build_factor_0023() -> rt.FactorGraph:
    """Neg(TsRank(Div(TsSum(Mul(volume,ret),30),
                      TsSum(Abs(Mul(volume,ret)),180)),360))"""
    g = rt.FactorGraph()
    v = g.add_input("volume")
    r = g.add_input("ret")
    signed_vol = g.add_binary(Op.MUL, v, r)
    sv_short = g.add_rolling(Op.TS_SUM, signed_vol, 30)
    abs_sv = g.add_unary(Op.ABS, signed_vol)
    sv_norm = g.add_rolling(Op.TS_SUM, abs_sv, 180)
    dv_ratio = g.add_binary(Op.DIV, sv_short, sv_norm)
    dv_ranked = g.add_rolling(Op.TS_RANK, dv_ratio, 360)
    g.add_unary(Op.NEG, dv_ranked)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0024: Intraday Momentum Exhaustion via Price Efficiency Decay
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0024")
def build_factor_0024() -> rt.FactorGraph:
    """Neg(Div(Div(TsDiff(Log(close),20),TsStd(TsDiff(Log(close),1),20)),
              Ma(TsStd(TsDiff(Log(close),1),120),30)))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    logc = g.add_unary(Op.LOG, c)
    ret1 = g.add_rolling(Op.TS_DIFF, logc, 1)
    disp_short = g.add_rolling(Op.TS_DIFF, logc, 20)
    rough_short = g.add_rolling(Op.TS_STD, ret1, 20)
    efficiency = g.add_binary(Op.DIV, disp_short, rough_short)
    long_vol = g.add_rolling(Op.TS_STD, ret1, 120)
    smooth_vol = g.add_rolling(Op.MA, long_vol, 30)
    signal = g.add_binary(Op.DIV, efficiency, smooth_vol)
    g.add_unary(Op.NEG, signal)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0025: Relative Volatility Compression Breakout
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0025")
def build_factor_0025() -> rt.FactorGraph:
    """Neg(Mul(Mul(TsZscore(TsDiff(Log(close),15),120),
                   Div(Sub(close,TsMin(low,60)),Sub(TsMax(high,60),TsMin(low,60)))),
              Div(TsStd(TsDiff(Log(close),1),30),TsStd(TsDiff(Log(close),1),240))))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    h = g.add_input("high")
    lo = g.add_input("low")
    logc = g.add_unary(Op.LOG, c)
    ret1 = g.add_rolling(Op.TS_DIFF, logc, 1)
    short_vol = g.add_rolling(Op.TS_STD, ret1, 30)
    long_vol = g.add_rolling(Op.TS_STD, ret1, 240)
    vol_comp = g.add_binary(Op.DIV, short_vol, long_vol)
    wh = g.add_rolling(Op.TS_MAX, h, 60)
    wl = g.add_rolling(Op.TS_MIN, lo, 60)
    range_pos = g.add_binary(Op.DIV, g.add_binary(Op.SUB, c, wl),
                             g.add_binary(Op.SUB, wh, wl))
    move_str = g.add_rolling(Op.TS_ZSCORE, g.add_rolling(Op.TS_DIFF, logc, 15), 120)
    raw = g.add_binary(Op.MUL, move_str, range_pos)
    final = g.add_binary(Op.MUL, raw, vol_comp)
    g.add_unary(Op.NEG, final)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0026: Order Flow Imbalance Persistence
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0026")
def build_factor_0026() -> rt.FactorGraph:
    """Neg(Tanh(TsZscore(TsSum(Mul(TsDiff(Log(close),1),SLog1p(volume)),60),360)))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    v = g.add_input("volume")
    logc = g.add_unary(Op.LOG, c)
    minute_ret = g.add_rolling(Op.TS_DIFF, logc, 1)
    log_vol = g.add_unary(Op.SLOG1P, v)
    signed_flow = g.add_binary(Op.MUL, minute_ret, log_vol)
    flow_accum = g.add_rolling(Op.TS_SUM, signed_flow, 60)
    flow_z = g.add_rolling(Op.TS_ZSCORE, flow_accum, 360)
    compressed = g.add_unary(Op.TANH, flow_z)
    g.add_unary(Op.NEG, compressed)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0028: Volume-Weighted Price Convexity
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0028")
def build_factor_0028() -> rt.FactorGraph:
    """Mul(Mul(TsDiff(Log(close),60),
              Neg(TsRank(Div(Abs(TsDiff(close,120)),
                             TsSum(Abs(TsDiff(close,1)),120)),240))),
           TsRank(Ma(Mul(Sqr(TsDiff(Log(close),1)),Div(volume,Ma(volume,120))),30),240))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    v = g.add_input("volume")
    logc = g.add_unary(Op.LOG, c)
    disp = g.add_unary(Op.ABS, g.add_rolling(Op.TS_DIFF, c, 120))
    abs_step = g.add_unary(Op.ABS, g.add_rolling(Op.TS_DIFF, c, 1))
    path = g.add_rolling(Op.TS_SUM, abs_step, 120)
    path_eff = g.add_binary(Op.DIV, disp, path)
    ret_sq = g.add_unary(Op.SQR, g.add_rolling(Op.TS_DIFF, logc, 1))
    vol_w = g.add_binary(Op.DIV, v, g.add_rolling(Op.MA, v, 120))
    weighted_vol = g.add_rolling(Op.MA, g.add_binary(Op.MUL, ret_sq, vol_w), 30)
    eff_rank = g.add_rolling(Op.TS_RANK, path_eff, 240)
    vol_rank = g.add_rolling(Op.TS_RANK, weighted_vol, 240)
    recent_trend = g.add_rolling(Op.TS_DIFF, logc, 60)
    g.add_binary(Op.MUL, g.add_binary(Op.MUL, recent_trend,
                                       g.add_unary(Op.NEG, eff_rank)), vol_rank)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0029: Volume-Weighted Price Convexity (acceleration)
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0029")
def build_factor_0029() -> rt.FactorGraph:
    """Neg(Mul(Div(Sub(TsDiff(Log(close),15),
                       Div(TsDiff(Log(close),45),3)),
                   TsStd(TsDiff(Log(close),1),120)),
              TsRank(volume,120)))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    v = g.add_input("volume")
    logc = g.add_unary(Op.LOG, c)
    ret1 = g.add_rolling(Op.TS_DIFF, logc, 1)
    mom_short = g.add_rolling(Op.TS_DIFF, logc, 15)
    mom_mid = g.add_rolling(Op.TS_DIFF, logc, 45)
    avg_mid = g.add_scalar_op(Op.DIV_SCALAR, mom_mid, 3.0)
    accel = g.add_binary(Op.SUB, mom_short, avg_mid)
    vol = g.add_rolling(Op.TS_STD, ret1, 120)
    norm_accel = g.add_binary(Op.DIV, accel, vol)
    vol_regime = g.add_rolling(Op.TS_RANK, v, 120)
    raw = g.add_binary(Op.MUL, norm_accel, vol_regime)
    g.add_unary(Op.NEG, raw)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0050: Volume-price rank correlation
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0050")
def build_factor_0050() -> rt.FactorGraph:
    """Neg(Corr(TsRank(pct_change(close), 30), TsRank(volume, 30), 120))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    v = g.add_input("volume")
    pct = g.add_rolling(Op.PCT_CHANGE, c, 1)
    rr = g.add_rolling(Op.TS_RANK, pct, 30)
    vr = g.add_rolling(Op.TS_RANK, v, 30)
    corr = g.add_bivariate(Op.CORR, rr, vr, 120)
    g.add_unary(Op.NEG, corr)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0100: Volume-Price Divergence Reversal
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0100")
def build_factor_0100() -> rt.FactorGraph:
    """Neg(Div(Ma(Sub(TsRank(close,180), TsRank(vol,180)), 30),
              TsStd(Sub(TsRank(close,180), TsRank(vol,180)), 360)))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    v = g.add_input("volume")
    pr = g.add_rolling(Op.TS_RANK, c, 180)
    vr = g.add_rolling(Op.TS_RANK, v, 180)
    div_ = g.add_binary(Op.SUB, pr, vr)
    smooth = g.add_rolling(Op.MA, div_, 30)
    std = g.add_rolling(Op.TS_STD, div_, 360)
    norm = g.add_binary(Op.DIV, smooth, std)
    g.add_unary(Op.NEG, norm)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0031: Volume-Price Divergence Regime
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0031")
def build_factor_0031() -> rt.FactorGraph:
    """Neg(TsRank(Tanh(Mul(TsZscore(Ma(ret,30),360),
                           Corr(Abs(ret),volume,120))),240))"""
    g = rt.FactorGraph()
    r = g.add_input("ret")
    v = g.add_input("volume")
    ret30 = g.add_rolling(Op.MA, r, 30)
    price_z = g.add_rolling(Op.TS_ZSCORE, ret30, 360)
    abs_ret = g.add_unary(Op.ABS, r)
    rv_corr = g.add_bivariate(Op.CORR, abs_ret, v, 120)
    raw = g.add_unary(Op.TANH, g.add_binary(Op.MUL, price_z, rv_corr))
    ranked = g.add_rolling(Op.TS_RANK, raw, 240)
    g.add_unary(Op.NEG, ranked)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0032: Liquidity-Adjusted Volatility Compression (range-based)
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0032")
def build_factor_0032() -> rt.FactorGraph:
    """Mul(TsDiff(Log(close),30),
           Sub(1, TsRank(Div(Div(Sub(high,low),close),
                              SLog1p(Ma(volume,5))),240)))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    h = g.add_input("high")
    lo = g.add_input("low")
    v = g.add_input("volume")
    range_pct = g.add_binary(Op.DIV, g.add_binary(Op.SUB, h, lo), c)
    log_vol = g.add_unary(Op.SLOG1P, g.add_rolling(Op.MA, v, 5))
    rve = g.add_binary(Op.DIV, range_pct, log_vol)
    rve_rank = g.add_rolling(Op.TS_RANK, rve, 240)
    comp_intensity = g.add_scalar_op(Op.SCALAR_SUB, rve_rank, 1.0)
    logc = g.add_unary(Op.LOG, c)
    short_mom = g.add_rolling(Op.TS_DIFF, logc, 30)
    g.add_binary(Op.MUL, short_mom, comp_intensity)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0033: Relative Volatility Efficiency
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0033")
def build_factor_0033() -> rt.FactorGraph:
    """Neg(Mul(Mul(TsRank(Div(Abs(TsDiff(Log(close),30)),TsStd(TsDiff(Log(close),1),30)),720),
                   TsRank(Div(Ma(volume,30),Ma(volume,240)),720)),
              Sign(TsDiff(close,30))))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    v = g.add_input("volume")
    logc = g.add_unary(Op.LOG, c)
    ret1 = g.add_rolling(Op.TS_DIFF, logc, 1)
    disp = g.add_unary(Op.ABS, g.add_rolling(Op.TS_DIFF, logc, 30))
    path_noise = g.add_rolling(Op.TS_STD, ret1, 30)
    efficiency = g.add_binary(Op.DIV, disp, path_noise)
    vol_short = g.add_rolling(Op.MA, v, 30)
    vol_long = g.add_rolling(Op.MA, v, 240)
    rel_vol = g.add_binary(Op.DIV, vol_short, vol_long)
    climax = g.add_binary(Op.MUL, g.add_rolling(Op.TS_RANK, efficiency, 720),
                          g.add_rolling(Op.TS_RANK, rel_vol, 720))
    direction = g.add_unary(Op.SIGN, g.add_rolling(Op.TS_DIFF, c, 30))
    raw = g.add_binary(Op.MUL, climax, direction)
    g.add_unary(Op.NEG, raw)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0034: Liquidity-Adjusted Volatility Compression (corr-based)
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0034")
def build_factor_0034() -> rt.FactorGraph:
    """Neg(Mul(TsZscore(TsDiff(Log(close),60),120),
              Mul(Inv(Div(TsStd(TsDiff(Log(close),1),30),
                          TsStd(TsDiff(Log(close),1),240))),
                  Corr(Abs(TsDiff(Log(close),1)),SLog1p(volume),60))))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    v = g.add_input("volume")
    logc = g.add_unary(Op.LOG, c)
    ret1 = g.add_rolling(Op.TS_DIFF, logc, 1)
    std_short = g.add_rolling(Op.TS_STD, ret1, 30)
    std_long = g.add_rolling(Op.TS_STD, ret1, 240)
    vol_ratio = g.add_binary(Op.DIV, std_short, std_long)
    comp_str = g.add_unary(Op.INV, vol_ratio)
    abs_ret = g.add_unary(Op.ABS, ret1)
    slogv = g.add_unary(Op.SLOG1P, v)
    vol_force = g.add_bivariate(Op.CORR, abs_ret, slogv, 60)
    z_ret = g.add_rolling(Op.TS_ZSCORE, g.add_rolling(Op.TS_DIFF, logc, 60), 120)
    raw = g.add_binary(Op.MUL, z_ret, g.add_binary(Op.MUL, comp_str, vol_force))
    g.add_unary(Op.NEG, raw)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0035: EMA crossover momentum
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0035")
def build_factor_0035() -> rt.FactorGraph:
    """Div(Sub(Ema(close, 10), Ema(close, 60)), TsStd(close, 60))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    ema10 = g.add_rolling(Op.EMA, c, 10)
    ema60 = g.add_rolling(Op.EMA, c, 60)
    diff = g.add_binary(Op.SUB, ema10, ema60)
    vol = g.add_rolling(Op.TS_STD, c, 60)
    g.add_binary(Op.DIV, diff, vol)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0036: Volume surge z-score
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0036")
def build_factor_0036() -> rt.FactorGraph:
    """TsZscore(SLog1p(volume), 120)"""
    g = rt.FactorGraph()
    v = g.add_input("volume")
    sv = g.add_unary(Op.SLOG1P, v)
    g.add_rolling(Op.TS_ZSCORE, sv, 120)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0037: High-low range normalized by MA
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0037")
def build_factor_0037() -> rt.FactorGraph:
    """Neg(TsZscore(Div(Sub(high, low), Ma(close, 30)), 120))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    h = g.add_input("high")
    lo = g.add_input("low")
    rng = g.add_binary(Op.SUB, h, lo)
    ma30 = g.add_rolling(Op.MA, c, 30)
    norm_rng = g.add_binary(Op.DIV, rng, ma30)
    g.add_unary(Op.NEG, g.add_rolling(Op.TS_ZSCORE, norm_rng, 120))
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0038: Return autocorrelation (lag 5)
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0038")
def build_factor_0038() -> rt.FactorGraph:
    """Neg(Autocorr(TsDiff(Log(close), 1), 60, 5))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    logc = g.add_unary(Op.LOG, c)
    ret1 = g.add_rolling(Op.TS_DIFF, logc, 1)
    ac = g.add_autocorr(ret1, 60, 5)
    g.add_unary(Op.NEG, ac)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0039: Volume-weighted return rank
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0039")
def build_factor_0039() -> rt.FactorGraph:
    """Neg(TsRank(Mul(TsDiff(Log(close), 1), SLog1p(volume)), 60))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    v = g.add_input("volume")
    logc = g.add_unary(Op.LOG, c)
    ret1 = g.add_rolling(Op.TS_DIFF, logc, 1)
    sv = g.add_unary(Op.SLOG1P, v)
    vret = g.add_binary(Op.MUL, ret1, sv)
    g.add_unary(Op.NEG, g.add_rolling(Op.TS_RANK, vret, 60))
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0040: Realized variance ratio (short vs long)
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0040")
def build_factor_0040() -> rt.FactorGraph:
    """Neg(Div(TsStd(TsDiff(Log(close), 1), 15), TsStd(TsDiff(Log(close), 1), 120)))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    logc = g.add_unary(Op.LOG, c)
    ret1 = g.add_rolling(Op.TS_DIFF, logc, 1)
    std_short = g.add_rolling(Op.TS_STD, ret1, 15)
    std_long = g.add_rolling(Op.TS_STD, ret1, 120)
    g.add_unary(Op.NEG, g.add_binary(Op.DIV, std_short, std_long))
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0041: Close position within Bollinger Band
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0041")
def build_factor_0041() -> rt.FactorGraph:
    """Div(Sub(close, Ma(close, 60)), TsStd(close, 60))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    ma = g.add_rolling(Op.MA, c, 60)
    dev = g.add_binary(Op.SUB, c, ma)
    std = g.add_rolling(Op.TS_STD, c, 60)
    g.add_binary(Op.DIV, dev, std)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0042: Momentum-volume correlation
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0042")
def build_factor_0042() -> rt.FactorGraph:
    """Neg(Corr(TsDiff(Log(close), 5), SLog1p(volume), 60))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    v = g.add_input("volume")
    logc = g.add_unary(Op.LOG, c)
    ret5 = g.add_rolling(Op.TS_DIFF, logc, 5)
    sv = g.add_unary(Op.SLOG1P, v)
    g.add_unary(Op.NEG, g.add_bivariate(Op.CORR, ret5, sv, 60))
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0043: Close relative to rolling min-max range
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0043")
def build_factor_0043() -> rt.FactorGraph:
    """Sub(Div(Sub(close, TsMin(low, 60)), Sub(TsMax(high, 60), TsMin(low, 60))), 0.5)"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    h = g.add_input("high")
    lo = g.add_input("low")
    rmin = g.add_rolling(Op.TS_MIN, lo, 60)
    rmax = g.add_rolling(Op.TS_MAX, h, 60)
    numer = g.add_binary(Op.SUB, c, rmin)
    denom = g.add_binary(Op.SUB, rmax, rmin)
    pos = g.add_binary(Op.DIV, numer, denom)
    g.add_scalar_op(Op.SUB_SCALAR, pos, 0.5)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0044: Intraday range efficiency
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0044")
def build_factor_0044() -> rt.FactorGraph:
    """TsZscore(Div(Abs(TsDiff(close, 1)), Sub(high, low)), 60)"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    h = g.add_input("high")
    lo = g.add_input("low")
    abs_chg = g.add_unary(Op.ABS, g.add_rolling(Op.TS_DIFF, c, 1))
    rng = g.add_binary(Op.SUB, h, lo)
    eff = g.add_binary(Op.DIV, abs_chg, rng)
    g.add_rolling(Op.TS_ZSCORE, eff, 60)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0045: Cumulative return rank reversal
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0045")
def build_factor_0045() -> rt.FactorGraph:
    """Neg(TsRank(TsDiff(Log(close), 30), 120))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    logc = g.add_unary(Op.LOG, c)
    ret30 = g.add_rolling(Op.TS_DIFF, logc, 30)
    g.add_unary(Op.NEG, g.add_rolling(Op.TS_RANK, ret30, 120))
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0046: Volume-adjusted price deviation (EMA)
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0046")
def build_factor_0046() -> rt.FactorGraph:
    """Mul(Div(Sub(close, Ema(close, 30)), Ema(close, 30)), TsRank(volume, 30))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    v = g.add_input("volume")
    ema30 = g.add_rolling(Op.EMA, c, 30)
    dev = g.add_binary(Op.DIV, g.add_binary(Op.SUB, c, ema30), ema30)
    vrank = g.add_rolling(Op.TS_RANK, v, 30)
    g.add_binary(Op.MUL, dev, vrank)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0047: Return dispersion ratio
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0047")
def build_factor_0047() -> rt.FactorGraph:
    """Neg(Div(TsSum(Abs(TsDiff(Log(close), 1)), 30), Abs(TsDiff(Log(close), 30))))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    logc = g.add_unary(Op.LOG, c)
    ret1 = g.add_rolling(Op.TS_DIFF, logc, 1)
    abs_ret1 = g.add_unary(Op.ABS, ret1)
    path_len = g.add_rolling(Op.TS_SUM, abs_ret1, 30)
    net_move = g.add_unary(Op.ABS, g.add_rolling(Op.TS_DIFF, logc, 30))
    g.add_unary(Op.NEG, g.add_binary(Op.DIV, path_len, net_move))
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0048: Tail ratio (max drawdown vs max rally)
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0048")
def build_factor_0048() -> rt.FactorGraph:
    """Div(TsMin(TsDiff(Log(close), 1), 60), Neg(TsMax(TsDiff(Log(close), 1), 60)))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    logc = g.add_unary(Op.LOG, c)
    ret1 = g.add_rolling(Op.TS_DIFF, logc, 1)
    worst = g.add_rolling(Op.TS_MIN, ret1, 60)
    best = g.add_rolling(Op.TS_MAX, ret1, 60)
    neg_best = g.add_unary(Op.NEG, best)
    g.add_binary(Op.DIV, worst, neg_best)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0049: Mean reversion intensity (Hurst proxy)
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0049")
def build_factor_0049() -> rt.FactorGraph:
    """Sub(Div(TsStd(TsDiff(close, 10), 30), Mul(TsStd(TsDiff(close, 1), 30), 3.16)), 1.0)"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    diff10 = g.add_rolling(Op.TS_DIFF, c, 10)
    diff1 = g.add_rolling(Op.TS_DIFF, c, 1)
    std10 = g.add_rolling(Op.TS_STD, diff10, 30)
    std1 = g.add_rolling(Op.TS_STD, diff1, 30)
    scaled_std1 = g.add_scalar_op(Op.MUL_SCALAR, std1, 3.162)
    ratio = g.add_binary(Op.DIV, std10, scaled_std1)
    g.add_scalar_op(Op.SUB_SCALAR, ratio, 1.0)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0051: Volume-price rank divergence
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0051")
def build_factor_0051() -> rt.FactorGraph:
    """Sub(TsRank(close, 60), TsRank(volume, 60))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    v = g.add_input("volume")
    cr = g.add_rolling(Op.TS_RANK, c, 60)
    vr = g.add_rolling(Op.TS_RANK, v, 60)
    g.add_binary(Op.SUB, cr, vr)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0052: Smoothed return z-score (EMA-based)
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0052")
def build_factor_0052() -> rt.FactorGraph:
    """TsZscore(Ema(TsDiff(Log(close), 1), 10), 120)"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    logc = g.add_unary(Op.LOG, c)
    ret1 = g.add_rolling(Op.TS_DIFF, logc, 1)
    smooth = g.add_rolling(Op.EMA, ret1, 10)
    g.add_rolling(Op.TS_ZSCORE, smooth, 120)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0053: Volatility of volume
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0053")
def build_factor_0053() -> rt.FactorGraph:
    """Neg(TsZscore(TsStd(SLog1p(volume), 30), 120))"""
    g = rt.FactorGraph()
    v = g.add_input("volume")
    sv = g.add_unary(Op.SLOG1P, v)
    vstd = g.add_rolling(Op.TS_STD, sv, 30)
    g.add_unary(Op.NEG, g.add_rolling(Op.TS_ZSCORE, vstd, 120))
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0054: High breakout momentum
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0054")
def build_factor_0054() -> rt.FactorGraph:
    """Div(Sub(close, TsMax(high, 30)), TsStd(close, 30))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    h = g.add_input("high")
    rmax = g.add_rolling(Op.TS_MAX, h, 30)
    dist = g.add_binary(Op.SUB, c, rmax)
    vol = g.add_rolling(Op.TS_STD, c, 30)
    g.add_binary(Op.DIV, dist, vol)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0055: Short-term reversal with volume filter
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0055")
def build_factor_0055() -> rt.FactorGraph:
    """Neg(Mul(TsDiff(Log(close), 5), Div(Ma(volume, 5), Ma(volume, 60))))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    v = g.add_input("volume")
    logc = g.add_unary(Op.LOG, c)
    ret5 = g.add_rolling(Op.TS_DIFF, logc, 5)
    vshort = g.add_rolling(Op.MA, v, 5)
    vlong = g.add_rolling(Op.MA, v, 60)
    vratio = g.add_binary(Op.DIV, vshort, vlong)
    g.add_unary(Op.NEG, g.add_binary(Op.MUL, ret5, vratio))
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0056: Close-to-low ratio ranked
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0056")
def build_factor_0056() -> rt.FactorGraph:
    """Neg(TsRank(Div(Sub(close, low), Sub(high, low)), 60))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    h = g.add_input("high")
    lo = g.add_input("low")
    numer = g.add_binary(Op.SUB, c, lo)
    denom = g.add_binary(Op.SUB, h, lo)
    ratio = g.add_binary(Op.DIV, numer, denom)
    g.add_unary(Op.NEG, g.add_rolling(Op.TS_RANK, ratio, 60))
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0057: Delayed momentum divergence
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0057")
def build_factor_0057() -> rt.FactorGraph:
    """Sub(TsDiff(Log(close), 5), Delay(TsDiff(Log(close), 5), 10))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    logc = g.add_unary(Op.LOG, c)
    ret5 = g.add_rolling(Op.TS_DIFF, logc, 5)
    ret5_lag = g.add_rolling(Op.DELAY, ret5, 10)
    g.add_binary(Op.SUB, ret5, ret5_lag)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0058: Abs return rank correlation with volume
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0058")
def build_factor_0058() -> rt.FactorGraph:
    """Neg(Corr(TsRank(Abs(TsDiff(Log(close), 1)), 30), TsRank(volume, 30), 60))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    v = g.add_input("volume")
    logc = g.add_unary(Op.LOG, c)
    ret1 = g.add_rolling(Op.TS_DIFF, logc, 1)
    abs_ret = g.add_unary(Op.ABS, ret1)
    rr = g.add_rolling(Op.TS_RANK, abs_ret, 30)
    vr = g.add_rolling(Op.TS_RANK, v, 30)
    g.add_unary(Op.NEG, g.add_bivariate(Op.CORR, rr, vr, 60))
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0059: Relative strength (EMA 10 vs EMA 120)
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0059")
def build_factor_0059() -> rt.FactorGraph:
    """TsZscore(Div(Ema(close, 10), Ema(close, 120)), 60)"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    ema10 = g.add_rolling(Op.EMA, c, 10)
    ema120 = g.add_rolling(Op.EMA, c, 120)
    ratio = g.add_binary(Op.DIV, ema10, ema120)
    g.add_rolling(Op.TS_ZSCORE, ratio, 60)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0060: Signed volume impulse
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0060")
def build_factor_0060() -> rt.FactorGraph:
    """TsZscore(Mul(Sign(TsDiff(close, 1)), SLog1p(volume)), 60)"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    v = g.add_input("volume")
    sign_chg = g.add_unary(Op.SIGN, g.add_rolling(Op.TS_DIFF, c, 1))
    sv = g.add_unary(Op.SLOG1P, v)
    signed_vol = g.add_binary(Op.MUL, sign_chg, sv)
    g.add_rolling(Op.TS_ZSCORE, signed_vol, 60)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0061: Rolling Sharpe ratio
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0061")
def build_factor_0061() -> rt.FactorGraph:
    """Div(Ma(TsDiff(Log(close), 1), 30), TsStd(TsDiff(Log(close), 1), 30))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    logc = g.add_unary(Op.LOG, c)
    ret1 = g.add_rolling(Op.TS_DIFF, logc, 1)
    mean_ret = g.add_rolling(Op.MA, ret1, 30)
    vol_ret = g.add_rolling(Op.TS_STD, ret1, 30)
    g.add_binary(Op.DIV, mean_ret, vol_ret)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0062: Volatility compression rank
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0062")
def build_factor_0062() -> rt.FactorGraph:
    """Neg(TsRank(TsStd(TsDiff(Log(close), 1), 15), 120))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    logc = g.add_unary(Op.LOG, c)
    ret1 = g.add_rolling(Op.TS_DIFF, logc, 1)
    rvol = g.add_rolling(Op.TS_STD, ret1, 15)
    g.add_unary(Op.NEG, g.add_rolling(Op.TS_RANK, rvol, 120))
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0063: Momentum acceleration
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0063")
def build_factor_0063() -> rt.FactorGraph:
    """TsDiff(TsDiff(Log(close), 10), 10)"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    logc = g.add_unary(Op.LOG, c)
    mom10 = g.add_rolling(Op.TS_DIFF, logc, 10)
    g.add_rolling(Op.TS_DIFF, mom10, 10)
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0064: Volume trend vs price trend
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0064")
def build_factor_0064() -> rt.FactorGraph:
    """Neg(Corr(Ma(close, 10), Ma(volume, 10), 120))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    v = g.add_input("volume")
    ma_c = g.add_rolling(Op.MA, c, 10)
    ma_v = g.add_rolling(Op.MA, v, 10)
    g.add_unary(Op.NEG, g.add_bivariate(Op.CORR, ma_c, ma_v, 120))
    g.compile()
    return g


# ═══════════════════════════════════════════════════════════════
#  0065: Range-weighted return
# ═══════════════════════════════════════════════════════════════

@register_factor("okx_perp", "0065")
def build_factor_0065() -> rt.FactorGraph:
    """Neg(TsZscore(Mul(TsDiff(Log(close), 1), Div(Sub(high, low), close)), 60))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    h = g.add_input("high")
    lo = g.add_input("low")
    logc = g.add_unary(Op.LOG, c)
    ret1 = g.add_rolling(Op.TS_DIFF, logc, 1)
    rng = g.add_binary(Op.DIV, g.add_binary(Op.SUB, h, lo), c)
    weighted = g.add_binary(Op.MUL, ret1, rng)
    g.add_unary(Op.NEG, g.add_rolling(Op.TS_ZSCORE, weighted, 60))
    g.compile()
    return g
