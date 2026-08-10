"""
Scheduled polling.

Runs `OpportunityScanner.scan_many()` on an interval and persists results
via `ScanStorage`. This is the piece Phase 5 alerts depend on — you can't
notify someone "this coin just flipped to Strong Buy" without something
running in the background comparing this scan to the last one.

This module only handles the polling + storage + signal-change detection.
Actually sending a Telegram/Discord/email notification is Phase 5 product
work — `get_recent_signal_changes()` here is what that layer would call.
"""

from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .scanner import OpportunityScanner
from .storage import ScanStorage
from .config import ScannerConfig
from .alerts import AlertDispatcher
from .settings import AlertsSettings

logger = logging.getLogger("opportunity_scanner.scheduler")


class ScannerPoller:
    def __init__(
        self,
        config: Optional[ScannerConfig] = None,
        universe: Optional[List[str]] = None,
        db_path: str = "opportunity_scanner.db",
        interval_minutes: int = 15,
        alerts_settings: Optional[AlertsSettings] = None,
        blacklist: Optional[List[str]] = None,
        whitelist: Optional[List[str]] = None,
    ):
        self.config = config or ScannerConfig()
        self.universe = universe or [
            "BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "AVAX", "LINK", "DOT",
        ]
        self.blacklist = blacklist or []
        self.whitelist = whitelist or []
        self.storage = ScanStorage(db_path)
        self.scanner = OpportunityScanner(self.config)
        self.interval_minutes = interval_minutes
        self._scheduler = AsyncIOScheduler()
        self._last_run_at: Optional[datetime] = None
        self.dispatcher = AlertDispatcher(alerts_settings, self.storage) if alerts_settings else None

    async def run_once(self):
        logger.info(f"Running scan cycle over {len(self.universe)} coins...")
        try:
            results = await self.scanner.scan_many(self.universe, blacklist=self.blacklist, whitelist=self.whitelist)
        except Exception as e:  # noqa: BLE001 — a failed cycle shouldn't kill the scheduler
            logger.error(f"Scan cycle failed: {e}")
            return

        await self.storage.save_scan_results(results)
        self._last_run_at = datetime.now(timezone.utc)
        logger.info(f"Scan cycle complete: {len(results)} coins scored and persisted.")

        if self.dispatcher is not None:
            try:
                dispatched = await self.dispatcher.process_results(results)
                if dispatched:
                    logger.info(f"Alerts dispatched: {len(dispatched)}")
            except Exception as e:  # noqa: BLE001 — an alert failure shouldn't break the scan cycle
                logger.error(f"Alert dispatch failed: {e}")

    async def get_recent_signal_changes(self, lookback_minutes: int = 60) -> List[dict]:
        since = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
        return await self.storage.get_signal_changes(since.isoformat())

    def start(self):
        self._scheduler.add_job(
            self.run_once,
            "interval",
            minutes=self.interval_minutes,
            next_run_time=datetime.now(timezone.utc),  # run immediately on startup, then on interval
            id="scan_cycle",
            max_instances=1,  # never overlap a slow cycle with the next scheduled one
        )
        self._scheduler.start()
        logger.info(f"Scanner poller started — running every {self.interval_minutes} minutes.")

    async def stop(self):
        self._scheduler.shutdown(wait=False)
        await self.scanner.close()
        if self.dispatcher is not None:
            await self.dispatcher.close()


async def run_standalone(interval_minutes: int = 15):
    """
    Run the poller as a standalone process (separate from the API server).
    In production you'd run this as its own worker/dyno rather than inside
    the request-handling FastAPI process, so a slow scan cycle never blocks
    an incoming API request.
    """
    logging.basicConfig(level=logging.INFO)
    from .settings import load_settings
    settings = load_settings()
    poller = ScannerPoller(
        config=settings.to_scanner_config(),
        universe=settings.universe.default,
        db_path=settings.storage.db_path,
        interval_minutes=interval_minutes or settings.scheduler.interval_minutes,
        alerts_settings=settings.alerts if settings.alerts.enabled else None,
        blacklist=settings.universe.blacklist,
        whitelist=settings.universe.whitelist,
    )
    poller.start()
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        await poller.stop()


if __name__ == "__main__":
    asyncio.run(run_standalone())
