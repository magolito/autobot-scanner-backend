# AutoBot Opportunity Scanner

A multi-factor crypto scanner that ranks coins by a composite **Opportunity
Score (0-100)**, built from four independently-testable pillars:

| Pillar | Weight (default) | What it reads |
|---|---|---|
| **Strength** | 30% | Relative strength vs BTC, volume quality/depth, market structure (higher highs/lows) |
| **Open Interest Dynamics** | 20% | OI change, price/OI divergence, funding rate, long/short ratio |
| **Trend & Momentum** | 30% | Multi-timeframe (15m/1h/4h/1d) EMA trend, RSI, MACD |
| **Social Virality** | 20% | Mention velocity, engagement quality, sentiment shift, mindshare (LunarCrush) |

Every score comes with a plain-language explanation trail — nothing is a
black box. Output is a signal grade: **Strong Buy / Buy / Neutral /
Caution / Strong Avoid**.

## Why this architecture

- **Each pillar lives in its own file** (`factors/strength.py`,
  `factors/oi_dynamics.py`, `factors/momentum.py`, `factors/social.py`) with
  no cross-dependencies. You can develop, test, or replace any one of them
  without touching the others.
- **Missing data doesn't tank a score.** If a coin isn't listed on
  derivatives (no OI/funding data) or isn't tracked by LunarCrush, that
  pillar returns `available=False` and its weight is proportionally
  redistributed across the pillars that *do* have data (see
  `config.py::Weights.redistribute_missing`). A coin is never penalized
  for a data gap that isn't its fault.
- **Quality filters run before scoring, not as part of it** (`filters.py`).
  Illiquid, unlistable, or data-starved coins are excluded from results
  entirely rather than scored artificially low — this keeps false
  positives/negatives from noise out of the ranked list.
- **Risk tier is separate from the score** (`risk.py`). A brand-new,
  thin-liquidity coin with genuine momentum + OI confirmation + real
  social virality can and should score highly — that's the point of a
  scanner that catches real strength early. It just carries a `high_risk`
  tag alongside the score so the read stays honest about liquidity.

## Phase 3 additions: caching, storage, whale data, scheduling

- **`cache.py`** — shared `TTLCache` (swap for Redis later, interface is
  compatible) plus `@with_retry` for exponential-backoff retries on
  transient network failures. Wired into `data_sources/exchange.py`
  already (ticker: 20s TTL, OHLCV: scales by timeframe, OI: 5min TTL).
- **`storage.py`** — SQLite persistence for every scan result and OI
  snapshot. This is what makes `GET /backtest/{signal}` possible: a rough
  sanity-check on whether "Strong Buy" calls have actually outperformed
  (no fees/slippage modeled — it's a directional sanity check, not a
  trading backtest).
- **`data_sources/whale.py`** — Whale Alert integration, ported from the
  old Node `scanner-backend/` (now retired — see below). Surfaced via
  `GET /whales/{symbol}` as supplementary context, deliberately NOT
  blended into the composite score.
- **`scheduler.py`** — `ScannerPoller` runs `scan_many()` on an interval
  (default 15 min) and persists results, enabling `get_history()` and
  `backtest_signal()` to have something to work with. Start it via
  `POST /poller/start`, or run standalone: `python -m opportunity_scanner.scheduler`.
  In production, run this as its own worker process, not inside the
  request-handling API process.

### The old Node backend is retired

`scanner-backend/` (Node/Express, previously handled the HTML scanner's
social + whale layers) is superseded by this package. Its functionality
now lives in `data_sources/social.py` and `data_sources/whale.py`. Point
`scanner.html`'s `SOCIAL_API_BASE` at this FastAPI service instead once
it's deployed (Phase 4).

## Two scoring interfaces — same formulas, different entry points

**`scanner.py` + `factors/*.py`** — the "full" async pipeline. Takes a
symbol, pulls OHLCV/OI/social from live data sources, computes everything
from raw time series. This is what the FastAPI service and the scheduler
use.

**`scoring_engine.py`** — a decoupled, synchronous, metrics-in interface
(`ScoringEngine.score(CoinMetrics) -> FinalResult`). It takes
already-computed scalar values (an RSI reading, an OI % change, a galaxy
score) rather than raw OHLCV, and does only the weighting/blending/
grading/explaining. Useful when:
- you already have metrics from elsewhere (a different pipeline, a
  webhook, a spreadsheet import) and just want them scored
