"""
Meme Coin Scanner — Streamlit dashboard.

Run: streamlit run opportunity_scanner/meme_dashboard.py

Reuses run_scan() from meme_main.py directly rather than duplicating scan
logic — the CLI and the dashboard are two views onto the exact same
pipeline (discovery -> aggregator -> engine -> storage -> hype events ->
alerts), not two implementations that could drift apart.

Same architecture lesson already learned building dashboard.py (the main
scanner's dashboard): only the actual scan is async; every storage read
goes through meme_storage.py's *_sync methods, since Streamlit's
execution model is synchronous and wrapping simple SQLite reads in
asyncio.run() caused real problems there (see that file's docstring).
"""

from __future__ import annotations
import asyncio
import os
import sys

# Same fix as dashboard.py, same reason — see that file's comment for
# the full explanation (a real ModuleNotFoundError discovered on live
# Railway deployment, not theoretical).
import json
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opportunity_scanner.settings import load_settings
from opportunity_scanner.meme_scoring_engine import (
    ScoringEngine, FinalMemeResult, SafetyResult, PillarScores, RiskFlag, HypeEvent, Mode,
)
from opportunity_scanner.meme_storage import MemeScanStorage
import opportunity_scanner.meme_main as meme_main_module

# ----------------------------------------------------------------- page setup

st.set_page_config(page_title="Meme Scanner", page_icon="🔥", layout="wide", initial_sidebar_state="collapsed")

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,400;0,500;1,400;1,500&family=DM+Sans:wght@300;400;500&family=DM+Mono:wght@300;400&display=swap');

:root {
    --black: #0e0e0c; --bg: #0a0a08; --white: #f5f4f0; --gray: #8c8c89; --gray-mid: #b8b7b2;
    --border: rgba(245,244,240,0.08); --border-strong: rgba(245,244,240,0.16);
    --green: #4ade80; --amber: #fbbf24; --red: #f87171; --gold: rgba(180,155,100,0.6); --magenta: #e879f9;
}
html, body, [class*="css"] { font-family: 'DM Sans', sans-serif; }
.stApp {
    background: var(--bg);
    background-image: radial-gradient(ellipse at 85% 0%, rgba(180,80,40,0.05) 0%, transparent 50%);
    color: var(--white);
}
#MainMenu, footer, header {visibility: hidden;}

.topbar { display: flex; align-items: center; justify-content: space-between; padding: 18px 4px; border-bottom: 1px solid var(--border); margin-bottom: 20px; }
.topbar-logo { font-family: 'DM Sans', sans-serif; font-weight: 500; font-size: 17px; color: var(--white); display: flex; align-items: center; gap: 8px; }
.topbar-logo::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: var(--gold); }
.topbar-meta { font-family: 'DM Mono', monospace; font-size: 10px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--gray); }
.live-dot { display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: var(--green); margin-right: 6px; }
.live-dot.stale { background: var(--gray); }
.mono-label { font-family: 'DM Mono', monospace; font-size: 9px; letter-spacing: 0.16em; text-transform: uppercase; color: var(--gray); margin-bottom: 8px; }
.section-h { font-family: 'Playfair Display', serif; font-size: 20px; color: var(--white); margin-bottom: 4px; }

.safety-badge { font-family: 'DM Mono', monospace; font-size: 10px; letter-spacing: 0.05em; padding: 3px 9px; border-radius: 3px; white-space: nowrap; }
.safety-pass { background: rgba(74,222,128,0.12); color: var(--green); border: 1px solid rgba(74,222,128,0.25); }
.safety-caution { background: rgba(251,191,36,0.1); color: var(--amber); border: 1px solid rgba(251,191,36,0.22); }
.hype-badge { font-family: 'DM Mono', monospace; font-size: 10px; padding: 3px 9px; border-radius: 3px; }
.hype-explosive { background: rgba(232,121,249,0.12); color: var(--magenta); border: 1px solid rgba(232,121,249,0.25); }
.hype-high { background: rgba(74,222,128,0.1); color: var(--green); border: 1px solid rgba(74,222,128,0.2); }
.hype-medium { background: rgba(251,191,36,0.08); color: var(--amber); border: 1px solid rgba(251,191,36,0.18); }
.hype-low { background: rgba(245,244,240,0.04); color: var(--gray-mid); border: 1px solid var(--border-strong); }

