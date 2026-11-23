# SLAWatch

A ticket that arrives at Friday 17:00 under a four-business-hour SLA is on time if it is
answered by Monday 13:00. The customer waited 68 hours. SLAWatch measures business-hours SLAs
correctly (holidays, time zones, observed days) and reports the customer's wall-clock wait
beside them, because the SLA number cannot express it. It also shows a result that surprises
people: tightening the SLA makes the worst case *relatively* worse.

The work is calendar arithmetic, plus a decision about which of two correct numbers to report.

---

## The Asymmetry

Wall-clock elapsed time is a function of two instants, and it is **translation-invariant** — shift
both endpoints by the same amount and the answer does not move.

Business elapsed time is **not**. The same 48-hour span consumes **16 business hours** starting Monday
morning and **0** starting Friday evening.

So a promise expressed in business hours is a promise about **how much of our working time we will
spend**. It says nothing bounded about how long the customer waits, and those are different quantities.

---

## The Headline: Tightening the SLA Makes the Worst Case Relatively Worse

Worst-case amplification is `1 + G/B`, where `G` is the longest stretch the calendar is closed and `B`
is the SLA. For a ticket arriving at Friday close on a Mon–Fri 09:00–17:00 calendar, `G = 64`:

```
  SLA   worst wait   amplification   1 + G/B
    1         65h           65.0x     65.0x
    2         66h           33.0x     33.0x
    4         68h           17.0x     17.0x
    8         72h            9.0x      9.0x
   40        168h            4.2x      2.6x   <- G/B underestimates past one working day
```

**`G` belongs to the calendar. `B` is the promise.** So the promise scales the ratio and cannot shrink
the constant:

> Cutting the promise 8× — 8 business hours to 1 — improves the worst **wait** from 72 hours to 65, a
> **9.7% gain**, and makes the worst **amplification 7.2× worse**.

Tightening the SLA is the obvious lever and on the worst case it barely moves anything, because the
dead gap dominates the sum. Cross-checked against a brute-force scan of all 8,760 hourly arrivals.

Add a holiday and the constant grows: the 2026 Easter shutdown gives `G = 112`, and a one-hour SLA
arriving before it has a worst case of **113 hours**.

---

## Compliance Cannot Express It, and a Finer Slice Would Not Help

```
region          n  compl  long  perfect  meanBiz  meanWall    worst  coverage
de              3  100.0%    1     True      2.3      23.7    33.0x  covered
de_archive      2     n/a    0    False      n/a       n/a      n/a  year_out_of_range
eg              2  100.0%    1     True      2.5      34.5    33.0x  covered
gb             53   92.0%    4    False      4.2      18.4    38.3x  covered
il              2  100.0%    1     True      3.0      34.5    32.5x  covered
jp             20  100.0%    1     True      1.9       5.2    32.5x  covered
uk_national     2     n/a    0    False      n/a       n/a      n/a  subdivision_missing
us             29   96.6%    1    False      2.2       5.2    43.7x  covered
xx              1     n/a    0    False      n/a       n/a      n/a  unsupported_region
```

Four regions report **100% compliance** while a customer waited 32–33× the promise. The fleet figure is
95.3%, so the aggregate *would* prompt someone to look — those regions individually look finished.

And the single worst customer experience in the corpus — **US-3003 at 43.7×**, 1.5 business hours
against 66 wall hours — has the verdict `met_with_long_wait`. **It is recorded as a success.**

This is a *different* mechanism from the per-slice hiding in InitiativeHub and StatusChecker, and the
difference matters: there, a finer slice exposed the problem. Here it cannot. Compliance is a
**projection that discards the wall-clock dimension**, so the information is gone before any grouping
happens.

---

## Six Verdicts, Three of Them Refusals

