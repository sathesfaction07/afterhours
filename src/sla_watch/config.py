"""Config. `SW_`-prefixed environment variables or a .env file."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SW_", env_file=".env", extra="ignore")

    tickets: Path = Path("data/tickets.jsonl")

    now: datetime = datetime(2026, 6, 15, 12, 0, tzinfo=ZoneInfo("UTC"))
    """The instant open tickets are measured against.

    Pinned rather than defaulting to the wall clock, because an open ticket's elapsed time grows
    with time: a report over a corpus containing one is not reproducible unless the instant it was
    measured against travels with it. Vintage reached the same conclusion for a different
    reason -- there the recorded instant said WHEN WE LOOKED, and the same label applies here.
    """

    year: int = 2026
    """The calendar year `amplify`, `gaps` and `transitions` analyse."""

    sla_hours: float = Field(default=8.0, gt=0.0)
    """The SLA used by `amplify`, in business hours."""

    long_wait_multiple: float | None = None
    """Override for the long-wait threshold.

    None means derive it from each calendar as `1 / open_fraction` -- 168/40 = 4.2 for a standard
    week, 168/45 = 3.73 for one open nine hours a day. Derived is the better default because a fixed
    number silently means something different for every calendar it is applied to.
    """

    region: str = "gb"
    """The region `amplify`, `gaps`, `transitions` and `queue` operate on by default."""

    scan_first_year: int = 2015
    scan_last_year: int = 2031
    """Range for `transitions`. Wide enough to cover several DST rule changes in both directions."""

    # No list-typed fields on purpose: Pydantic Settings parses those as JSON, so `SW_X=a,b` would
    # fail with a decode error that reads like a bug in the tool rather than a bad value. The same
    # note appears in Doubletake, for the same reason.
