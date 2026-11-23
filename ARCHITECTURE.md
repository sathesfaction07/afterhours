# Afterhours — Technical Architecture

## Overview

Measures business-hours SLAs, and reports the customer's wait as a separate number because the SLA
cannot express it.

**Design principle:** wall-clock elapsed time is translation-invariant; business elapsed time is not.
The same 48-hour span consumes 16 business hours from Monday morning and 0 from Friday evening, so a
promise in business hours measures OUR EFFORT and says nothing bounded about the customer's wait.

No model. The accuracy problem is calendar arithmetic, plus a decision about which of two correct
numbers to report.

---

## System Architecture

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                          CLI (afterhours)                                      │
│                                                                              │
│  check       EXIT 1 on an unreadable row or an unresolvable calendar, because  │
│              both are fixable at source. Does NOT fail on a long wait or a     │
│              missing creation date -- those are properties of the world, and a  │
│              gate firing whenever someone reports a fault on a Friday          │
│              afternoon gets switched off inside a week                         │
│  report      compliance and experience side by side; --sliced per region       │
│              EXIT 1 when the dataset carries a caveat, so a pipeline opts in    │
│  amplify     1 + G/B, the sensitivity table, and the arrival cohorts           │
│  deadline    one promise, waits varying by an order of magnitude on phase       │
│  gaps        the closed stretches; G in the arithmetic                         │
│  transitions whether DST can enter a working window, derived from tzdata        │
│  overlap     shared working hours between two regions, month by month          │
│  queue       tickets simultaneous in business time                             │
│  explain     one ticket, both measurements, and the gap it arrived in          │
│  doctor      config, calendars, holiday library version, the vocabulary        │
└────────────────────────────────┬─────────────────────────────────────────────┘
                                 ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                              models.py                                        │
│                                                                              │
│  elapsed(a, b)  Real time between two instants, VIA UTC. Not decoration:      │
│    Python differences two aware datetimes that SHARE a tzinfo by their NAIVE   │
│    wall-clock values and ignores the offsets. Documented, and silent.          │
│                                                                              │
│      plain subtraction        48.0h                                           │
│      via UTC (real elapsed)   47.0h                                           │
│                                                                              │
│    Worse, it depended on how the timestamp was BUILT -- `isoformat` bakes a    │
│    FIXED offset and subtracts correctly, so the shipped corpus produced the    │
│    RIGHT number while the code was wrong. One instant, two spellings, two      │
│    answers, no error raised.                                                  │
│                                                                              │
│  Elapsed  a PAIR, and neither half is derivable from the other:               │
│    business_hours   what the SLA promises                                      │
│    wall_hours       what the customer experienced                              │
│    amplification    wall/business, and float | None -- None when NO working    │
│                       time was consumed. A ticket opened and closed inside one │
│                       weekend has an UNDEFINED ratio, and 0.0 would read as    │
│                       "no amplification", the exact opposite                   │
│                                                                              │
│  Verdict  SIX values. Two are what everyone expects; the rest exist because a  │
│    boolean `met: bool` forces three different unknowns and one important       │
│    SUCCESS into the wrong bucket:                                             │
│      met                  kept the promise, wait proportionate                 │
│      met_with_long_wait   kept the promise, waited a MULTIPLE of it  <- the    │
│                             headline, and a SUCCESS, so no arithmetic over     │
│                             met/breached can surface it                        │
│      breached             missed the promise                                   │
│      open                 unresolved, so elapsed is a GROWING lower bound      │
│      unknown_start        no creation instant, bounded on ONE SIDE only        │
│      no_calendar          calendar unresolvable, so NO claim is made          │
│                                                                              │
│  Coverage  FOUR values, and only one fails loudly on its own. The holiday      │
│    library answers `date in calendar` with False for an uncovered year, and    │
│    False is indistinguishable from "that day is a working day" -- so an        │
│    uncovered year reads as a SEVEN-DAY WORKING WEEK and manufactures breaches. │
└────────────────────────────────┬─────────────────────────────────────────────┘
                                 ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                             calendars.py                                      │
