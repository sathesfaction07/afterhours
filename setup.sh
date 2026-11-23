#!/usr/bin/env bash
set -euo pipefail

BLUE='\033[0;34m'; GREEN='\033[0;32m'; RED='\033[0;31m'; NC='\033[0m'
info() { echo -e "${BLUE}==>${NC} $1"; }
ok()   { echo -e "${GREEN} ok ${NC} $1"; }
fail() { echo -e "${RED}fail${NC} $1"; exit 1; }

for arg in "$@"; do
    case "$arg" in
        -h|--help)
            cat <<'USAGE'
Usage: bash setup.sh

No options, no credentials, no network, no model. The accuracy problem here is calendar arithmetic,
plus a decision about which of two correct numbers to report.
USAGE
            exit 0 ;;
        *) fail "unknown option: $arg (try --help)" ;;
    esac
done

info "Checking Python"
command -v python3 >/dev/null 2>&1 || fail "python3 not found (macOS: brew install python@3.12)"
PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
[ "$(python3 -c 'import sys; print(1 if sys.version_info >= (3,11) else 0)')" = "1" ] \
    || fail "Python 3.11+ required (StrEnum, datetime.UTC). Found $PY_VER."
ok "Python $PY_VER"

info "Creating virtual environment"
[ -d .venv ] || python3 -m venv .venv || fail "could not create .venv"
ok ".venv ready"

info "Installing dependencies"
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -e ".[dev]" || fail "pip install failed"
ok "dependencies installed"

info "Verifying imports and timezone data"
.venv/bin/python - <<'PYCHECK' || fail "core imports failed"
from zoneinfo import ZoneInfo, available_timezones

import click                                        # noqa: F401
import holidays
from pydantic_settings import BaseSettings          # noqa: F401
from sla_watch.calendars import BusinessCalendar    # noqa: F401
from sla_watch.clock import business_elapsed        # noqa: F401

zones = available_timezones()
assert len(zones) > 300, f"only {len(zones)} zones -- tzdata looks incomplete"
for required in ("Europe/London", "Asia/Tokyo", "Africa/Cairo", "Asia/Jerusalem"):
    assert required in zones, f"missing zone {required}"
    ZoneInfo(required)
print(f"     {len(zones)} zones available, python-holidays {holidays.__version__}")
PYCHECK
ok "imports resolve and tzdata is present"

info "Generating the corpus"
[ -f data/tickets.jsonl ] || .venv/bin/python data/generate.py || fail "could not generate data"
ok "corpus present"

info "Deriving the asymmetry the project rests on"
.venv/bin/python - <<'PYCHECK' || fail "the asymmetry does not hold"
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sla_watch.calendars import BusinessCalendar, holiday_set
from sla_watch.clock import business_elapsed, wall_hours

LONDON = ZoneInfo("Europe/London")
gb = BusinessCalendar(region="GB", zone_name="Europe/London", subdivision="ENG")
days = holiday_set(gb, (2026,))

span = timedelta(hours=48)
monday = datetime(2026, 3, 2, 9, 0, tzinfo=LONDON)
friday = datetime(2026, 3, 6, 17, 0, tzinfo=LONDON)

print("     The SAME 48-hour span, moved four days later:")
print(f"       from Monday 09:00  business {business_elapsed(gb, days, monday, monday + span):>5.1f}h"
      f"   wall {wall_hours(monday, monday + span):>5.1f}h")
print(f"       from Friday 17:00  business {business_elapsed(gb, days, friday, friday + span):>5.1f}h"
      f"   wall {wall_hours(friday, friday + span):>5.1f}h")
print()
print("     Wall time is translation-invariant. Business time is NOT, so the phase of the arrival")
print("     is part of the answer -- and an SLA in business hours measures OUR EFFORT rather than")
print("     the customer's wait.")

assert business_elapsed(gb, days, monday, monday + span) == 16.0
assert business_elapsed(gb, days, friday, friday + span) == 0.0
assert wall_hours(monday, monday + span) == wall_hours(friday, friday + span) == 48.0
PYCHECK
ok "business time is not translation-invariant, and wall time is"

