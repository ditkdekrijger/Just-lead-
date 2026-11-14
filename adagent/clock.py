"""Centrale 'vandaag'-functie (in tijdzone van het bureau, te overschrijven voor simulaties)."""

from __future__ import annotations

import calendar
import datetime as dt
from zoneinfo import ZoneInfo

from .config import settings


def today() -> dt.date:
    return dt.datetime.now(ZoneInfo(settings.timezone)).date()


def days_in_month(day: dt.date) -> int:
    return calendar.monthrange(day.year, day.month)[1]


def month_start(day: dt.date) -> dt.date:
    return day.replace(day=1)
