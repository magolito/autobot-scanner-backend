# Meme Coin Scanner — Architecture & Scoring Design

Companion to `ARCHITECTURE.md` (the main four-pillar scanner), but a
genuinely different model, not a reskin. The main scanner blends four
pillars into one number. This one **gates first, scores second** — a
coin either survives Safety or it never gets an Opportunity Score at
all. This document is the design; a follow-up implementation pass turns
the pseudocode below into tested code, same pattern as the main
scanner's `ARCHITECTURE.md` → `scoring_engine.py` progression.

---

## 0. A design tension worth naming before the formulas

Your stated pillar ranges — Hype 35–40%, On-chain 25–30%, Momentum
20–25% — are independent ranges and don't sum to 100 by construction
(midpoints: 37.5 + 27.5 + 22.5 = 87.5). That's fine as *starting
guidance*, but the actual blend needs concrete numbers that sum to 100
per mode. Rather than silently picking something, here's the resolution:
**Early Momentum is treated as the base case** (closest to your stated
ranges), and **Sniper/Confirmed Runner deviate deliberately** — Sniper
pushes Hype higher (it's nearly the only signal at 0–20min, there's no
on-chain history yet), Confirmed Runner pushes On-chain higher (hype is
already proven by hour 3, sustainability now matters more than
virality). Exact weights are in §5.

---

## 1. Modes

Age-gated, each with its own hard-filter thresholds (stricter as risk
tolerance should shrink, not grow, with unproven age) and its own pillar
weights.

**All numbers in this section are now settings-driven**, not hardcoded —
see `settings.yaml`'s `meme_scanner.thresholds` / `meme_scanner.weights`
/ `meme_scanner.hype_formula` / `meme_scanner.caution_margin` sections.
The values below are the shipped defaults (identical to what's in
`settings.yaml` — one source of truth, not two numbers that could drift
apart), not fixed constants.

| Mode | Age window | Philosophy |
|---|---|---|
| **Sniper** | 0–20 min | Highest risk, highest reward. Minimal on-chain history exists, so hype velocity + first-mover organic buying are almost the whole signal. |
| **Early Momentum** | 20 min – 3 h | The best risk/reward window for most people — enough history to judge holder quality, still early enough to catch real moves. |
| **Confirmed Runner** | 3 h – 24 h | Safer, smaller edge. Already showing sustained volume/holder growth; the job now is timing entry, not discovery. |

```python
class Mode(str, Enum):
    SNIPER = "sniper"
    EARLY_MOMENTUM = "early_momentum"
    CONFIRMED_RUNNER = "confirmed_runner"

MODE_AGE_WINDOWS = {
    Mode.SNIPER: (0, 20),                # minutes
    Mode.EARLY_MOMENTUM: (20, 180),
    Mode.CONFIRMED_RUNNER: (180, 1440),
}
```

---

## 2. Safety Score — the gatekeeper (§ not part of the weighted blend)

Three-tier outcome, not a single reject/pass, so borderline coins are
visible rather than silently dropped:

```
Fail    → breaches any HARD threshold → excluded entirely, no Opportunity Score computed
Caution → clears hard thresholds but breaches any CAUTION (tighter) threshold → scored, but flagged prominently
Pass    → clears both → scored normally
```

### 2.1 Hard thresholds (mode-scaled — stricter for younger coins)