| verdict | meaning |
|:--|:--|
| `met` | kept the promise, wait proportionate to it |
| `met_with_long_wait` | kept the promise, customer waited a multiple of it |
| `breached` | missed the promise |
| `open` | not resolved, so elapsed is a **growing lower bound** |
| `unknown_start` | no creation instant, so elapsed is bounded on **one side** |
| `no_calendar` | the calendar could not be resolved, so no claim is made |

**A one-sided bound still settles the question when it falls on the wrong side.** An open ticket past
its deadline is `breached` whatever happens next; a ticket whose lower-bound elapsed already exceeds
its SLA is breached however early it was really created. A bound is not the same as ignorance.

The long-wait threshold is **derived, not chosen**: `1 / open_fraction` is 168/40 = 4.2 for a standard
week and 168/45 = 3.73 for Tokyo. A fixed constant would mean something different for every calendar.

---

## A Bug I Shipped: Same-Zone Subtraction Is Not Elapsed Time

Python differences two aware datetimes that share a `tzinfo` by their **naive wall-clock values**,
ignoring the offsets. `wall_hours` did exactly that, while its own docstring claimed *"aware
subtraction, so this is real elapsed time and DST is handled by construction."*

```
plain subtraction         48.0h
via UTC (real elapsed)    47.0h
```

What let it survive is that **the answer depended on how the timestamp was built**:

```
same instants, ZoneInfo spelling       48.0h
same instants, fixed-offset spelling   47.0h
```

The corpus round-trips through `isoformat`, which bakes a *fixed* offset — and two different fixed
offsets subtract correctly. **The shipped data produced the right number while the code was wrong.**
Only a directly-constructed test datetime exposed it.

What it cost, on the number the headline is computed from:

```
weekend                 reported now   raw subtraction
ordinary                        64h                64h
spring forward                  63h                64h
clocks go back                  65h                64h
```

The errors run in **both directions**, so they do not cancel and neither reads as a consistent bias.
A single `elapsed()` helper now converts to UTC before subtracting, everywhere.

One consequence: **the longest London weekend of 2026 is 65 hours** — the one where the clocks go back.
The old code called it an ordinary 64.

---

## DST Cannot Enter a Daytime Window, and That Is Not the Code's Doing

Scanning **1,794 transitions across 518 zones** from the host's tzdata:

```
local hour:  {0: 24, 1: 514, 2: 326, 3: 786, 4: 85, 21: 10, 23: 49}
weekday:     {Sun: 1700, Sat: 72, Fri: 15, Thu: 7}
inside Mon-Fri 09:00-17:00:  0
```

Zero. Transitions are **scheduled** into the small hours. So the elaborate DST handling a reader might
expect **was not built**, because it cannot fire — and `transition_risk` derives the count from tzdata
at call time so the build fails if that ever changes.

The immunity is **two independent protections**, and relying on the wrong one is a trap:

```
window                            scanned  inside
daytime 09:00-17:00  Mon-Fri           34       0
daytime 09:00-17:00  7 days            34       0    <- the HOUR protects alone
night   00:30-08:00  Mon-Fri           34       0    <- the DAY protects alone
night   00:30-08:00  7 days            34      34    <- both gone
```

**"DST is always on a Sunday" is folklore.** 22 of the 1,794 transitions are Thursday or Friday —
Israel moves on a **Friday**, Egypt on a Friday and a Thursday. `Asia/Jerusalem` on a night shift sees
**17** inside its window where London sees 0. Only the hour protection is general.

---

## Absence Is the Dangerous Input

The holiday library answers `date in calendar` with `False` for a year it does not cover. `False` is
indistinguishable from *"that day is a working day"*, so an uncovered year reads as a **seven-day
working week**, makes every SLA look tighter, and manufactures breaches.

| coverage | how it fails |
|:--|:--|
| `year_out_of_range` | returns an **empty set**, silently |
| `subdivision_missing` | returns a **smaller set**, silently |
| `unsupported_region` | **raises** — the one safe failure |

`GB` alone gives 7 holidays for 2026; England gives 9, Scotland 11, Northern Ireland 12. The national
default is a *subset*.