- you want to unit-test scoring logic without touching an exchange at all
- you want a lightweight scoring pass that doesn't need pandas/ccxt

Both implement the exact same formulas from `ARCHITECTURE.md` — they're
two interfaces onto one scoring model, not two different models. Every
`calculate_*` method in `scoring_engine.py` is robust to missing fields:
a sub-score just doesn't contribute if its inputs aren't there, rather
than crashing or silently defaulting to something misleading. See
`tests/test_scoring_engine.py` for a worked example, including a
mostly-empty `CoinMetrics` that still scores without errors (defaults to
neutral 50 on every pillar it has zero data for).

## Resilience layer: circuit breakers, Redis caching, multi-exchange fallback

- **`circuit_breaker.py`** — separate from retry (`cache.py`'s `@with_retry`
  handles a single transient blip). Circuit breaker handles a provider
  being genuinely down: after `failure_threshold` consecutive failures
  (default 5), the breaker OPENS and fails fast — no network call at all —
  for a cooldown period (default 60s), then allows one test call through
  (HALF_OPEN) to check recovery. Tested: opens on threshold, fails fast
  while open (verified zero network attempts), half-opens after cooldown,
  closes on a successful test call — all 5 behaviors pass.
- **`cache.py::make_cache()`** — factory that returns a `RedisCache` if
  `REDIS_URL` is set (for cache state shared across multiple process
  instances — the API server and scheduler worker hitting the same
  cache), or falls back to the existing in-memory `TTLCache` otherwise.
  Same interface either way — nothing calling the cache needs to know or
  care which one it got.
- **`data_sources/multi_exchange_oi.py`** — `MultiExchangeOIProvider`,
  two-tier: **CoinGlass** (paid, if `COINGLASS_API_KEY` is set) aggregates
  OI/funding/long-short *including top-trader ratio* across 6 exchanges in
  one call — this is what closes the "top-trader ratio not available" gap
  from the previous revision. Falls back to **direct Bybit + Hyperliquid**
  calls (free, no key) if CoinGlass isn't configured or its circuit is
  open, averaging OI/funding across both when both respond. See
  `PROVIDERS.md` for the full recommendation rationale and current pricing.
- **`provider_models.py`** — pydantic contracts (`OHLCVSeries`,
  `TickerData`, `OpenInterestData`, `FundingRateData`,
  `LongShortRatioData`, `DerivativesSnapshot`, `SocialMetrics`) every
  provider method returns. Every one carries `DataSourceMeta`
  (`source`, `is_stale`, `is_fallback`) — callers always know which tier
  actually answered, not just the number.

## Project structure

