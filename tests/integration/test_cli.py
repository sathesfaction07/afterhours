"""CLI tests. Every command, every exit-code reason, and the JSON and CSV contents.

`check` and `report` exit 1 on the shipped corpus by design, so the tests assert the code AND the
reason -- an exit code with no explanation would pass even if the tool failed for the wrong cause.
"""

from __future__ import annotations

import csv
import json
from io import StringIO

import pytest
from click.testing import CliRunner

from sla_watch.cli import cli


@pytest.fixture
def run(monkeypatch, tmp_path):
    """Invoke the CLI from the package root so the default corpus path resolves."""
    from conftest import DATA

    monkeypatch.setenv("SW_TICKETS", str(DATA))
    monkeypatch.setenv("SW_NOW", "2026-06-15T12:00:00+00:00")
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    def invoke(*args):
        return runner.invoke(cli, list(args), catch_exceptions=False)

    return invoke


class TestDoctor:
    def test_it_runs_with_no_credentials_and_no_network(self, run):
        result = run("doctor")
        assert result.exit_code == 0
        assert "CONFIG" in result.output
        assert "CALENDARS" in result.output

    def test_it_prints_the_holiday_library_version(self, run):
        import holidays

        assert holidays.__version__ in run("doctor").output

    def test_it_explains_why_the_version_matters(self, run):
        output = run("doctor").output
        assert "PAST year can change between releases" in output

    def test_it_shows_the_derived_threshold_per_calendar(self, run):
        output = run("doctor").output
        assert "4.20" in output, "168/40 for a standard week"
        assert "3.73" in output, "168/45 for Tokyo"

    def test_it_shows_a_non_western_week(self, run):
        output = run("doctor").output
        # Sunday-Thursday renders with Friday and Saturday blanked out.
        assert "-MTWT--" in output or "SMTWT--" in output or "MTWT--S" in output, output

    def test_it_says_compliance_cannot_express_a_long_wait(self, run):
        assert "cannot express a long wait" in run("doctor").output


