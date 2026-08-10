"""
GoPlus Security data source — free tier, multi-chain, no key required at
low volume (an API key raises rate limits but isn't mandatory).

Covers the honeypot + buy/sell tax checks in the Safety gate — the part
RugCheck doesn't specialize in. Chain-aware: Solana uses a dedicated
endpoint shape, EVM chains (Base, Ethereum, etc — "Solana first, then
Base/Ethereum" per the Phase 1 brief) use the standard numeric-chain-id
token_security endpoint. This split matters: don't assume one endpoint
shape works for both.

Endpoint shapes confirmed via public documentation:
  Solana:  GET https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses={mint}
  EVM:     GET https://api.gopluslabs.io/api/v1/token_security/{chain_id}?contract_addresses={address}

As with rugcheck.py: exact response field names below are a best-effort
mapping from public documentation, not verified against a live response
in this sandbox. Flagged, not silently assumed correct.
"""

from __future__ import annotations
from typing import Optional
import httpx
from pydantic import BaseModel

from ..cache import make_cache, with_retry
from ..circuit_breaker import breakers, CircuitOpenError

GOPLUS_BASE_URL = "https://api.gopluslabs.io/api/v1"

# GoPlus's numeric chain IDs for the EVM-style endpoint — extend as needed
EVM_CHAIN_IDS = {
    "ethereum": "1",
    "base": "8453",
    "bsc": "56",
    "polygon": "137",
    "arbitrum": "42161",
}

goplus_cache = make_cache("goplus")


class GoPlusSecurityReport(BaseModel):
    is_honeypot: Optional[bool] = None
    buy_tax_pct: Optional[float] = None
    sell_tax_pct: Optional[float] = None
    is_mintable: Optional[bool] = None       # EVM-specific, cross-check against RugCheck's mint_authority_revoked on Solana
    is_proxy_contract: Optional[bool] = None  # EVM-specific — a proxy can have its logic swapped post-launch, a rug vector RugCheck doesn't check


class GoPlusProvider:
    def __init__(self, api_key: Optional[str] = None, cache_ttl_seconds: float = 120, failure_threshold: int = 4, cooldown_seconds: float = 120):
        headers = {"API-KEY": api_key} if api_key else {}
        self._http = httpx.AsyncClient(base_url=GOPLUS_BASE_URL, timeout=10.0, headers=headers)
        self._breaker = breakers.get("goplus", failure_threshold=failure_threshold, cooldown_seconds=cooldown_seconds)
        self.cache_ttl_seconds = cache_ttl_seconds

    async def close(self):
        await self._http.aclose()

    @with_retry(max_attempts=3)
    async def _get_solana_security(self, mint: str) -> dict:
        resp = await self._http.get("/solana/token_security", params={"contract_addresses": mint})
        resp.raise_for_status()
        return resp.json()

    @with_retry(max_attempts=3)
    async def _get_evm_security(self, chain_id: str, address: str) -> dict:
        resp = await self._http.get(f"/token_security/{chain_id}", params={"contract_addresses": address})
        resp.raise_for_status()
        return resp.json()

    def _parse_report(self, raw: dict, address: str) -> GoPlusSecurityReport:
        try:
            result = (raw.get("result") or {}).get(address.lower(), {}) or (raw.get("result") or {}).get(address, {})
            if not result:
                # GoPlus keys results by lowercased address for EVM; Solana
                # addresses are case-sensitive base58, so try exact match
                # first, then fall back to the first (only) value present
                result_dict = raw.get("result") or {}
                result = next(iter(result_dict.values()), {}) if result_dict else {}

            def _to_bool(v):
                if v is None:
                    return None
                return str(v) == "1" or v is True

            def _to_pct(v):
                if v is None:
                    return None
                try:
                    return float(v) * 100 if float(v) <= 1 else float(v)
                except (TypeError, ValueError):
                    return None

            return GoPlusSecurityReport(
                is_honeypot=_to_bool(result.get("is_honeypot")),
                buy_tax_pct=_to_pct(result.get("buy_tax")),
                sell_tax_pct=_to_pct(result.get("sell_tax")),
                is_mintable=_to_bool(result.get("is_mintable")),
                is_proxy_contract=_to_bool(result.get("is_proxy")),
            )
        except (KeyError, TypeError, AttributeError) as e:
            print(f"[goplus] failed to parse report: {e}")
            return GoPlusSecurityReport()

    async def get_security_report(self, address: str, chain_id: str = "solana") -> Optional[GoPlusSecurityReport]:
        cache_key = f"security:{chain_id}:{address}"

        async def fetch():
            try:
                if chain_id == "solana":
                    raw = await self._breaker.call(lambda: self._get_solana_security(address))
                else:
                    evm_id = EVM_CHAIN_IDS.get(chain_id, chain_id)  # allow passing a raw numeric id too
                    raw = await self._breaker.call(lambda: self._get_evm_security(evm_id, address))
            except (CircuitOpenError, Exception) as e:  # noqa: BLE001
                print(f"[goplus] fetch failed for {address} on {chain_id}: {e}")
                return None
            return self._parse_report(raw, address)

        return await goplus_cache.get_or_fetch(cache_key, ttl_seconds=self.cache_ttl_seconds, fetch_fn=fetch)
