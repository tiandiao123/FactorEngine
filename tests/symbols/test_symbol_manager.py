from __future__ import annotations

import pytest

from dataflow.symbols import (
    LocalTextSymbolSource,
    StaticSymbolSource,
    SymbolManager,
    SymbolProvider,
    SymbolUniverse,
)
from dataflow.symbols.filters import AllowListFilter, DenyListFilter


def test_symbol_manager_stages_and_commits_diff():
    manager = SymbolManager(["BTC-USDT", "ETH-USDT"], exchange="binance")

    diff = manager.stage_universe(
        SymbolUniverse(
            version="v2",
            exchange="binance",
            symbols=("BTC-USDT", "SOL-USDT"),
        )
    )

    assert diff is not None
    assert diff.added == frozenset({"SOL-USDT"})
    assert diff.removed == frozenset({"ETH-USDT"})
    assert diff.unchanged == frozenset({"BTC-USDT"})
    assert manager.active_symbols == frozenset({"BTC-USDT", "ETH-USDT"})

    committed = manager.commit_pending()
    assert committed == diff
    assert manager.active_symbols == frozenset({"BTC-USDT", "SOL-USDT"})
    assert manager.version == "v2"
    assert manager.commit_pending() is None


def test_symbol_manager_rejects_old_version_and_empty_universe():
    manager = SymbolManager(["BTC-USDT"], exchange="binance", version="v2")

    with pytest.raises(ValueError, match="must be newer"):
        manager.stage_universe(
            SymbolUniverse(version="v1", exchange="binance", symbols=("ETH-USDT",))
        )

    with pytest.raises(ValueError, match="Empty"):
        manager.stage_universe(
            SymbolUniverse(version="v3", exchange="binance", symbols=())
        )


def test_local_text_source_reads_comments_dedupes_and_provider_filters(tmp_path):
    path = tmp_path / "symbols.txt"
    path.write_text(
        "# comment\nBTC-USDT\nETH-USDT\n\nSOL-USDT\nBTC-USDT\n",
        encoding="utf-8",
    )

    source = LocalTextSymbolSource(path, exchange="binance", version="v1")
    universe = source.fetch()
    assert universe.symbols == ("BTC-USDT", "ETH-USDT", "SOL-USDT")

    provider = SymbolProvider(
        StaticSymbolSource(
            ["BTC-USDT", "ETH-USDT", "SOL-USDT"],
            exchange="binance",
            version="v2",
        ),
        filters=[
            AllowListFilter({"BTC-USDT", "SOL-USDT"}),
            DenyListFilter({"SOL-USDT"}),
        ],
    )
    filtered = provider.fetch()
    assert filtered.symbols == ("BTC-USDT",)
    assert filtered.metadata["filters"] == ["allow_list", "deny_list"]
