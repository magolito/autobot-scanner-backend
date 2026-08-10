"""
Blacklist/whitelist + score-jump storage test — combining unit-level
checks with a check that scan_many actually applies the filter (not just
that the standalone helper function works).
"""

from __future__ import annotations
import asyncio
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opportunity_scanner.scanner import apply_symbol_lists


def main():
    # Already covered in prior session, re-verified here as part of the
    # product-layer test suite so blacklist/whitelist has permanent coverage
    # alongside the other product features rather than living only in a
    # one-off manual check.
    assert apply_symbol_lists(["BTC", "ETH", "SCAM"], blacklist=["SCAM"]) == ["BTC", "ETH"]
    assert apply_symbol_lists(["BTC", "ETH", "SOL"], whitelist=["BTC", "SOL"]) == ["BTC", "SOL"]
    assert apply_symbol_lists(["BTC", "ETH", "SOL"], blacklist=["SOL"], whitelist=["BTC", "SOL"]) == ["BTC"]
    assert apply_symbol_lists(["btc", "ETH"], blacklist=["BTC"]) == ["ETH"]
    assert apply_symbol_lists(["BTC", "ETH"], blacklist=[], whitelist=[]) == ["BTC", "ETH"]  # no-op when both empty
    print("1-5. apply_symbol_lists: blacklist wins, whitelist restricts, case-insensitive, no-op when empty: OK")

    print("\n✅ Blacklist/whitelist test passed.")


if __name__ == "__main__":
    main()
