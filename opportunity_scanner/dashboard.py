"""
AutoBot Opportunity Scanner — Streamlit dashboard.

Run: streamlit run opportunity_scanner/dashboard.py

Design language matches autobotpro.trading: off-black backgrounds,
Playfair Display for headline numbers, DM Mono for labels/data, gold
hairline accents. Streamlit doesn't support custom fonts/most CSS out of
the box, so this injects the site's actual design tokens via a CSS block
rather than trying to reskin Streamlit's defaults into something that
merely resembles the brand.

Architecture note: OpportunityScanner is async; Streamlit's execution
model is synchronous (top-to-bottom rerun on every interaction). Each
scan is triggered explicitly (the "Scan Now" button or the mode
selector), wrapped in `asyncio.run()`, and results are cached in
`st.session_state` so reruns from OTHER interactions (moving a weight
slider, selecting a coin) don't re-fetch data — they just re-render or
re-score from what's already in memory. Re-weighting is a good example:
moving a pillar weight slider re-runs `combine_factors()` on the
already-fetched FactorResults instantly, no network call at all.
"""

from __future__ import annotations
import asyncio
import os
import sys

# Defensive sys.path fix — discovered via a live deployment failure, not
# theoretical. This exact script (streamlit run opportunity_scanner/
# dashboard.py from the project root) worked correctly in local testing,
# but failed with "ModuleNotFoundError: No module named 'opportunity_
# scanner'" on Railway specifically — evidence that Streamlit's own
# sys.path setup for a script at a relative subpath isn't consistent
# across every hosting environment. Rather than depend on that behavior
# (or on PYTHONPATH being set correctly by whatever's launching this),
# explicitly ensure the project root — the parent of this file's own
# directory — is on sys.path before any opportunity_scanner import runs.
import dataclasses
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opportunity_scanner.settings import load_settings
from opportunity_scanner.scanner import OpportunityScanner
from opportunity_scanner.scoring import combine_factors
from opportunity_scanner.config import Weights
from opportunity_scanner.config import UNIVERSE_PRESETS, DEFAULT_UNIVERSE_PRESET
from opportunity_scanner.storage import ScanStorage
from opportunity_scanner.models import ScanResult, FactorResult
from opportunity_scanner.smart_view import Bucket, bucket_results, BUCKET_LABELS, data_completeness

# ----------------------------------------------------------------- page setup

st.set_page_config(
    page_title="AutoBot Scanner",
    page_icon="●",
    layout="wide",
    initial_sidebar_state="collapsed",
)

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,400;0,500;1,400;1,500&family=DM+Sans:wght@300;400;500&family=DM+Mono:wght@300;400&display=swap');

:root {
    --black: #0e0e0c;
    --bg: #0a0a08;
    --white: #f5f4f0;
    --gray: #8c8c89;
    --gray-mid: #b8b7b2;
    --border: rgba(245,244,240,0.08);
    --border-strong: rgba(245,244,240,0.16);
    --green: #4ade80;
    --amber: #fbbf24;
    --red: #f87171;
    --gold: rgba(180,155,100,0.6);
}

html, body, [class*="css"] { font-family: 'DM Sans', sans-serif; }
.stApp {
    background: var(--bg);
    background-image: radial-gradient(ellipse at 15% 0%, rgba(180,155,100,0.04) 0%, transparent 50%);
    color: var(--white);
}
#MainMenu, footer, header {visibility: hidden;}

/* Top bar */
.topbar {
    display: flex; align-items: center; justify-content: space-between;
    padding: 18px 4px; border-bottom: 1px solid var(--border); margin-bottom: 24px;
}
.topbar-logo { font-family: 'DM Sans', sans-serif; font-weight: 500; font-size: 17px; color: var(--white); display: flex; align-items: center; gap: 8px; }
.topbar-logo::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: var(--gold); }
.topbar-meta { font-family: 'DM Mono', monospace; font-size: 10px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--gray); }
.live-dot { display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: var(--green); margin-right: 6px; }
.live-dot.stale { background: var(--gray); }

/* Mono labels */
.mono-label { font-family: 'DM Mono', monospace; font-size: 9px; letter-spacing: 0.16em; text-transform: uppercase; color: var(--gray); margin-bottom: 10px; }

/* Signal badges */
.sig-badge { font-family: 'DM Mono', monospace; font-size: 10px; letter-spacing: 0.05em; padding: 3px 9px; border-radius: 3px; white-space: nowrap; }
.sig-strongbuy { background: rgba(74,222,128,0.12); color: var(--green); border: 1px solid rgba(74,222,128,0.25); }
.sig-buy { background: rgba(74,222,128,0.07); color: var(--green); border: 1px solid rgba(74,222,128,0.15); }
.sig-neutral { background: rgba(245,244,240,0.05); color: var(--gray-mid); border: 1px solid var(--border-strong); }
.sig-caution { background: rgba(251,191,36,0.08); color: var(--amber); border: 1px solid rgba(251,191,36,0.2); }
.sig-strongavoid { background: rgba(248,113,113,0.1); color: var(--red); border: 1px solid rgba(248,113,113,0.22); }

/* Big score display (detail view) */
.big-score { font-family: 'Playfair Display', serif; font-size: 72px; line-height: 1; color: var(--white); }
.big-score-unit { font-family: 'DM Mono', monospace; font-size: 14px; color: var(--gray); }

/* Pillar bars */
.pillar-row { margin-bottom: 16px; }
.pillar-label-row { display: flex; justify-content: space-between; font-family: 'DM Mono', monospace; font-size: 11px; color: var(--gray-mid); margin-bottom: 6px; }
.pillar-track { height: 6px; background: var(--border); border-radius: 3px; overflow: hidden; }
.pillar-fill { height: 100%; border-radius: 3px; }

