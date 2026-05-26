"""Composable filters for symbol universes."""

from __future__ import annotations

from pathlib import Path

from .models import SymbolUniverse


class AllowListFilter:
    """Keep only symbols present in an allow-list."""

    def __init__(self, symbols: set[str] | list[str] | tuple[str, ...]):
        self._symbols = {s.upper() for s in symbols}

    @classmethod
    def from_txt(cls, path: str | Path) -> "AllowListFilter":
        symbols: list[str] = []
        with Path(path).open("r", encoding="utf-8") as f:
            for line in f:
                sym = line.strip()
                if not sym or sym.startswith("#"):
                    continue
                symbols.append(sym)
        return cls(symbols)

    def apply(self, universe: SymbolUniverse) -> SymbolUniverse:
        return _replace_symbols(
            universe,
            tuple(sym for sym in universe.symbols if sym.upper() in self._symbols),
            filter_name="allow_list",
        )


class DenyListFilter:
    """Drop symbols present in a deny-list."""

    def __init__(self, symbols: set[str] | list[str] | tuple[str, ...]):
        self._symbols = {s.upper() for s in symbols}

    def apply(self, universe: SymbolUniverse) -> SymbolUniverse:
        return _replace_symbols(
            universe,
            tuple(sym for sym in universe.symbols if sym.upper() not in self._symbols),
            filter_name="deny_list",
        )


class QuoteAssetFilter:
    """Keep records with a matching quote asset."""

    def __init__(self, quote_asset: str):
        self._quote_asset = quote_asset.upper()

    def apply(self, universe: SymbolUniverse) -> SymbolUniverse:
        records = _records_by_symbol(universe)
        keep = tuple(
            sym for sym in universe.symbols
            if (records.get(sym) is None or records[sym].quote_asset == self._quote_asset)
        )
        return _replace_symbols(universe, keep, filter_name="quote_asset")


class ContractTypeFilter:
    """Keep records with a matching contract type."""

    def __init__(self, contract_type: str):
        self._contract_type = contract_type.upper()

    def apply(self, universe: SymbolUniverse) -> SymbolUniverse:
        records = _records_by_symbol(universe)
        keep = tuple(
            sym for sym in universe.symbols
            if (records.get(sym) is None or records[sym].contract_type == self._contract_type)
        )
        return _replace_symbols(universe, keep, filter_name="contract_type")


class TradableStatusFilter:
    """Keep records marked as actively tradable."""

    _TRADABLE = {"TRADING", "LIVE"}

    def apply(self, universe: SymbolUniverse) -> SymbolUniverse:
        records = _records_by_symbol(universe)
        keep = tuple(
            sym for sym in universe.symbols
            if (records.get(sym) is None or (records[sym].status or "").upper() in self._TRADABLE)
        )
        return _replace_symbols(universe, keep, filter_name="tradable_status")


class TopQuoteVolumeFilter:
    """Keep top-N symbols by 24h quote volume descending."""

    def __init__(self, top_n: int):
        if top_n <= 0:
            raise ValueError("top_n must be positive")
        self._top_n = top_n

    def apply(self, universe: SymbolUniverse) -> SymbolUniverse:
        records = _records_by_symbol(universe)
        ranked = sorted(
            universe.symbols,
            key=lambda sym: (
                records[sym].quote_volume if sym in records and records[sym].quote_volume is not None else -1.0,
                sym,
            ),
            reverse=True,
        )
        return _replace_symbols(
            universe,
            tuple(ranked[: self._top_n]),
            filter_name="top_quote_volume",
        )


def _replace_symbols(
    universe: SymbolUniverse,
    symbols: tuple[str, ...],
    *,
    filter_name: str,
) -> SymbolUniverse:
    metadata = dict(universe.metadata)
    records = metadata.get("records")
    if isinstance(records, dict):
        metadata["records"] = {
            sym: record for sym, record in records.items() if sym in set(symbols)
        }
    filters = list(metadata.get("filters", []))
    filters.append(filter_name)
    metadata["filters"] = filters
    return SymbolUniverse(
        version=universe.version,
        exchange=universe.exchange,
        symbols=symbols,
        ttl_seconds=universe.ttl_seconds,
        metadata=metadata,
        allow_empty=universe.allow_empty,
    )


def _records_by_symbol(universe: SymbolUniverse) -> dict:
    records = universe.metadata.get("records")
    return records if isinstance(records, dict) else {}
