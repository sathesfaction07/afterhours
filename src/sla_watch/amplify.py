"""The arithmetic of the gap between the promise and the wait.

Worst-case amplification is `1 + G/B`, where G is the longest stretch the calendar is closed and B
is the SLA in business hours. G belongs to the CALENDAR; B is the promise. So:

- Tightening the SLA makes the worst RATIO hyperbolically worse: 8 business hours gives 9x on a
  Friday-evening arrival, 1 business hour gives 65x.
- And it barely improves the worst WAIT: 72 hours becomes 65, a 9.7% gain for an 8x tighter promise,
  because G dominates the sum.

Neither of those is visible in a compliance percentage, because both cases are recorded as MET.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from pydantic import BaseModel

from .calendars import UTC, BusinessCalendar
from .clock import add_business_hours, dead_gap_containing, next_open, wall_hours
from .models import DeadGap


class GapProfile(BaseModel):
    """The closed stretches in a calendar year, longest first."""

    region: str
    year: int
    longest: DeadGap | None = None
    gaps_found: int = 0
    weekend_hours: float = 0.0
    longest_holiday_hours: float = 0.0

    @property
    def longest_hours(self) -> float:
        return self.longest.hours if self.longest else 0.0


def gap_profile(
    calendar: BusinessCalendar,
    holidays: frozenset[date],
    year: int,
) -> GapProfile:
    """Find every distinct closed stretch in a year and keep the longest.

    Walks the year's closures by jumping from one gap's end to the next rather than sampling, so a
    gap cannot be missed by landing between samples.
    """
    cursor = datetime(year, 1, 1, tzinfo=UTC).astimezone(calendar.zone)
    end = datetime(year + 1, 1, 1, tzinfo=UTC).astimezone(calendar.zone)

    longest: DeadGap | None = None
    weekend_best = 0.0
    holiday_best = 0.0
    found = 0

    while cursor < end:
        gap = dead_gap_containing(calendar, holidays, cursor)
        if gap is None:
            opens = next_open(calendar, holidays, cursor)
            if opens is None:
                break
            cursor = opens + timedelta(minutes=1)
            continue
        found += 1
        if longest is None or gap.hours > longest.hours:
            longest = gap
        if gap.reason == "weekend":
            weekend_best = max(weekend_best, gap.hours)
        elif gap.reason.startswith("holiday"):
            holiday_best = max(holiday_best, gap.hours)
        cursor = gap.ends_at + timedelta(minutes=1)

    return GapProfile(
        region=calendar.region,
        year=year,
        longest=longest,
        gaps_found=found,
        weekend_hours=weekend_best,
        longest_holiday_hours=holiday_best,
    )


def worst_amplification(gap_hours: float, sla_hours: float) -> float:
    """`1 + G/B`. The closed-form worst case, for cross-checking the measured one.

    Exact only while the SLA fits inside one working day, because a longer SLA also accumulates
    overnight gaps. Beyond that this is a lower bound on the worst case, and `amplification_profile`
    measures the real figure.
    """
    if sla_hours <= 0:
        raise ValueError("SLA must be positive")
    return 1.0 + gap_hours / sla_hours


class SlaRow(BaseModel):
    """One row of the sensitivity table: what tightening the promise actually buys."""

    sla_hours: float
    worst_wall_hours: float
    worst_amplification: float
    arrival: datetime


class Sensitivity(BaseModel):
    """How the worst case moves as the SLA is tightened.

    The point of the table is the two columns moving in OPPOSITE directions: the amplification gets
    dramatically worse while the wait barely improves.
    """

    region: str
    rows: tuple[SlaRow, ...]
    gap_hours: float

    @property
    def wait_improvement(self) -> float | None:
        """Fractional improvement in worst wait between the loosest and tightest SLA."""
        if len(self.rows) < 2:
            return None
        loose, tight = self.rows[-1], self.rows[0]
        if loose.worst_wall_hours <= 0:
            return None
        return 1.0 - tight.worst_wall_hours / loose.worst_wall_hours

    @property
    def amplification_ratio(self) -> float | None:
        """How many times worse the amplification gets across the same range."""
        if len(self.rows) < 2:
            return None
        loose, tight = self.rows[-1], self.rows[0]
        if loose.worst_amplification <= 0:
            return None
        return tight.worst_amplification / loose.worst_amplification


def sensitivity(
    calendar: BusinessCalendar,
    holidays: frozenset[date],
    slas: tuple[float, ...],
    year: int,
) -> Sensitivity:
    """Measure the worst case for each SLA by starting at the longest gap in the year."""
    profile = gap_profile(calendar, holidays, year)
    if profile.longest is None:
        return Sensitivity(region=calendar.region, rows=(), gap_hours=0.0)

    arrival = profile.longest.starts_at
    rows = []
    for sla in sorted(slas):
        deadline = add_business_hours(calendar, holidays, arrival, sla)
        if deadline is None:
            continue
        wall = wall_hours(arrival, deadline)
        rows.append(
            SlaRow(
                sla_hours=sla,
                worst_wall_hours=wall,
                worst_amplification=wall / sla,
                arrival=arrival,
            )
        )
    return Sensitivity(
        region=calendar.region, rows=tuple(rows), gap_hours=profile.longest.hours
    )


class ArrivalProfile(BaseModel):
    """The distribution of amplification over arrivals, and the assumption behind it.

    `arrival_region` names whose clock the arrivals follow. Uniform arrival is the neutral choice and
    it is WRONG in both directions: real tickets cluster into the customer's working hours, which
    helps when the customer shares the support region's calendar and hurts when they do not.

    `landed_in_gap` is reported and is NOT what drives amplification, which is worth saying because
    it is the intuitive candidate and it turns out close to anti-correlated. I predicted a London
    team serving Tokyo customers would be the worst cohort, since 93.8% of those arrivals land while
    London is shut. Measured, it is the BEST of the three -- mean wait 13.6h against 31.2h for
    London's own customers, who land in a gap 0.0% of the time.

    The reason is `mean_dead_hours`: Tokyo lands in London's short OVERNIGHT gap and then gets a
    full working day, for 5.6 dead hours inside the deadline window. London's own customers arrive
    during business hours and a fifth of them straddle a weekend, for 23.2. What amplifies is dead
    time inside the DEADLINE WINDOW, not a closed calendar at the moment of arrival.
    """

    region: str
    arrival_region: str | None
    year: int
    sla_hours: float
    samples: int
    landed_in_gap: int
    mean_wall_hours: float = 0.0
    worst_wall_hours: float = 0.0
    worst_amplification: float | None = None
    over_2x: int = 0
    over_4x: int = 0
    over_8x: int = 0

    @property
    def gap_share(self) -> float | None:
        """Share of arrivals landing while the calendar was shut.

        Kept in the report despite not predicting amplification, because it is the number a reader
        expects to be the explanation, and seeing it diverge is the finding.
        """
        if self.samples == 0:
            return None
        return self.landed_in_gap / self.samples

    @property
    def mean_dead_hours(self) -> float:
        """Mean wall time inside the deadline window during which the calendar was shut.

        This is what actually drives amplification, and it is a property of the window rather than
        of the arrival instant.
        """
        return max(self.mean_wall_hours - self.sla_hours, 0.0)

    @property
    def over_4x_share(self) -> float | None:
        if self.samples == 0:
            return None
        return self.over_4x / self.samples


def amplification_profile(
    calendar: BusinessCalendar,
    holidays: frozenset[date],
    sla_hours: float,
    year: int,
    arrival_calendar: BusinessCalendar | None = None,
    arrival_holidays: frozenset[date] | None = None,
    step_hours: int = 1,
) -> ArrivalProfile:
    """Step through a year hourly and measure the amplification each arrival would see.

    Deterministic rather than sampled, so the answer is reproducible without a seed.

    When `arrival_calendar` is given, only instants inside THAT calendar's working hours count as
    arrivals. That turns "our customers are in another region" from an observation into a number --
    though not the number I expected, which is recorded on `ArrivalProfile`.
    """
    cursor = datetime(year, 1, 1, tzinfo=UTC)
    end = datetime(year + 1, 1, 1, tzinfo=UTC)
    step = timedelta(hours=step_hours)

    samples = in_gap = over2 = over4 = over8 = 0
    total_wall = 0.0
    worst_wall = 0.0
    worst_amp: float | None = None

    while cursor < end:
        instant = cursor
        cursor += step

        if arrival_calendar is not None:
            local = instant.astimezone(arrival_calendar.zone)
            if local.weekday() not in arrival_calendar.working_days:
                continue
            if arrival_holidays and local.date() in arrival_holidays:
                continue
            if not arrival_calendar.opens <= local.timetz().replace(tzinfo=None) < (
                arrival_calendar.closes
            ):
                continue

        deadline = add_business_hours(calendar, holidays, instant, sla_hours)
        if deadline is None:
            continue
        samples += 1
        wall = wall_hours(instant, deadline)
        total_wall += wall
        worst_wall = max(worst_wall, wall)

        if dead_gap_containing(calendar, holidays, instant) is not None:
            in_gap += 1

        amp = wall / sla_hours
        if worst_amp is None or amp > worst_amp:
            worst_amp = amp
        if amp > 2:
            over2 += 1
        if amp > 4:
            over4 += 1
        if amp > 8:
            over8 += 1

    return ArrivalProfile(
        region=calendar.region,
        arrival_region=arrival_calendar.region if arrival_calendar else None,
        year=year,
        sla_hours=sla_hours,
        samples=samples,
        landed_in_gap=in_gap,
        mean_wall_hours=total_wall / samples if samples else 0.0,
        worst_wall_hours=worst_wall,
        worst_amplification=worst_amp,
        over_2x=over2,
        over_4x=over4,
        over_8x=over8,
    )
