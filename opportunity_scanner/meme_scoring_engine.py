"""
Meme Coin Scoring Engine — metrics-in, safety-gated, hype-first score-out.

Companion to opportunity_scanner/scoring_engine.py (the main four-pillar
engine), but a genuinely different model per MEME_ARCHITECTURE.md:
Safety is evaluated FIRST and gates everything else. A coin that fails
Safety gets no Opportunity Score at all — not a low one, none. Only
coins that pass (Pass or Caution) proceed to the three weighted pillars,
where Hype & Virality is deliberately the strongest signal (up to 45%
depending on mode — see MODE_WEIGHTS).

Every calculate_* method is robust to missing data, same principle as
the main scoring_engine.py: a missing field doesn't crash it and
doesn't get treated as "bad," it just doesn't contribute to that
sub-score, with the gap disclosed in `reasons`, not silently papered over.
"""

from __future__ import annotations
from pydantic import BaseModel, Field
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple
from enum import Enum


# ---------------------------------------------------------------- mode

class Mode(str, Enum):
    SNIPER = "sniper"
    EARLY_MOMENTUM = "early_momentum"
    CONFIRMED_RUNNER = "confirmed_runner"


class ModeThresholds(BaseModel):
    """Everything mode-specific about the Safety gate and age window, in
    one object per mode — merged from what used to be two separate
    module-level dicts (HARD_THRESHOLDS + MODE_AGE_WINDOWS_MINUTES),
    since they're both "the definition of this mode" and belong together
    once this becomes settings-driven rather than hardcoded."""
    min_liquidity_usd: float
    max_tax_pct: float
    min_lp_locked_pct: float
    max_top10_pct: float
    max_dev_pct: float
    min_holders: int
    max_rugcheck_score: float
    age_min_minutes: float
    age_max_minutes: float


class CautionMarginConfig(BaseModel):
    """Caution zone: clears the hard threshold but not comfortably — e.g.
    a coin needs to clear max_top10_pct * top10_holder_fraction (not just
    max_top10_pct) to get a clean Pass rather than a Caution."""
    liquidity_multiplier: float = 1.5        # must be >= this x the hard minimum for a clean Pass
    top10_holder_fraction: float = 0.85       # must be <= this fraction of the hard-reject ceiling
    dev_wallet_fraction: float = 0.75
    rugcheck_score_fraction: float = 0.7


class HypeFormulaConfig(BaseModel):
    """
    The normalize ranges and sub-weights inside calculate_hype_virality —
    exposed here (rather than as literals inside the method) specifically
    because Hype is the strongest signal in the whole engine, so its
    internal formula is the highest-leverage thing to be able to tune
    without a code change.
    """
    velocity_normalize_lo: float = 1.0
    velocity_normalize_hi: float = 5.0
    acceleration_normalize_lo: float = 0.5
    acceleration_normalize_hi: float = 3.0
    boost_normalize_hi: float = 500.0
    kol_boost_cap: float = 15.0
    velocity_weight: float = 0.35
    acceleration_weight: float = 0.30
    trending_weight: float = 0.20
    social_presence_weight: float = 0.15


@dataclass
class MemeEngineConfig:
    """
    Everything ScoringEngine needs, bundled — the object settings.py's
    Settings.to_meme_engine_config() builds from settings.yaml, and what
    ScoringEngine falls back to sensible defaults for if none is passed
    in (so direct construction / existing tests keep working unchanged).
    """
    mode_thresholds: Dict[Mode, ModeThresholds] = field(default_factory=lambda: dict(DEFAULT_MODE_THRESHOLDS))
    mode_weights: Dict[Mode, Dict[str, float]] = field(default_factory=lambda: dict(DEFAULT_MODE_WEIGHTS))
    caution_margin: CautionMarginConfig = field(default_factory=CautionMarginConfig)
    hype_formula: HypeFormulaConfig = field(default_factory=HypeFormulaConfig)


