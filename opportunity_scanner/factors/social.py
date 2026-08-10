"""
Pillar 4: Social Virality
----------------------------
Reads mindshare growth, not just current buzz level — a coin with 1000
mentions/day that's been flat at 1000 for a month is not "viral," a coin
that went from 50 to 500 is. Everything here compares against a recent
baseline (snap.social should include both a current snapshot and a short
time-series from LunarCrush) rather than scoring absolute levels.

Expects snap.social to be a dict shaped like:
{
  "galaxy_score": float,          # 0-100, LunarCrush's own composite health score
  "galaxy_score_previous": float, # LunarCrush's own direct prior-24h value
  "alt_rank": int,                # lower is better
  "alt_rank_previous": int,       # LunarCrush's own direct prior-24h value
  "social_dominance": float,      # % share of total social volume this coin commands
  "social_volume_24h": float,     # mention count, last 24h
  "social_volume_baseline": float,# mention count, trailing avg (e.g. prior 7d/day)
  "recent_volume_points": list,   # last few daily mention-volume readings, for spike detection
  "sentiment": float,              # 0-100, % positive
  "sentiment_prev": float,         # sentiment N days ago, for shift detection
  "interactions_24h": float,       # likes/replies/retweets etc — engagement depth
  "interactions_baseline": float,
}
Any missing field degrades gracefully to a neutral sub-score, not a crash.
"""

from __future__ import annotations
from typing import Optional
from ..models import MarketSnapshot, FactorResult


def _clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))


def _normalize(v: float, lo: float, hi: float) -> float:
    if hi == lo:
        return 50.0
    return _clamp((v - lo) / (hi - lo) * 100.0)


def _mention_velocity_score(social: dict) -> tuple[float, str]:
    current = social.get("social_volume_24h")
    baseline_7d = social.get("social_volume_baseline")       # existing field, treated as 7d
    baseline_30d = social.get("social_volume_baseline_30d")  # new in v2, optional
    if current is None or not baseline_7d:
        return 50.0, "No mention baseline — neutral default"

    vel_7d = (current / baseline_7d - 1.0) * 100.0
    if baseline_30d:
        vel_30d = (current / baseline_30d - 1.0) * 100.0
        blended = vel_7d * 0.6 + vel_30d * 0.4
        note = f"Mention volume {vel_7d:+.0f}% vs 7d baseline, {vel_30d:+.0f}% vs 30d baseline"
    else:
        blended = vel_7d
        note = f"Mention volume {vel_7d:+.0f}% vs 7d baseline (no 30d baseline available)"

    score = _normalize(blended, -50, 150)
    return score, note


def _engagement_quality_score(social: dict) -> tuple[float, str]:
    mentions = social.get("social_volume_24h")
    if not mentions:
        return 50.0, "No engagement data — neutral default"

    # Weighted engagement: replies/retweets signal more conviction than a like
    likes = social.get("likes_24h")
    replies = social.get("replies_24h")
    retweets = social.get("retweets_24h")
    quotes = social.get("quote_tweets_24h")
    interactions = social.get("interactions_24h")

    if any(v is not None for v in (likes, replies, retweets, quotes)):
        weighted = (likes or 0) * 1.0 + (replies or 0) * 2.5 + (retweets or 0) * 3.0 + (quotes or 0) * 2.0
        per_mention = weighted / mentions
        score = _normalize(per_mention, 0, 25)
        return score, f"Weighted engagement {per_mention:.1f} per mention (replies/retweets weighted higher)"

    if interactions is not None:
        # fall back to unweighted interactions-per-mention if the platform
        # doesn't break out engagement types
        per_mention = interactions / mentions
        score = _normalize(per_mention, 0, 20)
        return score, f"{per_mention:.1f} interactions per mention (unweighted — engagement type breakdown unavailable)"

    return 50.0, "No engagement data — neutral default"


def _sentiment_shift_score(social: dict) -> tuple[float, str]:
    current = social.get("sentiment")
    prev = social.get("sentiment_prev")
    if current is None:
        return 50.0, "No sentiment data — neutral default"
    if prev is None:
        # fall back to absolute sentiment level
        score = _normalize(current, 30, 80)
        return score, f"Sentiment {current:.0f}/100 (no prior baseline for shift)"
    shift = current - prev
    # reward positive shift more than absolute level — a move from 40->60 matters
    # more than sitting at 70 the whole time
    level_score = _normalize(current, 30, 80)
    shift_score = _normalize(shift, -20, 20)
    score = level_score * 0.4 + shift_score * 0.6
    return score, f"Sentiment {current:.0f}/100, shifted {shift:+.0f}pp"


