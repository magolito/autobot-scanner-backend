"""
Alert dispatch — the part of the alerts config schema (settings.yaml's
`alerts:` section) that was previously just a schema with no sender
behind it. This is that sender.

Three pieces:
  - TelegramSender / DiscordSender: thin wrappers around each platform's
    API, each independently testable
  - format_alert_message: builds a short, explainable message from a
    ScanResult — reuses reasons_summary (the explainability layer) rather
    than inventing new copy
  - AlertDispatcher: the orchestrator — checks storage for signal changes
    and score jumps since the last dispatch cycle, applies the trigger
    rules (min confidence, cooldown) from settings, and sends through
    whichever channels are enabled

Cooldown is tracked in-memory, keyed by (base, signal) — a coin that
qualifies for a "Strong Buy" alert, then drops out and re-qualifies
within the cooldown window, is deliberately suppressed rather than
spamming the same call repeatedly. This resets if the process restarts,
which is an acceptable tradeoff for a single-instance deployment; a
multi-instance deployment sharing Redis (see cache.py) would want this
tracked there instead — noted as a known limitation, not silently wrong.
"""

from __future__ import annotations
import logging
import time
from typing import List, Optional
import httpx

from .models import ScanResult
from .settings import AlertsSettings
from .storage import ScanStorage

logger = logging.getLogger("opportunity_scanner.alerts")


class TelegramSender:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._http = httpx.AsyncClient(base_url=f"https://api.telegram.org/bot{bot_token}", timeout=10.0)

    async def close(self):
        await self._http.aclose()

    async def send(self, message: str) -> bool:
        try:
            resp = await self._http.post("/sendMessage", json={
                "chat_id": self.chat_id, "text": message, "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            })
            resp.raise_for_status()
            return True
        except Exception as e:  # noqa: BLE001
            logger.error(f"Telegram send failed: {e}")
            return False


class DiscordSender:
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url
        self._http = httpx.AsyncClient(timeout=10.0)

    async def close(self):
        await self._http.aclose()

    async def send(self, message: str) -> bool:
        try:
            resp = await self._http.post(self.webhook_url, json={"content": message})
            resp.raise_for_status()
            return True
        except Exception as e:  # noqa: BLE001
            logger.error(f"Discord send failed: {e}")
            return False


def format_alert_message(result: ScanResult, reason_label: str) -> str:
    """
    Reuses the explainability layer (reasons_summary) rather than
    inventing separate alert copy — the alert should say the same thing
    the dashboard would say for why this coin qualified.
    """
    lines = [
        f"🚨 {result.base} — {reason_label}",
        f"Score: {result.composite_score:.1f} ({result.signal}) · Confidence: {result.confidence:.0f} ({result.confidence_label})",
        f"Risk tier: {result.risk_tier}",
        "",
    ]
    for r in result.reasons_summary[:3]:
        lines.append(f"• {r}")
    if result.regime_adjustment_note:
        lines.append(f"⚠ {result.regime_adjustment_note}")
    return "\n".join(lines)


class AlertDispatcher:
    def __init__(self, settings: AlertsSettings, storage: ScanStorage):
        self.settings = settings
        self.storage = storage
        self._telegram: Optional[TelegramSender] = None
        self._discord: Optional[DiscordSender] = None
        self._last_alerted: dict[tuple[str, str], float] = {}  # (base, signal) -> monotonic time

        if settings.telegram.enabled and settings.telegram.bot_token and settings.telegram.chat_id:
            self._telegram = TelegramSender(settings.telegram.bot_token, settings.telegram.chat_id)
        if settings.discord.enabled and settings.discord.webhook_url:
            self._discord = DiscordSender(settings.discord.webhook_url)

    async def close(self):
        if self._telegram:
            await self._telegram.close()
        if self._discord:
            await self._discord.close()

    def _cooldown_key(self, base: str, signal: str) -> tuple[str, str]:
        return (base, signal)

    def _in_cooldown(self, base: str, signal: str) -> bool:
        key = self._cooldown_key(base, signal)
        last = self._last_alerted.get(key)
        if last is None:
            return False
        elapsed_minutes = (time.monotonic() - last) / 60
        return elapsed_minutes < self.settings.triggers.cooldown_minutes

    def _mark_alerted(self, base: str, signal: str):
        self._last_alerted[self._cooldown_key(base, signal)] = time.monotonic()

    async def _send(self, message: str) -> dict:
        sent = {"telegram": False, "discord": False}
        if self._telegram:
            sent["telegram"] = await self._telegram.send(message)
        if self._discord:
            sent["discord"] = await self._discord.send(message)
        return sent

    async def process_results(self, results: List[ScanResult]) -> List[dict]:
        """
        Call this once per scan cycle with the fresh batch of ScanResults.
        Checks each against the trigger rules directly (no extra storage
        round-trip needed for signal-change detection when we already have
        the fresh result in hand — we just need to know if THIS result
        qualifies and isn't in cooldown). Score-jump detection still needs
        the storage comparison since it requires the prior value.
        """
        if not self.settings.enabled:
            return []

        dispatched = []
        results_by_base = {r.base: r for r in results}

        for result in results:
            if result.confidence < self.settings.triggers.min_confidence:
                continue

            reason_label = None
            if result.signal in self.settings.triggers.on_signal_change_to:
                reason_label = f"New signal: {result.signal}"

            if reason_label and not self._in_cooldown(result.base, result.signal):
                message = format_alert_message(result, reason_label)
                send_status = await self._send(message)
                self._mark_alerted(result.base, result.signal)
                dispatched.append({"base": result.base, "reason": reason_label, "sent": send_status})
                logger.info(f"Alert dispatched for {result.base}: {reason_label} -> {send_status}")

        # Score jumps — needs the storage comparison against the prior scan
        from datetime import datetime, timedelta, timezone
        since = (datetime.now(timezone.utc) - timedelta(minutes=self.settings.triggers.cooldown_minutes)).isoformat()
        jumps = await self.storage.get_score_jumps(since, threshold=self.settings.triggers.score_jump_threshold)
        for jump in jumps:
            base = jump["base"]
            result = results_by_base.get(base)
            if result is None or result.confidence < self.settings.triggers.min_confidence:
                continue
            jump_signal_key = f"jump:{jump['score_delta']:+.0f}"
            if self._in_cooldown(base, jump_signal_key):
                continue
            direction = "jumped" if jump["score_delta"] > 0 else "dropped"
            reason_label = f"Score {direction} {abs(jump['score_delta']):.1f}pts in one cycle"
            message = format_alert_message(result, reason_label)
            send_status = await self._send(message)
            self._mark_alerted(base, jump_signal_key)
            dispatched.append({"base": base, "reason": reason_label, "sent": send_status})
            logger.info(f"Alert dispatched for {base}: {reason_label} -> {send_status}")

        return dispatched