info "Deriving the amplification arithmetic, and what tightening the SLA buys"
.venv/bin/python - <<'PYCHECK' || fail "the amplification arithmetic does not hold"
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sla_watch.amplify import worst_amplification
from sla_watch.calendars import UTC, BusinessCalendar, holiday_set
from sla_watch.clock import add_business_hours, dead_gap_containing, wall_hours

LONDON = ZoneInfo("Europe/London")
gb = BusinessCalendar(region="GB", zone_name="Europe/London", subdivision="ENG")
days = holiday_set(gb, (2026,))
friday = datetime(2026, 3, 6, 17, 0, tzinfo=LONDON)

gap = dead_gap_containing(gb, days, datetime(2026, 3, 6, 18, 0, tzinfo=LONDON))
print(f"     A ticket arriving Friday 17:00. Dead gap to Monday 09:00 = {gap.hours:.0f}h.")
print()
print(f"     {'SLA':>5}{'worst wait':>13}{'amplification':>16}{'1 + G/B':>11}")
for sla in (1, 2, 4, 8, 40):
    due = add_business_hours(gb, days, friday, sla)
    wall = wall_hours(friday, due)
    closed = worst_amplification(gap.hours, sla)
    marker = "" if sla <= 8 else "   <- G/B underestimates past one working day"
    print(f"     {sla:>5}{wall:>12.0f}h{wall / sla:>15.1f}x{closed:>10.1f}x{marker}")
    if sla <= 8:
        assert abs(wall / sla - closed) < 1e-9, (sla, wall / sla, closed)

tight = wall_hours(friday, add_business_hours(gb, days, friday, 1))
loose = wall_hours(friday, add_business_hours(gb, days, friday, 8))
print()
print(f"     Cutting the promise 8x (8h -> 1h) improves the worst WAIT from {loose:.0f}h to "
      f"{tight:.0f}h, a {1 - tight / loose:.1%} gain,")
print(f"     and makes the worst AMPLIFICATION {tight / 1 / (loose / 8):.1f}x worse.")
print()
print("     G belongs to the CALENDAR and B is the promise, so the promise scales the ratio and")
print("     cannot shrink the constant. That is the whole finding, and it is arithmetic.")
assert tight == 65.0 and loose == 72.0
assert (1 - tight / loose) < 0.12, "the wait must barely improve"

# Brute force: is the longest gap really the worst arrival?
best = 0.0
cursor, end = datetime(2026, 1, 1, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC)
while cursor < end:
    due = add_business_hours(gb, days, cursor, 8.0)
    if due is not None:
        best = max(best, wall_hours(cursor, due) / 8.0)
    cursor += timedelta(hours=1)
print()
print(f"     Brute-forced over all 8760 arrivals, the worst 8-hour amplification is {best:.1f}x")
print("     -- reached across the Easter shutdown rather than an ordinary weekend.")
assert best > 9.0
PYCHECK
ok "the arithmetic matches the closed form and the brute-force worst case"

info "Deriving the DST immunity -- and that it is NOT the code's doing"
.venv/bin/python - <<'PYCHECK' || fail "the DST immunity does not hold as documented"
from collections import Counter
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, available_timezones

from sla_watch.calendars import UTC, BusinessCalendar, transition_risk


def transitions(zone_name, y0, y1):
    zone = ZoneInfo(zone_name)
    out, step = [], timedelta(hours=1)
    t, end = datetime(y0, 1, 1, tzinfo=UTC), datetime(y1 + 1, 1, 1, tzinfo=UTC)
    prev = t.astimezone(zone).utcoffset()
    while t < end:
        t += step
        off = t.astimezone(zone).utcoffset()
        if off != prev:
            out.append(t.astimezone(zone))
            prev = off
    return out


zones = sorted(
    z for z in available_timezones() if "/" in z and not z.startswith(("Etc/", "SystemV"))
)
allt = []
for zone in zones:
    try:
        allt.extend(transitions(zone, 2024, 2028))
    except Exception:
        pass

hours = Counter(t.hour for t in allt)
weekdays = Counter(t.strftime("%a") for t in allt)
inside = [t for t in allt if t.weekday() < 5 and time(9) <= t.time() < time(17)]

