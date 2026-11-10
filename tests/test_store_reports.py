"""The results database, its queries, and the three report formats."""

import csv
import io
import json
import sqlite3
import unittest
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

from benchrun.reports import report_stem, write_reports
from benchrun.reports.csv_report import COLUMNS, render_csv
from benchrun.reports.html_report import render_html
from benchrun.reports.junit_report import render_junit
from benchrun.sim.instruments import Fault
from benchrun.store import SCHEMA_VERSION, Store
from tests.support import BenchTestCase, base_plan


def new_run(store, serial="A", plan="p", status=None, reason=None):
    run_id = store.create_run(serial, plan, "1", "hash", "{}", {}, "Operator A", "1.0.0")
    if status:
        store.finish_run(run_id, status, reason)
    return run_id


def add_values(store, run_id, values, low=None, high=None, step="s", point=None):
    step_id = store.add_step(run_id, 1, step, "test")
    for value in values:
        verdict = "pass"
        if (low is not None and value < low) or (high is not None and value > high):
            verdict = "fail"
        store.add_measurement(run_id, step_id, "m", "read", point or {}, value, "V", low, high, verdict)
    store.finish_step(step_id, "pass")


class Schema(BenchTestCase):
    def test_schema_version_is_set(self):
        self.assertEqual(self.store.conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)

    def test_reopening_keeps_the_data(self):
        new_run(self.store)
        with Store(self.store.path) as again:
            self.assertEqual(len(again.find_runs()), 1)

    def test_newer_schema_is_refused(self):
        path = self.folder / "future.sqlite"
        conn = sqlite3.connect(str(path))
        conn.execute("PRAGMA user_version = 99")
        conn.close()
        with self.assertRaisesRegex(sqlite3.DatabaseError, "schema 99"):
            Store(path)

    def test_measurements_survive_a_crash_before_finish(self):
        run_id = new_run(self.store)
        add_values(self.store, run_id, [1.0])
        with Store(self.store.path) as other:
            detail = other.run_detail(run_id)
        self.assertEqual(detail["status"], "running")
        self.assertEqual(detail["steps"][0]["measurements"][0]["value"], 1.0)