| Check | Sniper | Early Momentum | Confirmed Runner | Source |
|---|---|---|---|---|
| Liquidity (USD) | ≥ $15,000 | ≥ $20,000 | ≥ $25,000 | DexScreener |
| Mint authority | Revoked | Revoked | Revoked | RugCheck |
| Freeze authority | Revoked | Revoked | Revoked | RugCheck |
| Honeypot (can sell) | Must pass | Must pass | Must pass | GoPlus |
| Buy/sell tax | ≤ 10% each | ≤ 10% each | ≤ 8% each | GoPlus |
| LP locked or burned | ≥ 70% of LP | ≥ 80% of LP | ≥ 90% of LP | RugCheck |
| Top-10 holder % (ex-LP) | ≤ 30% | ≤ 28% | ≤ 25% | RugCheck |
| Dev/deployer wallet % | ≤ 8% | ≤ 6% | ≤ 5% | RugCheck |
| Unique holders | ≥ 50 | ≥ 100 | ≥ 150 | RugCheck |
| RugCheck normalized risk score | ≤ 40 | ≤ 35 | ≤ 30 | RugCheck |
| Insider/bundle wallet concentration | No flag | No flag | No flag | RugCheck |
| Pair age within mode window | — | — | — | DexScreener |

### 2.2 Caution thresholds (tighter — clearing hard but not these = "Caution", not "Pass")

```python
CAUTION_MARGIN = {
    "liquidity_usd": 1.5,      # 1.5x the hard minimum required for a clean Pass
    "top10_holder_pct": 0.85,  # must be ≤ 85% of the hard-reject ceiling
    "dev_wallet_pct": 0.75,
    "rugcheck_risk_score": 0.7,
}
```

### 2.3 Pseudocode

```python
def evaluate_safety(token: TokenSafetyData, mode: Mode) -> SafetyResult:
    hard = HARD_THRESHOLDS[mode]
    fails = []

    if token.liquidity_usd < hard.min_liquidity_usd:
        fails.append(f"Liquidity ${token.liquidity_usd:,.0f} below ${hard.min_liquidity_usd:,.0f} minimum")
    if not token.mint_authority_revoked:
        fails.append("Mint authority NOT revoked — supply can be inflated at will")
    if not token.freeze_authority_revoked:
        fails.append("Freeze authority NOT revoked — holder wallets can be frozen")
    if token.is_honeypot:
        fails.append("Honeypot detected — sells are blocked or fail")
    if token.buy_tax_pct > hard.max_tax_pct or token.sell_tax_pct > hard.max_tax_pct:
        fails.append(f"Tax too high: buy {token.buy_tax_pct}%, sell {token.sell_tax_pct}%")
    if token.lp_locked_pct < hard.min_lp_locked_pct:
        fails.append(f"Only {token.lp_locked_pct}% of LP locked/burned")
    if token.top10_holder_pct > hard.max_top10_pct:
        fails.append(f"Top 10 holders control {token.top10_holder_pct}% — concentration risk")
    if token.dev_wallet_pct > hard.max_dev_pct:
        fails.append(f"Dev wallet holds {token.dev_wallet_pct}% of supply")
    if token.unique_holders < hard.min_holders:
        fails.append(f"Only {token.unique_holders} holders, below {hard.min_holders} minimum")
    if token.rugcheck_risk_score > hard.max_rugcheck_score:
        fails.append(f"RugCheck risk score {token.rugcheck_risk_score} exceeds {hard.max_rugcheck_score} ceiling")
    if token.insider_bundle_flag:
        fails.append("RugCheck flagged insider/bundle wallet concentration")

    if fails:
        return SafetyResult(grade="Fail", reasons=fails)

    cautions = []
    caution = CAUTION_THRESHOLDS[mode]
    if token.liquidity_usd < hard.min_liquidity_usd * CAUTION_MARGIN["liquidity_usd"]:
        cautions.append("Liquidity clears the minimum but isn't comfortably above it")
    if token.top10_holder_pct > hard.max_top10_pct * CAUTION_MARGIN["top10_holder_pct"]:
        cautions.append("Holder concentration near the reject ceiling")
    if token.dev_wallet_pct > hard.max_dev_pct * CAUTION_MARGIN["dev_wallet_pct"]:
        cautions.append("Dev wallet holding near the reject ceiling")
    if token.rugcheck_risk_score > hard.max_rugcheck_score * CAUTION_MARGIN["rugcheck_risk_score"]:
        cautions.append("RugCheck score elevated, though under the hard ceiling")

    grade = "Caution" if cautions else "Pass"
    return SafetyResult(grade=grade, reasons=cautions)
```

