# Deployment Guide

## Prerequisites

- `settings.yaml` reviewed — weights, filters, universe, alerts triggers,
  meme scanner thresholds
- Scanner data API keys ready (as many or as few as you want live):
  `LUNARCRUSH_API_KEY`, `WHALE_ALERT_API_KEY`, `COINGLASS_API_KEY`. Bybit,
  Hyperliquid, DexScreener, RugCheck, and GoPlus need no key.
- If using alerts: `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`, and/or
  `DISCORD_WEBHOOK_URL`. Also flip `alerts.enabled: true` and
  `alerts.telegram.enabled` / `alerts.discord.enabled` in `settings.yaml`.
- If using billing (paid plans — see `plans.py`): `STRIPE_SECRET_KEY` +
  `STRIPE_WEBHOOK_SECRET` and/or `NOWPAYMENTS_API_KEY` +
  `NOWPAYMENTS_IPN_SECRET`, plus `stripe.price_id_pro`/`price_id_elite`
  in `settings.yaml` (from the Stripe Dashboard — not secrets, safe to
  commit). Flip `stripe.enabled: true` / `crypto_payments.enabled: true`.
  Without these, both dashboards' Account & Billing sections just show
  "billing isn't configured" and every account defaults to Free with its
  documented limits — the product still fully works, just with no way to
  upgrade.
- `APP_BASE_URL` — set this to whatever public URL you'll actually use
  once you have it (Stripe/crypto checkout redirects back here after
  payment). Fine to leave as the `localhost` default until you know the
  real URL, then update it.

## Option A: Railway (recommended — see the topology note below for why)

1. Push this project to a GitHub repo (or `railway up` directly from this folder)
2. `railway.json` is already set up — Railway will build from the Dockerfile
3. In the Railway dashboard, set environment variables from the Prerequisites
   above, plus `RUN_SCHEDULER_IN_PROCESS=true` (see note below on why this matters)
4. Railway gives you one public URL by default (routes to port 8000, the
   API). Under the service's **Settings → Networking**, add two more
   public domains, pointed at ports **8501** (Opportunity Scanner
   dashboard) and **8502** (Meme Scanner dashboard) — this is what makes
   both dashboards actually reachable as their own product surfaces, not
   just the API.
5. Confirm each with `curl https://your-api-domain.up.railway.app/health`
   and by opening the two dashboard URLs in a browser — you should land
   on a Sign In / Create Account screen for each.

## Option B: Render

1. `render.yaml` is already set up as a single web service with a
   persistent disk for all three SQLite databases, and
   `RUN_SCHEDULER_IN_PROCESS=true` baked in
2. Push to GitHub, connect the repo in Render's dashboard, it reads `render.yaml` automatically
3. Set the `sync: false` env vars in Render's dashboard (API keys, alert secrets, billing secrets)
4. Confirm with `curl https://your-app.onrender.com/health`
5. **Read the topology note below before assuming both dashboards are
   reachable** — Render's standard web service only routes external
   traffic to one port, so only the API gets a public URL through this
   config as-is. Given both dashboards are now real paid-product
   surfaces (not internal tools), Railway is the better fit for this
   project unless you're prepared to add the extra complexity noted below.

## Why `RUN_SCHEDULER_IN_PROCESS` matters — read this before deploying

The scheduler (which runs scans on an interval and dispatches alerts) can run:

- **In-process** (`RUN_SCHEDULER_IN_PROCESS=true`, the default in both
  configs here) — the API server itself launches the scheduler as a
  background task on startup. Simple, and **correct as long as storage
  is SQLite**, since everything reads/writes the same file.
- **As a separate worker process** (`python -m opportunity_scanner.scheduler`
  run as its own service) — architecturally cleaner (a slow scan cycle
  can never block an API request), but **only correct once you've moved
  off SQLite to Postgres**. An earlier draft of `render.yaml` set this up
  as two separate services sharing one SQLite file — that's actually
  broken on Render, since separate services don't share disks by default,
  so each would silently get its own disconnected database. Fixed before
  shipping this guide; flagging it here so the reasoning is visible, not
  just the fix.

Stick with in-process (the default) until you have a concrete reason to
split it out, at which point also migrate to Postgres — don't do the
split without the migration.

## Deployment topology: where do the dashboards fit?

Three options exist for how the Streamlit dashboards relate to the API +
scheduler. This isn't abstract — given this project's current storage
(SQLite, a single file on disk) and the platforms this guide targets
(Railway, Render — both single-port-per-service, no disk sharing between
separate services by default, as discovered and documented above), only
one of the three is actually correct today without further work.

| Option | Works with SQLite today? | Notes |
|---|---|---|
| **Dashboards only** (no scheduler running anywhere) | Yes, trivially | Simplest, but "last scan" only updates when someone manually clicks Scan Now — no automatic refresh, no alerts firing in the background. Fine for occasional manual use, not for anything you want to trust to stay current. |
| **Dashboards + scheduler as separate services** | **No** — silently broken | Same disk-sharing problem already documented for the API/scheduler split above: separate services on Render/Railway don't share a filesystem by default, so a dashboard would read from a different (stale, or empty) SQLite file than the one the scheduler writes to. This would *look* like it's deployed correctly and just... never update. Don't do this without first migrating to Postgres. |
| **API + both dashboards + scheduler in one container, one process each, same filesystem** | **Yes — recommended for now** | Same pattern as `RUN_SCHEDULER_IN_PROCESS` already used for the API: everything that touches the SQLite files lives in the same container, so it's trivially the same disk. |

