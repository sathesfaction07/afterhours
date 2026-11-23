"""Verdicts, and the measurement that a compliance percentage cannot express.

The two numbers here answer different questions and are both correct:

- COMPLIANCE is `met / decided`. It measures whether we kept the promise we made.
- EXPERIENCE is the distribution of wall-clock waits. It measures what the customer got.

A team can report 100% compliance while a predictable share of its customers wait multiples of the
promise, and no arithmetic on MET and BREACHED can reveal it -- both cases are MET. That is not an
aggregation artefact that a finer slice would expose: the compliance metric is a PROJECTION that
discards the wall-clock dimension entirely, so the information is gone before any grouping happens.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime

from pydantic import BaseModel

from .calendars import BusinessCalendar
from .clock import add_business_hours, business_elapsed, business_position, wall_hours
from .models import Bound, Coverage, Elapsed, Ticket, TicketResult, Verdict
from .tickets import ResolvedRegion


def long_wait_threshold(calendar: BusinessCalendar) -> float:
    """The amplification above which a wait is worse than the calendar's own structure.

    Derived rather than chosen: `1 / open_fraction` is 168/40 = 4.2 for a standard week, which is
    the long-run ratio between wall time and working time. A ticket amplified beyond it waited
    longer than the calendar alone accounts for, so the threshold moves correctly for a region with
    different opening hours instead of being a magic number that suits only one of them.
    """
    return 1.0 / calendar.open_fraction


def evaluate(
    ticket: Ticket,
    region: ResolvedRegion,
    now: datetime,
    threshold: float | None = None,
) -> TicketResult:
    """Decide one ticket, refusing where the evidence does not support a decision.

    The order of the checks matters. Coverage is checked first because a missing calendar makes
    every later number meaningless, and an open ticket is checked before a missing start because
    "we do not know yet" and "we will never know" are different refusals.

    One refinement: a ONE-SIDED bound still settles the question when it falls on the wrong side. An
    open ticket that has already consumed more than its SLA is BREACHED whatever happens next, and a
    ticket whose lower-bound elapsed exceeds its SLA is breached even though its true start is
    unknown. A bound is not the same as ignorance.
    """
    if not region.is_usable:
        coverage = region.coverage.coverage if region.coverage else Coverage.UNSUPPORTED_REGION
        detail = region.coverage.detail if region.coverage else "no calendar registered"
        return TicketResult(
            ticket_id=ticket.ticket_id,
            region=ticket.region,
            verdict=Verdict.NO_CALENDAR,
            sla_business_hours=ticket.sla_business_hours,
            coverage=coverage,
            note=detail or "calendar could not be resolved",
        )

    calendar = region.calendar
    assert calendar is not None
    limit = threshold if threshold is not None else long_wait_threshold(calendar)
    start = ticket.effective_start

    if start is None:
        return TicketResult(
            ticket_id=ticket.ticket_id,
            region=ticket.region,
            verdict=Verdict.UNKNOWN_START,
            bound=Bound.AT_LEAST,
            sla_business_hours=ticket.sla_business_hours,
            note="no creation instant and no recorded activity, so elapsed time has no lower bound",
        )

    deadline = add_business_hours(
        calendar, region.holidays, start, ticket.sla_business_hours
    )

    if not ticket.is_closed:
        consumed = business_elapsed(calendar, region.holidays, start, now)
        elapsed = Elapsed(business_hours=consumed, wall_hours=wall_hours(start, now))
        if consumed > ticket.sla_business_hours:
            return TicketResult(
                ticket_id=ticket.ticket_id,
                region=ticket.region,
                verdict=Verdict.BREACHED,
                elapsed=elapsed,
                bound=Bound.AT_LEAST,
                sla_business_hours=ticket.sla_business_hours,
                deadline=deadline,
                note="still open and already past the deadline, so the breach is settled",
            )
        return TicketResult(
            ticket_id=ticket.ticket_id,
            region=ticket.region,
            verdict=Verdict.OPEN,
            elapsed=elapsed,
            bound=Bound.AT_LEAST,
            sla_business_hours=ticket.sla_business_hours,
            deadline=deadline,
            note="not resolved, so elapsed time is a lower bound that is still growing",
        )

    resolved_at = ticket.resolved_at
    assert resolved_at is not None
    consumed = business_elapsed(calendar, region.holidays, start, resolved_at)
    elapsed = Elapsed(business_hours=consumed, wall_hours=wall_hours(start, resolved_at))

    if ticket.start_bound is Bound.AT_LEAST:
        if consumed > ticket.sla_business_hours:
            return TicketResult(
                ticket_id=ticket.ticket_id,
                region=ticket.region,
                verdict=Verdict.BREACHED,
                elapsed=elapsed,
                bound=Bound.AT_LEAST,
                sla_business_hours=ticket.sla_business_hours,
                deadline=deadline,
                note=(
                    "creation instant missing, but the lower bound measured from first activity "
                    "already exceeds the SLA, so the breach holds however early it was created"
                ),
            )
        return TicketResult(
            ticket_id=ticket.ticket_id,
            region=ticket.region,
            verdict=Verdict.UNKNOWN_START,
            elapsed=elapsed,
            bound=Bound.AT_LEAST,
            sla_business_hours=ticket.sla_business_hours,
            deadline=deadline,
            note=(
                "measured from first activity, which is an upper bound on creation; the true "
                "elapsed time is this or more and there is no evidence for how much more"
            ),
        )

    if deadline is not None and resolved_at > deadline:
        verdict = Verdict.BREACHED
    else:
        amplification = elapsed.amplification
        verdict = (
            Verdict.MET_WITH_LONG_WAIT
            if amplification is not None and amplification > limit
            else Verdict.MET
        )

    return TicketResult(
        ticket_id=ticket.ticket_id,
        region=ticket.region,
        verdict=verdict,
        elapsed=elapsed,
        sla_business_hours=ticket.sla_business_hours,
        deadline=deadline,
    )


class RegionSummary(BaseModel):
    """One region's outcome, with compliance and experience side by side."""

    region: str
    label: str = ""
    coverage: Coverage = Coverage.COVERED
    tickets: int = 0
    counts: dict[str, int] = {}
    threshold: float | None = None
    worst_amplification: float | None = None
    worst_ticket: str | None = None
    mean_wall_hours: float | None = None
    mean_business_hours: float | None = None

    @property
    def decided(self) -> int:
        return sum(
            count for name, count in self.counts.items() if Verdict(name).is_decided
        )

    @property
    def met(self) -> int:
        return sum(count for name, count in self.counts.items() if Verdict(name).is_met)

    @property
    def compliance(self) -> float | None:
        """Met over decided.

        None rather than 0.0 or 1.0 when nothing was decided. A region whose calendar could not be
        resolved has made no claim either way, and reporting 100% for it -- which `all([])` would --
        turns a refusal into a perfect score.
        """
        if self.decided == 0:
            return None
        return self.met / self.decided

    @property
    def hidden_long_waits(self) -> int:
        """Tickets counted as MET whose customers waited beyond the structural threshold.

        The number a compliance percentage cannot contain, because these are successes.
        """
        return self.counts.get(Verdict.MET_WITH_LONG_WAIT.value, 0)

    @property
    def looks_perfect(self) -> bool:
        """Compliance is 100% AND some customer waited beyond the threshold anyway.

        The signature to look for: nothing in the compliance number invites a question, and the
        experience it describes is poor. Computed per region rather than from a total, because a
        fleet-wide compliance figure below 100% would prompt someone to investigate while a region
        sitting at 100% with long waits looks finished.
        """
        return self.compliance == 1.0 and self.hidden_long_waits > 0