print(f"     {len(allt)} transitions across {len(zones)} zones, 2024-2028")
print(f"       local hour:    {dict(sorted(hours.items()))}")
print(f"       weekday:       {dict(weekdays)}")
print(f"       inside Mon-Fri 09:00-17:00: {len(inside)}")
assert len(allt) > 500
assert not inside, f"a transition landed in business hours: {inside[:3]}"

print()
print("     Note the weekday spread: transitions are NOT all Sunday. Israel moves on a FRIDAY and")
print("     Egypt on a Friday and a Thursday, so 'DST is always on a Sunday' is folklore.")
assert set(weekdays) - {"Sun"}, "the counterexamples must be present"

print()
print("     There are TWO independent protections, and losing one is survivable:")
week7 = frozenset(range(7))


def risk(opens, closes, days=None, zone="Europe/London"):
    kwargs = {"region": "GB", "zone_name": zone, "opens": opens, "closes": closes}
    if days is not None:
        kwargs["working_days"] = days
    return transition_risk(BusinessCalendar(**kwargs), 2015, 2031)


print(f"       {'window':<32}{'scanned':>9}{'inside':>8}")
cases = (
    ("daytime 09:00-17:00  Mon-Fri", time(9), time(17), None),
    ("daytime 09:00-17:00  7 days", time(9), time(17), week7),
    ("night   00:30-08:00  Mon-Fri", time(0, 30), time(8), None),
    ("night   00:30-08:00  7 days", time(0, 30), time(8), week7),
)
results = {}
for label, opens, closes, days in cases:
    got = risk(opens, closes, days)
    results[label] = got
    print(f"       {label:<32}{got.transitions_scanned:>9}{got.inside_window:>8}")

assert results["daytime 09:00-17:00  7 days"].is_immune, "the HOUR protects on its own"
assert results["night   00:30-08:00  Mon-Fri"].is_immune, "the DAY protects on its own"
assert not results["night   00:30-08:00  7 days"].is_immune, "both gone means no immunity"

print()
print("     So a Monday-Friday daytime calendar never sees a transition inside its window, and that")
print("     belongs to the SCHEDULE rather than to any care taken in the code. No DST handling was")
print("     built for the inside-the-window case because it cannot fire -- and the immunity is")
print("     asserted from tzdata so the build fails if that ever stops being true.")
print()
jerusalem_night = risk(time(0, 30), time(8), None, "Asia/Jerusalem")
print(f"     And it is calendar-specific: Asia/Jerusalem on a night shift sees "
      f"{jerusalem_night.inside_window} inside,")
print("     because its transitions are on Fridays and a Monday-Friday week does not exclude them.")
assert jerusalem_night.inside_window > 0
PYCHECK
ok "the immunity is derived from tzdata, and its two causes are separated"

info "Demonstrating the bug this project shipped, and that it is fixed"
.venv/bin/python - <<'PYCHECK' || fail "the elapsed-time fix is not in place"
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sla_watch.calendars import BusinessCalendar, holiday_set
from sla_watch.clock import dead_gap_containing, wall_hours
from sla_watch.models import elapsed_hours

LONDON = ZoneInfo("Europe/London")
before = datetime(2026, 3, 28, 12, 0, tzinfo=LONDON)
after = datetime(2026, 3, 30, 12, 0, tzinfo=LONDON)

print("     Python differences two aware datetimes that SHARE a tzinfo by their naive wall-clock")
print("     values and ignores the offsets. Documented, and silent:")
print()
print(f"       plain subtraction        {(after - before).total_seconds() / 3600:>5.1f}h")
print(f"       via UTC (real elapsed)   {elapsed_hours(before, after):>5.1f}h")
print(f"       offsets differ:          {before.utcoffset()} -> {after.utcoffset()}")
assert (after - before) == timedelta(hours=48)
assert elapsed_hours(before, after) == 47.0

print()
print("     Worse, the answer depended on how the timestamp was BUILT. `isoformat` bakes a FIXED")
print("     offset, and two different fixed offsets subtract correctly:")
a2 = datetime.fromisoformat(before.isoformat())
b2 = datetime.fromisoformat(after.isoformat())
print(f"       same instants, ZoneInfo spelling      {(after - before).total_seconds() / 3600:>5.1f}h")
print(f"       same instants, fixed-offset spelling  {(b2 - a2).total_seconds() / 3600:>5.1f}h")
assert before == a2 and after == b2
assert (after - before) != (b2 - a2)
print("     One instant, two spellings, two answers, no error. The shipped corpus round-trips")
print("     through isoformat, so it produced the RIGHT number while the code was wrong.")

