"""
Settings — YAML + environment variables, via pydantic-settings.

Precedence (highest wins): environment variables > config.yaml > field
defaults below. This is pydantic-settings' standard precedence order,
which is why secrets (API keys) belong in the environment, never in
config.yaml — that file is meant to be committed to version control,
your .env / host environment variables are not.

This layer is intentionally separate from `config.py`'s dataclasses:
`config.py` is what every existing module (`scanner.py`, `factors/*.py`,
`regime.py`, etc.) already imports and depends on — changing that would
mean touching every file that uses it. Settings.to_scanner_config()
converts one to the other, so the YAML/env layer is additive: you get
config-file + env-var driven configuration without any existing module
needing to change.
"""

from __future__ import annotations
from pathlib import Path
from typing import Dict, List, Optional
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict, YamlConfigSettingsSource, PydanticBaseSettingsSource

from .config import (
    ScannerConfig, Weights, QualityFilters, TimeframeConfig,
    SignalBands, ConfidenceBands, RegimeConfig, SmartViewConfig, BucketThresholds,
)

CONFIG_YAML_PATH = Path(__file__).resolve().parent.parent / "settings.yaml"


class WeightsSettings(BaseModel):
    strength: float = 0.26
    oi_dynamics: float = 0.34
    momentum: float = 0.30
    social: float = 0.10

    @model_validator(mode="after")
    def _check_sum(self):
        total = self.strength + self.oi_dynamics + self.momentum + self.social
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"weights must sum to 1.0, got {total:.4f}")
        return self


class FiltersSettings(BaseModel):
    min_24h_volume_usd: float = 5_000_000.0
    min_market_cap_usd: float = 10_000_000.0
    min_exchange_listings: int = 1
    max_bid_ask_spread_pct: float = 1.5
    require_min_candles: int = 50


class TimeframesSettings(BaseModel):
    list: List[str] = ["15m", "1h", "4h", "1d"]
    weights: Dict[str, float] = {"15m": 0.10, "1h": 0.25, "4h": 0.30, "1d": 0.35}
    candles_per_timeframe: int = 250


class SignalBandsSettings(BaseModel):
    strong_buy: float = 80.0
    buy: float = 65.0
    neutral: float = 45.0
    caution: float = 25.0


class ConfidenceBandsSettings(BaseModel):
    high: float = 75.0
    medium: float = 50.0


class RegimeSettings(BaseModel):
    risk_on_threshold: float = 65.0
    risk_off_threshold: float = 35.0
    risk_off_dampener_points: float = 12.0
    dampen_above_score: float = 50.0
    volatility_lookback_days: int = 20
    volatility_normalize_lo: float = 0.30
    volatility_normalize_hi: float = 1.20


class BucketThresholdSettings(BaseModel):
    min_score: float
    min_confidence: float
    allowed_risk_tiers: List[str]
    min_data_completeness: float
    min_alignment_score: float = 0.0


class SmartViewSettings(BaseModel):
    """Dashboard Smart View bucket thresholds — see smart_view.py for
    the classification logic these feed into."""
    enabled: bool = True
    super_strong: BucketThresholdSettings = BucketThresholdSettings(
        min_score=80.0, min_confidence=75.0, allowed_risk_tiers=["core", "small_cap"], min_data_completeness=0.75,
        min_alignment_score=60.0,
    )
    strong: BucketThresholdSettings = BucketThresholdSettings(
        min_score=65.0, min_confidence=50.0, allowed_risk_tiers=["core", "small_cap", "high_risk"], min_data_completeness=0.5,
        min_alignment_score=0.0,
    )
    building: BucketThresholdSettings = BucketThresholdSettings(
        min_score=45.0, min_confidence=0.0, allowed_risk_tiers=["core", "small_cap", "high_risk"], min_data_completeness=0.0,
        min_alignment_score=0.0,
    )


class ExchangeSettings(BaseModel):
    primary: str = "bybit"
    quote_currency: str = "USDT"
    # Strict priority order for price/OHLCV/OI/funding, tried in order —
    # first success wins, no averaging. Hyperliquid first (no US
    # restriction, matches what AutoBot actually trades), Bybit last and
    # optional (confirmed geo-blocked for US-hosted deployments via a
    # live Railway deployment, not theoretical). Override this list to
    # change the order without touching code, e.g. to drop Bybit
    # entirely once it's no longer worth even trying.
    market_data_priority: List[str] = ["hyperliquid", "coingecko", "coinbase", "kraken", "bybit"]


