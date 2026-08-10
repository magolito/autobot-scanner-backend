"""
Exchange BadSymbol resilience test — the actual fix confirmed from live
production logs: BOTH Hyperliquid and Bybit circuit breakers open for
EVERY coin in a scan, killing OHLCV entirely for that whole window,
including for well-listed majors like ETH. Root cause traced to
messages like "hyperliquid does not have market symbol SHIB/USDC:USDC"
— a normal, expected condition for a curated derivatives exchange
that was nonetheless being counted as a circuit-breaker failure.

This is the real-world scenario: Trending Now discovery surfaces some
coins with real CoinGecko volume that simply aren't among Hyperliquid's
curated ~150 listed perpetuals. Before this fix, hitting just 3 of
those in a row (failure_threshold=3) tripped the breaker for its full
90s cooldown — killing OHLCV for every OTHER coin scanned in that
window, majors included, regardless of whether THEY were listed fine.
"""

from __future__ import annotations
import asyncio
import ccxt
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opportunity_scanner.config import ScannerConfig
from opportunity_scanner.data_sources.exchange import ExchangeDataSource
from opportunity_scanner.circuit_breaker import breakers, CircuitState


async def main():
    await breakers.get("hyperliquid_exchange")._record_success()  # reset for test isolation

    source = ExchangeDataSource(ScannerConfig())

    # Simulate exactly the real scenario: several coins in a row that
    # simply aren't listed on Hyperliquid (BadSymbol), then a genuinely
    # major, well-listed coin afterward.
    unlisted_coins = ["JOHN", "TUT", "BMT", "MMT", "STONKBROKER"]  # 5 in a row — well past the old threshold=3

    async def fake_ticker_unlisted(symbol):
        raise ccxt.BadSymbol(f"hyperliquid does not have market symbol {symbol}")

    source._hyperliquid.fetch_ticker = fake_ticker_unlisted

    for coin in unlisted_coins:
        result = await source._fetch_ticker_from("hyperliquid", coin)
        assert result is None, f"An unlisted symbol should gracefully return None, not crash"

    status = breakers.get("hyperliquid_exchange").status()
    print(f"Breaker status after {len(unlisted_coins)} consecutive unlisted-symbol responses: {status}")
    assert status["state"] == "closed", f"THE ACTUAL FIX: the breaker should stay CLOSED after unlisted-symbol responses, even well past the old threshold — got {status['state']}"
    print(f"1. Breaker correctly stayed CLOSED after {len(unlisted_coins)} consecutive 'symbol not listed' responses (well past the old failure_threshold=3): OK")

    # Now a genuinely major, well-listed coin — this MUST still succeed,
    # proving the breaker wasn't falsely poisoned by the unlisted coins
    # that came before it in the same scan
    async def fake_ticker_eth(symbol):
        return {"last": 3200.0, "bid": 3199.5, "ask": 3200.5, "quoteVolume": 15_000_000_000}

    source._hyperliquid.fetch_ticker = fake_ticker_eth
    eth_result = await source._fetch_ticker_from("hyperliquid", "ETH")
    assert eth_result is not None, "ETH's real ticker fetch must succeed — this is the actual bug: real majors were failing because unlisted coins scanned earlier had already tripped the shared breaker"
    assert eth_result["price"] == 3200.0
    print("2. THE ACTUAL FIX CONFIRMED: a genuinely major, well-listed coin (ETH) scanned right after several unlisted coins still gets its real data — the breaker was never falsely poisoned: OK")

    await breakers.get("hyperliquid_exchange")._record_success()
    await source.close()

    print("\n✅ BadSymbol resilience test passed: the exact live-confirmed bug (unlisted discovery coins killing OHLCV for real majors scanned in the same window) is fixed and proven with the real ccxt exception type.")


if __name__ == "__main__":
    asyncio.run(main())