/* Metric cards */
.metric-card { background: rgba(245,244,240,0.03); border: 1px solid var(--border); padding: 16px 18px; }
.metric-card .metric-label { font-family: 'DM Mono', monospace; font-size: 9px; letter-spacing: 0.1em; text-transform: uppercase; color: var(--gray); margin-bottom: 6px; }
.metric-card .metric-value { font-family: 'DM Mono', monospace; font-size: 16px; color: var(--white); }

/* Flags */
.flag-row { display: flex; gap: 10px; padding: 10px 14px; margin-bottom: 6px; border-left: 3px solid var(--border-strong); font-size: 13px; }
.flag-danger { border-left-color: var(--red); background: rgba(248,113,113,0.06); color: #ffb4b4; }
.flag-warning { border-left-color: var(--amber); background: rgba(251,191,36,0.06); color: #ffd98a; }
.flag-info { border-left-color: var(--gray); background: rgba(245,244,240,0.03); color: var(--gray-mid); }

/* Thesis box */
.thesis-box { background: rgba(245,244,240,0.03); border-left: 2px solid var(--gold); padding: 16px 20px; font-size: 14px; color: var(--gray-mid); line-height: 1.7; }

/* Alert feed rows */
.alert-row { display: flex; justify-content: space-between; padding: 10px 0; border-bottom: 1px solid var(--border); font-size: 13px; }
.alert-row .alert-symbol { font-weight: 500; color: var(--white); }
.alert-row .alert-reason { color: var(--gray-mid); font-family: 'DM Mono', monospace; font-size: 11px; }

/* Section headers */
.section-h { font-family: 'Playfair Display', serif; font-size: 20px; color: var(--white); margin-bottom: 4px; }

hr { border-color: var(--border); }

/* Login gate */
.login-wrap { display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 70vh; text-align: center; }
.login-logo { font-family: 'DM Sans', sans-serif; font-weight: 500; font-size: 15px; color: var(--white); display: flex; align-items: center; gap: 8px; margin-bottom: 28px; }
.login-logo::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: var(--gold); }
.login-title { font-family: 'Playfair Display', serif; font-size: 40px; color: var(--white); margin-bottom: 10px; }
.login-sub { font-family: 'DM Mono', monospace; font-size: 11px; letter-spacing: 0.1em; text-transform: uppercase; color: var(--gray); margin-bottom: 36px; }

/* Fatal error page */
.error-page { display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 65vh; text-align: center; padding: 0 24px; }
.error-icon { font-family: 'Playfair Display', serif; font-size: 32px; color: var(--red); margin-bottom: 20px; }
.error-title { font-family: 'Playfair Display', serif; font-size: 28px; color: var(--white); margin-bottom: 12px; }
.error-message { font-family: 'DM Sans', sans-serif; font-size: 15px; color: var(--gray-mid); line-height: 1.6; max-width: 480px; margin-bottom: 8px; }
.error-hint { font-family: 'DM Mono', monospace; font-size: 11px; color: var(--gray); letter-spacing: 0.03em; margin-top: 12px; }
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)

# ----------------------------------------------------------------- fatal error page


def render_fatal_error(title: str, message: str, hint: str = "", exc: Optional[Exception] = None):
    """
    One shared component for any startup failure (broken settings.yaml,
    unreachable/unwritable storage, etc.) so every failure mode looks
    the same to the person looking at it — professional and dark-mode
    consistent, not a raw traceback. Always calls st.stop() itself, so
    callers don't need to remember to.
    """
    st.markdown('<div class="error-page">', unsafe_allow_html=True)
    st.markdown('<div class="error-icon">⚠</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="error-title">{title}</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="error-message">{message}</div>', unsafe_allow_html=True)
    if hint:
        st.markdown(f'<div class="error-hint">{hint}</div>', unsafe_allow_html=True)
    st.markdown('</div>', unsafe_allow_html=True)

    if exc is not None:
        _, center, _ = st.columns([1, 1.4, 1])
        with center:
            with st.expander("Technical details"):
                st.code(f"{type(exc).__name__}: {exc}", language="text")
                import traceback
                st.code(traceback.format_exc(), language="text")

    st.stop()


# ----------------------------------------------------------------- settings

try:
    _settings = load_settings()
except Exception as e:  # noqa: BLE001
    render_fatal_error(
        title="Couldn't load settings.yaml",
        message="The dashboard couldn't read its configuration file. This usually means settings.yaml is missing, misplaced, or has a YAML syntax error.",
        hint="Check that settings.yaml exists in the project root and is valid YAML.",
        exc=e,
    )


from opportunity_scanner.app_storage import AppStorage
from opportunity_scanner.auth_ui import require_auth, render_logout_button
from opportunity_scanner.billing_ui import render_billing_section
from opportunity_scanner.access_control import check_scanner_access, record_scan

_app_storage = AppStorage(_settings.app_db_path)
_user = require_auth(_app_storage, product_name="AutoBot Scanner")

# ----------------------------------------------------------------- helpers

SIGNAL_CLASS = {
    "Strong Buy": "sig-strongbuy", "Buy": "sig-buy", "Neutral": "sig-neutral",
    "Caution": "sig-caution", "Strong Avoid": "sig-strongavoid",
}
SIGNAL_BAR_COLOR = {
    "Strong Buy": "#4ade80", "Buy": "#4ade80", "Neutral": "#8c8c89",
    "Caution": "#fbbf24", "Strong Avoid": "#f87171",
}