│                                                                              │
│  BusinessCalendar   zone + weekly window + holidays. `working_days` is a SET,  │
│    not a Monday-Friday constant: GULF_WEEK is Sunday-Thursday, and applying a  │
│    Western week to Cairo is wrong TWICE -- Friday counted as working, Sunday   │
│    as weekend. One corpus ticket gives 2 business hours correctly and 9 under  │
│    the assumption, and NEITHER raises.                                        │
│                                                                              │
│    Rejected shapes: no working days (can never advance), an overnight window   │
│    (silently wrapping is worse than refusing), a midnight close (ambiguous).   │
│                                                                              │
│  window_on(day)  returns None when a bound's local wall time DOES NOT EXIST.   │
│    `datetime.combine` will happily build a time inside a spring-forward gap    │
│    and `astimezone` will silently move it, so both bounds are round-tripped.   │
│    Unreachable for the shipped calendars, so a test asserts it directly.       │
│                                                                              │
│  check_coverage()  the three ways a calendar goes missing, only one loud:      │
│    year_out_of_range     an EMPTY set, silently                               │
│    subdivision_missing   a SMALLER set, silently -- GB gives 7 holidays for    │
│                            2026 where ENG gives 9, SCT 11, NIR 12             │
│    unsupported_region    RAISES, the one safe failure                         │
│                                                                              │
│    Checked against the years THAT REGION's tickets touch. An earlier version   │
│    used the union across the whole file, which made coverage CONTAGIOUS: one   │
│    migrated 1989 row refused every ticket in the dataset and looked like a     │
│    broken tool rather than one bad row.                                       │
│                                                                              │
│  transition_risk()  counts offset changes inside a calendar's window, derived  │
│    from the host's tzdata at CALL TIME. 1,794 transitions across 518 zones     │
│    give ZERO inside Mon-Fri 09:00-17:00, because transitions are SCHEDULED     │
│    into the small hours -- 90% land at 01:00-03:00 local.                      │
│                                                                              │
│    So the DST handling a reader expects was NOT BUILT. It cannot fire. What    │
│    exists instead is this check, so the build fails if that changes.           │
│                                                                              │
│    TWO independent protections, and relying on the wrong one is a trap:        │
│      daytime  Mon-Fri   0 inside                                              │
│      daytime  7 days    0 inside   <- the HOUR protects alone                 │
│      night    Mon-Fri   0 inside   <- the DAY protects alone                  │
│      night    7 days   34 inside   <- both gone                               │
│                                                                              │
│    "DST is always on a Sunday" is FOLKLORE: 22 of 1,794 are Thu or Fri. Israel │
│    moves on a FRIDAY, Egypt on a Friday and a Thursday, so Asia/Jerusalem on a │
│    night shift sees 17 inside where London sees 0. Only the HOUR is general.   │
│                                                                              │
│  overlap_hours()  two regions can share a working DAY and no working HOUR.     │
│    London and Tokyo overlap 0h in January and 1h in July -- and that single    │
│    hour exists only because British Summer Time shifts London against UTC, so  │
│    DST CREATES the only overlap they have.                                     │
└────────────────────────────────┬─────────────────────────────────────────────┘
                                 ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                               clock.py                                        │
