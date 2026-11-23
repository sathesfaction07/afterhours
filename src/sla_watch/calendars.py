"""Working calendars: a zone, a weekly window, and a holiday set -- each of which can be absent.

The weekly window is a set of weekdays, not a Monday-to-Friday assumption. Israel and Egypt work
Sunday to Thursday, so hardcoding Saturday and Sunday as the weekend would place their weekend on
two working days and their working days on the weekend. That single detail also decides whether the
DST handling in `clock` can ever fire -- see `transition_risk`.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import holidays
from pydantic import BaseModel, Field, field_validator

from .models import Coverage, elapsed_hours

UTC = ZoneInfo("UTC")

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)

WESTERN_WEEK = frozenset({MON, TUE, WED, THU, FRI})
"""Monday-Friday. Provably immune to DST inside working hours -- see `transition_risk`."""

GULF_WEEK = frozenset({SUN, MON, TUE, WED, THU})
"""Sunday-Thursday, used across much of the Middle East.

Includes Sunday, which is when essentially every DST transition on earth is scheduled -- so this
week SHARES A DAY with the transitions that the Monday-Friday week never touches.
"""


class BusinessCalendar(BaseModel):
    """When a support region is open.

    `region` is a two-letter code passed to the holiday library. `subdivision` is separate because
    a country with subdivisions has no single national calendar: `GB` alone yields 7 holidays for
    2026 while England yields 9, Scotland 11 and Northern Ireland 12.
    """

    model_config = {"frozen": True}

    region: str
    zone_name: str
    opens: time = time(9, 0)
    closes: time = time(17, 0)
    working_days: frozenset[int] = WESTERN_WEEK
    subdivision: str | None = None
    label: str = ""

    @field_validator("working_days")
    @classmethod
    def _at_least_one_day(cls, value: frozenset[int]) -> frozenset[int]:
        if not value:
            raise ValueError("a calendar with no working days can never advance")
        if not all(0 <= day <= 6 for day in value):
            raise ValueError("working days must be 0 (Monday) through 6 (Sunday)")
        return value

    @field_validator("closes")
    @classmethod
    def _must_close_after_opening(cls, value: time) -> time:
        if value == time(0, 0):
            raise ValueError("a close time of midnight is ambiguous; use 23:59 for a full day")
        return value

    def model_post_init(self, _context: object) -> None:
        if self.closes <= self.opens:
            raise ValueError(
                f"{self.region}: closes {self.closes} is not after opens {self.opens}. "
                "Overnight windows are not supported -- see ADR 002 for why."
            )

    @property
    def zone(self) -> ZoneInfo:
        return _zone(self.zone_name)

    @property
    def hours_per_day(self) -> float:
        """Working hours in one open day, by the LOCAL clock."""
        return (
            datetime.combine(date(2001, 1, 1), self.closes)
            - datetime.combine(date(2001, 1, 1), self.opens)
        ).total_seconds() / 3600

    @property
    def hours_per_week(self) -> float:
        return self.hours_per_day * len(self.working_days)

    @property
    def open_fraction(self) -> float:
        """Share of wall time the calendar is open. 40/168 = 23.8% for a standard week.

        The reciprocal is the long-run amplification floor: over a span long enough to average out
        the phase, the customer's wait is at least this many times the working time consumed.
        """
        return self.hours_per_week / 168.0

    def is_working_day(self, day: date, holiday_set: frozenset[date]) -> bool:
        return day.weekday() in self.working_days and day not in holiday_set

    def window_on(self, day: date) -> tuple[datetime, datetime] | None:
        """The open and close instants for one local date, or None if either does not exist.

        Both bounds are round-tripped through UTC. A local wall time inside a spring-forward gap
        never happens, and Python does not raise for it -- `datetime.combine` will happily build one
        and `astimezone` will silently move it. Returning None makes that day visible rather than
        quietly mis-measured. For a Monday-Friday 09:00-17:00 calendar this is provably unreachable,
        which is asserted rather than assumed.
        """
        opens = datetime.combine(day, self.opens, tzinfo=self.zone)
        closes = datetime.combine(day, self.closes, tzinfo=self.zone)
        for bound in (opens, closes):
            if bound.astimezone(UTC).astimezone(self.zone).replace(tzinfo=None) != bound.replace(
                tzinfo=None
            ):
                return None
        return opens, closes

    def local_date(self, instant: datetime) -> date:
        return instant.astimezone(self.zone).date()


@lru_cache(maxsize=64)
def _zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


class CalendarCoverage(BaseModel):
    """Whether a calendar can be resolved, and what was missing when it could not."""

    region: str
    coverage: Coverage
    years: tuple[int, ...] = ()
    holiday_count: int = 0
    supported_range: tuple[int, int] | None = None
    subdivisions: tuple[str, ...] = ()
    detail: str = ""

    @property
    def is_usable(self) -> bool:
        return self.coverage.is_usable


def check_coverage(calendar: BusinessCalendar, years: tuple[int, ...]) -> CalendarCoverage:
    """Resolve a calendar's holiday set, or say precisely why it cannot be resolved.

    The three failure modes are genuinely different, and only one of them is loud on its own:

    - UNSUPPORTED_REGION raises from the library, so it cannot be missed.
    - SUBDIVISION_MISSING returns a SMALLER set with no warning.
    - YEAR_OUT_OF_RANGE returns an EMPTY set with no warning, so every day looks like a working
      day and the SLA is computed tighter than reality.

    The last two are why this function exists. An empty holiday set is indistinguishable from a
    region that genuinely has no holidays, and the difference decides whether a breach is real.
    """
    try:
        probe = holidays.country_holidays(calendar.region, subdiv=calendar.subdivision)
    except (NotImplementedError, KeyError) as err:
        return CalendarCoverage(
            region=calendar.region,
            coverage=Coverage.UNSUPPORTED_REGION,
            years=years,
            detail=str(err)[:120],
        )

    subdivisions = tuple(getattr(probe, "subdivisions", ()) or ())
    start_year = getattr(probe, "start_year", None)
    end_year = getattr(probe, "end_year", None)
    supported = (start_year, end_year) if start_year and end_year else None

    if supported:
        outside = [year for year in years if not start_year <= year <= end_year]
        if outside:
            return CalendarCoverage(
                region=calendar.region,
                coverage=Coverage.YEAR_OUT_OF_RANGE,
                years=years,
                supported_range=supported,
                subdivisions=subdivisions,
                detail=(
                    f"years {sorted(outside)} are outside {start_year}-{end_year}; the library "
                    "answers False for them, which reads as a seven-day working week"
                ),
            )

    if subdivisions and calendar.subdivision is None:
        chosen = len(holidays.country_holidays(calendar.region, years=years))
        widest = max(
            len(holidays.country_holidays(calendar.region, subdiv=sub, years=years))
            for sub in subdivisions
        )
        return CalendarCoverage(
            region=calendar.region,
            coverage=Coverage.SUBDIVISION_MISSING,
            years=years,
            holiday_count=chosen,
            supported_range=supported,
            subdivisions=subdivisions,
            detail=(
                f"{calendar.region} has subdivisions {list(subdivisions)} and none was chosen: "
                f"the national default gives {chosen} holidays where the widest subdivision gives "
                f"{widest}, so real days off are silently missing"
            ),
        )

    resolved = holidays.country_holidays(
        calendar.region, subdiv=calendar.subdivision, years=years
    )
    return CalendarCoverage(
        region=calendar.region,
        coverage=Coverage.COVERED,
        years=years,
        holiday_count=len(resolved),
        supported_range=supported,
        subdivisions=subdivisions,
    )


def holiday_set(calendar: BusinessCalendar, years: tuple[int, ...]) -> frozenset[date]:
    """The holiday dates for a calendar, on the assumption coverage was already checked.

    Callers must check `check_coverage` first. This returns an empty set for an uncovered year, and
    acting on that without checking is precisely the failure the coverage check exists to prevent.
    """
    try:
        resolved = holidays.country_holidays(
            calendar.region, subdiv=calendar.subdivision, years=years
        )
    except (NotImplementedError, KeyError):
        return frozenset()
    return frozenset(resolved.keys())


def overlap_hours(
    left: BusinessCalendar,
    right: BusinessCalendar,
    day: date,
) -> float:
    """Hours on one date when BOTH calendars are open, computed in real time.

    Two regions can share a working day and still have zero overlapping working hours. London and
    Tokyo overlap for 0 hours in January and 1 hour in July -- the single hour exists only because
    British Summer Time shifts London's window an hour earlier against UTC. So a follow-the-sun
    handoff between them has no window at all for most of the year, and DST creates the only one
    they get.
    """
    lw, rw = left.window_on(day), right.window_on(day)
    if lw is None or rw is None:
        return 0.0
    start, end = max(lw[0], rw[0]), min(lw[1], rw[1])
    return elapsed_hours(start, end)


class TransitionRisk(BaseModel):
    """Whether DST can land inside a calendar's working window.

    The answer is empirical, not a matter of care in the code: transitions are SCHEDULED into the
    small hours, so a daytime window never sees one. This model exists so the immunity is a checked
    property rather than an assumption -- if tzdata ever moves a transition into business hours,
    the test that builds this fails.
    """

    region: str
    transitions_scanned: int
    inside_window: int
    examples: tuple[str, ...] = Field(default=())

    @property
    def is_immune(self) -> bool:
        return self.inside_window == 0


def transition_risk(
    calendar: BusinessCalendar,
    first_year: int,
    last_year: int,
) -> TransitionRisk:
    """Count offset changes falling inside this calendar's working window.

    Derived from the host's tzdata at call time rather than a hardcoded list of dates, so the answer
    stays true as the database is updated. Same approach Meridian used for its DST tests;
    the question here is different -- not "is this local time ambiguous" but "can a transition
    corrupt a working-hours count".
    """
    zone = calendar.zone
    step = timedelta(hours=1)
    instant = datetime(first_year, 1, 1, tzinfo=UTC)
    end = datetime(last_year + 1, 1, 1, tzinfo=UTC)
    previous = instant.astimezone(zone).utcoffset()

    scanned, inside, examples = 0, 0, []
    while instant < end:
        instant += step
        offset = instant.astimezone(zone).utcoffset()
        if offset == previous:
            continue
        previous = offset
        scanned += 1
        local = instant.astimezone(zone)
        if local.weekday() in calendar.working_days and (
            calendar.opens <= local.time() < calendar.closes
        ):
            inside += 1
            if len(examples) < 4:
                examples.append(f"{local:%Y-%m-%d %a %H:%M} {calendar.zone_name}")

    return TransitionRisk(
        region=calendar.region,
        transitions_scanned=scanned,
        inside_window=inside,
        examples=tuple(examples),
    )


def zone_exists(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True
