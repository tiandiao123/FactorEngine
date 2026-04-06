"""Internal event models for the dataflow layer."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class MarketEvent:
    """Common metadata shared by all internal market events."""

    symbol: str
    channel: str
    ts_event: int
    ts_recv: int


@dataclass(slots=True)
class BarEvent(MarketEvent):
    """Normalized bar event used by bar caches and factor runtime."""

    open: float
    high: float
    low: float
    close: float
    vol: float


@dataclass(slots=True)
class TradeEvent(MarketEvent):
    """Normalized trade event for trade-level collectors."""

    trade_id: str | None
    px: float
    sz: float
    side: str
    count: int = 1
    is_aggregated: bool = False


@dataclass(slots=True)
class BookLevel:
    """One price level in a shallow order-book snapshot."""

    px: float
    sz: float
    orders: int | None = None


@dataclass(slots=True)
class BookEvent(MarketEvent):
    """Normalized shallow order-book snapshot."""

    best_bid_px: float
    best_bid_sz: float
    best_ask_px: float
    best_ask_sz: float
    bids: list[BookLevel] = field(default_factory=list)
    asks: list[BookLevel] = field(default_factory=list)