---

## 3. Hype & Virality

### 3.1 Sub-metrics

```
mention_velocity_ratio   = mentions_last_15min / baseline_mentions_per_15min
                            (baseline = trailing hourly average, or a small
                             fixed floor for brand-new tokens with no baseline yet)

acceleration_ratio        = mention_velocity_ratio_now / mention_velocity_ratio_prior_window
                            (is the velocity itself increasing? — 2nd derivative)

kol_boost                 = additive, capped at +15 (same pattern as the main
                             scanner's social pillar) — requires a tracked KOL
                             list or LunarCrush creators tier; returns 0 if
                             unavailable, never fabricated

trending_score             = 100 if DexScreener-boosted/trending right now,
                              else normalize(boost_amount_paid, 0, 500)

social_presence_score       = (has_real_website + has_twitter + has_telegram) / 3 * 100
                              ("real" = not a dead link / not a placeholder —
                              best-effort check, not guaranteed)
```

### 3.2 Composite

```python
def compute_hype(data: HypeInputData) -> FactorResult:
    velocity_score = normalize(data.mention_velocity_ratio, 1.0, 5.0)
    acceleration_score = normalize(data.acceleration_ratio, 0.5, 3.0)
    trending_score = 100.0 if data.dex_boosted else normalize(data.boost_amount, 0, 500)
    social_score = ((data.has_website + data.has_twitter + data.has_telegram) / 3) * 100

    composite = (
        velocity_score * 0.35
        + acceleration_score * 0.30
        + trending_score * 0.20
        + social_score * 0.15
    )
    composite = clamp(composite + data.kol_boost, 0, 100)

    return FactorResult(name="hype", score=composite, reasons=[
        f"Mention velocity {data.mention_velocity_ratio:.1f}x baseline",
        f"Velocity {'accelerating' if data.acceleration_ratio > 1 else 'decelerating'} ({data.acceleration_ratio:.2f}x)",
        f"{'Currently boosted/trending on DexScreener' if data.dex_boosted else 'Not currently boosted'}",
    ])
```

### 3.3 Hype Level (categorical, for the final output)

```python
def hype_level(hype_score: float) -> str:
    if hype_score >= 85: return "Explosive"
    if hype_score >= 65: return "High"
    if hype_score >= 40: return "Medium"
    return "Low"
```

**Honest limitation, stated plainly**: LunarCrush (the mention-velocity
source already integrated) lags for tokens that are literally minutes
old — it wasn't built to track pump.fun-speed launches. For Sniper mode
specifically, expect `mention_velocity_ratio` to often be a weak or
missing signal, and expect `trending_score` (DexScreener's own boost/
trending data, which updates in real time) to carry more real weight
than the formula's 20% suggests at that age. Worth knowing before
tuning thresholds against backtests that don't reflect this lag.

---

## 4. On-chain Health

### 4.1 Sub-metrics

```
buyer_quality_score   = normalize(unique_makers_1h, mode.min_makers, mode.min_makers * 3)
                         (falls back to raw buy tx COUNT if unique-wallet data
                          isn't available — a real degradation, flagged in
                          reasons, not silently substituted)

buy_sell_score        = healthy_band(buy_sell_ratio, lo=0.8, hi=3.0, exhaustion_hi=8.0, exhaustion_lo=0.2)
                         (extreme skew either direction is suspicious, not just low)

holder_growth_score   = normalize(holder_growth_pct_1h, 0, 20)
                         (requires a stored snapshot from ~1h ago — same
                          pattern as the main scanner's OI history tracking)

liquidity_quality_score = normalize(volume_to_liquidity_ratio, 0.5, 5.0)
                           with a fade above ~10x (extreme ratio reads as
                           wash-trading suspicion, not organic activity —
                           same principle as the flag already in degen_radar.py)

organic_score          = 100 - wash_trading_penalty(token)
```

