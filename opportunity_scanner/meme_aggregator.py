"""
Meme data aggregator — the composition layer that turns raw provider
responses into a MemeCoinMetrics ready for ScoringEngine.score().

Three things this owns that no single provider does on its own:
  1. Venue-aware field mapping — a bonding-curve pump.fun token has no
     traditional LP to lock, so lp_locked_pct is deliberately left None
     (N/A) rather than reading a value RugCheck would report as 0% or
     absent anyway. See degen_models.py::PairVenue for the full reasoning.
  2. Cross-source conflict detection — RugCheck and GoPlus can disagree
     (e.g. RugCheck says mint authority revoked, GoPlus flags the
     contract as mintable). That disagreement is itself a signal worth
     surfacing, not silently resolved by picking one source arbitrarily.
  3. Honest gaps — unique_makers_1h and holder_growth_pct_1h have no
     single-call source anywhere (see MEME_ARCHITECTURE.md §9); they
     stay None here rather than being filled with a misleading proxy
     that isn't labeled as such.
"""

from __future__ import annotations
import asyncio
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from .data_sources.dexscreener import DexScreenerProvider
from .data_sources.rugcheck import RugCheckProvider, RugCheckReport
from .data_sources.goplus import GoPlusSecurityReport, GoPlusProvider
from .data_sources.social import SocialDataSource
from .degen_models import DexPair, PairVenue
from .meme_scoring_engine import MemeCoinMetrics


def _pair_age_minutes(pair_created_at: Optional[str]) -> float:
    if not pair_created_at:
        return 0.0
    try:
        created = datetime.fromisoformat(pair_created_at)
        return max((datetime.now(timezone.utc) - created).total_seconds() / 60, 0.0)
    except ValueError:
        return 0.0


def detect_data_quality_issues(
    pair: DexPair, rugcheck: Optional[RugCheckReport], goplus: Optional[GoPlusSecurityReport],
) -> List[str]:
    """
    Explicit cross-source conflict + missing-data detection, per the
    Phase 3 requirement. Returns human-readable notes — these get folded
    into risk_flags downstream, not silently swallowed.
    """
    notes: List[str] = []

    if rugcheck is None:
        notes.append("RugCheck data unavailable — safety read is incomplete")
    if goplus is None:
        notes.append("GoPlus data unavailable — honeypot/tax check is incomplete")

    if rugcheck is not None and goplus is not None:
        # RugCheck's mint_authority_revoked vs GoPlus's is_mintable should agree —
        # both describe the same underlying fact (can supply still be inflated?)
        if rugcheck.mint_authority_revoked is True and goplus.is_mintable is True:
            notes.append("CONFLICT: RugCheck says mint authority revoked, but GoPlus flags the token as mintable — verify manually before trusting either")

    if pair.venue == PairVenue.BONDING_CURVE:
        notes.append("Still on pump.fun's bonding curve — no traditional LP exists yet, LP-lock check is N/A rather than failed")

    if pair.liquidity_usd is None or pair.liquidity_usd == 0:
        notes.append("Liquidity data missing or zero — treat any score as unreliable")

    return notes