```
opportunity-scanner/
├── settings.yaml                     # tunable settings — weights, filters, timeframes, regime, universe
├── Dockerfile
├── railway.json
├── render.yaml
├── DEPLOYMENT.md                      # how to actually get this running live
├── .env.example                    # secrets go here (API keys), never in settings.yaml
├── requirements.txt
├── ARCHITECTURE.md                 # full formula spec
├── PROVIDERS.md                    # data source recommendations + 2026 pricing
├── README.md                       # this file
│
├── opportunity_scanner/
│   ├── settings.py                 # YAML + env config loader (pydantic-settings)
│   ├── config.py                   # dataclass config every module actually uses
│   ├── models.py                   # MarketSnapshot / FactorResult / ScanResult
│   ├── provider_models.py          # pydantic contracts for the data provider layer
│   │
│   ├── filters.py                  # hard quality filters (run before scoring)
│   ├── risk.py                     # risk tier classification (separate from score)
│   ├── regime.py                   # BTC regime awareness / bullish-score dampener
│   ├── scoring.py                  # composite blend + confidence (async pipeline)
│   ├── scoring_engine.py           # decoupled metrics-in scoring engine (sync, pydantic)
│   ├── scanner.py                  # orchestrator: data -> filters -> factors -> regime -> score
│   ├── main.py                     # CLI entry point — ranked table + explanations
│   ├── dashboard.py                # Streamlit dashboard — matches AutoBot's design system
│   ├── alerts.py                   # Telegram/Discord alert dispatch, cooldown, message formatting
│   ├── logging_config.py           # text/JSON structured logging, driven by settings.yaml
│   ├── api.py                      # FastAPI service
│   ├── scheduler.py                # background polling + persistence
│   ├── storage.py                  # SQLite scan history + backtesting
│   ├── cache.py                    # TTL cache / Redis cache / retry decorator
│   ├── circuit_breaker.py          # per-provider circuit breaker
│   ├── degen_models.py             # on-chain/DEX pair pydantic models (separate from main pillars)
│   ├── degen_radar.py              # risk-flagging for memecoin/on-chain tokens (not scored 0-100)
│   ├── meme_scoring_engine.py      # safety-gated, hype-weighted meme coin scoring (MEME_ARCHITECTURE.md)
│   ├── meme_aggregator.py          # DexScreener + RugCheck + GoPlus -> MemeCoinMetrics
│   ├── meme_main.py                # meme scanner CLI — discovery, scoring, high-quality filter
│   │
│   ├── factors/                    # the four pillars, independently testable
│   │   ├── strength.py
│   │   ├── oi_dynamics.py
│   │   ├── momentum.py
│   │   └── social.py
│   │
│   └── data_sources/                # external API integrations
│       ├── exchange.py              # Bybit (ccxt + raw v5) — OHLCV, ticker, OI, funding, long/short
│       ├── multi_exchange_oi.py     # CoinGlass (paid, multi-exchange) + Bybit/Hyperliquid fallback
│       ├── social.py                # LunarCrush
│       ├── whale.py                 # Whale Alert
│       ├── dexscreener.py           # DexScreener — free, on-chain/memecoin pair data
│       ├── rugcheck.py              # RugCheck.xyz — free, Solana mint/freeze authority, LP lock, holders
│       └── goplus.py                # GoPlus Security — free tier, honeypot/tax detection, chain-aware
│
└── tests/
    ├── test_scoring_demo.py         # full pipeline, synthetic data
    ├── test_scoring_engine.py       # decoupled scoring engine
    ├── test_regime.py               # BTC regime dampening
    ├── test_circuit_breaker.py      # resilience layer
    └── test_cli_rendering.py        # CLI table output
```

## Running the CLI

```bash
python -m opportunity_scanner.main
python -m opportunity_scanner.main --symbols BTC,ETH,SOL --min-score 60
```

Prints a ranked, color-coded table (score, signal, confidence, risk tier,
per-pillar breakdown) followed by a plain-language explanation for every
coin — including regime dampening notes and why any filtered-out coin
was excluded.

## Configuration: YAML + environment variables

`settings.yaml` holds every tunable (weights, filters, timeframes, regime
thresholds, alerts, provider resilience, universe, scheduler interval) and
is meant to be committed to version control. Environment variables
override individual fields using double-underscore nesting:

```bash
# override weights.strength from settings.yaml
export WEIGHTS__STRENGTH=0.30
export WEIGHTS__OI_DYNAMICS=0.20
# (weights must still sum to 1.0, validated on load — fails loudly, not silently)

# tune provider resilience without touching code
export RESILIENCE__CACHE_TTL_SECONDS__TICKER=30
export RESILIENCE__CIRCUIT_BREAKERS__COINGLASS__FAILURE_THRESHOLD=6

# point at Redis-shared caching
export CACHE__REDIS_URL=redis://localhost:6379

# API keys and alert secrets — always env vars, never settings.yaml
export LUNARCRUSH_API_KEY=...
export COINGLASS_API_KEY=...
export WHALE_ALERT_API_KEY=...
export TELEGRAM_BOT_TOKEN=...       # auto-backfills into alerts.telegram.bot_token
export TELEGRAM_CHAT_ID=...
export DISCORD_WEBHOOK_URL=...
```

Precedence: env vars > `.env` file > `settings.yaml` > field defaults. See
`settings.py` for the loader; `Settings.to_scanner_config()` converts to
the dataclass-based `ScannerConfig` every other module already uses, and
`to_cache_ttls()` / `to_breaker_config()` feed the resilience layer — so
nothing else in the codebase needed to change, but cache freshness and
circuit breaker thresholds are now genuinely config-driven, not hardcoded.

