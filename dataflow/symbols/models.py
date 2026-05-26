"""Data models for dynamic symbol universes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class SymbolRecord:
    """Instrument metadata used by source/filter pipelines."""

    symbol: str
    exchange: str
    quote_asset: str | None = None
    contract_type: str | None = None
    status: str | None = None
    quote_volume: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SymbolUniverse:
    """A full target symbol universe from a source/provider."""

    version: str
    exchange: str
    symbols: tuple[str, ...]
    ttl_seconds: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    allow_empty: bool = False


@dataclass(frozen=True, slots=True)
class SymbolDiff:
    """Diff between current active symbols and a target universe."""

    version: str
    added: frozenset[str]
    removed: frozenset[str]
    unchanged: frozenset[str]
    target: frozenset[str]
    reason: str | None = None

    @property
    def is_empty(self) -> bool:
        return not self.added and not self.removed
