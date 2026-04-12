"""Synthetic data generators for simulation mode."""

from __future__ import annotations

import numpy as np


class BarGenerator:
    """Generate synthetic OHLCV bars with geometric-random-walk midprice."""

    def __init__(
        self,
        base_price: float = 3000.0,
        volatility: float = 0.001,
        base_volume: float = 100.0,
        seed: int | None = None,
    ):
        self._rng = np.random.default_rng(seed)
        self._last_close = base_price
        self._volatility = volatility
        self._base_volume = base_volume

    def next_bar(self, ts_ms: int) -> np.ndarray:
        """Return a shape-(8,) float64 bar: [ts, o, h, l, c, vol, vol_ccy, vol_ccy_quote]."""
        sigma = self._volatility
        rng = self._rng

        open_ = self._last_close
        close = open_ * np.exp(rng.normal(0, sigma))
        high = max(open_, close) * (1 + abs(rng.normal(0, sigma / 2)))
        low = min(open_, close) * (1 - abs(rng.normal(0, sigma / 2)))
        vol = self._base_volume * (1 + abs(rng.normal(0, 0.3)))
        vol_ccy = vol * close
        vol_ccy_quote = vol_ccy

        self._last_close = close
        return np.array(
            [ts_ms, open_, high, low, close, vol, vol_ccy, vol_ccy_quote],
            dtype=np.float64,
        )