# Table-cell indicators — st.dataframe can't reliably do per-cell
# background coloring alongside row-selection, so a colored circle
# prefixed directly into the text is the practical, always-rendering
# equivalent. Matches SIGNAL_BAR_COLOR's green/gray/yellow/red scheme.
SIGNAL_INDICATOR = {
    "Strong Buy": "🟢", "Buy": "🟢", "Neutral": "⚪",
    "Caution": "🟡", "Strong Avoid": "🔴",
}
RISK_INDICATOR = {"core": "🟢", "small_cap": "🟡", "high_risk": "🔴"}

MODE_TIMEFRAME_WEIGHTS = {
    "Scalp": {"15m": 0.35, "1h": 0.35, "4h": 0.20, "1d": 0.10},
    "Swing": {"15m": 0.05, "1h": 0.15, "4h": 0.35, "1d": 0.45},
}


def signal_badge_html(signal: str) -> str:
    cls = SIGNAL_CLASS.get(signal, "sig-neutral")
    return f'<span class="sig-badge {cls}">{signal}</span>'


def derive_display_flags(result: ScanResult) -> list[dict]:
    """
    Pulls flags from the raw sub-scores each factor already computes,
    rather than inventing a parallel flag system — reuses exactly what
    factors/*.py already calculated (divergence scores, funding extremes,
    etc.) and translates them into the same info/warning/danger shape
    Degen Radar uses, for a consistent reading experience across both
    dashboards.
    """
    flags = []
    oi = result.factors.get("oi_dynamics")
    if oi and oi.available:
        div = oi.raw.get("div_score")
        if div is not None and div < 45:
            flags.append({"label": "OI diverging from price — move may not be backed by fresh positioning", "sev": "warning"})
        fund = oi.raw.get("fund_score")
        if fund is not None and fund < 40:
            flags.append({"label": "Crowded funding — elevated reversal/squeeze risk", "sev": "warning"})

    momentum = result.factors.get("momentum")
    if momentum and momentum.available:
        mraw = momentum.raw or {}
        div_score = mraw.get("divergence_score")
        if div_score == 25.0:
            flags.append({"label": "Bearish momentum divergence detected", "sev": "danger"})
        elif div_score == 75.0:
            flags.append({"label": "Bullish momentum divergence detected", "sev": "info"})

    if result.regime_adjustment_note:
        flags.append({"label": result.regime_adjustment_note, "sev": "warning"})

    if not result.passed_filters:
        for note in result.filter_notes:
            flags.append({"label": note, "sev": "danger"})

    if not flags:
        flags.append({"label": "No divergence or crowding flags detected in available data", "sev": "info"})

    return flags


async def _run_scan_async(settings, mode: str, universe: list[str]) -> list[ScanResult]:
    config = settings.to_scanner_config()
    config.timeframe_config.timeframe_weights = dict(MODE_TIMEFRAME_WEIGHTS[mode])

    # Real fix for a genuinely significant, previously-undiscovered bug:
    # risk_tier classification unconditionally returns "high_risk" when
    # market_cap_rank is missing, and this dashboard has NEVER supplied
    # market cap data to any scan, static presets or Trending Now, since
    # it was first built — every coin in every scan has always been
    # forced into high_risk regardless of its real standing. One extra
    # CoinGecko call (cached, same pattern as discovery) fixes this for
    # every genuinely-ranked coin; a coin truly outside the top 250 by
    # market cap correctly still falls through to high_risk, which is
    # the right outcome for an actually obscure/tiny asset.
    from opportunity_scanner.data_sources.coingecko_discovery import CoinGeckoDiscoveryProvider
    market_caps, market_cap_ranks = {}, {}
    mcap_provider = CoinGeckoDiscoveryProvider()
    try:
        lookup = await mcap_provider.get_market_cap_lookup(top_n=250)
        for base in universe:
            entry = lookup.get(base.upper())
            if entry:
                rank, cap = entry
                if rank is not None:
                    market_cap_ranks[base] = rank
                if cap is not None:
                    market_caps[base] = cap
    finally:
        await mcap_provider.close()

    scanner = OpportunityScanner(
        config, whale_api_key=settings.whale_alert_api_key,
        cache_ttls=settings.to_cache_ttls(),
    )
    try:
        results = await scanner.scan_many(
            universe, market_caps=market_caps, market_cap_ranks=market_cap_ranks,
            include_filtered=True,
            blacklist=settings.universe.blacklist, whitelist=settings.universe.whitelist,
        )
    finally:
        await scanner.close()
    return results


def run_scan(settings, mode: str, universe: list[str]) -> list[ScanResult]:
    return asyncio.run(_run_scan_async(settings, mode, universe))


def discover_trending_universe(max_size: int = 25) -> list[str]:
    """
    The actual fix for "the scanner only knows coins I hardcoded" — live
    discovery via CoinGecko's trending-search + top-volume rankings,
    not a fixed list. The provider has its own 15-minute cache
    internally, so repeat calls within that window are cheap; this
    function's own job is just the async->sync bridge, same pattern as
    run_scan above.
    """
    from opportunity_scanner.data_sources.coingecko_discovery import CoinGeckoDiscoveryProvider

    async def _discover():
        provider = CoinGeckoDiscoveryProvider()
        try:
            return await provider.discover_universe(max_size=max_size)
        finally:
            await provider.close()

    return asyncio.run(_discover())


def fetch_trending_overview(symbols: list[str]) -> dict:
    """
    Live price/volume/24h-range preview for the discovered universe —
    reuses the exact same cached CoinGecko fetch as the risk-tier market
    cap fix (get_market_overview shares a cache entry with
    get_market_cap_lookup), so this doesn't cost an extra network call
    beyond what a scan would already make.
    """
    from opportunity_scanner.data_sources.coingecko_discovery import CoinGeckoDiscoveryProvider

    async def _fetch():
        provider = CoinGeckoDiscoveryProvider()
        try:
            overview = await provider.get_market_overview(top_n=250)
            return {s: overview[s] for s in symbols if s in overview}
        finally:
            await provider.close()

    return asyncio.run(_fetch())


