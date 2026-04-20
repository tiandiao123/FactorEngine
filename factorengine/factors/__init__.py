"""
Factor Registry — auto-discovers and registers factor builder functions.

Factor builders live in platform-specific subpackages:
    factorengine/factors/
    ├── okx_perp/          # OKX perpetual swap factors
    ├── binance_perp/      # (future) Binance perpetual factors
    └── stock_cn/          # (future) A-share stock factors

Usage:
    from factorengine.factors import FactorRegistry

    reg = FactorRegistry()
    reg.load_all()                          # load all platforms
    reg.load_group("okx_perp")              # or just one platform

    graphs = reg.build_group("okx_perp")    # {factor_id: FactorGraph}
    g = reg.build("0001", group="okx_perp") # single factor
"""

from .registry import FactorRegistry, register_factor

__all__ = ["FactorRegistry", "register_factor"]
