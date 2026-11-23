"""Loading tickets and resolving the calendar each one is measured against."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import time
from pathlib import Path

from pydantic import ValidationError

from .calendars import (
    GULF_WEEK,
    WESTERN_WEEK,
    BusinessCalendar,
    CalendarCoverage,
    check_coverage,
    holiday_set,
    zone_exists,
)
from .models import Ticket

REGISTRY: dict[str, BusinessCalendar] = {
    "gb": BusinessCalendar(
        region="GB",
        zone_name="Europe/London",
        subdivision="ENG",
        label="London (England)",
    ),
    "us": BusinessCalendar(
        region="US",
        zone_name="America/New_York",
        subdivision="NY",
        label="New York",
    ),
    "jp": BusinessCalendar(
        region="JP",
        zone_name="Asia/Tokyo",
        opens=time(9),
        closes=time(18),
        label="Tokyo",
    ),
    "de": BusinessCalendar(
        region="DE",
        zone_name="Europe/Berlin",
        subdivision="BE",
        label="Berlin",
    ),
    # Sunday-Thursday. Included because a Saturday-Sunday weekend is an assumption, not a fact, and
    # this is the week that shares a working day with the Sunday DST transitions.
    "eg": BusinessCalendar(
        region="EG",
        zone_name="Africa/Cairo",
        working_days=GULF_WEEK,
        label="Cairo (Sun-Thu)",
    ),
    "il": BusinessCalendar(
        region="IL",
        zone_name="Asia/Jerusalem",
        working_days=GULF_WEEK,
        label="Tel Aviv (Sun-Thu)",
    ),
    # Deliberately left without a subdivision, so the report shows what the national default costs:
    # 7 holidays where England has 9, Scotland 11 and Northern Ireland 12.
    "uk_national": BusinessCalendar(
        region="GB",
        zone_name="Europe/London",
        label="UK (no subdivision chosen)",
    ),
    # Same Berlin calendar, used by rows carried over from a pre-1991 system. The holiday library's
    # German data starts at reunification, so these years resolve to an EMPTY holiday set -- which
    # reads as a seven-day working week rather than as missing data. Isolated in its own region key
    # because coverage is checked per region: mixing these rows into `de` would drag its whole
    # coverage check back to 1989 and refuse every modern Berlin ticket too.
    "de_archive": BusinessCalendar(
        region="DE",
        zone_name="Europe/Berlin",
        subdivision="BE",
        label="Berlin (migrated archive)",
    ),
    # A region the holiday library does not implement. Kept in the registry rather than omitted,
    # because the honest outcome is a refusal and refusals need somewhere to come from.
    "xx": BusinessCalendar(
        region="XX",
        zone_name="Etc/UTC",
        working_days=WESTERN_WEEK,
        label="unmapped region",
    ),
}


def calendar_for(region: str) -> BusinessCalendar | None:
    return REGISTRY.get(region.strip().lower())


def load_tickets(path: Path) -> tuple[list[Ticket], list[str]]:
    """Read a JSON Lines ticket file, collecting problems rather than raising on the first.

    A row missing its IDENTITY is reported and dropped, because there is nothing to attribute a
    verdict to. A row missing its TIMESTAMPS still loads, because deciding what to do about a
    missing creation instant is the job rather than an obstacle to it.
    """
    tickets: list[Ticket] = []
    problems: list[str] = []
    if not path.exists():
        return tickets, [f"{path}: not found"]

    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as err:
            problems.append(f"line {number}: not valid JSON ({err.msg})")
            continue
        try:
            tickets.append(Ticket.model_validate(payload))
        except ValidationError as err:
            first = err.errors()[0]
            field = ".".join(str(part) for part in first["loc"]) or "?"
            problems.append(f"line {number}: {field}: {first['msg']}")
    return tickets, problems


def years_present(tickets: list[Ticket]) -> tuple[int, ...]:
    """Every calendar year any ticket timestamp touches.

    Collected from the data rather than assumed, because the holiday library must be asked for the
    right years -- and asking for the wrong ones returns an empty set that reads as "no holidays".
    """
    years: set[int] = set()
    for ticket in tickets:
        for instant in (ticket.created_at, ticket.first_activity_at, ticket.resolved_at):
            if instant is not None:
                years.add(instant.year)
    return tuple(sorted(years))


def group_by_region(tickets: list[Ticket]) -> dict[str, list[Ticket]]:
    grouped: dict[str, list[Ticket]] = defaultdict(list)
    for ticket in tickets:
        grouped[ticket.region.strip().lower()].append(ticket)
    return dict(grouped)


class ResolvedRegion:
    """A region's calendar, its coverage verdict and its holiday set, resolved once."""

    __slots__ = ("calendar", "coverage", "holidays", "region")

    def __init__(
        self,
        region: str,
        calendar: BusinessCalendar | None,
        coverage: CalendarCoverage | None,
        holidays: frozenset,
    ) -> None:
        self.region = region
        self.calendar = calendar
        self.coverage = coverage
        self.holidays = holidays

    @property
    def is_usable(self) -> bool:
        return (
            self.calendar is not None
            and self.coverage is not None
            and self.coverage.is_usable
            and zone_exists(self.calendar.zone_name)
        )


def resolve_regions(
    tickets: list[Ticket],
    years: tuple[int, ...] | None = None,
) -> dict[str, ResolvedRegion]:
    """Resolve every region referenced by the tickets, including the ones that cannot be resolved.

    An unresolvable region is kept with its reason attached rather than dropped, so a ticket in an
    uncovered region produces a refusal instead of quietly vanishing from the denominator.

    Coverage is checked against the years THAT REGION's tickets touch, not the years present in the
    whole dataset. Using the global set makes coverage contagious: one migrated 1989 row would put
    1989 into every region's check, and since no region's holiday data reaches back that far, every
    region in the file would be reported as uncovered and every ticket refused. The failure would
    look like a broken tool rather than one bad row.
    """
    grouped = group_by_region(tickets)
    resolved: dict[str, ResolvedRegion] = {}
    for region in sorted(grouped):
        spans = years or years_present(grouped[region])
        calendar = calendar_for(region)
        if calendar is None:
            resolved[region] = ResolvedRegion(region, None, None, frozenset())
            continue
        coverage = check_coverage(calendar, spans)
        holidays = holiday_set(calendar, spans) if coverage.is_usable else frozenset()
        resolved[region] = ResolvedRegion(region, calendar, coverage, holidays)
    return resolved
