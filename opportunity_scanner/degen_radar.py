"""
Degen Radar — risk-flagging logic for on-chain/memecoin tokens.

Deliberately NOT a 0-100 score like the four main pillars. Blending a
thin-liquidity memecoin into the same numeric scale as BTC would imply a
false equivalence of confidence — a "72" here and a "72" on the main
Opportunity Score should not read as comparably trustworthy. Flags are
the primary output; a human reads them and decides, rather than the
system collapsing everything into a single number that invites blind
trust.
"""

from __future__ import annotations
from datetime import datetime, timezone
from typing import List, Optional

from .degen_models import DexPair, DegenSnapshot, DegenFlag


def _pair_age_hours(pair_created_at: Optional[str]) -> Optional[float]:
    if not pair_created_at:
        return None
    try:
        created = datetime.fromisoformat(pair_created_at)
        return (datetime.now(timezone.utc) - created).total_seconds() / 3600
    except ValueError:
        return None


def build_degen_snapshot(pair: DexPair) -> DegenSnapshot:
    flags: List[DegenFlag] = []
    age_hours = _pair_age_hours(pair.pair_created_at)

    # Liquidity depth — the single most important risk signal for a thin token
    if pair.liquidity_usd is not None:
        if pair.liquidity_usd < 10_000:
            flags.append(DegenFlag(label=f"Extremely thin liquidity (${pair.liquidity_usd:,.0f}) — high slippage/rug risk", severity="danger"))
        elif pair.liquidity_usd < 50_000:
            flags.append(DegenFlag(label=f"Thin liquidity (${pair.liquidity_usd:,.0f}) — expect significant slippage", severity="warning"))
    else:
        flags.append(DegenFlag(label="No liquidity data available", severity="warning"))

    # Pair age — brand-new pairs carry materially different risk than established ones
    if age_hours is not None:
        if age_hours < 1:
            flags.append(DegenFlag(label=f"Pair created {age_hours*60:.0f} minutes ago — extremely new", severity="danger"))
        elif age_hours < 24:
            flags.append(DegenFlag(label=f"Pair created {age_hours:.1f} hours ago — very new", severity="warning"))

    # Buy/sell imbalance — heavily skewed can indicate a pump or a dump in progress
    if pair.txns_24h and pair.txns_24h.buy_sell_ratio is not None:
        ratio = pair.txns_24h.buy_sell_ratio
        if ratio > 3:
            flags.append(DegenFlag(label=f"Buy/sell ratio {ratio:.1f}:1 — heavy buy pressure (could be organic or coordinated)", severity="info"))
        elif ratio < 0.33:
            flags.append(DegenFlag(label=f"Buy/sell ratio {ratio:.2f}:1 — heavy sell pressure", severity="warning"))

    # Volume relative to liquidity — extreme ratios suggest wash trading or extreme volatility
    if pair.volume_24h_usd is not None and pair.liquidity_usd:
        vol_liq_ratio = pair.volume_24h_usd / pair.liquidity_usd
        if vol_liq_ratio > 10:
            flags.append(DegenFlag(label=f"24h volume is {vol_liq_ratio:.0f}x liquidity — extreme turnover, verify it's not wash trading", severity="warning"))

    # Price action context (informational, not a directional call)
    if pair.price_change_1h_pct is not None and abs(pair.price_change_1h_pct) > 30:
        direction = "up" if pair.price_change_1h_pct > 0 else "down"
        flags.append(DegenFlag(label=f"Price moved {abs(pair.price_change_1h_pct):.0f}% {direction} in the last hour", severity="info"))

    if not flags:
        flags.append(DegenFlag(label="No major red flags detected in available data — still treat as high risk by category", severity="info"))

    return DegenSnapshot(
        symbol=pair.base_symbol,
        token_address=pair.base_token_address,
        chain_id=pair.chain_id,
        price_usd=pair.price_usd,
        liquidity_usd=pair.liquidity_usd,
        volume_24h_usd=pair.volume_24h_usd,
        price_change_1h_pct=pair.price_change_1h_pct,
        price_change_24h_pct=pair.price_change_24h_pct,
        buy_sell_ratio_24h=pair.txns_24h.buy_sell_ratio if pair.txns_24h else None,
        pair_age_hours=age_hours,
        flags=flags,
    )
