"""The projection from instants onto working time, and the facts that projection destroys.

Wall-clock elapsed time is a function of two instants: `end - start`. It is translation-invariant,
so shifting both endpoints by the same amount leaves it unchanged.

Business elapsed time is NOT. It depends on WHERE IN THE WEEK the span falls, so the same 48-hour
span consumes 16 business hours starting Monday and 0 starting Friday evening. That single fact is
what makes an SLA expressed in business hours a promise about our effort rather than about the
customer's wait, and everything else in this module follows from it.

The projection is also MANY-TO-ONE. Every instant from Friday close to Monday open maps to the same
business-time position, so two tickets 64 hours apart are simultaneous in business time and cannot
be ordered by it.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from .calendars import UTC, BusinessCalendar
from .models import DeadGap, elapsed, elapsed_hours

MAX_DAYS = 40_000
"""Iteration guard. A span longer than roughly a century is a data error, not a long ticket."""


def _walk(calendar: BusinessCalendar, first: date, last: date):
    """Yield local dates from `first` to `last` inclusive, refusing absurd spans."""
    if (last - first).days > MAX_DAYS:
        raise ValueError(
            f"span of {(last - first).days} days exceeds the {MAX_DAYS}-day guard; "
            "this is almost certainly a bad timestamp rather than a long-running ticket"
        )
    day = first
    while day <= last:
        yield day
        day += timedelta(days=1)


def business_elapsed(
    calendar: BusinessCalendar,
    holidays: frozenset[date],
    start: datetime,
    end: datetime,
) -> float:
    """Hours of working time in the half-open interval [start, end).

    Computed as the PHYSICAL duration of each day's open window intersected with the interval, so
    the result is real elapsed time rather than a count of local clock hours. The two differ only
    when a DST transition falls inside a working window, which for a daytime Monday-Friday calendar
    never happens -- a property asserted from tzdata rather than assumed.
    """
    if end <= start:
        return 0.0

    total = timedelta()
    for day in _walk(calendar, calendar.local_date(start), calendar.local_date(end)):
        if not calendar.is_working_day(day, holidays):
            continue
        window = calendar.window_on(day)
        if window is None:
            continue
        opens, closes = window
        lower, upper = max(opens, start), min(closes, end)
        if upper > lower:
            total += elapsed(lower, upper)
    return total.total_seconds() / 3600


def add_business_hours(
    calendar: BusinessCalendar,
    holidays: frozenset[date],
    start: datetime,
    hours: float,
) -> datetime | None:
    """The instant at which `hours` of working time have elapsed from `start`.

    Returns None if the calendar has no open window within the guard, which happens for a region
    whose entire scanned range is holidays -- reported rather than approximated.

    Note this is NOT the inverse of `business_elapsed`, and cannot be. The projection is many-to-one,
    so an arrival inside a dead gap shares its business-time position with every other instant in
    that gap, and no function of the position can recover which one it was.
    """
    remaining = timedelta(hours=hours)
    cursor = start

    for day in _walk(
        calendar, calendar.local_date(start), calendar.local_date(start) + timedelta(days=MAX_DAYS)
    ):
        if not calendar.is_working_day(day, holidays):
            continue
        window = calendar.window_on(day)
        if window is None:
            continue
        opens, closes = window
        if closes <= cursor:
            continue
        entry = max(opens, cursor)
        available = closes - entry
        if remaining <= available:
            return entry + remaining
        remaining -= available
        cursor = closes
    return None


def next_open(
    calendar: BusinessCalendar,
    holidays: frozenset[date],
    instant: datetime,
) -> datetime | None:
    """The next instant the clock runs, which is `instant` itself if it already does."""
    for day in _walk(
        calendar,
        calendar.local_date(instant),
        calendar.local_date(instant) + timedelta(days=MAX_DAYS),
    ):
        if not calendar.is_working_day(day, holidays):
            continue
        window = calendar.window_on(day)
        if window is None:
            continue
        opens, closes = window
        if instant < opens:
            return opens
        if opens <= instant < closes:
            return instant
    return None


def last_close(
    calendar: BusinessCalendar,
    holidays: frozenset[date],
    instant: datetime,
) -> datetime | None:
    """The most recent instant the clock stopped at or before `instant`."""
    day = calendar.local_date(instant)
    for _ in range(400):
        if calendar.is_working_day(day, holidays):
            window = calendar.window_on(day)
            if window is not None:
                opens, closes = window
                if closes <= instant:
                    return closes
                if opens < instant:
                    return instant
        day -= timedelta(days=1)
    return None


def dead_gap_containing(
    calendar: BusinessCalendar,
    holidays: frozenset[date],
    instant: datetime,
) -> DeadGap | None:
    """The closed stretch `instant` falls inside, or None if the calendar is open then.

    The gap's length is the constant in the amplification arithmetic. It is set by the calendar --
    weekend length, holidays, opening hours -- and no choice of SLA changes it.
    """
    opens_next = next_open(calendar, holidays, instant)
    if opens_next is None or opens_next == instant:
        return None
    closed_since = last_close(calendar, holidays, instant)
    if closed_since is None:
        return None

    span_days = {
        day
        for day in _walk(
            calendar, calendar.local_date(closed_since), calendar.local_date(opens_next)
        )
        if day in holidays
    }
    if span_days:
        reason = "holiday " + ", ".join(str(day) for day in sorted(span_days))
    elif elapsed(closed_since, opens_next) > timedelta(hours=calendar.hours_per_day * 2):
        reason = "weekend"
    else:
        reason = "overnight"

    return DeadGap(
        region=calendar.region, starts_at=closed_since, ends_at=opens_next, reason=reason
    )


def business_position(
    calendar: BusinessCalendar,
    holidays: frozenset[date],
    instant: datetime,
    epoch: datetime,
) -> float:
    """Working hours between a fixed epoch and `instant` -- the projection's coordinate.

    Two instants with the same coordinate are SIMULTANEOUS in business time however far apart they
    are in real time. This is what makes first-in-first-out by business-hours-aged unable to order
    weekend arrivals: a ticket that landed Friday evening and one that landed Monday morning have
    the same coordinate, so the queue reports them as equally aged while one customer has been
    waiting all weekend.
    """
    if instant <= epoch:
        return -business_elapsed(calendar, holidays, instant, epoch)
    return business_elapsed(calendar, holidays, epoch, instant)


def wall_hours(start: datetime, end: datetime) -> float:
    """Physical hours between two instants.

    Delegates to `elapsed_hours`, which converts both operands to UTC first. Plain subtraction is
    NOT safe here: Python differences two aware datetimes that share a `tzinfo` by their naive
    wall-clock values, so a span across a spring forward comes back an hour too long.

    An earlier version of this function did exactly that and claimed in its own docstring that
    "aware subtraction ... is real elapsed time". It was wrong in a way that depended on how the
    caller built the timestamp: the corpus round-trips through `isoformat`, which bakes a FIXED
    offset and subtracts correctly, while the same instant built with `tzinfo=ZoneInfo(...)` did
    not. Two spellings of one instant, two answers, no error.
    """
    return elapsed_hours(start, end)


def utc(instant: datetime) -> datetime:
    return instant.astimezone(UTC)
