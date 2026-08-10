"""
Meme coin scanner — main entry point.

Usage:
    python -m opportunity_scanner.meme_main
    python -m opportunity_scanner.meme_main --addresses <mint1>,<mint2>
    python -m opportunity_scanner.meme_main --mode sniper --min-score 70

Wires together everything built in Phases 1-3: settings.yaml (mode +
thresholds) -> discovery (DexScreener boosts feed + watchlist) ->
meme_aggregator.py (DexScreener + RugCheck + GoPlus -> MemeCoinMetrics)
-> meme_scoring_engine.py (safety gate -> pillars -> opportunity score)
-> filtered, explained console output.

The "only high-quality candidates" requirement is enforced in two
places, not one: the safety gate itself (a Fail is never shown,
period — there's no threshold that overrides that), and the
opportunity-score filter (settings.yaml's min_opportunity_score_to_show)
on top of whatever passes/cautions through the gate.
"""

from __future__ import annotations
import argparse
import asyncio
from typing import List, Optional

from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from .settings import load_settings, Settings
from .data_sources.dexscreener import DexScreenerProvider
from .data_sources.rugcheck import RugCheckProvider
from .data_sources.goplus import GoPlusProvider
from .data_sources.social import SocialDataSource
from .meme_aggregator import MemeDataAggregator
from .meme_scoring_engine import ScoringEngine, FinalMemeResult, Mode, RiskFlag
from .meme_storage import MemeScanStorage
from .meme_hype_events import detect_hype_events, detect_hype_score_jump
from .meme_alerts import MemeAlertDispatcher

console = Console()

SAFETY_COLOR = {"Pass": "green", "Caution": "yellow", "Fail": "red"}
HYPE_COLOR = {"Explosive": "bold magenta", "High": "green", "Medium": "yellow", "Low": "dim"}


async def discover_candidates(settings: Settings, dex: DexScreenerProvider) -> List[str]:
    """
    Builds the candidate list for this scan cycle. Two sources, combined
    and de-duplicated: the watchlist (always checked, regardless of
    whether it's currently trending) and DexScreener's real-time boosted/
    trending feed (the actual discovery mechanism — new candidates show
    up here without anyone having to add them manually).
    """
    candidates = list(settings.meme_scanner.discovery.watchlist)

    if settings.meme_scanner.discovery.use_dexscreener_boosts:
        try:
            boosted = await dex.get_boosted_token_addresses(settings.meme_scanner.chain_id)
            for addr in boosted:
                if addr not in candidates:
                    candidates.append(addr)
        except Exception as e:  # noqa: BLE001
            console.print(f"[yellow]Warning: couldn't fetch boosted tokens: {e}[/yellow]")

    cap = settings.meme_scanner.discovery.max_candidates_per_scan
    return candidates[:cap]


async def run_scan(settings: Settings, mode: Mode, addresses: Optional[List[str]] = None) -> List[FinalMemeResult]:
    dex = DexScreenerProvider(
        cache_ttl_seconds=settings.meme_scanner.resilience.dexscreener_cache_ttl_seconds,
        failure_threshold=settings.meme_scanner.resilience.dexscreener_breaker.failure_threshold,
        cooldown_seconds=settings.meme_scanner.resilience.dexscreener_breaker.cooldown_seconds,
    )
    rugcheck = RugCheckProvider(
        cache_ttl_seconds=settings.meme_scanner.resilience.rugcheck_cache_ttl_seconds,
        failure_threshold=settings.meme_scanner.resilience.rugcheck_breaker.failure_threshold,
        cooldown_seconds=settings.meme_scanner.resilience.rugcheck_breaker.cooldown_seconds,
    )
    goplus = GoPlusProvider(
        cache_ttl_seconds=settings.meme_scanner.resilience.goplus_cache_ttl_seconds,
        failure_threshold=settings.meme_scanner.resilience.goplus_breaker.failure_threshold,
        cooldown_seconds=settings.meme_scanner.resilience.goplus_breaker.cooldown_seconds,
    )
    social = SocialDataSource(settings.lunarcrush_api_key) if settings.lunarcrush_api_key else None
    aggregator = MemeDataAggregator(dex, rugcheck, goplus, social=social)

    storage = MemeScanStorage(settings.meme_scanner.db_path)
    deployer_blacklist = set(storage.get_all_blacklisted_deployers_sync())
    engine = ScoringEngine(mode=mode, config=settings.to_meme_engine_config(), deployer_blacklist=deployer_blacklist)

    dispatcher: Optional[MemeAlertDispatcher] = None
    if settings.meme_scanner.alerts.enabled:
        dispatcher = MemeAlertDispatcher(
            telegram_bot_token=settings.telegram_bot_token, telegram_chat_id=settings.telegram_chat_id,
            discord_webhook_url=settings.discord_webhook_url,
            min_opportunity_score=settings.meme_scanner.alerts.min_opportunity_score,
            require_pass_grade_only=settings.meme_scanner.alerts.require_pass_grade_only,
            cooldown_minutes=settings.meme_scanner.alerts.cooldown_minutes,
        )

    try:
        candidate_addresses = addresses or await discover_candidates(settings, dex)
        if not candidate_addresses:
            console.print("[yellow]No candidates found — watchlist is empty and DexScreener boosts returned nothing.[/yellow]")
            return []

        console.print(f"[dim]Scanning {len(candidate_addresses)} candidate(s) in {mode.value} mode...[/dim]")

        results: List[FinalMemeResult] = []
        for addr in candidate_addresses:
            try:
                built = await aggregator.build_metrics(addr, settings.meme_scanner.chain_id)
            except Exception as e:  # noqa: BLE001
                console.print(f"[dim red]Skipped {addr[:12]}...: {e}[/dim red]")
                continue
            if built is None:
                continue  # no DexScreener data at all for this address
            metrics, quality_notes = built

            # Hype event detection needs the PRIOR scan, fetched before this
            # one gets scored/saved — comparing metrics to what was true last time
            previous_scan = storage.get_previous_scan_sync(addr)
            hype_events = detect_hype_events(metrics, previous_scan)

            result = engine.score(metrics)
            quality_flags = [RiskFlag(label=note, severity="warning") for note in quality_notes]
            result.risk_flags = quality_flags + result.risk_flags

            score_jump_event = detect_hype_score_jump(result.pillar_scores.hype, previous_scan) if result.pillar_scores else None
            if score_jump_event:
                hype_events.append(score_jump_event)
            result.hype_events = hype_events

            storage.save_result_sync(
                result, price_usd=metrics.price_usd, mention_velocity_ratio=metrics.mention_velocity_ratio,
                dex_boosted=metrics.dex_boosted, kol_score=metrics.kol_score, deployer_address=metrics.deployer_address,
            )

            # Auto-blacklist: a token that fails safety specifically because
            # of an insider/bundle flag or honeypot is real evidence against
            # its deployer, worth remembering for every future token from
            # that same wallet — not just this one scan.
            if metrics.deployer_address and result.safety.grade == "Fail":
                fail_reasons_lower = " ".join(result.safety.reasons).lower()
                if "insider" in fail_reasons_lower or "bundle" in fail_reasons_lower or "honeypot" in fail_reasons_lower:
                    storage.add_to_deployer_blacklist_sync(
                        metrics.deployer_address,
                        reason=f"Auto-flagged from {metrics.symbol}: {result.safety.reasons[0]}",
                        auto_detected=True, source_token_address=addr,
                    )

            if dispatcher is not None:
                await dispatcher.process_result(result, hype_events)

            results.append(result)

        return results
    finally:
        await aggregator.close()
        if dispatcher is not None:
            await dispatcher.close()


