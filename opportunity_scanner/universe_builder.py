"""
Universe builder — decide WHAT to scan before spending calls on HOW it looks.

The scanner's cost is per-coin: four timeframes of OHLCV, open interest,
funding, social. So the cheapest way to scan "the whole market" is to
never spend that budget on coins where the answer can't be meaningful.

Two stages:

  1. One CoinGecko call returns the top N coins with volume and market cap.
     Filter on liquidity here — zero per-coin cost.
  2. Rank what survived, and hand the scanner a list it can actually
     finish.

Why a liquidity floor and not just "top 20 by volume": the top 20 is what
everyone already watches. Telling a member BTC is strong isn't worth much.
The edge is in coins liquid enough for technicals to mean something but
not so obvious that the move already happened. That's the band this
targets.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


# Stablecoins and wrapped assets never produce a tradeable signal — they're
# pegged. Scanning them is pure waste, and they rank high on volume.
EXCLUDE = {
    "USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "PYUSD", "USDS", "RLUSD",
    "BUSD", "USDD", "FRAX", "LUSD", "GUSD", "EURC", "USD1",
    "WBTC", "WETH", "WBETH", "WEETH", "STETH", "WSTETH", "RETH", "CBBTC",
    "SOLVBTC", "LBTC", "BSC-USD", "WBT",
}

# Tickers that no longer trade under that symbol. Every scan spends four
# failed lookups on these otherwise.
RENAMED = {"MATIC": "POL", "FTM": "S"}


@dataclass
class UniverseConfig:
    """Tunable knobs. The defaults are the ones I'd actually run."""
    discovery_top_n: int = 250          # one CoinGecko call, no per-coin cost
    min_volume_24h_usd: float = 20_000_000
    min_market_cap_usd: float = 50_000_000
    max_symbols: int = 80               # what stage 2 will actually analyse
    always_include: tuple = ("BTC", "ETH", "SOL")   # context, always scanned
    exclude: frozenset = frozenset(EXCLUDE)


def _get(row: dict, *names, default=None):
    """Providers disagree about key names; try the likely ones."""
    for n in names:
        if n in row and row[n] is not None:
            return row[n]
    return default


def build_universe(overview: dict, config: Optional[UniverseConfig] = None) -> dict:
    """
    overview: {BASE: {market_cap_usd, volume_24h_usd, market_cap_rank, ...}}
              — exactly what CoinGeckoDiscoveryProvider.get_market_overview
              already returns.

    Returns {"symbols": [...], "market_caps": {...}, "market_cap_ranks": {...},
             "stats": {...}} ready to hand straight to scan_many.
    """
    cfg = config or UniverseConfig()

    considered = 0
    rejected_illiquid = 0
    rejected_excluded = 0
    candidates = []

    for base, row in (overview or {}).items():
        base = str(base).upper()
        considered += 1

        if base in cfg.exclude:
            rejected_excluded += 1
            continue

        base = RENAMED.get(base, base)

        vol = _get(row, "volume_24h_usd", "total_volume", "volume_24h", default=0) or 0
        mcap = _get(row, "market_cap_usd", "market_cap", default=0) or 0
        rank = _get(row, "market_cap_rank", "rank")

        try:
            vol, mcap = float(vol), float(mcap)
        except (TypeError, ValueError):
            continue

        if vol < cfg.min_volume_24h_usd or mcap < cfg.min_market_cap_usd:
            rejected_illiquid += 1
            continue

        # Volume relative to size: a coin turning over a large share of its
        # market cap is where attention actually is right now. This is the
        # ranking signal that isn't just "biggest first".
        turnover = (vol / mcap) if mcap else 0

        candidates.append({
            "base": base,
            "volume_24h_usd": vol,
            "market_cap_usd": mcap,
            "market_cap_rank": rank,
            "turnover": turnover,
        })

    # Rank by turnover, but keep it honest: a coin needs real volume too,
    # so score blends turnover with absolute volume rather than either alone.
    if candidates:
        max_vol = max(c["volume_24h_usd"] for c in candidates) or 1
        max_turn = max(c["turnover"] for c in candidates) or 1
        for c in candidates:
            c["priority"] = (
                0.6 * (c["turnover"] / max_turn) +
                0.4 * (c["volume_24h_usd"] / max_vol)
            )
        candidates.sort(key=lambda c: c["priority"], reverse=True)

    chosen = [c["base"] for c in candidates[: cfg.max_symbols]]

    # majors always scanned — they're the market's context even when quiet
    for base in cfg.always_include:
        if base not in chosen:
            chosen.insert(0, base)
    chosen = list(dict.fromkeys(chosen))[: cfg.max_symbols + len(cfg.always_include)]

    by_base = {c["base"]: c for c in candidates}
    stats = {
        "considered": considered,
        "rejected_excluded": rejected_excluded,
        "rejected_illiquid": rejected_illiquid,
        "passed_liquidity": len(candidates),
        "selected": len(chosen),
    }
    log.info(
        "[universe] %d considered → %d liquid → scanning %d "
        "(dropped %d stable/wrapped, %d illiquid)",
        considered, len(candidates), len(chosen), rejected_excluded, rejected_illiquid,
    )

    return {
        "symbols": chosen,
        "market_caps": {b: by_base[b]["market_cap_usd"] for b in chosen if b in by_base},
        "market_cap_ranks": {
            b: by_base[b]["market_cap_rank"] for b in chosen
            if b in by_base and by_base[b]["market_cap_rank"] is not None
        },
        "stats": stats,
    }


def tiered_universe(overview: dict, tier: str = "full",
                    config: Optional[UniverseConfig] = None) -> dict:
    """
    Most coins don't change character in fifteen minutes. Scanning the
    majors often and the tail hourly costs a fraction of scanning
    everything constantly, and loses almost nothing.

        "core"  — top 20 by priority, for frequent runs
        "full"  — everything that passed the liquidity floor
    """
    built = build_universe(overview, config)
    if tier == "core":
        built["symbols"] = built["symbols"][:20]
        built["stats"]["selected"] = len(built["symbols"])
    return built