def hydrate_from_storage(storage: ScanStorage) -> tuple[list[ScanResult], Optional[datetime]]:
    """
    Rebuilds ScanResult objects from the most recent stored scan per
    symbol, so the dashboard isn't empty just because the process
    restarted. Reconstruction is necessarily partial — the DB schema
    stores the four pillar scores, weights, and reasons_summary, but not
    each factor's full `reasons` list or `raw` sub-scores (those exist
    only in-memory during a live scan). Practically this means: the main
    table, confidence, signal, and the detail modal's thesis all work
    correctly from hydrated data (reasons_summary IS stored), but the
    detail modal's flags will show "no flags detected" rather than real
    divergence/crowding flags until a fresh Scan Now populates the full
    in-memory FactorResults. Regime label/score also aren't in the
    schema (not the queried table's job), so hydrated rows show
    regime as "Unknown" rather than a stale/misleading Risk-On or
    Risk-Off — a fresh scan is what actually knows the current regime.
    """
    try:
        rows = storage.get_latest_scan_per_symbol_sync()
    except Exception as e:  # noqa: BLE001
        st.warning(f"Couldn't load previous scan history: {e}")
        return [], None

    if not rows:
        return [], None

    results = []
    latest_scanned_at = None
    for row in rows:
        try:
            weights_used = json.loads(row["weights_used_json"]) if row["weights_used_json"] else {}
        except (json.JSONDecodeError, TypeError):
            weights_used = {}
        try:
            reasons_summary = json.loads(row["reasons_summary_json"]) if row["reasons_summary_json"] else []
        except (json.JSONDecodeError, TypeError):
            reasons_summary = []

        factors = {
            name: FactorResult(
                name=name,
                score=row[f"{name}_score"] if row[f"{name}_score"] is not None else 50.0,
                reasons=[],  # not stored — see docstring
                available=row[f"{name}_score"] is not None,
            )
            for name in ["strength", "oi_dynamics", "momentum", "social"]
        }

        results.append(ScanResult(
            symbol=row["symbol"], base=row["base"], price=row["price"],
            composite_score=row["composite_score"], confidence=row["confidence"],
            confidence_label=row["confidence_label"], signal=row["signal"],
            factors=factors, weights_used=weights_used, reasons_summary=reasons_summary,
            risk_tier=row["risk_tier"], passed_filters=True,
        ))

        scanned_at = datetime.fromisoformat(row["scanned_at"])
        if latest_scanned_at is None or scanned_at > latest_scanned_at:
            latest_scanned_at = scanned_at

    results.sort(key=lambda r: r.composite_score, reverse=True)
    return results, latest_scanned_at


def load_recent_alerts(storage: ScanStorage, minutes: int = 240) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    changes = storage.get_signal_changes_sync(since)
    jumps = storage.get_score_jumps_sync(since, threshold=15.0)
    combined = [{"base": c["base"], "reason": f"{c['prev_signal']} → {c['signal']}", "scanned_at": c["scanned_at"]} for c in changes]
    combined += [{"base": j["base"], "reason": f"Score {'jumped' if j['score_delta']>0 else 'dropped'} {abs(j['score_delta']):.1f}pts", "scanned_at": j["scanned_at"]} for j in jumps]
    combined.sort(key=lambda x: x["scanned_at"], reverse=True)
    return combined[:15]


# ----------------------------------------------------------------- session state

if "results" not in st.session_state:
    st.session_state.results = []
if "last_scan_time" not in st.session_state:
    st.session_state.last_scan_time = None
if "mode" not in st.session_state:
    st.session_state.mode = "Swing"
if "weights" not in st.session_state:
    st.session_state.weights = {"strength": 0.22, "oi_dynamics": 0.28, "momentum": 0.25, "social": 0.25}

try:
    _storage = ScanStorage(_settings.storage.db_path)
except Exception as e:  # noqa: BLE001
    render_fatal_error(
        title="Database connection error",
        message=f"The dashboard couldn't open its storage database at \"{_settings.storage.db_path}\". This usually means the path is invalid, the directory doesn't exist, or the process doesn't have write permission there.",
        hint="Check storage.db_path in settings.yaml and that the directory is writable.",
        exc=e,
    )

# Access decision computed once per script run, used both to truncate
# hydrated results below (a Free user shouldn't see a full historical
# result set just by reloading, even though it's cached in storage) and
# by the Scan Now button further down.
_access = check_scanner_access(_user, "opportunity", _app_storage)

# Load the most recent scan per symbol on first render after a restart —
# only once per session (the flag prevents re-querying storage on every
# widget interaction rerun; results already in session_state after that
# point are the source of truth, refreshed only by Scan Now).
if "hydrated" not in st.session_state:
    st.session_state.hydrated = True
    if not st.session_state.results:
        hydrated_results, hydrated_time = hydrate_from_storage(_storage)
        if hydrated_results:
            if _access.max_results_shown is not None:
                hydrated_results = sorted(hydrated_results, key=lambda r: r.composite_score, reverse=True)[:_access.max_results_shown]
            st.session_state.results = hydrated_results
            st.session_state.last_scan_time = hydrated_time

# ----------------------------------------------------------------- top bar

def _preset_slug(display_name: str) -> str:
    return display_name.lower().replace(" ", "_")