**A default calendar is refused deliberately.** Falling back to Mon–Fri 09:00–17:00 for an unknown
region produces a confident answer for a region whose weekend may be Friday and Saturday.

### A Saturday–Sunday weekend is an assumption

`GULF_WEEK` is Sunday–Thursday. Applying Mon–Fri to Cairo is wrong **twice over** — Friday counted as
working, Sunday as weekend. One corpus ticket gives **2** business hours correctly and **9** under the
assumption, and neither raises.

---

## The Projection Is Many-to-One

Every instant inside one closed stretch maps to the same business-time coordinate:

```
  position   tickets   real spread  ids
     368.0h        3           50h  GB-1009, GB-1006, GB-1010
```

Three tickets, 50 real hours apart, **simultaneous in business time**. A queue ordered by
business-hours-aged reports them as equally old, so "oldest first" does not serve the
longest-waiting customer first — and no tie-break computed from business time can fix it.

`sla-watch deadline` shows the same thing from the other side: arrivals at Friday 17:00, Saturday
10:00 and Sunday 20:00 all share one deadline.

---

## Running

```bash
bash setup.sh                    # re-derives every finding above, including the bug
source .venv/bin/activate

sla-watch doctor
sla-watch check                  # exits 1: two unreadable rows, 5 tickets with no calendar
sla-watch report --sliced        # compliance and experience, per region
sla-watch amplify                # 1 + G/B, and the cohort table that refuted my hypothesis
sla-watch deadline               # one promise, waits varying by an order of magnitude
sla-watch gaps                   # closed stretches, including the 65-hour weekend
sla-watch transitions            # the DST immunity, derived from tzdata
sla-watch overlap                # London and Tokyo: 0 shared hours in winter, 1 in summer
sla-watch queue                  # tickets simultaneous in business time
sla-watch explain GB-1002        # one ticket, measured both ways
```

| Command | Exit |
|:--|:--|
| `check` | **1** on an unreadable row or an unresolvable calendar — both fixable at source |
| `report` | **1** if the dataset carries a caveat, so a pipeline has to opt in |
| everything else | 0 |

`check` does **not** fail on a long wait or a missing creation date. Those are properties of the world,
and a gate that fires whenever someone reports a fault on a Friday afternoon gets switched off inside
a week.

---

## A Hypothesis I Got Wrong

I expected a London desk serving **Tokyo** customers to be the worst cohort, since almost every such
arrival lands while London is shut. Measured:

```
arrivals from        n  shut on arrival  mean dead h  mean wait  over 4x
uniform           8760            76.9%         22.5      30.5h    29.9%
GB customers      2024             0.0%         23.2      31.2h    18.0%
JP customers      2196            93.8%          5.6      13.6h     2.3%
```

It is the **best** of the three, and the two columns are close to **anti-correlated**: 0.0% shut with
23 dead hours, against 93.8% shut with 6.

Being closed at the moment of arrival is not what amplifies — **dead time inside the deadline window**
is. Tokyo lands in London's short *overnight* gap and then gets a full working day; London's own
customers arrive mid-afternoon on a Friday and straddle a weekend. `gap_share` is still reported,
labelled as not being the driver, because it is the number a reader expects to be the explanation.

---

## Testing

```bash
pytest                          # 153 tests, ~14s
pytest --cov=sla_watch --cov-report=term-missing    # 96%
```

Two tests exist to stop the others being vacuous: **every `Verdict` value and every `Coverage` value
must occur** in the shipped corpus. Nothing is mocked — the holiday data is a local package and the
timezone data comes from the host, which is why the DST tests derive their inputs at test time rather
than hardcoding dates that go stale.

A test pins the specific 2026 holiday dates the corpus outcomes rest on. It earned its place
immediately by catching me asserting **2026-05-04 was a working Monday** when it is the Early May bank
holiday — and two filler tickets had independently landed on it and on US Election Day.

---

## Scope: What This Deliberately Does Not Do