class TestCheck:
    def test_it_fails_on_the_shipped_corpus(self, run):
        result = run("check")
        assert result.exit_code == 1

    def test_it_names_the_unreadable_rows(self, run):
        output = run("check").output
        assert "unreadable rows: 2" in output
        assert "not valid JSON" in output
        assert "timezone-aware" in output

    def test_it_names_all_three_calendar_failures(self, run):
        output = run("check").output
        assert "subdivision_missing" in output
        assert "year_out_of_range" in output
        assert "unsupported_region" in output

    def test_it_calls_the_calendar_gap_a_configuration_problem(self, run):
        output = run("check").output
        assert "CONFIGURATION gap" in output
        assert "Friday and Saturday" in output, "the reason a default calendar is refused"

    def test_it_reports_long_waits_without_failing_on_them(self, run):
        output = run("check").output
        assert "met with a long wait" in output
        assert "Reported and NOT failed" in output
        assert "Friday afternoon gets switched off" in output

    def test_it_reports_missing_creation_instants_without_failing_on_them(self, run):
        output = run("check").output
        assert "no creation instant" in output
        assert "bounded on one side only" in output

    def test_a_clean_file_passes(self, run, tmp_path):
        """So the gate is not unconditional -- a check that always fires carries no information."""
        clean = tmp_path / "clean.jsonl"
        clean.write_text(
            json.dumps(
                {
                    "ticket_id": "OK-1",
                    "region": "gb",
                    "sla_business_hours": 8,
                    "created_at": "2026-03-02T09:30:00+00:00",
                    "resolved_at": "2026-03-02T12:30:00+00:00",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        result = CliRunner().invoke(
            cli, ["check"], env={"SW_TICKETS": str(clean)}, catch_exceptions=False
        )
        assert result.exit_code == 0
        assert "PASS" in result.output

    def test_a_long_wait_alone_does_not_fail_the_gate(self, run, tmp_path):
        """The case the gate must tolerate: a kept promise with a weekend inside it."""
        friday = tmp_path / "friday.jsonl"
        friday.write_text(
            json.dumps(
                {
                    "ticket_id": "FRI-1",
                    "region": "gb",
                    "sla_business_hours": 8,
                    "created_at": "2026-03-06T16:00:00+00:00",
                    "resolved_at": "2026-03-09T10:00:00+00:00",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        result = CliRunner().invoke(
            cli, ["check"], env={"SW_TICKETS": str(friday)}, catch_exceptions=False
        )
        assert result.exit_code == 0
        assert "met with a long wait: 1" in result.output
        assert "PASS" in result.output


class TestReport:
    def test_it_exits_one_because_the_corpus_is_not_clean(self, run):
        result = run("report")
        assert result.exit_code == 1

    def test_it_reports_compliance_and_experience_separately(self, run):
        output = run("report").output
        assert "COMPLIANCE -- did we keep the promise" in output
        assert "EXPERIENCE -- what the customer got" in output
        assert "answer different questions" in output

    def test_every_verdict_is_listed_even_at_zero(self, run):
        output = run("report").output
        for name in (
            "met",
            "met_with_long_wait",
            "breached",
            "open",
            "unknown_start",
            "no_calendar",
        ):
            assert name in output

    def test_it_names_the_regions_that_look_finished(self, run):
        output = run("report").output
        assert "REGIONS AT 100% COMPLIANCE WITH LONG WAITS" in output
        assert "these regions look finished" in output

    def test_it_counts_the_undefined_ratio_rather_than_hiding_it(self, run):
        output = run("report").output
        assert "undefined ratio" in output
        assert "consumed no working time at all" in output

    def test_sliced_adds_the_per_region_table_with_its_caveat(self, run):
        output = run("report", "--sliced").output
        assert "coverage" in output
        assert "year_out_of_range" in output
        assert "can reach 100% by luck" in output, "the small-sample caveat must travel with it"

    def test_json_carries_the_numbers_the_docs_quote(self, run):
        result = run("report", "--format", "json")
        payload = json.loads(result.output)

        assert payload["tickets"] == 114
        assert payload["counts"]["met_with_long_wait"] == 9
        assert payload["counts"]["no_calendar"] == 5
        assert payload["refused"] == 8
        assert payload["hidden_long_waits"] == 9
        assert payload["worst_amplification"] > 40
        assert payload["is_clean"] is False
        assert len(payload["problems"]) == 2
        assert set(payload["regions_looking_perfect"]) >= {"de", "jp"}

        coverages = {region["coverage"] for region in payload["regions"]}
        assert coverages == {
            "covered",
            "year_out_of_range",
            "subdivision_missing",
            "unsupported_region",
        }

    def test_json_reports_a_refused_region_as_null_not_perfect(self, run):
        payload = json.loads(run("report", "--format", "json").output)
        refused = {region["region"]: region for region in payload["regions"]}
        assert refused["xx"]["compliance"] is None
        assert refused["xx"]["decided"] == 0
        assert refused["xx"]["looks_perfect"] is False
        assert refused["de_archive"]["compliance"] is None

    def test_json_marks_a_perfect_looking_region(self, run):
        payload = json.loads(run("report", "--format", "json").output)
        regions = {region["region"]: region for region in payload["regions"]}
        assert regions["jp"]["compliance"] == 1.0
        assert regions["jp"]["hidden_long_waits"] >= 1
        assert regions["jp"]["looks_perfect"] is True
        assert regions["jp"]["threshold"] == pytest.approx(168 / 45)

    def test_csv_leaves_an_undefined_ratio_blank(self, run):
        rows = list(csv.DictReader(StringIO(run("report", "--format", "csv").output)))
        assert len(rows) == 114
        weekend = next(row for row in rows if row["ticket_id"] == "GB-1006")
        assert weekend["business_hours"] == "0.0000"
        # Blank, not 0: a spreadsheet averages a 0 and skips a blank.
        assert weekend["amplification"] == ""

        refused = next(row for row in rows if row["ticket_id"] == "XX-9001")
        assert refused["verdict"] == "no_calendar"
        assert refused["business_hours"] == ""
        assert refused["coverage"] == "unsupported_region"

    def test_out_writes_both_files(self, run, tmp_path):
        destination = tmp_path / "out"
        result = run("report", "-o", str(destination))
        assert result.exit_code == 1
        assert (destination / "report.json").exists()
        assert (destination / "tickets.csv").exists()
        assert json.loads((destination / "report.json").read_text())["tickets"] == 114


class TestAmplify:
    def test_it_publishes_the_arithmetic(self, run):
        output = run("amplify").output
        assert result_has(output, "1 + G/B")
        assert "belongs to the" in output
        assert "opposite directions" in output

    def test_the_two_columns_move_in_opposite_directions(self, run):
        output = run("amplify").output
        assert "improves the worst WAIT by" in output
        assert "worse." in output

    def test_it_shows_the_cohort_table_with_the_refuted_hypothesis(self, run):
        output = run("amplify").output
        assert "shut on arrival" in output
        assert "is not the driver" in output
        assert "DEADLINE WINDOW" in output

    def test_the_cohorts_include_the_regions_own_customers(self, run):
        output = run("amplify").output
        assert "uniform" in output
        assert "JP" in output
        assert "GB" in output

    def test_an_unknown_region_exits_two(self, run):
        result = run("amplify", "--region", "atlantis")
        assert result.exit_code == 2
        assert "unknown region" in result.output


class TestGaps:
    def test_it_names_the_longest_stretch(self, run):
        output = run("gaps").output
        assert "CLOSED STRETCHES" in output
        assert "longest weekend" in output
        assert "longest holiday stretch" in output

    def test_the_longest_weekend_includes_the_clock_change_hour(self, run):
        """65 hours, not 64. The autumn weekend really is an hour longer."""
        assert "longest weekend            65h" in run("gaps").output

    def test_it_says_the_gap_is_the_calendars_property(self, run):
        output = run("gaps").output
        assert "property of the calendar" in output
        assert "no SLA target" in output


class TestTransitions:
    def test_no_transition_lands_inside_a_working_window(self, run):
        output = run("transitions").output
        assert "0 of" in output
        assert "transitions fall inside a working window" in output

    def test_it_credits_the_schedule_rather_than_the_code(self, run):
        output = run("transitions").output
        assert "not a result of careful coding" in output
        assert "01:00 and 03:00" in output

    def test_it_says_the_immunity_does_not_generalise(self, run):
        output = run("transitions").output
        assert "does not generalise" in output
        assert "night shift" in output
        assert "24/7" in output

    def test_calendars_sharing_a_country_code_are_not_listed_twice(self, run):
        output = run("transitions").output
        # `de` and `de_archive` are the same calendar, so they collapse into one row.
        assert "de,de_archive" in output
        assert "gb,uk_national" in output


class TestOverlap:
    def test_london_and_tokyo_have_months_with_no_shared_hour(self, run):
        output = run("overlap").output
        assert "January" in output
        assert "range 0h to 1h" in output

    def test_it_says_dst_creates_the_only_overlap(self, run):
        output = run("overlap").output
        assert "no shared window" in output
        assert "DST CREATES the only overlap" in output

    def test_two_regions_in_one_zone_overlap_fully(self, run):
        output = run("overlap", "--left", "gb", "--right", "uk_national").output
        assert "range 8h to 8h" in output

    def test_an_unknown_region_exits_two(self, run):
        assert run("overlap", "--left", "atlantis").exit_code == 2


class TestQueue:
    def test_it_finds_the_planted_simultaneous_group(self, run):
        output = run("queue").output
        assert "SIMULTANEOUS IN BUSINESS TIME" in output
        assert "GB-1009" in output
        assert "GB-1010" in output

    def test_it_reports_the_real_spread_behind_one_coordinate(self, run):
        output = run("queue").output
        assert "hours of real difference" in output
        assert "many-to-one" in output

    def test_it_draws_the_queueing_consequence(self, run):
        output = run("queue").output
        assert "equally old" in output
        assert "longest-waiting customer first" in output

    def test_a_region_with_nothing_to_group_says_so(self, run):
        output = run("queue", "--region", "xx").output
        assert "none" in output


class TestExplain:
    def test_it_shows_both_measurements_for_one_ticket(self, run):
        output = run("explain", "GB-1002").output
        assert "what the SLA measures" in output
        assert "what the customer waited" in output
        assert "met_with_long_wait" in output
        assert "33.0x" in output

    def test_it_names_the_closed_stretch_the_ticket_arrived_in(self, run):
        output = run("explain", "GB-1009").output
        assert "closed stretch" in output
        assert "weekend" in output
        assert "clock" in output

    def test_it_explains_an_undefined_ratio_rather_than_printing_zero(self, run):
        output = run("explain", "GB-1006").output
        assert "undefined" in output
        assert "no working time was consumed" in output
        assert "which is the opposite" in output

    def test_it_reports_a_refusal_with_its_reason(self, run):
        output = run("explain", "UK-7001").output
        assert "no_calendar" in output
        assert "subdivision_missing" in output
        assert "ENG" in output

    def test_it_shows_a_one_sided_bound(self, run):
        output = run("explain", "GB-1013").output
        assert "at_least" in output
        assert "breached" in output
        assert "however early" in output

    def test_an_open_ticket_is_labelled_open(self, run):
        output = run("explain", "GB-1004").output
        assert "-- open" in output
        assert "lower bound" in output

    def test_a_missing_ticket_exits_two(self, run):
        result = run("explain", "NOPE-1")
        assert result.exit_code == 2
        assert "no ticket" in result.output


class TestDeadline:
    def test_the_same_promise_gives_wildly_different_waits(self, run):
        output = run("deadline").output
        assert "1.0x" in output
        assert "9.0x" in output
        assert "order of" in output
        assert "translation-invariant" in output

    def test_a_tighter_sla_makes_the_ratio_worse(self, run):
        loose = run("deadline", "--sla", "8").output
        tight = run("deadline", "--sla", "1").output
        assert "9.0x" in loose, "8 business hours from Friday close"
        # 65x, which is 1 + 64/1 -- the closed form, reproduced from the CLI.
        assert "65.0x" in tight

    def test_arrivals_inside_one_closed_stretch_share_a_deadline(self, run):
        """The many-to-one projection, visible in the output rather than only in a unit test."""
        lines = [
            line for line in run("deadline", "--sla", "1").output.splitlines()
            if "Mon 09 Mar 10:00" in line
        ]
        assert len(lines) >= 3, lines
        assert "  Fri 06 Mar 17:00" in "\n".join(lines)
        assert "  Sun 08 Mar 20:00" in "\n".join(lines)


def result_has(output: str, needle: str) -> bool:
    return needle in output