def _preset_from_slug(slug: str) -> str:
    for display_name in ["🔥 Trending Now"] + list(UNIVERSE_PRESETS.keys()) + ["Custom"]:
        if _preset_slug(display_name) == slug:
            return display_name
    return DEFAULT_UNIVERSE_PRESET


top_l, top_r = st.columns([3, 2])
with top_l:
    last_scan_str = st.session_state.last_scan_time.strftime("%H:%M:%S UTC") if st.session_state.last_scan_time else "never"
    is_live = st.session_state.last_scan_time and (datetime.now(timezone.utc) - st.session_state.last_scan_time) < timedelta(minutes=20)
    dot_cls = "live-dot" if is_live else "live-dot stale"
    st.markdown(f"""
    <div class="topbar">
      <div class="topbar-logo">AutoBot Scanner</div>
      <div class="topbar-meta"><span class="{dot_cls}"></span>{'LIVE' if is_live else 'STALE'} · Last scan: {last_scan_str}</div>
    </div>
    """, unsafe_allow_html=True)

with top_r:
    c0, c1, c2, c3 = st.columns([0.5, 1, 1, 1.2])
    with c0:
        render_logout_button(help_text=f"Signed in as {_user.email} · Sign out")
    with c1:
        mode = st.selectbox("Mode", ["Scalp", "Swing"], index=["Scalp", "Swing"].index(st.session_state.mode), label_visibility="collapsed")
        st.session_state.mode = mode
    with c2:
        preset_names = ["🔥 Trending Now"] + list(UNIVERSE_PRESETS.keys()) + ["Custom"]
        if "universe_preset" not in st.session_state:
            st.session_state.universe_preset = _preset_from_slug(_user.last_universe_preset)
        selected_preset = st.selectbox(
            "Universe", preset_names, index=preset_names.index(st.session_state.universe_preset) if st.session_state.universe_preset in preset_names else preset_names.index(DEFAULT_UNIVERSE_PRESET),
            label_visibility="collapsed", key="universe_preset_select",
        )
        if selected_preset != st.session_state.universe_preset:
            st.session_state.universe_preset = selected_preset
            if selected_preset == "🔥 Trending Now":
                st.session_state.pop("trending_universe_cache", None)  # force a fresh fetch on switch
            _app_storage.save_universe_preference(
                _user.id, _preset_slug(selected_preset),
                _user.last_universe_custom if selected_preset == "Custom" else None,
            )
    with c3:
        scan_clicked = st.button("⟳ Scan Now", width='stretch', disabled=not _access.allowed)

if st.session_state.universe_preset == "🔥 Trending Now":
    if "trending_universe_cache" not in st.session_state:
        with st.spinner("Discovering trending + high-volume coins..."):
            discovered = discover_trending_universe(max_size=25)
        st.session_state.trending_universe_cache = discovered
        st.session_state.pop("trending_overview_cache", None)  # force a fresh overview fetch to match
    active_universe = st.session_state.trending_universe_cache
    refresh_col, caption_col = st.columns([0.15, 0.85])
    with refresh_col:
        if st.button("↻ Refresh", key="refresh_trending"):
            st.session_state.pop("trending_universe_cache", None)
            st.session_state.pop("trending_overview_cache", None)
            st.rerun()
    with caption_col:
        if not active_universe:
            st.warning("Trending discovery unavailable right now (CoinGecko unreachable) — falling back to High Liquidity.")
            active_universe = UNIVERSE_PRESETS["High Liquidity"]
            discovery_failed = True
        else:
            st.caption(f"Live-discovered from CoinGecko trending + top volume ({len(active_universe)} coins)")
            discovery_failed = False

    if active_universe and not discovery_failed:
        if "trending_overview_cache" not in st.session_state:
            with st.spinner("Loading live prices and volume..."):
                st.session_state.trending_overview_cache = fetch_trending_overview(active_universe)
        overview = st.session_state.trending_overview_cache
        preview_rows = []
        for sym in active_universe:
            data = overview.get(sym)
            preview_rows.append({
                "Symbol": sym,
                "Price": data["price"] if data and data.get("price") is not None else None,
                "24h Change": data["change_24h_pct"] if data and data.get("change_24h_pct") is not None else None,
                "24h Volume": data["volume_24h_usd"] if data and data.get("volume_24h_usd") is not None else None,
                "24h High": data["high_24h"] if data and data.get("high_24h") is not None else None,
                "24h Low": data["low_24h"] if data and data.get("low_24h") is not None else None,
                "MCap Rank": data["market_cap_rank"] if data and data.get("market_cap_rank") is not None else None,
            })
        preview_df = pd.DataFrame(preview_rows)
        st.dataframe(
            preview_df, width='stretch', hide_index=True, height=min(300, 45 + 36 * len(preview_rows)),
            column_config={
                "Price": st.column_config.NumberColumn(format="$%.4f"),
                "24h Change": st.column_config.NumberColumn(format="%+.2f%%"),
                "24h Volume": st.column_config.NumberColumn(format="$%,.0f"),
                "24h High": st.column_config.NumberColumn(format="$%.4f"),
                "24h Low": st.column_config.NumberColumn(format="$%.4f"),
                "MCap Rank": st.column_config.NumberColumn(format="%d"),
            },
        )
        missing = [s for s in active_universe if s not in overview]
        if missing:
            st.caption(f"No live market data found for: {', '.join(missing)} (likely outside CoinGecko's top 250 by market cap)")
elif st.session_state.universe_preset == "Custom":
    custom_default = _user.last_universe_custom or ",".join(_settings.universe.default)
    custom_universe_input = st.text_input(
        "Custom universe (comma-separated symbols)", value=custom_default, key="custom_universe_input",
    )
    if custom_universe_input != (_user.last_universe_custom or ""):
        _app_storage.save_universe_preference(_user.id, "custom", custom_universe_input)
    active_universe = [s.strip().upper() for s in custom_universe_input.split(",") if s.strip()]
