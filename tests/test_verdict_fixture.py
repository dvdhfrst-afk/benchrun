"""Proves verdicts against results worked out separately from the code.

The expected values in fixtures/verdict_expected.json were computed from the
equations in docs/simulators.md with plain arithmetic. This test runs the real
command line against the simulators, then reads the results with bare sqlite3.
It imports nothing from the runner, the limits code or the simulator model.
"""

import contextlib
import io
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from benchrun.cli import main

FIXTURES = Path(__file__).parent / "fixtures"
PLAN = FIXTURES / "verdict_plan.json"
EXPECTED = json.loads((FIXTURES / "verdict_expected.json").read_text(encoding="utf-8"))
EXIT_CODES = {"pass": 0, "fail": 1, "aborted": 3}


def run_case(db_path, case, noise="0", seed="1"):
    argv = ["run", str(PLAN), "--serial", "FIXTURE-1", "--sim", "--noise", noise, "--seed", seed, "--db", str(db_path)]
    argv += ["--dut", case["dut"]]
    for fault in case["faults"]:
        argv += ["--fault", fault]
    with contextlib.redirect_stdout(io.StringIO()):
        return main(argv)


class VerdictFixtureTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.db_path = self.folder / "results.sqlite"
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)

    def rows(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        return conn

    def check_case(self, case):
        code = run_case(self.db_path, case)
        self.assertEqual(code, EXIT_CODES[case["status"]])
        conn = self.rows()
        run = conn.execute("SELECT * FROM runs").fetchone()
        self.assertEqual(run["status"], case["status"])
        self.assertEqual(run["serial"], "FIXTURE-1")
        got = conn.execute(
            "SELECT s.name AS step, m.name, m.point, m.value, m.low, m.high, m.units, m.verdict"
            " FROM measurements m JOIN steps s ON s.id = m.step_id ORDER BY m.id"
        ).fetchall()
        want = case["measurements"]
        self.assertEqual(
            [(g["step"], g["name"]) for g in got],
            [(w["step"], w["name"]) for w in want],
            "every expected measurement, in order, and nothing else",
        )
        for g, w in zip(got, want):
            where = f"{w['step']}.{w['name']} at {w['point']} ({w['worked']})"
            self.assertEqual(json.loads(g["point"]), w["point"], where)
            self.assertEqual(g["verdict"], w["verdict"], where)
            self.assertEqual(g["units"], w["units"], where)
            self.assertEqual(g["low"], w["low"], where)
            self.assertEqual(g["high"], w["high"], where)
            if w["value"] is None:
                self.assertIsNone(g["value"], where)
            else:
                self.assertAlmostEqual(g["value"], w["value"], delta=w["tolerance"], msg=where)
        return conn, run

    def test_units_with_a_verdict(self):
        for case in EXPECTED["cases"]:
            if case["status"] == "aborted":
                continue
            with self.subTest(case["label"]):
                self.db_path.unlink() if self.db_path.exists() else None
                conn, run = self.check_case(case)
                self.assertIsNone(run["reason"])
                conn.close()

    def test_failing_cases_really_exist(self):
        """Checks the fixture itself: it must hold both a pass and specific fails."""
        by_dut = {c["dut"]: c for c in EXPECTED["cases"] if not c["faults"]}
        self.assertEqual(by_dut["good"]["status"], "pass")
        failed = [m["name"] for m in by_dut["poor-line"]["measurements"] if m["verdict"] == "fail"]
        self.assertEqual(failed, ["line_reg"])
        failed = [(m["step"], m["name"]) for m in by_dut["high-dropout"]["measurements"] if m["verdict"] == "fail"]
        self.assertIn(("headroom", "vout"), failed)
        self.assertNotIn(("load", "vout"), failed)

    def test_aborted_runs(self):
        for case in EXPECTED["cases"]:
            if case["status"] != "aborted":
                continue
            with self.subTest(case["label"]):
                self.db_path.unlink() if self.db_path.exists() else None
                conn, run = self.check_case(case)
                self.assertIn(case["reason_contains"], run["reason"])
                steps = {row["name"]: row for row in conn.execute("SELECT * FROM steps")}
                self.assertEqual(steps[case["failed_step"]]["status"], "error")
                self.assertIn(case["reason_contains"], steps[case["failed_step"]]["message"])
                for name in case["skipped_steps"]:
                    self.assertEqual(steps[name]["status"], "skipped", name)
                conn.close()

    def test_noise_stays_near_the_worked_value(self):
        """With noise on, readings sit within 5 sigma of the noiseless answer."""
        good = next(c for c in EXPECTED["cases"] if c["dut"] == "good" and not c["faults"])
        self.assertEqual(run_case(self.db_path, good, noise="1", seed="7"), 0)
        conn = self.rows()
        got = conn.execute(
            "SELECT s.name AS step, m.value FROM measurements m JOIN steps s ON s.id = m.step_id"
            " WHERE m.name = 'vout' ORDER BY m.id"
        ).fetchall()
        want = [m for m in good["measurements"] if m["name"] == "vout"]
        self.assertEqual(len(got), len(want))
        for g, w in zip(got, want):
            difference = abs(g["value"] - w["value"])
            self.assertLess(difference, 5 * 30e-6, f"{w['step']}: multimeter noise is 30 uV rms")
            self.assertGreater(difference, 0.0, "noise must actually be present")
        conn.close()


if __name__ == "__main__":
    unittest.main()