class Queries(BenchTestCase):
    def test_by_serial_and_plan_newest_first(self):
        first = new_run(self.store, "A", "p1", "pass")
        new_run(self.store, "B", "p1", "fail")
        third = new_run(self.store, "A", "p2", "aborted", "x")
        self.assertEqual([r["id"] for r in self.store.find_runs(serial="A")], [third, first])
        self.assertEqual(len(self.store.find_runs(plan="p1")), 2)
        self.assertEqual(len(self.store.find_runs(serial="A", plan="p1")), 1)
        self.assertEqual([r["id"] for r in self.store.find_runs(status="aborted")], [third])
        self.assertEqual(len(self.store.find_runs(limit=1)), 1)
        self.assertEqual(self.store.find_runs(serial="nope"), [])

    def test_counts_per_run(self):
        run_id = new_run(self.store, status="fail")
        add_values(self.store, run_id, [1.0, 2.0, 3.0], high=2.5)
        row = self.store.find_runs()[0]
        self.assertEqual((row["measurements"], row["passed"], row["failed"], row["errors"]), (3, 2, 1, 0))

    def test_run_without_measurements_has_zero_counts(self):
        new_run(self.store, status="aborted", reason="no connection")
        row = self.store.find_runs()[0]
        self.assertEqual((row["measurements"], row["passed"], row["failed"]), (0, 0, 0))
        self.assertEqual(row["reason"], "no connection")

    def test_missing_run(self):
        self.assertIsNone(self.store.run_detail(999))

    def test_first_pass_yield_ignores_retests_and_aborts(self):
        new_run(self.store, "A", "p", "fail")
        new_run(self.store, "A", "p", "pass")
        new_run(self.store, "B", "p", "aborted", "x")
        new_run(self.store, "B", "p", "pass")
        new_run(self.store, "C", "p", "aborted", "x")
        new_run(self.store, "D", "p", "pass")
        new_run(self.store, "E", "other", "pass")
        summary = self.store.plan_summary("p")
        self.assertEqual(summary["runs"], 6)
        self.assertEqual(summary["serials"], 4)
        self.assertEqual(summary["counts"]["pass"], 3)
        self.assertEqual(summary["counts"]["fail"], 1)
        self.assertEqual(summary["counts"]["aborted"], 2)
        self.assertEqual(summary["units_with_verdict"], 3)
        self.assertEqual(summary["first_pass_units"], 2)
        self.assertAlmostEqual(summary["first_pass_yield"], 2 / 3)

    def test_yield_with_no_verdicts(self):
        new_run(self.store, "A", "p", "aborted", "x")
        self.assertIsNone(self.store.plan_summary("p")["first_pass_yield"])

    def test_stats_match_hand_arithmetic(self):
        for value in (4.9, 5.0, 5.1):
            run_id = new_run(self.store, status="pass")
            add_values(self.store, run_id, [value], low=4.5, high=5.5)
        stats = self.store.measurement_stats("p")
        self.assertEqual(len(stats), 1)
        row = stats[0]
        self.assertEqual(row["count"], 3)
        self.assertAlmostEqual(row["mean"], 5.0)
        self.assertAlmostEqual(row["stdev"], 0.1)
        self.assertEqual(row["minimum"], 4.9)
        self.assertEqual(row["maximum"], 5.1)
        self.assertAlmostEqual(row["ppk"], 0.5 / 0.3)

    def test_stats_keep_sweep_points_apart(self):
        run_id = new_run(self.store, status="pass")
        step_id = self.store.add_step(run_id, 1, "s", "test")
        self.store.add_measurement(run_id, step_id, "m", "read", {"v": 1.0}, 1.0, "V", None, None, "info")
        self.store.add_measurement(run_id, step_id, "m", "read", {"v": 2.0}, 2.0, "V", None, None, "info")
        stats = self.store.measurement_stats("p")
        self.assertEqual([s["point"] for s in stats], [{"v": 1.0}, {"v": 2.0}])
        self.assertIsNone(stats[0]["ppk"])
        self.assertIsNone(stats[0]["stdev"])

    def test_stats_leave_out_aborted_runs(self):
        good = new_run(self.store, status="pass")
        add_values(self.store, good, [1.0])
        bad = new_run(self.store, status="aborted", reason="x")
        add_values(self.store, bad, [100.0])
        self.assertEqual(self.store.measurement_stats("p")[0]["count"], 1)

    def test_stats_for_one_serial(self):
        add_values(self.store, new_run(self.store, "A", status="pass"), [1.0])
        add_values(self.store, new_run(self.store, "B", status="pass"), [2.0])
        self.assertEqual(self.store.measurement_stats("p", serial="B")[0]["mean"], 2.0)

    def test_stats_keep_different_limits_and_units_apart(self):
        def add(value, units, low, high):
            run_id = new_run(self.store, status="pass")
            step_id = self.store.add_step(run_id, 1, "s", "test")
            self.store.add_measurement(run_id, step_id, "m", "read", {}, value, units, low, high, "pass")

        add(5.0, "V", 4.9, 5.1)
        add(5.05, "V", 4.9, 5.1)
        add(3.3, "V", 3.2, 3.4)
        add(5000.0, "mV", 4900.0, 5100.0)
        rows = [(s["units"], s["low"], s["high"], s["count"]) for s in self.store.measurement_stats("p")]
        self.assertEqual(rows, [("V", 4.9, 5.1, 2), ("V", 3.2, 3.4, 1), ("mV", 4900.0, 5100.0, 1)])

    def test_one_sided_ppk(self):
        for value in (1.0, 2.0, 3.0):
            add_values(self.store, new_run(self.store, status="pass"), [value], high=5.0)
        self.assertAlmostEqual(self.store.measurement_stats("p")[0]["ppk"], 3.0 / 3.0)