else:
    active_universe = UNIVERSE_PRESETS[st.session_state.universe_preset]
    st.caption(f"{st.session_state.universe_preset}: {', '.join(active_universe)}")

if scan_clicked:
    # Re-verify right at the point of action rather than trusting the
    # disabled= attribute computed a moment earlier — the disabled
    # button is a UX nicety, this recheck is the actual enforcement.
    _recheck = check_scanner_access(_user, "opportunity", _app_storage)
    if not _recheck.allowed:
        st.error(_recheck.reason)
    else:
        with st.spinner("Scanning…"):
            try:
                results = run_scan(_settings, st.session_state.mode, active_universe)
                if _recheck.max_results_shown is not None:
                    results = sorted(results, key=lambda r: r.composite_score, reverse=True)[:_recheck.max_results_shown]
                st.session_state.results = results
                st.session_state.last_scan_time = datetime.now(timezone.utc)
                _storage.save_scan_results_sync(results)
                record_scan(_user, "opportunity", _app_storage)
            except Exception as e:  # noqa: BLE001
                st.error(f"Scan failed: {e}")

remaining_display = "unlimited" if _access.scans_remaining_today is None else str(_access.scans_remaining_today)
if not _access.allowed:
    st.warning(_access.reason)
st.caption(f"Plan: **{_user.plan.value.title()}** · Scans remaining today: **{remaining_display}**"
           + (f" · Results capped to top {_access.max_results_shown}" if _access.max_results_shown else ""))

with st.expander(f"Account & Billing — {_user.email}"):
    render_billing_section(
        _user, _settings,
        success_url=f"{_settings.app_base_url}?checkout=success",
        cancel_url=f"{_settings.app_base_url}?checkout=cancel",
    )

st.markdown("<br>", unsafe_allow_html=True)

# ----------------------------------------------------------------- main layout

left, right = st.columns([7, 3])