### 4.2 Wash-trading penalty (feeds `organic_score`)

```python
def wash_trading_penalty(token: OnchainData) -> float:
    penalty = 0.0
    if token.volume_to_liquidity_ratio > 15:
        penalty += 30  # extreme turnover relative to pool size
    if token.unique_makers_1h and token.buy_tx_count_1h:
        # many transactions from very few wallets = bot/wash pattern
        tx_per_wallet = token.buy_tx_count_1h / max(token.unique_makers_1h, 1)
        if tx_per_wallet > 4:
            penalty += 25
    if token.avg_tx_size_variance is not None and token.avg_tx_size_variance < 0.05:
        # suspiciously uniform transaction sizes
        penalty += 20
    return min(penalty, 70)  # cap — never let this alone zero out the score
```

### 4.3 Composite

```python
def compute_onchain(data: OnchainData) -> FactorResult:
    buyer_score = normalize(data.unique_makers_1h, data.mode_min_makers, data.mode_min_makers * 3)
    bs_score = healthy_band(data.buy_sell_ratio, 0.8, 3.0, 8.0, 0.2)
    growth_score = normalize(data.holder_growth_pct_1h, 0, 20) if data.holder_growth_pct_1h is not None else 50.0
    liq_quality = normalize(data.volume_to_liquidity_ratio, 0.5, 5.0)
    organic = 100 - wash_trading_penalty(data)

    composite = (
        buyer_score * 0.30
        + bs_score * 0.20
        + growth_score * 0.20
        + liq_quality * 0.15
        + organic * 0.15
    )
    return FactorResult(name="onchain", score=clamp(composite), reasons=[
        f"{data.unique_makers_1h} unique makers in the last hour" if data.unique_makers_1h else "Unique maker count unavailable — used tx count as a weaker proxy",
        f"Buy/sell ratio {data.buy_sell_ratio:.2f}",
        f"Organic-flow score {organic:.0f}/100 (wash-trading penalty: {wash_trading_penalty(data):.0f})",
    ])
```

---

## 5. Momentum & Flow

### 5.1 Sub-metrics

```
vol_accel_score           = normalize(vol_accel_ratio, 1.0, 4.0)
                             where vol_accel_ratio = (volume_15m annualized to 1h) / volume_1h_average

price_vol_alignment_score  = 80 if price_up and volume_up      # healthy: real demand
                            = 60 if price_up and not volume_up  # weaker: could be thin-book drift
                            = 20 if price_down and volume_up    # distribution / selling into strength
                            = 40 otherwise                        # quiet, no strong read either way

mcap_sweet_spot_score       = triangular_band(mcap, floor=50_000, peak=300_000, ceiling=2_000_000)
                              (peaks in the middle of your stated $50k–$2M
                              range, fades toward both edges rather than a
                              flat pass/fail — a $2.5M mcap coin isn't
                              instantly worthless, just past the sweet spot)
```

```python
def triangular_band(value: float, floor: float, peak: float, ceiling: float) -> float:
    if value <= floor or value >= ceiling:
        return 20.0  # still gets a low-but-nonzero score outside the band
    if value <= peak:
        return 20 + 80 * (value - floor) / (peak - floor)
    return 100 - 80 * (value - peak) / (ceiling - peak)
```

### 5.2 Price/volume divergence (explicitly requested — same logic pattern as the main scanner's OI divergence)

```python
def detect_momentum_divergence(price_change_pct: float, volume_change_pct: float) -> tuple[str, float]:
    """Direction-agnostic conviction read, same principle as ARCHITECTURE.md §2.2."""
    aligned = (price_change_pct >= 0) == (volume_change_pct >= 0)
    if aligned:
        return "confirmed", 75.0
    return "divergent", 30.0
    # "divergent" = price moving without volume backing it (or volume
    # building while price is flat/falling) — an exhaustion/weakness signal,
    # surfaced as a Risk Flag, not silently folded into the score
```

