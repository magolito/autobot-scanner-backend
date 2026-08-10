"""
Meme scanner end-to-end integration test — actually calls run_scan()
itself (not just its component pieces in isolation), twice in sequence
for the same token with rising hype between calls. This is the test that
proves the Phase 5 wiring works TOGETHER: storage persistence, hype
event detection reading back what storage just wrote, and alert dispatch
triggering only when all three required conditions are met at once.

No live network — every provider method that would hit a real API is
monkeypatched at the CLASS level, since run_scan() constructs its own
provider instances internally and there's no way to inject a
pre-configured instance from outside.
"""

from __future__ import annotations
import asyncio
import os
import sys
from _time_helpers import relative_iso_timestamp
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opportunity_scanner.settings import load_settings
from opportunity_scanner.meme_scoring_engine import Mode
from opportunity_scanner.data_sources.dexscreener import DexScreenerProvider
from opportunity_scanner.data_sources.rugcheck import RugCheckProvider, RugCheckReport
from opportunity_scanner.data_sources.goplus import GoPlusProvider, GoPlusSecurityReport
from opportunity_scanner.degen_models import DexPair, DexTransactionCounts, TimeframeStats, PairVenue
from opportunity_scanner.provider_models import DataSourceMeta
from opportunity_scanner.alerts import TelegramSender
import opportunity_scanner.meme_main as meme_main_module


FAKE_TOKEN = "fake_integration_token"
SENT_MESSAGES = []


def make_pair(boosted: bool, price_usd: float) -> DexPair:
    return DexPair(
        chain_id="solana", dex_id="raydium", venue=PairVenue.RAYDIUM,
        pair_address="fake_pair", base_symbol="INTEGTEST", base_token_address=FAKE_TOKEN,
        quote_symbol="SOL", price_usd=price_usd, liquidity_usd=60_000, market_cap_usd=400_000,
        volume_24h_usd=200_000, price_change_24h_pct=30, price_change_1h_pct=10,
        txns_24h=DexTransactionCounts(buys=250, sells=80),
        pair_created_at=relative_iso_timestamp(90),  # relative, not hardcoded — same fix as test_meme_aggregator.py's earlier bug
        fdv_usd=400_000,
        meta=DataSourceMeta(source="dexscreener"),
        h1=TimeframeStats(price_change_pct=10, volume_usd=50_000, buys=100, sells=30),
        is_boosted=boosted, boost_amount=300 if boosted else None,
        has_website=True, has_twitter=True, has_telegram=True,
    )


async def fake_telegram_send(self, message: str) -> bool:
    SENT_MESSAGES.append(message)
    return True


async def main():
    db_path = "/tmp/test_meme_integration.db"
    if os.path.exists(db_path):
        os.remove(db_path)

    original_get_pair = DexScreenerProvider.get_best_pair_for_token
    original_rc_report = RugCheckProvider.get_report
    original_gp_report = GoPlusProvider.get_security_report
    original_telegram_send = TelegramSender.send

    call_state = {"boosted": False, "price": 0.001}

    async def fake_get_pair(self, addr, chain_id="solana"):
        return make_pair(boosted=call_state["boosted"], price_usd=call_state["price"])

    async def fake_rc_report(self, addr):
        return RugCheckReport(
            mint_authority_revoked=True, freeze_authority_revoked=True,
            lp_locked_pct=95, top10_holder_pct=15, dev_wallet_pct=3,
            unique_holders=200, risk_score=8, deployer_address="deployer_clean",
        )

    async def fake_gp_report(self, addr, chain_id="solana"):
        return GoPlusSecurityReport(is_honeypot=False, buy_tax_pct=0, sell_tax_pct=0)

    DexScreenerProvider.get_best_pair_for_token = fake_get_pair
    RugCheckProvider.get_report = fake_rc_report
    GoPlusProvider.get_security_report = fake_gp_report
    TelegramSender.send = fake_telegram_send

    try:
        settings = load_settings()
        settings.meme_scanner.db_path = db_path
        settings.meme_scanner.discovery.watchlist = [FAKE_TOKEN]
        settings.meme_scanner.discovery.use_dexscreener_boosts = False
        settings.meme_scanner.min_opportunity_score_to_show = 0
        settings.meme_scanner.alerts.enabled = True
        settings.meme_scanner.alerts.min_opportunity_score = 50.0
        settings.telegram_bot_token = "fake_token"
        settings.telegram_chat_id = "fake_chat"

        # --- First scan: not boosted, no prior scan exists yet ---
        results1 = await meme_main_module.run_scan(settings, Mode.EARLY_MOMENTUM, addresses=[FAKE_TOKEN])
        assert len(results1) == 1
        r1 = results1[0]
        print(f"Scan 1: safety={r1.safety.grade}, score={r1.opportunity_score}, hype_events={len(r1.hype_events)}")
        assert len(r1.hype_events) == 0, "Expected no hype events on the very first scan (no prior data to compare against)"
        assert len(SENT_MESSAGES) == 0, "Expected no alert on first scan (no rising-hype event yet)"
        print("1. First scan: no hype events (nothing to compare against), no alert sent: OK")

        # --- Second scan: newly boosted -> should detect a hype event and alert ---
        call_state["boosted"] = True
        call_state["price"] = 0.0015
        results2 = await meme_main_module.run_scan(settings, Mode.EARLY_MOMENTUM, addresses=[FAKE_TOKEN])
        assert len(results2) == 1
        r2 = results2[0]
        print(f"Scan 2: safety={r2.safety.grade}, score={r2.opportunity_score}, hype_events={[e.label for e in r2.hype_events]}")
        assert len(r2.hype_events) > 0, f"Expected a hype event on the second scan (newly boosted), got {r2.hype_events}"
        assert any("boosted" in e.label.lower() for e in r2.hype_events)
        print("2. Second scan (newly boosted): hype event correctly detected: OK")

        assert len(SENT_MESSAGES) == 1, f"Expected exactly 1 alert dispatched after the hype event, got {len(SENT_MESSAGES)}"
        assert "INTEGTEST" in SENT_MESSAGES[0]
        print("3. Alert dispatched after rising hype + high score + safety pass — message contains symbol: OK")

        from opportunity_scanner.meme_storage import MemeScanStorage
        storage = MemeScanStorage(db_path)
        history = storage.get_history_sync(FAKE_TOKEN)
        assert len(history) == 2, f"Expected 2 persisted scans, got {len(history)}"
        print(f"4. Storage correctly persisted both scans: {len(history)} rows: OK")

        print("\n✅ End-to-end integration test passed: run_scan() itself correctly wires storage, hype event detection, and alert dispatch together across consecutive scans.")

    finally:
        DexScreenerProvider.get_best_pair_for_token = original_get_pair
        RugCheckProvider.get_report = original_rc_report
        GoPlusProvider.get_security_report = original_gp_report
        TelegramSender.send = original_telegram_send
        if os.path.exists(db_path):
            os.remove(db_path)


if __name__ == "__main__":
    asyncio.run(main())
