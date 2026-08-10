"""
Alert dispatcher test — mocked senders (no live Telegram/Discord calls),
real SQLite storage, no network needed.

Checks:
  1. A qualifying signal (in on_signal_change_to, confidence above minimum)
     triggers a send
  2. A non-qualifying signal (e.g. "Neutral", not in the trigger list)
     does NOT trigger a send
  3. Low confidence suppresses an otherwise-qualifying alert
  4. Cooldown suppresses a repeat alert for the same coin+signal within
     the window
  5. A large score jump triggers a send even without a signal-grade change
  6. format_alert_message produces a real, readable message using the
     actual reasons_summary (explainability reuse)
"""

from __future__ import annotations
import asyncio
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opportunity_scanner.alerts import AlertDispatcher, format_alert_message
from opportunity_scanner.settings import AlertsSettings, AlertTriggerSettings, TelegramAlertSettings
from opportunity_scanner.storage import ScanStorage
from opportunity_scanner.models import ScanResult, FactorResult


def make_result(base, score, signal, confidence=80.0) -> ScanResult:
    factors = {k: FactorResult(name=k, score=score, reasons=[f"{k} reason"]) for k in ["strength", "oi_dynamics", "momentum", "social"]}
    return ScanResult(
        symbol=f"{base}/USDT", base=base, price=100.0, composite_score=score,
        confidence=confidence, confidence_label="High" if confidence >= 75 else "Medium",
        signal=signal, factors=factors, weights_used={"strength": 0.25, "oi_dynamics": 0.25, "momentum": 0.25, "social": 0.25},
        reasons_summary=[f"[momentum, weight 25%] Strong uptrend detected for {base}", f"[oi_dynamics, weight 25%] OI confirming for {base}"],
        risk_tier="core", passed_filters=True,
    )


class FakeSender:
    """Records calls instead of hitting a real API."""
    def __init__(self):
        self.sent_messages = []

    async def send(self, message: str) -> bool:
        self.sent_messages.append(message)
        return True

    async def close(self):
        pass


async def main():
    db_path = "/tmp/test_alerts.db"
    if os.path.exists(db_path):
        os.remove(db_path)
    storage = ScanStorage(db_path)

    settings = AlertsSettings(
        enabled=True,
        triggers=AlertTriggerSettings(on_signal_change_to=["Strong Buy", "Buy"], min_confidence=60.0, cooldown_minutes=60, score_jump_threshold=15.0),
        telegram=TelegramAlertSettings(enabled=True, bot_token="fake", chat_id="fake"),
    )
    dispatcher = AlertDispatcher(settings, storage)
    fake_sender = FakeSender()
    dispatcher._telegram = fake_sender  # inject fake, skip real HTTP entirely

    # 1. Qualifying signal triggers a send
    strong_btc = make_result("BTC", 85.0, "Strong Buy", confidence=80.0)
    await storage.save_scan_result(strong_btc)
    dispatched = await dispatcher.process_results([strong_btc])
    assert len(dispatched) == 1, f"Expected 1 dispatch, got {dispatched}"
    assert len(fake_sender.sent_messages) == 1
    print("1. Qualifying signal (Strong Buy, high confidence) triggers a send: OK")

    # 2. Non-qualifying signal does NOT trigger
    neutral_eth = make_result("ETH", 50.0, "Neutral", confidence=80.0)
    dispatched2 = await dispatcher.process_results([neutral_eth])
    assert len(dispatched2) == 0, f"Expected 0 dispatches for Neutral signal, got {dispatched2}"
    print("2. Non-qualifying signal (Neutral) does not trigger: OK")

    # 3. Low confidence suppresses an otherwise-qualifying alert
    low_conf_sol = make_result("SOL", 82.0, "Strong Buy", confidence=40.0)
    dispatched3 = await dispatcher.process_results([low_conf_sol])
    assert len(dispatched3) == 0, f"Expected 0 dispatches for low confidence, got {dispatched3}"
    print("3. Low confidence suppresses alert: OK")

    # 4. Cooldown suppresses a repeat for the same coin+signal
    dispatched4 = await dispatcher.process_results([strong_btc])  # same BTC Strong Buy again
    assert len(dispatched4) == 0, f"Expected cooldown to suppress repeat, got {dispatched4}"
    assert len(fake_sender.sent_messages) == 1, "Expected no new message sent during cooldown"
    print("4. Cooldown suppresses repeat alert for same coin+signal: OK")

    # 5. Score jump triggers even without signal-grade change
    await storage.save_scan_result(make_result("AVAX", 40.0, "Caution", confidence=80.0))
    await asyncio.sleep(0.01)
    jumped_avax = make_result("AVAX", 58.0, "Neutral", confidence=80.0)  # +18, still "Neutral" band, no grade-boundary alert
    await storage.save_scan_result(jumped_avax)
    dispatched5 = await dispatcher.process_results([jumped_avax])
    assert len(dispatched5) == 1, f"Expected 1 score-jump dispatch, got {dispatched5}"
    assert "jumped" in dispatched5[0]["reason"]
    print(f"5. Score jump (+18pts, no grade change) triggers a send: {dispatched5[0]['reason']}")

    # 6. Message formatting reuses explainability
    msg = format_alert_message(strong_btc, "New signal: Strong Buy")
    assert "BTC" in msg
    assert "Strong Buy" in msg
    assert "Strong uptrend detected for BTC" in msg, "Expected reasons_summary content in the alert message"
    print(f"6. format_alert_message reuses reasons_summary:\n---\n{msg}\n---")

    os.remove(db_path)
    print("\n✅ Alert dispatcher test passed: trigger matching, confidence gating, cooldown enforcement, score-jump detection, and explainability reuse all verified.")


if __name__ == "__main__":
    asyncio.run(main())
