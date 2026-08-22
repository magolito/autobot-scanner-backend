"""
FastAPI wrapper around OpportunityScanner.

Run: uvicorn opportunity_scanner.api:app --reload --port 8000

Endpoints:
  GET  /health
  GET  /scan?symbols=BTC,ETH,SOL          -> full scan, sorted by score
  GET  /config                             -> current weights/filters/bands
  POST /config/weights                     -> override weights for this process
"""

from __future__ import annotations
import os
from typing import List, Optional
from fastapi import FastAPI, Query, Request, HTTPException
from pydantic import BaseModel

from .config import ScannerConfig, Weights
from .scanner import OpportunityScanner
from .storage import ScanStorage
from .scheduler import ScannerPoller
from .models import ScanResult, FactorResult
from .settings import load_settings
from .data_sources.dexscreener import DexScreenerProvider
from .degen_radar import build_degen_snapshot
from .degen_models import DegenSnapshot
from .logging_config import setup_logging_from_settings
from .app_storage import AppStorage
from . import billing_stripe
from . import billing_crypto

app = FastAPI(title="AutoBot Opportunity Scanner", version="1.2.0")

_settings = load_settings()
setup_logging_from_settings(_settings)
_config = ScannerConfig(lunarcrush_api_key=os.getenv("LUNARCRUSH_API_KEY"))
_scanner = OpportunityScanner(_config, whale_api_key=os.getenv("WHALE_ALERT_API_KEY"))
_storage = ScanStorage(os.getenv("SCANNER_DB_PATH", "opportunity_scanner.db"))
_poller: Optional[ScannerPoller] = None
_degen_provider = DexScreenerProvider(cache_ttl_seconds=_settings.resilience.cache_ttl_seconds.dexscreener)

DEFAULT_UNIVERSE = [
    "BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "AVAX", "LINK", "DOT",
    "MATIC", "TON", "SUI", "NEAR", "APT", "ARB", "OP", "INJ", "TIA", "SEI",
]


def _factor_to_dict(f: FactorResult) -> dict:
    return {
        "score": f.score,
        "available": f.available,
        "reasons": f.reasons,
    }


def _result_to_dict(r: ScanResult) -> dict:
    return {
        "symbol": r.symbol,
        "base": r.base,
        "price": r.price,
        "composite_score": r.composite_score,
        "confidence": r.confidence,
        "confidence_label": r.confidence_label,
        "signal": r.signal,
        "risk_tier": r.risk_tier,
        "passed_filters": r.passed_filters,
        "filter_notes": r.filter_notes,
        "regime": {
            "label": r.regime_label,
            "score": r.regime_score,
            "adjustment_note": r.regime_adjustment_note,
            "score_before_adjustment": r.score_before_regime_adjustment,
        },
        "weights_used": r.weights_used,
        "reasons_summary": r.reasons_summary,
        "factors": {name: _factor_to_dict(f) for name, f in r.factors.items()},
    }


@app.on_event("startup")
async def startup():
    """
    If RUN_SCHEDULER_IN_PROCESS=true, launches the scheduler inside this
    same process rather than requiring a separate worker service. This is
    the practical default for a single-service deployment (see
    render.yaml) — correct as long as storage is SQLite, since a separate
    process would need its own disk and silently diverge. Off by default;
    explicit opt-in via env var.
    """
    global _poller
    if os.getenv("RUN_SCHEDULER_IN_PROCESS", "").lower() == "true" and _poller is None:
        _poller = ScannerPoller(
            config=_config,
            universe=_settings.universe.default,
            db_path=_storage.db_path,
            interval_minutes=_settings.scheduler.interval_minutes,
            alerts_settings=_settings.alerts if _settings.alerts.enabled else None,
            blacklist=_settings.universe.blacklist,
            whitelist=_settings.universe.whitelist,
        )
        _poller.start()


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    """
    Verifies the signature BEFORE doing anything else — an unverified
    webhook endpoint is an open door for anyone to POST a fake "payment
    succeeded" event and grant themselves a paid plan for free. Returns
    400 (not 200) on a bad signature so Stripe's retry/monitoring
    correctly treats it as a failed delivery rather than success.
    """
    settings = load_settings()
    if not settings.stripe.enabled or not settings.stripe.webhook_secret:
        raise HTTPException(status_code=503, detail="Stripe billing is not configured")

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    try:
        event = billing_stripe.verify_and_parse_webhook(payload, sig_header, settings.stripe.webhook_secret)
    except Exception as e:  # noqa: BLE001 — stripe.SignatureVerificationError and malformed-payload errors both mean "reject"
        raise HTTPException(status_code=400, detail=f"Webhook verification failed: {e}")

    app_storage = AppStorage(settings.app_db_path)
    result = billing_stripe.handle_stripe_event(event, app_storage, settings.stripe.price_id_pro, settings.stripe.price_id_elite)
    return {"received": True, "handled": result is not None, "detail": result}


