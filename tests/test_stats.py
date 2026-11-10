"""Production statistics: every formula against hand arithmetic, the queries, and the stats command."""

import contextlib
import csv
import io
import json
import math
import shutil
import sqlite3
import tempfile
import time
import unittest
from html.parser import HTMLParser
from pathlib import Path

from benchrun.cli import EXIT_PASS, EXIT_USAGE, main
from benchrun.stats import command, engine, query
from benchrun.stats.query import Selection
from benchrun.store import Store, now_iso
from tests.support import BenchTestCase

ROOT = Path(__file__).parent.parent
QUICK = str(ROOT / "examples" / "regulator_quick.json")


def call(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main([str(a) for a in argv])
        except SystemExit as exc:  # argparse
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def new_run(store, serial="SN-1001", plan="p", status="pass", partial=False, **extra):
    run_id = store.create_run(
        serial, plan, "1", "hash", "{}", {}, "Operator A", "1.0.0", selected_steps=["s"] if partial else None, **extra
    )
    if status != "running":
        store.finish_run(run_id, status, "x" if status == "aborted" else None)
    return run_id


def measure(store, run_id, values, low=None, high=None, step="s", name="m", point=None, units="V"):
    step_id = store.add_step(run_id, 1, step, "test")
    for value in values:
        verdict = "info" if low is None and high is None else "pass"
        if value is None or (low is not None and value < low) or (high is not None and value > high):
            verdict = "fail"
        store.add_measurement(run_id, step_id, name, "read", point or {}, value, units, low, high, verdict)
    store.finish_step(step_id, "pass")


def set_started(store, run_id, when):
    with store.conn:
        store.conn.execute("UPDATE runs SET started_at = ? WHERE id = ?", (when, run_id))


class Descriptive(unittest.TestCase):
    def test_mean_and_sample_stdev(self):
        values = [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]
        self.assertEqual(engine.mean(values), 5.0)
        self.assertAlmostEqual(engine.sample_stdev(values), math.sqrt(32.0 / 7.0), places=12)

    def test_mean_is_exactly_rounded(self):
        values = [1e100, 1.0, -1e100]
        self.assertEqual(engine.mean(values), 1.0 / 3.0)

    def test_identical_values_have_no_spread_and_their_own_mean(self):
        values = [3.3, 3.3, 3.3]
        self.assertEqual(engine.mean(values), 3.3)
        self.assertEqual(engine.sample_stdev(values), 0.0)

    def test_one_value_and_none(self):
        self.assertEqual(engine.mean([7.0]), 7.0)
        self.assertIsNone(engine.sample_stdev([7.0]))
        self.assertIsNone(engine.mean([]))
        summary = engine.summarize([])
        self.assertEqual(summary["count"], 0)
        self.assertIsNone(summary["median"])
        self.assertIsNone(summary["minimum"])

    def test_percentiles_interpolate_between_closest_ranks(self):
        ordered = [1.0, 2.0, 3.0, 4.0]
        self.assertEqual(engine.percentile(ordered, 0), 1.0)
        self.assertEqual(engine.percentile(ordered, 100), 4.0)
        self.assertEqual(engine.percentile(ordered, 50), 2.5)
        self.assertEqual(engine.percentile(ordered, 25), 1.75)  # Rank 0.75.
        self.assertAlmostEqual(engine.percentile(ordered, 95), 3.85)  # Rank 2.85.
        self.assertEqual(engine.percentile([5.0], 99), 5.0)
        self.assertEqual(engine.percentile([1.0, 2.0, 3.0], 50), 2.0)

    def test_summary(self):
        summary = engine.summarize([3.0, 1.0, 2.0, 4.0])
        self.assertEqual((summary["count"], summary["minimum"], summary["median"], summary["maximum"]), (4, 1.0, 2.5, 4.0))
        self.assertEqual(sorted(summary["percentiles"]), sorted(f"p{q}" for q in engine.PERCENTILES))

    def test_values_near_the_float_limit_never_give_inf(self):
        big = [1.7e308, 1.7e308]
        self.assertEqual(engine.mean(big), 1.7e308)
        self.assertEqual(engine.sample_stdev(big), 0.0)
        wide = [1.7e308, -1.7e308, 1.7e308]
        self.assertIsNone(engine.sample_stdev(wide))
        self.assertEqual(engine.capability(wide, 0.0, 1.0)["note"], "values too large to compute")
        self.assertEqual(engine.control_chart(wide)["note"], "values too large to compute")
        self.assertTrue(math.isfinite(engine.percentile(sorted(wide), 50)))
        self.assertTrue(math.isfinite(engine.percentile([-1.7e308, 1.7e308], 30)))


class Capability(unittest.TestCase):
    def test_symmetric_hand_values(self):
        # Mean 5.0, overall sigma 0.1, moving ranges 0.1 and 0.1, so within sigma 0.1 / 1.128.
        c = engine.capability([4.9, 5.0, 5.1], 4.5, 5.5)
        self.assertAlmostEqual(c["sigma_overall"], 0.1)
        self.assertAlmostEqual(c["sigma_within"], 0.1 / 1.128)
        self.assertAlmostEqual(c["cp"], 1.0 * 1.128 / 0.6)
        self.assertAlmostEqual(c["cpk"], 0.5 * 1.128 / 0.3)
        self.assertAlmostEqual(c["pp"], 1.0 / 0.6)
        self.assertAlmostEqual(c["ppk"], 0.5 / 0.3)
        self.assertIsNone(c["note"])

    def test_off_centre_hand_values(self):
        # Mean 2.5; overall sigma sqrt(5/3); moving ranges 2, 1, 2, so within sigma (5/3) / 1.128.
        c = engine.capability([1.0, 3.0, 2.0, 4.0], 0.0, 10.0)
        within = (5.0 / 3.0) / 1.128
        overall = math.sqrt(5.0 / 3.0)
        self.assertAlmostEqual(c["cp"], 10.0 / (6 * within))
        self.assertAlmostEqual(c["cpk"], 2.5 / (3 * within))  # The low side is nearer.
        self.assertAlmostEqual(c["pp"], 10.0 / (6 * overall))
        self.assertAlmostEqual(c["ppk"], 2.5 / (3 * overall))
        self.assertAlmostEqual(c["cpk"], 0.564)

    def test_order_changes_cpk_but_not_ppk(self):
        steady = engine.capability([1.0, 1.0, 1.0, 3.0, 3.0, 3.0], 0.0, 4.0)
        jumpy = engine.capability([1.0, 3.0, 1.0, 3.0, 1.0, 3.0], 0.0, 4.0)
        self.assertAlmostEqual(steady["ppk"], jumpy["ppk"])
        self.assertGreater(steady["cpk"], jumpy["cpk"])

    def test_one_sided_limits(self):
        upper = engine.capability([1.0, 2.0, 3.0], None, 5.0)
        self.assertIsNone(upper["cp"])
        self.assertIsNone(upper["pp"])
        self.assertAlmostEqual(upper["ppk"], 3.0 / 3.0)
        self.assertAlmostEqual(upper["cpk"], 3.0 / (3 * (1.0 / 1.128)))
        lower = engine.capability([1.0, 2.0, 3.0], 0.5, None)
        self.assertAlmostEqual(lower["ppk"], 1.5 / 3.0)
        self.assertIsNone(lower["cp"])

    def test_nothing_reported_without_grounds(self):
        for values, low, high, note in (
            ([1.0, 2.0], None, None, "no limits"),
            ([1.0], 0.0, 2.0, "fewer than 2 values"),
            ([], 0.0, 2.0, "fewer than 2 values"),
            ([1.0, 1.0, 1.0], 0.0, 2.0, "no spread"),
            ([0.1] * 53, 0.0, 2.0, "no spread"),
        ):
            with self.subTest(values=values[:3], low=low, high=high):
                c = engine.capability(values, low, high)
                self.assertEqual(c["note"], note)
                self.assertEqual([c[k] for k in ("cp", "cpk", "pp", "ppk")], [None] * 4)


class Histogram(unittest.TestCase):
    def test_sturges(self):
        self.assertEqual([engine.sturges_bins(n) for n in (1, 2, 3, 8, 9, 100, 1000)], [1, 2, 3, 4, 5, 8, 11])

    def test_binning_includes_the_top_in_the_last_bin(self):
        h = engine.histogram([0.0, 1.0, 2.0, 3.0, 4.0], bins=2)
        self.assertEqual(h["edges"], [0.0, 2.0, 4.0])
        self.assertEqual(h["counts"], [2, 3])
        self.assertEqual(h["rule"], "given")

    def test_default_rule_and_counts_add_up(self):
        values = [float(i % 17) for i in range(100)]
        h = engine.histogram(values)
        self.assertEqual((h["rule"], h["bins"], sum(h["counts"])), ("Sturges", 8, 100))
        self.assertEqual(len(h["edges"]), 9)

    def test_no_spread_is_one_bin(self):
        self.assertEqual(engine.histogram([2.0, 2.0], bins=10), {"rule": "given", "bins": 1, "edges": [2.0, 2.0], "counts": [2]})

    def test_empty_and_huge_ranges(self):
        self.assertIsNone(engine.histogram([]))
        h = engine.histogram([-1.7e308, 0.0, 1.7e308], bins=4)
        self.assertTrue(all(math.isfinite(edge) for edge in h["edges"]))
        self.assertEqual(sum(h["counts"]), 3)


class Rules(unittest.TestCase):
    def broken(self, z):
        return engine.western_electric(z)

    def test_rule_1(self):
        self.assertEqual(self.broken([0.0, 3.5, 0.0, -3.2, 3.0])[1], [1, 3])

    def test_rule_2_needs_two_of_three_on_one_side(self):
        self.assertEqual(self.broken([2.5, 0.0, 2.1])[2], [0, 2])
        self.assertEqual(self.broken([-2.5, -2.1, 0.0])[2], [0, 1])
        self.assertEqual(self.broken([2.5, -2.5, 0.0])[2], [])
        self.assertEqual(self.broken([2.5, 2.0, 0.0])[2], [])
        self.assertEqual(self.broken([2.5, 0.0, 0.0, 2.5])[2], [])
        self.assertEqual(self.broken([2.5, 2.5])[2], [])  # No full window of three.

    def test_rule_3_needs_four_of_five_on_one_side(self):
        self.assertEqual(self.broken([1.5, 1.5, 0.0, 1.5, 1.5])[3], [0, 1, 3, 4])
        self.assertEqual(self.broken([1.5, 1.5, 0.0, 1.5, -1.5])[3], [])
        self.assertEqual(self.broken([-1.2, -1.2, -1.2, -1.2, 5.0])[3], [0, 1, 2, 3])

    def test_rule_4_eight_in_a_row(self):
        self.assertEqual(self.broken([0.1] * 8)[4], list(range(8)))
        self.assertEqual(self.broken([0.1] * 7)[4], [])
        self.assertEqual(self.broken([0.1] * 7 + [0.0] + [0.1] * 7)[4], [])
        self.assertEqual(self.broken([-0.1] * 10)[4], list(range(10)))
        self.assertEqual(self.broken([0.1] * 4 + [-0.1] * 8)[4], list(range(4, 12)))

    def test_a_long_run_is_linear(self):
        started = time.monotonic()
        result = self.broken([0.5] * 200000)
        self.assertEqual(len(result[4]), 200000)
        self.assertLess(time.monotonic() - started, 5.0)

    def test_control_chart_by_hand(self):
        chart = engine.control_chart([1.0, 3.0, 2.0, 4.0])
        sigma = (5.0 / 3.0) / 1.128
        self.assertAlmostEqual(chart["centre"], 2.5)
        self.assertAlmostEqual(chart["sigma"], sigma)
        self.assertAlmostEqual(chart["ucl"], 2.5 + 3 * sigma)
        self.assertAlmostEqual(chart["lcl"], 2.5 - 3 * sigma)
        self.assertAlmostEqual(chart["mr_centre"], 5.0 / 3.0)
        self.assertAlmostEqual(chart["mr_ucl"], 3.267 * 5.0 / 3.0)
        self.assertEqual(chart["moving_ranges"], [None, 2.0, 1.0, 2.0])
        self.assertEqual(chart["rules"], {1: [], 2: [], 3: [], 4: []})

    def test_control_chart_catches_a_shift(self):
        # The centre is 10.114: the alternating values straddle it, the last 10.2 and the 10.15s do not.
        chart = engine.control_chart([10.0, 10.2] * 10 + [10.15] * 8)
        self.assertEqual(chart["rules"], {1: [], 2: [], 3: [], 4: list(range(19, 28))})

    def test_control_chart_catches_an_outlier(self):
        # Average moving range (19 * 0.2 + 2.8) / 20 = 0.33, so 13.0 is about 9 sigma above the centre.
        chart = engine.control_chart([10.0, 10.2] * 10 + [13.0])
        self.assertEqual(chart["rules"][1], [20])
        self.assertEqual(chart["rules"][4], list(range(20)))  # The outlier lifts the centre above every other point.

    def test_control_chart_needs_values_and_spread(self):
        self.assertEqual(engine.control_chart([1.0])["note"], "fewer than 2 values")
        self.assertEqual(engine.control_chart([2.0, 2.0, 2.0])["note"], "no spread")


class Queries(BenchTestCase):
    def test_yield_counts_first_and_last_complete_runs(self):
        s = self.store
        new_run(s, "A", status="fail")
        new_run(s, "A", status="pass")
        new_run(s, "B", status="pass")
        new_run(s, "C", status="fail")
        new_run(s, "C", status="fail")
        new_run(s, "D", status="aborted")
        new_run(s, "D", status="pass")
        new_run(s, "E", status="pass", partial=True)
        new_run(s, "F", status="running")
        new_run(s, "G", plan="other")
        (entry,) = query.yield_by_plan(s.conn, Selection(plan="p"))
        self.assertEqual(entry["runs"], 9)
        self.assertEqual(entry["counts"], {"pass": 3, "fail": 3, "aborted": 1, "running": 1})
        self.assertEqual(entry["partial_runs"], 1)
        self.assertEqual(entry["serials"], 6)
        self.assertEqual(entry["units_with_verdict"], 4)
        self.assertEqual(entry["first_pass_units"], 2)
        self.assertAlmostEqual(entry["first_pass_yield"], 0.5)
        self.assertEqual(entry["final_pass_units"], 3)
        self.assertAlmostEqual(entry["final_yield"], 0.75)
        self.assertEqual((entry["retests"], entry["retested_units"]), (2, 2))
        totals = query.run_totals([entry])
        self.assertEqual(totals, {"selected": 9, "complete": 6, "aborted": 1, "running": 1, "partial": 1})

    def test_yield_follows_start_time_not_insertion_order(self):
        late = new_run(self.store, "A", status="pass")
        early = new_run(self.store, "A", status="fail")
        set_started(self.store, late, "2026-02-01T00:00:00.000+00:00")
        set_started(self.store, early, "2026-01-01T00:00:00.000+00:00")
        (entry,) = query.yield_by_plan(self.store.conn, Selection(plan="p"))
        self.assertEqual((entry["first_pass_units"], entry["final_pass_units"]), (0, 1))

    def test_groups_leave_out_incomplete_runs_and_missing_values(self):
        measure(self.store, new_run(self.store), [1.0, None, float("inf")], high=5.0)
        measure(self.store, new_run(self.store, status="aborted"), [100.0])
        measure(self.store, new_run(self.store, status="running"), [100.0])
        measure(self.store, new_run(self.store, partial=True), [100.0])
        (group,) = query.measurement_groups(self.store.conn, Selection(plan="p"))
        self.assertEqual(group["values"], [1.0])
        self.assertEqual((group["readings"], group["no_value"], group["fails"]), (3, 2, 2))

    def test_limit_sets_are_split_and_numbered(self):
        measure(self.store, new_run(self.store), [5.0], 4.9, 5.1)
        measure(self.store, new_run(self.store), [5.02], 4.8, 5.2)
        measure(self.store, new_run(self.store), [5.01], 4.9, 5.1)
        measure(self.store, new_run(self.store), [3.3], 3.2, 3.4, name="other")
        groups = query.measurement_groups(self.store.conn, Selection(plan="p"))
        rows = [(g["name"], g["low"], g["limit_set"], g["limit_sets"], g["values"]) for g in groups]
        self.assertEqual(rows, [("m", 4.9, 1, 2, [5.0, 5.01]), ("m", 4.8, 2, 2, [5.02]), ("other", 3.2, 1, 1, [3.3])])
        summary = query.measurement_summary(groups[0])
        self.assertAlmostEqual(summary["capability"]["ppk"], (5.1 - 5.005) / (3 * engine.sample_stdev([5.0, 5.01])))

    def test_sweep_points_and_units_are_their_own_groups(self):
        run_id = new_run(self.store)
        measure(self.store, run_id, [1.0], point={"vin": 8.0})
        measure(self.store, run_id, [2.0], point={"vin": 16.0})
        measure(self.store, run_id, [3.0], units="mV")
        groups = query.measurement_groups(self.store.conn, Selection(plan="p"))
        self.assertEqual([(g["point"], g["units"]) for g in groups], [({"vin": 8.0}, "V"), ({"vin": 16.0}, "V"), ({}, "mV")])

    def test_values_are_in_start_time_order(self):
        first = new_run(self.store)
        measure(self.store, first, [2.0])
        second = new_run(self.store)
        measure(self.store, second, [1.0])
        set_started(self.store, first, "2026-03-01T00:00:00.000+00:00")
        set_started(self.store, second, "2026-02-01T00:00:00.000+00:00")
        (group,) = query.measurement_groups(self.store.conn, Selection(plan="p"))
        self.assertEqual((group["values"], group["run_ids"]), ([1.0, 2.0], [second, first]))

    def test_filters(self):
        s = self.store
        with s.conn:
            s.conn.execute("INSERT INTO lots (name, created_at) VALUES ('L2026-11-01', ?)", (now_iso(),))
        lot = s.conn.execute("SELECT id FROM lots").fetchone()[0]
        runs = {
            "a": new_run(s, "SN-1001", product="PN-100-A", station="LINE-1", lot_id=lot),
            "b": new_run(s, "SN-1002", product="PN-100-B", station="LINE-2"),
            "c": new_run(s, "SN_100", product="PN-100-A", station="LINE-1"),
            "d": new_run(s, "XSN-1001", plan="q", product="PN-100-A"),
        }
        for key, run_id in runs.items():
            measure(s, run_id, [float(run_id)], step="s1" if key != "b" else "s2")
        set_started(s, runs["a"], "2026-01-05T10:00:00.000+00:00")
        set_started(s, runs["b"], "2026-01-06T10:00:00.000+00:00")
        set_started(s, runs["c"], "2026-01-07T10:00:00.000+00:00")
        set_started(s, runs["d"], "2026-01-08T10:00:00.000+00:00")

        def values(**kwargs):
            return sorted(v for g in query.measurement_groups(s.conn, Selection(**kwargs)) for v in g["values"])

        a, b, c, d = (float(runs[k]) for k in "abcd")
        self.assertEqual(values(plan="p"), [a, b, c])
        self.assertEqual(values(product="PN-100-A"), [a, c, d])
        self.assertEqual(values(plan="p", station="LINE-1"), [a, c])
        self.assertEqual(values(plan="p", lot_id=lot), [a])
        self.assertEqual(values(plan="p", serial_prefix="SN-"), [a, b])
        self.assertEqual(values(plan="p", serial_prefix="SN_"), [c])  # Not a wildcard.
        self.assertEqual(values(plan="p", serial_prefix="sn-"), [])  # Case counts.
        self.assertEqual(values(plan="p", serial="SN-1002"), [b])
        self.assertEqual(values(plan="p", since="2026-01-06T00:00:00.000+00:00"), [b, c])
        self.assertEqual(values(plan="p", until="2026-01-06T23:59:59.999+00:00"), [a, b])
        self.assertEqual(values(plan="p", step="s2"), [b])
        self.assertEqual(values(plan="p", measurement="nothing"), [])
        self.assertEqual(values(plan="p", limit_runs=2), [b, c])
        self.assertEqual(values(plan="p", limit_runs=0), [])

    def test_pareto(self):
        s = self.store
        for serial, values in (("A", [9.0, 9.0, 1.0]), ("B", [9.0, 1.0, 1.0]), ("C", [1.0, 1.0, 1.0])):
            run_id = new_run(s, serial, status="fail" if 9.0 in values else "pass")
            measure(s, run_id, values, high=5.0, name="vout")
            if serial == "A":
                measure(s, run_id, [9.0], high=5.0, name="iq", step="t")
        measure(s, new_run(s, "D", status="aborted"), [9.0], high=5.0, name="ignored")
        rows = query.failure_pareto(s.conn, Selection(plan="p"))
        self.assertEqual([(r["name"], r["runs"], r["readings"]) for r in rows], [("vout", 2, 3), ("iq", 1, 1)])
        self.assertAlmostEqual(rows[0]["share"], 2 / 3)
        self.assertAlmostEqual(rows[1]["cumulative"], 1.0)
        self.assertEqual(query.failure_pareto(s.conn, Selection(plan="p", step="t"))[0]["name"], "iq")

    def test_run_labels_in_chunks(self):
        with self.store.conn:
            self.store.conn.executemany(
                "INSERT INTO runs (uuid, serial, plan_name, plan_version, plan_hash, plan_source, variables, operator,"
                " software_version, status, started_at) VALUES (?, ?, 'p', '1', 'h', '{}', '{}', '', '1', 'pass', 't')",
                ((f"u{i}", f"SN-{i}") for i in range(1203)),
            )
        ids = [row[0] for row in self.store.conn.execute("SELECT id FROM runs ORDER BY id")]
        with query.chosen_runs(self.store.conn, Selection(plan="p")) as chosen:
            labels = query.run_labels(self.store.conn, chosen, ids + ids[:5])
        self.assertEqual(len(labels), 1203)
        self.assertEqual(labels[ids[-1]][0], "SN-1202")

    def test_queries_never_read_plan_sources(self):
        for i in range(5):
            measure(self.store, new_run(self.store, f"SN-{i}", status="fail"), [9.0, 1.0], high=5.0)
        read = set()

        def note_reads(action, table, column, *_):
            if action == sqlite3.SQLITE_READ:
                read.add((table, column))
            return sqlite3.SQLITE_OK

        self.store.conn.set_authorizer(note_reads)
        try:
            for view in ("summary", "yield", "pareto", "histogram", "control"):
                command.build_report(self.store.conn, Selection(plan="p"), view, everything=True)
        finally:
            self.store.conn.set_authorizer(None)
        self.assertIn(("measurements", "value"), read)
        self.assertNotIn(("runs", "plan_source"), read)
        self.assertNotIn(("runs", "variables"), read)


class ManyRuns(BenchTestCase):
    def test_twenty_thousand_runs_stay_quick(self):
        conn = self.store.conn
        runs = 20000
        with conn:
            conn.executemany(
                "INSERT INTO runs (id, uuid, serial, plan_name, plan_version, plan_hash, plan_source, variables, operator,"
                " software_version, status, started_at) VALUES (?, ?, ?, 'big', '1', 'h', ?, '{}', 'Operator A', '1', ?, ?)",
                (
                    (i, f"u{i}", f"SN-{i % 15000}", "x" * 1000, "fail" if i % 50 == 0 else "pass", f"2026-01-01T00:00:{i:08d}")
                    for i in range(1, runs + 1)
                ),
            )
            conn.executemany(
                "INSERT INTO steps (id, run_id, ordinal, name, kind, status) VALUES (?, ?, 1, 's', 'test', 'pass')",
                ((i, i) for i in range(1, runs + 1)),
            )
            conn.executemany(
                "INSERT INTO measurements (run_id, step_id, name, kind, point, samples, value, units, low, high, verdict,"
                " taken_at) VALUES (?, ?, 'm', 'read', '{}', 1, ?, 'V', 4.0, 6.0, ?, 't')",
                ((i, i, 7.0 if i % 50 == 0 else 5.0 + (i % 7) * 0.01, "fail" if i % 50 == 0 else "pass") for i in range(1, runs + 1)),
            )
        started = time.monotonic()
        report = command.build_report(conn, Selection(plan="big"), "summary", everything=True)
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertEqual(report["runs"]["complete"], runs)
        (summary,) = report["measurements"]
        self.assertEqual((summary["count"], summary["fails"]), (runs, runs // 50))
        (entry,) = report["yield"]
        self.assertEqual((entry["units_with_verdict"], entry["retests"]), (15000, 5000))
        self.assertLess(len(command.charts.render_html(report)), 3000000)


class HistoryAgreement(BenchTestCase):
    """``history --stats`` runs on the same engine, and its numbers became exact where they used to drift."""

    def test_identical_values_give_no_ppk_and_zero_stdev(self):
        for _ in range(3):
            measure(self.store, new_run(self.store), [3.3], low=3.2, high=3.4)
        (row,) = self.store.measurement_stats("p")
        self.assertEqual((row["mean"], row["stdev"], row["ppk"]), (3.3, 0.0, None))

    def test_values_that_are_not_finite_stay_out(self):
        measure(self.store, new_run(self.store), [1.0, float("inf"), 3.0], high=5.0)
        (row,) = self.store.measurement_stats("p")
        self.assertEqual((row["count"], row["mean"], row["maximum"]), (2, 2.0, 3.0))

    def test_history_capability_is_the_overall_sigma_index(self):
        for value in (1.0, 3.0, 2.0, 4.0):
            measure(self.store, new_run(self.store), [value], low=0.0, high=10.0)
        (row,) = self.store.measurement_stats("p")
        (summary,) = command.build_report(self.store.conn, Selection(plan="p"), "summary")["measurements"]
        for key in ("count", "fails", "mean", "stdev", "minimum", "maximum", "low", "high"):
            self.assertEqual(row[key], summary[key], key)
        self.assertEqual(row["ppk"], summary["capability"]["ppk"])
        summary_entry = self.store.plan_summary("p")
        self.assertEqual(summary_entry, query.yield_by_plan(self.store.conn, Selection(plan="p"))[0])

    def test_plan_summary_of_an_unknown_plan(self):
        summary = self.store.plan_summary("none")
        self.assertEqual((summary["runs"], summary["first_pass_yield"]), (0, None))


class Pages(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)

    def handle_data(self, data):
        self.text.append(data)


class Command(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = Path(tempfile.mkdtemp(prefix="benchrun-stats-"))
        cls.db = cls.folder / "r.sqlite"
        # Ten good units, one that fails and passes on a second run, and one aborted run.
        for seed in range(1, 11):
            call("run", QUICK, "--serial", f"SN-{1000 + seed}", "--sim", "--seed", seed, "--db", cls.db)
        call("run", QUICK, "--serial", "SN-1020", "--sim", "--dut", "high-output", "--db", cls.db)
        call("run", QUICK, "--serial", "SN-1020", "--sim", "--seed", 20, "--db", cls.db)
        call("run", QUICK, "--serial", "SN-1030", "--sim", "--fault", "dmm:drop:1", "--db", cls.db)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.folder, ignore_errors=True)

    def stats(self, *argv):
        return call("stats", "--db", self.db, *argv)

    def test_summary_text(self):
        code, out, _ = self.stats("--plan", "regulator-quick")
        self.assertEqual(code, EXIT_PASS)
        self.assertIn("13 runs selected, 12 complete runs used; left out: 1 aborted.", out)
        for word in ("cpk", "ppk", "median", "vin=12"):
            self.assertIn(word, out)

    def test_every_view_in_every_format(self):
        for view in ([], ["--yield"], ["--pareto"], ["--histogram", "vout"], ["--control", "vout"]):
            for output in command.FORMATS:
                with self.subTest(view=view, output=output):
                    code, out, err = self.stats("--plan", "regulator-quick", "--format", output, *view)
                    self.assertEqual(code, EXIT_PASS, err)
                    if output == "json":
                        self.assertEqual(json.loads(out)["runs"]["complete"], 12)
                    elif output == "csv":
                        rows = list(csv.reader(io.StringIO(out)))
                        self.assertGreater(len(rows), 1)
                        self.assertEqual(len({len(row) for row in rows}), 1)
                    elif output == "html":
                        page = Pages()
                        page.feed(out)
                        self.assertNotIn("script", page.tags)
                        self.assertNotIn("link", page.tags)
                        self.assertTrue(out.startswith("<!DOCTYPE html>"))

    def test_html_summary_has_a_histogram_and_a_control_chart_per_measurement(self):
        code, out, _ = self.stats("--plan", "regulator-quick", "--format", "html")
        self.assertEqual(code, EXIT_PASS)
        self.assertEqual(out.count("<svg"), 6)
        self.assertEqual(out.count('aria-label="Histogram of'), 3)
        for heading in ("Yield", "Failures", "Measurements"):
            self.assertIn(f"<h2>{heading}</h2>", out)

    def test_yield_and_pareto(self):
        code, out, _ = self.stats("--plan", "regulator-quick", "--yield", "--json")
        (entry,) = json.loads(out)["yield"]
        self.assertEqual((entry["units_with_verdict"], entry["first_pass_units"], entry["final_pass_units"], entry["retests"]), (11, 10, 11, 1))
        self.assertEqual(entry["counts"]["aborted"], 1)
        code, out, _ = self.stats("--plan", "regulator-quick", "--pareto")
        self.assertIn("1 failed run of 12 complete runs.", out)
        self.assertIn("output_voltage", out)

    def test_control_chart_names_the_outlier(self):
        code, out, _ = self.stats("--plan", "regulator-quick", "--control", "vout", "--step", "output_voltage", "--json")
        charts = json.loads(out)["charts"]
        self.assertEqual(len(charts), 3)
        for chart in charts:
            outliers = [p["serial"] for p in chart["points"] if 1 in p["rules"]]
            self.assertEqual(outliers, ["SN-1020"])
            self.assertEqual(chart["chart"]["rules"][0]["points"], [p["number"] for p in chart["points"] if 1 in p["rules"]])

    def test_histogram_bins_and_limits_in_text(self):
        code, out, _ = self.stats("--plan", "regulator-quick", "--histogram", "vout", "--bins", "4")
        self.assertEqual(code, EXIT_PASS)
        self.assertIn("4 bins (given)", out)
        self.assertIn("--- low limit 4.9 ---", out)
        self.assertIn("--- high limit 5.1 ---", out)

    def test_filters_reach_the_query(self):
        code, out, _ = self.stats("--plan", "regulator-quick", "--serial", "SN-102", "--json")
        self.assertEqual(json.loads(out)["runs"]["selected"], 2)
        code, out, _ = self.stats("--plan", "regulator-quick", "--limit-runs", "3", "--yield", "--json")
        self.assertEqual(json.loads(out)["runs"]["selected"], 3)
        code, out, _ = self.stats("--plan", "regulator-quick", "--since", "2000-01-01", "--until", "2999-12-31", "--json")
        self.assertEqual(json.loads(out)["runs"]["selected"], 13)

    def test_out_file_is_written_once_and_replaced_only_with_force(self):
        target = self.folder / "stats.html"
        code, out, _ = self.stats("--plan", "regulator-quick", "--format", "html", "--out", target)
        self.assertEqual(code, EXIT_PASS)
        self.assertIn("Wrote", out)
        first = target.read_text(encoding="utf-8")
        self.assertIn("<svg", first)
        code, _, err = self.stats("--plan", "regulator-quick", "--out", target)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("--force", err)
        self.assertEqual(target.read_text(encoding="utf-8"), first)
        code, _, _ = self.stats("--plan", "regulator-quick", "--out", target, "--force", "--json")
        self.assertEqual(code, EXIT_PASS)
        json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual([p.name for p in self.folder.iterdir() if p.name.endswith(".tmp")], [])

    def test_problems_are_plain_and_exit_2(self):
        for argv, expected in (
            ([], "--plan"),
            (["--plan", "regulator-quick", "--lot", "L-NONE"], "no lot named"),
            (["--plan", "nothing"], "No matching runs"),
            (["--plan", "regulator-quick", "--histogram", "nothing"], "No values of measurement 'nothing'"),
            (["--plan", "regulator-quick", "--bins", "5"], "--bins applies"),
            (["--plan", "regulator-quick", "--json", "--format", "csv"], "different formats"),
            (["--plan", "regulator-quick", "--histogram", "vout", "--measurement", "iq"], "different measurements"),
            (["--plan", "regulator-quick", "--force"], "--force only applies"),
            (["--plan", "regulator-quick", "--since", "yesterday"], "is not a time"),
        ):
            with self.subTest(argv=argv):
                code, out, err = self.stats(*argv)
                self.assertEqual(code, EXIT_USAGE)
                self.assertIn(expected, err)
        for argv in (["--bins", "0"], ["--bins", "1001"], ["--limit-runs", "-1"], ["--yield", "--pareto"], ["--format", "pdf"]):
            with self.subTest(argv=argv):
                code, _, _ = self.stats("--plan", "regulator-quick", *argv)
                self.assertEqual(code, EXIT_USAGE)
        code, _, err = call("stats", "--plan", "p", "--db", self.folder / "missing.sqlite")
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("no results database", err)

    def test_lot_by_name_and_by_id(self):
        folder = Path(tempfile.mkdtemp(prefix="benchrun-stats-lot-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        db = folder / "lot.sqlite"
        with Store(db) as store:
            with store.conn:
                store.conn.execute("INSERT INTO lots (name, created_at) VALUES ('L2026-11-01', ?)", (now_iso(),))
            measure(store, new_run(store, lot_id=1), [1.0])
            measure(store, new_run(store), [2.0])
        for lot in ("L2026-11-01", "1"):
            code, out, _ = call("stats", "--db", db, "--plan", "p", "--lot", lot, "--json")
            self.assertEqual(code, EXIT_PASS)
            self.assertEqual(json.loads(out)["measurements"][0]["mean"], 1.0)

    def test_history_and_stats_agree_on_the_simulators(self):
        _, out, _ = call("history", "--plan", "regulator-quick", "--stats", "--json", "--db", self.db)
        history = json.loads(out)["stats"]
        _, out, _ = self.stats("--plan", "regulator-quick", "--json")
        stats = json.loads(out)["measurements"]
        self.assertEqual(len(history), len(stats))
        for old, new in zip(history, stats):
            self.assertEqual((old["step"], old["name"], old["point"]), (new["step"], new["name"], new["point"]))
            self.assertEqual((old["count"], old["mean"], old["stdev"], old["ppk"]), (new["count"], new["mean"], new["stdev"], new["capability"]["ppk"]))


class Hostile(BenchTestCase):
    def test_names_are_escaped_in_html_and_neutralised_in_csv(self):
        evil = '<script>alert("x")</script>'
        run_id = new_run(self.store, serial="=cmd|' /C calc'!A0", plan=evil)
        step_id = self.store.add_step(run_id, 1, evil, "test")
        for value in (1.0, 2.0, 9.0):
            self.store.add_measurement(run_id, step_id, "+" + evil, "read", {"v": 1.0}, value, evil, None, 5.0, "pass" if value < 5 else "fail")
        selection = Selection(plan=evil)
        for view in ("summary", "control", "histogram", "pareto"):
            report = command.build_report(self.store.conn, selection, view, everything=view == "summary")
            page = command.charts.render_html(report)
            self.assertNotIn("<script", page)
            self.assertIn("&lt;script&gt;", page)
            for row in csv.reader(io.StringIO(command.render_csv(report))):
                for cell in row:
                    self.assertNotIn(cell[:1], ("=", "+", "@"), cell)
            parsed = json.loads(command.render_json(report))
            self.assertEqual(parsed["view"], view)

    def test_numbers_at_the_float_limit_draw_finite_charts(self):
        run_id = new_run(self.store)
        measure(self.store, run_id, [1.7e308, -1.7e308, 0.0, 1e-300], high=1.0)
        report = command.build_report(self.store.conn, Selection(plan="p"), "summary", everything=True)
        page = command.charts.render_html(report)
        self.assertNotIn("nan", page.lower().replace("nanosecond", ""))
        self.assertNotIn("inf\"", page)
        json.loads(command.render_json(report))
        command.render_text(report)


if __name__ == "__main__":
    unittest.main()
