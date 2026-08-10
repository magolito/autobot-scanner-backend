# Opportunity Scanner — Architecture & Scoring Engine v2

This is the full formula-level spec behind `opportunity_scanner/`. Every
sub-metric below maps to a real function in the codebase (file/function
noted in parentheses). Where a metric is new in this revision, that's
flagged — the previous version (equal-ish weights, no confidence score)
is now superseded by this one.

---

## 0. Hard Filters (run BEFORE scoring — `filters.py`)

A coin failing any of these is excluded from results, not scored low.
Scoring a data-starved or unlistable coin produces a number that *looks*
meaningful but isn't — exclusion is more honest than a bad score.

```python
def passes_hard_filters(coin) -> tuple[bool, list[str]]:
    checks = [
        coin.volume_24h_usd            >= 5_000_000,     # min $5M 24h volume
        coin.market_cap_usd            >= 10_000_000,    # min $10M market cap
        coin.exchange_listings         >= 1,              # listed on ≥1 tracked major CEX
        coin.bid_ask_spread_pct        <= 1.5,             # reject thin/unreliable books
        len(coin.ohlcv["1d"])          >= 50,               # enough history for indicators
    ]
    return all(checks), [reason for check, reason in zip(checks, REASONS) if not check]
```

Defaults are configurable per-deployment in `config.py::QualityFilters`.

---

## 1. Strength Pillar — 25% (`factors/strength.py`)

### 1.1 Relative Strength vs BTC and vs Sector

Computed per timeframe (1h, 4h, 1d), then blended:

```
RS_btc(tf)    = coin_return(tf) - btc_return(tf)
RS_sector(tf) = coin_return(tf) - sector_avg_return(tf)

RS_blended = Σ tf_weight[tf] * (0.65 * RS_btc(tf) + 0.35 * RS_sector(tf))
             for tf in [1h, 4h, 1d], tf_weight = {1h: 0.2, 4h: 0.35, 1d: 0.45}

RS_score = normalize(RS_blended, lo=-20, hi=20)   # → 0-100
```

`sector_avg_return` needs a sector map (e.g. L1s, DeFi, memecoins, AI
tokens) — the equal-weighted average return of the coin's sector peers
over the same timeframe. **New in v2**: `config.py::SECTOR_MAP` and
`data_sources/exchange.py::fetch_sector_returns()`.

### 1.2 Volume Quality — three sub-components

```
volume_surge   = current_24h_volume / avg_volume_20d - 1        # % surge
volume_profile = % of last 20 candles' volume concentrated in
                  the current price's value area (70% zone)       # 0-1
obv_slope      = linear_regression_slope(OBV, window=14)          # normalized

VolQuality = normalize(volume_surge, -0.3, 2.0) * 0.4
           + normalize(volume_profile, 0, 1)     * 0.3
           + normalize(obv_slope, -1, 1)          * 0.3
```

`OBV` = On-Balance Volume, standard cumulative running total (`ta.volume.on_balance_volume`).
**New in v2**: volume_surge and obv_slope; volume_profile approximated as
% of recent volume within ±1 ATR of current price (a lightweight value-area proxy).

### 1.3 Market Structure — higher highs/lows + break of structure

```
structure_score = 85 if (higher_high AND higher_low)                    # confirmed uptrend
                 = 15 if (lower_high AND lower_low)                     # confirmed downtrend
                 = 70 if break_of_structure_bullish                     # BOS up: closed above prior swing high
                 = 30 if break_of_structure_bearish                     # BOS down: closed below prior swing low
                 = 50 otherwise                                          # range/undefined
```

`break_of_structure_bullish` = most recent candle's close exceeds the
highest high of the prior N=20 candles' *swing highs* (local maxima) —
i.e., the market just did something it hasn't done recently. **New in v2**:
explicit BOS detection layered on top of the v1 higher-highs/lows read.

### 1.4 Liquidity Score

```
LiquidityScore = normalize(24h_volume_usd / market_cap_usd, 0, 0.35) * 0.6
               + normalize(1 / max(bid_ask_spread_pct, 0.01), 0, 100) * 0.4
```

### Strength Pillar Composite

```
Strength = RS_score * 0.35 + VolQuality * 0.25 + Structure * 0.25 + Liquidity * 0.15
```

