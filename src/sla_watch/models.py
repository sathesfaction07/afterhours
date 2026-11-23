"""Models. The load-bearing decision is that elapsed time is a PAIR, not a number."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


def elapsed(start: datetime, end: datetime) -> timedelta:
    """Real time between two instants, via UTC.

    Both operands are converted before subtracting, and that is not decoration. Python subtracts two
    aware datetimes that share a `tzinfo` by differencing their NAIVE wall-clock values and ignoring
    the offsets -- documented behaviour, and it means a span across a DST transition comes back wrong
    by exactly the transition. Two `ZoneInfo("Europe/London")` instants either side of a spring
    forward report 64 hours where 63 elapsed: an hour that never existed.

    It is silent, and it is worse than silent -- it depends on how the timestamp was BUILT. A value
    parsed from `2026-03-29T10:00:00+01:00` carries a fixed offset, so it subtracts correctly, while
    the identical instant built with `tzinfo=ZoneInfo(...)` does not. The same logical span gives two
    different answers with no error raised.
    """
    return end.astimezone(UTC) - start.astimezone(UTC)


def elapsed_hours(start: datetime, end: datetime) -> float:
    """`elapsed` in hours, floored at zero."""
    return max(elapsed(start, end).total_seconds() / 3600, 0.0)


class Coverage(StrEnum):
    """Whether a region's working calendar can actually be resolved for a given year.

    This exists because the holiday library answers `date in calendar` with False for a year
    outside its coverage, and False is indistinguishable from "that day is a working day". An
    uncovered year therefore reads as a SEVEN-DAY WORKING WEEK, which makes every SLA look
    tighter than it is and manufactures breaches.
    """

    COVERED = "covered"

    YEAR_OUT_OF_RANGE = "year_out_of_range"
    """The library has this region but not this year. Silently False, so it must be caught here."""

    SUBDIVISION_MISSING = "subdivision_missing"
    """The region has subdivisions and none was chosen.

    Not an error and not covered either. `GB` alone yields 7 holidays for 2026 while ENG yields 9,
    SCT 11 and NIR 12 -- so the default is a SUBSET, and using it silently drops real days off.
    """

    UNSUPPORTED_REGION = "unsupported_region"
    """The library raises for this region, which is the one safe failure in the set."""

    @property
    def is_usable(self) -> bool:
        return self is Coverage.COVERED


class Verdict(StrEnum):
    """The outcome for one ticket.

    MET and BREACHED are the two everyone expects. The other four exist because a boolean
    `met: bool` forces three different unknowns and one important success into the wrong bucket.
    """

    MET = "met"
    """Met the promise, and the customer's wait was proportionate to it."""

    MET_WITH_LONG_WAIT = "met_with_long_wait"
    """Met the promise, and the customer still waited a large multiple of it.

    The headline case. A business-hours SLA measures OUR EFFORT; this is where that diverges from
    the customer's experience, and no compliance percentage computed from MET/BREACHED can show it.
    """

    BREACHED = "breached"

    OPEN = "open"
    """Not closed yet, so elapsed time is a growing lower bound rather than a measurement.

    Counting an open ticket as MET would flatter every report run before the tickets are resolved.
    """

    UNKNOWN_START = "unknown_start"
    """No creation instant, so elapsed time is bounded on ONE SIDE only.

    The first recorded activity is an upper bound on creation, which makes measured elapsed a LOWER
    bound. There is no upper bound available at all, so this can never be resolved to a number.
    """

    NO_CALENDAR = "no_calendar"
    """The region's working calendar could not be resolved, so no business-hours claim is possible.

    Deliberately NOT folded into a default calendar. Falling back to Mon-Fri 09:00-17:00 for an
    unknown region produces a confident number for a region whose weekend may be Friday-Saturday.
    """

    @property
    def is_decided(self) -> bool:
        """Whether a business-hours SLA claim was actually made for this ticket."""
        return self in {Verdict.MET, Verdict.MET_WITH_LONG_WAIT, Verdict.BREACHED}

    @property
    def is_met(self) -> bool:
        return self in {Verdict.MET, Verdict.MET_WITH_LONG_WAIT}


