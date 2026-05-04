"""Tests for dataflow.binance.common — symbol mapping, URL construction."""

import pytest

from dataflow.binance.common import (
    build_combined_stream_url,
    from_binance_symbol,
    to_binance_symbol,
)


class TestSymbolMapping:

    @pytest.mark.parametrize(
        "unified, expected",
        [
            ("BTC-USDT-SWAP", "btcusdt"),
            ("ETH-USDT-SWAP", "ethusdt"),
            ("BTC-USDT", "btcusdt"),
            ("SOL-USDT", "solusdt"),
            ("DOGE-USDT-SWAP", "dogeusdt"),
        ],
    )
    def test_to_binance_symbol(self, unified: str, expected: str):
        assert to_binance_symbol(unified) == expected

    @pytest.mark.parametrize(
        "binance, expected",
        [
            ("BTCUSDT", "BTC-USDT"),
            ("btcusdt", "BTC-USDT"),
            ("ETHUSDT", "ETH-USDT"),
            ("SOLUSDT", "SOL-USDT"),
            ("DOGEUSDT", "DOGE-USDT"),
        ],
    )
    def test_from_binance_symbol(self, binance: str, expected: str):
        assert from_binance_symbol(binance) == expected

    def test_roundtrip(self):
        """to_binance → from_binance should recover the unified format (minus -SWAP)."""
        original = "BTC-USDT-SWAP"
        binance = to_binance_symbol(original)
        recovered = from_binance_symbol(binance)
        assert recovered == "BTC-USDT"


class TestBuildCombinedStreamUrl:

    def test_single_stream(self):
        url = build_combined_stream_url(["btcusdt@aggTrade"])
        assert url == "wss://fstream.binance.com/market/stream?streams=btcusdt@aggTrade"

    def test_multiple_streams(self):
        url = build_combined_stream_url(["btcusdt@aggTrade", "ethusdt@aggTrade"])
        assert "btcusdt@aggTrade/ethusdt@aggTrade" in url
        assert url.startswith("wss://fstream.binance.com/market/stream?streams=")