---

## 2. Open Interest Pillar — 25% (`factors/oi_dynamics.py`)

### 2.1 OI Change, multi-timeframe

```
oi_change_1h  = pct_change(oi_history, window=1h)
oi_change_4h  = pct_change(oi_history, window=4h)
oi_change_24h = pct_change(oi_history, window=24h)

OI_change_score = normalize(
    oi_change_1h * 0.2 + oi_change_4h * 0.35 + oi_change_24h * 0.45,
    lo=-25, hi=25
)
```

### 2.2 Price vs OI Divergence Matrix

```
                    OI rising              OI falling
price rising    →   bullish confirmed  |   short covering (weak)
price falling   →   bearish confirmed  |   long liquidation (capitulation, weak)
```

```python
def divergence_score(price_chg_pct, oi_chg_pct) -> float:
    aligned = (price_chg_pct >= 0) == (oi_chg_pct >= 0)
    magnitude = normalize(abs(oi_chg_pct), 0, 20)
    return clamp(60 + magnitude * 0.4) if aligned else clamp(40 - magnitude * 0.4)
```

This scores *conviction strength*, direction-agnostic — direction comes
from the Momentum pillar. A confirmed downtrend and a confirmed uptrend
both score high here; an unconfirmed move (either direction) scores low.

### 2.3 Funding Rate — extremes + trend

```
funding_extreme_score = 100 - normalize(abs(funding_rate_bps), 0, 50)   # near-zero = healthy
funding_trend_score   = 70 if funding_accelerating_toward_zero          # crowding unwinding
                       = 30 if funding_accelerating_away_from_zero      # crowding building
                       = 50 otherwise

FundingScore = funding_extreme_score * 0.6 + funding_trend_score * 0.4
```

**New in v2**: the trend component (is crowding building or unwinding),
not just the static extreme level from v1.

### 2.4 Aggregated Long/Short Ratio — top traders + global

```
LS_score = normalize(100 - |top_trader_ratio - 1.0| * 50, 0, 100) * 0.6
         + normalize(100 - |global_account_ratio - 1.0| * 50, 0, 100) * 0.4
```

Top-trader ratio (Bybit: `/v5/market/account-ratio` filtered to top-position
accounts where the venue exposes it) is weighted higher than the global
retail ratio — retail positioning is a noisier, more contrarian signal.
**Note**: not every exchange exposes a distinct "top trader" ratio; where
unavailable, falls back to global ratio alone with weight redistributed.

### 2.5 OI / Market Cap Ratio

```
OI_MCap_score = normalize(open_interest_usd / market_cap_usd, 0, 0.5)
```

High OI relative to market cap = heavy derivatives interest relative to
the coin's actual size — read as elevated volatility/liquidation-cascade
risk, folded in as a mild dampener rather than a reward.

### OI Pillar Composite

```
OI_Dynamics = OI_change_score * 0.25
            + divergence_score * 0.35
            + FundingScore * 0.20
            + LS_score * 0.15
            - max(0, OI_MCap_score - 70) * 0.10   # dampener only kicks in above 70
```

---

## 3. Trend & Momentum Pillar — 25% (`factors/momentum.py`)

### 3.1 Multi-Timeframe Trend Alignment

```
EMA_stack_score(tf) = 100 if EMA9 > EMA21 > EMA50 > EMA200 (full bullish stack)
                     = 0   if EMA9 < EMA21 < EMA50 < EMA200 (full bearish stack)
                     = normalize(count_correctly_ordered_pairs, 0, 6) * 100  # partial credit, else

SuperTrend_score(tf) = 100 if close > SuperTrend_line else 0

ADX_DI_score(tf) = adx_strength_component * 0.5 + di_direction_component * 0.5
  where adx_strength_component = normalize(ADX, 15, 40)          # trend strength, direction-agnostic
        di_direction_component = 100 if +DI > -DI else 0          # direction confirmation

TF_score(tf) = EMA_stack_score * 0.4 + SuperTrend_score * 0.3 + ADX_DI_score * 0.3
```

**New in v2**: full EMA9/21/50/200 stack (v1 used EMA20/50 only),
SuperTrend, and ADX/+DI/-DI explicitly (v1 folded trend strength into the
EMA spread only).

