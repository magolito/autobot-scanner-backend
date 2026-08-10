"""
Main CLI scanner — run a scan against live data and print a ranked,
explainable table.

Usage:
    python -m opportunity_scanner.main
    python -m opportunity_scanner.main --symbols BTC,ETH,SOL --min-score 60
    python -m opportunity_scanner.main --config /path/to/config.yaml

This wires together everything built so far: settings.py (YAML+env config)
-> scanner.py (async pipeline: data sources -> filters -> factors ->
regime -> scoring) -> a rich-rendered table with per-coin explanations.
It's the "does this whole thing actually work end to end" entry point.
"""

from __future__ import annotations
import argparse
import asyncio
import sys
from typing import List, Optional

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text

from .settings import load_settings
from .scanner import OpportunityScanner
from .models import ScanResult
from .logging_config import setup_logging_from_settings

console = Console()

SIGNAL_COLORS = {
    "Strong Buy": "bold green",
    "Buy": "green",
    "Neutral": "yellow",
    "Caution": "dark_orange",
    "Strong Avoid": "bold red",
}

RISK_COLORS = {
    "core": "cyan",
    "small_cap": "yellow",
    "high_risk": "bold red",
}


def _signal_text(signal: str) -> Text:
    return Text(signal, style=SIGNAL_COLORS.get(signal, "white"))


def _risk_text(risk_tier: str) -> Text:
    label = {"core": "Core", "small_cap": "Small Cap", "high_risk": "High Risk"}.get(risk_tier, risk_tier)
    return Text(label, style=RISK_COLORS.get(risk_tier, "white"))


def render_results_table(results: List[ScanResult], regime_label: Optional[str] = None) -> None:
    if regime_label:
        regime_style = {"Risk-On": "green", "Neutral": "yellow", "Risk-Off": "red"}.get(regime_label, "white")
        console.print(Panel(f"Market Regime: [{regime_style}]{regime_label}[/{regime_style}]", expand=False))

    table = Table(title="Opportunity Scanner Results", show_lines=False)
    table.add_column("#", justify="right", style="dim", width=3)
    table.add_column("Symbol", style="bold", no_wrap=True)
    table.add_column("Score", justify="right", width=6)
    table.add_column("Signal", no_wrap=True)
    table.add_column("Conf", justify="right", width=5)
    table.add_column("Risk", no_wrap=True)
    table.add_column("Str", justify="right", style="dim", width=4)
    table.add_column("OI", justify="right", style="dim", width=4)
    table.add_column("Mom", justify="right", style="dim", width=4)
    table.add_column("Soc", justify="right", style="dim", width=4)

    for i, r in enumerate(results, 1):
        f = r.factors
        table.add_row(
            str(i),
            r.base,
            f"{r.composite_score:.1f}",
            _signal_text(r.signal),
            f"{r.confidence:.0f}",
            _risk_text(r.risk_tier),
            f"{f['strength'].score:.0f}" if f['strength'].available else "—",
            f"{f['oi_dynamics'].score:.0f}" if f['oi_dynamics'].available else "—",
            f"{f['momentum'].score:.0f}" if f['momentum'].available else "—",
            f"{f['social'].score:.0f}" if f['social'].available else "—",
        )

    console.print(table)
    console.print("[dim]Conf = confidence score (0-100). Str/OI/Mom/Soc = per-pillar scores, '—' = pillar unavailable for this coin.[/dim]")

    console.print("\n[bold]Explanations:[/bold]")
    for i, r in enumerate(results, 1):
        console.print(f"\n[bold]{i}. {r.base}[/bold] — {r.composite_score:.1f} ({r.signal})")
        for reason in r.reasons_summary:
            console.print(f"   • {reason}")
        if r.regime_adjustment_note:
            console.print(f"   [yellow]⚠ {r.regime_adjustment_note}[/yellow]")
        if not r.passed_filters:
            console.print(f"   [red]Filtered out: {'; '.join(r.filter_notes)}[/red]")


async def run_scan(symbols: Optional[List[str]], min_score: float, config_path: Optional[str]) -> List[ScanResult]:
    settings = load_settings()
    setup_logging_from_settings(settings)
    scanner_config = settings.to_scanner_config()

    universe = symbols or settings.universe.default
    scanner = OpportunityScanner(
        scanner_config,
        whale_api_key=settings.whale_alert_api_key,
        cache_ttls=settings.to_cache_ttls(),
    )

    try:
        results = await scanner.scan_many(
            universe, include_filtered=True,
            blacklist=settings.universe.blacklist, whitelist=settings.universe.whitelist,
        )
    finally:
        await scanner.close()

    filtered = [r for r in results if r.composite_score >= min_score]
    return filtered


def main():
    parser = argparse.ArgumentParser(description="AutoBot Opportunity Scanner — CLI")
    parser.add_argument("--symbols", type=str, default=None, help="Comma-separated base symbols, e.g. BTC,ETH,SOL")
    parser.add_argument("--min-score", type=float, default=0.0, help="Only show coins scoring at or above this")
    parser.add_argument("--config", type=str, default=None, help="Path to a config.yaml (defaults to project root)")
    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else None

    console.print("[dim]Running scan...[/dim]")
    try:
        results = asyncio.run(run_scan(symbols, args.min_score, args.config))
    except Exception as e:  # noqa: BLE001
        console.print(f"[bold red]Scan failed:[/bold red] {e}")
        sys.exit(1)

    if not results:
        console.print("[yellow]No results — check your min-score threshold or network/API key configuration.[/yellow]")
        return

    regime_label = results[0].regime_label if results else None
    render_results_table(results, regime_label)


if __name__ == "__main__":
    main()