# Hype is deliberately the strongest signal in every mode, per the brief —
# heaviest in Sniper (almost the only real signal that early), lightest
# (but still meaningful) in Confirmed Runner where on-chain sustainability
# starts to matter more than virality. See MEME_ARCHITECTURE.md §6.
# These are DEFAULTS now — settings.yaml's meme_scanner.weights overrides
# them; nothing here is the source of truth once Settings is in the loop.
DEFAULT_MODE_WEIGHTS: Dict[Mode, Dict[str, float]] = {
    Mode.SNIPER: {"hype": 0.45, "onchain": 0.25, "momentum": 0.30},
    Mode.EARLY_MOMENTUM: {"hype": 0.40, "onchain": 0.32, "momentum": 0.28},
    Mode.CONFIRMED_RUNNER: {"hype": 0.38, "onchain": 0.34, "momentum": 0.28},
}

DEFAULT_MODE_THRESHOLDS: Dict[Mode, ModeThresholds] = {
    Mode.SNIPER: ModeThresholds(
        min_liquidity_usd=15_000, max_tax_pct=10, min_lp_locked_pct=70,
        max_top10_pct=30, max_dev_pct=8, min_holders=50, max_rugcheck_score=40,
        age_min_minutes=0, age_max_minutes=20,
    ),
    Mode.EARLY_MOMENTUM: ModeThresholds(
        min_liquidity_usd=20_000, max_tax_pct=10, min_lp_locked_pct=80,
        max_top10_pct=28, max_dev_pct=6, min_holders=100, max_rugcheck_score=35,
        age_min_minutes=20, age_max_minutes=180,
    ),
    Mode.CONFIRMED_RUNNER: ModeThresholds(
        min_liquidity_usd=25_000, max_tax_pct=8, min_lp_locked_pct=90,
        max_top10_pct=25, max_dev_pct=5, min_holders=150, max_rugcheck_score=30,
        age_min_minutes=180, age_max_minutes=1440,
    ),
}

# Backward-compat aliases — MODE_AGE_WINDOWS_MINUTES is still read by
# meme_aggregator.py's docstrings/any external code expecting the old
# module-level dict shape. Derived from DEFAULT_MODE_THRESHOLDS so there's
# one source of truth, not two dicts that could drift apart.
MODE_AGE_WINDOWS_MINUTES: Dict[Mode, Tuple[float, float]] = {
    m: (t.age_min_minutes, t.age_max_minutes) for m, t in DEFAULT_MODE_THRESHOLDS.items()
}
MODE_WEIGHTS = DEFAULT_MODE_WEIGHTS
HARD_THRESHOLDS = DEFAULT_MODE_THRESHOLDS


# ---------------------------------------------------------------- input model

class MemeCoinMetrics(BaseModel):
    symbol: str
    token_address: str
    chain_id: str = "solana"
    price_usd: Optional[float] = None
    market_cap_usd: Optional[float] = None
    liquidity_usd: float
    pair_age_minutes: float
    exchange_listings: int = 1

    # --- Safety (RugCheck + GoPlus) ---
    mint_authority_revoked: Optional[bool] = None
    freeze_authority_revoked: Optional[bool] = None
    is_honeypot: Optional[bool] = None
    buy_tax_pct: Optional[float] = None
    sell_tax_pct: Optional[float] = None
    lp_locked_pct: Optional[float] = None
    top10_holder_pct: Optional[float] = None
    dev_wallet_pct: Optional[float] = None
    unique_holders: Optional[int] = None
    rugcheck_risk_score: Optional[float] = None   # 0-100, higher = riskier
    insider_bundle_flag: bool = False
    deployer_address: Optional[str] = None

    # --- Hype & Virality ---
    mention_velocity_ratio: Optional[float] = None   # current 15min mentions / baseline
    acceleration_ratio: Optional[float] = None        # velocity now / velocity prior window
    dex_boosted: bool = False
    boost_amount: Optional[float] = None
    has_website: bool = False
    has_twitter: bool = False
    has_telegram: bool = False
    kol_score: Optional[float] = None                  # 0-100 if tracked, else no boost

    # --- On-chain Health ---
    unique_makers_1h: Optional[int] = None
    buy_tx_count_1h: Optional[int] = None
    sell_tx_count_1h: Optional[int] = None
    buy_sell_ratio: Optional[float] = None
    holder_growth_pct_1h: Optional[float] = None
    volume_to_liquidity_ratio: Optional[float] = None
    volume_24h_usd: Optional[float] = None            # raw volume — needed for dashboard display, not just the ratio
    avg_tx_size_variance: Optional[float] = None        # low variance = suspiciously uniform (bot-shaped)

    # --- Momentum & Flow ---
    vol_accel_ratio: Optional[float] = None              # 15m volume annualized to 1h / 1h average volume
    price_change_pct: Optional[float] = None
    volume_change_pct: Optional[float] = None