class Elapsed(BaseModel):
    """Time spent on a ticket, measured two ways because they answer different questions.

    `business_hours` is what the SLA promises: hours the support calendar was actually open.
    `wall_hours` is what the customer experienced: physical time from arrival to resolution.

    These are not two estimates of one quantity. Wall time is translation-invariant -- shift both
    endpoints by the same amount and it is unchanged -- while business time is NOT, because the
    answer depends on where in the week the span happens to fall. The same 48-hour span consumes 16
    business hours starting Monday and 0 starting Friday evening.
    """

    business_hours: float = Field(ge=0.0)
    wall_hours: float = Field(ge=0.0)

    @property
    def amplification(self) -> float | None:
        """How many times the customer's wait exceeded the working time spent.

        None when no business time was consumed at all, which is a real and common case: a ticket
        opened and closed inside one weekend consumed 0 business hours and some wall hours, and the
        ratio is genuinely undefined rather than zero or infinite. Returning 0.0 would read as
        "no amplification", which is the exact opposite of what happened.
        """
        if self.business_hours <= 0.0:
            return None
        return self.wall_hours / self.business_hours

    @property
    def dead_hours(self) -> float:
        """Wall time during which the support calendar was closed."""
        return max(self.wall_hours - self.business_hours, 0.0)


class Bound(StrEnum):
    """Whether a measured quantity is exact or one-sided."""

    EXACT = "exact"
    AT_LEAST = "at_least"
    """A lower bound: the true value is this or larger. Used for open tickets and unknown starts."""


class Ticket(BaseModel):
    """One support ticket. `created_at` is optional because the brief's worst input omits it."""

    ticket_id: str
    region: str
    created_at: datetime | None = None
    first_activity_at: datetime | None = None
    resolved_at: datetime | None = None
    sla_business_hours: float = Field(gt=0.0)
    priority: str = "standard"

    @field_validator("created_at", "first_activity_at", "resolved_at")
    @classmethod
    def _must_be_aware(cls, value: datetime | None) -> datetime | None:
        """A naive timestamp cannot be placed on a calendar in another zone.

        Rejected rather than assumed-UTC: assuming UTC for a Tokyo ticket shifts it by 9 hours,
        which moves it across the open/close boundary and changes the answer without any error.
        """
        if value is not None and value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return value

    @property
    def is_closed(self) -> bool:
        return self.resolved_at is not None

    @property
    def start_bound(self) -> Bound:
        """EXACT when creation is recorded; AT_LEAST when it has to be inferred from activity."""
        return Bound.EXACT if self.created_at is not None else Bound.AT_LEAST

    @property
    def effective_start(self) -> datetime | None:
        """The instant the clock starts from, or None if nothing at all is recorded.

        Falls back to first recorded activity, which is an UPPER bound on creation -- so the
        resulting elapsed time is a lower bound. Deliberately one-sided: there is no evidence
        available for how long the ticket existed before anyone touched it.
        """
        return self.created_at or self.first_activity_at


class TicketResult(BaseModel):
    """The verdict for one ticket, with the reasoning kept attached to it."""

    ticket_id: str
    region: str
    verdict: Verdict
    elapsed: Elapsed | None = None
    bound: Bound = Bound.EXACT
    sla_business_hours: float
    deadline: datetime | None = None
    coverage: Coverage = Coverage.COVERED
    note: str | None = None

    @property
    def amplification(self) -> float | None:
        return self.elapsed.amplification if self.elapsed else None


class DeadGap(BaseModel):
    """A stretch of wall time during which a calendar is closed.

    Named because it is the constant in the amplification arithmetic: worst-case amplification is
    1 + gap/sla, so the gap -- a property of the CALENDAR -- sets the ceiling, and the SLA only
    scales it. Tightening the SLA cannot shrink the gap.
    """

    region: str
    starts_at: datetime
    ends_at: datetime
    reason: str

    @property
    def hours(self) -> float:
        return elapsed_hours(self.starts_at, self.ends_at)

    @property
    def duration(self) -> timedelta:
        """Real elapsed time, via `elapsed`.

        A weekend gap spanning a spring forward is 63 hours, not 64. Differencing the two bounds
        directly reports 64 -- both carry the same ZoneInfo, so Python subtracts wall-clock values
        and hands back an hour that never happened. The autumn gap errs the other way, so the two
        do not cancel and neither shows up as a consistent bias.
        """
        return elapsed(self.starts_at, self.ends_at)