class UniverseSettings(BaseModel):
    default: List[str] = ["BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "AVAX", "LINK", "DOT"]
    blacklist: List[str] = []
    whitelist: List[str] = []


class SchedulerSettings(BaseModel):
    interval_minutes: int = 15


class StorageSettings(BaseModel):
    db_path: str = "opportunity_scanner.db"


class CacheSettings(BaseModel):
    redis_url: Optional[str] = None


class AlertTriggerSettings(BaseModel):
    on_signal_change_to: List[str] = ["Strong Buy", "Buy"]
    min_confidence: float = 60.0
    cooldown_minutes: int = 60
    score_jump_threshold: float = 15.0


class TelegramAlertSettings(BaseModel):
    enabled: bool = False
    bot_token: Optional[str] = None
    chat_id: Optional[str] = None


class DiscordAlertSettings(BaseModel):
    enabled: bool = False
    webhook_url: Optional[str] = None


class EmailAlertSettings(BaseModel):
    enabled: bool = False
    smtp_host: Optional[str] = None
    smtp_port: int = 587
    from_address: Optional[str] = None
    to_addresses: List[str] = []


class AlertsSettings(BaseModel):
    """
    Config schema for Phase 5 alerting. Defines WHAT should trigger an
    alert and WHERE it should go; the actual send logic (calling the
    Telegram/Discord/SMTP APIs) is product work for later — this exists
    now so that work has settings to build against instead of hardcoded
    thresholds buried in sender code.
    """
    enabled: bool = False
    triggers: AlertTriggerSettings = AlertTriggerSettings()
    telegram: TelegramAlertSettings = TelegramAlertSettings()
    discord: DiscordAlertSettings = DiscordAlertSettings()
    email: EmailAlertSettings = EmailAlertSettings()


class CircuitBreakerSettings(BaseModel):
    failure_threshold: int = 5
    cooldown_seconds: float = 60.0


class CacheTTLSettings(BaseModel):
    ticker: float = 20
    ohlcv_15m: float = 60
    ohlcv_1h: float = 180
    ohlcv_4h: float = 600
    ohlcv_1d: float = 1800
    open_interest: float = 300
    derivatives_snapshot: float = 180
    social: float = 300
    whale: float = 120
    dexscreener: float = 30


class ResilienceSettings(BaseModel):
    cache_ttl_seconds: CacheTTLSettings = CacheTTLSettings()
    circuit_breakers: Dict[str, CircuitBreakerSettings] = {
        "bybit": CircuitBreakerSettings(failure_threshold=5, cooldown_seconds=60),
        "hyperliquid": CircuitBreakerSettings(failure_threshold=5, cooldown_seconds=60),
        "coinglass": CircuitBreakerSettings(failure_threshold=4, cooldown_seconds=120),
        "lunarcrush": CircuitBreakerSettings(failure_threshold=4, cooldown_seconds=120),
        "whale_alert": CircuitBreakerSettings(failure_threshold=4, cooldown_seconds=120),
        "dexscreener": CircuitBreakerSettings(failure_threshold=5, cooldown_seconds=60),
    }


class AuthSettings(BaseModel):
    """
    Dashboard login gate. The `password` field lives here structurally so
    the auth section is visible and documented in settings.yaml, but the
    actual value should come from DASHBOARD_PASSWORD (env var) — same
    pattern as alerts.telegram.bot_token elsewhere in this file. Never
    put a real password directly in settings.yaml; that file is meant to
    be committed to version control.
    """
    enabled: bool = True
    password: Optional[str] = None


class DegenRadarSettings(BaseModel):
    """
    Separate from the main scanner by design — see degen_radar.py. Off by
    default; turning it on surfaces on-chain/memecoin discovery data
    alongside the main scanner, never blended into its score.
    """
    enabled: bool = False
    chain_id: str = "solana"
    min_liquidity_usd_to_show: float = 5000


class ProviderToggleSettings(BaseModel):
    lunarcrush_enabled: bool = True
    coinglass_enabled: bool = True
    whale_alert_enabled: bool = True


class LoggingSettings(BaseModel):
    level: str = "INFO"
    format: str = "text"

    @model_validator(mode="after")
    def _validate(self):
        if self.level.upper() not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ValueError(f"logging.level must be one of DEBUG/INFO/WARNING/ERROR/CRITICAL, got {self.level}")
        if self.format not in ("text", "json"):
            raise ValueError(f"logging.format must be 'text' or 'json', got {self.format}")
        return self


class ApiSettings(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    cors_origins: List[str] = ["*"]


class MemeCircuitBreakerSettings(BaseModel):
    failure_threshold: int = 4
    cooldown_seconds: float = 120


class MemeResilienceSettings(BaseModel):
    """Separate from the main scanner's `resilience:` section — the meme
    scanner's providers (RugCheck, GoPlus, DexScreener-for-memes) have
    different rate limits and reliability characteristics than the main
    scanner's exchange APIs, so they get their own tuning knobs rather
    than sharing thresholds that don't fit either use case well."""
    rugcheck_cache_ttl_seconds: float = 120
    goplus_cache_ttl_seconds: float = 120
    dexscreener_cache_ttl_seconds: float = 30
    rugcheck_breaker: MemeCircuitBreakerSettings = MemeCircuitBreakerSettings(failure_threshold=4, cooldown_seconds=120)
    goplus_breaker: MemeCircuitBreakerSettings = MemeCircuitBreakerSettings(failure_threshold=4, cooldown_seconds=120)
    dexscreener_breaker: MemeCircuitBreakerSettings = MemeCircuitBreakerSettings(failure_threshold=5, cooldown_seconds=60)


class MemeDiscoverySettings(BaseModel):
    """How candidate tokens are found in the first place — the meme
    scanner has no fixed universe the way the main scanner does (there's
    no equivalent of "BTC, ETH, SOL..." for tokens that didn't exist an
    hour ago), so discovery is itself a real design surface, not just a
    config list."""
    use_dexscreener_boosts: bool = True   # pull DexScreener's real-time boosted/trending feed
    watchlist: List[str] = []              # specific token addresses to always check, regardless of discovery feed
    max_candidates_per_scan: int = 30       # cap — discovery feeds can return a lot, and every candidate costs a RugCheck+GoPlus call


class MemeModeThresholdSettings(BaseModel):
    """One mode's full Safety-gate + age-window definition — mirrors
    meme_scoring_engine.py::ModeThresholds exactly, so
    Settings.to_meme_engine_config() is a direct field-for-field mapping,
    not a lossy translation."""
    min_liquidity_usd: float
    max_tax_pct: float
    min_lp_locked_pct: float
    max_top10_pct: float
    max_dev_pct: float
    min_holders: int
    max_rugcheck_score: float
    age_min_minutes: float
    age_max_minutes: float


class MemeModeWeightSettings(BaseModel):
    hype: float
    onchain: float
    momentum: float


class MemeCautionMarginSettings(BaseModel):
    liquidity_multiplier: float = 1.5
    top10_holder_fraction: float = 0.85
    dev_wallet_fraction: float = 0.75
    rugcheck_score_fraction: float = 0.7


class MemeHypeFormulaSettings(BaseModel):
    """Hype is the strongest signal in the engine (up to 45% weight) — its
    internal formula constants get their own settings section rather than
    being buried as literals in code, since this is the highest-leverage
    thing to tune without a redeploy."""
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


# Three named presets — Sniper / Early Momentum / Confirmed Runner — as
# concrete, documented starting values. These are the DEFAULTS baked into
# the Settings model; settings.yaml's meme_scanner.thresholds/weights
# override them, but if that section were ever omitted from the YAML,
# these are what the system falls back to (matching
# meme_scoring_engine.py's own DEFAULT_MODE_THRESHOLDS/DEFAULT_MODE_WEIGHTS
# exactly, so there's one source of truth for "what a fresh install gets").
DEFAULT_MEME_MODE_THRESHOLDS: Dict[str, MemeModeThresholdSettings] = {
    "sniper": MemeModeThresholdSettings(
        min_liquidity_usd=15_000, max_tax_pct=10, min_lp_locked_pct=70,
        max_top10_pct=30, max_dev_pct=8, min_holders=50, max_rugcheck_score=40,
        age_min_minutes=0, age_max_minutes=20,
    ),
    "early_momentum": MemeModeThresholdSettings(
        min_liquidity_usd=20_000, max_tax_pct=10, min_lp_locked_pct=80,
        max_top10_pct=28, max_dev_pct=6, min_holders=100, max_rugcheck_score=35,
        age_min_minutes=20, age_max_minutes=180,
    ),
    "confirmed_runner": MemeModeThresholdSettings(
        min_liquidity_usd=25_000, max_tax_pct=8, min_lp_locked_pct=90,
        max_top10_pct=25, max_dev_pct=5, min_holders=150, max_rugcheck_score=30,
        age_min_minutes=180, age_max_minutes=1440,
    ),
}

DEFAULT_MEME_MODE_WEIGHTS: Dict[str, MemeModeWeightSettings] = {
    "sniper": MemeModeWeightSettings(hype=0.45, onchain=0.25, momentum=0.30),
    "early_momentum": MemeModeWeightSettings(hype=0.40, onchain=0.32, momentum=0.28),
    "confirmed_runner": MemeModeWeightSettings(hype=0.38, onchain=0.34, momentum=0.28),
}


class MemeAlertsSettings(BaseModel):
    """
    'Only high-conviction + safety passed + rising hype' — all three are
    independent required conditions here, not a blended threshold. A
    token can't alert on a high score alone without an actual detected
    hype event; see meme_alerts.py::MemeAlertDispatcher.should_alert.
    """
    enabled: bool = False
    min_opportunity_score: float = 80.0     # stricter than min_opportunity_score_to_show — alerts should be pickier than the display filter
    require_pass_grade_only: bool = False     # if True, Caution-tier results never alert, even if not Fail
    cooldown_minutes: int = 120


class MemeScannerSettings(BaseModel):
    """
    Top-level meme scanner config. `mode` selects which of the three
    MEME_ARCHITECTURE.md modes (sniper/early_momentum/confirmed_runner)
    governs hard thresholds and pillar weights for this run — see
    meme_scoring_engine.py::Mode. Deliberately a single active mode per
    scan cycle rather than scoring every candidate under all three modes
    simultaneously: age already determines which mode a token even
    qualifies for (see MODE_AGE_WINDOWS_MINUTES), so running all three
    would mostly produce redundant N/A results for tokens outside a
    given mode's window.
    """
    enabled: bool = False
    mode: str = "early_momentum"   # "sniper" | "early_momentum" | "confirmed_runner"
    chain_id: str = "solana"
    min_opportunity_score_to_show: float = 60.0   # the "only high-quality candidates" filter
    show_caution_grade: bool = True                 # include Safety=Caution results, not just clean Pass
    db_path: str = "meme_scanner.db"
    discovery: MemeDiscoverySettings = MemeDiscoverySettings()
    resilience: MemeResilienceSettings = MemeResilienceSettings()
    alerts: MemeAlertsSettings = MemeAlertsSettings()

    # The actual adjustable knobs: liquidity minimums, holder concentration
    # limits, tax ceilings, and age windows — one full set per mode, keyed
    # by mode name. Edit these in settings.yaml to change scanner behavior
    # with zero code changes.
    thresholds: Dict[str, MemeModeThresholdSettings] = Field(default_factory=lambda: dict(DEFAULT_MEME_MODE_THRESHOLDS))
    weights: Dict[str, MemeModeWeightSettings] = Field(default_factory=lambda: dict(DEFAULT_MEME_MODE_WEIGHTS))
    caution_margin: MemeCautionMarginSettings = MemeCautionMarginSettings()
    hype_formula: MemeHypeFormulaSettings = MemeHypeFormulaSettings()


class StripeSettings(BaseModel):
    """
    Secret key and webhook secret are read from top-level env vars
    (STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET) via the same backfill
    pattern as Telegram/Discord/DASHBOARD_PASSWORD — never put real
    values in settings.yaml. Price IDs are safe to commit (they're not
    secrets, just identifiers for Products/Prices created in the Stripe
    Dashboard) but ARE required for checkout.session.completed handling
    to know which plan a payment corresponds to.
    """
    enabled: bool = False
    secret_key: Optional[str] = None
    webhook_secret: Optional[str] = None
    price_id_pro: Optional[str] = None
    price_id_elite: Optional[str] = None


class CryptoPaymentSettings(BaseModel):
    """NowPayments — accepts 150+ cryptocurrencies via a hosted invoice
    page, notifies via IPN (their term for webhook) on completion. API
    key and IPN secret are top-level env vars, same reasoning as Stripe."""
    enabled: bool = False
    api_key: Optional[str] = None
    ipn_secret: Optional[str] = None
    sandbox: bool = False
    price_usd_pro: float = 39.0
    price_usd_elite: float = 89.0


class Settings(BaseSettings):
    """
    Load order: config.yaml provides the base, then environment variables
    override individual fields (e.g. `WEIGHTS__STRENGTH=0.20` overrides
    `weights.strength`, `CACHE__REDIS_URL=redis://...` sets the cache URL).
    API keys are read directly as top-level env vars, never put those in
    config.yaml.
    """

    model_config = SettingsConfigDict(
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    weights: WeightsSettings = WeightsSettings()
    filters: FiltersSettings = FiltersSettings()
    timeframes: TimeframesSettings = TimeframesSettings()
    signal_bands: SignalBandsSettings = SignalBandsSettings()
    confidence_bands: ConfidenceBandsSettings = ConfidenceBandsSettings()
    regime: RegimeSettings = RegimeSettings()
    smart_view: SmartViewSettings = SmartViewSettings()
    exchange: ExchangeSettings = ExchangeSettings()
    universe: UniverseSettings = UniverseSettings()
    scheduler: SchedulerSettings = SchedulerSettings()
    storage: StorageSettings = StorageSettings()
    app_db_path: str = "app_users.db"   # Stage 1 foundation — users/plans/subscriptions, separate from scan-history storage
    app_base_url: str = "http://localhost:8501"   # this dashboard's own public URL — needed to build Stripe/crypto success+cancel redirect URLs
    stripe: StripeSettings = StripeSettings()
    crypto_payments: CryptoPaymentSettings = CryptoPaymentSettings()
    cache: CacheSettings = CacheSettings()
    alerts: AlertsSettings = AlertsSettings()
    resilience: ResilienceSettings = ResilienceSettings()
    providers: ProviderToggleSettings = ProviderToggleSettings()
    degen_radar: DegenRadarSettings = DegenRadarSettings()
    meme_scanner: MemeScannerSettings = MemeScannerSettings()
    auth: AuthSettings = AuthSettings()
    logging: LoggingSettings = LoggingSettings()
    api: ApiSettings = ApiSettings()
    sector_map: Dict[str, List[str]] = {
        "l1": ["BTC", "ETH", "SOL", "AVAX", "NEAR", "SUI", "APT", "ADA"],
        "l2": ["ARB", "OP", "MATIC", "STRK", "ZK"],
        "defi": ["UNI", "AAVE", "LINK", "MKR", "CRV", "LDO"],
        "ai": ["TAO", "FET", "RNDR", "AGIX", "WLD"],
        "memecoin": ["DOGE", "SHIB", "PEPE", "WIF", "BONK", "FLOKI"],
    }

    # Secrets — read directly from environment, never from settings.yaml
    lunarcrush_api_key: Optional[str] = None
    whale_alert_api_key: Optional[str] = None
    coinglass_api_key: Optional[str] = None
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    discord_webhook_url: Optional[str] = None
    dashboard_password: Optional[str] = None
    stripe_secret_key: Optional[str] = None
    stripe_webhook_secret: Optional[str] = None
    nowpayments_api_key: Optional[str] = None
    nowpayments_ipn_secret: Optional[str] = None

    @model_validator(mode="after")
    def _fill_alert_secrets(self):
        """
        Secrets can be set as simple top-level env vars (TELEGRAM_BOT_TOKEN)
        rather than the nested ALERTS__TELEGRAM__BOT_TOKEN form, since
        that's the more natural way to set a secret. This backfills them
        into the nested structure so consuming code always reads from
        `settings.alerts.telegram.bot_token` etc. — one place, regardless
        of how the value was actually supplied.
        """
        if self.telegram_bot_token and not self.alerts.telegram.bot_token:
            self.alerts.telegram.bot_token = self.telegram_bot_token
        if self.telegram_chat_id and not self.alerts.telegram.chat_id:
            self.alerts.telegram.chat_id = self.telegram_chat_id
        if self.discord_webhook_url and not self.alerts.discord.webhook_url:
            self.alerts.discord.webhook_url = self.discord_webhook_url
        if self.dashboard_password and not self.auth.password:
            self.auth.password = self.dashboard_password
        if self.stripe_secret_key and not self.stripe.secret_key:
            self.stripe.secret_key = self.stripe_secret_key
        if self.stripe_webhook_secret and not self.stripe.webhook_secret:
            self.stripe.webhook_secret = self.stripe_webhook_secret
        if self.nowpayments_api_key and not self.crypto_payments.api_key:
            self.crypto_payments.api_key = self.nowpayments_api_key
        if self.nowpayments_ipn_secret and not self.crypto_payments.ipn_secret:
            self.crypto_payments.ipn_secret = self.nowpayments_ipn_secret
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ):
        # Precedence, highest first: init kwargs > env vars > .env file > config.yaml > defaults
        yaml_source = YamlConfigSettingsSource(settings_cls, yaml_file=CONFIG_YAML_PATH)
        return (init_settings, env_settings, dotenv_settings, yaml_source, file_secret_settings)

    def to_cache_ttls(self) -> Dict[str, float]:
        """Maps settings.yaml's resilience.cache_ttl_seconds onto the flat
        {timeframe/name: seconds} dict ExchangeDataSource expects."""
        c = self.resilience.cache_ttl_seconds
        return {
            "ticker": c.ticker, "15m": c.ohlcv_15m, "1h": c.ohlcv_1h,
            "4h": c.ohlcv_4h, "1d": c.ohlcv_1d, "open_interest": c.open_interest,
        }

    def to_breaker_config(self) -> Dict[str, dict]:
        """Maps settings.yaml's resilience.circuit_breakers onto the dict
        shape MultiExchangeOIProvider expects."""
        return {
            name: {"failure_threshold": cb.failure_threshold, "cooldown_seconds": cb.cooldown_seconds}
            for name, cb in self.resilience.circuit_breakers.items()
        }

    def to_meme_mode(self):
        """Converts meme_scanner.mode (a plain string in YAML/env) to the
        Mode enum meme_scoring_engine.py actually uses. Raises clearly on
        a typo rather than silently falling back to a default mode with
        different risk thresholds than the person intended."""
        from .meme_scoring_engine import Mode
        try:
            return Mode(self.meme_scanner.mode)
        except ValueError:
            valid = [m.value for m in Mode]
            raise ValueError(
                f"meme_scanner.mode '{self.meme_scanner.mode}' is not valid — must be one of {valid}"
            )

    def to_meme_engine_config(self):
        """
        Converts settings.yaml's meme_scanner.thresholds/weights/
        hype_formula/caution_margin into the MemeEngineConfig object
        ScoringEngine actually consumes. Field-for-field mapping, no
        lossy translation — if a mode is missing from settings.yaml's
        thresholds/weights dicts, this raises rather than silently
        falling back to a different mode's numbers, since that's a
        real misconfiguration worth surfacing loudly.
        """
        from .meme_scoring_engine import Mode, ModeThresholds, CautionMarginConfig, HypeFormulaConfig, MemeEngineConfig

        mode_thresholds = {}
        mode_weights = {}
        for mode in Mode:
            key = mode.value
            if key not in self.meme_scanner.thresholds:
                raise ValueError(f"meme_scanner.thresholds is missing an entry for mode '{key}'")
            if key not in self.meme_scanner.weights:
                raise ValueError(f"meme_scanner.weights is missing an entry for mode '{key}'")
            t = self.meme_scanner.thresholds[key]
            mode_thresholds[mode] = ModeThresholds(
                min_liquidity_usd=t.min_liquidity_usd, max_tax_pct=t.max_tax_pct,
                min_lp_locked_pct=t.min_lp_locked_pct, max_top10_pct=t.max_top10_pct,
                max_dev_pct=t.max_dev_pct, min_holders=t.min_holders,
                max_rugcheck_score=t.max_rugcheck_score,
                age_min_minutes=t.age_min_minutes, age_max_minutes=t.age_max_minutes,
            )
            w = self.meme_scanner.weights[key]
            mode_weights[mode] = {"hype": w.hype, "onchain": w.onchain, "momentum": w.momentum}

        cm = self.meme_scanner.caution_margin
        caution_margin = CautionMarginConfig(
            liquidity_multiplier=cm.liquidity_multiplier, top10_holder_fraction=cm.top10_holder_fraction,
            dev_wallet_fraction=cm.dev_wallet_fraction, rugcheck_score_fraction=cm.rugcheck_score_fraction,
        )

        hf = self.meme_scanner.hype_formula
        hype_formula = HypeFormulaConfig(
            velocity_normalize_lo=hf.velocity_normalize_lo, velocity_normalize_hi=hf.velocity_normalize_hi,
            acceleration_normalize_lo=hf.acceleration_normalize_lo, acceleration_normalize_hi=hf.acceleration_normalize_hi,
            boost_normalize_hi=hf.boost_normalize_hi, kol_boost_cap=hf.kol_boost_cap,
            velocity_weight=hf.velocity_weight, acceleration_weight=hf.acceleration_weight,
            trending_weight=hf.trending_weight, social_presence_weight=hf.social_presence_weight,
        )

        return MemeEngineConfig(
            mode_thresholds=mode_thresholds, mode_weights=mode_weights,
            caution_margin=caution_margin, hype_formula=hype_formula,
        )


    def to_scanner_config(self) -> ScannerConfig:
        """Convert to the dataclass-based ScannerConfig every existing module already uses."""
        return ScannerConfig(
            weights=Weights(
                strength=self.weights.strength,
                oi_dynamics=self.weights.oi_dynamics,
                momentum=self.weights.momentum,
                social=self.weights.social,
            ),
            filters=QualityFilters(
                min_24h_volume_usd=self.filters.min_24h_volume_usd,
                min_market_cap_usd=self.filters.min_market_cap_usd,
                min_exchange_listings=self.filters.min_exchange_listings,
                max_bid_ask_spread_pct=self.filters.max_bid_ask_spread_pct,
                require_min_candles=self.filters.require_min_candles,
            ),
            timeframe_config=TimeframeConfig(
                timeframes=list(self.timeframes.list),
                timeframe_weights=dict(self.timeframes.weights),
                candles_per_timeframe=self.timeframes.candles_per_timeframe,
            ),
            signal_bands=SignalBands(
                strong_buy=self.signal_bands.strong_buy,
                buy=self.signal_bands.buy,
                neutral=self.signal_bands.neutral,
                caution=self.signal_bands.caution,
            ),
            confidence_bands=ConfidenceBands(
                high=self.confidence_bands.high,
                medium=self.confidence_bands.medium,
            ),
            regime_config=RegimeConfig(
                risk_on_threshold=self.regime.risk_on_threshold,
                risk_off_threshold=self.regime.risk_off_threshold,
                risk_off_dampener_points=self.regime.risk_off_dampener_points,
                dampen_above_score=self.regime.dampen_above_score,
                volatility_lookback_days=self.regime.volatility_lookback_days,
                volatility_normalize_lo=self.regime.volatility_normalize_lo,
                volatility_normalize_hi=self.regime.volatility_normalize_hi,
            ),
            sector_map=dict(self.sector_map),
            primary_exchange=self.exchange.primary,
            quote_currency=self.exchange.quote_currency,
            market_data_priority=list(self.exchange.market_data_priority),
            lunarcrush_api_key=self.lunarcrush_api_key,
            smart_view=SmartViewConfig(
                enabled=self.smart_view.enabled,
                super_strong=BucketThresholds(**self.smart_view.super_strong.model_dump()),
                strong=BucketThresholds(**self.smart_view.strong.model_dump()),
                building=BucketThresholds(**self.smart_view.building.model_dump()),
            ),
        )


def load_settings() -> Settings:
    """
    Entry point every script/module should use rather than constructing
    Settings() directly.

    Explicitly checks the file exists first: pydantic-settings'
    YamlConfigSettingsSource silently returns an empty config (falling
    back to field defaults) if the file is missing, rather than raising.
    That's the wrong behavior here — a missing settings.yaml should be a
    loud, clear error, not a silent switch to defaults that don't match
    what's actually committed (different weights, different universe,
    degen_radar/alerts toggled differently, etc., with no indication
    anything changed).
    """
    if not CONFIG_YAML_PATH.exists():
        raise FileNotFoundError(
            f"settings.yaml not found at {CONFIG_YAML_PATH}. "
            f"Expected it in the project root, one level up from opportunity_scanner/."
        )
    return Settings()