print()
gb = BusinessCalendar(region="GB", zone_name="Europe/London", subdivision="ENG")
days = holiday_set(gb, (2026,))
spring = dead_gap_containing(gb, days, datetime(2026, 3, 28, 12, 0, tzinfo=LONDON))
autumn = dead_gap_containing(gb, days, datetime(2026, 10, 24, 12, 0, tzinfo=LONDON))
ordinary = dead_gap_containing(gb, days, datetime(2026, 3, 7, 12, 0, tzinfo=LONDON))
print("     What it cost, on the number the headline is computed from:")
print(f"       {'weekend':<28}{'reported now':>14}{'raw subtraction':>18}")
for label, gap in (("ordinary", ordinary), ("spring forward", spring), ("clocks go back", autumn)):
    raw = (gap.ends_at - gap.starts_at).total_seconds() / 3600
    print(f"       {label:<28}{gap.hours:>13.0f}h{raw:>17.0f}h")
assert ordinary.hours == 64.0
assert spring.hours == 63.0, "an hour that never existed"
assert autumn.hours == 65.0
print()
print("     The errors run in BOTH directions, so they do not cancel and neither shows up as a")
print("     consistent bias. And the longest London weekend of 2026 is the 65-hour one where the")
print("     clocks go back, which the old code reported as an ordinary 64.")
assert wall_hours(before, after) == 47.0
PYCHECK
ok "same-zone subtraction is no longer mistaken for elapsed time"

info "Measuring the hypothesis I got wrong"
.venv/bin/python - <<'PYCHECK' || fail "the cohort measurement does not behave as documented"
from sla_watch.amplify import amplification_profile
from sla_watch.calendars import BusinessCalendar, holiday_set
from datetime import time

gb = BusinessCalendar(region="GB", zone_name="Europe/London", subdivision="ENG")
jp = BusinessCalendar(region="JP", zone_name="Asia/Tokyo", opens=time(9), closes=time(18))
hgb, hjp = holiday_set(gb, (2026,)), holiday_set(jp, (2026,))

print("     I expected a London desk serving TOKYO customers to be the worst cohort, because")
print("     almost every such arrival lands while London is shut. Measured:")
print()
print(f"     {'arrivals from':<16}{'n':>6}{'shut on arrival':>17}{'mean dead h':>13}{'mean wait':>11}{'over 4x':>9}")
rows = {}
for label, cal, hol in (("uniform", None, None), ("GB customers", gb, hgb), ("JP customers", jp, hjp)):
    p = amplification_profile(gb, hgb, 8.0, 2026, arrival_calendar=cal, arrival_holidays=hol)
    rows[label] = p
    print(f"     {label:<16}{p.samples:>6}{p.gap_share:>16.1%}{p.mean_dead_hours:>13.1f}"
          f"{p.mean_wall_hours:>10.1f}h{p.over_4x_share:>9.1%}")

own, distant = rows["GB customers"], rows["JP customers"]
assert own.gap_share == 0.0
assert distant.gap_share > 0.9
assert distant.mean_wall_hours < own.mean_wall_hours / 2

print()
print("     It is the BEST of the three, and the two columns are close to anti-correlated: 0.0%")
print("     shut on arrival with 23 dead hours, against 93.8% shut with 6. Being closed at the")
print("     moment of arrival is not what amplifies -- dead time inside the DEADLINE WINDOW is.")
print("     Tokyo lands in London's short OVERNIGHT gap and then gets a full working day.")
PYCHECK
ok "the refuted hypothesis is reproduced, with the mechanism"

info "Checking the shipped corpus behaves as documented"
.venv/bin/python - <<'PYCHECK' || fail "the corpus did not behave as documented"
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import holidays
from sla_watch.measure import build_report, simultaneous_in_business_time
from sla_watch.models import Coverage, Verdict
from sla_watch.tickets import group_by_region, load_tickets, resolve_regions

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=ZoneInfo("UTC"))
tickets, problems = load_tickets(Path("data/tickets.jsonl"))
regions = resolve_regions(tickets)
report = build_report(tickets, dict(regions), NOW, tuple(problems), holidays.__version__)