**Draft escalation emails**, which the brief asks for. BlurbGen already does templated generation with
a validator and a gate; a drafted email cannot assert in CI.

**A Rich live-updating dashboard**, also asked for. This portfolio carries a logged gap for untested
UI code. A text table asserts; a live display does not.

**Overnight and 24/7 windows** are rejected rather than supported — which is the honest inverse of the
finding, since a rota with no dead gap has no amplification problem. The calendars this tool handles
are the ones with the problem.

The diligence check found business-day arithmetic, holiday calendars and working-hours logic **absent
portfolio-wide** (`business_day|business_hour|workday`, `holiday`, `busday`: zero files). tz handling
exists only in LifecycleTracker, whose `time_in_state` does `total += span.duration` — exact instant
subtraction with no calendar. StatusChecker's `AgeBound` bounds the *observation*, not a calendar.
Nobody projects an interval onto a working calendar.

---

## Tradeoffs

**`no_calendar` refuses 5 of 114 tickets, and a refusal helps nobody.** The remedy is configuration,
and until someone does it those tickets have no answer — a team under pressure will reasonably prefer
a slightly wrong number to none.

**`met_with_long_wait` is a judgement dressed as a measurement.** The threshold is derived rather than
arbitrary, but 4.2 is a line across a continuum: 4.1 reads as ordinary, 4.3 is flagged.

**`looks_perfect` fires on tiny regions.** Three of the four that trigger it have two or three
tickets. The count is shown alongside and no minimum-size rule is applied — deliberate, to avoid
hiding real cases, and it means the flag will fire on noise.

**Amplification is undefined for exactly the tickets a manager may care about most** — those that
arrived and closed while the office was shut. They are counted, not measured.

**The region registry is hardcoded in Python**, which is the wrong shape for something that changes
when the business opens an office. And `working_days` cannot express a **half day**, which several
real calendars use.

**Nothing here fixes anything.** The remedies are all outside the tool: an out-of-hours rota, a
wall-clock SLA for high severity, or an honestly published expectation.

**`python-holidays` is a dependency where InitiativeHub refused `dateutil`.** Not inconsistent: a
holiday list is a *fact* that cannot be derived, while `dayfirst` is a *decision* that silently
resolves the ambiguity that project existed to expose. The cost is that a past year's calendar can
change between releases, so the version is pinned, printed by `doctor`, carried in every report, and
the dates the corpus depends on are pinned by a test.

---

## Configuration

```bash
SW_TICKETS=data/tickets.jsonl
SW_NOW=2026-06-15T12:00:00+00:00   # pinned: an open ticket's elapsed time grows
SW_YEAR=2026
SW_SLA_HOURS=8.0
SW_REGION=gb
SW_LONG_WAIT_MULTIPLE=            # empty means derive it per calendar
```

`now` is pinned rather than read from the clock because an open ticket's elapsed time grows, so a
report containing one is not reproducible without the instant it was measured against travelling with
it. StatusChecker reached the same conclusion for a different reason, and the label is the same: this
is when we looked.

---

## Stack

| Concern | Choice |
|:--|:--|
| Language | Python 3.11+ |
| Models + config | Pydantic, Pydantic Settings |
| CLI | Click |
| Holiday data | `python-holidays`, pinned to a minor range |
| Timezones | `zoneinfo` and the host's tzdata |
| Output | text, JSON, CSV |
| Tests | pytest, Click's CliRunner |
| Models/AI | none, deliberately |

The corpus is synthetic and reproducible via `data/generate.py`: 114 tickets across 9 region keys, plus
two deliberately broken rows. Planted: a Friday-afternoon arrival, an Easter-shutdown breach and a
matching success, a weekend-only ticket with an undefined ratio, an open ticket, an open ticket already
past its deadline, two flavours of missing creation instant, a lower bound that settles a breach, three
tickets simultaneous in business time, a Sunday–Thursday region, a ticket spanning Israel's Friday DST
transition, and one region for each of the three coverage failures.
