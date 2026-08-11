"""
Correlation awareness — the direct answer to "if five 'Ready' signals
are all just following BTC, that's one bet expressed five times, not
five independent opportunities." Nothing in this scanner previously
distinguished a set of results that are genuinely uncorrelated
(different, independent theses) from a set that's really just one
macro move wearing five different tickers.

Computes pairwise correlation of recent daily returns across every
coin scanned in the SAME batch — not a new API call, this reuses OHLCV
data already fetched for scoring, via the scan-cycle memoization built
earlier this session (re-requesting a symbol's snapshot mid-scan-cycle
is a cache hit, not a new network call).
"""

from __future__ import annotations
from typing import Dict, List, Optional
import pandas as pd

DEFAULT_MIN_CORRELATION = 0.70
DEFAULT_LOOKBACK_DAYS = 20


def _daily_returns(daily_ohlcv: Optional[pd.DataFrame], lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> Optional[pd.Series]:
    if daily_ohlcv is None or len(daily_ohlcv) <= lookback_days:
        return None
    returns = daily_ohlcv["close"].pct_change().dropna()
    recent = returns.tail(lookback_days)
    if len(recent) <= 1:
        return None
    return recent.reset_index(drop=True)


def compute_correlation_clusters(
    daily_ohlcv_by_base: Dict[str, Optional[pd.DataFrame]],
    min_correlation: float = DEFAULT_MIN_CORRELATION,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> Dict[str, List[str]]:
    """
    Returns {base: [other bases in this same batch whose recent daily
    returns correlate above min_correlation]}. Every base gets an entry,
    even if empty — callers can rely on the key always existing.

    A coin needs at least lookback_days+1 daily candles to participate;
    coins with insufficient history are excluded from the correlation
    check entirely (not treated as uncorrelated OR correlated with
    anything — genuinely unknown, so silently skipped rather than
    guessed at).
    """
    returns_by_base: Dict[str, pd.Series] = {}
    for base, ohlcv in daily_ohlcv_by_base.items():
        r = _daily_returns(ohlcv, lookback_days)
        if r is not None:
            returns_by_base[base] = r

    clusters: Dict[str, List[str]] = {base: [] for base in daily_ohlcv_by_base}
    bases = list(returns_by_base.keys())
    for i, base_a in enumerate(bases):
        for base_b in bases[i + 1:]:
            series_a, series_b = returns_by_base[base_a], returns_by_base[base_b]
            n = min(len(series_a), len(series_b))
            if n <= 1:
                continue
            corr = series_a.tail(n).reset_index(drop=True).corr(series_b.tail(n).reset_index(drop=True))
            if corr is not None and corr >= min_correlation:
                clusters[base_a].append(base_b)
                clusters[base_b].append(base_a)

    return clusters
