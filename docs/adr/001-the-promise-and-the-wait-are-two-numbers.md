# ADR 001: A business-hours SLA measures our effort, and the customer's wait is a separate number

## Status

Accepted

## Context

The brief asks for "correct business-day calculation across regions". Getting that calculation right
is necessary and it is not sufficient, because of a property of the quantity being calculated.

Wall-clock elapsed time is a function of two instants. It is **translation-invariant**: shift both
endpoints by the same amount and the answer does not move.

Business elapsed time is **not**. The same 48-hour span consumes 16 business hours starting Monday
morning and **0** starting Friday evening. So the answer depends on *where in the week the span
falls*, and two tickets handled with identical diligence get different numbers.

The consequence is not a rounding concern. It means a promise expressed in business hours is a
promise about **how much of our working time we will spend**, and says nothing bounded about how long
the customer waits. Those are different quantities and both are legitimate. Reporting only the first
is what this project is about.

## Decision

### `Elapsed` is a pair, and neither half is derivable from the other

```python
class Elapsed(BaseModel):
    business_hours: float   # what the SLA promises
    wall_hours: float       # what the customer experienced
```

`amplification` is `wall_hours / business_hours`, and it is `float | None`. **None when no working
time was consumed at all** — a ticket opened and closed inside one weekend consumed 0 business hours
and 4 wall hours, and the ratio is genuinely undefined. Returning `0.0` would read as *"no
amplification"*, which is the exact opposite of what happened. Five tickets in the shipped corpus hit
this, and `Report.undefined_amplification` counts them rather than leaving a dash in a table.

### The arithmetic: `1 + G/B`, verified against brute force

`G` is the longest stretch the calendar is closed. `B` is the SLA in business hours. For a ticket
arriving at Friday close on a Monday–Friday 09:00–17:00 calendar, `G = 64`:

```
  SLA   worst wait   amplification   1 + G/B
    1         65h           65.0x     65.0x
    2         66h           33.0x     33.0x
    4         68h           17.0x     17.0x
    8         72h            9.0x      9.0x
   40        168h            4.2x      2.6x   <- G/B underestimates past one working day
```

**G belongs to the calendar and B is the promise.** So the promise scales the ratio and cannot shrink
the constant, which produces the finding:

> Cutting the promise 8× — from 8 business hours to 1 — improves the worst **wait** from 72 hours to
> 65, a **9.7% gain**, and makes the worst **amplification 7.2× worse** (9.0x becomes 65.0x).

Tightening an SLA is the obvious lever and on the worst case it is almost entirely ineffective,
because the dead gap dominates the sum. `sensitivity` is cross-checked against a brute-force scan of
all 8,760 hourly arrivals in the year, so "start at the longest gap" is verified rather than assumed.

With a holiday attached the constant grows: the 2026 Easter shutdown makes `G = 112`, and a
one-business-hour SLA arriving before it has a worst case of **113 hours**.

### Six verdicts, because a boolean forces three unknowns into the wrong bucket

| verdict | meaning |
|:--|:--|
| `met` | kept the promise, wait proportionate to it |
| `met_with_long_wait` | kept the promise, customer waited a large multiple of it |
| `breached` | missed the promise |
| `open` | not resolved, so elapsed is a **growing lower bound** |
| `unknown_start` | no creation instant, so elapsed is bounded on **one side** |
| `no_calendar` | the region's calendar could not be resolved, so no claim is made |

`met_with_long_wait` is the headline value. It is a **success** on the promise, so no arithmetic over
`met` and `breached` can surface it.

**A one-sided bound still settles the question when it falls on the wrong side.** An open ticket that
has already consumed more than its SLA is `breached` whatever happens next, and a ticket whose
lower-bound elapsed exceeds its SLA is breached however early it was really created. A bound is not
the same as ignorance, and three corpus tickets are decided this way.

### The long-wait threshold is derived, not chosen

