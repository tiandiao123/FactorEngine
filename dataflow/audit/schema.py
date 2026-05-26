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
    event_type: str = "aggregate_bar"
    symbol_version: str | None = None
    active_symbols_count: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class SymbolDiffEvent:
    """One committed dynamic symbol universe change."""

    commit_ts_ms: int
    effective_from_ts_ms: int | None
    exchange: str
    data_source: str
    symbol_version: str
    added: list[str]
    removed: list[str]
    unchanged_count: int
    target_count: int
    reason: str | None = None
    source: str = "engine_symbol_manager"
    event_type: str = "symbol_diff"

    def to_dict(self) -> dict:
        return asdict(self)
