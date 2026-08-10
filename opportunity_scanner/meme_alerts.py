"""
Meme alert dispatcher — "only high-conviction + safety passed + rising
hype", per the Phase 5 brief, taken literally as three independent
AND conditions, not three factors that can compensate for each other.
A token with an explosive hype event but Safety=Fail never alerts. A
token with Safety=Pass and a high score but flat/declining hype never
alerts either — a high score alone isn't "rising," it's just "good
right now," and this is specifically meant to be picky.

Reuses TelegramSender/DiscordSender from alerts.py rather than
duplicating them — they were already generic (just take a message
string), no meme-specific coupling to undo.
"""

from __future__ import annotations
import logging
import time
from typing import List, Optional

from .alerts import TelegramSender, DiscordSender
from .meme_scoring_engine import FinalMemeResult, HypeEvent

logger = logging.getLogger("opportunity_scanner.meme_alerts")


def format_meme_alert_message(result: FinalMemeResult, hype_events: List[HypeEvent]) -> str:
    lines = [
        f"🚀 {result.symbol} — High-conviction meme signal",
        f"Score: {result.opportunity_score:.1f} · Safety: {result.safety.grade} · Hype: {result.hype_level} · Confidence: {result.confidence:.0f}",
        "",
        f"Thesis: {result.thesis}",
        "",
        "Hype events:",
    ]
    for e in hype_events[:3]:
        lines.append(f"  🔥 {e.label}")
    if result.risk_flags:
        lines.append("")
        lines.append("Flags:")
        for f in result.risk_flags[:3]:
            lines.append(f"  ⚠ {f.label}")
    lines.append("")
    lines.append(f"Token: {result.token_address}")
    return "\n".join(lines)


class MemeAlertDispatcher:
    def __init__(
        self,
        telegram_bot_token: Optional[str] = None,
        telegram_chat_id: Optional[str] = None,
        discord_webhook_url: Optional[str] = None,
        min_opportunity_score: float = 80.0,
        require_pass_grade_only: bool = False,   # if True, Caution-tier is excluded even though it's not Fail
        cooldown_minutes: int = 120,
    ):
        self.min_opportunity_score = min_opportunity_score
        self.require_pass_grade_only = require_pass_grade_only
        self.cooldown_minutes = cooldown_minutes
        self._telegram = TelegramSender(telegram_bot_token, telegram_chat_id) if (telegram_bot_token and telegram_chat_id) else None
        self._discord = DiscordSender(discord_webhook_url) if discord_webhook_url else None
        self._last_alerted: dict[str, float] = {}   # token_address -> monotonic time

    async def close(self):
        if self._telegram:
            await self._telegram.close()
        if self._discord:
            await self._discord.close()

    def should_alert(self, result: FinalMemeResult, hype_events: List[HypeEvent]) -> tuple[bool, Optional[str]]:
        """Returns (should_alert, reason_if_not) — the reason is for
        logging/debugging, not shown to the end user, but makes it easy
        to answer 'why didn't this alert?' without guessing."""
        if result.safety.grade == "Fail":
            return False, "safety failed"
        if self.require_pass_grade_only and result.safety.grade != "Pass":
            return False, "safety is Caution, not a clean Pass, and require_pass_grade_only is set"
        if result.opportunity_score is None or result.opportunity_score < self.min_opportunity_score:
            return False, f"score {result.opportunity_score} below {self.min_opportunity_score} threshold"
        if not hype_events:
            return False, "no rising-hype event detected — score alone isn't enough"

        if self._in_cooldown(result.token_address):
            return False, "in cooldown"

        return True, None

    def _in_cooldown(self, token_address: str) -> bool:
        last = self._last_alerted.get(token_address)
        if last is None:
            return False
        return (time.monotonic() - last) / 60 < self.cooldown_minutes

    def _mark_alerted(self, token_address: str):
        self._last_alerted[token_address] = time.monotonic()

    async def process_result(self, result: FinalMemeResult, hype_events: List[HypeEvent]) -> bool:
        should, reason = self.should_alert(result, hype_events)
        if not should:
            logger.debug(f"Not alerting for {result.symbol}: {reason}")
            return False

        message = format_meme_alert_message(result, hype_events)
        sent_any = False
        if self._telegram:
            sent_any = await self._telegram.send(message) or sent_any
        if self._discord:
            sent_any = await self._discord.send(message) or sent_any

        self._mark_alerted(result.token_address)
        logger.info(f"Meme alert dispatched for {result.symbol} (score={result.opportunity_score}, {len(hype_events)} hype events)")
        return sent_any