**Alerts** (`settings.yaml`'s `alerts:` section) define the trigger rules
— which signal transitions notify, minimum confidence, cooldown — and
where notifications go (Telegram/Discord/email). The schema and config
loading is built and tested now; actually sending notifications is Phase
5 product work, not yet implemented — this exists so that work has a
settings surface to build against instead of hardcoded thresholds.

## Degen Radar — on-chain/memecoin tokens (separate from the main scanner)

`degen_models.py` + `degen_radar.py` + `data_sources/dexscreener.py`
cover the on-chain/memecoin world — pump.fun-origin tokens once they
migrate to a DEX (Raydium, PumpSwap), and anything else DexScreener
indexes (300+ DEXs, 80+ chains, free, no key).

This is deliberately **not** part of the four-pillar Opportunity Score.
Two design reasons:

1. **Different risk category, not just smaller.** Thin liquidity, easy
   manipulation, and pair ages measured in hours rather than years mean
   the same analytical rigor that works for BTC/ETH doesn't transfer
   cleanly to a token that launched 20 minutes ago.
2. **Output shape is deliberately different.** `DegenSnapshot` has no
   `score` field at all — it's a list of `DegenFlag`s (info/warning/
   danger), not a 0-100 number. A "72" on the main Opportunity Score and
   a "72" here would imply the same confidence level, which would be
   false. Flags force a human to actually read the specific risk rather
   than pattern-matching on a number.

Off by default (`degen_radar.enabled: false` in `settings.yaml`). See
`PROVIDERS.md` for why DexScreener specifically, and why FOMO/pump.fun's
own in-app leaderboards remain out of scope (no public API, would
violate their terms).

## Dashboard (Streamlit)

```bash
streamlit run opportunity_scanner/dashboard.py
```

**Rate limited against brute-forcing.** 3 wrong password attempts trigger
a 30-second lockout — no form shown at all during the lockout window,
even the correct password won't work until it expires. Counter resets
on a successful login.

**Doesn't start empty after a restart.** On first render each session,
if there's no scan in memory yet, the dashboard automatically loads the
most recent scan per symbol from storage — the main table is immediately
filled from whatever the scheduler last saved, not blank until you click
Scan Now. Reconstruction is necessarily partial: composite score,
confidence, signal, weights, and the thesis text all come through
correctly (they're stored), but per-factor divergence/crowding flags and
the regime label aren't in the database schema, so hydrated rows show
"no flags detected" and regime "Unknown" until a fresh Scan Now
repopulates the full in-memory detail. If no scan has ever run, shows
"No scans yet — click Scan Now to run your first scan" rather than a
blank table with no explanation.

**Fails cleanly, not with a traceback.** If `settings.yaml` is missing,
malformed, or the storage database can't be opened (bad path, no write
permission), the dashboard shows a clean, dark-mode-consistent error page
— what went wrong in plain language, a hint on how to fix it, and a
"Technical details" expander with the actual exception for debugging.
Nothing partially renders past the failure point. `load_settings()` in
`settings.py` explicitly checks the file exists before loading (a
missing file would otherwise silently fall back to field defaults rather
than raising — the wrong behavior for something meant to give a clear
signal something's wrong).

**Login required by default.** Set `DASHBOARD_PASSWORD` as an environment
variable — never put a real password in `settings.yaml` (that file is
meant to be committed to version control; the `auth.password` field there
stays `null` and only exists so the section is visible/documented, same
pattern as the alert channel secrets). Set `auth.enabled: false` in
`settings.yaml` to skip the login gate entirely for local development —
not recommended once deployed anywhere reachable, since "Scan Now"
triggers real, sometimes paid, API calls.

Matches AutoBot's design system (off-black backgrounds, Playfair Display
headline numbers, DM Mono labels, gold hairlines) via injected CSS —
Streamlit's defaults are overridden, not merely tinted.

- **Top bar**: last scan time with a live/stale indicator, Scan Now
  button, Scalp/Swing mode selector (changes which timeframes the
  Momentum pillar weights most heavily — Scalp leans 15m/1h, Swing leans
  4h/1d), editable universe.
