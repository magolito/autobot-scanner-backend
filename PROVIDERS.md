# Data Provider Recommendations (2026)

Practical, current recommendations for each data need — what to use free,
what to upgrade to, and why. Pricing/specifics verified via web search as
of this writing; providers change pricing without notice, so treat exact
numbers as directional.

## Price / OHLCV / Volume, Open Interest, Funding Rates (rebalanced)

**Strict priority chain, not averaging: Hyperliquid → CoinGecko
Derivatives → Coinbase/Kraken (spot confirmation) → Bybit (last,
optional).** This replaced an earlier Bybit-primary design after a real
live deployment (Railway) confirmed Bybit actively CloudFront-blocks
US-hosted server traffic — not theoretical, an actual production
failure with every price/OI/funding/OHLCV call returning
`"The Amazon CloudFront distribution is configured to block access from
your country"`. Given AutoBot's real trading business is US-based and
already runs on Hyperliquid, rebalancing toward the source with no such
restriction was the correct fix — not a proxy/geo-bypass around Bybit's
deliberate compliance control, which would have been circumventing it
rather than fixing the actual mismatch.

- **Hyperliquid** (primary) — no key, no US restriction, matches
  AutoBot's own trading venue. Current-snapshot OI/funding (no history
  endpoint at the free tier), real OHLCV candles via ccxt.
- **CoinGecko Derivatives** (secondary) — free, no key, one API call
  covers every tracked base symbol across every exchange CoinGecko
  aggregates (`GET /derivatives`, filtered client-side). Used when
  Hyperliquid doesn't have a contract or is down. Picks the highest-open-
  interest *perpetual* contract per symbol as the representative price —
  the most liquid venue is the most representative of the real market.
  No OHLCV history from this source, ticker/OI/funding only.
- **Coinbase** (spot confirmation) — free, no key, explicitly serves US
  customers. Ticker + spot price only, no derivatives data.
- **Kraken** (spot confirmation, narrower coverage) — free, no key, also
  explicitly US-facing. Pair-code naming is inconsistent across coins
  (e.g. "XBT" not "BTC"), so only major coins are mapped — an unmapped
  symbol falls through cleanly rather than guessing a pair code.
- **Bybit** (last, optional) — richest OI *history* when reachable (the
  only source with real historical points, useful for trend calc), but
  never depended on: every fetch degrades gracefully to the next source
  in priority, and the scan never fails outright just because Bybit is
  blocked.

Every fetch logs which source actually answered
(`MarketSnapshot.data_sources`, e.g. `{"price": "hyperliquid",
"open_interest": "coingecko", ...}`), so accuracy is auditable per data
point, not just assumed. The priority order itself is configurable via
`settings.yaml`'s `exchange.market_data_priority` — no code changes
needed to add, remove, or reorder a source.

**Paid upgrade, if you want top-trader ratio:** CoinGlass aggregates 6
exchanges including top-trader position ratio, which none of the free
sources above expose. 2026 pricing: Hobbyist $29/mo (personal use, 30
req/min), Startup $79/mo, Standard $299/mo (**required for
commercial/product use**), Professional $699/mo. This is a genuinely
separate, still-unwired-into-the-live-path integration
(`MultiExchangeOIProvider`/`CoinGlassProvider` in
`data_sources/multi_exchange_oi.py`) — built and tested in an earlier
phase, but discovered during this rebalancing work to have never
actually been called by the real scan pipeline (`scanner.py` only ever
used `ExchangeDataSource.build_snapshot()` directly). Worth wiring in
properly if/when a CoinGlass subscription is added, rather than left as
dead code indefinitely.

## Social Virality

**Primary: LunarCrush** (already built) — Galaxy Score, AltRank, social
volume/sentiment across X/Reddit/YouTube/TikTok, now with MCP support.
Paid, usage-based.

**Fallback if LunarCrush is down or unset:** no free equivalent exists
with comparable depth. Santiment is a paid alternative with overlapping
but not identical metrics (different scoring model — don't treat their
numbers as interchangeable with Galaxy Score if you ever dual-source).
Practical fallback in code: social pillar returns `available=False` and
its weight redistributes — already built, still the right behavior when
no social source is reachable at all.

## Market Data (market cap, listings, general metadata)

**CoinGecko** — free tier is genuinely sufficient for market cap, rank,
listing counts. Paid "Analyst" tier ($129/mo, $103/mo annual) only
needed if you exceed free rate limits at scale.

## On-chain / memecoin discovery (DexScreener) — the real answer to "FOMO or pump.fun data"

Earlier guidance held that FOMO and pump.fun don't expose public developer
APIs for their leaderboards, and that's still true — nothing changed
there. But **DexScreener does have a real, free, documented public API**,
and it directly indexes the pools pump.fun tokens land on once they
migrate off the bonding curve (Raydium, PumpSwap), plus Orca/Meteora and
300+ other DEXs across 80+ chains. That's legitimate, real coverage of
the on-chain memecoin world — just not literally FOMO/pump.fun's own
in-app leaderboard data.

- **No API key needed.** Rate limits: 60 req/min on token-profile/boost
  endpoints, 300 req/min on pair/token endpoints — generous enough for
  this use case.
- **What it gives you:** live price, liquidity, volume, buy/sell
  transaction counts, pair age, and price change, per on-chain pool.
- **What it doesn't give you:** wallet-level "who's the best trader"
  data — that's a different problem (see the Birdeye/Nansen Smart Money
  note below, still the right tool for that specific question).
- **Built as `data_sources/dexscreener.py` + `degen_radar.py`**,
  deliberately kept separate from the four-pillar Opportunity Score.
  Output is risk-flags, not a 0-100 score — blending a thin-liquidity
  memecoin into the same numeric scale as BTC would imply a false
  equivalence of confidence.

**Still true from before:** if you want verified on-chain wallet
performance (not just pair-level data), Birdeye or Nansen Smart Money
remain the right paid tools for that specific job — DexScreener doesn't
do wallet tracking.

## Whale Movement

**Whale Alert** (already built) — paid, no strong free alternative at
comparable coverage.

---

## Summary: what to actually pay for, in priority order

1. **LunarCrush** — social pillar is non-functional without it (25% of the score)
2. **CoinGlass Standard ($299/mo)** — closes the top-trader-ratio gap, only once you're ready for commercial use
3. **Whale Alert** — supplementary, lowest priority of the three paid sources
4. CoinGecko/CoinAPI upgrades — not needed yet, revisit if rate limits become a real constraint
