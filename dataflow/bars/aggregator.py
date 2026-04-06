"""Bar aggregation helpers."""

from __future__ import annotations

from dataclasses import dataclass

from ..events import BarEvent


@dataclass(slots=True)
class _PartialBar:
    ts_event: int
    ts_recv: int
    open: float
    high: float
    low: float
    close: float
    vol: float


class BarAggregator:
    """Accumulate confirmed 1s candles and emit an aggregated N-second bar."""

    def __init__(self, agg_seconds: int = 5):
        self.agg_seconds = agg_seconds
        self._buf: list[_PartialBar] = []

    def on_candle1s(self, record: dict) -> BarEvent | None:
        """Feed one OKX candle1s record and return an aggregated bar if complete."""
        raw = record["raw"]
        if raw[-1] != "1":
            return None

        partial = _PartialBar(
            ts_event=int(raw[0]),
            ts_recv=int(record.get("ts_recv", raw[0])),
            open=float(raw[1]),
            high=float(raw[2]),
            low=float(raw[3]),
            close=float(raw[4]),
            vol=float(raw[5]),
        )
        self._buf.append(partial)
        if len(self._buf) < self.agg_seconds:
            return None

        bar = self._merge(symbol=record.get("instId", ""))
        self._buf.clear()
        return bar

    def _merge(self, symbol: str) -> BarEvent:
        buf = self._buf
        return BarEvent(
            symbol=symbol,
            channel=f"bar_{self.agg_seconds}s",
            ts_event=buf[0].ts_event,
            ts_recv=buf[-1].ts_recv,
            open=buf[0].open,
            high=max(bar.high for bar in buf),
            low=min(bar.low for bar in buf),
            close=buf[-1].close,
            vol=sum(bar.vol for bar in buf),
        )