- **Left (70%)**: ranked opportunities table, click a row to open a detail
  modal (`st.dialog`) with the big score, pillar breakdown bars, an
  explainability thesis built from `reasons_summary`, and flags derived
  from the same divergence/funding/crowding sub-scores each factor
  already computes (not a separate flag system).
- **Right (30%)**: regime status, live pillar-weight sliders (re-scores
  already-fetched results instantly — no re-fetch, just re-runs
  `combine_factors()` on the stored `FactorResult`s), score/risk filters,
  signal counts.
- **Bottom**: recent alerts feed (signal changes + score jumps from
  storage) and a 30-day Strong Buy backtest summary.

**Architecture note**: `OpportunityScanner` is async, Streamlit is
synchronous. Only the actual network-bound scan uses `asyncio.run()` —
everything else (storage reads for alerts/backtest, saving results) goes
through the sync convenience methods on `ScanStorage`
(`get_signal_changes_sync`, `backtest_signal_sync`, etc.), added
specifically so a sync caller like Streamlit never needs to wrap simple
SQLite reads in an event loop. An earlier draft used `asyncio.run()`
everywhere and hung under Streamlit's `AppTest` testing framework — fixed
by removing the unnecessary async surface rather than working around the
symptom.

## Meme Scanner Configuration

All meme-scanner-specific tuning lives in `settings.yaml`'s
`meme_scanner:` section — nothing about thresholds, weights, or the
internal hype formula is hardcoded anymore. This is the same pattern as
the main scanner's `weights:`/`filters:` sections, applied to a
genuinely different scoring model:

```bash
# tighten Sniper mode's liquidity floor
export MEME_SCANNER__THRESHOLDS__SNIPER__MIN_LIQUIDITY_USD=25000

# rebalance Early Momentum's pillar weights (still needs to sum to ~1.0 —
# ScoringEngine normalizes automatically, but keep it deliberate)
export MEME_SCANNER__WEIGHTS__EARLY_MOMENTUM__HYPE=0.35
export MEME_SCANNER__WEIGHTS__EARLY_MOMENTUM__ONCHAIN=0.37
export MEME_SCANNER__WEIGHTS__EARLY_MOMENTUM__MOMENTUM=0.28

# make the Hype pillar's KOL boost more influential
export MEME_SCANNER__HYPE_FORMULA__KOL_BOOST_CAP=25
```

`Settings.to_meme_engine_config()` converts the YAML/env config into the
`MemeEngineConfig` object `ScoringEngine` actually consumes — verified
end to end (not just that it parses): overriding a threshold really
changes whether a coin passes the Safety gate, overriding weights really
changes the composite score, and a missing mode in `thresholds`/`weights`
raises a clear error rather than silently using the wrong numbers. See
`tests/test_meme_settings_adjustability.py`.

## Meme Scanner — Product Layer (Phase 5)

- **Deployer blacklist** (`meme_storage.py`) — checked first in the Safety
  gate, before any other logic. Grows automatically: a Fail caused by an
  insider/bundle/honeypot finding blacklists that deployer for every
  future token, not just the one that triggered it.
- **Hype event detection** (`meme_hype_events.py`) — reads a token's
  *previous* scan and flags genuine changes (velocity jumps, newly
  boosted, KOL activity, hype score jumps), not just current-state
  thresholds. A token sitting at high-but-stable hype for hours isn't an
  "event"; one that just crossed from quiet to loud is.
- **Alerts** (`meme_alerts.py`) — fires only when safety passed AND the
  score clears a (stricter-than-display) threshold AND an actual hype
  event was detected. All three required independently — a high score
  alone never alerts.
- **Performance tracking** (`meme_storage.py::backtest_high_conviction`)
  — rough forward-return sanity check on past high-conviction calls, same
  honest no-fees/no-slippage limitations as the main scanner's backtest.

Run `python -m opportunity_scanner.meme_main` with `meme_scanner.alerts.enabled: true`
in `settings.yaml` to turn alerting on.

## Meme Scanner Dashboard

```bash
streamlit run opportunity_scanner/meme_dashboard.py
```

Reuses `run_scan()` from `meme_main.py` directly for the Scan Now button
— the CLI and dashboard are two views onto the exact same pipeline, not
two implementations that could drift apart. Same auth/error-handling/
hydration patterns as the main dashboard (`dashboard.py`), applied fresh
here since Streamlit scripts can't import each other's module-level code.