### 5.3 Composite

```python
def compute_momentum(data: MomentumData) -> FactorResult:
    vol_score = normalize(data.vol_accel_ratio, 1.0, 4.0)
    alignment_score = price_vol_alignment_score(data.price_change_pct, data.volume_change_pct)
    mcap_score = triangular_band(data.market_cap_usd, 50_000, 300_000, 2_000_000)

    composite = vol_score * 0.40 + alignment_score * 0.30 + mcap_score * 0.30
    return FactorResult(name="momentum", score=clamp(composite), reasons=[
        f"Volume acceleration {data.vol_accel_ratio:.1f}x",
        f"Price/volume alignment: {'confirmed' if alignment_score >= 70 else 'divergent' if alignment_score <= 30 else 'mixed'}",
        f"Market cap ${data.market_cap_usd:,.0f} ({'sweet spot' if 50_000 <= data.market_cap_usd <= 2_000_000 else 'outside typical range'})",
    ])
```

---

## 6. Mode-specific weights (resolves §0's tension with concrete numbers)

```python
MODE_WEIGHTS = {
    Mode.SNIPER: {           # hype dominant — almost the only signal this early
        "hype": 0.45, "onchain": 0.25, "momentum": 0.30,
    },
    Mode.EARLY_MOMENTUM: {   # closest to your stated ranges — the "base case"
        "hype": 0.40, "onchain": 0.32, "momentum": 0.28,
    },
    Mode.CONFIRMED_RUNNER: { # hype still leads (per explicit requirement that
                              # hype is always the strongest signal), but the
                              # gap to on-chain narrows with age rather than
                              # inverting — sustainability matters more by
                              # hour 3-24, just not enough to overtake hype
        "hype": 0.38, "onchain": 0.34, "momentum": 0.28,
    },
}
```

**Revision note**: an earlier draft of this table had On-chain (0.40) exceed
Hype (0.32) in Confirmed Runner, reasoning that sustainability matters more
than virality once a coin's proven itself for hours. That directly
contradicted the explicit requirement that hype is the strongest signal in
every mode — caught by a test asserting `weights["hype"] == max(weights.values())`
across all three modes, which failed against the original numbers. Fixed:
hype now leads in all three modes; the *gap* to on-chain narrows with age
(0.20pp in Sniper → 0.08pp in Confirmed Runner) rather than the ranking
inverting.

---

## 7. Full composite — Python pseudocode, end to end