class Report(BaseModel):
    """The whole dataset, region by region and in total."""

    generated_at: datetime
    tickets: int
    problems: tuple[str, ...] = ()
    results: tuple[TicketResult, ...] = ()
    regions: tuple[RegionSummary, ...] = ()
    holidays_library: str = ""

    @property
    def counts(self) -> dict[str, int]:
        tally: Counter[str] = Counter()
        for result in self.results:
            tally[result.verdict.value] += 1
        return dict(tally)

    @property
    def decided(self) -> int:
        return sum(1 for result in self.results if result.verdict.is_decided)

    @property
    def met(self) -> int:
        return sum(1 for result in self.results if result.verdict.is_met)

    @property
    def compliance(self) -> float | None:
        if self.decided == 0:
            return None
        return self.met / self.decided

    @property
    def hidden_long_waits(self) -> int:
        return sum(
            1 for result in self.results if result.verdict is Verdict.MET_WITH_LONG_WAIT
        )

    @property
    def refused(self) -> int:
        """Tickets on which no SLA claim was made, for any of the three reasons."""
        return sum(1 for result in self.results if not result.verdict.is_decided)

    @property
    def undefined_amplification(self) -> int:
        """Tickets that were measured and whose amplification has no value.

        Counted separately rather than rendered as a dash. These consumed zero working time -- a
        ticket opened and closed inside one weekend -- so the ratio is undefined rather than absent,
        and that is a different statement from "this region had nothing to measure".
        """
        return sum(
            1
            for result in self.results
            if result.elapsed is not None and result.amplification is None
        )

    @property
    def regions_looking_perfect(self) -> tuple[str, ...]:
        return tuple(summary.region for summary in self.regions if summary.looks_perfect)

    @property
    def worst_amplification(self) -> float | None:
        seen = [
            result.amplification for result in self.results if result.amplification is not None
        ]
        return max(seen) if seen else None

    @property
    def is_clean(self) -> bool:
        """Whether the dataset supports a compliance claim without a caveat travelling with it."""
        return not self.problems and self.refused == 0 and self.hidden_long_waits == 0


