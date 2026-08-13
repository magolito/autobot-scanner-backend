"""
Scanner orchestrator.

This is the single entry point that ties data sources -> filters ->
factors -> scoring -> risk tier into one ScanResult per coin. Each stage
is independently importable and testable (see tests/), but this is what
you actually call to run a scan.
"""

from __future__ import annotations
import asyncio
from types import SimpleNamespace
from typing import List, Optional

from .config import ScannerConfig
from .models import MarketSnapshot, ScanResult
from .filters import passes_quality_filters
from .risk import classify_risk_tier, compute_realized_volatility
from .scoring import combine_factors
from .regime import compute_market_regime, apply_regime_filter, RegimeResult
from .correlation import compute_correlation_clusters
from .factors import compute_strength, compute_oi_dynamics, compute_momentum, compute_social
from .data_sources.exchange import ExchangeDataSource
from .data_sources.social import SocialDataSource
from .data_sources.whale import WhaleDataSource


def apply_symbol_lists(bases: List[str], blacklist: Optional[List[str]] = None, whitelist: Optional[List[str]] = None) -> List[str]:
    """
    Blacklist always wins. If a non-empty whitelist is given, only symbols
    in it are scanned (blacklist still applies on top of that). Comparison
    is case-insensitive; the original casing of `bases` is preserved in
    the output.
    """
    blacklist_upper = {s.upper() for s in (blacklist or [])}
    whitelist_upper = {s.upper() for s in (whitelist or [])}

    filtered = [b for b in bases if b.upper() not in blacklist_upper]
    if whitelist_upper:
        filtered = [b for b in filtered if b.upper() in whitelist_upper]
    return filtered