│                                                                              │
│  business_elapsed()  the PHYSICAL duration of each open window intersected     │
│    with the interval, accumulated via `elapsed` so it is real time rather than │
│    a count of local clock hours.                                              │
│                                                                              │
│  add_business_hours()  the deadline. NOT the inverse of business_elapsed, and  │
│    it cannot be -- the projection is MANY-TO-ONE. Returns None when the        │
│    calendar never opens inside the guard, rather than approximating.           │
│                                                                              │
│  business_position()  the projection's coordinate. Every instant inside one    │
│    closed stretch maps to the SAME value, so two tickets 50 real hours apart   │
│    are SIMULTANEOUS in business time:                                         │
│                                                                              │
│      position   tickets   real spread  ids                                    │
│         368.0h        3           50h  GB-1009, GB-1006, GB-1010              │
│                                                                              │
│    Consequence: a queue ordered by business-hours-aged reports them as equally │
│    old, so "oldest first" does not serve the longest-waiting customer first,   │
│    and NO tie-break computed from business time can fix it.                    │
│                                                                              │
│  dead_gap_containing()  the closed stretch an instant falls in, with a reason  │
│    (overnight / weekend / the holiday dates). Its length is G in 1 + G/B.      │
│                                                                              │
│  MAX_DAYS = 40,000  a span past roughly a century is a bad timestamp, not a    │
│    long-running ticket, so it raises rather than iterating.                    │
└────────────────────────────────┬─────────────────────────────────────────────┘
                                 ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                 amplify.py                        measure.py                  │
│                                                                              │
│  worst_amplification = 1 + G/B         evaluate()  order matters. Coverage     │
│    G belongs to the CALENDAR and B is    first, because a missing calendar     │
│    the promise, so the promise SCALES    makes every later number meaningless. │
│    the ratio and cannot shrink the       Then open before missing-start, since │
│    constant. Exact while the SLA fits    "we do not know yet" and "we will     │
│    inside one working day; a lower       never know" are different refusals.   │
│    bound beyond that, and the docstring                                       │
│    says so.                            A ONE-SIDED BOUND still settles the    │
│                                          question when it falls on the wrong  │
│  sensitivity()  the worst case per SLA,   side: an open ticket already past    │
│    started from the longest gap. Cross-   its deadline is BREACHED whatever    │
│    checked against brute force over all   happens next, and a lower bound      │
│    8,760 hourly arrivals, so "start at    exceeding the SLA is a breach        │
│    the longest gap" is VERIFIED.          however early it was created.       │
│                                                                              │
│  amplification_profile()  arrivals over  long_wait_threshold()  DERIVED as    │
│    a year, optionally restricted to        1 / open_fraction: 168/40 = 4.2,   │
│    another calendar's working hours.       168/45 = 3.73 for Tokyo. A fixed    │
│                                            constant would mean something      │
│    I PREDICTED a London desk serving       different for every calendar.       │
│    Tokyo customers would be the worst                                         │
│    cohort, since 93.8% of those          looks_perfect  100% compliance AND at │
│    arrivals land while London is shut.     least one long wait. Nothing in the │
│    It is the BEST of the three:            number invites a question. FOUR of  │
│                                            six measurable regions qualify      │
│      cohort     shut   dead h   wait       while the fleet figure is 95.3%.    │
│      uniform   76.9%    22.5   30.5h                                          │
│      GB         0.0%    23.2   31.2h     compliance  float | None -- None when │
│      JP        93.8%     5.6   13.6h       nothing was decided. `all([])` is   │
│                                            True, and reporting 100% for a      │
│    Close to ANTI-CORRELATED. What            refused region turns a refusal    │
│    amplifies is dead time inside the         into a perfect score.             │
│    DEADLINE WINDOW, not a shut calendar                                       │
│    at the moment of arrival. Tokyo lands  undefined_amplification  counts the │
│    in a short OVERNIGHT gap and then        tickets whose ratio has no value,  │
│    gets a full working day.                 rather than leaving a dash in a    │
│                                             table next to "no data".          │
│    gap_share is still reported, LABELLED                                      │
│    as not being the driver, because it                                        │
│    is the number a reader expects to be                                       │
│    the explanation.                                                           │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## The Findings

### One: the arithmetic, and what tightening the SLA buys

```
  SLA   worst wait   amplification   1 + G/B
    1         65h           65.0x     65.0x
    8         72h            9.0x      9.0x
   40        168h            4.2x      2.6x
```

Cutting the promise 8× improves the worst **wait** by **9.7%** and makes the worst **amplification
7.2× worse**. The dead gap dominates the sum, and it belongs to the calendar.

### Two: the worst customer experience is recorded as a success

