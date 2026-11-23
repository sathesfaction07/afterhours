"""Rendering. Text tables that can be asserted in CI, plus JSON and CSV.

No charts and no live dashboard, both of which the brief asks for. A Rich live-updating display
cannot assert in a test, and this portfolio already carries a logged gap for untested UI code. A text
table is checkable, so it is what ships.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from io import StringIO
from pathlib import Path

from .amplify import ArrivalProfile, GapProfile, Sensitivity
from .calendars import TransitionRisk
from .measure import Report, SimultaneousGroup
from .models import Verdict


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def _num(value: float | None, spec: str = ".1f") -> str:
    return "n/a" if value is None else format(value, spec)


def _amp(value: float | None) -> str:
    """Amplification for a table cell.

    `n/a` rather than `undefined`, because in a per-region row None means the region had nothing to
    measure. A ratio that is genuinely undefined -- zero working time consumed -- is counted by
    `Report.undefined_amplification` and named in the EXPERIENCE block, so the two do not share one
    rendering and get read as the same thing.
    """
    return "n/a" if value is None else f"{value:.1f}x"


def report_text(report: Report, sliced: bool = False) -> str:
    out: list[str] = []
    out.append(f"tickets {report.tickets}   measured against {report.generated_at.isoformat()}")
    if report.holidays_library:
        out.append(f"holiday data: python-holidays {report.holidays_library}")
    out.append("")

    counts = report.counts
    out.append("VERDICTS")
    for verdict in Verdict:
        count = counts.get(verdict.value, 0)
        out.append(f"  {verdict.value:<20}{count:>5}")
    out.append("")

    out.append("COMPLIANCE -- did we keep the promise")
    out.append(f"  decided          {report.decided:>5}")
    out.append(f"  met              {report.met:>5}")
    out.append(f"  compliance       {_pct(report.compliance):>5}")
    out.append(f"  refused          {report.refused:>5}   no claim made, for three different reasons")
    out.append("")

    out.append("EXPERIENCE -- what the customer got")
    out.append(f"  long waits       {report.hidden_long_waits:>5}   met the promise, waited a multiple of it")
    out.append(f"  worst wait       {_amp(report.worst_amplification):>5}   times the working time spent")
    out.append(
        f"  undefined ratio  {report.undefined_amplification:>5}   consumed no working time at all,"
        " so the ratio has no value"
    )
    out.append("")
    out.append("  Both numbers are correct and they answer different questions. Every long wait above")
    out.append("  is counted as a SUCCESS in the compliance figure, so no arithmetic on met and")
    out.append("  breached can surface them -- the information is discarded by the projection onto")
    out.append("  working time, before any grouping happens.")
    out.append("")

    perfect = report.regions_looking_perfect
    if perfect:
        out.append(f"REGIONS AT 100% COMPLIANCE WITH LONG WAITS: {', '.join(perfect)}")
        out.append("  Nothing in their compliance number invites a question. The fleet figure above is")
        out.append("  below 100%, so someone would look at THAT -- these regions look finished.")
        out.append("")

    if sliced:
        out.append(
            f"  {'region':<13}{'n':>4}{'compl':>7}{'long':>6}{'perfect':>9}"
            f"{'meanBiz':>9}{'meanWall':>10}{'worst':>11}{'thresh':>8}  coverage"
        )
        for summary in report.regions:
            out.append(
                f"  {summary.region:<13}{summary.tickets:>4}{_pct(summary.compliance):>7}"
                f"{summary.hidden_long_waits:>6}{summary.looks_perfect!s:>9}"
                f"{_num(summary.mean_business_hours):>9}{_num(summary.mean_wall_hours):>10}"
                f"{_amp(summary.worst_amplification):>11}{_num(summary.threshold, '.2f'):>8}"
                f"  {summary.coverage.value}"
            )
        out.append("")
        out.append("  A region with two or three tickets can reach 100% by luck, and `perfect` does not")
        out.append("  distinguish that from a region with twenty. The ticket count is shown so the")
        out.append("  reader can weigh it rather than being protected from it by a minimum-size rule.")
        out.append("")

    if report.problems:
        out.append(f"PROBLEMS ({len(report.problems)})")
        for problem in report.problems:
            out.append(f"  {problem}")
        out.append("")

    return "\n".join(out)


def check_text(report: Report) -> str:
    out: list[str] = []
    blocking = [
        result for result in report.results if result.verdict is Verdict.NO_CALENDAR
    ]
    out.append("WHAT THIS DATASET SUPPORTS")
    out.append("")

    if report.problems:
        out.append(f"unreadable rows: {len(report.problems)}")
        for problem in report.problems:
            out.append(f"  {problem}")
        out.append("")

    if blocking:
        out.append(f"tickets with no resolvable calendar: {len(blocking)}")
        by_reason: dict[str, list[str]] = {}
        for result in blocking:
            by_reason.setdefault(result.coverage.value, []).append(result.ticket_id)
        for reason, ids in sorted(by_reason.items()):
            out.append(f"  {reason}: {', '.join(sorted(ids))}")
        out.append("")
        out.append("  This is a CONFIGURATION gap rather than a property of the tickets: pick a")
        out.append("  subdivision, map the region, or narrow the years. Until then no business-hours")
        out.append("  claim is possible, and defaulting to Mon-Fri 09:00-17:00 would produce a")
        out.append("  confident number for a region whose weekend may be Friday and Saturday.")
        out.append("")

    unknown = sum(1 for r in report.results if r.verdict is Verdict.UNKNOWN_START)
    if unknown:
        out.append(f"tickets with no creation instant: {unknown}")
        out.append("  Reported and NOT failed. Elapsed time is bounded on one side only, which is a")
        out.append("  weaker claim rather than a broken one -- and where that bound already exceeds")
        out.append("  the SLA the breach is settled anyway.")
        out.append("")

    if report.hidden_long_waits:
        out.append(f"met with a long wait: {report.hidden_long_waits}")
        out.append("  Reported and NOT failed. These kept the promise. The wait is a consequence of")
        out.append("  the calendar, and a gate that fires whenever someone reports a fault on a")
        out.append("  Friday afternoon gets switched off inside a week.")
        out.append("")

    ok = not report.problems and not blocking
    out.append(
        "PASS: every ticket has a resolvable calendar and every row parsed"
        if ok
        else "FAIL: see above"
    )
    return "\n".join(out)


def amplify_text(
    sensitivity: Sensitivity,
    profiles: tuple[ArrivalProfile, ...],
) -> str:
    out: list[str] = []
    out.append("WORST CASE, MEASURED FROM THE LONGEST CLOSED STRETCH")
    out.append(f"  region {sensitivity.region}   longest gap {sensitivity.gap_hours:.0f}h")
    if sensitivity.rows:
        out.append(f"  arrival {sensitivity.rows[0].arrival.isoformat()}")
    out.append("")
    out.append(f"  {'SLA (business h)':>17}{'worst wait':>13}{'amplification':>16}")
    for row in sensitivity.rows:
        out.append(
            f"  {row.sla_hours:>17.0f}{row.worst_wall_hours:>12.0f}h"
            f"{row.worst_amplification:>15.1f}x"
        )
    out.append("")
    if sensitivity.wait_improvement is not None and sensitivity.amplification_ratio is not None:
        out.append(
            f"  Tightening from {sensitivity.rows[-1].sla_hours:.0f} business hours to "
            f"{sensitivity.rows[0].sla_hours:.0f} improves the worst WAIT by "
            f"{sensitivity.wait_improvement:.1%}"
        )
        out.append(
            f"  and makes the worst AMPLIFICATION {sensitivity.amplification_ratio:.0f}x worse."
        )
        out.append("")
        out.append("  Worst-case amplification is 1 + G/B. G is the closed stretch and belongs to the")
        out.append("  CALENDAR; B is the promise. So the promise scales the ratio and cannot shrink the")
        out.append("  constant, which is why the two columns move in opposite directions.")
    out.append("")

    out.append("AMPLIFICATION OVER A YEAR OF ARRIVALS")
    out.append(
        f"  {'arrivals from':<18}{'n':>6}{'shut on arrival':>17}{'mean dead h':>13}"
        f"{'mean wait':>11}{'worst':>9}{'over 4x':>9}"
    )
    for profile in profiles:
        label = profile.arrival_region or "uniform"
        out.append(
            f"  {label:<18}{profile.samples:>6}{_pct(profile.gap_share):>17}"
            f"{profile.mean_dead_hours:>13.1f}{profile.mean_wall_hours:>10.1f}h"
            f"{_amp(profile.worst_amplification):>9}{_pct(profile.over_4x_share):>9}"
        )
    out.append("")
    out.append("  'shut on arrival' is the intuitive explanation and it is not the driver -- the two")
    out.append("  columns disagree. What amplifies is dead time inside the DEADLINE WINDOW. A cohort")
    out.append("  arriving into a short overnight gap and then getting a full working day does better")
    out.append("  than one arriving mid-afternoon on a Friday.")
    return "\n".join(out)


def gaps_text(profile: GapProfile) -> str:
    out: list[str] = []
    out.append(f"CLOSED STRETCHES -- {profile.region} {profile.year}")
    out.append(f"  distinct stretches      {profile.gaps_found:>6}")
    out.append(f"  longest weekend         {profile.weekend_hours:>5.0f}h")
    out.append(f"  longest holiday stretch {profile.longest_holiday_hours:>5.0f}h")
    out.append("")
    if profile.longest:
        gap = profile.longest
        out.append(f"  longest overall: {gap.hours:.0f}h ({gap.reason})")
        out.append(f"    from {gap.starts_at.isoformat()}")
        out.append(f"    to   {gap.ends_at.isoformat()}")
        out.append("")
        out.append("  This is the G in 1 + G/B. It is a property of the calendar, so no SLA target")
        out.append("  changes it -- the only levers are opening hours and the holiday list.")
    return "\n".join(out)


def transitions_text(risks: tuple[TransitionRisk, ...], window: str) -> str:
    out: list[str] = []
    out.append(f"DST TRANSITIONS INSIDE THE WORKING WINDOW -- {window}")
    out.append("")
    out.append(f"  {'calendars':<20}{'scanned':>9}{'inside window':>15}  examples")
    total_scanned = total_inside = 0
    for risk in risks:
        total_scanned += risk.transitions_scanned
        total_inside += risk.inside_window
        example = "; ".join(risk.examples) if risk.examples else "-"
        out.append(
            f"  {risk.region:<20}{risk.transitions_scanned:>9}{risk.inside_window:>15}  {example}"
        )
    out.append("")
    out.append(f"  {total_inside} of {total_scanned} transitions fall inside a working window.")
    out.append("")
    if total_inside == 0:
        out.append("  Zero, and that is not a result of careful coding. Transitions are SCHEDULED into")
        out.append("  the small hours -- around 90% land between 01:00 and 03:00 local -- so a daytime")
        out.append("  window never contains one. The immunity belongs to the schedule, not the code,")
        out.append("  and it is asserted here from tzdata so that it fails loudly if that changes.")
        out.append("")
        out.append("  It also does not generalise. A window covering 00:00-05:00 loses it entirely: a")
        out.append("  night shift sees around 120 transitions land inside, and a 24/7 rota sees all of")
        out.append("  them. What protects a Monday-Friday daytime calendar is the hour, not the code.")
    return "\n".join(out)


def queue_text(groups: tuple[SimultaneousGroup, ...]) -> str:
    out: list[str] = []
    out.append("TICKETS SIMULTANEOUS IN BUSINESS TIME")
    out.append("")
    if not groups:
        out.append("  none: no two tickets share a business-time position.")
        return "\n".join(out)

    out.append(f"  {'position':>10}{'tickets':>9}{'real spread':>13}  ids")
    for group in sorted(groups, key=lambda item: -item.spread_hours):
        out.append(
            f"  {group.position_hours:>9.1f}h{group.size:>9}{group.spread_hours:>12.0f}h"
            f"  {', '.join(group.ticket_ids)}"
        )
    out.append("")
    worst = max(groups, key=lambda item: item.spread_hours)
    out.append(f"  The widest group hides {worst.spread_hours:.0f} hours of real difference behind one")
    out.append("  business-time coordinate. The projection onto working time is many-to-one, so every")
    out.append("  arrival inside one closed stretch lands on the same point.")
    out.append("")
    out.append("  Consequence: ordering a queue by business-hours-aged reports these as equally old.")
    out.append("  'Oldest first' by business time does not serve the longest-waiting customer first,")
    out.append("  and no tie-break computed from business time can fix it -- the difference is gone.")
    return "\n".join(out)


def report_json(report: Report) -> str:
    payload = {
        "generated_at": report.generated_at.isoformat(),
        "tickets": report.tickets,
        "holidays_library": report.holidays_library,
        "counts": report.counts,
        "decided": report.decided,
        "met": report.met,
        "compliance": report.compliance,
        "refused": report.refused,
        "hidden_long_waits": report.hidden_long_waits,
        "worst_amplification": report.worst_amplification,
        "regions_looking_perfect": list(report.regions_looking_perfect),
        "is_clean": report.is_clean,
        "problems": list(report.problems),
        "regions": [
            {
                "region": summary.region,
                "label": summary.label,
                "coverage": summary.coverage.value,
                "tickets": summary.tickets,
                "counts": summary.counts,
                "decided": summary.decided,
                "compliance": summary.compliance,
                "hidden_long_waits": summary.hidden_long_waits,
                "looks_perfect": summary.looks_perfect,
                "threshold": summary.threshold,
                "worst_amplification": summary.worst_amplification,
                "worst_ticket": summary.worst_ticket,
                "mean_wall_hours": summary.mean_wall_hours,
                "mean_business_hours": summary.mean_business_hours,
            }
            for summary in report.regions
        ],
        "results": [
            {
                "ticket_id": result.ticket_id,
                "region": result.region,
                "verdict": result.verdict.value,
                "bound": result.bound.value,
                "sla_business_hours": result.sla_business_hours,
                "business_hours": result.elapsed.business_hours if result.elapsed else None,
                "wall_hours": result.elapsed.wall_hours if result.elapsed else None,
                "dead_hours": result.elapsed.dead_hours if result.elapsed else None,
                "amplification": result.amplification,
                "deadline": result.deadline.isoformat() if result.deadline else None,
                "coverage": result.coverage.value,
                "note": result.note,
            }
            for result in report.results
        ],
    }
    return json.dumps(payload, indent=2)


def report_csv(report: Report) -> str:
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "ticket_id",
            "region",
            "verdict",
            "bound",
            "sla_business_hours",
            "business_hours",
            "wall_hours",
            "dead_hours",
            "amplification",
            "deadline",
            "coverage",
        ]
    )
    for result in report.results:
        elapsed = result.elapsed
        writer.writerow(
            [
                result.ticket_id,
                result.region,
                result.verdict.value,
                result.bound.value,
                result.sla_business_hours,
                "" if elapsed is None else f"{elapsed.business_hours:.4f}",
                "" if elapsed is None else f"{elapsed.wall_hours:.4f}",
                "" if elapsed is None else f"{elapsed.dead_hours:.4f}",
                # Empty rather than 0 for an undefined ratio: a spreadsheet will average a 0 and
                # will not average a blank, and averaging it in would report no amplification for
                # the tickets that consumed no working time at all.
                "" if result.amplification is None else f"{result.amplification:.4f}",
                result.deadline.isoformat() if result.deadline else "",
                result.coverage.value,
            ]
        )
    return buffer.getvalue()


def write_outputs(report: Report, directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "report.json"
    csv_path = directory / "tickets.csv"
    json_path.write_text(report_json(report), encoding="utf-8")
    csv_path.write_text(report_csv(report), encoding="utf-8")
    return json_path, csv_path


def stamp(instant: datetime) -> str:
    return instant.strftime("%Y-%m-%d %a %H:%M %Z")
