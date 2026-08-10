"""
Circuit breaker test — synthetic failing/succeeding calls, no network needed.

Checks:
  1. Breaker stays CLOSED and calls go through while the provider succeeds
  2. After `failure_threshold` consecutive failures, breaker OPENS
  3. While OPEN, calls fail fast with CircuitOpenError (no attempt made)
  4. After cooldown elapses, breaker allows a test call through (HALF_OPEN)
  5. A successful test call CLOSES the breaker again
"""

from __future__ import annotations
import asyncio
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opportunity_scanner.circuit_breaker import CircuitBreaker, CircuitOpenError, CircuitState


async def main():
    breaker = CircuitBreaker(name="test_provider", failure_threshold=3, cooldown_seconds=0.3)

    call_count = {"n": 0}

    async def always_succeeds():
        call_count["n"] += 1
        return "ok"

    async def always_fails():
        call_count["n"] += 1
        raise ConnectionError("simulated network failure")

    # 1. Normal operation
    result = await breaker.call(always_succeeds)
    assert result == "ok"
    assert breaker.state == CircuitState.CLOSED
    print("1. Breaker stays CLOSED on success: OK")

    # 2. Trip the breaker
    for i in range(3):
        try:
            await breaker.call(always_fails)
        except ConnectionError:
            pass
    assert breaker.state == CircuitState.OPEN, f"Expected OPEN after 3 failures, got {breaker.state}"
    print(f"2. Breaker OPENS after {breaker.failure_threshold} consecutive failures: OK")

    # 3. Fails fast while open — call_count should NOT increment
    count_before = call_count["n"]
    try:
        await breaker.call(always_succeeds)
        assert False, "Expected CircuitOpenError while breaker is OPEN"
    except CircuitOpenError:
        pass
    assert call_count["n"] == count_before, "Expected no actual call attempt while circuit is OPEN"
    print("3. Breaker fails fast while OPEN (no network call attempted): OK")

    # 4. Wait for cooldown, then it should allow a test call through
    await asyncio.sleep(0.35)
    assert breaker.state == CircuitState.HALF_OPEN, f"Expected HALF_OPEN after cooldown, got {breaker.state}"
    print("4. Breaker transitions to HALF_OPEN after cooldown: OK")

    # 5. A successful call closes it again
    result = await breaker.call(always_succeeds)
    assert result == "ok"
    assert breaker.state == CircuitState.CLOSED, f"Expected CLOSED after successful test call, got {breaker.state}"
    print("5. Breaker CLOSES again after a successful test call: OK")

    print("\n✅ Circuit breaker test passed: opens on threshold, fails fast, half-opens after cooldown, closes on recovery.")

    await test_ignore_exceptions()


async def test_ignore_exceptions():
    """
    The actual fix for a confirmed live bug: a symbol simply not being
    listed on an exchange (ccxt.BadSymbol, or any similarly "expected,
    not a health signal" exception) was being counted as a circuit-
    breaker failure identically to a real network/health failure.
    Confirmed live: discovering just 3 coins in a row that happened not
    to be listed on Hyperliquid was enough to trip the breaker for its
    full cooldown, killing OHLCV for every OTHER coin in that window —
    including genuinely major, well-listed coins unrelated to the
    missing-symbol coins that tripped it.
    """
    from opportunity_scanner.circuit_breaker import CircuitBreaker, CircuitState

    class FakeBadSymbol(Exception):
        pass

    class FakeNetworkError(Exception):
        pass

    breaker = CircuitBreaker(name="test_ignore", failure_threshold=3, cooldown_seconds=60)

    # 1. An ignored exception still propagates to the caller as normal
    async def raises_bad_symbol():
        raise FakeBadSymbol("not listed here")

    for _ in range(3):
        try:
            await breaker.call(raises_bad_symbol, ignore_exceptions=(FakeBadSymbol,))
            assert False, "Should have raised"
        except FakeBadSymbol:
            pass
    print("1. Ignored exceptions still propagate to the caller normally (caller can still handle them): OK")

    # 2. THE ACTUAL FIX: repeated ignored exceptions do NOT trip the breaker,
    # even well past the failure threshold
    assert breaker.state == CircuitState.CLOSED, f"Expected breaker to stay CLOSED after 3 ignored 'bad symbol' exceptions (threshold=3), got {breaker.state}"
    print("2. THE ACTUAL FIX: repeated 'symbol not listed' exceptions do NOT trip the breaker, even past the failure threshold — a coin simply not being listed says nothing about the exchange's health: OK")

    # 3. A genuine, non-ignored failure right after still counts normally —
    # ignore_exceptions doesn't accidentally disable the breaker entirely
    async def raises_network_error():
        raise FakeNetworkError("connection refused")

    for _ in range(3):
        try:
            await breaker.call(raises_network_error, ignore_exceptions=(FakeBadSymbol,))
        except FakeNetworkError:
            pass
    assert breaker.state == CircuitState.OPEN, f"Expected the breaker to correctly trip on 3 REAL failures (not ignored), got {breaker.state}"
    print("3. Genuine, non-ignored failures still correctly trip the breaker — ignore_exceptions only exempts the specific exception types listed, nothing else: OK")

    print("\n✅ ignore_exceptions test passed: expected conditions like an unlisted symbol no longer falsely trip the breaker, while genuine health failures still do.")


if __name__ == "__main__":
    asyncio.run(main())