**US-3003 at 43.7×** — 1.5 business hours against 66 wall hours — verdict `met_with_long_wait`. Four
regions report 100% compliance with a 32–33× wait. The fleet figure is 95.3%, so the aggregate would
prompt a look and those regions individually look finished.

Distinct from the per-slice hiding in Doubletake and Vintage: there a finer slice exposed the
problem, here it cannot. Compliance is a **projection** that discards the wall-clock dimension.

### Three: a bug that depended on how the timestamp was spelled

```
weekend                 reported now   raw subtraction
ordinary                        64h                64h
spring forward                  63h                64h
clocks go back                  65h                64h
```

Errors in **both directions**, so they neither cancel nor read as a bias. The longest London weekend of
2026 is the **65-hour** one where the clocks go back.

### Four: the DST immunity is not the code's doing

**0 of 1,794** transitions across 518 zones fall inside Mon–Fri 09:00–17:00. Two independent
protections — the hour and the day — and only the hour is general, because Israel and Egypt transition
on weekdays.

---

## Data Flow

```
1.  afterhours report --sliced
2.  load 114 tickets; 2 deliberately broken rows reported by line
      one unreadable JSON line, one naive timestamp
3.  resolve each region's calendar against ITS OWN ticket years
      6 covered, 1 year_out_of_range, 1 subdivision_missing, 1 unsupported_region
4.  evaluate each ticket, coverage first
      met 92, met_with_long_wait 9, breached 5, open 1, unknown_start 2, no_calendar 5
      3 breaches settled by a ONE-SIDED bound
      5 tickets with an UNDEFINED amplification ratio
5.  COMPLIANCE  95.3% over 106 decided, 8 refused
    EXPERIENCE  9 long waits, worst 43.7x, and that ticket's verdict is MET
6.  per region -> 4 report 100% compliance WITH a long wait
7.  exit 1: the dataset carries a caveat, so a pipeline has to opt in
```

---

## Testing

```bash
pytest                          # 153 tests, ~14s
pytest --cov=sla_watch --cov-report=term-missing   # 96%
```

| File | Covers |
|:--|:--|
| `conftest.py` | calendars as session fixtures — London, Tokyo, Cairo, a night shift, a 24/7 rota, Cairo-as-Mon-Fri; the corpus loaded once |
| `test_calendars_clock_measure.py` | the translation-invariance asymmetry; the many-to-one projection; `1 + G/B` against brute force; **the two DST protections separated**; **the same-tzinfo subtraction bug**; the non-Western week giving 2 against 9; all four `Coverage` values and non-contagion; all six verdicts; both one-sided-bound breaches; the derived threshold; `looks_perfect`; every `None` case; corpus properties |
| `integration/test_cli.py` | all ten commands, every exit-code reason, JSON and CSV contents |

Two tests exist to stop the others being vacuous: **every `Verdict` and every `Coverage` value must
occur** in the corpus. Nothing is mocked — the holiday data is a local package and tzdata comes from
the host, so the DST tests derive their inputs at test time rather than hardcoding dates.

---

## Configuration

`SW_`-prefixed environment variables or a `.env` file. No list-typed fields, since Pydantic Settings
parses those as JSON and `SW_X=a,b` would fail with a decode error reading like a bug in the tool.

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SW_", env_file=".env", extra="ignore")

    tickets: Path = Path("data/tickets.jsonl")
    now: datetime = datetime(2026, 6, 15, 12, 0, tzinfo=ZoneInfo("UTC"))
    year: int = 2026
    sla_hours: float = Field(default=8.0, gt=0.0)
    long_wait_multiple: float | None = None      # None means derive per calendar
    region: str = "gb"
```

`now` is **pinned** rather than read from the clock: an open ticket's elapsed time grows, so a report
containing one is not reproducible unless the instant it was measured against travels with it.
`long_wait_multiple` defaults to None so the threshold is derived as `1 / open_fraction` per calendar
rather than being one constant that means something different for each.
