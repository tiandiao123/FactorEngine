"""Array schema constants for the dataflow layer.

This module intentionally avoids event classes. It only defines the canonical
column layout used by factorengine-facing numpy arrays.
"""

from __future__ import annotations

BAR_COLUMNS = ("ts", "open", "high", "low", "close", "vol")
BAR_NUM_FIELDS = len(BAR_COLUMNS)

TRADE_COLUMNS = ("px", "sz", "side")
TRADE_NUM_FIELDS = len(TRADE_COLUMNS)
TRADE_PX_COL = 0
TRADE_SZ_COL = 1
TRADE_SIDE_COL = 2
TRADE_SIDE_BUY = 1.0
TRADE_SIDE_SELL = -1.0

BOOK_LEVELS = 5
BOOK_COLUMNS = (
    "bid_px1", "bid_px2", "bid_px3", "bid_px4", "bid_px5",
    "bid_sz1", "bid_sz2", "bid_sz3", "bid_sz4", "bid_sz5",
    "ask_px1", "ask_px2", "ask_px3", "ask_px4", "ask_px5",
    "ask_sz1", "ask_sz2", "ask_sz3", "ask_sz4", "ask_sz5",
)
BOOK_NUM_FIELDS = len(BOOK_COLUMNS)
BID_PX_SLICE = slice(0, 5)
BID_SZ_SLICE = slice(5, 10)
ASK_PX_SLICE = slice(10, 15)
ASK_SZ_SLICE = slice(15, 20)


def encode_trade_side(side: str) -> float:
    """Encode OKX trade side as numeric direction for factor computation."""
    return TRADE_SIDE_BUY if side == "buy" else TRADE_SIDE_SELL