def _mindshare_score(social: dict) -> tuple[float, str]:
    """
    Combines LunarCrush's own composite ranking (galaxy_score, alt_rank)
    with social_dominance (share of total social volume this coin
    commands) — three distinct "how much attention does this coin have,
    relative to everything else" signals blended together, not just one.

    Uses galaxy_score_previous/alt_rank_previous — LunarCrush's own
    direct prior-24h values — for the growth/trend read. This replaces a
    real bug: the previous version looked for galaxy_score_7d_ago/
    alt_rank_7d_ago, fields the data source never actually populated, so
    this growth sub-signal silently always fell through to "no baseline"
    for every coin, every scan.
    """
    galaxy = social.get("galaxy_score")
    alt_rank = social.get("alt_rank")
    dominance = social.get("social_dominance")
    galaxy_prev = social.get("galaxy_score_previous")
    alt_rank_prev = social.get("alt_rank_previous")

    parts = []
    if galaxy is not None:
        parts.append(_clamp(galaxy))
    if alt_rank is not None:
        parts.append(_normalize(300 - alt_rank, 0, 300))
    if dominance is not None:
        # social_dominance is a small percentage for most coins (BTC/ETH
        # might command 15-30%, most alts under 5%) — normalize against a
        # realistic range rather than 0-100, or every non-BTC/ETH coin
        # would score near zero on this component regardless of real standing
        parts.append(_normalize(dominance, 0, 10))
    if not parts:
        return 50.0, "No mindshare ranking data — neutral default"
    level_score = sum(parts) / len(parts)

    growth_score = None
    if galaxy is not None and galaxy_prev is not None:
        growth_score = _normalize(galaxy - galaxy_prev, -20, 20)
    elif alt_rank is not None and alt_rank_prev is not None:
        # rank improving (going down) = growth
        growth_score = _normalize(alt_rank_prev - alt_rank, -100, 100)

    dominance_note = f", social dominance {dominance:.1f}%" if dominance is not None else ""
    if growth_score is not None:
        score = level_score * 0.5 + growth_score * 0.5
        note = f"Galaxy Score {galaxy if galaxy is not None else 'n/a'}, AltRank {alt_rank if alt_rank is not None else 'n/a'}{dominance_note}, mindshare {'growing' if growth_score > 50 else 'shrinking'}"
    else:
        score = level_score
        note = f"Galaxy Score {galaxy if galaxy is not None else 'n/a'}, AltRank {alt_rank if alt_rank is not None else 'n/a'}{dominance_note} (no prior-period baseline for growth read)"

    return score, note


def _spike_detection(social: dict, velocity_score: float) -> tuple[bool, str]:
    """
    "Is attention accelerating RIGHT NOW" — distinct from velocity alone.
    Velocity says mentions are up vs baseline; spike asks whether the
    MOST RECENT daily readings are still climbing (still accelerating)
    rather than having already leveled off after an earlier jump. Needs
    both signals to agree: elevated velocity AND the last couple of
    recent daily points still trending upward — either alone is a
    weaker, noisier read than both together.
    """
    recent_points = social.get("recent_volume_points") or []
    if velocity_score < 70 or len(recent_points) < 2:
        return False, ""
    # Are the most recent daily points still climbing, not just elevated?
    still_climbing = all(b >= a for a, b in zip(recent_points, recent_points[1:]))
    if still_climbing:
        return True, f"Mention volume still climbing day-over-day across the last {len(recent_points)} readings, not just elevated"
    return False, ""


def _narrative_signal(velocity_score: float, sentiment_score: float, mindshare_score: float, is_spike: bool) -> str:
    """
    A single qualitative label summarizing the pillar for at-a-glance
    dashboard display — the "basic narrative strength signal" — derived
    from the sub-scores already computed, not a new independent metric.
    """
    if is_spike and sentiment_score >= 40:
        return "🔥 Heating"
    if velocity_score >= 65 and mindshare_score >= 55:
        return "📈 Building"
    if velocity_score <= 35 and mindshare_score <= 45:
        return "❄️ Cooling"
    return "Neutral"


def _kol_boost(social: dict) -> tuple[float, list[str]]:
    """
    Additive bonus (capped at +15) for tracked KOL/influencer activity —
    NOT part of the weighted blend, since a single influencer post
    shouldn't be able to single-handedly carry a coin to Strong Buy.
    Requires social['kol_mentions'] populated by the data source (needs a
    LunarCrush creators-tier subscription or a maintained KOL list —
    returns no boost if that data isn't wired up).
    """
    kol_mentions = social.get("kol_mentions")  # expected: list of {followers, weight} or a precomputed score
    if not kol_mentions:
        return 0.0, []
    raw_score = social.get("kol_score")
    if raw_score is None:
        return 0.0, []
    boost = min(_normalize(raw_score, 0, 100) * 0.15, 15.0)
    return boost, [f"KOL activity boost +{boost:.1f} (tracked influencers mentioning this coin)"]


def compute_social(snap: MarketSnapshot) -> FactorResult:
    if not snap.social:
        return FactorResult(
            name="social",
            score=50.0,
            reasons=["No social data source connected for this scan — pillar excluded"],
            available=False,
        )

    social = snap.social
    reasons: list[str] = []

    velocity_score, v_note = _mention_velocity_score(social)
    engagement_score, e_note = _engagement_quality_score(social)
    sentiment_score, s_note = _sentiment_shift_score(social)
    mindshare_score, m_note = _mindshare_score(social)
    kol_boost, kol_notes = _kol_boost(social)
    is_spike, spike_note = _spike_detection(social, velocity_score)
    narrative = _narrative_signal(velocity_score, sentiment_score, mindshare_score, is_spike)

    reasons.extend([v_note, e_note, s_note, m_note])
    reasons.extend(kol_notes)
    if spike_note:
        reasons.append(spike_note)

    # Mention velocity is the headline "going viral" signal; mindshare rank
    # anchors it so a single spam wave doesn't dominate
    composite = (
        velocity_score * 0.35
        + mindshare_score * 0.30
        + sentiment_score * 0.20
        + engagement_score * 0.15
    )
    composite = _clamp(composite + kol_boost)

    return FactorResult(
        name="social",
        score=round(_clamp(composite), 1),
        reasons=reasons,
        raw={
            "velocity_score": velocity_score, "engagement_score": engagement_score,
            "sentiment_score": sentiment_score, "mindshare_score": mindshare_score,
            "kol_boost": kol_boost, "is_spike": is_spike, "narrative_signal": narrative,
            "galaxy_score": social.get("galaxy_score"), "alt_rank": social.get("alt_rank"),
            "sentiment": social.get("sentiment"), "social_dominance": social.get("social_dominance"),
        },
        available=True,
    )
