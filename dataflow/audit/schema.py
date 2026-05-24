"""Structured records written by the audit logger."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(slots=True)
class AggregateBarEvent:
    """One online aggregate bar produced by ClockDriver for one symbol."""

    bucket_ts_ms: int
    flush_ts_ms: int
    exchange: str
    data_source: str
    freq_seconds: float
    symbol: str
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float | None
    vwap: float | None
    buy_vol: float | None
    sell_vol: float | None
    trade_count: int
    source: str = "online_clockdriver"

    def to_dict(self) -> dict:
        return asdict(self)
