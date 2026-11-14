"""Dagelijkse planning (APScheduler, draait mee in het webproces)."""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import settings
from .db import get_sessionmaker
from .runner import run_all

log = logging.getLogger(__name__)
_scheduler: BackgroundScheduler | None = None


def daily_job() -> None:
    log.info("Dagelijkse agent-run gestart")
    ids = run_all(get_sessionmaker())
    log.info("Dagelijkse agent-run klaar: %d klanten", len(ids))


def start() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = BackgroundScheduler(timezone=settings.timezone)
        _scheduler.add_job(
            daily_job,
            CronTrigger(hour=settings.daily_hour, minute=0, timezone=settings.timezone),
            id="daily",
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=6 * 3600,
        )
        _scheduler.start()
        log.info("Planner actief: dagelijks om %02d:00 (%s)", settings.daily_hour, settings.timezone)
    return _scheduler


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
