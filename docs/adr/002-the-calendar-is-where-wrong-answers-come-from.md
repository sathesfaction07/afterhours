# ADR 002: The calendar is where the wrong answers come from, and absence is the worst of them

## Status

Accepted

## Context

The brief's worst input is "tickets with no creation date, timezone mismatches, holidays varying by
region". Working through it, the timezone half turned out to be *less* dangerous than expected and
the holiday half *more*, and one of the wrong answers came from Python rather than from the data.

Three questions had to be answered before any business-hours figure could be trusted:

1. When is a region actually open? — a Saturday-Sunday weekend is an assumption, not a fact.
2. Can DST corrupt a working-hours count? — and if not, why not.
3. What does a missing holiday calendar look like? — because it does not look like an error.

## Decision

### The working week is data, not an assumption

`BusinessCalendar.working_days` is a `frozenset[int]`, not a Monday-to-Friday constant.

`GULF_WEEK` (Sunday–Thursday) is used across much of the Middle East. Applying a Monday–Friday
assumption to Cairo is wrong **twice over**: it counts Friday as a working day and Sunday as a
weekend. On one corpus ticket spanning the Egyptian weekend the correct answer is 2 business hours and
the assumed one is **9** — and neither raises, so nothing in either number says which week was used.

A calendar with no working days is rejected, because it can never advance. An overnight window
(`22:00`–`06:00`) is rejected rather than silently wrapped, and a midnight close is rejected as
ambiguous.

### DST cannot enter a daytime window, and that is not the code's doing

Scanning **1,794 transitions across 518 zones** from the host's tzdata:

```
local hour:  {0: 24, 1: 514, 2: 326, 3: 786, 4: 85, 21: 10, 23: 49}
weekday:     {Sun: 1700, Sat: 72, Fri: 15, Thu: 7}
inside Mon-Fri 09:00-17:00:  0
```

Zero. Transitions are **scheduled** into the small hours, so no daytime window contains one.

So the elaborate DST handling a reader might expect **was not built**, because it cannot fire. What
was built instead is `transition_risk`, which derives the count from tzdata at call time so the build
fails if that ever stops being true. Choosing not to build something and asserting the reason is a
better outcome here than either ignoring DST or handling a case that never arrives.

**And the immunity decomposes into two independent protections**, which matters because relying on the
wrong one is a trap:

```
window                            scanned  inside
daytime 09:00-17:00  Mon-Fri           34       0
daytime 09:00-17:00  7 days            34       0    <- the HOUR protects alone
night   00:30-08:00  Mon-Fri           34       0    <- the DAY protects alone
night   00:30-08:00  7 days            34      34    <- both gone
```

**"DST is always on a Sunday" is folklore.** 22 of the 1,794 transitions are on a Thursday or Friday:
Israel moves on a **Friday** and Egypt on a Friday and a Thursday. So the day protection is
zone-specific, and `Asia/Jerusalem` on a night shift sees **17** transitions inside its window where
London sees 0. Only the hour protection is general.

`window_on` still returns `None` for a local time inside a spring-forward gap, and the day contributes
nothing rather than a wrong number. That branch is unreachable for the shipped calendars, so it is
asserted directly in a test rather than left to the corpus.

### The bug: same-zone subtraction is not elapsed time

Python differences two aware datetimes that share a `tzinfo` by their **naive wall-clock values**,
ignoring the offsets. Documented behaviour, and `wall_hours` did exactly that while its own docstring
claimed *"aware subtraction, so this is real elapsed time and DST is handled by construction"*.

```
plain subtraction         48.0h
via UTC (real elapsed)    47.0h
offsets differ:           0:00:00 -> 1:00:00
```

What made it survive review is that **the answer depended on how the timestamp was built**:

```
same instants, ZoneInfo spelling       48.0h
same instants, fixed-offset spelling   47.0h
```

The corpus round-trips through `isoformat`, which bakes a *fixed* offset — and two different fixed
offsets subtract correctly. So the shipped data produced the right number while the code was wrong,
and only a directly-constructed test datetime exposed it.

What it cost, on the number the amplification headline is computed from:

```
weekend                 reported now   raw subtraction
ordinary                        64h                64h
spring forward                  63h                64h
clocks go back                  65h                64h
```

The errors run in **both directions**, so they do not cancel and neither shows up as a consistent
bias. The fix is a single `elapsed()` helper that converts both operands to UTC, used by `wall_hours`,
`DeadGap.hours`, `DeadGap.duration`, `business_elapsed` and `overlap_hours`.

A consequence worth noting: **the longest London weekend of 2026 is 65 hours**, the one where the
clocks go back. The old code called it an ordinary 64.

### An absent holiday calendar reads as a seven-day working week

The holiday library answers `date in calendar` with `False` for a year outside its coverage.
`False` is indistinguishable from *"that day is a working day"*, so an uncovered year makes every SLA
look **tighter** than it is and manufactures breaches. `Coverage` names the three ways this happens,
and only one of them is loud on its own:

| value | how it fails |
|:--|:--|
| `covered` | — |
| `year_out_of_range` | returns an **empty set**, silently |
| `subdivision_missing` | returns a **smaller set**, silently |
| `unsupported_region` | **raises** — the one safe failure |

`subdivision_missing` is the easiest to hit in practice. `GB` alone yields 7 holidays for 2026 while
England yields 9, Scotland 11 and Northern Ireland 12 — so the national default is a *subset*, and
using it drops real days off. All three produce a `no_calendar` refusal rather than a number.

**A default calendar is refused deliberately.** Falling back to Monday–Friday 09:00–17:00 for an
unresolvable region produces a confident answer for a region whose weekend may be Friday and Saturday.

Coverage is checked against the years **that region's** tickets touch. An earlier version used the
union of years across the whole file, which made coverage *contagious*: one migrated 1989 row put 1989
into every region's check, no region's holiday data reaches back that far, and every ticket in the
dataset was refused. The failure looked like a broken tool rather than one bad row.

### `python-holidays` is a dependency, and `dateutil` was not in InitiativeHub

Worth stating together, because the two decisions look inconsistent and are not. A holiday list is a
**fact** that cannot be derived from first principles — lunar holidays, legislative changes,
observed-shift rules. `dateutil`'s `dayfirst` is a **decision** that silently resolves the exact
ambiguity that project existed to expose.

The cost of taking the dependency is that the calendar for a *past* year can change between releases,
which would change a past SLA answer. So the version is pinned to a minor range, printed by `doctor`,
carried in every report, and a test pins the specific 2026 dates the corpus outcomes rest on.

That test earned its place immediately: it caught me asserting that **2026-05-04 was a working
Monday** when it is the Early May bank holiday. Two filler tickets had independently landed on it and
on US Election Day, which is how a holiday set that is actually being consulted behaves.

## Validation

- `TestANonWesternWeekIsNotAnEdgeCase` — Friday and Saturday as a weekend; the same ticket giving 2
  against 9 hours under the wrong week; every rejected calendar shape
- `TestDstCannotEnterADaytimeWindow` — every registry calendar immune; all zones over a bounded window;
  the transition-hour clustering; the two protections separated; a zone whose transitions are Fridays
- `TestSameZoneSubtractionIsNotElapsedTime` — the language behaviour asserted directly; the same
  instant giving two answers by spelling; `DeadGap.hours` at 63/64/65
- `TestCoverageIsNotTheSameAsEmptiness` — the library returning False out of range; all four
  `Coverage` values; the national default being a subset; coverage not contagious
- `TestPropertiesOverTheShippedCorpus` — every `Coverage` value occurs; the three refusal reasons are
  distinguished; the specific 2026 holiday dates
- `setup.sh` and CI re-scan tzdata, re-derive the two protections and re-demonstrate the bug

## Consequences

**Good**

- The working week is configurable, so a Sunday–Thursday region gets a correct answer rather than a
  confident wrong one
- Elapsed time is now elapsed time everywhere, and a test pins the language behaviour that caused it
- The three ways a calendar goes missing are distinguished, and none of them produces a number
- DST machinery that could never fire was not built, and the reason is a checked property

**Bad**

- **`no_calendar` refuses 5 of 114 corpus tickets, and a refusal is not useful to anyone.** The
  remedy is configuration, but until someone does it those tickets have no SLA answer at all — a team
  under pressure will reasonably prefer a slightly wrong number to none
- The region registry is **hardcoded in Python**. Adding a region means editing source, which is the
  wrong shape for something that changes when the business opens an office
- `working_days` cannot express a **half day**. A Friday that closes at noon is either a full working
  day or not one, and several real calendars — including the UAE's — work exactly that way
- Overnight and 24/7 windows are rejected rather than supported, so the tool cannot measure the very
  rota that has no dead gap and therefore no amplification problem. That is the honest inverse of the
  finding: the calendars this tool handles are the ones with the problem
- The DST immunity is empirical over 2015–2031. It is asserted from tzdata rather than assumed, but a
  legislature could schedule a midday transition tomorrow and the correct behaviour then is a build
  failure and a code change, not graceful handling
- Holiday data is trusted once resolved. A region that observes a holiday the library does not know
  about produces a confidently wrong deadline, and nothing here detects it

**Neutral**

- Two registry keys share a country code deliberately — `de`/`de_archive` and `gb`/`uk_national` — so
  the report can show what a missing subdivision and an uncovered year each cost. `transitions`
  collapses them by calendar signature, since scanning identical calendars twice looked like a bug
- `holiday_set` returns an empty set for an unsupported region and says in its docstring that callers
  must check coverage first. That is a weaker guarantee than raising, and it exists so that the
  coverage check is the single place the decision is made
