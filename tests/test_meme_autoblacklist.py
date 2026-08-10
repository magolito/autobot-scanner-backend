"""
Auto-blacklist integration test — proves the deployer blacklist actually
grows from real scan results. Two tokens, same deployer: the first is a
honeypot and fails; the second looks completely clean on its own but
should still be rejected because of what its deployer did last time.
"""

from __future__ import annotations
import asyncio
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _time_helpers import relative_iso_timestamp

from opportunity_scanner.settings import load_settings
from opportunity_scanner.meme_scoring_engine import Mode
from opportunity_scanner.data_sources.dexscreener import DexScreenerProvider
from opportunity_scanner.data_sources.rugcheck import RugCheckProvider, RugCheckReport
from opportunity_scanner.data_sources.goplus import GoPlusProvider, GoPlusSecurityReport
from opportunity_scanner.degen_models import DexPair, DexTransactionCounts, TimeframeStats, PairVenue
from opportunity_scanner.provider_models import DataSourceMeta
import opportunity_scanner.meme_main as meme_main_module

SAME_DEPLOYER = "deployer_serial_rugger"
TOKEN_A = "fake_token_A_honeypot"
TOKEN_B = "fake_token_B_looks_clean"


def make_pair(token_address: str, symbol: str) -> DexPair:
    return DexPair(
        chain_id="solana", dex_id="raydium", venue=PairVenue.RAYDIUM,
        pair_address=f"pair_{symbol}", base_symbol=symbol, base_token_address=token_address,
        quote_symbol="SOL", price_usd=0.001, liquidity_usd=60_000, market_cap_usd=300_000,
        volume_24h_usd=150_000, price_change_24h_pct=20, price_change_1h_pct=8,
        txns_24h=DexTransactionCounts(buys=200, sells=90),
        pair_created_at=relative_iso_timestamp(90), fdv_usd=300_000,
        meta=DataSourceMeta(source="dexscreener"),
        h1=TimeframeStats(price_change_pct=8, volume_usd=40_000, buys=90, sells=30),
        has_website=True, has_twitter=True, has_telegram=True,
    )


async def main():
    db_path = "/tmp/test_meme_autoblacklist.db"
    if os.path.exists(db_path):
        os.remove(db_path)

    original_get_pair = DexScreenerProvider.get_best_pair_for_token
    original_rc_report = RugCheckProvider.get_report
    original_gp_report = GoPlusProvider.get_security_report

    async def fake_get_pair(self, addr, chain_id="solana"):
        symbol = "HONEYPOT" if addr == TOKEN_A else "LOOKSCLEAN"
        return make_pair(addr, symbol)

    async def fake_rc_report(self, addr):
        return RugCheckReport(
            mint_authority_revoked=True, freeze_authority_revoked=True,
            lp_locked_pct=95, top10_holder_pct=15, dev_wallet_pct=3,
            unique_holders=200, risk_score=8, deployer_address=SAME_DEPLOYER,
        )

    async def fake_gp_report(self, addr, chain_id="solana"):
        is_honeypot = (addr == TOKEN_A)
        return GoPlusSecurityReport(is_honeypot=is_honeypot, buy_tax_pct=0, sell_tax_pct=0)

    DexScreenerProvider.get_best_pair_for_token = fake_get_pair
    RugCheckProvider.get_report = fake_rc_report
    GoPlusProvider.get_security_report = fake_gp_report

    try:
        settings = load_settings()
        settings.meme_scanner.db_path = db_path
        settings.meme_scanner.discovery.use_dexscreener_boosts = False
        settings.meme_scanner.min_opportunity_score_to_show = 0
        settings.meme_scanner.alerts.enabled = False

        results_a = await meme_main_module.run_scan(settings, Mode.EARLY_MOMENTUM, addresses=[TOKEN_A])
        assert len(results_a) == 1
        ra = results_a[0]
        print(f"Token A (honeypot): safety={ra.safety.grade}, reasons={ra.safety.reasons}")
        assert ra.safety.grade == "Fail"
        assert any("honeypot" in r.lower() for r in ra.safety.reasons)

        from opportunity_scanner.meme_storage import MemeScanStorage
        storage = MemeScanStorage(db_path)
        blacklist_entry = storage.is_deployer_blacklisted_sync(SAME_DEPLOYER)
        assert blacklist_entry is not None, "Expected the deployer to be auto-blacklisted after a honeypot Fail"
        print(f"1. Deployer auto-blacklisted after Token A's honeypot Fail: {blacklist_entry['reason']}")

        results_b = await meme_main_module.run_scan(settings, Mode.EARLY_MOMENTUM, addresses=[TOKEN_B])
        assert len(results_b) == 1
        rb = results_b[0]
        print(f"Token B (clean on its own, same deployer): safety={rb.safety.grade}, reasons={rb.safety.reasons}")
        assert rb.safety.grade == "Fail", "Expected token B to be rejected via the deployer blacklist despite looking clean on its own"
        assert any("blacklisted" in r.lower() for r in rb.safety.reasons)
        print("2. Token B correctly rejected via deployer blacklist, despite looking clean on every other signal: OK")

        print("\n✅ Auto-blacklist integration test passed: a real scan failure grows the blacklist, and a fresh scan cycle correctly reloads and applies it to a completely different token from the same deployer.")

    finally:
        DexScreenerProvider.get_best_pair_for_token = original_get_pair
        RugCheckProvider.get_report = original_rc_report
        GoPlusProvider.get_security_report = original_gp_report
        if os.path.exists(db_path):
            os.remove(db_path)


if __name__ == "__main__":
    asyncio.run(main())