def summarise_region(
    region_key: str,
    region: ResolvedRegion,
    results: list[TicketResult],
) -> RegionSummary:
    counts: Counter[str] = Counter()
    for result in results:
        counts[result.verdict.value] += 1

    measured = [result for result in results if result.elapsed is not None]
    amplified = [
        (result.amplification, result.ticket_id)
        for result in measured
        if result.amplification is not None
    ]
    worst = max(amplified) if amplified else None

    return RegionSummary(
        region=region_key,
        label=region.calendar.label if region.calendar else "",
        coverage=region.coverage.coverage if region.coverage else Coverage.UNSUPPORTED_REGION,
        tickets=len(results),
        counts=dict(counts),
        threshold=long_wait_threshold(region.calendar) if region.calendar else None,
        worst_amplification=worst[0] if worst else None,
        worst_ticket=worst[1] if worst else None,
        mean_wall_hours=(
            sum(r.elapsed.wall_hours for r in measured) / len(measured) if measured else None
        ),
        mean_business_hours=(
            sum(r.elapsed.business_hours for r in measured) / len(measured) if measured else None
        ),
    )


class SimultaneousGroup(BaseModel):
    """Tickets that share a business-time position while arriving at different instants.

    The projection onto working time is many-to-one, so every arrival inside one closed stretch maps
    to the same coordinate. A queue ordered by business-hours-aged reports these as equally old
    however far apart they really arrived, which is why "oldest first" by business time does not put
    the longest-waiting customer first.
    """

    region: str
    position_hours: float
    ticket_ids: tuple[str, ...]
    spread_hours: float

    @property
    def size(self) -> int:
        return len(self.ticket_ids)


def simultaneous_in_business_time(
    tickets: list[Ticket],
    region: ResolvedRegion,
    epoch: datetime,
) -> tuple[SimultaneousGroup, ...]:
    """Group tickets by business-time coordinate and keep the groups that hide a real spread."""
    if not region.is_usable:
        return ()
    calendar = region.calendar
    assert calendar is not None

    buckets: dict[float, list[tuple[datetime, str]]] = {}
    for ticket in tickets:
        start = ticket.effective_start
        if start is None:
            continue
        position = round(
            business_position(calendar, region.holidays, start, epoch), 6
        )
        buckets.setdefault(position, []).append((start, ticket.ticket_id))

    groups = []
    for position, members in sorted(buckets.items()):
        if len(members) < 2:
            continue
        instants = [instant for instant, _ in members]
        spread = wall_hours(min(instants), max(instants))
        if spread <= 0:
            continue
        groups.append(
            SimultaneousGroup(
                region=region.region,
                position_hours=position,
                ticket_ids=tuple(name for _, name in sorted(members)),
                spread_hours=spread,
            )
        )
    return tuple(groups)


def build_report(
    tickets: list[Ticket],
    regions: dict[str, ResolvedRegion],
    now: datetime,
    problems: tuple[str, ...] = (),
    library_version: str = "",
) -> Report:
    """Evaluate every ticket and roll the results up per region."""
    per_region: dict[str, list[TicketResult]] = {key: [] for key in regions}
    results: list[TicketResult] = []

    for ticket in tickets:
        key = ticket.region.strip().lower()
        region = regions.get(key)
        if region is None:
            region = ResolvedRegion(key, None, None, frozenset())
            regions[key] = region
            per_region[key] = []
        result = evaluate(ticket, region, now)
        results.append(result)
        per_region.setdefault(key, []).append(result)

    summaries = tuple(
        summarise_region(key, regions[key], per_region.get(key, []))
        for key in sorted(regions)
    )
    return Report(
        generated_at=now,
        tickets=len(tickets),
        problems=problems,
        results=tuple(results),
        regions=summaries,
        holidays_library=library_version,
    )


def holidays_in(region: ResolvedRegion, year: int) -> tuple[date, ...]:
    return tuple(sorted(day for day in region.holidays if day.year == year))