# ---------------------------------------------------------------- output models

class SafetyResult(BaseModel):
    grade: str = Field(..., description='"Pass" | "Caution" | "Fail"')
    reasons: List[str] = []


class PillarScores(BaseModel):
    hype: float = Field(..., ge=0, le=100)
    onchain_health: float = Field(..., ge=0, le=100)
    momentum: float = Field(..., ge=0, le=100)


class RiskFlag(BaseModel):
    """Structured, not a plain string — same info/warning/danger severity
    shape as degen_models.py::DegenFlag, so both scanners read consistently
    to whoever's looking at either one's output."""
    label: str
    severity: str = "warning"   # "info" | "warning" | "danger"


class HypeEvent(BaseModel):
    """A detected change since the token's previous scan — 'sudden mention
    spike', 'newly boosted', 'KOL activity jump', etc. Structurally
    identical to RiskFlag (same reasoning: consistent shape across the
    codebase) but kept as its own type since a hype event isn't a risk —
    it's the opposite, a reason TO pay attention, not a reason for caution."""
    label: str
    severity: str = "info"   # "info" | "notable" | "explosive"


class FinalMemeResult(BaseModel):
    symbol: str
    token_address: str
    mode: str
    safety: SafetyResult
    opportunity_score: Optional[float] = None    # None if safety.grade == "Fail"
    hype_level: Optional[str] = None               # "Low" | "Medium" | "High" | "Explosive"
    pillar_scores: Optional[PillarScores] = None
    confidence: float = Field(..., ge=0, le=100)
    risk_flags: List[RiskFlag] = []
    hype_events: List[HypeEvent] = []
    thesis: str
    key_metrics: Dict[str, Optional[float]] = {}


# ---------------------------------------------------------------- helpers

def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return float(max(lo, min(hi, v)))


def _normalize(v: float, lo: float, hi: float) -> float:
    if hi == lo:
        return 50.0
    return _clamp((v - lo) / (hi - lo) * 100.0)


def _healthy_band(value: Optional[float], lo: float, hi: float, exhaustion_hi: float, exhaustion_lo: float) -> Optional[float]:
    if value is None:
        return None
    if lo <= value <= hi:
        return 50 + _normalize(value, lo, hi) * 0.5
    if value > hi:
        overshoot = value - hi
        span = max(exhaustion_hi - hi, 1e-6)
        return _clamp(100 - _normalize(overshoot, 0, span) * 0.7)
    overshoot = lo - value
    span = max(lo - exhaustion_lo, 1e-6)
    return _clamp(_normalize(value, exhaustion_lo, lo) * 0.7)


def _triangular_band(value: float, floor: float, peak: float, ceiling: float) -> float:
    if value <= floor or value >= ceiling:
        return 20.0
    if value <= peak:
        return 20 + 80 * (value - floor) / (peak - floor)
    return 100 - 80 * (value - peak) / (ceiling - peak)


def _blend(parts: List[Tuple[Optional[float], float]]) -> Optional[float]:
    available = [(v, w) for v, w in parts if v is not None]
    if not available:
        return None
    total_w = sum(w for _, w in available)
    if total_w <= 0:
        return None
    return sum(v * w for v, w in available) / total_w