class Reports(BenchTestCase):
    def pass_detail(self):
        _, detail, _ = self.run_plan(base_plan(), serial="SN-OK")
        return detail

    def fail_detail(self):
        _, detail, _ = self.run_plan(base_plan(), serial="SN-BAD", dut="high-output")
        return detail

    def abort_detail(self):
        _, detail, _ = self.run_plan(base_plan(), serial="SN-AB", faults={"dmm": Fault("drop", 1)})
        return detail

    def suite(self, detail):
        return ET.fromstring(render_junit(detail)).find("testsuite")

    def test_junit_pass(self):
        suite = self.suite(self.pass_detail())
        self.assertEqual(suite.get("name"), "small")
        self.assertEqual(suite.get("tests"), "5")
        self.assertEqual(suite.get("failures"), "0")
        self.assertEqual(suite.get("errors"), "0")
        self.assertEqual(suite.get("skipped"), "0")

    def test_junit_properties_carry_traceability(self):
        properties = {p.get("name"): p.get("value") for p in self.suite(self.pass_detail()).iter("property")}
        self.assertEqual(properties["serial"], "SN-OK")
        self.assertEqual(properties["run_status"], "pass")
        self.assertIn("SIM-DMM", properties["idn.dmm"])
        self.assertIn("127.0.0.1", properties["address.dmm"])
        self.assertEqual(len(properties["plan_sha256"]), 64)

    def test_junit_fail_names_value_and_limits(self):
        suite = self.suite(self.fail_detail())
        self.assertEqual(suite.get("failures"), "4")
        failure = suite.find("testcase/failure")
        self.assertIn("outside limits 4.9 to 5.1", failure.get("message"))
        names = [c.get("name") for c in suite.iter("testcase")]
        self.assertIn("vout[vin=8]", names)

    def test_junit_abort_shows_error_and_skips(self):
        suite = self.suite(self.abort_detail())
        self.assertEqual(suite.get("errors"), "1")
        self.assertEqual(suite.get("skipped"), "1")
        self.assertRegex(suite.find("testcase/error").get("message"), "closed|lost")

    def test_junit_abort_with_no_measurements_is_not_green(self):
        data = base_plan()
        data["instruments"]["dmm"]["expect_idn"] = "nothing like it"
        _, detail, _ = self.run_plan(data)
        suite = self.suite(detail)
        self.assertGreaterEqual(int(suite.get("errors")), 1)

    def test_junit_rejected_setting_is_an_error_case(self):
        data = base_plan()
        data["steps"][0]["sweep"] = {"var": "vin", "values": [99]}
        _, detail, _ = self.run_plan(data)
        self.assertGreaterEqual(int(self.suite(detail).get("errors")), 1)

    def test_junit_unfinished_run_is_not_green(self):
        run_id = new_run(self.store)
        suite = self.suite(self.store.run_detail(run_id))
        self.assertEqual(suite.get("errors"), "1")

    def test_junit_survives_control_characters(self):
        run_id = new_run(self.store, serial="A\x01B", status="aborted", reason="bad \x07 byte")
        self.assertIn("A?B", render_junit(self.store.run_detail(run_id)))

    def test_csv_has_one_row_per_measurement(self):
        rows = list(csv.DictReader(io.StringIO(render_csv(self.fail_detail()))))
        self.assertEqual(len(rows), 5)
        self.assertEqual(tuple(rows[0].keys()), COLUMNS)
        first = rows[0]
        self.assertEqual(first["serial"], "SN-BAD")
        self.assertEqual(first["step"], "vout_vs_vin")
        self.assertEqual(json.loads(first["point"]), {"vin": 8.0})
        self.assertAlmostEqual(float(first["value"]), 5.246, places=6)
        self.assertEqual(first["verdict"], "fail")
        self.assertAlmostEqual(float(first["low"]), 4.9)

    def test_csv_keeps_full_precision(self):
        rows = list(csv.DictReader(io.StringIO(render_csv(self.pass_detail()))))
        self.assertAlmostEqual(float(rows[0]["value"]), 4.996, places=9)

    def test_csv_missing_value_is_empty_not_zero(self):
        rows = list(csv.DictReader(io.StringIO(render_csv(self.abort_detail()))))
        error = next(row for row in rows if row["verdict"] == "error")
        self.assertEqual(error["value"], "")
        self.assertRegex(error["note"], "closed|lost")

    def test_csv_shows_skipped_steps_and_the_reason(self):
        rows = list(csv.DictReader(io.StringIO(render_csv(self.abort_detail()))))
        self.assertEqual((rows[-1]["step"], rows[-1]["step_status"]), ("final_check", "skipped"))
        self.assertEqual(rows[-1]["measurement"], "")
        self.assertEqual(rows[-1]["run_status"], "aborted")
        self.assertRegex(rows[-1]["run_reason"], "closed|lost")
        self.assertTrue(all(len(row) == len(COLUMNS) and None not in row for row in rows))

    def test_csv_for_a_run_with_no_steps_still_says_what_happened(self):
        run_id = new_run(self.store, serial="NC", status="aborted", reason="cannot connect")
        rows = list(csv.DictReader(io.StringIO(render_csv(self.store.run_detail(run_id)))))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["run_status"], "aborted")
        self.assertEqual(rows[0]["run_reason"], "cannot connect")
        self.assertEqual(rows[0]["serial"], "NC")

    def test_html_is_well_formed_and_complete(self):
        text = render_html(self.fail_detail())

        class Checker(HTMLParser):
            def __init__(self):
                super().__init__()
                self.stack = []

            def handle_starttag(self, tag, attrs):
                if tag not in ("meta", "circle", "rect", "polyline"):
                    self.stack.append(tag)

            def handle_endtag(self, tag):
                if tag not in ("circle", "rect", "polyline"):
                    self.stack.pop()

        checker = Checker()
        checker.feed(text)
        self.assertEqual(checker.stack, [])
        self.assertIn("SN-BAD", text)
        self.assertIn('class="badge fail"', text)
        self.assertIn("benchrun,SIM-DMM,SIM0001,1.0", text)
        self.assertIn("<svg", text)
        self.assertNotIn("<script", text)
        self.assertNotIn("http://", text.replace("http://www.w3.org", ""))

    def test_html_escapes_everything_from_outside(self):
        run_id = new_run(self.store, serial="<script>alert(1)</script>", status="aborted", reason="<img src=x>")
        step_id = self.store.add_step(run_id, 1, "<b>step</b>", "test")
        self.store.add_measurement(run_id, step_id, "<i>m</i>", "read", {}, 1.0, "<u>", None, None, "info", "<note>")
        self.store.finish_step(step_id, "error", "<msg>")
        instrument = self.store.add_instrument(run_id, "<n>", "dmm", "<a>")
        self.store.set_instrument_idn(instrument, "<idn>")
        text = render_html(self.store.run_detail(run_id))
        for raw in ("<script>", "<img", "<b>", "<i>", "<u>", "<note>", "<msg>", "<idn>", "<n>", "<a>"):
            self.assertNotIn(raw, text, raw)
        self.assertIn("&lt;img src=x&gt;", text)

    def test_html_shows_why_a_run_aborted(self):
        text = render_html(self.abort_detail())
        self.assertIn("Why the run was aborted", text)
        self.assertRegex(text, "closed by the instrument|lost")

    def test_html_plot_handles_flat_values(self):
        run_id = new_run(self.store, status="pass")
        step_id = self.store.add_step(run_id, 1, "s", "test")
        for x in (1.0, 2.0, 3.0):
            self.store.add_measurement(run_id, step_id, "m", "read", {"x": x}, 5.0, "V", None, 6.0, "pass")
        self.assertIn("<svg", render_html(self.store.run_detail(run_id)))

    def test_html_plot_has_finite_coordinates_when_all_x_match(self):
        run_id = new_run(self.store, status="pass")
        step_id = self.store.add_step(run_id, 1, "s", "test")
        for y in (1.0, 2.0, 3.0):
            self.store.add_measurement(run_id, step_id, "m", "read", {"x": 1.0}, y, "V", 0.0, 6.0, "pass")
        text = render_html(self.store.run_detail(run_id))
        self.assertNotIn("nan", text)
        self.assertNotIn("inf", text.replace("info", ""))

    def test_file_names_are_safe(self):
        self.assertEqual(report_stem({"id": 7, "serial": "../../etc/pass wd"}), "run-000007-etc_pass_wd")
        self.assertEqual(report_stem({"id": 7, "serial": "///"}), "run-000007-unit")

    def test_write_reports_creates_each_format(self):
        detail = self.pass_detail()
        paths = write_reports(detail, self.folder / "made" / "here")
        self.assertEqual(sorted(p.suffix for p in paths), [".csv", ".html", ".xml"])
        for path in paths:
            self.assertGreater(path.stat().st_size, 100)
            self.assertEqual(path.parent, self.folder / "made" / "here")
        ET.parse(str(next(p for p in paths if p.suffix == ".xml")))

    def test_write_reports_one_format(self):
        paths = write_reports(self.pass_detail(), self.folder, ("csv",))
        self.assertEqual([p.suffix for p in paths], [".csv"])


if __name__ == "__main__":
    unittest.main()
