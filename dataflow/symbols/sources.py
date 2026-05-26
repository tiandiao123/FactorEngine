"""Symbol sources and providers for dynamic universes."""

from __future__ import annotations

import time
import urllib.request
from pathlib import Path
from typing import Callable, Protocol
import json

from dataflow.binance.common import BINANCE_FSTREAM_REST, from_binance_symbol
from dataflow.okx.common import OKX_REST_INSTRUMENTS

from .models import SymbolRecord, SymbolUniverse


class SymbolSource(Protocol):
    """Source of a full target universe."""

    def fetch(self) -> SymbolUniverse:
        ...


class SymbolFilter(Protocol):
    """Filter applied to a fetched symbol universe."""

    def apply(self, universe: SymbolUniverse) -> SymbolUniverse:
        ...


class StaticSymbolSource:
    """In-memory source, mainly for tests and backward compatibility."""

    def __init__(
        self,
        symbols: list[str] | tuple[str, ...],
        *,
        exchange: str,
        version: str = "static",
        metadata: dict | None = None,
    ):
        self._symbols = tuple(symbols)
        self._exchange = exchange
        self._version = version
        self._metadata = metadata or {"source": "static"}

    def fetch(self) -> SymbolUniverse:
        return SymbolUniverse(
            version=self._version,
            exchange=self._exchange,
            symbols=self._symbols,
            metadata=dict(self._metadata),
        )


class LocalTextSymbolSource:
    """Read one symbol per line from a local text file."""

    def __init__(
        self,
        path: str | Path,
        *,
        exchange: str,
        version: str | None = None,
        comment_prefix: str = "#",
    ):
        self._path = Path(path)
        self._exchange = exchange
        self._version = version
        self._comment_prefix = comment_prefix

    def fetch(self) -> SymbolUniverse:
        symbols: list[str] = []
        seen: set[str] = set()
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                sym = line.strip()
                if not sym or sym.startswith(self._comment_prefix):
                    continue
                sym = sym.upper()
                if sym not in seen:
                    seen.add(sym)
                    symbols.append(sym)

        version = self._version
        if version is None:
            stat = self._path.stat()
            version = f"file:{self._path}:{int(stat.st_mtime_ns)}"

        return SymbolUniverse(
            version=version,
            exchange=self._exchange,
            symbols=tuple(symbols),
            metadata={"source": "local_text", "path": str(self._path)},
        )


JsonFetcher = Callable[[str], dict | list]


class BinanceExchangeSymbolSource:
    """Fetch Binance Futures instruments and 24h quote volumes."""

    def __init__(
        self,
        *,
        rest_base: str = BINANCE_FSTREAM_REST,
        fetch_json: JsonFetcher | None = None,
        version: str | None = None,
    ):
        self._rest_base = rest_base.rstrip("/")
        self._fetch_json = fetch_json or _fetch_json
        self._version = version

    def fetch(self) -> SymbolUniverse:
        exchange_info = self._fetch_json(f"{self._rest_base}/fapi/v1/exchangeInfo")
        tickers = self._fetch_json(f"{self._rest_base}/fapi/v1/ticker/24hr")
        ticker_by_symbol = {
            str(ticker.get("symbol", "")).upper(): ticker
            for ticker in tickers
            if isinstance(ticker, dict)
        }

        records: dict[str, SymbolRecord] = {}
        symbols: list[str] = []
        for info in exchange_info.get("symbols", []):
            raw_symbol = str(info.get("symbol", "")).upper()
            if not raw_symbol:
                continue
            ticker = ticker_by_symbol.get(raw_symbol, {})
            unified = from_binance_symbol(raw_symbol)
            quote_volume = _to_float(ticker.get("quoteVolume"))
            record = SymbolRecord(
                symbol=unified,
                exchange="binance",
                quote_asset=info.get("quoteAsset"),
                contract_type=info.get("contractType"),
                status=info.get("status"),
                quote_volume=quote_volume,
                raw={"exchange_info": info, "ticker_24hr": ticker},
            )
            records[unified] = record
            symbols.append(unified)

        version = self._version or f"binance:{int(time.time() * 1000)}"
        return SymbolUniverse(
            version=version,
            exchange="binance",
            symbols=tuple(sorted(set(symbols))),
            metadata={"source": "binance_exchange", "records": records},
        )


class OkxExchangeSymbolSource:
    """Fetch OKX swap instruments and optional ticker metadata."""

    def __init__(
        self,
        *,
        instruments_url: str = OKX_REST_INSTRUMENTS,
        fetch_json: JsonFetcher | None = None,
        version: str | None = None,
    ):
        self._instruments_url = instruments_url
        self._fetch_json = fetch_json or _fetch_json
        self._version = version

    def fetch(self) -> SymbolUniverse:
        body = self._fetch_json(self._instruments_url)
        if body.get("code") not in (None, "0", 0):
            raise RuntimeError(f"OKX instruments request failed: {body}")

        records: dict[str, SymbolRecord] = {}
        symbols: list[str] = []
        for inst in body.get("data", []):
            symbol = inst.get("instId")
            if not symbol:
                continue
            record = SymbolRecord(
                symbol=symbol,
                exchange="okx",
                quote_asset=inst.get("settleCcy") or inst.get("quoteCcy"),
                contract_type=inst.get("instType"),
                status=inst.get("state"),
                raw=inst,
            )
            records[symbol] = record
            symbols.append(symbol)

        version = self._version or f"okx:{int(time.time() * 1000)}"
        return SymbolUniverse(
            version=version,
            exchange="okx",
            symbols=tuple(sorted(set(symbols))),
            metadata={"source": "okx_exchange", "records": records},
        )


class SymbolProvider:
    """Compose a source and filters into a final SymbolUniverse."""

    def __init__(
        self,
        source: SymbolSource,
        filters: list[SymbolFilter] | None = None,
        *,
        ttl_seconds: int | None = None,
        refresh_interval_seconds: float | None = None,
    ):
        self.source = source
        self.filters = list(filters or [])
        self.ttl_seconds = ttl_seconds
        self.refresh_interval_seconds = refresh_interval_seconds
        self.provider_errors = 0
        self.last_fetch_ts_ms = 0

    def fetch(self) -> SymbolUniverse:
        try:
            universe = self.source.fetch()
            for symbol_filter in self.filters:
                universe = symbol_filter.apply(universe)
        except Exception:
            self.provider_errors += 1
            raise

        self.last_fetch_ts_ms = int(time.time() * 1000)
        if self.ttl_seconds is None:
            return universe
        return SymbolUniverse(
            version=universe.version,
            exchange=universe.exchange,
            symbols=universe.symbols,
            ttl_seconds=self.ttl_seconds,
            metadata=dict(universe.metadata),
            allow_empty=universe.allow_empty,
        )


def _fetch_json(url: str) -> dict | list:
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.load(response)


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