# ---------------------------------------------------------------- engine

class ScoringEngine:
    def __init__(self, mode: Mode = Mode.EARLY_MOMENTUM, weights: Optional[Dict[str, float]] = None, config: Optional[MemeEngineConfig] = None, deployer_blacklist: Optional[set] = None):
        self.mode = mode
        self.config = config or MemeEngineConfig()
        self.deployer_blacklist = deployer_blacklist or set()
        self.weights = dict(weights or self.config.mode_weights[mode])
        total = sum(self.weights.values())
        self.weights = {k: v / total for k, v in self.weights.items()}  # normalize, same defensive pattern as the main engine

    # ------------------------------------------------------- 1. Safety (gate)

    def evaluate_safety(self, m: MemeCoinMetrics) -> SafetyResult:
        if m.deployer_address and m.deployer_address in self.deployer_blacklist:
            return SafetyResult(
                grade="Fail",
                reasons=[f"Deployer wallet {m.deployer_address[:12]}... is blacklisted — previously associated with a rug"],
            )

        hard = self.config.mode_thresholds[self.mode]
        margin = self.config.caution_margin
        fails: List[str] = []

        if m.liquidity_usd < hard.min_liquidity_usd:
            fails.append(f"Liquidity ${m.liquidity_usd:,.0f} below ${hard.min_liquidity_usd:,.0f} minimum")
        if m.mint_authority_revoked is False:
            fails.append("Mint authority NOT revoked — supply can be inflated at will")
        if m.freeze_authority_revoked is False:
            fails.append("Freeze authority NOT revoked — holder wallets can be frozen")
        if m.is_honeypot is True:
            fails.append("Honeypot detected — sells are blocked or fail")
        if m.buy_tax_pct is not None and m.buy_tax_pct > hard.max_tax_pct:
            fails.append(f"Buy tax {m.buy_tax_pct:.1f}% exceeds {hard.max_tax_pct:.0f}% ceiling")
        if m.sell_tax_pct is not None and m.sell_tax_pct > hard.max_tax_pct:
            fails.append(f"Sell tax {m.sell_tax_pct:.1f}% exceeds {hard.max_tax_pct:.0f}% ceiling")
        if m.lp_locked_pct is not None and m.lp_locked_pct < hard.min_lp_locked_pct:
            fails.append(f"Only {m.lp_locked_pct:.0f}% of LP locked/burned, below {hard.min_lp_locked_pct:.0f}% minimum")
        if m.top10_holder_pct is not None and m.top10_holder_pct > hard.max_top10_pct:
            fails.append(f"Top 10 holders control {m.top10_holder_pct:.0f}% — exceeds {hard.max_top10_pct:.0f}% ceiling")
        if m.dev_wallet_pct is not None and m.dev_wallet_pct > hard.max_dev_pct:
            fails.append(f"Dev wallet holds {m.dev_wallet_pct:.1f}% of supply — exceeds {hard.max_dev_pct:.0f}% ceiling")
        if m.unique_holders is not None and m.unique_holders < hard.min_holders:
            fails.append(f"Only {m.unique_holders} holders, below {hard.min_holders} minimum")
        if m.rugcheck_risk_score is not None and m.rugcheck_risk_score > hard.max_rugcheck_score:
            fails.append(f"RugCheck risk score {m.rugcheck_risk_score:.0f} exceeds {hard.max_rugcheck_score:.0f} ceiling")
        if m.insider_bundle_flag:
            fails.append("RugCheck flagged insider/bundle wallet concentration")

        if not (hard.age_min_minutes <= m.pair_age_minutes <= hard.age_max_minutes):
            fails.append(f"Age {m.pair_age_minutes:.0f}min outside {self.mode.value} window ({hard.age_min_minutes:.0f}-{hard.age_max_minutes:.0f}min)")

        if fails:
            return SafetyResult(grade="Fail", reasons=fails)

        cautions: List[str] = []
        if m.liquidity_usd < hard.min_liquidity_usd * margin.liquidity_multiplier:
            cautions.append("Liquidity clears the minimum but isn't comfortably above it")
        if m.top10_holder_pct is not None and m.top10_holder_pct > hard.max_top10_pct * margin.top10_holder_fraction:
            cautions.append("Holder concentration near the reject ceiling")
        if m.dev_wallet_pct is not None and m.dev_wallet_pct > hard.max_dev_pct * margin.dev_wallet_fraction:
            cautions.append("Dev wallet holding near the reject ceiling")
        if m.rugcheck_risk_score is not None and m.rugcheck_risk_score > hard.max_rugcheck_score * margin.rugcheck_score_fraction:
            cautions.append("RugCheck score elevated, though under the hard ceiling")

        # Missing critical safety data is itself a caution, not silently ignored
        missing = [
            name for name, val in [
                ("mint authority status", m.mint_authority_revoked), ("freeze authority status", m.freeze_authority_revoked),
                ("holder concentration", m.top10_holder_pct), ("RugCheck score", m.rugcheck_risk_score),
            ] if val is None
        ]
        if missing:
            cautions.append(f"Missing safety data: {', '.join(missing)} — treat with extra scrutiny")

        return SafetyResult(grade="Caution" if cautions else "Pass", reasons=cautions)

    # ------------------------------------------------- 2. Hype & Virality (strongest signal)

    def calculate_hype_virality(self, m: MemeCoinMetrics) -> Tuple[float, List[str]]:
        """
        The strongest signal in the whole engine, per the brief. Every
        sub-component is independently robust to missing data — for a
        brand-new Sniper-mode coin, mention_velocity_ratio is often
        unavailable (LunarCrush lags real-time launches, see
        MEME_ARCHITECTURE.md §3.3), so dex_boosted/social presence carry
        more real weight in practice than their nominal share suggests.
        """
        reasons: List[str] = []
        hf = self.config.hype_formula

        velocity_score = _normalize(m.mention_velocity_ratio, hf.velocity_normalize_lo, hf.velocity_normalize_hi) if m.mention_velocity_ratio is not None else None
        if velocity_score is not None:
            reasons.append(f"Mention velocity {m.mention_velocity_ratio:.1f}x baseline")

        acceleration_score = _normalize(m.acceleration_ratio, hf.acceleration_normalize_lo, hf.acceleration_normalize_hi) if m.acceleration_ratio is not None else None
        if acceleration_score is not None:
            direction = "accelerating" if m.acceleration_ratio > 1 else "decelerating"
            reasons.append(f"Mention velocity {direction} ({m.acceleration_ratio:.2f}x)")

        if m.dex_boosted:
            trending_score = 100.0
            reasons.append("Currently boosted/trending on DexScreener")
        elif m.boost_amount is not None:
            trending_score = _normalize(m.boost_amount, 0, hf.boost_normalize_hi)
        else:
            trending_score = None

        social_count = sum([m.has_website, m.has_twitter, m.has_telegram])
        social_score = (social_count / 3) * 100
        reasons.append(f"{social_count}/3 social links present (website/twitter/telegram)")

        parts: List[Tuple[Optional[float], float]] = [
            (velocity_score, hf.velocity_weight), (acceleration_score, hf.acceleration_weight),
            (trending_score, hf.trending_weight), (social_score, hf.social_presence_weight),
        ]
        composite = _blend(parts)
        if composite is None:
            composite = 50.0
            reasons.append("No hype signal data available at all — neutral default, treat as unknown rather than low")

        kol_boost = 0.0
        if m.kol_score is not None:
            kol_boost = min(_normalize(m.kol_score, 0, 100) * (hf.kol_boost_cap / 100), hf.kol_boost_cap)
            reasons.append(f"KOL activity boost +{kol_boost:.1f}")

        return _clamp(composite + kol_boost), reasons

    @staticmethod
    def get_hype_level(hype_score: float) -> str:
        if hype_score >= 85:
            return "Explosive"
        if hype_score >= 65:
            return "High"
        if hype_score >= 40:
            return "Medium"
        return "Low"

    # ------------------------------------------------------ 3. On-chain Health

    def _wash_trading_penalty(self, m: MemeCoinMetrics) -> float:
        penalty = 0.0
        if m.volume_to_liquidity_ratio is not None and m.volume_to_liquidity_ratio > 15:
            penalty += 30
        if m.unique_makers_1h and m.buy_tx_count_1h:
            tx_per_wallet = m.buy_tx_count_1h / max(m.unique_makers_1h, 1)
            if tx_per_wallet > 4:
                penalty += 25
        if m.avg_tx_size_variance is not None and m.avg_tx_size_variance < 0.05:
            penalty += 20
        return min(penalty, 70)

    def calculate_onchain_health(self, m: MemeCoinMetrics) -> Tuple[float, List[str]]:
        reasons: List[str] = []
        hard = self.config.mode_thresholds[self.mode]

        if m.unique_makers_1h is not None:
            buyer_score = _normalize(m.unique_makers_1h, hard.min_holders * 0.5, hard.min_holders * 1.5)
            reasons.append(f"{m.unique_makers_1h} unique makers in the last hour")
        elif m.buy_tx_count_1h is not None:
            buyer_score = _normalize(m.buy_tx_count_1h, hard.min_holders * 0.5, hard.min_holders * 1.5)
            reasons.append(f"Unique maker count unavailable — used {m.buy_tx_count_1h} buy tx count as a weaker proxy")
        else:
            buyer_score = None

        bs_score = _healthy_band(m.buy_sell_ratio, 0.8, 3.0, 8.0, 0.2)
        if bs_score is not None:
            reasons.append(f"Buy/sell ratio {m.buy_sell_ratio:.2f}")

        growth_score = _normalize(m.holder_growth_pct_1h, 0, 20) if m.holder_growth_pct_1h is not None else None
        if growth_score is not None:
            reasons.append(f"Holder growth {m.holder_growth_pct_1h:+.1f}% (1h)")

        liq_quality = _normalize(m.volume_to_liquidity_ratio, 0.5, 5.0) if m.volume_to_liquidity_ratio is not None else None

        wash_penalty = self._wash_trading_penalty(m)
        organic_score = 100 - wash_penalty
        if wash_penalty > 0:
            reasons.append(f"Wash-trading penalty {wash_penalty:.0f}pts applied to organic-flow read")

        parts: List[Tuple[Optional[float], float]] = [
            (buyer_score, 0.30), (bs_score, 0.20), (growth_score, 0.20),
            (liq_quality, 0.15), (organic_score, 0.15),
        ]
        composite = _blend(parts)
        if composite is None:
            composite = 50.0
            reasons.append("No on-chain health data available — neutral default")

        return _clamp(composite), reasons

    # ------------------------------------------------------- 4. Momentum & Flow

    @staticmethod
    def _price_vol_alignment(price_change_pct: Optional[float], volume_change_pct: Optional[float]) -> Tuple[Optional[float], bool]:
        """
        Sign-based, same pattern as the main scanner's OI/price divergence
        (ARCHITECTURE.md §2.2): aligned signs = confirmed move, opposite
        signs = divergence, direction-agnostic. Returns (score, is_divergent).
        """
        if price_change_pct is None or volume_change_pct is None:
            return None, False
        aligned = (price_change_pct >= 0) == (volume_change_pct >= 0)
        if aligned:
            magnitude = min(abs(price_change_pct), abs(volume_change_pct))
            score = _clamp(60 + _normalize(magnitude, 0, 50) * 0.4)
            return score, False
        return 25.0, True

    def calculate_momentum(self, m: MemeCoinMetrics) -> Tuple[float, List[str], bool]:
        reasons: List[str] = []

        vol_score = _normalize(m.vol_accel_ratio, 1.0, 4.0) if m.vol_accel_ratio is not None else None
        if vol_score is not None:
            reasons.append(f"Volume acceleration {m.vol_accel_ratio:.1f}x")

        alignment_score, divergent = self._price_vol_alignment(m.price_change_pct, m.volume_change_pct)
        if alignment_score is not None:
            read = "divergent" if divergent else "confirmed" if alignment_score >= 70 else "mixed"
            reasons.append(f"Price/volume alignment: {read}")

        mcap_score = _triangular_band(m.market_cap_usd, 50_000, 300_000, 2_000_000) if m.market_cap_usd is not None else None
        if mcap_score is not None:
            in_range = 50_000 <= m.market_cap_usd <= 2_000_000
            reasons.append(f"Market cap ${m.market_cap_usd:,.0f} ({'sweet spot' if in_range else 'outside typical range'})")

        parts: List[Tuple[Optional[float], float]] = [(vol_score, 0.40), (alignment_score, 0.30), (mcap_score, 0.30)]
        composite = _blend(parts)
        if composite is None:
            composite = 50.0
            reasons.append("No momentum data available — neutral default")

        return _clamp(composite), reasons, divergent

    # ------------------------------------------------------------ confidence

    def calculate_confidence(self, m: MemeCoinMetrics, safety: SafetyResult) -> float:
        """
        Lower confidence for: missing data across pillars, Caution-tier
        safety (even though it passed the gate, a Caution result means
        something's genuinely uncertain), and very young pair age (less
        time for signals to have stabilized).
        """
        all_fields = [
            m.mention_velocity_ratio, m.acceleration_ratio, m.kol_score,
            m.unique_makers_1h, m.buy_sell_ratio, m.holder_growth_pct_1h,
            m.vol_accel_ratio, m.price_change_pct, m.volume_change_pct,
            m.rugcheck_risk_score, m.top10_holder_pct,
        ]
        completeness = sum(1 for f in all_fields if f is not None) / len(all_fields)

        safety_confidence = {"Pass": 1.0, "Caution": 0.6, "Fail": 0.0}[safety.grade]

        age_lo, age_hi = self.config.mode_thresholds[self.mode].age_min_minutes, self.config.mode_thresholds[self.mode].age_max_minutes
        age_progress = _clamp(_normalize(m.pair_age_minutes, age_lo, age_hi) / 100, 0, 1) if age_hi > age_lo else 1.0

        confidence = (completeness * 0.4 + safety_confidence * 0.4 + age_progress * 0.2) * 100
        return round(_clamp(confidence), 1)

    # ------------------------------------------------------------ flags/thesis

    def generate_risk_flags(self, m: MemeCoinMetrics, safety: SafetyResult, divergent: bool) -> List[RiskFlag]:
        flags = [RiskFlag(label=r, severity="warning") for r in safety.reasons]  # Caution-tier reasons carry through
        if divergent:
            flags.append(RiskFlag(label="Price/volume divergence — move may not be backed by real demand", severity="warning"))
        wash_penalty = self._wash_trading_penalty(m)
        if wash_penalty > 50:
            flags.append(RiskFlag(label=f"Elevated wash-trading signal (penalty {wash_penalty:.0f}pts)", severity="danger"))
        elif wash_penalty > 30:
            flags.append(RiskFlag(label=f"Elevated wash-trading signal (penalty {wash_penalty:.0f}pts)", severity="warning"))
        if not flags:
            flags.append(RiskFlag(label="No elevated risk signals beyond standard memecoin category risk", severity="info"))
        return flags

    def generate_thesis(self, m: MemeCoinMetrics, safety: SafetyResult, hype_score: Optional[float], onchain_score: Optional[float], divergent: bool) -> str:
        """
        Explicitly structured around "why the hype is real" + "what
        on-chain data supports it" per the Phase 5 brief — pulls the
        single most concrete piece of evidence for each, rather than
        blindly taking whichever reason happened to be generated first.
        """
        parts = [f"Safety: {safety.grade}."]

        hype_evidence = []
        if m.dex_boosted:
            hype_evidence.append("actively boosted/trending on DexScreener")
        if m.mention_velocity_ratio is not None and m.mention_velocity_ratio > 1.5:
            hype_evidence.append(f"mentions running {m.mention_velocity_ratio:.1f}x baseline")
        if m.kol_score is not None and m.kol_score > 50:
            hype_evidence.append("real KOL/influencer activity")
        if hype_evidence:
            parts.append(f"Hype is real: {', '.join(hype_evidence)}.")
        elif hype_score is not None and hype_score < 40:
            parts.append("Hype is weak — no strong velocity, boost, or KOL signal detected.")

        onchain_evidence = []
        if m.unique_makers_1h is not None and m.unique_makers_1h > 0:
            onchain_evidence.append(f"{m.unique_makers_1h} unique makers in the last hour")
        if m.holder_growth_pct_1h is not None and m.holder_growth_pct_1h > 0:
            onchain_evidence.append(f"holders growing {m.holder_growth_pct_1h:+.0f}%/hr")
        if m.buy_sell_ratio is not None and m.buy_sell_ratio > 1.2:
            onchain_evidence.append(f"buy/sell ratio {m.buy_sell_ratio:.1f} favoring accumulation")
        if onchain_evidence:
            parts.append(f"On-chain support: {', '.join(onchain_evidence)}.")
        elif onchain_score is not None and onchain_score < 40:
            parts.append("On-chain support is thin — limited buyer/holder-growth evidence.")

        if divergent:
            parts.append("Momentum divergence detected — treat with extra caution.")

        return " ".join(parts)

    # ------------------------------------------------------------------ score

    def score(self, m: MemeCoinMetrics) -> FinalMemeResult:
        safety = self.evaluate_safety(m)

        key_metrics = {
            "liquidity_usd": m.liquidity_usd, "market_cap_usd": m.market_cap_usd,
            "pair_age_minutes": m.pair_age_minutes, "top10_holder_pct": m.top10_holder_pct,
            "unique_holders": float(m.unique_holders) if m.unique_holders is not None else None,
            "rugcheck_risk_score": m.rugcheck_risk_score,
            "volume_24h_usd": m.volume_24h_usd, "mention_velocity_ratio": m.mention_velocity_ratio,
        }

        if safety.grade == "Fail":
            confidence = self.calculate_confidence(m, safety)
            return FinalMemeResult(
                symbol=m.symbol, token_address=m.token_address, mode=self.mode.value,
                safety=safety, opportunity_score=None, hype_level=None, pillar_scores=None,
                confidence=confidence,
                risk_flags=[RiskFlag(label=r, severity="danger") for r in safety.reasons],
                thesis="Rejected at the safety gate — no opportunity score computed.",
                key_metrics=key_metrics,
            )

        hype_score, hype_reasons = self.calculate_hype_virality(m)
        onchain_score, onchain_reasons = self.calculate_onchain_health(m)
        momentum_score, momentum_reasons, divergent = self.calculate_momentum(m)

        pillar_scores = PillarScores(hype=hype_score, onchain_health=onchain_score, momentum=momentum_score)

        opportunity_score = (
            hype_score * self.weights["hype"]
            + onchain_score * self.weights["onchain"]
            + momentum_score * self.weights["momentum"]
        )

        confidence = self.calculate_confidence(m, safety)
        risk_flags = self.generate_risk_flags(m, safety, divergent)
        thesis = self.generate_thesis(m, safety, hype_score, onchain_score, divergent)

        return FinalMemeResult(
            symbol=m.symbol, token_address=m.token_address, mode=self.mode.value,
            safety=safety,
            opportunity_score=round(_clamp(opportunity_score), 1),
            hype_level=self.get_hype_level(hype_score),
            pillar_scores=pillar_scores,
            confidence=confidence,
            risk_flags=risk_flags,
            thesis=thesis,
            key_metrics=key_metrics,
        )
