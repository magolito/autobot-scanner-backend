"""
Bybit cooldown duration test — the real fix for a meaningful chunk of a
live-reported 15-minute scan time. Confirmed, repeated finding across
this whole session: Bybit is PERMANENTLY blocked for this deployment
(CloudFront geo-block), not transiently down. The old 90-second cooldown
meant the breaker retried a source that can never succeed roughly every
90 seconds for the ENTIRE duration of every scan — on a long scan,
that's many pointless retry attempts, each a real network round-trip,
burning real time for zero chance of success.
"""

from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    from opportunity_scanner.data_sources.exchange import ExchangeDataSource
    from opportunity_scanner.config import ScannerConfig
    import inspect

    sig = inspect.signature(ExchangeDataSource.__init__)
    default_cooldown = sig.parameters["bybit_cooldown_seconds"].default
    hyperliquid_cooldown = sig.parameters["hyperliquid_cooldown_seconds"].default

    # 1. THE ACTUAL FIX: Bybit's cooldown is now measured in hours, not
    # seconds — confirmed permanently blocked, retrying every 90s was
    # pure waste
    assert default_cooldown >= 3600, f"Expected Bybit's cooldown to be at least 1 hour (permanently blocked, not transient), got {default_cooldown}s"
    print(f"1. THE ACTUAL FIX: Bybit's default cooldown is now {default_cooldown}s ({default_cooldown/3600:.1f}h) instead of the old 90s — a confirmed-permanently-blocked source no longer gets retried every 90 seconds for the entire duration of every scan: OK")

    # 2. Hyperliquid — a genuinely healthy, primary source with occasional
    # real transient blips — deliberately keeps a SHORT cooldown, since
    # unlike Bybit, retrying it quickly after a real hiccup is worthwhile
    assert hyperliquid_cooldown < 200, f"Hyperliquid's cooldown should stay short (genuinely healthy source, worth quick retries), got {hyperliquid_cooldown}s"
    print(f"2. Hyperliquid's cooldown correctly stays short ({hyperliquid_cooldown}s) — this fix is specific to Bybit's confirmed PERMANENT block, not a blanket 'retry less' change that would also hurt genuine transient-failure recovery: OK")

    # 3. Concretely quantify the time saved on a realistic long scan: how
    # many pointless Bybit retry cycles would the OLD 90s cooldown cause
    # over a 15-minute (900s) scan, vs the new cooldown?
    scan_duration_seconds = 900  # the actual reported real-world scan time
    old_retry_cycles = scan_duration_seconds // 90
    new_retry_cycles = scan_duration_seconds // default_cooldown
    assert old_retry_cycles >= 10, f"Old cooldown should cause 10+ pointless retries over a 15-min scan, got {old_retry_cycles}"
    assert new_retry_cycles == 0, f"New cooldown should cause ZERO retries within a single 15-min scan (it only tests again after the scan is long over), got {new_retry_cycles}"
    print(f"3. Concretely: over a real 15-minute scan, the OLD cooldown would trigger ~{old_retry_cycles} pointless retry attempts against a permanently-dead source; the NEW cooldown triggers {new_retry_cycles} — each old retry was real, wasted network round-trip time on every single scan: OK")

    print("\n✅ Bybit cooldown duration test passed: a confirmed permanent block is no longer retried on a short timer forever, while genuinely healthy sources keep fast recovery.")


if __name__ == "__main__":
    main()
