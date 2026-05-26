from __future__ import annotations

from dataflow.symbols import (
    BinanceExchangeSymbolSource,
    OkxExchangeSymbolSource,
    SymbolProvider,
)
from dataflow.symbols.filters import (
    AllowListFilter,
    ContractTypeFilter,
    QuoteAssetFilter,
    TopQuoteVolumeFilter,
    TradableStatusFilter,
)


def test_binance_source_and_top_volume_filter_with_allowlist(tmp_path):
    allowlist = tmp_path / "allow.txt"
    allowlist.write_text("BTC-USDT\nSOL-USDT\n", encoding="utf-8")

    def fetch_json(url: str):
        if url.endswith("/exchangeInfo"):
            return {
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "baseAsset": "BTC",
                        "quoteAsset": "USDT",
                        "contractType": "PERPETUAL",
                        "status": "TRADING",
                    },
                    {
                        "symbol": "ETHUSDT",
                        "baseAsset": "ETH",
                        "quoteAsset": "USDT",
                        "contractType": "PERPETUAL",
                        "status": "TRADING",
                    },
                    {
                        "symbol": "SOLUSDT",
                        "baseAsset": "SOL",
                        "quoteAsset": "USDT",
                        "contractType": "PERPETUAL",
                        "status": "TRADING",
                    },
                    {
                        "symbol": "OLDUSDT",
                        "baseAsset": "OLD",
                        "quoteAsset": "USDT",
                        "contractType": "PERPETUAL",
                        "status": "BREAK",
                    },
                ]
            }
        if url.endswith("/ticker/24hr"):
            return [
                {"symbol": "BTCUSDT", "quoteVolume": "100"},
                {"symbol": "ETHUSDT", "quoteVolume": "1000"},
                {"symbol": "SOLUSDT", "quoteVolume": "500"},
                {"symbol": "OLDUSDT", "quoteVolume": "9999"},
            ]
        raise AssertionError(f"unexpected url: {url}")

    provider = SymbolProvider(
        BinanceExchangeSymbolSource(fetch_json=fetch_json, version="v1"),
        filters=[
            QuoteAssetFilter("USDT"),
            ContractTypeFilter("PERPETUAL"),
            TradableStatusFilter(),
            AllowListFilter.from_txt(allowlist),
            TopQuoteVolumeFilter(top_n=1),
        ],
    )

    universe = provider.fetch()
    assert universe.exchange == "binance"
    assert universe.symbols == ("SOL-USDT",)
    assert universe.metadata["filters"] == [
        "quote_asset",
        "contract_type",
        "tradable_status",
        "allow_list",
        "top_quote_volume",
    ]
    assert set(universe.metadata["records"]) == {"SOL-USDT"}


def test_okx_source_preserves_live_swap_records():
    def fetch_json(_url: str):
        return {
            "code": "0",
            "data": [
                {
                    "instId": "BTC-USDT-SWAP",
                    "instType": "SWAP",
                    "settleCcy": "USDT",
                    "state": "live",
                },
                {
                    "instId": "ETH-USDT-SWAP",
                    "instType": "SWAP",
                    "settleCcy": "USDT",
                    "state": "suspend",
                },
            ],
        }

    provider = SymbolProvider(
        OkxExchangeSymbolSource(fetch_json=fetch_json, version="v1"),
        filters=[
            ContractTypeFilter("SWAP"),
            QuoteAssetFilter("USDT"),
            TradableStatusFilter(),
        ],
    )

    universe = provider.fetch()
    assert universe.exchange == "okx"
    assert universe.symbols == ("BTC-USDT-SWAP",)
    assert set(universe.metadata["records"]) == {"BTC-USDT-SWAP"}
