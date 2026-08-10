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


if __name__ == "__main__":
    asyncio.run(main())
