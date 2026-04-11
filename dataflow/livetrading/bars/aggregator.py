"""Bar aggregation helpers."""

from __future__ import annotations

import numpy as np



class BarAggregator:
    """Accumulate confirmed 1s candles and emit an aggregated N-second bar."""

    def __init__(self, agg_seconds: int = 5):
        self.agg_seconds = agg_seconds
        self._buf: list[np.ndarray] = []

    @staticmethod
    def parse_bar(record: dict) -> np.ndarray | None:
        """Parse one confirmed OKX candle record into a bar array.

        Used by direct-channel mode (>= 1min) where no aggregation is needed.
        Returns None if the candle is not yet confirmed.
        """
        raw = record["raw"]
        if raw[-1] != "1":
            return None
        return np.array(
            [
                int(raw[0]),
                float(raw[1]),
                float(raw[2]),
                float(raw[3]),
                float(raw[4]),
                float(raw[5]),
                float(raw[6]),
                float(raw[7]),
            ],
            dtype=np.float64,
        )

    def on_candle1s(self, record: dict) -> np.ndarray | None:
        """Feed one OKX candle1s record and return an aggregated bar if complete."""
        raw = record["raw"]
        if raw[-1] != "1":
            return None

        partial = np.array(
            [
                int(raw[0]),
                float(raw[1]),
                float(raw[2]),
                float(raw[3]),
                float(raw[4]),
                float(raw[5]),
                float(raw[6]),
                float(raw[7]),
            ],
            dtype=np.float64,
        )
        self._buf.append(partial)
        if len(self._buf) < self.agg_seconds:
            return None

        bar = self._merge()
        self._buf.clear()
        return bar

    def _merge(self) -> np.ndarray:
        buf = self._buf
        return np.array(
            [
                buf[0][0],
                buf[0][1],
                max(bar[2] for bar in buf),
                min(bar[3] for bar in buf),
                buf[-1][4],
                sum(bar[5] for bar in buf),
                sum(bar[6] for bar in buf),
                sum(bar[7] for bar in buf),
            ],
            dtype=np.float64,
        )