- **Top**: mode selector (Sniper/Early/Runner), live/stale status, quick
  safety overview (Pass/Caution counts — Fail-grade tokens are never
  shown, even historical ones from storage).
- **Table**: only Safety-passed candidates, sortable by Opportunity Score
  or Hype Level, columns include liquidity, holders, top-10%, volume,
  and mention velocity.
- **Detail modal**: big Safety + Opportunity scores, pillar breakdown
  bars, hype events, full risk flags with severity coloring, thesis, and
  direct DexScreener + Solscan links.

## Real User Accounts (Stage 2)

Both dashboards now use real per-user login/registration (`auth_ui.py`,
`app_storage.py`) instead of the Stage 1 shared `DASHBOARD_PASSWORD`.

```bash
streamlit run opportunity_scanner/dashboard.py
streamlit run opportunity_scanner/meme_dashboard.py
```

Either one shows a Sign In / Create Account screen. Registering an
account auto-logs in — passwords are hashed with PBKDF2-HMAC-SHA256
(`auth_utils.py`, stdlib only), never stored in plain text, and login
failures don't reveal whether the email exists or the password was
wrong. New accounts default to the Free plan, with real limits enforced
— see Stage 4 below.

Users live in `app_users.db` (`app_db_path` in `settings.yaml`),
separate from `opportunity_scanner.db`/`meme_scanner.db` — accounts are
per-user, scan data is shared/global.

## Plan-Based Access Control (Stage 4)

`plans.py` defines per-scanner limits — not one global number, since
"Pro gets unlimited Opportunity Scanner but a capped Meme Scanner"
couldn't be expressed with a single `max_scans_per_day`:

| Plan | Opportunity Scanner | Meme Scanner |
|---|---|---|
| Free | 5 scans/day, results capped to top 5 | Blocked entirely |
| Pro | Unlimited, full results | 10 scans/day |
| Elite | Unlimited, full results | Unlimited |

`access_control.py` is the single place both dashboards call —
`check_scanner_access()` returns an allowed/denied decision with a
ready-to-show reason, `record_scan()` logs usage only after a scan
actually completes (so a network failure doesn't burn a Free user's
daily allowance on nothing). Enforcement happens twice, deliberately:
once to disable the button for a clean UX, and again — the real
check — right when the button is clicked, so the disabled attribute is
a nicety, not the actual security boundary. A Free user hitting the
Meme Scanner doesn't see a grayed-out button; the entire scanning UI is
replaced with an upgrade page.

`require_auth()` re-fetches the user record from storage on every
Streamlit rerun rather than trusting a cached session object — a
Stripe/crypto webhook is handled by the separate API process, so
without this, a plan upgrade wouldn't take effect until the person
logged out and back in.

**Known limitation, stated plainly**: `st.session_state` doesn't survive
a hard page refresh — Streamlit has no built-in persistent session
cookie. A refresh logs you out. This matches the Stage 1 shared-password
login's exact same limitation (not a regression), and is a deliberate
scope line: real "stay logged in across a refresh" needs a proper
session-token + cookie mechanism, which is future work, not built here.

## Billing — Stripe + Crypto (Stage 3)

Both dashboards have an "Account & Billing" section (collapsed by
default, click to expand) showing the current plan and upgrade options.
Off by default — set `stripe.enabled: true` and/or
`crypto_payments.enabled: true` in `settings.yaml`, plus the real
secrets as environment variables (never in `settings.yaml`):

```bash
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...     # from the Stripe Dashboard's webhook endpoint settings
NOWPAYMENTS_API_KEY=...
NOWPAYMENTS_IPN_SECRET=...
```

