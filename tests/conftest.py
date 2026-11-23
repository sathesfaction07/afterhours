"""Shared fixtures. The corpus is loaded once per session and treated as read-only."""

from __future__ import annotations

from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import holidays
import pytest

from sla_watch.calendars import (
    GULF_WEEK,
    WESTERN_WEEK,
    BusinessCalendar,
    holiday_set,
)
from sla_watch.measure import build_report
from sla_watch.tickets import group_by_region, load_tickets, resolve_regions

UTC = ZoneInfo("UTC")
LONDON = ZoneInfo("Europe/London")

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
"""Matches the corpus generator. An open ticket's elapsed time grows, so the instant is pinned."""

DATA = Path(__file__).resolve().parents[1] / "data" / "tickets.jsonl"


@pytest.fixture(scope="session")
def london() -> BusinessCalendar:
    """Monday-Friday 09:00-17:00. The calendar the arithmetic in the docs is quoted for."""
    return BusinessCalendar(
        region="GB", zone_name="Europe/London", subdivision="ENG", label="London"
    )


@pytest.fixture(scope="session")
def london_holidays(london: BusinessCalendar) -> frozenset:
    return holiday_set(london, (2026,))


@pytest.fixture(scope="session")
def tokyo() -> BusinessCalendar:
    """Open nine hours a day, so its derived threshold is 168/45 rather than 168/40."""
    return BusinessCalendar(
        region="JP", zone_name="Asia/Tokyo", opens=time(9), closes=time(18), label="Tokyo"
    )


@pytest.fixture(scope="session")
def cairo() -> BusinessCalendar:
    """Sunday-Thursday. Its weekend is Friday and Saturday."""
    return BusinessCalendar(
        region="EG", zone_name="Africa/Cairo", working_days=GULF_WEEK, label="Cairo"
    )


@pytest.fixture(scope="session")
def night_shift() -> BusinessCalendar:
    """A window covering the hours DST transitions are scheduled into.

    Not `22:00-06:00`, because an overnight window is rejected by the model. `00:00-08:00` covers
    01:00-03:00 local, which is where roughly 90% of transitions land, and that is enough to show
    the immunity of a daytime calendar is a property of the HOUR rather than of the code.
    """
    return BusinessCalendar(
        region="GB",
        zone_name="Europe/London",
        opens=time(0, 30),
        closes=time(8, 0),
        subdivision="ENG",
        label="night shift",
    )


@pytest.fixture(scope="session")
def always_open() -> BusinessCalendar:
    """A 24/7 rota. Every transition falls inside its window, so it has no immunity at all."""
    return BusinessCalendar(
        region="GB",
        zone_name="Europe/London",
        opens=time(0, 0, 1),
        closes=time(23, 59),
        working_days=frozenset(range(7)),
        subdivision="ENG",
        label="24/7",
    )


@pytest.fixture(scope="session")
def western_cairo() -> BusinessCalendar:
    """Cairo's zone with a Monday-Friday week -- the assumption, applied where it is wrong."""
    return BusinessCalendar(
        region="EG", zone_name="Africa/Cairo", working_days=WESTERN_WEEK, label="Cairo as Mon-Fri"
    )


@pytest.fixture(scope="session")
def corpus():
    tickets, problems = load_tickets(DATA)
    return tickets, problems


@pytest.fixture(scope="session")
def regions(corpus):
    tickets, _ = corpus
    return resolve_regions(tickets)


@pytest.fixture(scope="session")
def report(corpus, regions):
    tickets, problems = corpus
    return build_report(tickets, dict(regions), NOW, tuple(problems), holidays.__version__)


@pytest.fixture(scope="session")
def by_id(report):
    return {result.ticket_id: result for result in report.results}


@pytest.fixture(scope="session")
def gb_tickets(corpus):
    tickets, _ = corpus
    return group_by_region(tickets)["gb"]


def at(month: int, day: int, hour: int, minute: int = 0, zone=LONDON) -> datetime:
    """A London instant in 2026, for readable test setup."""
    return datetime(2026, month, day, hour, minute, tzinfo=zone)


FRIDAY_CLOSE = at(3, 6, 17, 0)
"""Friday 6 March 2026 at 17:00 -- the start of a 64-hour weekend gap."""

MONDAY_OPEN = at(3, 9, 9, 0)
"""The instant the clock next runs after FRIDAY_CLOSE."""