print(f"     {len(tickets)} tickets, {len(problems)} deliberately broken rows reported:")
for problem in problems:
    print(f"       {problem}")
assert len(problems) == 2

missing = {v.value for v in Verdict} - set(report.counts)
assert not missing, f"corpus never produces {sorted(missing)}"
print()
print("     every Verdict value occurs, so no assertion about one is vacuous:")
for verdict in Verdict:
    print(f"       {verdict.value:<20}{report.counts.get(verdict.value, 0):>4}")

seen = {s.coverage for s in report.regions}
assert seen == set(Coverage), f"missing {sorted(set(Coverage) - seen)}"
print()
print(f"     COMPLIANCE  {report.compliance:.1%} over {report.decided} decided, {report.refused} refused")
print(f"     EXPERIENCE  {report.hidden_long_waits} met with a long wait, worst "
      f"{report.worst_amplification:.1f}x, {report.undefined_amplification} undefined ratio")
print()
print(f"     {'region':<13}{'n':>4}{'compl':>8}{'long':>6}{'looks perfect':>15}  coverage")
for summary in report.regions:
    compliance = "n/a" if summary.compliance is None else f"{summary.compliance:.0%}"
    print(f"     {summary.region:<13}{summary.tickets:>4}{compliance:>8}{summary.hidden_long_waits:>6}"
          f"{str(summary.looks_perfect):>15}  {summary.coverage.value}")

perfect = report.regions_looking_perfect
assert perfect, "the signature this project exists to surface must occur"
assert report.compliance < 1.0
print()
print(f"     {len(perfect)} region(s) report 100% compliance AND a customer who waited past the")
print(f"     threshold: {', '.join(perfect)}. The fleet figure is {report.compliance:.1%}, so the")
print("     aggregate would prompt a look while those regions individually look finished.")

worst = max((r for r in report.results if r.amplification is not None), key=lambda r: r.amplification)
print()
print(f"     The single worst customer experience in the corpus is {worst.ticket_id} at "
      f"{worst.amplification:.1f}x")
print(f"     ({worst.elapsed.business_hours:.1f} business hours, {worst.elapsed.wall_hours:.0f} wall)"
      f" -- and its verdict is {worst.verdict.value.upper()}.")
assert worst.verdict is Verdict.MET_WITH_LONG_WAIT

groups = simultaneous_in_business_time(
    group_by_region(tickets)["gb"], regions["gb"], datetime(2026, 1, 1, tzinfo=ZoneInfo("UTC"))
)
widest = max(groups, key=lambda g: g.spread_hours)
print()
print(f"     {len(groups)} group(s) share a business-time position. The widest holds "
      f"{widest.size} tickets")
print(f"     spread over {widest.spread_hours:.0f} real hours: {', '.join(widest.ticket_ids)}.")
print("     A queue ordered by business-hours-aged reports them as equally old.")
assert widest.spread_hours >= 48
PYCHECK
ok "the corpus behaves as documented"

info "Running the test suite"
.venv/bin/python -m pytest -q || fail "tests failed"
ok "tests pass"

cat <<'DONE'

Setup complete.

  source .venv/bin/activate

  afterhours doctor
  afterhours check                  # exits 1: two unreadable rows, and 5 tickets with no calendar
  afterhours report --sliced        # compliance and experience, per region
  afterhours amplify                # 1 + G/B, and the cohort table that refuted my hypothesis
  afterhours deadline               # one promise, waits varying by an order of magnitude
  afterhours gaps                   # the closed stretches, including the 65-hour weekend
  afterhours transitions            # the DST immunity, derived from tzdata
  afterhours overlap                # London and Tokyo share 0 hours in winter, 1 in summer
  afterhours queue                  # tickets simultaneous in business time
  afterhours explain GB-1002        # one ticket, measured both ways

`check` fails on unreadable rows and on tickets whose calendar cannot be resolved, because both are
fixable at source. It does NOT fail on a long wait or a missing creation date: those are properties
of the world, and a gate that fires whenever someone reports a fault on a Friday afternoon gets
switched off inside a week.
DONE