with right:
    st.markdown('<div class="mono-label">Regime</div>', unsafe_allow_html=True)
    if st.session_state.results:
        r0 = st.session_state.results[0]
        regime_color = {"Risk-On": "#4ade80", "Neutral": "#fbbf24", "Risk-Off": "#f87171"}.get(r0.regime_label, "#8c8c89")
        st.markdown(f'<div style="font-family:DM Mono,monospace;font-size:13px;color:{regime_color}">{r0.regime_label} ({r0.regime_score if r0.regime_score is not None else "—"})</div>', unsafe_allow_html=True)
    else:
        st.markdown('<div style="font-family:DM Mono,monospace;font-size:13px;color:#8c8c89">No scan yet</div>', unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown('<div class="mono-label">Pillar weights</div>', unsafe_allow_html=True)
    w = st.session_state.weights
    ws = w["strength"] = st.slider("Strength", 0.0, 1.0, w["strength"], 0.01, key="w_strength")
    wo = w["oi_dynamics"] = st.slider("OI Dynamics", 0.0, 1.0, w["oi_dynamics"], 0.01, key="w_oi")
    wm = w["momentum"] = st.slider("Momentum", 0.0, 1.0, w["momentum"], 0.01, key="w_momentum")
    wsoc = w["social"] = st.slider("Social", 0.0, 1.0, w["social"], 0.01, key="w_social")
    total_w = ws + wo + wm + wsoc
    st.caption(f"Sum: {total_w:.2f} (auto-normalized when applied)")

    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown('<div class="mono-label">Filters</div>', unsafe_allow_html=True)
    min_score_filter = st.slider("Min score", 0, 100, 0)
    risk_filter = st.multiselect("Risk tier", ["core", "small_cap", "high_risk"], default=["core", "small_cap", "high_risk"])

    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown('<div class="mono-label">Signal count</div>', unsafe_allow_html=True)
    if st.session_state.results:
        counts = pd.Series([r.signal for r in st.session_state.results]).value_counts()
        for sig in ["Strong Buy", "Buy", "Neutral", "Caution", "Strong Avoid"]:
            n = int(counts.get(sig, 0))
            st.markdown(f'<div style="display:flex;justify-content:space-between;font-family:DM Mono,monospace;font-size:12px;padding:4px 0">{signal_badge_html(sig)}<span style="color:#8c8c89">{n}</span></div>', unsafe_allow_html=True)
    else:
        st.caption("Run a scan to see signal counts")

# apply weight overrides to results (instant, no re-fetch — recomputes from stored FactorResults)
def rescored_results() -> list[ScanResult]:
    if not st.session_state.results:
        return []
    total = sum(st.session_state.weights.values())
    if total <= 0:
        return st.session_state.results
    normalized = Weights(**{k: v / total for k, v in st.session_state.weights.items()})
    out = []
    for r in st.session_state.results:
        composite, confidence, confidence_label, signal, weights_used, reasons_summary = combine_factors(
            r.factors, normalized, _settings.to_scanner_config().signal_bands, _settings.to_scanner_config().confidence_bands,
        )
        r2 = dataclasses.replace(
            r,
            composite_score=composite, confidence=confidence, confidence_label=confidence_label,
            signal=signal, weights_used=weights_used, reasons_summary=reasons_summary,
        )
        out.append(r2)
    out.sort(key=lambda x: x.composite_score, reverse=True)
    return out


display_results = [
    r for r in rescored_results()
    if r.composite_score >= min_score_filter and r.risk_tier in risk_filter
]


def _build_rows(results: list[ScanResult]) -> pd.DataFrame:
    rows = []
    for i, r in enumerate(results, 1):
        f = r.factors
        social_available = f["social"].available
        rows.append({
            "Rank": i, "Symbol": r.base, "Score": r.composite_score,
            "Signal": f"{SIGNAL_INDICATOR.get(r.signal, '⚪')} {r.signal}",
            "Confidence": r.confidence, "Strength": f["strength"].score if f["strength"].available else None,
            "OI": f["oi_dynamics"].score if f["oi_dynamics"].available else None,
            "Momentum": f["momentum"].score if f["momentum"].available else None,
            "Social": f["social"].score if social_available else None,
            "Narrative": f["social"].raw.get("narrative_signal", "—") if social_available else "—",
            "Price": r.price,
            "Risk": f"{RISK_INDICATOR.get(r.risk_tier, '⚪')} {r.risk_tier}",
        })
    return pd.DataFrame(rows)


def _render_result_table(results: list[ScanResult], widget_key: str):
    df = _build_rows(results)
    event = st.dataframe(
        df, width='stretch', hide_index=True, height=min(560, 60 + 36 * len(results)),
        on_select="rerun", selection_mode="single-row", key=widget_key,
        column_config={
            "Score": st.column_config.ProgressColumn("Score", min_value=0, max_value=100, format="%.1f"),
            "Confidence": st.column_config.ProgressColumn("Confidence", min_value=0, max_value=100, format="%.0f"),
            "Strength": st.column_config.NumberColumn(format="%.0f"),
            "OI": st.column_config.NumberColumn(format="%.0f"),
            "Momentum": st.column_config.NumberColumn(format="%.0f"),
            "Social": st.column_config.NumberColumn(format="%.0f"),
            "Price": st.column_config.NumberColumn(format="$%.4f"),
        },
    )
    selected_rows = event.selection.rows if event and event.selection else []
    if selected_rows:
        selected_symbol = df.iloc[selected_rows[0]]["Symbol"]
        selected_result = next((r for r in results if r.base == selected_symbol), None)
        if selected_result:
            st.session_state.detail_result = selected_result


with left:
    st.markdown('<div class="section-h">Top Opportunities</div>', unsafe_allow_html=True)
    if not st.session_state.results:
        st.info("No scans yet — click **Scan Now** to run your first scan.")
    elif not display_results:
        st.info("No results match your current filters — try widening the score or risk-tier filters.")
    elif not _settings.smart_view.enabled:
        # Smart View disabled in settings — original flat-table behavior, unchanged
        _render_result_table(display_results, widget_key="flat_table")
    else:
        smart_config = _settings.to_scanner_config().smart_view
        buckets = bucket_results(display_results, smart_config)

        # Super Strong and Strong are the prominent, always-open sections —
        # this is what "make Super Strong and Strong most prominent" means
        # concretely: they render first, always expanded, with a visible
        # count even when empty (empty Super Strong is the EXPECTED normal
        # case most scans, not an error — "very selective" means usually zero).
        super_strong = buckets[Bucket.SUPER_STRONG]
        st.markdown(f'<div class="mono-label" style="margin-top:8px">{BUCKET_LABELS[Bucket.SUPER_STRONG]} ({len(super_strong)})</div>', unsafe_allow_html=True)
        if super_strong:
            _render_result_table(super_strong, widget_key="bucket_super_strong")
        else:
            st.caption("No Super Strong setups this scan — this bucket is intentionally selective, an empty result here is normal, not an error.")

        strong = buckets[Bucket.STRONG]
        st.markdown(f'<div class="mono-label" style="margin-top:20px">{BUCKET_LABELS[Bucket.STRONG]} ({len(strong)})</div>', unsafe_allow_html=True)
        if strong:
            _render_result_table(strong, widget_key="bucket_strong")
        else:
            st.caption("No results in this bucket right now.")

        building = buckets[Bucket.BUILDING]
        if building:
            st.markdown(f'<div class="mono-label" style="margin-top:20px">{BUCKET_LABELS[Bucket.BUILDING]} ({len(building)})</div>', unsafe_allow_html=True)
            _render_result_table(building, widget_key="bucket_building")

        # High Risk / Low Conviction — deliberately less prominent, collapsed
        # by default, matching "should be less prominent or collapsed"
        high_risk = buckets[Bucket.HIGH_RISK_LOW_CONVICTION]
        if high_risk:
            with st.expander(f"{BUCKET_LABELS[Bucket.HIGH_RISK_LOW_CONVICTION]} ({len(high_risk)}) — click to expand", expanded=False):
                _render_result_table(high_risk, widget_key="bucket_high_risk")

        # Full detailed table always stays available, per the explicit
        # "keep the ability to see the full detailed table if needed"
        # requirement — collapsed by default so it doesn't compete with
        # the bucketed view above, but never removed.
        with st.expander(f"View full ranked table ({len(display_results)} results, ungrouped)", expanded=False):
            _render_result_table(display_results, widget_key="full_flat_table")

# ----------------------------------------------------------------- detail modal

@st.dialog("Coin Detail", width="large")
def show_detail(result: ScanResult):
    top1, top2 = st.columns([1, 2])
    with top1:
        st.markdown(f'<div class="big-score">{result.composite_score:.1f}<span class="big-score-unit">/100</span></div>', unsafe_allow_html=True)
        st.markdown(signal_badge_html(result.signal), unsafe_allow_html=True)
        st.markdown(f'<div style="margin-top:8px;font-family:DM Mono,monospace;font-size:12px;color:#8c8c89">Confidence: {result.confidence:.0f} ({result.confidence_label})</div>', unsafe_allow_html=True)
        st.markdown(f'<div style="font-family:DM Mono,monospace;font-size:12px;color:#8c8c89">Risk tier: {result.risk_tier}</div>', unsafe_allow_html=True)

    with top2:
        st.markdown(f'<div class="section-h">{result.base}</div>', unsafe_allow_html=True)
        st.markdown(f'<div style="font-family:DM Mono,monospace;font-size:13px;color:#b8b7b2">${result.price:,.4f}</div>', unsafe_allow_html=True)

        st.markdown('<div class="mono-label" style="margin-top:20px">Pillar breakdown</div>', unsafe_allow_html=True)
        for name, label in [("strength", "Strength"), ("oi_dynamics", "OI Dynamics"), ("momentum", "Momentum"), ("social", "Social")]:
            f = result.factors[name]
            score = f.score if f.available else 0
            color = "#4ade80" if score >= 65 else "#fbbf24" if score >= 45 else "#f87171"
            avail_note = "" if f.available else " (unavailable)"
            st.markdown(f"""
            <div class="pillar-row">
              <div class="pillar-label-row"><span>{label}{avail_note}</span><span>{f.score:.0f}</span></div>
              <div class="pillar-track"><div class="pillar-fill" style="width:{score}%;background:{color}"></div></div>
            </div>
            """, unsafe_allow_html=True)

        social_factor = result.factors.get("social")
        if social_factor and social_factor.available and social_factor.raw:
            raw = social_factor.raw
            st.markdown('<div class="mono-label" style="margin-top:20px">Social detail</div>', unsafe_allow_html=True)
            narrative = raw.get("narrative_signal", "—")
            spike_note = " · attention accelerating right now" if raw.get("is_spike") else ""
            st.markdown(f'<div style="font-family:DM Mono,monospace;font-size:13px;color:#f5f4f0;margin-bottom:8px">{narrative}{spike_note}</div>', unsafe_allow_html=True)
            sm1, sm2, sm3, sm4 = st.columns(4)
            with sm1:
                st.metric("Galaxy Score", f"{raw['galaxy_score']:.0f}" if raw.get("galaxy_score") is not None else "—")
            with sm2:
                st.metric("AltRank", f"{raw['alt_rank']:.0f}" if raw.get("alt_rank") is not None else "—")
            with sm3:
                st.metric("Sentiment", f"{raw['sentiment']:.0f}%" if raw.get("sentiment") is not None else "—")
            with sm4:
                st.metric("Dominance", f"{raw['social_dominance']:.1f}%" if raw.get("social_dominance") is not None else "—")

    st.markdown('<div class="mono-label" style="margin-top:24px">Thesis</div>', unsafe_allow_html=True)
    thesis_text = " ".join(result.reasons_summary[:4]) if result.reasons_summary else "No explanation available."
    st.markdown(f'<div class="thesis-box">{thesis_text}</div>', unsafe_allow_html=True)

    st.markdown('<div class="mono-label" style="margin-top:24px">Flags</div>', unsafe_allow_html=True)
    for flag in derive_display_flags(result):
        st.markdown(f'<div class="flag-row flag-{flag["sev"]}">{flag["label"]}</div>', unsafe_allow_html=True)

    st.markdown('<div class="mono-label" style="margin-top:24px">Key metrics</div>', unsafe_allow_html=True)
    m1, m2, m3, m4 = st.columns(4)
    for col, label, value in zip(
        [m1, m2, m3, m4],
        ["Weight (Strength)", "Weight (OI)", "Weight (Momentum)", "Weight (Social)"],
        [result.weights_used.get("strength", 0), result.weights_used.get("oi_dynamics", 0),
         result.weights_used.get("momentum", 0), result.weights_used.get("social", 0)],
    ):
        with col:
            st.markdown(f'<div class="metric-card"><div class="metric-label">{label}</div><div class="metric-value">{value*100:.0f}%</div></div>', unsafe_allow_html=True)


if st.session_state.get("detail_result") is not None:
    show_detail(st.session_state.detail_result)
    st.session_state.detail_result = None  # consume — dialog re-opens only on a fresh selection

# ----------------------------------------------------------------- bottom: alerts + backtest

st.markdown("<br><hr><br>", unsafe_allow_html=True)
bottom_l, bottom_r = st.columns([1, 1])

with bottom_l:
    st.markdown('<div class="section-h">Recent Alerts</div>', unsafe_allow_html=True)
    try:
        alerts = load_recent_alerts(_storage)
    except Exception:  # noqa: BLE001
        alerts = []
    if not alerts:
        st.caption("No signal changes or score jumps in the last 4 hours.")
    else:
        for a in alerts:
            st.markdown(f"""
            <div class="alert-row">
              <span class="alert-symbol">{a['base']}</span>
              <span class="alert-reason">{a['reason']}</span>
            </div>
            """, unsafe_allow_html=True)

with bottom_r:
    st.markdown('<div class="section-h">Performance (30d, Strong Buy)</div>', unsafe_allow_html=True)
    try:
        bt = _storage.backtest_signal_sync("Strong Buy", lookback_days=30)
    except Exception:  # noqa: BLE001
        bt = {"sample_size": 0}
    if bt.get("sample_size", 0) == 0:
        st.caption("Not enough scan history yet to backtest — needs the scheduler running for a while first.")
    else:
        m1, m2 = st.columns(2)
        with m1:
            st.markdown(f'<div class="metric-card"><div class="metric-label">Avg forward return</div><div class="metric-value">{bt["avg_forward_return_pct"]:+.1f}%</div></div>', unsafe_allow_html=True)
        with m2:
            st.markdown(f'<div class="metric-card"><div class="metric-label">Win rate</div><div class="metric-value">{bt["win_rate_pct"]:.0f}% ({bt["sample_size"]} signals)</div></div>', unsafe_allow_html=True)