```python
def score_meme_coin(token_address: str, mode: Mode, data_sources: DataSources) -> MemeScanResult:
    # 1. Gather raw data (concurrent fetch, graceful degradation per source)
    dex_data = data_sources.dexscreener.get_pair(token_address)
    safety_data = data_sources.rugcheck.get_token_report(token_address)  # + GoPlus cross-check
    hype_data = data_sources.build_hype_inputs(token_address, dex_data)
    onchain_data = data_sources.build_onchain_inputs(token_address, dex_data, safety_data)
    momentum_data = data_sources.build_momentum_inputs(dex_data)

    # 2. Age/mode check
    age_minutes = pair_age_minutes(dex_data.pair_created_at)
    if not within_mode_window(age_minutes, mode):
        return MemeScanResult(symbol=dex_data.symbol, mode=mode.value,
                               safety_grade="N/A", reasons=[f"Age {age_minutes}min outside {mode.value} window"])

    # 3. SAFETY GATE — must pass or Caution to proceed
    safety = evaluate_safety(safety_data, mode)
    if safety.grade == "Fail":
        return MemeScanResult(
            symbol=dex_data.symbol, token_address=token_address, mode=mode.value,
            safety_grade="Fail", safety_reasons=safety.reasons,
            opportunity_score=None, hype_level=None, pillar_scores=None,
            risk_flags=[DegenFlag(label=r, severity="danger") for r in safety.reasons],
            thesis="Rejected at the safety gate — no opportunity score computed.",
        )

    # 4. Pillars (only reached if Safety != Fail)
    hype = compute_hype(hype_data)
    onchain = compute_onchain(onchain_data)
    momentum = compute_momentum(momentum_data)

    weights = MODE_WEIGHTS[mode]
    opportunity_score = (
        hype.score * weights["hype"]
        + onchain.score * weights["onchain"]
        + momentum.score * weights["momentum"]
    )

    # 5. Divergence + risk flags
    div_direction, div_score = detect_momentum_divergence(momentum_data.price_change_pct, momentum_data.volume_change_pct)
    flags = build_risk_flags(safety, onchain_data, div_direction)

    # 6. Thesis — short, reuses the pillar reasons rather than inventing new copy
    thesis = build_thesis(hype, onchain, momentum, safety, div_direction)

    return MemeScanResult(
        symbol=dex_data.symbol, token_address=token_address, mode=mode.value,
        safety_grade=safety.grade, safety_reasons=safety.reasons,
        opportunity_score=round(clamp(opportunity_score), 1),
        hype_level=hype_level(hype.score),
        pillar_scores={"hype": hype.score, "onchain": onchain.score, "momentum": momentum.score},
        risk_flags=flags,
        thesis=thesis,
    )


def build_risk_flags(safety: SafetyResult, onchain: OnchainData, div_direction: str) -> list[DegenFlag]:
    flags = [DegenFlag(label=r, severity="warning") for r in safety.reasons]  # Caution-tier reasons, if any
    if div_direction == "divergent":
        flags.append(DegenFlag(label="Price/volume divergence — move may not be backed by real demand", severity="warning"))
    if wash_trading_penalty(onchain) > 30:
        flags.append(DegenFlag(label="Elevated wash-trading signal in volume pattern", severity="warning"))
    if not flags:
        flags.append(DegenFlag(label="No elevated risk signals beyond standard memecoin category risk", severity="info"))
    return flags


def build_thesis(hype, onchain, momentum, safety, div_direction) -> str:
    parts = [f"Safety: {safety.grade}."]
    parts.append(hype.reasons[0] if hype.reasons else "")
    parts.append(onchain.reasons[0] if onchain.reasons else "")
    if div_direction == "divergent":
        parts.append("Momentum divergence detected — treat with extra caution.")
    return " ".join(p for p in parts if p)
```

---

## 8. Final output shape (exactly as requested)

```python
@dataclass
class MemeScanResult:
    symbol: str
    token_address: str
    mode: str                          # "sniper" | "early_momentum" | "confirmed_runner"
    safety_grade: str                  # "Pass" | "Caution" | "Fail"
    safety_reasons: list[str]
    opportunity_score: Optional[float] # None if safety_grade == "Fail"
    hype_level: Optional[str]          # "Low" | "Medium" | "High" | "Explosive", None if Fail
    pillar_scores: Optional[dict]      # {"hype":.., "onchain":.., "momentum":..}, None if Fail
    risk_flags: list[DegenFlag]
    thesis: str
```

---

## 10. Phase 5 — product layer (alerts, explainability, performance tracking, deployer blacklist)