### Recommended: single container, three processes

`start.sh` runs the API (with `RUN_SCHEDULER_IN_PROCESS=true`) and both
Streamlit dashboards as three processes, all reading/writing the same
local SQLite files:

```bash
#!/bin/bash
# start.sh
set -e
uvicorn opportunity_scanner.api:app --host 0.0.0.0 --port "${API_PORT:-8000}" &
API_PID=$!
streamlit run opportunity_scanner/dashboard.py --server.port "${DASHBOARD_PORT:-8501}" --server.address 0.0.0.0 --server.headless true &
DASHBOARD_PID=$!
streamlit run opportunity_scanner/meme_dashboard.py --server.port "${MEME_DASHBOARD_PORT:-8502}" --server.address 0.0.0.0 --server.headless true &
MEME_DASHBOARD_PID=$!
wait -n "$API_PID" "$DASHBOARD_PID" "$MEME_DASHBOARD_PID"
EXIT_CODE=$?
kill "$API_PID" "$DASHBOARD_PID" "$MEME_DASHBOARD_PID" 2>/dev/null || true
exit "$EXIT_CODE"
```

Already wired up: the Dockerfile's `CMD` is `["./start.sh"]`, and all
three ports (`8000` API, `8501` Opportunity Scanner, `8502` Meme
Scanner) are `EXPOSE`d.

**Since Stage 2/4, both dashboards are real product surfaces with paid
accounts behind them, not internal-only tools** — this changes the
calculus on the port-exposure limitation below. You genuinely want both
publicly reachable, not just the API.

**Platform-specific note on exposing three ports from one service:**
Railway supports adding multiple public domains/ports to a single
service under its Networking settings — add one for each of the three
ports to get separate public URLs for the API and both dashboards from
one deployed container. This is the straightforward path given the
paid-product requirement above.

Render's standard web service only routes external traffic to **one**
port (`8000`, the API, per `render.yaml`'s config) — the two dashboards
run in the same container but aren't reachable from outside Render's
network this way. Since both dashboards are now meant to be publicly
reachable paid product surfaces, **Railway is the better fit for this
project as it stands today.** If you're committed to Render anyway, the
practical options are: run two additional Render services that reverse-
proxy to the same container's internal ports (real added complexity), or
migrate off SQLite to Postgres and split into three genuinely separate
Render services, each independently publicly reachable.

### The proper long-term fix, if you outgrow this

The real architectural fix — not needed yet — is to stop having the
dashboards touch SQLite directly at all, and instead have them call the
API's own HTTP endpoints (`/scan`, `/history/{symbol}`,
`/backtest/{signal}`) the same way any other client would. That
decouples the two completely: they become independent services talking
over HTTP, deployable anywhere, no shared disk or shared container
required. Worth doing once you're scaling to multiple instances or
moving off SQLite to Postgres anyway — not needed for a single-instance
deployment today, even now that both dashboards are real paid-product
surfaces rather than internal tools.

## Confirming it's actually working

```bash
curl https://your-api-domain/health
# {"ok": true}

curl "https://your-api-domain/scan?symbols=BTC,ETH,SOL"
# real ranked results, or an error you can debug from

curl https://your-api-domain/poller/status
# {"running": true, "last_run_at": "...", "interval_minutes": 15}
```

If `/scan` errors, that's the first real live-data confirmation this
whole project has been missing — every test so far has run against
synthetic data because the build sandbox couldn't reach any of these
exchanges. Whatever the error says is genuinely new information; fix
forward from there rather than assuming the code should already be right.

**Both dashboards**: open each dashboard's public URL in a browser —
you should land on a Sign In / Create Account screen (not the shared
password from the old Stage 1 login). Register a real account, confirm
the Opportunity Scanner dashboard shows "Plan: Free · Scans remaining
today: 5" and the Meme Scanner dashboard is either fully blocked with an
upgrade prompt (Free tier — expected) or shows its own remaining-scans
count if you've manually upgraded that test account's plan.

**If billing is enabled**: register the webhook URLs with each
provider — `https://your-api-domain/webhooks/stripe` in the Stripe
Dashboard's webhook endpoint settings, and the equivalent NowPayments
IPN callback URL in your NowPayments account settings. Then run an
actual test-mode purchase through one of the dashboard's upgrade
buttons — this is the one thing in the whole billing system that's
never touched a live account (signature verification and event handling
are fully tested with synthetic payloads; checkout/invoice *creation* is
the untested live-network half, see `README.md`'s Billing section).

## Connecting the website

Two separate things to update on the marketing site, once the backend
has a real URL:

1. **`scanner.html` and `degen-radar.html`** (if keeping them as free
   teasers) — set `API_BASE` near the top of each file's `<script>`
   block to your deployed API URL, then redeploy to Netlify.
2. **The actual paid product's "Get Access" / pricing CTAs** — point
   these at the deployed dashboard URL(s), not the free teaser pages.
   This is the link between "someone reads about the product on the
   marketing site" and "someone actually signs up and pays for it" —
   without it, the two halves of this project stay disconnected even
   though both are live.
