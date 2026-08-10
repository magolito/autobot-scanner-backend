"""
Hype event detection — the actual "sudden mention spikes, KOL activity,
trending jumps" requirement from the Phase 5 brief.

The key distinction from the Hype pillar's normal scoring: pillar scoring
reads CURRENT state (is mention velocity high right now?). Hype events
read CHANGE (did something just happen that wasn't true a moment ago?).
A token sitting at high-but-stable velocity for hours isn't a hype
"event" — a token that just crossed from quiet to loud is. This needs
the previous scan from meme_storage.py; there's no way to detect a
"sudden" anything from a single snapshot.
"""

from __future__ import annotations
from typing import List, Optional

from .meme_scoring_engine import MemeCoinMetrics, HypeEvent


def detect_hype_events(current: MemeCoinMetrics, previous_scan: Optional[dict]) -> List[HypeEvent]:
    """
    `previous_scan` is a row dict from MemeScanStorage.get_previous_scan_sync
    (or None if this is the token's first-ever scan — no prior state means
    no events, not a crash). Every check here is a DELTA, not a threshold
    on the current value alone — that's what makes it "event detection"
    rather than a restatement of the Hype pillar's own scoring.
    """
    if previous_scan is None:
        return []

    events: List[HypeEvent] = []

    prev_velocity = previous_scan.get("mention_velocity_ratio")
    if current.mention_velocity_ratio is not None and prev_velocity is not None and prev_velocity > 0:
        velocity_jump = current.mention_velocity_ratio / prev_velocity
        if velocity_jump >= 3.0:
            events.append(HypeEvent(
                label=f"Mention velocity jumped {velocity_jump:.1f}x since last scan ({prev_velocity:.1f}x → {current.mention_velocity_ratio:.1f}x baseline)",
                severity="explosive",
            ))
        elif velocity_jump >= 1.8:
            events.append(HypeEvent(
                label=f"Mention velocity rising ({prev_velocity:.1f}x → {current.mention_velocity_ratio:.1f}x baseline)",
                severity="notable",
            ))

    prev_boosted = bool(previous_scan.get("dex_boosted"))
    if current.dex_boosted and not prev_boosted:
        events.append(HypeEvent(label="Newly boosted/trending on DexScreener (wasn't before)", severity="notable"))

    prev_kol = previous_scan.get("kol_score")
    if current.kol_score is not None:
        if prev_kol is None and current.kol_score > 50:
            events.append(HypeEvent(label="New KOL/influencer activity detected", severity="notable"))
        elif prev_kol is not None and current.kol_score - prev_kol >= 25:
            events.append(HypeEvent(label=f"KOL activity score jumped ({prev_kol:.0f} → {current.kol_score:.0f})", severity="explosive"))

    prev_hype_score = previous_scan.get("hype_score")
    # (current hype_score isn't known yet at detection time in the normal
    # call order — see meme_main.py's integration, which calls this AFTER
    # scoring and passes the fresh hype_score in separately when needed.
    # Kept as a documented seam rather than silently ignored.)

    return events


def detect_hype_score_jump(current_hype_score: float, previous_scan: Optional[dict], threshold: float = 20.0) -> Optional[HypeEvent]:
    """
    Separate from detect_hype_events because it needs the freshly-computed
    hype pillar score, which only exists after ScoringEngine.score() has
    run — this is meant to be called right after, not folded into the
    pre-scoring detection pass above.
    """
    if previous_scan is None:
        return None
    prev_score = previous_scan.get("hype_score")
    if prev_score is None:
        return None
    delta = current_hype_score - prev_score
    if delta >= threshold:
        return HypeEvent(label=f"Hype score jumped {delta:+.0f}pts since last scan ({prev_score:.0f} → {current_hype_score:.0f})", severity="explosive" if delta >= 35 else "notable")
    return None