def filter_high_quality(results: List[FinalMemeResult], settings: Settings) -> List[FinalMemeResult]:
    """
    The "only high-quality candidates" gate. Two independent filters:
    Safety=Fail is ALWAYS excluded, no threshold overrides it — that's
    the whole point of a gatekeeper. On top of that, whatever the
    opportunity score threshold in settings.yaml says.
    """
    out = []
    for r in results:
        if r.safety.grade == "Fail":
            continue
        if r.safety.grade == "Caution" and not settings.meme_scanner.show_caution_grade:
            continue
        if r.opportunity_score is not None and r.opportunity_score < settings.meme_scanner.min_opportunity_score_to_show:
            continue
        out.append(r)
    out.sort(key=lambda r: r.opportunity_score or 0, reverse=True)
    return out


def render_results(results: List[FinalMemeResult], mode: Mode, total_scanned: int) -> None:
    console.print(Panel(f"Mode: [bold]{mode.value}[/bold] · {len(results)} high-quality of {total_scanned} scanned", expand=False))

    if not results:
        console.print("[yellow]No candidates cleared the safety gate + quality threshold this cycle.[/yellow]")
        return

    table = Table(title="High-Quality Meme Candidates", show_lines=True)
    table.add_column("Symbol", style="bold")
    table.add_column("Safety")
    table.add_column("Score", justify="right")
    table.add_column("Hype")
    table.add_column("Confidence", justify="right")
    table.add_column("Hype/Chain/Mom", justify="right")

    for r in results:
        safety_color = SAFETY_COLOR.get(r.safety.grade, "white")
        hype_color = HYPE_COLOR.get(r.hype_level or "", "white")
        pillars = f"{r.pillar_scores.hype:.0f}/{r.pillar_scores.onchain_health:.0f}/{r.pillar_scores.momentum:.0f}" if r.pillar_scores else "—"
        table.add_row(
            r.symbol,
            f"[{safety_color}]{r.safety.grade}[/{safety_color}]",
            f"{r.opportunity_score:.1f}" if r.opportunity_score is not None else "—",
            f"[{hype_color}]{r.hype_level or '—'}[/{hype_color}]",
            f"{r.confidence:.0f}",
            pillars,
        )

    console.print(table)

    console.print("\n[bold]Explanations:[/bold]")
    for r in results:
        console.print(f"\n[bold]{r.symbol}[/bold] ({r.token_address[:16]}...) — {r.thesis}")
        for flag in r.risk_flags[:4]:
            flag_color = {"danger": "red", "warning": "yellow", "info": "dim"}.get(flag.severity, "white")
            console.print(f"   [{flag_color}]⚠ {flag.label}[/{flag_color}]")
        for event in r.hype_events[:3]:
            console.print(f"   [magenta]🔥 {event.label}[/magenta]")


def main():
    parser = argparse.ArgumentParser(description="Meme Coin Scanner — CLI")
    parser.add_argument("--addresses", type=str, default=None, help="Comma-separated token addresses (overrides discovery)")
    parser.add_argument("--mode", type=str, default=None, help="sniper | early_momentum | confirmed_runner (overrides settings.yaml)")
    parser.add_argument("--min-score", type=float, default=None, help="Override min_opportunity_score_to_show")
    args = parser.parse_args()

    settings = load_settings()
    if args.mode:
        settings.meme_scanner.mode = args.mode
    if args.min_score is not None:
        settings.meme_scanner.min_opportunity_score_to_show = args.min_score

    mode = settings.to_meme_mode()
    addresses = [a.strip() for a in args.addresses.split(",")] if args.addresses else None

    all_results = asyncio.run(run_scan(settings, mode, addresses))
    high_quality = filter_high_quality(all_results, settings)
    render_results(high_quality, mode, total_scanned=len(all_results))


if __name__ == "__main__":
    main()
