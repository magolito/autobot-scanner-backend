"""
Degen Radar test — synthetic DexPair data, no network needed.

Checks:
  1. Extremely thin liquidity triggers a "danger" flag
  2. Brand-new pair (<1hr old) triggers a "danger" flag
  3. Heavy sell pressure triggers a "warning" flag
  4. Extreme volume/liquidity ratio triggers a wash-trading warning
  5. A healthy-looking pair still gets flagged (category risk acknowledgment),
     never returns zero flags
  6. DegenSnapshot is never confused for a 0-100 pillar score (no `score` field)
"""

from __future__ import annotations
import sys, os
from datetime import datetime, timezone, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opportunity_scanner.degen_models import DexPair, DexTransactionCounts, DataSourceMeta
from opportunity_scanner.degen_radar import build_degen_snapshot


def make_pair(
    liquidity_usd=None, volume_24h_usd=None, buys=0, sells=0,
    hours_old=None, price_change_1h=None,
) -> DexPair:
    created_at = None
    if hours_old is not None:
        created_at = (datetime.now(timezone.utc) - timedelta(hours=hours_old)).isoformat()
    return DexPair(
        chain_id="solana", dex_id="raydium", pair_address="fake_address",
        base_symbol="DEGENCOIN", base_token_address="fake_token",
        quote_symbol="SOL",
        price_usd=0.0001, liquidity_usd=liquidity_usd, volume_24h_usd=volume_24h_usd,
        price_change_24h_pct=None, price_change_1h_pct=price_change_1h,
        txns_24h=DexTransactionCounts(buys=buys, sells=sells),
        pair_created_at=created_at, fdv_usd=None,
        meta=DataSourceMeta(source="dexscreener"),
    )


def main():
    # 1. Thin liquidity
    thin = make_pair(liquidity_usd=3_000, hours_old=48)
    snap1 = build_degen_snapshot(thin)
    assert any(f.severity == "danger" and "thin liquidity" in f.label.lower() for f in snap1.flags)
    print(f"1. Thin liquidity flagged danger: {[f.label for f in snap1.flags if f.severity=='danger']}")

    # 2. Brand new pair
    fresh = make_pair(liquidity_usd=100_000, hours_old=0.25)
    snap2 = build_degen_snapshot(fresh)
    assert any(f.severity == "danger" and "minutes ago" in f.label for f in snap2.flags)
    print(f"2. Brand-new pair flagged danger: {[f.label for f in snap2.flags if 'minutes' in f.label]}")

    # 3. Heavy sell pressure
    dumping = make_pair(liquidity_usd=100_000, hours_old=48, buys=10, sells=80)
    snap3 = build_degen_snapshot(dumping)
    assert any("sell pressure" in f.label for f in snap3.flags)
    print(f"3. Heavy sell pressure flagged: {[f.label for f in snap3.flags if 'sell pressure' in f.label]}")

    # 4. Wash-trading-shaped volume
    washy = make_pair(liquidity_usd=50_000, volume_24h_usd=2_000_000, hours_old=48)
    snap4 = build_degen_snapshot(washy)
    assert any("wash trading" in f.label for f in snap4.flags)
    print(f"4. Extreme volume/liquidity ratio flagged: {[f.label for f in snap4.flags if 'wash' in f.label]}")

    # 5. "Healthy" looking pair still gets at least one flag (never zero)
    healthy = make_pair(liquidity_usd=500_000, volume_24h_usd=200_000, hours_old=720, buys=100, sells=110)
    snap5 = build_degen_snapshot(healthy)
    assert len(snap5.flags) >= 1, "Expected at least one flag even for a healthy-looking pair (category risk)"
    print(f"5. Healthy-looking pair still flagged (never silent): {[f.label for f in snap5.flags]}")

    # 6. No numeric score field — can't be confused with the main pillars
    assert not hasattr(snap5, "score"), "DegenSnapshot should never have a 0-100 score field"
    assert not hasattr(snap5, "composite_score")
    print("6. DegenSnapshot has no numeric score field — can't be mistaken for main-pillar output: OK")

    print("\n✅ Degen Radar test passed: thin liquidity, new pairs, sell pressure, and wash-trading-shaped volume all flag correctly, healthy pairs still get a category-risk flag, output is structurally distinct from the main scoring pillars.")


if __name__ == "__main__":
    main()
