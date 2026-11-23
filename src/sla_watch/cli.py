"""CLI. Nine commands, and only `check` gates.

`check` fails on unreadable rows and on tickets whose calendar cannot be resolved, because both are
fixable at source. It does NOT fail on a long wait: that is a consequence of the calendar rather than
a defect, and a gate that fires whenever someone reports a fault on a Friday afternoon gets switched
off inside a week.
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import click
import holidays

from .amplify import amplification_profile, gap_profile, sensitivity
from .calendars import holiday_set, overlap_hours, transition_risk
from .clock import add_business_hours, business_elapsed, dead_gap_containing, wall_hours
from .config import Settings
from .measure import (
    build_report,
    evaluate,
    long_wait_threshold,
    simultaneous_in_business_time,
)
from .models import Verdict
from .render import (
    amplify_text,
    check_text,
    gaps_text,
    queue_text,
    report_csv,
    report_json,
    report_text,
    transitions_text,
    write_outputs,
)
from .tickets import (
    REGISTRY,
    calendar_for,
    group_by_region,
    load_tickets,
    resolve_regions,
    years_present,
)

SLA_LADDER = (1.0, 2.0, 4.0, 8.0, 16.0, 40.0)


def _load(settings: Settings):
    tickets, problems = load_tickets(settings.tickets)
    regions = resolve_regions(tickets)
    report = build_report(
        tickets, regions, settings.now, tuple(problems), holidays.__version__
    )
    return tickets, regions, report


def _region_or_die(name: str):
    calendar = calendar_for(name)
    if calendar is None:
        click.echo(f"unknown region {name!r}. known: {', '.join(sorted(REGISTRY))}", err=True)
        raise SystemExit(2)
    return calendar


@click.group()
def cli() -> None:
    """Business-hours SLA measurement, and the gap between the promise and the wait."""


@cli.command()
def doctor() -> None:
    """Config, calendars, and how to read the vocabulary."""
    settings = Settings()
    click.echo("CONFIG")
    click.echo(f"  tickets            {settings.tickets}")
    click.echo(f"  now                {settings.now.isoformat()}   (pinned, see config)")
    click.echo(f"  year               {settings.year}")
    click.echo(f"  sla_hours          {settings.sla_hours}")
    click.echo(f"  region             {settings.region}")
    click.echo(
        f"  long_wait_multiple {settings.long_wait_multiple or 'derived as 1 / open_fraction'}"
    )
    click.echo("")
    click.echo(f"HOLIDAY DATA: python-holidays {holidays.__version__}")
    click.echo("  Pinned to a minor range. The calendar for a PAST year can change between releases,")
    click.echo("  which would change a past SLA answer, so the version is printed with every report.")
    click.echo("")
    click.echo("CALENDARS")
    click.echo(f"  {'key':<13}{'region':<8}{'zone':<22}{'window':<14}{'days':<8}{'h/wk':>6}{'thresh':>8}")
    for key, calendar in sorted(REGISTRY.items()):
        days = "".join(
            "MTWTFSS"[index] if index in calendar.working_days else "-" for index in range(7)
        )
        window = f"{calendar.opens:%H:%M}-{calendar.closes:%H:%M}"
        click.echo(
            f"  {key:<13}{calendar.region:<8}{calendar.zone_name:<22}{window:<14}{days:<8}"
            f"{calendar.hours_per_week:>6.0f}{long_wait_threshold(calendar):>8.2f}"
        )
    click.echo("")
    click.echo("VERDICTS")
    click.echo("  met                 kept the promise, wait proportionate to it")
    click.echo("  met_with_long_wait  kept the promise, customer waited a multiple of it")
    click.echo("  breached            missed the promise")
    click.echo("  open                not resolved, so elapsed is a growing LOWER BOUND")
    click.echo("  unknown_start       no creation instant, so elapsed is bounded on ONE SIDE")
    click.echo("  no_calendar         region's calendar could not be resolved, so no claim is made")
    click.echo("")
    click.echo("Compliance is met/decided and measures the promise. It cannot express a long wait,")
    click.echo("because a long wait IS a success. Both numbers are reported side by side.")


@cli.command()
@click.option("--sliced", is_flag=True, help="Add the per-region table.")
@click.option("--format", "fmt", type=click.Choice(["text", "json", "csv"]), default="text")
@click.option("-o", "--out", type=click.Path(path_type=Path), help="Write report.json and tickets.csv.")
def report(sliced: bool, fmt: str, out: Path | None) -> None:
    """Full report: compliance and experience side by side."""
    settings = Settings()
    _, _, built = _load(settings)

    if fmt == "json":
        click.echo(report_json(built))
    elif fmt == "csv":
        click.echo(report_csv(built), nl=False)
    else:
        click.echo(report_text(built, sliced=sliced))

    if out is not None:
        json_path, csv_path = write_outputs(built, out)
        click.echo(f"\nwrote {json_path} and {csv_path}", err=True)

    if not built.is_clean:
        raise SystemExit(1)


@cli.command()
def check() -> None:
    """Gate on what is fixable: unreadable rows and unresolvable calendars."""
    settings = Settings()
    _, _, built = _load(settings)
    click.echo(check_text(built))
    blocking = any(result.verdict is Verdict.NO_CALENDAR for result in built.results)
    if built.problems or blocking:
        raise SystemExit(1)


@cli.command()
@click.option("--region", default=None, help="Support region to analyse.")
@click.option("--year", type=int, default=None)
def amplify(region: str | None, year: int | None) -> None:
    """The arithmetic: 1 + G/B, and what tightening the SLA actually buys."""
    settings = Settings()
    key = region or settings.region
    calendar = _region_or_die(key)
    span = year or settings.year
    _, regions, _ = _load(settings)
    resolved = regions.get(key)
    holiday_days = resolved.holidays if resolved and resolved.is_usable else frozenset()
    if not holiday_days:
        holiday_days = holiday_set(calendar, (span,))

    curve = sensitivity(calendar, holiday_days, SLA_LADDER, span)

    profiles = [
        amplification_profile(calendar, holiday_days, settings.sla_hours, span)
    ]
    for other_key in (key, "jp", "gb"):
        other = calendar_for(other_key)
        if other is None or any(p.arrival_region == other.region for p in profiles):
            continue
        profiles.append(
            amplification_profile(
                calendar,
                holiday_days,
                settings.sla_hours,
                span,
                arrival_calendar=other,
                arrival_holidays=holiday_set(other, (span,)),
            )
        )
    click.echo(amplify_text(curve, tuple(profiles)))


@cli.command()
@click.option("--region", default=None)
@click.option("--year", type=int, default=None)
def gaps(region: str | None, year: int | None) -> None:
    """The closed stretches in a year, longest first."""
    settings = Settings()
    key = region or settings.region
    calendar = _region_or_die(key)
    span = year or settings.year
    click.echo(gaps_text(gap_profile(calendar, holiday_set(calendar, (span,)), span)))


@cli.command()
@click.option("--year", type=int, default=None)
def transitions(year: int | None) -> None:
    """Whether DST can land inside a working window. Derived from tzdata, not hardcoded."""
    settings = Settings()
    del year
    distinct: dict[tuple, list[str]] = {}
    for key, calendar in sorted(REGISTRY.items()):
        if calendar.zone_name == "Etc/UTC":
            continue
        signature = (
            calendar.zone_name,
            calendar.opens,
            calendar.closes,
            tuple(sorted(calendar.working_days)),
        )
        distinct.setdefault(signature, []).append(key)

    risks = []
    for keys in distinct.values():
        calendar = REGISTRY[keys[0]]
        risk = transition_risk(calendar, settings.scan_first_year, settings.scan_last_year)
        risks.append(risk.model_copy(update={"region": ",".join(keys)}))
    risks = tuple(risks)
    window = (
        f"{settings.scan_first_year}-{settings.scan_last_year}, each calendar's own hours and days"
    )
    click.echo(transitions_text(risks, window))


@cli.command()
@click.option("--left", default="gb")
@click.option("--right", default="jp")
@click.option("--year", type=int, default=None)
def overlap(left: str, right: str, year: int | None) -> None:
    """Overlapping working hours between two regions, month by month."""
    settings = Settings()
    span = year or settings.year
    first, second = _region_or_die(left), _region_or_die(right)
    click.echo(f"WORKING-HOUR OVERLAP -- {first.label or left} and {second.label or right}")
    click.echo("")
    click.echo(f"  {'month':<10}{'overlap':>9}")
    monthly = []
    for month in range(1, 13):
        day = date(span, month, 15)
        while day.weekday() >= 5:
            day = date(span, month, day.day + 1)
        hours = overlap_hours(first, second, day)
        monthly.append(hours)
        click.echo(f"  {day.strftime('%B'):<10}{hours:>7.0f}h")
    click.echo("")
    low, high = min(monthly), max(monthly)
    click.echo(f"  range {low:.0f}h to {high:.0f}h across the year.")
    if low == 0 and high > 0:
        click.echo("")
        click.echo("  Zero for part of the year. A handoff between these two has no shared window at")
        click.echo("  all in those months, and the hours it does get exist only because one side's")
        click.echo("  summer time shifts its window against UTC -- so DST CREATES the only overlap")
        click.echo("  they have rather than merely disturbing it.")


@cli.command()
@click.option("--region", default=None)
def queue(region: str | None) -> None:
    """Tickets that share a business-time position while arriving hours or days apart."""
    settings = Settings()
    key = region or settings.region
    tickets, regions, _ = _load(settings)
    resolved = regions.get(key)
    if resolved is None:
        click.echo(f"no tickets for region {key!r}", err=True)
        raise SystemExit(2)
    grouped = group_by_region(tickets).get(key, [])
    years = years_present(grouped) or (settings.year,)
    epoch = datetime(min(years), 1, 1, tzinfo=ZoneInfo("UTC"))
    click.echo(queue_text(simultaneous_in_business_time(grouped, resolved, epoch)))


@cli.command()
@click.argument("ticket_id")
def explain(ticket_id: str) -> None:
    """One ticket, measured both ways, with the reasoning."""
    settings = Settings()
    tickets, regions, _ = _load(settings)
    match = next((item for item in tickets if item.ticket_id == ticket_id), None)
    if match is None:
        click.echo(f"no ticket {ticket_id!r} in {settings.tickets}", err=True)
        raise SystemExit(2)

    key = match.region.strip().lower()
    resolved = regions[key]
    result = evaluate(match, resolved, settings.now)

    click.echo(f"TICKET {match.ticket_id}   region {match.region}")
    if resolved.calendar:
        calendar = resolved.calendar
        days = "".join(
            "MTWTFSS"[index] if index in calendar.working_days else "-" for index in range(7)
        )
        click.echo(
            f"  calendar     {calendar.zone_name} {calendar.opens:%H:%M}-{calendar.closes:%H:%M} {days}"
        )
    click.echo(f"  coverage     {result.coverage.value}")
    click.echo(f"  SLA          {match.sla_business_hours:g} business hours")
    click.echo(f"  created      {match.created_at.isoformat() if match.created_at else '-- missing'}")
    if match.first_activity_at:
        click.echo(f"  first touch  {match.first_activity_at.isoformat()}")
    click.echo(f"  resolved     {match.resolved_at.isoformat() if match.resolved_at else '-- open'}")
    click.echo("")

    if result.elapsed:
        elapsed = result.elapsed
        click.echo(f"  business     {elapsed.business_hours:>8.2f}h   what the SLA measures")
        click.echo(f"  wall         {elapsed.wall_hours:>8.2f}h   what the customer waited")
        click.echo(f"  dead         {elapsed.dead_hours:>8.2f}h   calendar shut")
        if elapsed.amplification is None:
            click.echo("  amplification  undefined   no working time was consumed at all, so the")
            click.echo("                             ratio has no value -- 0 would read as 'no")
            click.echo("                             amplification', which is the opposite")
        else:
            click.echo(f"  amplification {elapsed.amplification:>7.1f}x")
    else:
        click.echo("  no elapsed time could be measured")
    click.echo("")

    if result.deadline:
        click.echo(f"  deadline     {result.deadline.isoformat()}")
    click.echo(f"  bound        {result.bound.value}")
    click.echo(f"  VERDICT      {result.verdict.value}")
    if result.note:
        click.echo(f"  {result.note}")

    if resolved.is_usable and match.effective_start is not None:
        calendar = resolved.calendar
        assert calendar is not None
        gap = dead_gap_containing(calendar, resolved.holidays, match.effective_start)
        click.echo("")
        if gap is None:
            click.echo("  arrived while the calendar was OPEN.")
        else:
            click.echo(
                f"  arrived inside a {gap.hours:.0f}h closed stretch ({gap.reason}), so the clock"
            )
            click.echo(f"  did not start until {gap.ends_at.isoformat()}")


@cli.command()
@click.option("--region", default=None)
@click.option("--sla", type=float, default=None)
def deadline(region: str | None, sla: float | None) -> None:
    """Show how a deadline moves with the arrival's position in the week."""
    settings = Settings()
    key = region or settings.region
    calendar = _region_or_die(key)
    hours = sla or settings.sla_hours
    holiday_days = holiday_set(calendar, (settings.year,))
    click.echo(f"A {hours:g}-BUSINESS-HOUR SLA, BY ARRIVAL -- {calendar.label or key}")
    click.echo("")
    click.echo(f"  {'arrival':<26}{'deadline':<26}{'wall':>8}{'amp':>8}")
    monday = datetime(settings.year, 3, 2, tzinfo=calendar.zone)
    assert monday.strftime("%a") == "Mon"
    for offset_days, hour in (
        (0, 9), (0, 14), (2, 16), (4, 9), (4, 16), (4, 17), (4, 18), (5, 10), (6, 20)
    ):
        arrival = (monday.replace(hour=hour)).replace(day=monday.day + offset_days)
        due = add_business_hours(calendar, holiday_days, arrival, hours)
        if due is None:
            continue
        wall = wall_hours(arrival, due)
        consumed = business_elapsed(calendar, holiday_days, arrival, due)
        amp = wall / consumed if consumed > 0 else None
        amp_text = "undefined" if amp is None else f"{amp:.1f}x"
        click.echo(
            f"  {arrival.strftime('%a %d %b %H:%M %Z'):<26}"
            f"{due.strftime('%a %d %b %H:%M %Z'):<26}{wall:>7.0f}h{amp_text:>8}"
        )
    click.echo("")
    click.echo("  Same promise, same calendar, and the customer's wait varies by an order of")
    click.echo("  magnitude on nothing but WHEN the ticket arrived. Business elapsed time is not")
    click.echo("  translation-invariant, so the phase of the arrival is part of the answer.")
    click.echo("")
    click.echo("  Note the last few rows share a deadline. Every arrival inside one closed stretch")
    click.echo("  gets the same one, because the projection onto working time cannot separate them.")


def main() -> None:
    try:
        cli(standalone_mode=False)
    except click.ClickException as err:
        err.show()
        sys.exit(err.exit_code)
    except SystemExit:
        raise
    except click.Abort:
        sys.exit(130)


if __name__ == "__main__":
    main()