@app.post("/webhooks/nowpayments")
async def nowpayments_webhook(request: Request):
    """Same signature-first-then-process principle as the Stripe handler."""
    settings = load_settings()
    if not settings.crypto_payments.enabled or not settings.crypto_payments.ipn_secret:
        raise HTTPException(status_code=503, detail="Crypto billing is not configured")

    payload = await request.json()
    sig_header = request.headers.get("x-nowpayments-sig", "")
    if not billing_crypto.verify_ipn_signature(payload, settings.crypto_payments.ipn_secret, sig_header):
        raise HTTPException(status_code=400, detail="IPN signature verification failed")

    app_storage = AppStorage(settings.app_db_path)
    result = billing_crypto.handle_crypto_ipn_event(payload, app_storage)
    return {"received": True, "handled": result is not None, "detail": result}


@app.get("/scan")
async def scan(
    symbols: Optional[str] = Query(
        default=None,
        description="Comma-separated base symbols, e.g. BTC,ETH,SOL. Defaults to a starter universe.",
    ),
    min_score: float = Query(default=0.0, description="Only return coins scoring at or above this."),
):
    bases = [s.strip().upper() for s in symbols.split(",")] if symbols else DEFAULT_UNIVERSE
    results = await _scanner.scan_many(bases, blacklist=_settings.universe.blacklist, whitelist=_settings.universe.whitelist)
    results = [r for r in results if r.composite_score >= min_score]
    await _storage.save_scan_results(results)
    return {"count": len(results), "results": [_result_to_dict(r) for r in results]}


@app.get("/scan/{symbol}")
async def scan_one(symbol: str):
    result = await _scanner.scan_symbol(base=symbol.upper())
    await _storage.save_scan_result(result)
    return _result_to_dict(result)


@app.get("/whales/{symbol}")
async def whale_context(symbol: str, lookback_minutes: int = Query(default=60)):
    return await _scanner.get_whale_context(symbol.upper())


@app.get("/history/{symbol}")
async def scan_history(symbol: str, limit: int = Query(default=100, le=1000)):
    rows = await _storage.get_history(symbol.upper(), limit)
    return {"base": symbol.upper(), "count": len(rows), "history": rows}


@app.get("/backtest/{signal}")
async def backtest(signal: str, lookback_days: int = Query(default=30, le=365)):
    """
    Rough sanity-check backtest (see storage.py docstring for exact
    methodology and its limitations — no fees/slippage/position sizing).
    Only meaningful once the poller has been running long enough to have
    accumulated history for `lookback_days`.
    """
    return await _storage.backtest_signal(signal, lookback_days)


@app.post("/poller/start")
async def start_poller(interval_minutes: int = Query(default=15)):
    global _poller
    if _poller is not None:
        return {"status": "already running"}
    _poller = ScannerPoller(config=_config, db_path=_storage.db_path, interval_minutes=interval_minutes)
    _poller.start()
    return {"status": "started", "interval_minutes": interval_minutes}


@app.get("/poller/status")
async def poller_status():
    if _poller is None:
        return {"running": False}
    return {"running": True, "last_run_at": _poller._last_run_at, "interval_minutes": _poller.interval_minutes}


@app.get("/degen/status")
async def degen_status():
    return {"enabled": _settings.degen_radar.enabled, "chain_id": _settings.degen_radar.chain_id}


@app.get("/degen/token/{token_address}")
async def degen_token(token_address: str):
    if not _settings.degen_radar.enabled:
        return {"error": "Degen Radar is disabled in settings.yaml (degen_radar.enabled: false)"}
    pair = await _degen_provider.get_best_pair_for_token(token_address, chain_id=_settings.degen_radar.chain_id)
    if pair is None:
        return {"error": f"No DEX pair found for {token_address} on {_settings.degen_radar.chain_id}"}
    snapshot = build_degen_snapshot(pair)
    return snapshot.model_dump()


@app.get("/degen/search")
async def degen_search(q: str = Query(..., description="Token symbol or name to search")):
    if not _settings.degen_radar.enabled:
        return {"error": "Degen Radar is disabled in settings.yaml (degen_radar.enabled: false)"}
    pairs = await _degen_provider.search_pairs(q)
    min_liq = _settings.degen_radar.min_liquidity_usd_to_show
    filtered = [p for p in pairs if (p.liquidity_usd or 0) >= min_liq]
    snapshots = [build_degen_snapshot(p).model_dump() for p in filtered[:20]]
    return {"query": q, "count": len(snapshots), "results": snapshots}


@app.get("/config")
async def get_config():
    return {
        "weights": _config.weights.as_dict(),
        "filters": _config.filters.__dict__,
        "timeframes": _config.timeframe_config.timeframes,
        "timeframe_weights": _config.timeframe_config.timeframe_weights,
        "signal_bands": _config.signal_bands.__dict__,
    }


class WeightsUpdate(BaseModel):
    strength: float
    oi_dynamics: float
    momentum: float
    social: float


@app.post("/config/weights")
async def update_weights(update: WeightsUpdate):
    global _config
    new_weights = Weights(**update.model_dump())  # raises if they don't sum to 1.0
    _config.weights = new_weights
    return {"weights": _config.weights.as_dict()}


@app.on_event("shutdown")
async def shutdown():
    await _scanner.close()
    await _degen_provider.close()