`SuperTrend` (10, 3.0 multiplier is a common default) isn't in the `ta`
library — implemented manually from ATR:
```python
basic_upper = (high+low)/2 + multiplier * ATR
basic_lower = (high+low)/2 - multiplier * ATR
# then the standard trailing/flip logic to lock in the band
```

### 3.2 Momentum Quality

```
RSI_score        = healthy_band_score(RSI, 50, 75) with exhaustion fade above 75/below 25
MACD_slope_score  = 80 if macd_hist rising & positive
                   = 60 if macd_hist positive but flattening
                   = 40 if macd_hist negative but flattening
                   = 20 if macd_hist falling & negative
ROC_score          = normalize(Rate_of_Change(14), -15, 15)
Stoch_score         = healthy_band_score(Stochastic %K, 40, 80) with exhaustion fade above 90/below 10

Momentum_quality = RSI_score * 0.3 + MACD_slope_score * 0.3 + ROC_score * 0.2 + Stoch_score * 0.2
```

**New in v2**: Rate of Change and Stochastic added alongside RSI/MACD.

### 3.3 Momentum Divergence Detection

```python
def detect_divergence(price_series, indicator_series, window=14) -> str:
    price_higher_high = price_series[-1] > max(price_series[-window:-1])
    ind_lower_high     = indicator_series[-1] < max(indicator_series[-window:-1])
    if price_higher_high and ind_lower_high:
        return "bearish_divergence"   # price makes new high, RSI/MACD doesn't confirm

    price_lower_low = price_series[-1] < min(price_series[-window:-1])
    ind_higher_low   = indicator_series[-1] > min(indicator_series[-window:-1])
    if price_lower_low and ind_higher_low:
        return "bullish_divergence"   # price makes new low, RSI/MACD doesn't confirm

    return "none"
```

```
Divergence_score = 25 if bearish_divergence detected on RSI or MACD
                  = 75 if bullish_divergence detected on RSI or MACD
                  = 50 if none
```

**New in v2** — v1 had no divergence detection at all.

### Trend & Momentum Pillar Composite

```
per_tf = { tf: TF_score(tf) * 0.6 + Momentum_quality(tf) * 0.4  for tf in [15m, 1h, 4h, 1d] }
blended = Σ timeframe_weight[tf] * per_tf[tf]

Momentum_pillar = blended * 0.85 + Divergence_score * 0.15
```

---

## 4. Social Virality Pillar — 25% (`factors/social.py`)

### 4.1 Mention Velocity vs Baseline

```
velocity_7d  = social_volume_24h / avg(social_volume, trailing_7d)  - 1
velocity_30d = social_volume_24h / avg(social_volume, trailing_30d) - 1

Velocity_score = normalize(velocity_7d * 0.6 + velocity_30d * 0.4, -0.5, 2.0)
```

**New in v2**: dual baseline (7d AND 30d) — v1 used a single baseline.
A spike vs both is a much stronger "going viral" signal than vs either alone.

### 4.2 Engagement Quality (weighted, not raw volume)

```
engagement_weighted = likes*1.0 + replies*2.5 + retweets*3.0 + quote_tweets*2.0
                       # replies/retweets weighted higher — they require more
                       # effort than a like, so signal stronger conviction

Engagement_score = normalize(engagement_weighted / social_volume_24h, 0, 25)
```

**New in v2**: weighted engagement composite — v1 used raw
interactions-per-mention without differentiating engagement types.

### 4.3 Sentiment Shift

```
sentiment_shift = current_sentiment - sentiment_Ndays_ago

Sentiment_score = normalize(current_sentiment, 30, 80) * 0.4
                 + normalize(sentiment_shift, -20, 20)   * 0.6
```

(Same as v1 — this formula already prioritized shift over level correctly.)

### 4.4 Mindshare Growth

```
mindshare_now  = f(galaxy_score, alt_rank)         # as in v1
mindshare_prior = f(galaxy_score_7d_ago, alt_rank_7d_ago)

Mindshare_score = normalize(mindshare_now, 0, 100) * 0.5
                 + normalize(mindshare_now - mindshare_prior, -20, 20) * 0.5
```

**New in v2**: mindshare *growth* (delta), not just current level.

### 4.5 Influencer / KOL Activity Boost