`1 / open_fraction`: 168/40 = **4.2** for a standard week, 168/45 = **3.73** for Tokyo's nine-hour
day. That is the long-run ratio between wall time and working time, so a ticket above it waited
longer than the calendar alone accounts for. A fixed constant would silently mean something different
for every calendar it was applied to.

### Compliance and experience are reported side by side, and neither is called the real one

`compliance` is `met / decided`, and it is `float | None` — **None when nothing was decided**, because
`all([])` is True and a region whose calendar could not be resolved has made no claim either way.
Reporting 100% for it turns a refusal into a perfect score.

`looks_perfect` is true when a region shows **100% compliance and at least one long wait**. On the
shipped corpus four of six measurable regions qualify while the fleet figure is 95.3% — so the
aggregate would prompt someone to look, and those regions individually look finished.

This is a *different mechanism* from the per-slice hiding in InitiativeHub and StatusChecker, and the
difference is worth stating: there, a finer slice would have exposed the problem. Here it would not.
The compliance metric is a **projection that discards the wall-clock dimension**, so the information
is gone before any grouping happens.

## Validation

- `TestBusinessTimeIsNotTranslationInvariant` — 16 hours from Monday and 0 from Friday for the same
  span; wall time unchanged; the undefined ratio
- `TestAmplificationArithmetic` — the closed form against measured values; `sensitivity` against a
  brute-force scan of all 8,760 arrivals; the Easter 113×; empty profiles returning None
- `TestVerdicts` — all six; both one-sided-bound breaches; an open ticket inside its SLA staying
  `open`; a weekend-only ticket not becoming a long wait because its ratio has no value
- `TestTheThresholdIsDerivedNotChosen` — 4.2 and 3.73, and that it moves with the calendar
- `TestComplianceCannotExpressExperience` — `looks_perfect`; compliance None rather than 1.0
- `TestPropertiesOverTheShippedCorpus::test_the_worst_customer_experience_is_recorded_as_a_success`
- `setup.sh` and CI re-derive the arithmetic, the brute-force worst case and the corpus figures

## Consequences

**Good**

- The case that a compliance percentage cannot express has a name, a count and a threshold
- The amplification arithmetic is published, so "tighten the SLA" can be evaluated before it is tried
- A refused region cannot be reported as compliant, and an undefined ratio cannot be reported as zero
- Every long wait is traceable to a specific closed stretch via `explain`

**Bad**

- **`met_with_long_wait` is a judgement dressed as a measurement.** The threshold is derived rather
  than arbitrary, but 4.2 is still a line drawn across a continuum, and a ticket at 4.1 is reported
  as ordinary while one at 4.3 is flagged
- `looks_perfect` on a two-ticket region is nearly meaningless, and three of the four regions that
  trigger it in the corpus have two or three tickets. The count is shown next to it and no
  minimum-size rule is applied, so the reader has to weigh it — that is a deliberate choice to avoid
  hiding real cases, and it means the flag will fire on noise
- Nothing here **fixes** anything. The tool reports that a promise measured in business hours has a
  weak relationship to the customer's wait; the remedies are all outside it — an out-of-hours rota, a
  wall-clock SLA for high severity, or an honest published expectation
- The worst-case table is computed from the longest gap in **one calendar year**. An organisation
  with a Christmas shutdown longer than Easter gets a different constant, and a multi-year view is
  not built
- Amplification is undefined for exactly the tickets a support manager might care about most — the
  ones that arrived and closed while the office was shut. They are counted, not measured

**Neutral**

- `open` and `unknown_start` are both refusals and are kept apart because "we do not know yet" and
  "we will never know" have different remedies
- The report is measured against a **pinned** instant rather than the wall clock, because an open
  ticket's elapsed time grows and a report containing one is otherwise not reproducible.
  StatusChecker reached the same conclusion for a different reason, and the label is the same: this
  is when we looked