class OpportunityScanner:
    def __init__(
        self,
        config: Optional[ScannerConfig] = None,
        whale_api_key: Optional[str] = None,
        cache_ttls: Optional[dict] = None,
    ):
        self.config = config or ScannerConfig()
        self.exchange_source = ExchangeDataSource(self.config, cache_ttls=cache_ttls)
        self.social_source = SocialDataSource(self.config.lunarcrush_api_key)
        self.whale_source = WhaleDataSource(whale_api_key)

    async def close(self):
        await self.exchange_source.close()
        await self.social_source.close()
        await self.whale_source.close()

    async def get_whale_context(self, base: str) -> dict:
        """Supplementary context, not part of the composite score — see data_sources/whale.py."""
        return await self.whale_source.summarize_for_symbol(base)

    async def scan_symbol(
        self,
        base: str,
        market_cap_usd: Optional[float] = None,
        market_cap_rank: Optional[int] = None,
        exchange_listings: int = 1,
        btc_snapshot: Optional[MarketSnapshot] = None,
        regime: Optional[RegimeResult] = None,
    ) -> ScanResult:
        snap = await self.exchange_source.build_snapshot(
            base=base, market_cap_usd=market_cap_usd, exchange_listings=exchange_listings,
        )
        snap.social = await self.social_source.get_social_blob(base)

        passed, filter_notes = passes_quality_filters(snap, self.config.filters)

        price_change_24h_pct = None
        daily = snap.ohlcv.get("1d")
        if daily is not None and len(daily) >= 2:
            prev_close = daily["close"].iloc[-2]
            if prev_close:
                price_change_24h_pct = (snap.price / prev_close - 1.0) * 100.0

        # Real fix for a live request ("chart of the last 24h") — reuses
        # the 1h OHLCV already fetched for momentum, no extra network
        # call. Capped to the last 24 hourly closes (a real, not
        # decorative, recent price path); left empty if 1h data isn't
        # available rather than fabricating a flat line.
        hourly = snap.ohlcv.get("1h")
        recent_prices = (
            [float(v) for v in hourly["close"].tail(24).tolist()]
            if hourly is not None and len(hourly) > 0
            else []
        )

        sector_bases = self.config.sector_peers(base)
        sector_snapshots = {}
        if sector_bases:
            # Lightweight peer fetch — see fetch_ohlcv_for_relative_strength's
            # docstring for the full reasoning. _relative_strength() only
            # ever reads peer_snap.ohlcv.get(tf), so a plain namespace with
            # just that attribute is all it needs — no reason to build or
            # fake a full MarketSnapshot for data that's never read.
            peer_ohlcv_results = await asyncio.gather(
                *[self.exchange_source.fetch_ohlcv_for_relative_strength(peer) for peer in sector_bases],
                return_exceptions=True,
            )
            sector_snapshots = {
                peer: SimpleNamespace(ohlcv=ohlcv)
                for peer, ohlcv in zip(sector_bases, peer_ohlcv_results)
                if not isinstance(ohlcv, Exception)
            }

        # Diagnostic logging added directly from a live report: oi_dynamics
        # shows unavailable for nearly every coin, every scan, but nothing
        # in the logs explains WHY for regular (non-meme) coins — no
        # explicit OI fetch failure is ever logged for them, unlike meme
        # coins where "does not have market symbol" is clear. This
        # exposes the actual raw values feeding the availability check
        # directly, so the next real scan's logs answer the question
        # definitively instead of guessing further from indirect evidence.
        print(
            f"[scanner] {base} OI inputs: open_interest_usd={snap.open_interest_usd}, "
            f"funding_rate={snap.funding_rate}, long_short_ratio={snap.long_short_ratio}"
        )

        factors = {
            "strength": compute_strength(snap, btc_snapshot, sector_bases, sector_snapshots),
            "oi_dynamics": compute_oi_dynamics(snap, price_change_24h_pct),
            "momentum": compute_momentum(snap, self.config.timeframe_config),
            "social": compute_social(snap),
        }
        unavailable = [name for name, f in factors.items() if not f.available]
        if unavailable:
            print(f"[scanner] {base}: {len(unavailable)}/4 pillars unavailable this scan ({', '.join(unavailable)}) — weight redistributed to the remaining pillars")

        composite, confidence, confidence_label, signal, weights_used, reasons_summary = combine_factors(
            factors, self.config.weights, self.config.signal_bands, self.config.confidence_bands
        )

        risk_tier = classify_risk_tier(
            market_cap_rank=market_cap_rank,
            market_cap_usd=market_cap_usd,
            volume_24h_usd=snap.volume_24h_usd,
            realized_volatility_annualized=compute_realized_volatility(snap.ohlcv.get("1d")),
        )

        regime_label, regime_score, regime_note, score_before = "Unknown", None, None, None
        final_signal = signal
        if regime is not None:
            is_btc = base.upper() == "BTC"
            adjusted, note = apply_regime_filter(
                composite, regime, self.config.regime_config, is_btc_itself=is_btc,
                relative_strength_score=factors["strength"].raw.get("rs_score") if factors["strength"].available else None,
            )
            regime_label = regime.label
            regime_score = regime.score
            if note is not None:
                score_before = composite
                composite = adjusted
                final_signal = self.config.signal_bands.grade(composite)
                regime_note = note
                reasons_summary.insert(0, f"[regime] {note}")

        return ScanResult(
            symbol=snap.symbol,
            base=base,
            price=snap.price,
            composite_score=composite,
            confidence=confidence,
            confidence_label=confidence_label,
            signal=final_signal,
            factors=factors,
            weights_used=weights_used,
            reasons_summary=reasons_summary,
            risk_tier=risk_tier,
            passed_filters=passed,
            filter_notes=filter_notes,
            regime_label=regime_label,
            regime_score=regime_score,
            regime_adjustment_note=regime_note,
            score_before_regime_adjustment=score_before,
            price_change_24h_pct=price_change_24h_pct,
            recent_prices=recent_prices,
        )

    async def scan_many(
        self,
        bases: List[str],
        market_caps: Optional[dict] = None,
        market_cap_ranks: Optional[dict] = None,
        exchange_listings: Optional[dict] = None,
        include_filtered: bool = False,
        blacklist: Optional[List[str]] = None,
        whitelist: Optional[List[str]] = None,
    ) -> List[ScanResult]:
        market_caps = market_caps or {}
        market_cap_ranks = market_cap_ranks or {}
        exchange_listings = exchange_listings or {}

        bases = apply_symbol_lists(bases, blacklist, whitelist)
        if not bases:
            return []

        # Activate scan-cycle memoization for the whole batch — see
        # ExchangeDataSource.build_snapshot's docstring for the real bug
        # this fixes (redundant sector-peer re-fetching multiplying total
        # work 5-8x, the actual cause of a live 10-30 minute scan report,
        # not just a concurrency limit issue). Guaranteed to deactivate
        # even if the scan raises, so a failed scan can't leave memoization
        # permanently stuck on for later one-off calls.
        self.exchange_source.start_scan_cycle()
        try:
            # Load Hyperliquid's market list once, explicitly, before any
            # concurrent fetches fire — see ensure_markets_loaded's
            # docstring for the exact race this closes.
            await self.exchange_source.ensure_markets_loaded()

            # BTC snapshot first — every other coin's relative strength needs it,
            # and the regime filter is computed from BTC's own momentum + volatility
            btc_snap = await self.exchange_source.build_snapshot(base="BTC")
            regime = compute_market_regime(btc_snap, self.config.timeframe_config, self.config.regime_config)

            tasks = [
                self.scan_symbol(
                    base=b,
                    market_cap_usd=market_caps.get(b),
                    market_cap_rank=market_cap_ranks.get(b),
                    exchange_listings=exchange_listings.get(b, 1),
                    btc_snapshot=btc_snap,
                    regime=regime,
                )
                for b in bases
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # Correlation clustering — the actual answer to "if five
            # 'Ready' signals are all just following BTC, that's one bet
            # expressed five times, not five independent opportunities."
            # Reuses the STILL-ACTIVE scan-cycle memoization to gather
            # each successfully-scanned coin's daily OHLCV — a cache hit
            # from the scoring pass just completed, not a new network
            # call, which is exactly why this runs here, before
            # end_scan_cycle() deactivates that cache.
            successful_bases = [base for base, r in zip(bases, results) if not isinstance(r, Exception)]
            daily_ohlcv_by_base: dict = {}
            for base in successful_bases:
                try:
                    snap = await self.exchange_source.build_snapshot(base=base)
                    daily_ohlcv_by_base[base] = snap.ohlcv.get("1d")
                except Exception as e:  # noqa: BLE001
                    print(f"[scanner] correlation snapshot lookup failed for {base}: {e}")
                    daily_ohlcv_by_base[base] = None
            clusters = compute_correlation_clusters(daily_ohlcv_by_base)
            for base, r in zip(bases, results):
                if not isinstance(r, Exception):
                    r.correlated_peers = clusters.get(base, [])
        finally:
            self.exchange_source.end_scan_cycle()

        clean: List[ScanResult] = []
        for base, r in zip(bases, results):
            if isinstance(r, Exception):
                print(f"[scanner] scan failed for {base}: {r}")
                continue
            if r.passed_filters or include_filtered:
                clean.append(r)

        clean.sort(key=lambda r: r.composite_score, reverse=True)
        return clean