```
kol_score = normalize(
    Σ (follower_count_weight(influencer) * post_count(influencer))
    for influencer in top_20_crypto_kols if influencer.mentioned(coin, last_24h)
, 0, threshold)

KOL_boost = min(kol_score * 0.15, 15)   # additive bonus, capped at +15 points
```

**New in v2** — entirely new sub-metric. This is additive on top of the
weighted blend below (capped, so it can meaningfully lift a score but
can't single-handedly carry a coin to "Strong Buy").

### Social Pillar Composite

```
Social = (Velocity_score * 0.35 + Mindshare_score * 0.30
          + Sentiment_score * 0.20 + Engagement_score * 0.15) + KOL_boost
Social = clamp(Social, 0, 100)
```

---

## 5. Composite Opportunity Score

```python
DEFAULT_WEIGHTS = {
    "strength":    0.22,
    "oi_dynamics": 0.28,
    "momentum":    0.25,
    "social":      0.25,
}

def composite_score(pillar_scores: dict, weights: dict, available: dict) -> float:
    missing = [k for k, ok in available.items() if not ok]
    w = redistribute_missing_weight(weights, missing)   # proportional, see config.py
    return sum(pillar_scores[k] * w[k] for k in pillar_scores)
```

**Weight change from v2**: OI Dynamics increased 25%→28%, Strength reduced
25%→22%. Rationale: derivatives positioning (OI + funding + long/short)
reflects real capital being committed right now — harder to fake than
price action alone, which a single large market order can move. Strength
(relative performance, volume, structure) is valuable but slower-moving
and more correlated with Momentum than OI is, so it carries slightly less
independent weight.

### 5.0 Regime Awareness (BTC Filter) — **new in this revision**

Everything above scores a coin in isolation. But the same "Strong Buy" on
an altcoin means something very different when BTC itself is in a healthy
uptrend versus when BTC is breaking down — alts overwhelmingly beta-trade
off BTC, so a bullish alt signal during a BTC risk-off regime is much more
likely to be a trap (a relief bounce inside a larger downtrend) than real
strength. This pillar doesn't score the coin — it adjusts how much to
trust a bullish call on it.

```python
def compute_market_regime(btc_snapshot, timeframe_config) -> RegimeResult:
    btc_momentum = compute_momentum(btc_snapshot, timeframe_config)  # reuse pillar 3, on BTC itself

    daily = btc_snapshot.ohlcv["1d"]
    daily_returns = daily["close"].pct_change().dropna()
    realized_vol = daily_returns.tail(20).std() * sqrt(365)   # annualized realized vol
    vol_score = 100 - normalize(realized_vol, 0.3, 1.2)         # lower vol = healthier regime

    regime_score = btc_momentum.score * 0.7 + vol_score * 0.3

    if regime_score >= 65:
        label = "Risk-On"
    elif regime_score <= 35:
        label = "Risk-Off"
    else:
        label = "Neutral"

    return RegimeResult(label=label, score=regime_score,
                         btc_momentum_score=btc_momentum.score, volatility_score=vol_score)
```

**Applying the regime filter** — this happens AFTER a coin's own
composite score is computed, and only dampens bullish-leaning scores; it
never inflates them, and it never touches bearish/neutral calls (a
"Caution" or "Strong Avoid" during Risk-Off is, if anything, more
trustworthy, not less):

```python
REGIME_DAMPENER_RISK_OFF = 12   # points

def apply_regime_filter(composite_score, regime, is_btc_itself) -> tuple[float, str | None]:
    if is_btc_itself:
        return composite_score, None   # BTC is the regime anchor, doesn't dampen itself

    if regime.label == "Risk-Off" and composite_score > 50:
        adjusted = clamp(composite_score - REGIME_DAMPENER_RISK_OFF, 0, 100)
        note = (f"Dampened {REGIME_DAMPENER_RISK_OFF}pts: BTC regime is Risk-Off "
                f"(regime score {regime.score:.0f}) — bullish alt signals need extra scrutiny here")
        return adjusted, note

    return composite_score, None
```

The regime itself is computed **once per scan cycle** (not once per
coin) and reused across every coin in that batch — it's a property of
the market, not of any individual coin. `ScanResult` carries the regime
label/score/note it was scored under, so results stay explainable even
when reviewed later.

### 5.1 Confidence Score — **new in v2**

The composite score alone doesn't tell you how *trustworthy* that number
is. A coin with all four pillars fully populated and internally agreeing
deserves more trust than one running on two pillars with the other two
neutral-defaulted. Confidence is reported alongside the score, not folded
into it — folding it in would conflate "this coin is weak" with "we don't
have enough data to know."

```python
def confidence_score(factors: dict, weights: dict) -> float:
    # Component A: data completeness — how much of the intended weight
    # is backed by real data vs redistributed/defaulted
    available_weight = sum(weights[k] for k, f in factors.items() if f.available)
    completeness = available_weight  # already 0-1 since weights sum to 1.0

    # Component B: signal agreement — do the available pillars broadly
    # agree, or are they contradicting each other (e.g. strength says 85,
    # OI says 20)? High variance across pillars = lower confidence even
    # if every pillar individually has good data.
    scores = [f.score for f in factors.values() if f.available]
    agreement = 1.0 - normalize(stdev(scores), 0, 35) / 100  # 0-1, higher = more agreement

    return round((completeness * 0.5 + agreement * 0.5) * 100, 1)  # 0-100
```

### 5.2 Signal Grading

```
score >= 80              → "Strong Buy"
65 <= score < 80          → "Buy"
45 <= score < 65           → "Neutral"
25 <= score < 45            → "Caution"
score < 25                   → "Strong Avoid"
```

Confidence is reported as a qualifier, not a grade of its own:
`"Buy (72.4, confidence: High)"` where confidence bands are
High ≥75, Medium 50–74, Low <50.

---

## 6. Full Composite — Python Pseudocode, End to End

```python
def score_coin(coin_data, config) -> ScanResult:
    passed, filter_notes = passes_hard_filters(coin_data, config.filters)
    if not passed:
        return ScanResult(passed_filters=False, filter_notes=filter_notes)

    factors = {
        "strength":    compute_strength(coin_data, btc_data, sector_data),
        "oi_dynamics": compute_oi_dynamics(coin_data),
        "momentum":    compute_momentum(coin_data, timeframes=["15m","1h","4h","1d"]),
        "social":      compute_social(coin_data),
    }

    available = {k: f.available for k, f in factors.items()}
    weights = redistribute_missing_weight(config.weights, missing=[k for k,v in available.items() if not v])

    composite = sum(factors[k].score * weights[k] for k in factors)
    signal = grade(composite, config.signal_bands)
    confidence = confidence_score(factors, weights)

    reasons = top_reasons_by_contribution(factors, weights, n=5)
    risk_tier = classify_risk_tier(coin_data.market_cap_rank, coin_data.market_cap_usd)

    return ScanResult(
        composite_score=composite,
        signal=signal,
        confidence=confidence,
        factors=factors,
        weights_used=weights,
        reasons_summary=reasons,
        risk_tier=risk_tier,
        passed_filters=True,
    )
```

---

## What's implemented vs. spec'd-for-next-pass

**Implemented in `opportunity_scanner/` as of this revision:**
Strength (RS vs BTC, volume quality w/ surge + OBV slope, structure w/ BOS,
liquidity), OI (change, divergence, funding w/ trend, OI/mcap ratio),
Momentum (EMA9/21/50/200 stack, SuperTrend, ADX/DI, RSI/MACD/ROC/Stochastic,
divergence detection), Social (dual-baseline velocity, weighted engagement,
sentiment shift, mindshare growth), composite scoring with redistribution,
confidence score, equal 25/25/25/25 default weights.

**Spec'd here but needs a data source not yet wired up:**
- Sector relative strength — needs a maintained sector map (which coins
  belong to L1/DeFi/AI/memecoin etc.) — `config.py::SECTOR_MAP` ships with
  a starter list, expand as needed.
- Top-trader long/short ratio — Bybit's public endpoint returns global
  account ratio; a distinct "top trader" cut isn't consistently available
  on Bybit's public tier. Falls back to global-only with weight
  redistributed until/unless you add a venue or paid data source that
  exposes it.
- KOL/influencer boost — needs LunarCrush's creators endpoint (a higher
  API tier) or a maintained list of tracked KOL accounts. Stubbed to
  return `available=False` (no boost applied) until wired up.
