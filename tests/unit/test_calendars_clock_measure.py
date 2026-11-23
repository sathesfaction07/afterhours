"""Unit and property tests.

Two tests exist purely to stop the others being vacuous: every `Verdict` value and every `Coverage`
value must occur in the shipped corpus. Without them, an assertion about a case the corpus never
reaches passes by never running.

Nothing is mocked. Nothing here talks to a network -- the holiday data is a local package and the
timezone data comes from the host, which is why the DST tests derive their inputs at test time
rather than hardcoding a list of dates that goes stale.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, available_timezones

import holidays
import pytest
from pydantic import ValidationError

from conftest import FRIDAY_CLOSE, MONDAY_OPEN, NOW, at
from sla_watch.amplify import (
    amplification_profile,
    gap_profile,
    sensitivity,
    worst_amplification,
)
from sla_watch.calendars import (
    GULF_WEEK,
    UTC,
    BusinessCalendar,
    check_coverage,
    holiday_set,
    overlap_hours,
    transition_risk,
)
from sla_watch.clock import (
    add_business_hours,
    business_elapsed,
    business_position,
    dead_gap_containing,
    last_close,
    next_open,
    wall_hours,
)
from sla_watch.measure import (
    build_report,
    evaluate,
    long_wait_threshold,
    simultaneous_in_business_time,
    summarise_region,
)
from sla_watch.models import (
    Bound,
    Coverage,
    Elapsed,
    Ticket,
    Verdict,
    elapsed,
    elapsed_hours,
)
from sla_watch.tickets import (
    ResolvedRegion,
    group_by_region,
    load_tickets,
    resolve_regions,
    years_present,
)


def ticket(**kwargs) -> Ticket:
    base = {"ticket_id": "T-1", "region": "gb", "sla_business_hours": 8.0}
    base.update(kwargs)
    return Ticket.model_validate(base)


def region_of(calendar: BusinessCalendar, years=(2026,)) -> ResolvedRegion:
    return ResolvedRegion(
        calendar.region.lower(), calendar, check_coverage(calendar, years), holiday_set(calendar, years)
    )


class TestBusinessTimeIsNotTranslationInvariant:
    """The asymmetry the whole project rests on."""

    def test_the_same_duration_consumes_different_business_hours(self, london, london_holidays):
        span = timedelta(hours=48)
        monday = at(3, 2, 9, 0)
        friday = at(3, 6, 17, 0)

        from_monday = business_elapsed(london, london_holidays, monday, monday + span)
        from_friday = business_elapsed(london, london_holidays, friday, friday + span)

        assert from_monday == 16.0
        assert from_friday == 0.0
        # Wall time IS translation-invariant: shift both endpoints and it does not move.
        assert wall_hours(monday, monday + span) == wall_hours(friday, friday + span) == 48.0

    def test_a_ticket_opened_and_closed_at_the_weekend_consumes_nothing(
        self, london, london_holidays
    ):
        saturday_open, saturday_close = at(3, 7, 10, 0), at(3, 7, 14, 0)
        elapsed = Elapsed(
            business_hours=business_elapsed(
                london, london_holidays, saturday_open, saturday_close
            ),
            wall_hours=wall_hours(saturday_open, saturday_close),
        )
        assert elapsed.business_hours == 0.0
        assert elapsed.wall_hours == 4.0
        # Undefined rather than zero. Zero would read as "no amplification", the opposite of true.
        assert elapsed.amplification is None
        assert elapsed.dead_hours == 4.0

    def test_dead_hours_never_goes_negative(self):
        assert Elapsed(business_hours=5.0, wall_hours=3.0).dead_hours == 0.0

    def test_elapsed_rejects_negative_input(self):
        with pytest.raises(ValidationError):
            Elapsed(business_hours=-1.0, wall_hours=1.0)

    def test_reversed_interval_is_zero_not_negative(self, london, london_holidays):
        assert business_elapsed(london, london_holidays, MONDAY_OPEN, FRIDAY_CLOSE) == 0.0
        assert wall_hours(MONDAY_OPEN, FRIDAY_CLOSE) == 0.0


class TestTheProjectionIsManyToOne:
    """Every instant in a closed stretch lands on one business-time coordinate."""

    def test_two_instants_in_one_gap_share_a_position(self, london, london_holidays):
        epoch = datetime(2026, 1, 1, tzinfo=UTC)
        friday_evening = at(3, 6, 18, 0)
        sunday_night = at(3, 8, 20, 0)

        left = business_position(london, london_holidays, friday_evening, epoch)
        right = business_position(london, london_holidays, sunday_night, epoch)

        assert left == right
        # 50 real hours apart, simultaneous in business time.
        assert wall_hours(friday_evening, sunday_night) == 50.0

    def test_add_then_measure_round_trips(self, london, london_holidays):
        for hours in (0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 40.0):
            deadline = add_business_hours(london, london_holidays, FRIDAY_CLOSE, hours)
            assert deadline is not None
            measured = business_elapsed(london, london_holidays, FRIDAY_CLOSE, deadline)
            assert measured == pytest.approx(hours, abs=1e-9)

    def test_measure_then_add_cannot_recover_the_instant(self, london, london_holidays):
        """The reverse direction loses information, because many instants share one measurement.

        My first version of this test asserted that adding zero business hours leaves the cursor
        where it was. It does not -- it advances to the next open instant. The real
        non-invertibility is coarser and more interesting: every instant in the closed stretch
        measures as 0.0 from Friday close, so the measurement cannot say which one it was.
        """
        ends = [
            at(3, 6, 17, 30),
            at(3, 7, 12, 0),
            at(3, 8, 23, 59),
            MONDAY_OPEN,
        ]
        measured = {
            business_elapsed(london, london_holidays, FRIDAY_CLOSE, end) for end in ends
        }
        assert measured == {0.0}, "four different instants, one measurement"

        # `add` has to pick one, and it picks the next open instant -- so three of the four are
        # unrecoverable and no tie-break computed from the measurement can distinguish them.
        recovered = add_business_hours(london, london_holidays, FRIDAY_CLOSE, 0.0)
        assert recovered == MONDAY_OPEN
        assert sum(1 for end in ends if end == recovered) == 1

    def test_position_is_negative_before_the_epoch(self, london, london_holidays):
        epoch = at(3, 9, 9, 0)
        earlier = at(3, 2, 9, 0)
        assert business_position(london, london_holidays, earlier, epoch) == -40.0

    def test_simultaneous_groups_need_a_real_spread(self, london, london_holidays):
        del london_holidays
        region = region_of(london)
        epoch = datetime(2026, 1, 1, tzinfo=UTC)
        same_instant = [
            ticket(ticket_id="A", created_at=at(3, 6, 18, 0)),
            ticket(ticket_id="B", created_at=at(3, 6, 18, 0)),
        ]
        # Identical arrival instants are not interesting: nothing is being hidden.
        assert simultaneous_in_business_time(same_instant, region, epoch) == ()

        spread = [
            ticket(ticket_id="A", created_at=at(3, 6, 18, 0)),
            ticket(ticket_id="B", created_at=at(3, 8, 20, 0)),
        ]
        groups = simultaneous_in_business_time(spread, region, epoch)
        assert len(groups) == 1
        assert groups[0].size == 2
        assert groups[0].spread_hours == 50.0

    def test_no_groups_without_a_usable_calendar(self, london):
        broken = ResolvedRegion("gb", london, None, frozenset())
        assert simultaneous_in_business_time([ticket(created_at=at(3, 6, 18, 0))], broken, NOW) == ()


class TestAmplificationArithmetic:
    """1 + G/B, and the two columns that move in opposite directions."""

    def test_the_closed_form_matches_a_measured_friday_arrival(self, london, london_holidays):
        gap = dead_gap_containing(london, london_holidays, at(3, 6, 18, 0))
        assert gap is not None
        assert gap.hours == 64.0
        assert gap.reason == "weekend"

        for sla, expected in ((1.0, 65.0), (2.0, 33.0), (4.0, 17.0), (8.0, 9.0)):
            deadline = add_business_hours(london, london_holidays, FRIDAY_CLOSE, sla)
            assert deadline is not None
            measured = wall_hours(FRIDAY_CLOSE, deadline) / sla
            assert measured == pytest.approx(expected)
            assert measured == pytest.approx(worst_amplification(gap.hours, sla))

    def test_tightening_the_sla_helps_the_wait_far_less_than_it_hurts_the_ratio(
        self, london, london_holidays
    ):
        curve = sensitivity(london, london_holidays, (1.0, 8.0, 40.0), 2026)
        tight, loose = curve.rows[0], curve.rows[-1]

        assert tight.sla_hours == 1.0
        assert loose.sla_hours == 40.0
        assert tight.worst_amplification > loose.worst_amplification * 10
        assert curve.wait_improvement is not None
        assert 0.0 < curve.wait_improvement < 0.60
        assert curve.amplification_ratio is not None
        assert curve.amplification_ratio > 10

    def test_the_worst_case_is_really_the_worst(self, london, london_holidays):
        """`sensitivity` starts at the longest gap. Confirm nothing beats it, by brute force."""
        for sla in (1.0, 8.0, 40.0):
            claimed = sensitivity(london, london_holidays, (sla,), 2026).rows[0]
            cursor = datetime(2026, 1, 1, tzinfo=UTC)
            end = datetime(2027, 1, 1, tzinfo=UTC)
            best = 0.0
            while cursor < end:
                due = add_business_hours(london, london_holidays, cursor, sla)
                if due is not None:
                    best = max(best, wall_hours(cursor, due) / sla)
                cursor += timedelta(hours=1)
            assert claimed.worst_amplification == pytest.approx(best, abs=0.01)

    def test_a_holiday_lengthens_the_gap_and_the_ratio_with_it(self, london, london_holidays):
        profile = gap_profile(london, london_holidays, 2026)
        assert profile.longest is not None
        assert profile.longest.reason.startswith("holiday")
        assert profile.longest_holiday_hours > profile.weekend_hours
        # 65 rather than 64: the longest weekend of the year is the one containing the October clock
        # change, which adds a real hour of closure. An earlier version of `DeadGap.hours` subtracted
        # two same-zone datetimes directly and reported 64 for it, hiding the extra hour.
        assert profile.weekend_hours == 65.0

    def test_the_longest_weekend_of_the_year_is_the_one_the_clocks_change_in(
        self, london, london_holidays
    ):
        """A consequence of measuring elapsed time properly, and a check that it stays measured.

        An ordinary London weekend is 64 hours. The autumn one is 65 and the spring one is 63.
        """
        spring = dead_gap_containing(london, london_holidays, at(3, 28, 12, 0))
        autumn = dead_gap_containing(london, london_holidays, at(10, 24, 12, 0))
        ordinary = dead_gap_containing(london, london_holidays, at(3, 7, 12, 0))

        assert ordinary is not None and ordinary.hours == 64.0
        assert spring is not None and spring.hours == 63.0
        assert autumn is not None and autumn.hours == 65.0

        # And the naive subtraction the fix removed would have called all three 64.
        for gap in (spring, autumn, ordinary):
            naive = (gap.ends_at - gap.starts_at).total_seconds() / 3600
            assert naive == 64.0
        assert spring.hours != 64.0
        assert autumn.hours != 64.0

        thursday = at(4, 2, 17, 0)
        deadline = add_business_hours(london, london_holidays, thursday, 1.0)
        assert deadline is not None
        assert wall_hours(thursday, deadline) == pytest.approx(113.0)

    def test_worst_amplification_refuses_a_zero_sla(self):
        with pytest.raises(ValueError, match="positive"):
            worst_amplification(64.0, 0.0)

    def test_being_shut_on_arrival_does_not_predict_amplification(
        self, london, london_holidays, tokyo
    ):
        """The intuitive explanation, measured and found close to anti-correlated.

        I expected a London desk serving Tokyo customers to be the worst cohort, because almost
        every such arrival lands while London is closed. It is the best of the three.
        """
        tokyo_holidays = holiday_set(tokyo, (2026,))
        own = amplification_profile(
            london, london_holidays, 8.0, 2026,
            arrival_calendar=london, arrival_holidays=london_holidays,
        )
        distant = amplification_profile(
            london, london_holidays, 8.0, 2026,
            arrival_calendar=tokyo, arrival_holidays=tokyo_holidays,
        )

        assert own.gap_share == 0.0
        assert distant.gap_share is not None and distant.gap_share > 0.9

        # And yet the distant cohort waits less than half as long.
        assert distant.mean_wall_hours < own.mean_wall_hours / 2
        assert distant.mean_dead_hours < own.mean_dead_hours / 3
        assert distant.over_4x < own.over_4x

    def test_an_empty_profile_reports_none_rather_than_zero(self, london, london_holidays):
        impossible = BusinessCalendar(
            region="GB", zone_name="Europe/London", subdivision="ENG",
            working_days=frozenset({0}), label="Mondays only",
        )
        del impossible
        profile = amplification_profile(
            london, london_holidays, 8.0, 2026,
            arrival_calendar=london, arrival_holidays=london_holidays, step_hours=100000,
        )
        assert profile.samples == 0
        assert profile.gap_share is None
        assert profile.over_4x_share is None
        assert profile.worst_amplification is None

    def test_open_fraction_is_the_long_run_floor(self, london, tokyo):
        assert london.hours_per_week == 40.0
        assert london.open_fraction == pytest.approx(40 / 168)
        assert 1 / london.open_fraction == pytest.approx(4.2)
        assert tokyo.hours_per_week == 45.0
        assert 1 / tokyo.open_fraction == pytest.approx(168 / 45)


class TestDstCannotEnterADaytimeWindow:
    """Derived from the host's tzdata at test time, not from a hardcoded list of dates."""

    def test_no_registry_calendar_sees_a_transition_inside_its_window(self):
        from sla_watch.tickets import REGISTRY

        total_scanned = total_inside = 0
        for calendar in REGISTRY.values():
            if calendar.zone_name == "Etc/UTC":
                continue
            risk = transition_risk(calendar, 2015, 2031)
            total_scanned += risk.transitions_scanned
            total_inside += risk.inside_window
            assert risk.is_immune, f"{calendar.zone_name}: {risk.examples}"
        assert total_scanned > 100, "the scan must actually find transitions"
        assert total_inside == 0

    def test_there_are_two_independent_protections_and_london_needs_only_one(self):
        """The immunity is not one property. It is two, and either alone is enough.

        My first version moved the window into the small hours and expected the immunity to break.
        It did not, because London's transitions are all SUNDAYS and the window kept a Monday-Friday
        week -- so a second protection was still holding. Both have to go.
        """
        week7 = frozenset(range(7))

        def risk(opens, closes, days=None):
            kwargs = {
                "region": "GB", "zone_name": "Europe/London", "opens": opens, "closes": closes
            }
            if days is not None:
                kwargs["working_days"] = days
            return transition_risk(BusinessCalendar(**kwargs), 2015, 2031)

        daytime_weekdays = risk(time(9), time(17))
        daytime_everyday = risk(time(9), time(17), week7)
        night_weekdays = risk(time(0, 30), time(8))
        night_everyday = risk(time(0, 30), time(8), week7)

        assert daytime_weekdays.transitions_scanned > 0
        # Protected by the HOUR alone: a seven-day week is still immune during the day.
        assert daytime_everyday.is_immune
        # Protected by the DAY alone: a night window is still immune on a Monday-Friday week.
        assert night_weekdays.is_immune
        # Both gone: every transition lands inside.
        assert not night_everyday.is_immune
        assert night_everyday.inside_window == night_everyday.transitions_scanned

    def test_a_zone_whose_transitions_are_not_sunday_has_only_one_protection(self):
        """"DST is always on a Sunday" is folklore, and relying on it is how the day protection goes.

        Israel transitions on a FRIDAY and Egypt on a Friday and a Thursday, so a Monday-Friday week
        does not exclude them and only the hour is left.
        """
        for zone, expected_weekday in (("Asia/Jerusalem", "Fri"), ("Africa/Cairo", "Fri")):
            night = transition_risk(
                BusinessCalendar(
                    region="X", zone_name=zone, opens=time(0, 30), closes=time(8)
                ),
                2015,
                2031,
            )
            assert not night.is_immune, zone
            assert night.inside_window > 0
            assert any(expected_weekday in example for example in night.examples), night.examples

            # And the daytime window is still immune, so the hour is doing the work on its own.
            assert transition_risk(
                BusinessCalendar(region="X", zone_name=zone, opens=time(9), closes=time(17)),
                2015,
                2031,
            ).is_immune

    def test_a_24_7_rota_has_no_protection_at_all(self, always_open):
        every_hour = transition_risk(always_open, 2015, 2031)
        assert every_hour.transitions_scanned > 0
        assert every_hour.inside_window == every_hour.transitions_scanned

    def test_transitions_cluster_in_the_small_hours(self):
        """The reason the immunity holds, measured rather than asserted."""
        zone = ZoneInfo("Europe/London")
        hours: Counter[int] = Counter()
        cursor = datetime(2015, 1, 1, tzinfo=UTC)
        end = datetime(2032, 1, 1, tzinfo=UTC)
        previous = cursor.astimezone(zone).utcoffset()
        while cursor < end:
            cursor += timedelta(hours=1)
            offset = cursor.astimezone(zone).utcoffset()
            if offset != previous:
                previous = offset
                hours[cursor.astimezone(zone).hour] += 1
        assert hours
        assert max(hours) <= 4, f"a transition landed at {max(hours)}:00 local"

    def test_a_transition_is_never_on_a_working_day_at_a_working_hour_across_all_zones(self):
        """The wide version of the claim, over every zone the host knows about.

        Bounded to a two-year window rather than the seventeen used in the docs, so the suite stays
        fast. The full scan lives in setup.sh and CI.
        """
        names = sorted(
            name
            for name in available_timezones()
            if "/" in name and not name.startswith(("Etc/", "SystemV"))
        )
        assert len(names) > 100
        offenders = []
        for name in names[::4]:
            try:
                zone = ZoneInfo(name)
            except Exception:
                continue
            cursor = datetime(2025, 1, 1, tzinfo=UTC)
            end = datetime(2027, 1, 1, tzinfo=UTC)
            previous = cursor.astimezone(zone).utcoffset()
            while cursor < end:
                cursor += timedelta(hours=1)
                offset = cursor.astimezone(zone).utcoffset()
                if offset == previous:
                    continue
                previous = offset
                local = cursor.astimezone(zone)
                if local.weekday() < 5 and time(9) <= local.time() < time(17):
                    offenders.append(f"{name} {local}")
        assert offenders == []

    def test_dst_moves_the_wall_number_and_leaves_business_alone(self):
        """A span across a spring-forward loses an hour of wall time and no working time."""
        jerusalem = BusinessCalendar(
            region="IL", zone_name="Asia/Jerusalem", working_days=GULF_WEEK, label="Tel Aviv"
        )
        days = holiday_set(jerusalem, (2026,))
        start = datetime(2026, 3, 26, 16, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
        end = datetime(2026, 3, 29, 10, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
        assert start.strftime("%a") == "Thu"

        # The transition is on FRIDAY at 03:00, inside the Israeli weekend.
        assert start.utcoffset() != end.utcoffset()
        naive_local = (end.replace(tzinfo=None) - start.replace(tzinfo=None)).total_seconds() / 3600
        assert naive_local == 66.0
        assert wall_hours(start, end) == 65.0
        assert business_elapsed(jerusalem, days, start, end) == 2.0

    def test_a_window_bound_inside_a_dst_gap_is_reported_rather_than_guessed(self):
        """`window_on` returns None for a local time that does not exist.

        Unreachable for the shipped calendars, which is the point: the guard is asserted directly
        because no corpus row can reach it.
        """
        gap_calendar = BusinessCalendar(
            region="GB", zone_name="Europe/London", opens=time(1, 30), closes=time(9, 0)
        )
        # 2026-03-29 01:00-02:00 local does not exist in London.
        spring_forward = date(2026, 3, 29)
        assert gap_calendar.window_on(spring_forward) is None
        assert gap_calendar.window_on(date(2026, 3, 30)) is not None

    def test_business_elapsed_skips_a_day_whose_window_does_not_exist(self):
        gap_calendar = BusinessCalendar(
            region="GB",
            zone_name="Europe/London",
            opens=time(1, 30),
            closes=time(9, 0),
            working_days=frozenset(range(7)),
        )
        start = datetime(2026, 3, 29, 0, 0, tzinfo=ZoneInfo("Europe/London"))
        end = datetime(2026, 3, 30, 0, 0, tzinfo=ZoneInfo("Europe/London"))
        # The day contributes nothing rather than a wrong number.
        assert business_elapsed(gap_calendar, frozenset(), start, end) == 0.0


class TestSameZoneSubtractionIsNotElapsedTime:
    """The bug this project actually shipped, pinned so it cannot come back.

    Python differences two aware datetimes that share a `tzinfo` by their NAIVE wall-clock values
    and ignores the offsets. Documented, silent, and it makes the answer depend on how the caller
    happened to build the timestamp.
    """

    def test_python_subtracts_same_zone_aware_datetimes_naively(self):
        """The language behaviour being defended against, asserted directly."""
        zone = ZoneInfo("Europe/London")
        before = datetime(2026, 3, 28, 12, 0, tzinfo=zone)
        after = datetime(2026, 3, 30, 12, 0, tzinfo=zone)

        assert before.utcoffset() != after.utcoffset(), "a transition falls between them"
        # Plain subtraction: 48 hours. True elapsed: 47.
        assert (after - before) == timedelta(hours=48)
        assert (after.astimezone(UTC) - before.astimezone(UTC)) == timedelta(hours=47)

    def test_the_same_instant_gives_two_answers_depending_how_it_was_built(self):
        """Why the bug survived: the corpus round-trips through `isoformat`, which is SAFE.

        `isoformat` bakes a fixed offset, and two different fixed offsets subtract correctly. So the
        shipped data produced the right number while a directly-constructed datetime did not.
        """
        zone = ZoneInfo("Europe/London")
        a = datetime(2026, 3, 28, 12, 0, tzinfo=zone)
        b = datetime(2026, 3, 30, 12, 0, tzinfo=zone)
        a2, b2 = datetime.fromisoformat(a.isoformat()), datetime.fromisoformat(b.isoformat())

        assert a == a2 and b == b2, "the same instants"
        assert (b - a) != (b2 - a2), "and two different answers"
        assert (b2 - a2) == timedelta(hours=47)

        # `elapsed` gives the same, correct answer for both spellings.
        assert elapsed(a, b) == elapsed(a2, b2) == timedelta(hours=47)
        assert elapsed_hours(a, b) == elapsed_hours(a2, b2) == 47.0

    def test_wall_hours_is_elapsed_time_for_both_spellings(self):
        zone = ZoneInfo("Asia/Jerusalem")
        a = datetime(2026, 3, 26, 16, 0, tzinfo=zone)
        b = datetime(2026, 3, 29, 10, 0, tzinfo=zone)
        assert wall_hours(a, b) == 65.0
        assert (b - a).total_seconds() / 3600 == 66.0, "the trap is still there in the language"
        assert wall_hours(
            datetime.fromisoformat(a.isoformat()), datetime.fromisoformat(b.isoformat())
        ) == 65.0

    def test_dead_gap_hours_is_elapsed_time(self, london, london_holidays):
        gap = dead_gap_containing(london, london_holidays, at(3, 28, 12, 0))
        assert gap is not None
        assert gap.hours == 63.0
        assert gap.duration == timedelta(hours=63)
        assert (gap.ends_at - gap.starts_at) == timedelta(hours=64), "the raw subtraction differs"

    def test_elapsed_is_floored_at_zero_but_the_delta_is_not(self):
        later, earlier = at(3, 9, 9, 0), at(3, 6, 17, 0)
        assert elapsed_hours(later, earlier) == 0.0
        assert elapsed(later, earlier) < timedelta(0), "the signed delta is preserved"

    def test_overlap_uses_elapsed_time(self, london, tokyo):
        # Both windows are in different zones here, so the raw subtraction happens to be right --
        # asserted so the delegation is not silently doing nothing.
        assert overlap_hours(london, tokyo, date(2026, 7, 15)) == 1.0


class TestANonWesternWeekIsNotAnEdgeCase:
    """A Saturday-Sunday weekend is an assumption. Applying it to Cairo is wrong twice over."""

    def test_the_gulf_week_treats_friday_and_saturday_as_the_weekend(self, cairo):
        days = holiday_set(cairo, (2026,))
        friday, saturday, sunday = date(2026, 5, 8), date(2026, 5, 9), date(2026, 5, 10)
        assert friday.strftime("%a") == "Fri"
        assert not cairo.is_working_day(friday, days)
        assert not cairo.is_working_day(saturday, days)
        assert cairo.is_working_day(sunday, days)

    def test_the_wrong_week_gives_a_confidently_different_answer(self, cairo, western_cairo):
        """Same tickets, same zone, same holidays -- only the working week differs."""
        days = holiday_set(cairo, (2026,))
        start = datetime(2026, 5, 7, 16, 0, tzinfo=ZoneInfo("Africa/Cairo"))
        end = datetime(2026, 5, 10, 10, 0, tzinfo=ZoneInfo("Africa/Cairo"))

        correct = business_elapsed(cairo, days, start, end)
        assumed = business_elapsed(western_cairo, days, start, end)

        assert correct == 2.0
        assert assumed == 9.0
        # Neither raises, and nothing in either number says which week was used.
        assert correct != assumed

    def test_a_calendar_with_no_working_days_is_rejected(self):
        with pytest.raises(ValidationError, match="never advance"):
            BusinessCalendar(
                region="GB", zone_name="Europe/London", working_days=frozenset()
            )

    def test_an_overnight_window_is_rejected_rather_than_silently_wrapped(self):
        with pytest.raises(ValueError, match="not after"):
            BusinessCalendar(
                region="GB", zone_name="Europe/London", opens=time(22), closes=time(6)
            )

    def test_a_midnight_close_is_rejected_as_ambiguous(self):
        with pytest.raises(ValidationError):
            BusinessCalendar(
                region="GB", zone_name="Europe/London", opens=time(9), closes=time(0)
            )

    def test_a_working_day_outside_zero_to_six_is_rejected(self):
        with pytest.raises(ValidationError, match="0 \\(Monday\\)"):
            BusinessCalendar(
                region="GB", zone_name="Europe/London", working_days=frozenset({0, 9})
            )

    def test_two_regions_can_share_a_working_day_and_no_working_hour(self, london, tokyo):
        assert overlap_hours(london, tokyo, date(2026, 1, 14)) == 0.0
        assert overlap_hours(london, tokyo, date(2026, 7, 15)) == 1.0

    def test_overlap_is_zero_when_either_window_does_not_exist(self, tokyo):
        gap_calendar = BusinessCalendar(
            region="GB", zone_name="Europe/London", opens=time(1, 30), closes=time(9, 0)
        )
        assert overlap_hours(gap_calendar, tokyo, date(2026, 3, 29)) == 0.0


class TestCoverageIsNotTheSameAsEmptiness:
    """An empty holiday set reads as a seven-day working week, so absence has to be named."""

    def test_the_library_answers_false_for_a_year_it_does_not_cover(self):
        """The behaviour being defended against, asserted directly."""
        german = holidays.country_holidays("DE", subdiv="BE")
        assert date(2026, 12, 25) in german
        assert date(1989, 12, 25) not in german
        assert len(holidays.country_holidays("DE", subdiv="BE", years=1989)) == 0

    def test_an_uncovered_year_is_caught_rather_than_read_as_no_holidays(self):
        berlin = BusinessCalendar(
            region="DE", zone_name="Europe/Berlin", subdivision="BE"
        )
        assert check_coverage(berlin, (2026,)).coverage is Coverage.COVERED
        uncovered = check_coverage(berlin, (1989,))
        assert uncovered.coverage is Coverage.YEAR_OUT_OF_RANGE
        assert not uncovered.is_usable
        assert "seven-day working week" in uncovered.detail
        assert uncovered.supported_range is not None

    def test_a_missing_subdivision_is_a_smaller_set_and_not_an_error(self):
        national = BusinessCalendar(region="GB", zone_name="Europe/London")
        england = BusinessCalendar(region="GB", zone_name="Europe/London", subdivision="ENG")

        verdict = check_coverage(national, (2026,))
        assert verdict.coverage is Coverage.SUBDIVISION_MISSING
        assert not verdict.is_usable
        assert "ENG" in verdict.detail
        assert check_coverage(england, (2026,)).coverage is Coverage.COVERED

        # And the difference is real: fewer holidays means a tighter deadline.
        assert len(holiday_set(national, (2026,))) < len(holiday_set(england, (2026,)))

    def test_an_unsupported_region_raises_and_is_reported(self):
        unmapped = BusinessCalendar(region="XX", zone_name="Etc/UTC")
        verdict = check_coverage(unmapped, (2026,))
        assert verdict.coverage is Coverage.UNSUPPORTED_REGION
        assert not verdict.is_usable
        assert verdict.detail

    def test_holiday_set_returns_empty_for_an_unsupported_region(self):
        unmapped = BusinessCalendar(region="XX", zone_name="Etc/UTC")
        assert holiday_set(unmapped, (2026,)) == frozenset()

    def test_coverage_is_checked_per_region_and_is_not_contagious(self):
        """One migrated 1989 row must not make every region uncovered.

        An earlier version checked every region against the union of years in the whole file, so a
        single ancient row refused every ticket in the dataset and looked like a broken tool.
        """
        rows = [
            ticket(ticket_id="MODERN", region="de", created_at=at(5, 4, 10, 0)),
            ticket(
                ticket_id="ANCIENT",
                region="de_archive",
                created_at=datetime(1989, 5, 4, 10, 0, tzinfo=ZoneInfo("Europe/Berlin")),
            ),
        ]
        resolved = resolve_regions(rows)
        assert resolved["de"].coverage.coverage is Coverage.COVERED
        assert resolved["de_archive"].coverage.coverage is Coverage.YEAR_OUT_OF_RANGE

    def test_years_present_reads_every_timestamp_field(self):
        rows = [
            ticket(created_at=at(3, 2, 9, 0)),
            ticket(first_activity_at=datetime(2024, 1, 1, tzinfo=UTC)),
            ticket(resolved_at=datetime(2028, 1, 1, tzinfo=UTC)),
            ticket(),
        ]
        assert years_present(rows) == (2024, 2026, 2028)

    def test_an_unregistered_region_resolves_to_nothing_usable(self):
        resolved = resolve_regions([ticket(region="atlantis", created_at=at(3, 2, 9, 0))])
        assert not resolved["atlantis"].is_usable
        assert resolved["atlantis"].coverage is None


class TestVerdicts:
    """Six outcomes, and the three that are refusals rather than results."""

    def test_a_ticket_resolved_inside_its_sla_is_met(self, london):
        result = evaluate(
            ticket(created_at=at(3, 2, 9, 30), resolved_at=at(3, 2, 12, 30)),
            region_of(london),
            NOW,
        )
        assert result.verdict is Verdict.MET
        assert result.bound is Bound.EXACT
        assert result.amplification == pytest.approx(1.0)

    def test_a_long_wait_is_still_met_and_says_so(self, london):
        result = evaluate(
            ticket(created_at=at(3, 6, 16, 0), resolved_at=at(3, 9, 10, 0)),
            region_of(london),
            NOW,
        )
        assert result.verdict is Verdict.MET_WITH_LONG_WAIT
        assert result.verdict.is_met
        assert result.verdict.is_decided
        assert result.amplification == pytest.approx(33.0)

    def test_a_breach_is_a_breach(self, london):
        result = evaluate(
            ticket(sla_business_hours=4.0, created_at=at(3, 2, 9, 0), resolved_at=at(3, 3, 14, 0)),
            region_of(london),
            NOW,
        )
        assert result.verdict is Verdict.BREACHED
        assert not result.verdict.is_met

    def test_an_open_ticket_inside_its_sla_is_open_not_met(self, london):
        result = evaluate(
            ticket(sla_business_hours=20.0, created_at=at(6, 12, 15, 0)),
            region_of(london),
            NOW,
        )
        assert result.verdict is Verdict.OPEN
        assert not result.verdict.is_decided
        assert result.bound is Bound.AT_LEAST

    def test_an_open_ticket_past_its_deadline_is_already_breached(self, london):
        """A one-sided bound settles the question when it falls on the wrong side."""
        result = evaluate(
            ticket(sla_business_hours=4.0, created_at=at(6, 1, 9, 0)),
            region_of(london),
            NOW,
        )
        assert result.verdict is Verdict.BREACHED
        assert result.bound is Bound.AT_LEAST
        assert "already past" in (result.note or "")

    def test_a_missing_start_that_cannot_settle_the_question_refuses(self, london):
        result = evaluate(
            ticket(first_activity_at=at(3, 4, 11, 0), resolved_at=at(3, 4, 15, 0)),
            region_of(london),
            NOW,
        )
        assert result.verdict is Verdict.UNKNOWN_START
        assert result.bound is Bound.AT_LEAST
        assert result.elapsed is not None
        assert "upper bound on creation" in (result.note or "")

    def test_a_missing_start_whose_lower_bound_settles_it_reports_the_breach(self, london):
        result = evaluate(
            ticket(
                sla_business_hours=4.0,
                first_activity_at=at(3, 2, 9, 0),
                resolved_at=at(3, 4, 15, 0),
            ),
            region_of(london),
            NOW,
        )
        assert result.verdict is Verdict.BREACHED
        assert result.bound is Bound.AT_LEAST
        assert "however early" in (result.note or "")

    def test_no_timestamps_at_all_has_no_bound(self, london):
        result = evaluate(ticket(resolved_at=at(3, 5, 16, 0)), region_of(london), NOW)
        assert result.verdict is Verdict.UNKNOWN_START
        assert result.elapsed is None
        assert "no lower bound" in (result.note or "")

    def test_an_unresolvable_calendar_refuses_instead_of_defaulting(self):
        unmapped = BusinessCalendar(region="XX", zone_name="Etc/UTC")
        result = evaluate(
            ticket(region="xx", created_at=at(3, 2, 9, 0), resolved_at=at(3, 2, 12, 0)),
            region_of(unmapped),
            NOW,
        )
        assert result.verdict is Verdict.NO_CALENDAR
        assert result.coverage is Coverage.UNSUPPORTED_REGION
        assert result.elapsed is None
        assert result.deadline is None

    def test_a_weekend_only_ticket_is_met_and_not_a_long_wait(self, london):
        """Amplification is undefined, so it cannot be compared against the threshold."""
        result = evaluate(
            ticket(created_at=at(3, 7, 10, 0), resolved_at=at(3, 7, 14, 0)),
            region_of(london),
            NOW,
        )
        assert result.verdict is Verdict.MET
        assert result.amplification is None

    def test_an_explicit_threshold_overrides_the_derived_one(self, london):
        payload = ticket(created_at=at(3, 6, 16, 0), resolved_at=at(3, 9, 10, 0))
        assert evaluate(payload, region_of(london), NOW, threshold=1000.0).verdict is Verdict.MET
        assert (
            evaluate(payload, region_of(london), NOW, threshold=2.0).verdict
            is Verdict.MET_WITH_LONG_WAIT
        )

    def test_a_naive_timestamp_is_rejected_not_assumed_utc(self):
        with pytest.raises(ValidationError, match="timezone-aware"):
            Ticket.model_validate(
                {
                    "ticket_id": "T-1",
                    "region": "gb",
                    "sla_business_hours": 8,
                    "created_at": "2026-03-02T09:00:00",
                }
            )

    def test_a_non_positive_sla_is_rejected(self):
        with pytest.raises(ValidationError):
            ticket(sla_business_hours=0.0)


class TestTheThresholdIsDerivedNotChosen:
    def test_it_is_the_reciprocal_of_the_open_fraction(self, london, tokyo):
        assert long_wait_threshold(london) == pytest.approx(4.2)
        assert long_wait_threshold(tokyo) == pytest.approx(3.7333, abs=1e-3)

    def test_it_moves_with_the_calendar(self):
        short_week = BusinessCalendar(
            region="GB", zone_name="Europe/London", subdivision="ENG",
            working_days=frozenset({0, 1, 2}),
        )
        assert long_wait_threshold(short_week) == pytest.approx(168 / 24)


class TestComplianceCannotExpressExperience:
    def test_a_region_at_full_compliance_can_still_have_long_waits(self, london):
        region = region_of(london)
        rows = [
            ticket(ticket_id="A", created_at=at(3, 2, 9, 30), resolved_at=at(3, 2, 12, 30)),
            ticket(ticket_id="B", created_at=at(3, 6, 16, 0), resolved_at=at(3, 9, 10, 0)),
        ]
        results = [evaluate(row, region, NOW) for row in rows]
        summary = summarise_region("gb", region, results)

        assert summary.compliance == 1.0
        assert summary.hidden_long_waits == 1
        assert summary.looks_perfect
        assert summary.worst_ticket == "B"

    def test_a_breach_stops_it_looking_perfect(self, london):
        region = region_of(london)
        rows = [
            ticket(ticket_id="A", created_at=at(3, 6, 16, 0), resolved_at=at(3, 9, 10, 0)),
            ticket(
                ticket_id="B", sla_business_hours=4.0,
                created_at=at(3, 2, 9, 0), resolved_at=at(3, 3, 14, 0),
            ),
        ]
        summary = summarise_region("gb", region, [evaluate(row, region, NOW) for row in rows])
        assert summary.compliance == 0.5
        assert summary.hidden_long_waits == 1
        assert not summary.looks_perfect

    def test_compliance_is_none_when_nothing_was_decided(self):
        unmapped = BusinessCalendar(region="XX", zone_name="Etc/UTC")
        region = region_of(unmapped)
        rows = [ticket(region="xx", created_at=at(3, 2, 9, 0), resolved_at=at(3, 2, 12, 0))]
        summary = summarise_region("xx", region, [evaluate(row, region, NOW) for row in rows])

        # Not 1.0. `all([])` is True, and a refusal reported as a perfect score is the failure.
        assert summary.compliance is None
        assert summary.decided == 0
        assert not summary.looks_perfect
        assert summary.mean_wall_hours is None
        assert summary.worst_amplification is None

    def test_an_empty_report_reports_nothing_rather_than_perfection(self):
        empty = build_report([], {}, NOW)
        assert empty.compliance is None
        assert empty.worst_amplification is None
        assert empty.regions_looking_perfect == ()
        assert empty.refused == 0


class TestPropertiesOverTheShippedCorpus:
    """These run against `data/`, so editing the corpus and breaking an invariant fails the build."""

    def test_every_verdict_value_occurs(self, report):
        """Without this, an assertion about a case the corpus never reaches passes vacuously."""
        seen = set(report.counts)
        missing = {verdict.value for verdict in Verdict} - seen
        assert missing == set(), f"corpus never produces: {sorted(missing)}"

    def test_every_coverage_value_occurs(self, report):
        seen = {summary.coverage for summary in report.regions}
        missing = set(Coverage) - seen
        assert missing == set(), f"corpus never produces: {sorted(item.value for item in missing)}"

    def test_both_deliberately_broken_rows_are_reported(self, report):
        assert len(report.problems) == 2
        assert any("not valid JSON" in problem for problem in report.problems)
        assert any("timezone-aware" in problem for problem in report.problems)

    def test_the_corpus_is_not_clean_and_says_why(self, report):
        assert not report.is_clean
        assert report.refused > 0
        assert report.hidden_long_waits > 0

    def test_some_region_reports_full_compliance_with_long_waits(self, report):
        perfect = report.regions_looking_perfect
        assert perfect, "the signature this project exists to surface must occur"
        # And the fleet-wide figure is NOT 100%, so the aggregate would prompt a look while these
        # regions individually look finished.
        assert report.compliance is not None
        assert report.compliance < 1.0

    def test_the_worst_customer_experience_is_recorded_as_a_success(self, report):
        worst = max(
            (r for r in report.results if r.amplification is not None),
            key=lambda r: r.amplification,
        )
        assert worst.amplification > 30
        assert worst.verdict.is_met
        assert worst.verdict is Verdict.MET_WITH_LONG_WAIT

    def test_named_tickets_keep_their_intended_outcomes(self, by_id):
        expected = {
            "GB-1001": Verdict.MET,
            "GB-1002": Verdict.MET_WITH_LONG_WAIT,
            "GB-1003": Verdict.BREACHED,
            "GB-1004": Verdict.OPEN,
            "GB-1005": Verdict.UNKNOWN_START,
            "GB-1006": Verdict.MET,
            "GB-1007": Verdict.BREACHED,
            "GB-1008": Verdict.MET_WITH_LONG_WAIT,
            "GB-1011": Verdict.UNKNOWN_START,
            "GB-1012": Verdict.BREACHED,
            "GB-1013": Verdict.BREACHED,
            "UK-7001": Verdict.NO_CALENDAR,
            "DA-8001": Verdict.NO_CALENDAR,
            "XX-9001": Verdict.NO_CALENDAR,
        }
        for ticket_id, verdict in expected.items():
            assert by_id[ticket_id].verdict is verdict, ticket_id

    def test_the_undefined_amplification_case_is_present(self, by_id):
        assert by_id["GB-1006"].elapsed is not None
        assert by_id["GB-1006"].elapsed.business_hours == 0.0
        assert by_id["GB-1006"].amplification is None

    def test_the_three_refusal_reasons_are_distinguished(self, by_id):
        assert by_id["UK-7001"].coverage is Coverage.SUBDIVISION_MISSING
        assert by_id["DA-8001"].coverage is Coverage.YEAR_OUT_OF_RANGE
        assert by_id["XX-9001"].coverage is Coverage.UNSUPPORTED_REGION

    def test_the_gulf_week_regions_measured_their_own_weekend(self, by_id):
        # EG-5002 spans Friday and Saturday, which are NOT working days in Cairo.
        assert by_id["EG-5002"].elapsed is not None
        assert by_id["EG-5002"].elapsed.business_hours == 2.0
        assert by_id["EG-5002"].verdict is Verdict.MET_WITH_LONG_WAIT

    def test_the_israeli_ticket_lost_an_hour_of_wall_time_and_no_working_time(self, by_id):
        result = by_id["IL-6002"]
        assert result.elapsed is not None
        assert result.elapsed.business_hours == 2.0
        assert result.elapsed.wall_hours == 65.0

    def test_the_corpus_contains_a_simultaneous_group_with_a_real_spread(
        self, gb_tickets, regions
    ):
        groups = simultaneous_in_business_time(
            gb_tickets, regions["gb"], datetime(2026, 1, 1, tzinfo=UTC)
        )
        assert groups
        widest = max(groups, key=lambda group: group.spread_hours)
        assert widest.size >= 2
        assert widest.spread_hours >= 48

    def test_open_tickets_are_measured_against_the_pinned_instant(self, report):
        assert report.generated_at == NOW

    def test_every_region_key_in_the_corpus_is_in_the_registry_or_reported(self, corpus, regions):
        tickets, _ = corpus
        for key in group_by_region(tickets):
            assert key in regions

    def test_the_holiday_library_version_travels_with_the_report(self, report):
        assert report.holidays_library == holidays.__version__
        assert report.holidays_library

    def test_the_specific_holiday_dates_this_corpus_depends_on(self):
        """Pin the dates the expected outcomes rest on.

        A holiday library upgrade can change the calendar for a PAST year, which would silently
        change a past SLA answer. This fails loudly instead.
        """
        england = holiday_set(
            BusinessCalendar(region="GB", zone_name="Europe/London", subdivision="ENG"), (2026,)
        )
        assert date(2026, 4, 3) in england, "Good Friday 2026"
        assert date(2026, 4, 6) in england, "Easter Monday 2026"
        assert date(2026, 8, 31) in england, "Summer bank holiday 2026"
        assert date(2026, 5, 4) in england, "Early May bank holiday 2026"
        assert date(2026, 5, 25) in england, "Spring bank holiday 2026"
        # A Monday the corpus relies on being a WORKING day.
        assert date(2026, 3, 2) not in england
        assert date(2026, 5, 11) not in england


class TestLoading:
    def test_a_missing_file_is_reported_rather_than_raised(self, tmp_path):
        tickets, problems = load_tickets(tmp_path / "absent.jsonl")
        assert tickets == []
        assert len(problems) == 1
        assert "not found" in problems[0]

    def test_blank_lines_and_comments_are_skipped(self, tmp_path):
        path = tmp_path / "t.jsonl"
        path.write_text(
            "\n# a comment\n"
            '{"ticket_id": "A", "region": "gb", "sla_business_hours": 8}\n\n',
            encoding="utf-8",
        )
        tickets, problems = load_tickets(path)
        assert len(tickets) == 1
        assert problems == []

    def test_a_row_missing_its_identity_is_reported_by_line(self, tmp_path):
        path = tmp_path / "t.jsonl"
        path.write_text('{"region": "gb", "sla_business_hours": 8}\n', encoding="utf-8")
        tickets, problems = load_tickets(path)
        assert tickets == []
        assert "line 1" in problems[0]
        assert "ticket_id" in problems[0]

    def test_a_row_with_no_timestamps_still_loads(self, tmp_path):
        """Deciding what to do about a missing instant is the job, not an obstacle to it."""
        path = tmp_path / "t.jsonl"
        path.write_text(
            '{"ticket_id": "A", "region": "gb", "sla_business_hours": 8}\n', encoding="utf-8"
        )
        tickets, problems = load_tickets(path)
        assert len(tickets) == 1
        assert problems == []
        assert tickets[0].effective_start is None
        assert tickets[0].start_bound is Bound.AT_LEAST


class TestClockEdges:
    def test_next_open_returns_the_instant_itself_when_already_open(self, london, london_holidays):
        during = at(3, 2, 11, 0)
        assert next_open(london, london_holidays, during) == during

    def test_next_open_skips_a_holiday(self, london, london_holidays):
        assert next_open(london, london_holidays, at(4, 3, 10, 0)) == at(4, 7, 9, 0)

    def test_last_close_returns_the_instant_itself_when_already_open(
        self, london, london_holidays
    ):
        during = at(3, 2, 11, 0)
        assert last_close(london, london_holidays, during) == during

    def test_no_dead_gap_while_the_calendar_is_open(self, london, london_holidays):
        assert dead_gap_containing(london, london_holidays, at(3, 2, 11, 0)) is None

    def test_an_overnight_gap_is_named_as_such(self, london, london_holidays):
        gap = dead_gap_containing(london, london_holidays, at(3, 3, 2, 0))
        assert gap is not None
        assert gap.reason == "overnight"
        assert gap.hours == 16.0

    def test_a_holiday_gap_names_its_dates(self, london, london_holidays):
        gap = dead_gap_containing(london, london_holidays, at(4, 4, 12, 0))
        assert gap is not None
        assert "2026-04-03" in gap.reason
        assert "2026-04-06" in gap.reason
        assert gap.duration == timedelta(hours=gap.hours)

    def test_an_absurd_span_is_refused_rather_than_looped_over(self, london, london_holidays):
        with pytest.raises(ValueError, match="guard"):
            business_elapsed(
                london,
                london_holidays,
                datetime(1000, 1, 1, tzinfo=UTC),
                datetime(2500, 1, 1, tzinfo=UTC),
            )

    def test_a_calendar_that_never_opens_returns_none_rather_than_guessing(self):
        """Every working day a holiday, right out to the iteration guard.

        The holiday set has to cover the whole guard range. A first version covered 400 days and the
        function correctly found an open Monday beyond it -- the test was wrong, not the code.
        """
        from sla_watch.clock import MAX_DAYS

        never = BusinessCalendar(
            region="GB", zone_name="Europe/London", working_days=frozenset({0})
        )
        start = at(3, 2, 9, 0)
        shut = frozenset(
            start.date() + timedelta(days=offset) for offset in range(MAX_DAYS + 2)
        )
        assert add_business_hours(never, shut, start, 1.0) is None
        assert next_open(never, shut, start) is None
        assert dead_gap_containing(never, shut, start) is None
