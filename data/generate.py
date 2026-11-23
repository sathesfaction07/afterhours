#!/usr/bin/env python3
"""Generate the ticket corpus. Deterministic: same output every run, no seed-dependent surprises.

Every outcome the code can produce occurs here at least once, so the property tests that assert
"every Verdict value appears" are not vacuous. The interesting rows are hand-placed with a comment
saying what they are FOR; the filler exists only to give the arrival distribution some body.

The reference NOW is fixed at 2026-06-15T12:00:00Z so that OPEN tickets have a stable answer. An
open ticket's elapsed time grows with the clock, so a corpus containing one is not reproducible
without pinning the instant it is measured against.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")
NEW_YORK = ZoneInfo("America/New_York")
TOKYO = ZoneInfo("Asia/Tokyo")
BERLIN = ZoneInfo("Europe/Berlin")
CAIRO = ZoneInfo("Africa/Cairo")
JERUSALEM = ZoneInfo("Asia/Jerusalem")

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=ZoneInfo("UTC"))
assert NOW.strftime("%a") == "Mon", "the reference instant should be a working Monday"


def row(
    ticket_id: str,
    region: str,
    sla: float,
    created: datetime | None = None,
    resolved: datetime | None = None,
    first_activity: datetime | None = None,
    priority: str = "standard",
) -> dict:
    payload: dict = {"ticket_id": ticket_id, "region": region, "sla_business_hours": sla}
    if created is not None:
        payload["created_at"] = created.isoformat()
    if first_activity is not None:
        payload["first_activity_at"] = first_activity.isoformat()
    if resolved is not None:
        payload["resolved_at"] = resolved.isoformat()
    if priority != "standard":
        payload["priority"] = priority
    return payload


def london(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=LONDON)


rows: list[dict] = []

# ---------------------------------------------------------------------------------------------
# London (England). The main region, carrying most of the interesting cases.
# ---------------------------------------------------------------------------------------------

# Resolved inside one working day. Amplification 1.0 -- the case everyone pictures.
rows.append(row("GB-1001", "gb", 8, london(3, 2, 9, 30), london(3, 2, 12, 30)))

# Arrives Friday afternoon, resolved Monday morning. 2 business hours, 66 wall hours. MET on the
# promise and 33x on the wait, which is the whole point of the project.
rows.append(row("GB-1002", "gb", 8, london(3, 6, 16, 0), london(3, 9, 10, 0)))

# Genuinely late: 13 business hours against a 4-hour SLA.
rows.append(row("GB-1003", "gb", 4, london(3, 2, 9, 0), london(3, 3, 14, 0)))

# Still open at the reference instant and comfortably inside its SLA, so elapsed is a LOWER BOUND
# that is still growing. Counting it as MET would flatter every report run before resolution.
rows.append(row("GB-1004", "gb", 20, london(6, 12, 15, 0)))

# No creation instant. First activity is an UPPER bound on creation, so the measured 4 hours is a
# lower bound -- and 4 is inside the 8-hour SLA, so the question cannot be settled.
rows.append(row("GB-1005", "gb", 8, None, london(3, 4, 15, 0), first_activity=london(3, 4, 11, 0)))

# Opened AND closed on a Saturday: 0 business hours, 4 wall hours. Amplification is UNDEFINED
# rather than zero or infinite, which is the case that forces `amplification` to be optional.
rows.append(row("GB-1006", "gb", 8, london(3, 7, 10, 0), london(3, 7, 14, 0)))

# Breach across the Easter shutdown: Good Friday 3 Apr and Easter Monday 6 Apr are both holidays,
# so 1 hour on Thursday plus 2 on Tuesday exceeds a 2-hour SLA.
rows.append(row("GB-1007", "gb", 2, london(4, 2, 16, 0), london(4, 7, 11, 0)))

# The same shutdown, met on the promise: 3.5 business hours against 8. The customer waited 115.5
# wall hours for it, which is 33x.
rows.append(row("GB-1008", "gb", 8, london(4, 2, 16, 30), london(4, 7, 12, 0)))

# These two arrive 50 hours apart, both inside the SAME closed stretch, so they project to the
# IDENTICAL business-time position. A queue ordered by business-hours-aged cannot tell them apart.
rows.append(row("GB-1009", "gb", 8, london(3, 6, 18, 0), london(3, 9, 11, 0)))
rows.append(row("GB-1010", "gb", 8, london(3, 8, 20, 0), london(3, 9, 11, 0)))

# Neither a creation instant nor any recorded activity, so there is no lower bound at all. Distinct
# from GB-1005, where a bound exists and simply does not settle the question.
rows.append(row("GB-1011", "gb", 8, None, london(3, 5, 16, 0)))

# Open AND already past its deadline. A one-sided bound still settles the question when it falls on
# the wrong side, so this is BREACHED rather than OPEN -- nothing that happens next can rescue it.
rows.append(row("GB-1012", "gb", 4, london(6, 1, 9, 0)))

# Unknown start whose LOWER bound already exceeds the SLA: 22 measured business hours against 4.
# The breach holds however much earlier the ticket was really created.
rows.append(row("GB-1013", "gb", 4, None, london(3, 4, 15, 0), first_activity=london(3, 2, 9, 0)))

# ---------------------------------------------------------------------------------------------
# Berlin. Built to have NO breaches and at least one long wait, so it reports 100% compliance
# while a customer waited 33x the promise. Nothing in the compliance number invites a question.
# ---------------------------------------------------------------------------------------------
rows.append(row("DE-2001", "de", 8, datetime(2026, 5, 4, 10, 0, tzinfo=BERLIN),
                datetime(2026, 5, 4, 13, 0, tzinfo=BERLIN)))
rows.append(row("DE-2002", "de", 8, datetime(2026, 5, 8, 16, 0, tzinfo=BERLIN),
                datetime(2026, 5, 11, 10, 0, tzinfo=BERLIN)))
rows.append(row("DE-2003", "de", 8, datetime(2026, 5, 5, 9, 0, tzinfo=BERLIN),
                datetime(2026, 5, 5, 11, 0, tzinfo=BERLIN)))

# ---------------------------------------------------------------------------------------------
# New York. Has a breach as well as a long wait, so its compliance is below 100% and it does NOT
# look finished -- the contrast that makes Berlin's signature worth naming.
# ---------------------------------------------------------------------------------------------
rows.append(row("US-3001", "us", 8, datetime(2026, 5, 4, 10, 0, tzinfo=NEW_YORK),
                datetime(2026, 5, 4, 14, 0, tzinfo=NEW_YORK)))
rows.append(row("US-3002", "us", 3, datetime(2026, 5, 5, 9, 0, tzinfo=NEW_YORK),
                datetime(2026, 5, 6, 15, 0, tzinfo=NEW_YORK)))
rows.append(row("US-3003", "us", 8, datetime(2026, 5, 8, 16, 30, tzinfo=NEW_YORK),
                datetime(2026, 5, 11, 10, 0, tzinfo=NEW_YORK)))

# ---------------------------------------------------------------------------------------------
# Tokyo, open 09:00-18:00. A 45-hour week gives a threshold of 168/45 = 3.73 rather than 4.2, so
# the long-wait line moves with the calendar instead of being a constant.
# ---------------------------------------------------------------------------------------------
rows.append(row("JP-4001", "jp", 9, datetime(2026, 5, 7, 10, 0, tzinfo=TOKYO),
                datetime(2026, 5, 7, 15, 0, tzinfo=TOKYO)))
rows.append(row("JP-4002", "jp", 9, datetime(2026, 5, 8, 17, 0, tzinfo=TOKYO),
                datetime(2026, 5, 11, 10, 0, tzinfo=TOKYO)))

# ---------------------------------------------------------------------------------------------
# Cairo, working Sunday-Thursday. EG-5002 spans the Egyptian weekend of Friday and Saturday, so a
# Monday-Friday assumption would count Friday as a working day and Sunday as a weekend -- giving a
# confident answer that is wrong in both directions at once.
# ---------------------------------------------------------------------------------------------
rows.append(row("EG-5001", "eg", 8, datetime(2026, 5, 3, 10, 0, tzinfo=CAIRO),
                datetime(2026, 5, 3, 13, 0, tzinfo=CAIRO)))
rows.append(row("EG-5002", "eg", 8, datetime(2026, 5, 7, 16, 0, tzinfo=CAIRO),
                datetime(2026, 5, 10, 10, 0, tzinfo=CAIRO)))

# ---------------------------------------------------------------------------------------------
# Tel Aviv, working Sunday-Thursday. IL-6002 spans Israel's spring transition, which happens on a
# FRIDAY at 03:00 -- inside the Israeli weekend and outside every working window. Business hours are
# therefore untouched while the WALL measurement loses an hour: 65 real hours where the local clock
# reads 66.
# ---------------------------------------------------------------------------------------------
rows.append(row("IL-6001", "il", 8, datetime(2026, 3, 22, 10, 0, tzinfo=JERUSALEM),
                datetime(2026, 3, 22, 14, 0, tzinfo=JERUSALEM)))
rows.append(row("IL-6002", "il", 8, datetime(2026, 3, 26, 16, 0, tzinfo=JERUSALEM),
                datetime(2026, 3, 29, 10, 0, tzinfo=JERUSALEM)))

# ---------------------------------------------------------------------------------------------
# The three ways a calendar fails to resolve. All three produce a refusal rather than a number.
# ---------------------------------------------------------------------------------------------

# SUBDIVISION_MISSING: the national default gives 7 holidays where England gives 9. Fewer holidays
# means a tighter deadline, which manufactures breaches -- so it is refused rather than used.
rows.append(row("UK-7001", "uk_national", 8, london(5, 4, 10, 0), london(5, 4, 14, 0)))
rows.append(row("UK-7002", "uk_national", 8, london(5, 5, 10, 0), london(5, 5, 12, 0)))

# YEAR_OUT_OF_RANGE: German holiday data starts at reunification, so 1989 resolves to an EMPTY
# holiday set. Empty is indistinguishable from "no holidays", which reads as a seven-day week.
rows.append(row("DA-8001", "de_archive", 8, datetime(1989, 5, 4, 10, 0, tzinfo=BERLIN),
                datetime(1989, 5, 4, 14, 0, tzinfo=BERLIN)))
rows.append(row("DA-8002", "de_archive", 8, datetime(1989, 6, 6, 9, 0, tzinfo=BERLIN),
                datetime(1989, 6, 8, 11, 0, tzinfo=BERLIN)))

# UNSUPPORTED_REGION: the library raises, which is the one loud failure of the three.
rows.append(row("XX-9001", "xx", 8, london(5, 6, 10, 0), london(5, 6, 15, 0)))

# ---------------------------------------------------------------------------------------------
# Filler. Arrivals spread through working weeks and resolved inside their SLA, so the corpus has a
# realistic bulk of ordinary tickets rather than only the pathological ones.
# ---------------------------------------------------------------------------------------------
rng = random.Random(17)
FILLER = (
    ("gb", LONDON, 9, 17, 40),
    ("us", NEW_YORK, 9, 17, 26),
    ("jp", TOKYO, 9, 18, 18),
)
for region, zone, open_hour, close_hour, count in FILLER:
    for index in range(count):
        month = rng.choice((2, 5, 9, 10, 11))
        day = rng.randint(1, 26)
        created = datetime(2026, month, day, rng.randint(open_hour, close_hour - 2), 0, tzinfo=zone)
        if created.weekday() >= 5:
            created += timedelta(days=2)
        resolved = created + timedelta(hours=rng.choice((1, 2, 3)))
        rows.append(
            row(f"{region.upper()}-F{index:03d}", region, 8.0, created, resolved)
        )

out = Path(__file__).parent / "tickets.jsonl"
lines = [json.dumps(item) for item in rows]

# One unreadable line and one row whose timestamp has no offset. Both are reported by name; the
# second matters because a naive timestamp assumed to be UTC would shift a Tokyo ticket by 9 hours
# and move it across the open/close boundary without any error being raised.
lines.insert(6, '{"ticket_id": "GB-BAD1", "region": "gb", not json at all}')
lines.append(
    json.dumps(
        {
            "ticket_id": "GB-BAD2",
            "region": "gb",
            "sla_business_hours": 8,
            "created_at": "2026-03-02T09:00:00",
            "resolved_at": "2026-03-02T13:00:00",
        }
    )
)

out.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"wrote {out} with {len(rows)} tickets, 2 deliberately broken lines")
print(f"reference instant NOW = {NOW.isoformat()}")
