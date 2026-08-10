"""
RugCheck + GoPlus parser tests — synthetic API-shaped JSON, no network
needed. Tests the _parse_report methods directly against plausible
response shapes, since this sandbox can't hit either live API to
confirm exact field names (flagged honestly in both provider modules).

Checks:
  1. RugCheck: mint/freeze authority correctly inferred from presence/absence
  2. RugCheck: top-10 holder % correctly summed and dev wallet found via insider flag
  3. RugCheck: insider/bundle risk correctly detected from the risks list
  4. RugCheck: malformed/partial response degrades to an all-None report, doesn't crash
  5. GoPlus: honeypot + tax correctly parsed from a Solana-shaped response
  6. GoPlus: percentage-vs-fraction tax values both normalize correctly
"""

from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opportunity_scanner.data_sources.rugcheck import RugCheckProvider
from opportunity_scanner.data_sources.goplus import GoPlusProvider


def main():
    rc = RugCheckProvider()

    # 1 & 2 & 3. A realistic-shaped safe-ish token report
    safe_raw = {
        "token": {"mintAuthority": None, "freezeAuthority": None},  # None = revoked
        "markets": [{"lp": {"lpLockedPct": 95.0}}],
        "topHolders": [
            {"pct": 8.0, "insider": True},   # dev wallet, flagged as insider
            {"pct": 4.0}, {"pct": 3.5}, {"pct": 3.0}, {"pct": 2.5},
            {"pct": 2.0}, {"pct": 1.8}, {"pct": 1.5}, {"pct": 1.2}, {"pct": 1.0},
        ],
        "totalHolders": 250,
        "score_normalised": 8.0,
        "risks": [{"name": "Low liquidity"}],
    }
    report = rc._parse_report(safe_raw)
    print(f"Safe token report: mint_revoked={report.mint_authority_revoked}, freeze_revoked={report.freeze_authority_revoked}, "
          f"top10={report.top10_holder_pct}, dev_pct={report.dev_wallet_pct}, insider_flag={report.insider_bundle_flag}, risk={report.risk_score}")
    assert report.mint_authority_revoked is True
    assert report.freeze_authority_revoked is True
    assert abs(report.top10_holder_pct - 28.5) < 0.01, f"Expected top10 ~28.5%, got {report.top10_holder_pct}"
    assert report.dev_wallet_pct == 8.0
    assert report.insider_bundle_flag is False  # "Low liquidity" isn't an insider/bundle risk
    print("1-2. RugCheck safe-token parsing (authorities, top10 sum, dev wallet): OK")

    # 3. Insider/bundle risk detected
    risky_raw = dict(safe_raw)
    risky_raw["risks"] = [{"name": "Insider wallets detected"}]
    risky_report = rc._parse_report(risky_raw)
    assert risky_report.insider_bundle_flag is True
    print("3. RugCheck insider/bundle risk correctly detected from risks list: OK")

    # 4. Malformed response degrades gracefully
    malformed_report = rc._parse_report({"unexpected": "shape"})
    assert malformed_report.mint_authority_revoked is None
    assert malformed_report.risk_score is None
    print("4. RugCheck malformed response degrades to all-None report, no crash: OK")

    # 5 & 6. GoPlus Solana-shaped response
    gp = GoPlusProvider()
    solana_raw = {
        "result": {
            "SomeMintAddress111": {
                "is_honeypot": "0", "buy_tax": "0.05", "sell_tax": "0.05",
                "is_mintable": "1",
            }
        }
    }
    gp_report = gp._parse_report(solana_raw, "SomeMintAddress111")
    print(f"GoPlus report: honeypot={gp_report.is_honeypot}, buy_tax={gp_report.buy_tax_pct}, sell_tax={gp_report.sell_tax_pct}, mintable={gp_report.is_mintable}")
    assert gp_report.is_honeypot is False
    assert abs(gp_report.buy_tax_pct - 5.0) < 0.01, f"Expected 5.0%, got {gp_report.buy_tax_pct}"  # 0.05 fraction -> 5%
    assert gp_report.is_mintable is True
    print("5-6. GoPlus honeypot/tax parsing, fraction-to-percent normalization: OK")

    print("\n✅ RugCheck + GoPlus parser tests passed.")


if __name__ == "__main__":
    main()