.big-score { font-family: 'Playfair Display', serif; font-size: 64px; line-height: 1; color: var(--white); }
.big-score-unit { font-family: 'DM Mono', monospace; font-size: 13px; color: var(--gray); }
.pillar-row { margin-bottom: 14px; }
.pillar-label-row { display: flex; justify-content: space-between; font-family: 'DM Mono', monospace; font-size: 11px; color: var(--gray-mid); margin-bottom: 5px; }
.pillar-track { height: 6px; background: var(--border); border-radius: 3px; overflow: hidden; }
.pillar-fill { height: 100%; border-radius: 3px; }
.metric-card { background: rgba(245,244,240,0.03); border: 1px solid var(--border); padding: 14px 16px; }
.metric-card .metric-label { font-family: 'DM Mono', monospace; font-size: 9px; letter-spacing: 0.1em; text-transform: uppercase; color: var(--gray); margin-bottom: 5px; }
.metric-card .metric-value { font-family: 'DM Mono', monospace; font-size: 15px; color: var(--white); }
.flag-row { display: flex; gap: 10px; padding: 9px 13px; margin-bottom: 6px; border-left: 3px solid var(--border-strong); font-size: 13px; }
.flag-danger { border-left-color: var(--red); background: rgba(248,113,113,0.06); color: #ffb4b4; }
.flag-warning { border-left-color: var(--amber); background: rgba(251,191,36,0.06); color: #ffd98a; }
.flag-info { border-left-color: var(--gray); background: rgba(245,244,240,0.03); color: var(--gray-mid); }
.hype-event-row { display: flex; gap: 10px; padding: 9px 13px; margin-bottom: 6px; border-left: 3px solid var(--magenta); background: rgba(232,121,249,0.06); color: #f0abfc; font-size: 13px; }
.thesis-box { background: rgba(245,244,240,0.03); border-left: 2px solid var(--gold); padding: 14px 18px; font-size: 14px; color: var(--gray-mid); line-height: 1.7; }
.link-btn { display: inline-block; padding: 8px 16px; border: 1px solid var(--border-strong); color: var(--white); text-decoration: none; font-family: 'DM Mono', monospace; font-size: 11px; margin-right: 8px; margin-top: 8px; }
.link-btn:hover { border-color: var(--gold); }

.login-wrap { display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 70vh; text-align: center; }
.login-logo { font-family: 'DM Sans', sans-serif; font-weight: 500; font-size: 15px; color: var(--white); display: flex; align-items: center; gap: 8px; margin-bottom: 28px; }
.login-logo::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: var(--gold); }
.login-title { font-family: 'Playfair Display', serif; font-size: 40px; color: var(--white); margin-bottom: 10px; }
.login-sub { font-family: 'DM Mono', monospace; font-size: 11px; letter-spacing: 0.1em; text-transform: uppercase; color: var(--gray); margin-bottom: 36px; }
.error-page { display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 65vh; text-align: center; padding: 0 24px; }
.error-icon { font-family: 'Playfair Display', serif; font-size: 32px; color: var(--red); margin-bottom: 20px; }
.error-title { font-family: 'Playfair Display', serif; font-size: 28px; color: var(--white); margin-bottom: 12px; }
.error-message { font-size: 15px; color: var(--gray-mid); line-height: 1.6; max-width: 480px; margin-bottom: 8px; }
hr { border-color: var(--border); }
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)

# ----------------------------------------------------------------- error page + settings

def render_fatal_error(title: str, message: str, exc: Optional[Exception] = None):
    st.markdown('<div class="error-page">', unsafe_allow_html=True)
    st.markdown('<div class="error-icon">⚠</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="error-title">{title}</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="error-message">{message}</div>', unsafe_allow_html=True)
    st.markdown('</div>', unsafe_allow_html=True)
    if exc is not None:
        _, center, _ = st.columns([1, 1.4, 1])
        with center:
            with st.expander("Technical details"):
                st.code(f"{type(exc).__name__}: {exc}", language="text")
    st.stop()


try:
    _settings = load_settings()
except Exception as e:  # noqa: BLE001
    render_fatal_error("Couldn't load settings.yaml", "Check that settings.yaml exists and is valid YAML.", exc=e)


# ----------------------------------------------------------------- auth (real per-user, shared with the main dashboard)

from opportunity_scanner.app_storage import AppStorage
from opportunity_scanner.auth_ui import require_auth, render_logout_button
from opportunity_scanner.billing_ui import render_billing_section
from opportunity_scanner.access_control import check_scanner_access, record_scan
from opportunity_scanner.plans import ScannerAccess, has_scanner_access

_app_storage = AppStorage(_settings.app_db_path)
_user = require_auth(_app_storage, product_name="Meme Scanner")

# ----------------------------------------------------------------- helpers

SAFETY_CLASS = {"Pass": "safety-pass", "Caution": "safety-caution"}
HYPE_CLASS = {"Explosive": "hype-explosive", "High": "hype-high", "Medium": "hype-medium", "Low": "hype-low"}


def safety_badge(grade: str) -> str:
    return f'<span class="safety-badge {SAFETY_CLASS.get(grade, "safety-caution")}">{grade}</span>'


def hype_badge(level: Optional[str]) -> str:
    if not level:
        return '<span class="hype-badge hype-low">—</span>'
    return f'<span class="hype-badge {HYPE_CLASS.get(level, "hype-low")}">{level}</span>'


def hydrate_from_storage(storage: MemeScanStorage) -> List[FinalMemeResult]:
    """
    Rebuilds FinalMemeResult objects from the most recent stored scan per
    token — don't start empty just because the process restarted. Risk
    flags ARE reconstructable (stored as JSON), unlike the main
    scanner's per-factor raw sub-scores.
    """
    try:
        rows = storage.get_latest_scan_per_token_sync()
    except Exception:  # noqa: BLE001
        return []

    results = []
    for row in rows:
        if row["safety_grade"] == "Fail":
            continue  # never show Fail-grade tokens, even historical ones
        try:
            flags_raw = json.loads(row["risk_flags_json"]) if row["risk_flags_json"] else []
            risk_flags = [RiskFlag(**f) for f in flags_raw]
        except (json.JSONDecodeError, TypeError):
            risk_flags = []

        pillar_scores = None
        if row["hype_score"] is not None:
            pillar_scores = PillarScores(
                hype=row["hype_score"], onchain_health=row["onchain_score"] or 50.0, momentum=row["momentum_score"] or 50.0,
            )

        results.append(FinalMemeResult(
            symbol=row["symbol"], token_address=row["token_address"], mode=row["mode"],
            safety=SafetyResult(grade=row["safety_grade"], reasons=[]),
            opportunity_score=row["opportunity_score"], hype_level=row["hype_level"],
            pillar_scores=pillar_scores, confidence=row["confidence"],
            risk_flags=risk_flags, hype_events=[], thesis=row["thesis"],
            key_metrics={
                "liquidity_usd": None, "market_cap_usd": None, "pair_age_minutes": None,
                "top10_holder_pct": None, "unique_holders": None, "rugcheck_risk_score": None,
                "volume_24h_usd": None, "mention_velocity_ratio": row["mention_velocity_ratio"],
            },
        ))

    results.sort(key=lambda r: r.opportunity_score or 0, reverse=True)
    return results


def run_scan_sync(settings, mode: Mode, addresses: Optional[List[str]] = None) -> List[FinalMemeResult]:
    return asyncio.run(meme_main_module.run_scan(settings, mode, addresses))


# ----------------------------------------------------------------- session state

if "meme_results" not in st.session_state:
    st.session_state.meme_results = []
if "meme_last_scan_time" not in st.session_state:
    st.session_state.meme_last_scan_time = None
if "meme_mode" not in st.session_state:
    st.session_state.meme_mode = _settings.meme_scanner.mode

try:
    _storage = MemeScanStorage(_settings.meme_scanner.db_path)
except Exception as e:  # noqa: BLE001
    render_fatal_error("Database connection error", f"Couldn't open storage at \"{_settings.meme_scanner.db_path}\".", exc=e)

_access = check_scanner_access(_user, "meme", _app_storage)

if has_scanner_access(_user.plan, "meme") == ScannerAccess.NONE:
    # NONE access (Free tier) — block the ENTIRE scanner, not just the
    # scan button. Free doesn't get a degraded meme scanner experience,
    # it gets none at all, per the documented plan decision in plans.py.
    _, center, _ = st.columns([1, 1.2, 1])
    with center:
        st.markdown('<div class="login-wrap">', unsafe_allow_html=True)
        st.markdown('<div class="login-logo">🔥 Meme Scanner</div>', unsafe_allow_html=True)
        st.markdown('<div class="login-title">Upgrade required</div>', unsafe_allow_html=True)
        st.markdown(f'<div class="login-sub">{_access.reason}</div>', unsafe_allow_html=True)
        with st.expander("View plans & upgrade", expanded=True):
            render_billing_section(
                _user, _settings,
                success_url=f"{_settings.app_base_url}?checkout=success",
                cancel_url=f"{_settings.app_base_url}?checkout=cancel",
            )
        render_logout_button(help_text=f"Signed in as {_user.email} · Sign out")
        st.markdown('</div>', unsafe_allow_html=True)
    st.stop()

if "meme_hydrated" not in st.session_state:
    st.session_state.meme_hydrated = True
    if not st.session_state.meme_results:
        hydrated = hydrate_from_storage(_storage)
        if hydrated:
            st.session_state.meme_results = hydrated

# ----------------------------------------------------------------- top section

top_l, top_r = st.columns([2.5, 2.5])
with top_l:
    last_scan_str = st.session_state.meme_last_scan_time.strftime("%H:%M:%S UTC") if st.session_state.meme_last_scan_time else "never"
    is_live = st.session_state.meme_last_scan_time and (datetime.now(timezone.utc) - st.session_state.meme_last_scan_time) < timedelta(minutes=20)
    dot_cls = "live-dot" if is_live else "live-dot stale"
    st.markdown(f"""
    <div class="topbar">
      <div class="topbar-logo">🔥 Meme Scanner</div>
      <div class="topbar-meta"><span class="{dot_cls}"></span>{'LIVE' if is_live else 'STALE'} · Last scan: {last_scan_str}</div>
    </div>
    """, unsafe_allow_html=True)

with top_r:
    c0, c1, c2 = st.columns([0.4, 1.2, 1.2])
    with c0:
        render_logout_button(help_text=f"Signed in as {_user.email} · Sign out")
    with c1:
        mode_labels = {"sniper": "Sniper", "early_momentum": "Early", "confirmed_runner": "Runner"}
        mode_keys = list(mode_labels.keys())
        selected_label = st.selectbox("Mode", [mode_labels[k] for k in mode_keys],
                                       index=mode_keys.index(st.session_state.meme_mode), label_visibility="collapsed")
        st.session_state.meme_mode = mode_keys[[mode_labels[k] for k in mode_keys].index(selected_label)]
    with c2:
        scan_clicked = st.button("🔥 Scan Now", width='stretch', disabled=not _access.allowed)
        if scan_clicked:
            # Re-verify right at the point of action, same reasoning as the
            # main dashboard — the disabled= attribute is a UX nicety, this
            # is the actual enforcement.
            _recheck = check_scanner_access(_user, "meme", _app_storage)
            if not _recheck.allowed:
                st.error(_recheck.reason)
            else:
                settings_copy = load_settings()
                settings_copy.meme_scanner.mode = st.session_state.meme_mode
                with st.spinner("Scanning candidates..."):
                    try:
                        results = run_scan_sync(settings_copy, Mode(st.session_state.meme_mode))
                        st.session_state.meme_results = results
                        st.session_state.meme_last_scan_time = datetime.now(timezone.utc)
                        record_scan(_user, "meme", _app_storage)
                    except Exception as e:  # noqa: BLE001
                        st.error(f"Scan failed: {e}")

remaining_display = "unlimited" if _access.scans_remaining_today is None else str(_access.scans_remaining_today)
if not _access.allowed:
    st.warning(_access.reason)
st.caption(f"Plan: **{_user.plan.value.title()}** · Meme Scanner scans remaining today: **{remaining_display}**")

# Quick safety overview
all_results = st.session_state.meme_results
pass_count = sum(1 for r in all_results if r.safety.grade == "Pass")
caution_count = sum(1 for r in all_results if r.safety.grade == "Caution")
st.markdown(
    f'<div class="mono-label">Quick safety overview: '
    f'<span style="color:#4ade80">{pass_count} Pass</span> · '
    f'<span style="color:#fbbf24">{caution_count} Caution</span> · '
    f'<span style="color:#8c8c89">Fail-grade tokens are never shown</span></div>',
    unsafe_allow_html=True,
)

with st.expander(f"Account & Billing — {_user.email}"):
    render_billing_section(
        _user, _settings,
        success_url=f"{_settings.app_base_url}?checkout=success",
        cancel_url=f"{_settings.app_base_url}?checkout=cancel",
    )

st.markdown("<br>", unsafe_allow_html=True)

# ----------------------------------------------------------------- main table

display_results = [r for r in all_results if r.safety.grade != "Fail"]

st.markdown('<div class="section-h">Candidates</div>', unsafe_allow_html=True)

if not all_results:
    st.info("No scans yet — click **🔥 Scan Now** to run your first scan.")
elif not display_results:
    st.info("No candidates passed Safety this cycle.")
else:
    sort_by = st.radio("Sort by", ["Opportunity Score", "Hype Level"], horizontal=True, label_visibility="collapsed")
    hype_rank = {"Explosive": 4, "High": 3, "Medium": 2, "Low": 1, None: 0}
    if sort_by == "Hype Level":
        display_results = sorted(display_results, key=lambda r: hype_rank.get(r.hype_level, 0), reverse=True)
    else:
        display_results = sorted(display_results, key=lambda r: r.opportunity_score or 0, reverse=True)

    rows = []
    for r in display_results:
        km = r.key_metrics
        danger_flags = sum(1 for f in r.risk_flags if f.severity == "danger")
        warning_flags = sum(1 for f in r.risk_flags if f.severity == "warning")
        rows.append({
            "Symbol": r.symbol, "Safety": r.safety.grade,
            "Age (min)": km.get("pair_age_minutes"), "Score": r.opportunity_score, "Hype": r.hype_level,
            "Liquidity": km.get("liquidity_usd"), "Holders": km.get("unique_holders"),
            "Top10%": km.get("top10_holder_pct"), "Volume 24h": km.get("volume_24h_usd"),
            "Mentions Velocity": km.get("mention_velocity_ratio"),
            "Flags": f"{danger_flags}🔴 {warning_flags}🟡" if (danger_flags or warning_flags) else "clean",
        })
    df = pd.DataFrame(rows)

    event = st.dataframe(
        df, width='stretch', hide_index=True, height=520,
        on_select="rerun", selection_mode="single-row",
        column_config={
            "Score": st.column_config.ProgressColumn("Score", min_value=0, max_value=100, format="%.1f"),
            "Liquidity": st.column_config.NumberColumn(format="$%.0f"),
            "Volume 24h": st.column_config.NumberColumn(format="$%.0f"),
            "Top10%": st.column_config.NumberColumn(format="%.0f%%"),
            "Mentions Velocity": st.column_config.NumberColumn(format="%.1fx"),
        },
    )

    selected_rows = event.selection.rows if event and event.selection else []
    if selected_rows:
        selected_symbol = df.iloc[selected_rows[0]]["Symbol"]
        selected_result = next((r for r in display_results if r.symbol == selected_symbol), None)
        if selected_result:
            st.session_state.meme_detail_result = selected_result

# ----------------------------------------------------------------- detail modal

@st.dialog("Token Detail", width="large")
def show_detail(result: FinalMemeResult):
    top1, top2 = st.columns([1, 2])
    with top1:
        score_display = f"{result.opportunity_score:.1f}" if result.opportunity_score is not None else "—"
        st.markdown(f'<div class="big-score">{score_display}<span class="big-score-unit">/100</span></div>', unsafe_allow_html=True)
        st.markdown(safety_badge(result.safety.grade), unsafe_allow_html=True)
        st.markdown(hype_badge(result.hype_level), unsafe_allow_html=True)
        st.markdown(f'<div style="margin-top:8px;font-family:DM Mono,monospace;font-size:12px;color:#8c8c89">Confidence: {result.confidence:.0f}</div>', unsafe_allow_html=True)

    with top2:
        st.markdown(f'<div class="section-h">{result.symbol}</div>', unsafe_allow_html=True)
        st.markdown(f'<div style="font-family:DM Mono,monospace;font-size:11px;color:#8c8c89">{result.token_address}</div>', unsafe_allow_html=True)

        if result.pillar_scores:
            st.markdown('<div class="mono-label" style="margin-top:18px">Pillar breakdown</div>', unsafe_allow_html=True)
            for name, label in [("hype", "Hype"), ("onchain_health", "On-chain Health"), ("momentum", "Momentum")]:
                score = getattr(result.pillar_scores, name)
                color = "#4ade80" if score >= 65 else "#fbbf24" if score >= 45 else "#f87171"
                st.markdown(f"""
                <div class="pillar-row">
                  <div class="pillar-label-row"><span>{label}</span><span>{score:.0f}</span></div>
                  <div class="pillar-track"><div class="pillar-fill" style="width:{score}%;background:{color}"></div></div>
                </div>
                """, unsafe_allow_html=True)

    st.markdown('<div class="mono-label" style="margin-top:20px">Thesis</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="thesis-box">{result.thesis}</div>', unsafe_allow_html=True)

    if result.hype_events:
        st.markdown('<div class="mono-label" style="margin-top:20px">Hype Events</div>', unsafe_allow_html=True)
        for e in result.hype_events:
            st.markdown(f'<div class="hype-event-row">🔥 {e.label}</div>', unsafe_allow_html=True)

    st.markdown('<div class="mono-label" style="margin-top:20px">Risk Flags</div>', unsafe_allow_html=True)
    for f in result.risk_flags:
        st.markdown(f'<div class="flag-row flag-{f.severity}">{f.label}</div>', unsafe_allow_html=True)

    km = result.key_metrics
    st.markdown('<div class="mono-label" style="margin-top:20px">On-chain Metrics</div>', unsafe_allow_html=True)
    m1, m2, m3, m4 = st.columns(4)
    metric_pairs = [
        ("Liquidity", f"${km.get('liquidity_usd'):,.0f}" if km.get("liquidity_usd") else "—"),
        ("Top 10 Holders", f"{km.get('top10_holder_pct'):.0f}%" if km.get("top10_holder_pct") is not None else "—"),
        ("Unique Holders", f"{km.get('unique_holders'):.0f}" if km.get("unique_holders") else "—"),
        ("Pair Age", f"{km.get('pair_age_minutes'):.0f}min" if km.get("pair_age_minutes") is not None else "—"),
    ]
    for col, (label, value) in zip([m1, m2, m3, m4], metric_pairs):
        with col:
            st.markdown(f'<div class="metric-card"><div class="metric-label">{label}</div><div class="metric-value">{value}</div></div>', unsafe_allow_html=True)

    st.markdown('<div class="mono-label" style="margin-top:20px">Links</div>', unsafe_allow_html=True)
    chain = "solana"
    dex_url = f"https://dexscreener.com/{chain}/{result.token_address}"
    solscan_url = f"https://solscan.io/token/{result.token_address}"
    st.markdown(
        f'<a class="link-btn" href="{dex_url}" target="_blank">📈 DexScreener Chart</a>'
        f'<a class="link-btn" href="{solscan_url}" target="_blank">🔍 Solscan</a>',
        unsafe_allow_html=True,
    )


if st.session_state.get("meme_detail_result") is not None:
    show_detail(st.session_state.meme_detail_result)
    st.session_state.meme_detail_result = None
