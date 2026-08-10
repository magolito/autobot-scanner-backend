"""
RugCheck.xyz data source — free, no API key, Solana-only.

Covers most of the Safety gate in MEME_ARCHITECTURE.md §2: mint/freeze
authority status, LP lock percentage, top-10 holder concentration,
insider/bundle wallet detection, and RugCheck's own normalized risk
score. This is the single highest-leverage integration in the meme
scanner's data layer — one call closes most of the hard-filter checklist.

Endpoint confirmed via public documentation/community integrations:
  GET https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary

Exact response field names below are a best-effort mapping based on
public documentation and third-party integrations, not a verified live
response (this sandbox can't reach RugCheck's API to confirm). Flagged
explicitly rather than silently assumed correct — verify field names
against a real response before relying on this in production, and
adjust `_parse_report` if any field is named differently than assumed.
"""

from __future__ import annotations
from typing import Optional
import httpx
from pydantic import BaseModel

from ..cache import make_cache, with_retry
from ..circuit_breaker import breakers, CircuitOpenError

RUGCHECK_BASE_URL = "https://api.rugcheck.xyz/v1"

rugcheck_cache = make_cache("rugcheck")


class RugCheckReport(BaseModel):
    mint_authority_revoked: Optional[bool] = None
    freeze_authority_revoked: Optional[bool] = None
    lp_locked_pct: Optional[float] = None
    top10_holder_pct: Optional[float] = None
    dev_wallet_pct: Optional[float] = None
    deployer_address: Optional[str] = None       # the wallet that created the token — feeds the deployer blacklist
    unique_holders: Optional[int] = None
    risk_score: Optional[float] = None          # RugCheck's own normalized 0-100, higher = riskier
    insider_bundle_flag: bool = False
    raw_risks: list[str] = []                     # RugCheck's own named risk findings, passed through for the thesis/flags layer


class RugCheckProvider:
    def __init__(self, cache_ttl_seconds: float = 120, failure_threshold: int = 4, cooldown_seconds: float = 120):
        self._http = httpx.AsyncClient(base_url=RUGCHECK_BASE_URL, timeout=10.0)
        self._breaker = breakers.get("rugcheck", failure_threshold=failure_threshold, cooldown_seconds=cooldown_seconds)
        self.cache_ttl_seconds = cache_ttl_seconds

    async def close(self):
        await self._http.aclose()

    @with_retry(max_attempts=3)
    async def _get_summary(self, mint: str) -> dict:
        resp = await self._http.get(f"/tokens/{mint}/report/summary")
        resp.raise_for_status()
        return resp.json()

    def _parse_report(self, raw: dict) -> RugCheckReport:
        """
        Defensive parsing — every field individually optional, since a
        partial/differently-shaped response should degrade gracefully
        (feeding a Caution-tier "missing safety data" flag downstream in
        ScoringEngine.evaluate_safety) rather than throw and lose the
        whole token's safety read.
        """
        try:
            token_info = raw.get("token", {}) or {}
            markets = raw.get("markets", []) or []
            top_holders = raw.get("topHolders", []) or []
            risks = raw.get("risks", []) or []

            mint_revoked = None
            if "mintAuthority" in token_info:
                mint_revoked = token_info.get("mintAuthority") is None
            freeze_revoked = None
            if "freezeAuthority" in token_info:
                freeze_revoked = token_info.get("freezeAuthority") is None

            lp_locked_pct = None
            if markets:
                lp_pcts = [m.get("lp", {}).get("lpLockedPct") for m in markets if m.get("lp", {}).get("lpLockedPct") is not None]
                if lp_pcts:
                    lp_locked_pct = sum(lp_pcts) / len(lp_pcts)

            top10_pct = None
            dev_pct = None
            if top_holders:
                sorted_holders = sorted(top_holders, key=lambda h: h.get("pct", 0), reverse=True)
                top10_pct = sum(h.get("pct", 0) for h in sorted_holders[:10])
                insider_holder = next((h for h in sorted_holders if h.get("insider")), None)
                if insider_holder:
                    dev_pct = insider_holder.get("pct")

            risk_names = [r.get("name", "") for r in risks if r.get("name")]
            insider_flag = any("insider" in r.lower() or "bundle" in r.lower() for r in risk_names)

            # RugCheck's "creator" field — the wallet that deployed the token.
            # Field name is a best-effort guess like the rest of this parser
            # (see module docstring); verify against a live response.
            deployer_address = raw.get("creator") or token_info.get("creator")

            return RugCheckReport(
                mint_authority_revoked=mint_revoked,
                freeze_authority_revoked=freeze_revoked,
                lp_locked_pct=lp_locked_pct,
                top10_holder_pct=top10_pct,
                dev_wallet_pct=dev_pct,
                deployer_address=deployer_address,
                unique_holders=raw.get("totalHolders"),
                risk_score=raw.get("score_normalised") or raw.get("score"),
                insider_bundle_flag=insider_flag,
                raw_risks=risk_names,
            )
        except (KeyError, TypeError, AttributeError) as e:
            print(f"[rugcheck] failed to parse report: {e}")
            return RugCheckReport()  # all-None report — treated as missing data, not a crash

    async def get_report(self, mint_address: str) -> Optional[RugCheckReport]:
        cache_key = f"report:{mint_address}"

        async def fetch():
            try:
                raw = await self._breaker.call(lambda: self._get_summary(mint_address))
            except (CircuitOpenError, Exception) as e:  # noqa: BLE001
                print(f"[rugcheck] fetch failed for {mint_address}: {e}")
                return None
            return self._parse_report(raw)

        return await rugcheck_cache.get_or_fetch(cache_key, ttl_seconds=self.cache_ttl_seconds, fetch_fn=fetch)
