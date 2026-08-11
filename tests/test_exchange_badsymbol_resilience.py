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

    await test_ensure_markets_loaded()


async def test_ensure_markets_loaded():
    """
    The real fix for a confirmed live bug: "hyperliquid markets not
    loaded" appearing on the very first coin of a scan. Root cause:
    ccxt lazily loads markets on first use, but build_snapshot() fires
    5 concurrent calls for the first coin, and multiple of those can
    race to trigger that lazy-load simultaneously.
    """
    source = ExchangeDataSource(ScannerConfig())
    call_count = {"value": 0}

    async def fake_load_markets():
        call_count["value"] += 1

    source._hyperliquid.load_markets = fake_load_markets

    # 1. First call actually loads markets
    await source.ensure_markets_loaded()
    assert call_count["value"] == 1
    print("1. First call to ensure_markets_loaded() actually invokes load_markets(): OK")

    # 2. Repeat calls are cheap no-ops, not re-fetching every time
    await source.ensure_markets_loaded()
    await source.ensure_markets_loaded()
    assert call_count["value"] == 1, f"Expected repeat calls to be no-ops, got {call_count['value']} total invocations"
    print("2. Repeat calls are guarded no-ops — markets only loaded once per ExchangeDataSource instance: OK")

    # 3. A failure in load_markets() degrades gracefully, never crashes the scan
    source2 = ExchangeDataSource(ScannerConfig())
    async def failing_load_markets():
        raise RuntimeError("network hiccup during markets load")
    source2._hyperliquid.load_markets = failing_load_markets
    await source2.ensure_markets_loaded()  # should not raise
    print("3. A failure loading markets degrades gracefully — never blocks or crashes the scan: OK")

    await source.close()
    await source2.close()
    print("\n✅ ensure_markets_loaded test passed: loads once, guards against repeat calls, degrades gracefully on failure.")

    await test_hyperliquid_oi_field_bug_fixed()


async def test_hyperliquid_oi_field_bug_fixed():
    """
    A confirmed, real bug found by reading ccxt's own source: Hyperliquid's
    ccxt adapter hardcodes openInterestValue to None ALWAYS (see
    parse_open_interest in ccxt/async_support/hyperliquid.py) — the real
    figure is openInterestAmount (base-asset units) which needs
    multiplying by markPx to get a USD value. Reading openInterestValue
    directly, what the code did before, silently returned None for
    every coin, every scan, even when Hyperliquid answered successfully
    — this is very likely THE actual explanation for "OI always shows
    None" across many live scan reports this session, not a network/
    availability issue at all.

    Uses the exact real response shape from ccxt's own source code
    comments, not an invented one.
    """
    source = ExchangeDataSource(ScannerConfig())

    # The exact real shape ccxt's own docstring shows fetch_open_interest returning
    async def fake_fetch_open_interest(symbol):
        return {
            "symbol": "HYPE/USDC:USDC",
            "openInterestAmount": 14677900.74,
            "openInterestValue": None,  # confirmed: ccxt ALWAYS sets this to None for Hyperliquid
            "timestamp": None,
            "datetime": None,
            "info": {
                "szDecimals": "2", "name": "HYPE", "maxLeverage": "3", "funding": "0.00014735",
                "openInterest": "14677900.74", "prevDayPx": "26.145", "dayNtlVlm": "299643445.12560016",
                "premium": "0.00081613", "oraclePx": "27.569", "markPx": "27.63", "midPx": "27.599",
                "impactPxs": ["27.5915", "27.6319"], "dayBaseVlm": "10790652.83", "baseId": 159,
            },
        }

    source._hyperliquid.fetch_open_interest = fake_fetch_open_interest
    history, src = await source.fetch_open_interest_history_with_source("HYPE")

    assert src == "hyperliquid", f"Expected Hyperliquid to be used as the source, got {src}"
    assert history is not None, "THE ACTUAL BUG: OI history should NOT be None when Hyperliquid genuinely answered with real data"
    oi_usd = history["oi_usd"].iloc[-1]
    expected_usd = 14677900.74 * 27.63  # amount (base units) x markPx (USD)
    assert abs(oi_usd - expected_usd) < 1.0, f"Expected OI in USD = amount x markPx = {expected_usd:,.0f}, got {oi_usd:,.0f}"
    print(f"1. THE ACTUAL BUG FIX CONFIRMED: real Hyperliquid OI response now correctly computes ${oi_usd:,.0f} USD (amount {14677900.74:,.0f} x mark price ${27.63}), not silently None: OK")

    # A response with a missing/malformed markPx should degrade gracefully, not crash
    async def fake_fetch_open_interest_bad_price(symbol):
        return {"openInterestAmount": 1000.0, "openInterestValue": None, "info": {"markPx": None}}
    source._hyperliquid.fetch_open_interest = fake_fetch_open_interest_bad_price
    history2, src2 = await source.fetch_open_interest_history_with_source("TEST")
    # Should fall through to the next source (or None entirely) rather than crash or return a garbage value
    assert history2 is None or "oi_usd" in history2.columns
    print("2. A malformed/missing mark price degrades gracefully (falls through), doesn't crash: OK")

    await source.close()
    print("\n✅ Hyperliquid OI field-name bug test passed: real ccxt response shape correctly parsed into a genuine USD open interest value.")


if __name__ == "__main__":
    asyncio.run(main())