class MemeDataAggregator:
    def __init__(
        self,
        dexscreener: DexScreenerProvider,
        rugcheck: RugCheckProvider,
        goplus: GoPlusProvider,
        social: Optional[SocialDataSource] = None,
    ):
        self.dexscreener = dexscreener
        self.rugcheck = rugcheck
        self.goplus = goplus
        self.social = social

    async def close(self):
        await self.dexscreener.close()
        await self.rugcheck.close()
        await self.goplus.close()
        if self.social:
            await self.social.close()

    async def _fetch_social_velocity(self, symbol: str) -> Tuple[Optional[float], Optional[float]]:
        """Returns (mention_velocity_ratio, kol_score) — both None if social
        isn't configured or the symbol isn't tracked (real limitation for
        brand-new tokens, see MEME_ARCHITECTURE.md §3.3)."""
        if self.social is None:
            return None, None
        blob = await self.social.get_social_blob(symbol)
        if blob is None:
            return None, None
        current = blob.get("social_volume_24h")
        baseline = blob.get("social_volume_baseline")
        velocity = (current / baseline) if (current is not None and baseline) else None
        return velocity, None  # kol_score stays None until a KOL data source is wired up — same honest stub as the main scanner

    async def build_metrics(self, token_address: str, chain_id: str = "solana") -> Optional[Tuple[MemeCoinMetrics, List[str]]]:
        pair = await self.dexscreener.get_best_pair_for_token(token_address, chain_id)
        if pair is None:
            return None  # no DexScreener data at all — can't build anything meaningful

        rugcheck_task = self.rugcheck.get_report(token_address) if chain_id == "solana" else _none_coro()
        goplus_task = self.goplus.get_security_report(token_address, chain_id)
        social_task = self._fetch_social_velocity(pair.base_symbol)

        rugcheck_report, goplus_report, (velocity, kol_score) = await asyncio.gather(
            rugcheck_task, goplus_task, social_task, return_exceptions=False,
        )

        quality_notes = detect_data_quality_issues(pair, rugcheck_report, goplus_report)

        # Venue-aware: no traditional LP on a still-curving pump.fun token
        lp_locked_pct = None
        if pair.venue != PairVenue.BONDING_CURVE and rugcheck_report is not None:
            lp_locked_pct = rugcheck_report.lp_locked_pct

        buy_sell_ratio = None
        if pair.h1 and pair.h1.buys is not None and pair.h1.sells:
            buy_sell_ratio = pair.h1.buys / pair.h1.sells

        vol_to_liq = None
        if pair.liquidity_usd:
            vol_to_liq = (pair.volume_24h_usd or 0) / pair.liquidity_usd

        vol_accel_ratio = None
        if pair.m5 and pair.m5.volume_usd is not None and pair.h1 and pair.h1.volume_usd:
            # 5-minute volume, annualized to an hourly rate, vs the actual hourly volume
            m5_annualized = pair.m5.volume_usd * 12
            vol_accel_ratio = m5_annualized / pair.h1.volume_usd if pair.h1.volume_usd else None

        volume_change_pct = None
        if pair.h1 and pair.h1.volume_usd is not None and pair.h6 and pair.h6.volume_usd:
            avg_hourly_from_h6 = pair.h6.volume_usd / 6
            if avg_hourly_from_h6 > 0:
                volume_change_pct = ((pair.h1.volume_usd - avg_hourly_from_h6) / avg_hourly_from_h6) * 100

        metrics = MemeCoinMetrics(
            symbol=pair.base_symbol, token_address=token_address, chain_id=chain_id,
            price_usd=pair.price_usd, market_cap_usd=pair.market_cap_usd or pair.fdv_usd,
            liquidity_usd=pair.liquidity_usd or 0.0,
            pair_age_minutes=_pair_age_minutes(pair.pair_created_at),
            exchange_listings=1,

            mint_authority_revoked=rugcheck_report.mint_authority_revoked if rugcheck_report else None,
            freeze_authority_revoked=rugcheck_report.freeze_authority_revoked if rugcheck_report else None,
            is_honeypot=goplus_report.is_honeypot if goplus_report else None,
            buy_tax_pct=goplus_report.buy_tax_pct if goplus_report else None,
            sell_tax_pct=goplus_report.sell_tax_pct if goplus_report else None,
            lp_locked_pct=lp_locked_pct,
            top10_holder_pct=rugcheck_report.top10_holder_pct if rugcheck_report else None,
            dev_wallet_pct=rugcheck_report.dev_wallet_pct if rugcheck_report else None,
            unique_holders=rugcheck_report.unique_holders if rugcheck_report else None,
            rugcheck_risk_score=rugcheck_report.risk_score if rugcheck_report else None,
            insider_bundle_flag=rugcheck_report.insider_bundle_flag if rugcheck_report else False,
            deployer_address=rugcheck_report.deployer_address if rugcheck_report else None,

            mention_velocity_ratio=velocity,
            acceleration_ratio=None,  # needs two velocity snapshots over time — not derivable from one fetch
            dex_boosted=pair.is_boosted, boost_amount=pair.boost_amount,
            has_website=pair.has_website, has_twitter=pair.has_twitter, has_telegram=pair.has_telegram,
            kol_score=kol_score,

            unique_makers_1h=None,   # honest gap — needs Birdeye/Helius, see MEME_ARCHITECTURE.md §9
            buy_tx_count_1h=pair.h1.buys if pair.h1 else None,
            sell_tx_count_1h=pair.h1.sells if pair.h1 else None,
            buy_sell_ratio=buy_sell_ratio,
            holder_growth_pct_1h=None,  # honest gap — needs our own snapshot history
            volume_to_liquidity_ratio=vol_to_liq,
            volume_24h_usd=pair.volume_24h_usd,
            avg_tx_size_variance=None,   # honest gap — DexScreener doesn't expose per-tx size

            vol_accel_ratio=vol_accel_ratio,
            price_change_pct=pair.price_change_1h_pct,
            volume_change_pct=volume_change_pct,
        )

        return metrics, quality_notes


async def _none_coro():
    return None
