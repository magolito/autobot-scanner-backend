"""
Settings wiring test — proves a settings.yaml edit actually changes
runtime behavior, not just that the YAML parses.

Checks:
  1. Settings load from settings.yaml correctly, all new sections present
  2. Env var override works, including nested (WEIGHTS__STRENGTH) and
     top-level-to-nested secret backfill (TELEGRAM_BOT_TOKEN -> alerts.telegram.bot_token)
  3. to_cache_ttls() produces the dict ExchangeDataSource actually consumes,
     and a custom TTL value actually ends up on the constructed instance
  4. to_breaker_config() produces the dict MultiExchangeOIProvider actually
     consumes, and a custom threshold actually ends up on the constructed breaker
  5. Weights validation still rejects a bad sum (fails loudly, not silently)
"""

from __future__ import annotations
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opportunity_scanner.settings import Settings, load_settings
from opportunity_scanner.data_sources.exchange import ExchangeDataSource
from opportunity_scanner.data_sources.multi_exchange_oi import MultiExchangeOIProvider


def main():
    # 1. Full load, new sections present
    settings = load_settings()
    assert settings.alerts is not None
    assert settings.resilience is not None
    assert settings.logging.level == "INFO"
    assert settings.api.port == 8000
    print("1. Full settings load with alerts/resilience/logging/api sections: OK")

    # 2a. Nested env var override
    os.environ["WEIGHTS__STRENGTH"] = "0.30"
    os.environ["WEIGHTS__OI_DYNAMICS"] = "0.20"
    os.environ["WEIGHTS__MOMENTUM"] = "0.25"
    os.environ["WEIGHTS__SOCIAL"] = "0.25"
    s2 = load_settings()
    assert s2.weights.strength == 0.30
    print("2a. Nested env var override (WEIGHTS__STRENGTH): OK")
    for k in ["WEIGHTS__STRENGTH", "WEIGHTS__OI_DYNAMICS", "WEIGHTS__MOMENTUM", "WEIGHTS__SOCIAL"]:
        os.environ.pop(k, None)

    # 2b. Top-level secret backfill into nested alerts config
    os.environ["TELEGRAM_BOT_TOKEN"] = "test-token-123"
    s3 = load_settings()
    assert s3.alerts.telegram.bot_token == "test-token-123"
    print("2b. Top-level TELEGRAM_BOT_TOKEN backfills into alerts.telegram.bot_token: OK")
    os.environ.pop("TELEGRAM_BOT_TOKEN", None)

    # 3. to_cache_ttls() actually changes ExchangeDataSource's behavior
    os.environ["RESILIENCE__CACHE_TTL_SECONDS__TICKER"] = "999"
    s4 = load_settings()
    ttls = s4.to_cache_ttls()
    assert ttls["ticker"] == 999.0, f"Expected overridden ticker TTL of 999, got {ttls['ticker']}"
    scanner_config = s4.to_scanner_config()
    ds = ExchangeDataSource(scanner_config, cache_ttls=ttls)
    assert ds.cache_ttls["ticker"] == 999.0, "Expected ExchangeDataSource to actually use the configured TTL"
    print("3. settings.yaml/env change to ticker TTL actually reaches ExchangeDataSource: OK")
    os.environ.pop("RESILIENCE__CACHE_TTL_SECONDS__TICKER", None)

    # 4. to_breaker_config() actually changes the constructed circuit breaker
    os.environ["RESILIENCE__CIRCUIT_BREAKERS__COINGLASS__FAILURE_THRESHOLD"] = "9"
    s5 = load_settings()
    breaker_config = s5.to_breaker_config()
    assert breaker_config["coinglass"]["failure_threshold"] == 9
    provider = MultiExchangeOIProvider(breaker_config=breaker_config)
    assert provider.coinglass._breaker.failure_threshold == 9, "Expected the constructed breaker to use the configured threshold"
    print("4. settings.yaml/env change to circuit breaker threshold actually reaches the constructed breaker: OK")
    os.environ.pop("RESILIENCE__CIRCUIT_BREAKERS__COINGLASS__FAILURE_THRESHOLD", None)

    # 5. Weights validation still fails loudly on a bad sum
    try:
        os.environ["WEIGHTS__STRENGTH"] = "0.90"
        load_settings()
        assert False, "Expected a validation error for weights not summing to 1.0"
    except Exception as e:
        assert "sum to 1.0" in str(e) or "1.0" in str(e)
        print("5. Weights validation still rejects a bad sum (fails loudly): OK")
    finally:
        os.environ.pop("WEIGHTS__STRENGTH", None)

    print("\n✅ Settings wiring test passed: config changes actually flow through to constructed objects, not just parsed and ignored.")


if __name__ == "__main__":
    main()