**Built and tested:**
- **`meme_storage.py`** — scan history, deployer blacklist, forward-return performance tracking. Same honest limitations as the main scanner's backtest (no fees/slippage/position sizing).
- **Deployer blacklist wired into the Safety gate directly** — checked first, before any other logic. A blacklisted deployer fails a token regardless of how clean everything else looks. Auto-grows: a Fail caused specifically by an insider/bundle/honeypot finding automatically blacklists that deployer for every future token, verified with a real two-token test (a clean-looking second token from a known-bad deployer correctly rejected).
- **Structured `RiskFlag`/`HypeEvent`** (label + severity), replacing plain strings — matches `degen_models.py::DegenFlag`'s shape for consistency across both scanners.
- **`generate_thesis` rewritten** to cite specific hype evidence (boost status, velocity ratio, KOL activity) and specific on-chain evidence (unique makers, holder growth, buy/sell ratio) rather than blindly taking the first generated reason — directly satisfies "clear thesis focusing on why the hype is real + on-chain support."
- **`meme_hype_events.py`** — delta-based detection (velocity jumps, newly-boosted status, KOL score jumps, hype pillar score jumps). Reads the token's *previous* scan from storage; the key distinction from normal pillar scoring is that this detects *change*, not current state.
- **`meme_alerts.py`** — "only high-conviction + safety passed + rising hype" enforced as three independent required conditions, not compensating factors. A high score with zero hype events never alerts.

**Verified end-to-end, not just in isolation** — two dedicated integration tests actually call `run_scan()` itself (not just its component pieces): one runs two consecutive scans of the same token with a hype event appearing between them, confirming storage → hype detection → alert dispatch genuinely work together; the other confirms the deployer blacklist grows from a real Fail and correctly rejects a completely different, individually-clean token from that same deployer on a fresh scan cycle.

**Real bugs caught along the way, not assumed away:**
- The `RiskFlag`/`HypeEvent` type change broke `meme_main.py`'s flag concatenation and rendering, and `meme_storage.py`'s JSON serialization — all three passed import checks but would have crashed at runtime; found by actually running the code, not by the type checker.
- A test using empty risk-flag lists meant the rendering bug above wouldn't have been caught by that test either — fixed the test's coverage gap, not just the code.
- **The same hardcoded-timestamp bug recurred a third time** (`test_meme_integration.py`, copy-pasted from the pattern that had already caused two prior failures) — this time fixed at the root: added `tests/_time_helpers.py` with a `relative_iso_timestamp()` helper and migrated all three affected tests to use it, rather than patching each occurrence individually as it resurfaces.
- A `str_replace` edit accidentally merged a comment with a real constructor argument (`fdv_usd=400_000,` silently absorbed into a comment line), dropping it from the test's synthetic data — caught by rereading the file after the edit, not by a test failure.

## 9. What's real vs. what's stubbed — updated after the data layer build

**Built and tested (Phase 3):**
- DexScreener (liquidity, volume, tx counts, pair age, **now also**: multi-timeframe m5/h1/h6/h24 buckets, boost/trending status, social link presence, venue classification) — `data_sources/dexscreener.py`
- RugCheck.xyz (mint/freeze authority, LP lock %, top-10 holder %, insider detection, risk score) — `data_sources/rugcheck.py`, free, no key
- GoPlus Security (honeypot, buy/sell tax, chain-aware Solana vs EVM routing) — `data_sources/goplus.py`, free tier
- `meme_aggregator.py` — ties all three (+ optional LunarCrush) into a `MemeCoinMetrics` ready for `ScoringEngine.score()`, with venue-aware field mapping and explicit cross-source conflict detection

**Real gaps, unchanged from the design doc — still need a decision:**
- Unique makers vs raw tx count — DexScreener gives tx count, not unique wallets. Needs Birdeye (paid) or accept the degraded tx-count proxy.
- Holder growth rate — needs our own snapshot history (same pattern as the main scanner's OI history), not available as a single API call anywhere.
- KOL boost — same stub as the main scanner's social pillar; returns `None` until a real KOL list or LunarCrush creators tier is wired up.
- Mention velocity for Sniper-mode coins — LunarCrush lags real-time for brand-new launches; DexScreener's own `is_boosted`/`boost_amount` (now wired up) is the more reliable real-time signal at this age.
- `avg_tx_size_variance` (wash-trading signal) — DexScreener doesn't expose per-transaction size, only aggregate counts; this field stays `None` until a source that does (Birdeye, or raw Solana RPC transaction parsing) is added.