You'll also need `stripe.price_id_pro` / `stripe.price_id_elite` in
`settings.yaml` (created in the Stripe Dashboard — Products → Prices;
these IDs aren't secrets, safe to commit).

**Webhook endpoints** (`/webhooks/stripe`, `/webhooks/nowpayments` on the
API service) are what actually activate a subscription after payment —
register `https://your-api-url/webhooks/stripe` in the Stripe
Dashboard's webhook settings, and the equivalent NowPayments IPN
callback URL in your NowPayments account settings.

**What's tested vs. not**, stated plainly: signature verification and
event handling (the security-critical logic — this is what stands
between a real payment and someone forging a free upgrade) are fully
tested with real, locally-signed payloads, no live network needed.
Checkout session / invoice *creation* — the actual redirect-to-payment
step — makes a real network call to Stripe/NowPayments and is untested
against a live account in this environment; verify it end-to-end with a
real test-mode key before relying on it in production.

## Setup

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# optionally add LUNARCRUSH_API_KEY to .env — the scanner runs without it,
# just without the social pillar
```

## Run the sanity check (no network required)

```bash
python -m tests.test_scoring_demo
```

This builds synthetic-but-consistent data for a "strong" and a "weak" coin,
runs the full pipeline, and asserts the strong one scores higher and grades
correctly. Run this first, before wiring up real keys, to confirm the
scoring logic itself behaves as expected on your machine.

## Run a real scan

```python
import asyncio
from opportunity_scanner import OpportunityScanner, ScannerConfig

async def main():
    scanner = OpportunityScanner(ScannerConfig(lunarcrush_api_key="..."))
    results = await scanner.scan_many(["BTC", "ETH", "SOL", "AVAX", "LINK"])
    for r in results:
        print(r.symbol, r.composite_score, r.signal, r.risk_tier)
    await scanner.close()

asyncio.run(main())
```

## Run the API

```bash
uvicorn opportunity_scanner.api:app --reload --port 8000
```

- `GET /scan?symbols=BTC,ETH,SOL` — scan specific coins
- `GET /scan` — scan the default starter universe
- `GET /scan/BTC` — scan one coin, full breakdown
- `GET /config` — see current weights/filters/timeframes
- `POST /config/weights` — override pillar weights at runtime (must sum to 1.0)

## Customizing weights

```python
from opportunity_scanner import ScannerConfig, Weights

config = ScannerConfig(
    weights=Weights(strength=0.25, oi_dynamics=0.25, momentum=0.30, social=0.20)
)
```

Weights are validated to sum to 1.0 on construction — this fails loudly
rather than silently normalizing, so a typo doesn't quietly change your
scoring without you noticing.

## Data sources & what needs a paid key

- **OHLCV, ticker, open interest, funding, long/short ratio** — Bybit
  public API, free, no key needed. `data_sources/exchange.py` uses ccxt for
  the unified parts (OHLCV/ticker) and raw Bybit v5 REST calls for OI/
  funding/long-short ratio, since those aren't consistently unified across
  ccxt exchanges. Swap `config.primary_exchange` to point at a different
  venue — you'll need to adapt the three raw-call methods if you do, since
  they're Bybit-specific by design (isolated in one file for exactly this
  reason).
- **Social (mention velocity, sentiment, mindshare)** — LunarCrush, paid
  API. Without a key, the social pillar returns `available=False` and its
  20% weight redistributes across the other three pillars automatically.

## Extending with new data sources

Each factor function takes a `MarketSnapshot` (or a couple of primitives)
and returns a `FactorResult(score, reasons, raw, available)`. To add a
fifth pillar (say, on-chain whale flow):

1. Add the relevant fields to `MarketSnapshot` in `models.py`
2. Populate them in a new `data_sources/onchain.py`
3. Write `factors/onchain.py::compute_onchain(snap) -> FactorResult`
4. Add the weight to `config.py::Weights` and wire it into
   `scanner.py::scan_symbol` and `scoring.py`

## Known limitations / next steps

- Long/short ratio and OI history are Bybit-specific (raw REST calls) —
  porting to another primary exchange means rewriting those two methods.
- Market structure detection in `factors/strength.py` is a coarse
  higher-highs/higher-lows split-window read, not full swing-point/
  fractal detection — deliberately simple and explainable over clever.
  Swap in `scipy.signal.argrelextrema` or similar if you want finer
  resolution.
- No persistent storage/history layer yet — every scan is a fresh pull.
  For true historical backtesting of the scoring model itself, you'd want
  to snapshot `ScanResult`s to a database (Postgres/SQLite) over time.
- No built-in alerting (Telegram/Discord/webhook on Strong Buy signals) —
  straightforward to add as a thin layer on top of `scan_many()`.
