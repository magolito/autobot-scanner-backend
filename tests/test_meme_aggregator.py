"""
Meme aggregator test — synthetic providers (monkeypatched, no live
network), verifying the two things this file uniquely owns:

  1. Venue-aware handling: a bonding-curve pump.fun pair gets
     lp_locked_pct=None (N/A) even if RugCheck reports a value for it —
     the field genuinely doesn't apply pre-graduation.
  2. Cross-source conflict detection: RugCheck saying mint authority is
     revoked while GoPlus flags the token as mintable is a real,
     surfaced conflict, not silently resolved.

Also verifies the full pipeline: aggregator output feeds cleanly into
ScoringEngine.score() without crashing, end to end.
"""

from __future__ import annotations
import asyncio
import sys, os
from _time_helpers import relative_iso_timestamp
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opportunity_scanner.meme_aggregator import MemeDataAggregator, detect_data_quality_issues
from opportunity_scanner.data_sources.dexscreener import DexScreenerProvider
from opportunity_scanner.data_sources.rugcheck import RugCheckProvider, RugCheckReport
from opportunity_scanner.data_sources.goplus import GoPlusProvider, GoPlusSecurityReport
from opportunity_scanner.degen_models import DexPair, DexTransactionCounts, TimeframeStats, PairVenue
from opportunity_scanner.provider_models import DataSourceMeta
from opportunity_scanner.meme_scoring_engine import ScoringEngine, Mode


def make_pair(venue: PairVenue, liquidity_usd=50_000) -> DexPair:
    return DexPair(
        chain_id="solana", dex_id=venue.value, venue=venue,
        pair_address="fake_pair", base_symbol="TESTMEME", base_token_address="fake_mint",
        quote_symbol="SOL", price_usd=0.001, liquidity_usd=liquidity_usd, market_cap_usd=300_000,
        volume_24h_usd=150_000, price_change_24h_pct=20, price_change_1h_pct=8,
        txns_24h=DexTransactionCounts(buys=200, sells=90),
        pair_created_at=relative_iso_timestamp(90),  # relative, not hardcoded — stays valid regardless of when the test actually runs
        fdv_usd=300_000,
        meta=DataSourceMeta(source="dexscreener"),
        m5=TimeframeStats(price_change_pct=3, volume_usd=8_000, buys=20, sells=8),
        h1=TimeframeStats(price_change_pct=8, volume_usd=40_000, buys=90, sells=30),
        h6=TimeframeStats(price_change_pct=15, volume_usd=180_000, buys=400, sells=150),
        is_boosted=True, boost_amount=200,
        has_website=True, has_twitter=True, has_telegram=True,
    )


async def main():
    dex = DexScreenerProvider()
    rc = RugCheckProvider()
    gp = GoPlusProvider()
    aggregator = MemeDataAggregator(dex, rc, gp, social=None)

    # 1. Bonding curve venue -> lp_locked_pct forced to None even though RugCheck has a value
    bonding_pair = make_pair(PairVenue.BONDING_CURVE)
    dex.get_best_pair_for_token = lambda addr, chain_id="solana": asyncio.sleep(0, result=bonding_pair)
    rc.get_report = lambda addr: asyncio.sleep(0, result=RugCheckReport(
        mint_authority_revoked=True, freeze_authority_revoked=True,
        lp_locked_pct=100.0,  # RugCheck reports SOMETHING here, but it shouldn't be used pre-graduation
        top10_holder_pct=20.0, unique_holders=80, risk_score=10.0,
    ))
    gp.get_security_report = lambda addr, chain_id="solana": asyncio.sleep(0, result=GoPlusSecurityReport(is_honeypot=False, buy_tax_pct=0, sell_tax_pct=0))

    result = await aggregator.build_metrics("fake_mint", "solana")
    assert result is not None
    metrics, notes = result
    print(f"Bonding curve pair -> lp_locked_pct={metrics.lp_locked_pct} (should be None, N/A)")
    assert metrics.lp_locked_pct is None, f"Expected lp_locked_pct=None for bonding curve venue, got {metrics.lp_locked_pct}"
    assert any("bonding curve" in n.lower() for n in notes), f"Expected a bonding-curve N/A note, got {notes}"
    print("1. Bonding curve venue correctly forces lp_locked_pct to N/A regardless of RugCheck's value: OK")

    # 2. Raydium venue -> lp_locked_pct DOES come through from RugCheck
    raydium_pair = make_pair(PairVenue.RAYDIUM)
    dex.get_best_pair_for_token = lambda addr, chain_id="solana": asyncio.sleep(0, result=raydium_pair)
    result2 = await aggregator.build_metrics("fake_mint", "solana")
    metrics2, notes2 = result2
    assert metrics2.lp_locked_pct == 100.0, f"Expected lp_locked_pct=100.0 for a graduated Raydium pair, got {metrics2.lp_locked_pct}"
    print("2. Raydium (graduated) venue correctly uses RugCheck's real lp_locked_pct: OK")

    # 3. Cross-source conflict: RugCheck says revoked, GoPlus says mintable
    rc.get_report = lambda addr: asyncio.sleep(0, result=RugCheckReport(mint_authority_revoked=True, freeze_authority_revoked=True))
    gp.get_security_report = lambda addr, chain_id="solana": asyncio.sleep(0, result=GoPlusSecurityReport(is_honeypot=False, is_mintable=True))
    result3 = await aggregator.build_metrics("fake_mint", "solana")
    _, notes3 = result3
    print(f"Conflict test notes: {notes3}")
    assert any("CONFLICT" in n for n in notes3), f"Expected a CONFLICT note, got {notes3}"
    print("3. Cross-source RugCheck/GoPlus conflict correctly detected and surfaced: OK")

    # 4. Missing providers -> quality notes, not a crash
    rc.get_report = lambda addr: asyncio.sleep(0, result=None)
    gp.get_security_report = lambda addr, chain_id="solana": asyncio.sleep(0, result=None)
    result4 = await aggregator.build_metrics("fake_mint", "solana")
    metrics4, notes4 = result4
    assert any("RugCheck data unavailable" in n for n in notes4)
    assert any("GoPlus data unavailable" in n for n in notes4)
    print("4. Missing provider data correctly noted, doesn't crash: OK")

    # 5. Full pipeline: aggregator output feeds cleanly into the scoring engine
    engine = ScoringEngine(mode=Mode.EARLY_MOMENTUM)
    scored = engine.score(metrics4)
    print(f"5. Full pipeline: aggregator -> scoring engine works end to end. Safety={scored.safety.grade}, score={scored.opportunity_score}")

    await dex.close()
    await rc.close()
    await gp.close()

    print("\n✅ Meme aggregator test passed: venue-aware LP handling, cross-source conflict detection, missing-data notes, and full pipeline integration all verified.")


if __name__ == "__main__":
    asyncio.run(main())
