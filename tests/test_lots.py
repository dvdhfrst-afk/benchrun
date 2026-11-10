"""Lots: opening, testing units one after another, closing, and the lot summary."""

import _thread
import contextlib
import csv
import io
import json
import shutil
import signal
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from benchrun import lots, session
from benchrun.cli import EXIT_ABORTED, EXIT_FAIL, EXIT_PASS, EXIT_USAGE, main
from benchrun.runner import RunResult
from benchrun.store import Store

ROOT = Path(__file__).parent.parent
EXAMPLES = ROOT / "examples"
QUICK = EXAMPLES / "regulator_quick.json"
PROMPTED = EXAMPLES / "fixture_with_operator.json"
LOT = "L2026-11-01"


def call(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main([str(a) for a in argv])
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def typed(*lines):
    """Stand in for the terminal: each call gives the next line, then the end of input."""
    answers = iter([None if line is None else line.encode("utf-8") for line in lines])
    return mock.patch("benchrun.session._read_line", side_effect=lambda: next(answers, None))


class Case(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="benchrun-lots-"))
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.db = self.folder / "r.sqlite"

    def lot(self, *argv):
        return call("lot", *argv, "--db", self.db)

    def ok(self, *argv):
        code, out, err = self.lot(*argv)
        self.assertEqual(code, EXIT_PASS, out + err)
        return out

    def open_lot(self, *extra, name=LOT, plan=QUICK):
        source = extra if "--product" in extra else ("--plan", plan) + extra
        return self.ok("open", name, *source)

    def serials(self, text, name="serials.txt"):
        path = self.folder / name
        path.write_text(text, encoding="utf-8")
        return path

    def run_lot(self, *extra, plan=QUICK, name=LOT):
        return self.lot("run", name, "--plan", plan, "--sim", *extra)

    def sim_run(self, serial, *extra, plan=QUICK):
        return call("run", plan, "--serial", serial, "--sim", "--db", self.db, *extra)

    def runs(self):
        with Store(self.db) as store:
            return [dict(row) for row in store.conn.execute("SELECT id, serial, status, lot_id, product FROM runs ORDER BY id")]

    def audit(self, name=LOT):
        code, out, _ = call("audit", "--subject", f"lot:{name}", "--json", "--db", self.db)
        self.assertEqual(code, EXIT_PASS)
        return list(reversed(json.loads(out)["entries"]))

    def add_product(self, *extra):
        code, out, err = call("product", "add", "PN-100-A", "--plan", QUICK, "--db", self.db, *extra)
        self.assertEqual(code, EXIT_PASS, out + err)


class Opening(Case):
    def test_a_plan_lot_records_the_plans_name_and_an_audit_entry(self):
        out = self.open_lot("--quantity", "5", "--note", "Work order WO-1", "--actor", "Operator A")
        self.assertIn("Opened lot L2026-11-01 of 5 units, tested as plan regulator-quick", out)
        self.assertIn(f"lot run {LOT} --plan", out)
        with Store(self.db) as store:
            lot = lots.find(store.conn, LOT)
        self.assertEqual((lot["plan_name"], lot["product"], lot["quantity"], lot["status"]), ("regulator-quick", "", 5, "open"))
        entry = self.audit()[0]
        self.assertEqual((entry["action"], entry["actor"]), ("lot.create", "Operator A"))
        self.assertEqual(entry["detail"]["note"], "Work order WO-1")

    def test_a_product_lot_takes_the_products_plan(self):
        self.add_product()
        out = self.ok("open", LOT, "--product", "PN-100-A")
        self.assertIn("product PN-100-A (plan regulator-quick)", out)
        with Store(self.db) as store:
            self.assertEqual(lots.find(store.conn, LOT)["product"], "PN-100-A")

    def test_refusals(self):
        self.open_lot()
        for argv, expected in (
            (("open", LOT, "--plan", QUICK), "already a lot named"),
            (("open", " L2", "--plan", QUICK), "no space at either end"),
            (("open", "L" * 65, "--plan", QUICK), "1 to 64"),
            (("open", "L3", "--plan", self.folder / "missing.json"), "missing.json"),
            (("open", "L4", "--product", "PN-NONE"), "PN-NONE"),
        ):
            with self.subTest(argv=argv):
                code, _, err = self.lot(*argv)
                self.assertEqual(code, EXIT_USAGE)
                self.assertIn(expected, err)
        code, _, err = self.lot("show", "9" * 40)
        self.assertEqual(code, EXIT_USAGE, "an id too large for the database is just not found")
        self.assertIn("no lot named", err)
        for argv in (
            ("open", "L5"),
            ("open", "L5", "--plan", QUICK, "--product", "PN"),
            ("open", "L5", "--plan", QUICK, "--quantity", "0"),
            ("open", "L5", "--plan", QUICK, "--quantity", "9" * 30),
        ):
            with self.subTest(argv=argv):
                self.assertEqual(self.lot(*argv)[0], EXIT_USAGE)
        self.assertEqual(len(self.audit()), 1)

    def test_lot_names_ignore_case_as_station_names_do(self):
        self.open_lot()
        code, _, err = self.lot("open", LOT.lower(), "--plan", QUICK)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn(f"already a lot named {LOT}", err)
        self.assertIn(f"Lot {LOT}: OPEN", self.ok("show", LOT.lower()))
        self.assertEqual(self.sim_run("SN-1001", "--lot", LOT.lower())[0], EXIT_PASS)
        code, out, _ = call("history", "--lot", LOT.lower(), "--json", "--db", self.db)
        self.assertEqual([r["serial"] for r in json.loads(out)["runs"]], ["SN-1001"])

    def test_list_and_status_filter(self):
        code, out, err = self.lot("list")
        self.assertEqual(code, EXIT_USAGE, "no database yet")
        self.assertIn("no results database", err)
        self.open_lot("--quantity", "3")
        self.open_lot(name="L-OLD")
        self.ok("close", "L-OLD")
        out = self.ok("list")
        self.assertLess(out.index("L-OLD"), out.index(LOT), "newest first")
        self.assertNotIn("L-OLD", self.ok("list", "--status", "open"))
        listed = json.loads(self.ok("list", "--status", "open", "--json"))["lots"]
        self.assertEqual([lot["name"] for lot in listed], [LOT])
        self.assertEqual(listed[0]["units"]["to_test"], 3)


class SerialsFile(Case):
    def setUp(self):
        super().setUp()
        self.open_lot("--quantity", "4")

    def test_every_unit_is_an_ordinary_run_in_the_lot(self):
        path = self.serials("# lot L2026-11-01, tray 1\nSN-1001\n\n  SN-1002  \nSN-1003\n")
        code, out, err = self.run_lot("--serials", path, "--report-dir", self.folder / "reports", "--report-format", "json")
        self.assertEqual(code, EXIT_PASS, out + err)
        runs = self.runs()
        self.assertEqual([r["serial"] for r in runs], ["SN-1001", "SN-1002", "SN-1003"])
        self.assertEqual({r["lot_id"] for r in runs}, {1})
        self.assertEqual({r["status"] for r in runs}, {"pass"})
        self.assertEqual(len(list((self.folder / "reports").glob("*.json"))), 3)
        self.assertIn("Session: 3 units tested: 3 pass, 0 fail, 0 aborted.", out)
        self.assertIn("Lot L2026-11-01: 3 of 4 units tested, 1 to go", out)
        self.assertIn("First-pass yield 100.0%", out)

    def test_a_failed_unit_makes_exit_1_and_names_what_failed(self):
        code, out, _ = self.run_lot("--serials", self.serials("SN-1001\nSN-1002\n"), "--dut", "high-output")
        self.assertEqual(code, EXIT_FAIL)
        self.assertIn("output_voltage.vout", out)
        self.assertIn("0 pass, 2 fail", out)

    def test_an_aborted_unit_makes_exit_3(self):
        code, out, _ = self.run_lot("--serials", self.serials("SN-1001\nSN-1002\n"), "--fault", "dmm:drop:4")
        self.assertEqual(code, EXIT_ABORTED)
        self.assertEqual([r["status"] for r in self.runs()], ["pass", "aborted"])

    def test_bad_files_are_refused_before_any_unit_is_tested(self):
        cases = {
            "SN-1001\nSN-1002\nSN-1001\n": "line 3: serial SN-1001 is already on line 1",
            "SN-1001\nbad\x07serial\n": "line 2",
            "# nothing\n\n": "holds no serials",
            "SN-" + "9" * 200 + "\n": "1 to 128",
        }
        for text, expected in cases.items():
            with self.subTest(expected=expected):
                code, _, err = self.run_lot("--serials", self.serials(text))
                self.assertEqual(code, EXIT_USAGE)
                self.assertIn(expected, err)
        (self.folder / "latin1.txt").write_bytes(b"SN-\xe9\n")
        (self.folder / "huge.txt").write_bytes(b"SN-1\n" * 300000)
        for name, expected in (("latin1.txt", "not UTF-8"), ("huge.txt", "too big"), ("absent.txt", "cannot read")):
            with self.subTest(name=name):
                code, _, err = self.run_lot("--serials", self.folder / name)
                self.assertEqual(code, EXIT_USAGE)
                self.assertIn(expected, err)
        self.assertEqual(self.runs(), [])

    def test_a_byte_order_mark_and_windows_line_ends_are_fine(self):
        (self.folder / "bom.txt").write_bytes(b"\xef\xbb\xbfSN-1001\r\nSN-1002\r\n")
        code, out, err = self.run_lot("--serials", self.folder / "bom.txt")
        self.assertEqual(code, EXIT_PASS, out + err)
        self.assertEqual([r["serial"] for r in self.runs()], ["SN-1001", "SN-1002"])

    def test_a_serial_that_passed_in_the_lot_needs_retest(self):
        self.run_lot("--serials", self.serials("SN-1001\n"))
        code, _, err = self.run_lot("--serials", self.serials("SN-1002\nSN-1001\n"))
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("SN-1001 already passed in lot L2026-11-01; add --retest", err)
        self.assertEqual(len(self.runs()), 1)
        code, _, _ = self.run_lot("--serials", self.serials("SN-1002\nSN-1001\n"), "--retest")
        self.assertEqual(code, EXIT_PASS)
        self.assertEqual(len(self.runs()), 3)

    def test_a_failed_serial_may_be_tested_again_without_retest(self):
        self.run_lot("--serials", self.serials("SN-1001\n"), "--dut", "high-output")
        code, _, _ = self.run_lot("--serials", self.serials("SN-1001\n"))
        self.assertEqual(code, EXIT_PASS)
        out = self.ok("show", LOT)
        self.assertIn("First-pass yield 0.0% (0 of 1 unit), final yield 100.0% (1 of 1)", out)
        self.assertIn("Retests: 1 run on 1 unit", out)

    def test_nobody_to_answer_an_operator_prompt_ends_the_session(self):
        self.open_lot(name="L-PROMPT", plan=PROMPTED)
        with mock.patch("benchrun.session._read_line", return_value=None):
            code, out, err = self.run_lot("--serials", self.serials("SN-1\nSN-2\n"), plan=PROMPTED, name="L-PROMPT")
        self.assertEqual(code, EXIT_ABORTED)
        self.assertIn("ended early: no one answered the operator prompt", out)
        self.assertEqual(len(self.runs()), 1)

    def test_auto_confirm_passes_through(self):
        self.open_lot(name="L-PROMPT", plan=PROMPTED)
        with mock.patch("benchrun.session._read_line", side_effect=AssertionError("must not ask")):
            code, out, _ = self.run_lot("--serials", self.serials("SN-1\nSN-2\n"), "--auto-confirm", plan=PROMPTED, name="L-PROMPT")
        self.assertEqual(code, EXIT_PASS, out)
        self.assertEqual(out.count("continued automatically"), 2)


class TypedSerials(Case):
    def setUp(self):
        super().setUp()
        self.open_lot()

    def test_serials_are_asked_for_until_an_empty_line(self):
        with typed("SN-1001", "SN-1002", "", "SN-NEVER"):
            code, out, err = self.run_lot()
        self.assertEqual(code, EXIT_PASS, out + err)
        self.assertEqual([r["serial"] for r in self.runs()], ["SN-1001", "SN-1002"])
        self.assertEqual(out.count("Serial number of the next unit"), 3)

    def test_end_of_input_ends_the_session(self):
        for end in (None, "\x1a", "\x04"):
            with self.subTest(end=end), typed("SN-2001", end, "SN-NEVER"):
                code, out, _ = self.run_lot("--retest")
                self.assertEqual(code, EXIT_PASS, out)
        self.assertEqual([r["serial"] for r in self.runs()], ["SN-2001"] * 3)

    def test_a_bad_or_passed_serial_is_refused_and_asked_again(self):
        with typed("SN-1001", "SN-1001", "bad\tserial", "SN-1002"):
            code, out, err = self.run_lot()
        self.assertEqual(code, EXIT_PASS, out + err)
        self.assertIn("already passed in lot L2026-11-01; add --retest", err)
        self.assertIn("must be 1 to 128 printable characters", err)
        self.assertEqual([r["serial"] for r in self.runs()], ["SN-1001", "SN-1002"])

    def test_text_that_is_not_utf8_is_asked_again(self):
        answers = iter([b"SN-\xff", b"SN-1001", None])
        with mock.patch("benchrun.session._read_line", side_effect=lambda: next(answers, None)):
            code, out, err = self.run_lot()
        self.assertEqual(code, EXIT_PASS, out + err)
        self.assertIn("not valid UTF-8", err)
        self.assertEqual([r["serial"] for r in self.runs()], ["SN-1001"])

    def test_nothing_typed_tests_nothing(self):
        with typed(""):
            code, out, _ = self.run_lot()
        self.assertEqual(code, EXIT_PASS)
        self.assertIn("No units were tested in this session.", out)

    def test_ctrl_c_during_a_unit_stops_it_safely_and_ends_the_session(self):
        self.open_lot(name="L-PROMPT", plan=PROMPTED)
        reads = []

        def terminal():
            reads.append(1)
            if len(reads) == 1:
                return b"SN-1"
            _thread.interrupt_main()  # At the operator prompt of the first unit.
            time.sleep(5)  # A read that never returns: the run must not wait for it.
            return b""

        started = time.monotonic()
        with mock.patch("benchrun.session._read_line", terminal):
            code, out, _ = self.run_lot(plan=PROMPTED, name="L-PROMPT")
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(code, EXIT_ABORTED)
        self.assertEqual(len(reads), 2, "no further serial is asked for")
        self.assertIn("ended early: stopped by the operator", out)
        self.assertIn("interrupted by the operator", out)
        self.assertIn("Session: 1 unit tested: 0 pass, 0 fail, 1 aborted.", out)
        self.assertEqual([r["status"] for r in self.runs()], ["aborted"])

    def test_ctrl_c_at_the_serial_prompt_ends_the_session_with_its_summary(self):
        reads = []

        def terminal():
            reads.append(1)
            if len(reads) == 1:
                return b"SN-1001"
            _thread.interrupt_main()
            time.sleep(5)
            return b""

        with mock.patch("benchrun.session._read_line", terminal):
            code, out, _ = self.run_lot()
        self.assertEqual(code, EXIT_PASS, "the unit tested passed")
        self.assertIn("ended early: stopped by the operator", out)
        self.assertIn("Session: 1 unit tested: 1 pass", out)


class Sessions(Case):
    def test_a_plan_lot_needs_its_plan_file_and_the_same_plan(self):
        self.open_lot()
        code, _, err = self.lot("run", LOT, "--sim")
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("records the name of its plan (regulator-quick), not the file", err)
        code, _, err = self.run_lot("--serials", self.serials("SN-1\n"), plan=PROMPTED)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("tested with plan 'regulator-quick'", err)
        self.assertEqual(self.runs(), [])

    def test_a_product_lot_uses_the_products_plan_and_serial_pattern(self):
        self.add_product("--serial-pattern", r"SN-\d+")
        self.ok("open", LOT, "--product", "PN-100-A")
        code, _, err = self.lot("run", LOT, "--sim", "--serials", self.serials("SN-1\nX-2\n"))
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("does not match the pattern", err)
        code, out, err = self.lot("run", LOT, "--sim", "--serials", self.serials("SN-1\nSN-2\n"))
        self.assertEqual(code, EXIT_PASS, out + err)
        self.assertEqual({r["product"] for r in self.runs()}, {"PN-100-A"})

    def test_closed_and_missing_lots_are_refused(self):
        self.open_lot()
        self.ok("close", LOT)
        code, _, err = self.run_lot("--serials", self.serials("SN-1\n"))
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("is closed; reopen it first", err)
        code, _, err = self.run_lot("--serials", self.serials("SN-1\n"), name="L-NONE")
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("no lot named 'L-NONE'", err)

    def test_station_options_are_checked_as_for_run(self):
        self.open_lot()
        code, _, err = self.run_lot("--serials", self.serials("SN-1\n"), "--station-name", "LINE-1")
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("cannot be combined", err)
        code, _, err = self.lot("run", LOT, "--plan", QUICK, "--dut", "high-output", "--serials", self.serials("SN-1\n"))
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("only apply with --sim", err)

    def test_unsafe_outputs_end_the_session(self):
        self.open_lot()
        unsafe = session.UnitOutcome(RunResult(1, "pass", None, 0, 3, unsafe=["supply"]), EXIT_PASS)
        self.assertEqual(unsafe.bench_in_doubt, "outputs not confirmed off: supply")
        with mock.patch("benchrun.session.UnitOutcome.bench_in_doubt", new_callable=mock.PropertyMock, return_value="outputs not confirmed off: supply"):
            code, out, _ = self.run_lot("--serials", self.serials("SN-1\nSN-2\n"))
        self.assertIn("ended early: outputs not confirmed off: supply", out)
        self.assertEqual(len(self.runs()), 1)
        self.assertEqual(code, EXIT_PASS)

    def test_a_stop_held_off_while_saving_is_still_seen(self):
        with session.stop_requests_interrupt(lambda: True) as stops:
            signal.raise_signal(signal.SIGINT)
        self.assertTrue(stops.requested)
        with self.assertRaises(KeyboardInterrupt), session.stop_requests_interrupt(lambda: False) as stops:
            signal.raise_signal(signal.SIGINT)
        self.assertTrue(stops.requested)


class RunWithLot(Case):
    def test_a_single_run_joins_an_open_lot(self):
        self.open_lot()
        code, out, _ = self.sim_run("SN-1", "--lot", LOT)
        self.assertEqual(code, EXIT_PASS, out)
        self.assertEqual(self.runs()[0]["lot_id"], 1)
        code, _, err = self.sim_run("SN-1", "--lot", LOT)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("add --retest", err)
        self.assertEqual(self.sim_run("SN-1", "--lot", "1", "--retest")[0], EXIT_PASS, "by id as well as by name")
        self.assertEqual(len(self.runs()), 2)

    def test_refusals(self):
        self.open_lot()
        self.add_product()
        self.ok("open", "L-PRODUCT", "--product", "PN-100-A")
        for argv, expected in (
            (("--lot", "L-NONE"), "no lot named"),
            (("--retest",), "--retest goes with --lot"),
            (("--lot", LOT, "--product", "PN-100-A"), "is for no product"),
            (("--lot", "L-PRODUCT", "--product", "PN-100-A"), None),
        ):
            with self.subTest(argv=argv):
                code, out, err = self.sim_run("SN-1", *argv)
                if expected is None:
                    self.assertEqual(code, EXIT_PASS, out + err)
                else:
                    self.assertEqual(code, EXIT_USAGE)
                    self.assertIn(expected, err)
        code, _, err = self.sim_run("SN-2", "--lot", LOT, plan=PROMPTED)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("tested with plan 'regulator-quick'", err)
        code, _, err = call("run", QUICK, "--serial", "SN-3", "--sim", "--lot", LOT, "--db", self.folder / "other.sqlite")
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("no results database", err)

    def test_a_product_lot_gives_the_product_when_the_run_names_none(self):
        self.add_product()
        self.ok("open", LOT, "--product", "PN-100-A")
        code, out, err = call("run", "--serial", "SN-1", "--sim", "--lot", LOT, "--db", self.db)
        self.assertEqual(code, EXIT_PASS, out + err)
        self.assertEqual(self.runs()[0]["product"], "PN-100-A")

    def test_show_history_and_stats_name_the_lot(self):
        self.open_lot()
        self.sim_run("SN-1", "--lot", LOT)
        self.sim_run("SN-2")
        _, out, _ = call("show", "--run", 1, "--db", self.db)
        self.assertIn(f"Lot: {LOT}", out)
        _, out, _ = call("show", "--run", 2, "--db", self.db)
        self.assertNotIn("Lot:", out)
        _, out, _ = call("history", "--lot", LOT, "--json", "--db", self.db)
        self.assertEqual([r["serial"] for r in json.loads(out)["runs"]], ["SN-1"])
        code, _, err = call("history", "--lot", "L-NONE", "--db", self.db)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("no lot named", err)
        for name in (LOT, "1"):
            code, out, _ = call("stats", "--plan", "regulator-quick", "--lot", name, "--yield", "--json", "--db", self.db)
            self.assertEqual(code, EXIT_PASS)
            self.assertEqual(json.loads(out)["runs"]["selected"], 1)


class Closing(Case):
    def setUp(self):
        super().setUp()
        self.open_lot()
        with Store(self.db) as store:
            self.running = store.create_run("SN-9", "regulator-quick", "1.0", "x", "{}", {}, "", "test", lot_id=1)

    def test_close_refuses_while_a_run_is_running_unless_forced(self):
        code, _, err = self.lot("close", LOT)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn(f"runs still running: {self.running}", err)
        self.assertIn("--force", err)
        out = self.ok("close", LOT, "--force", "--actor", "Operator B")
        self.assertIn("still running", out)
        entry = self.audit()[-1]
        self.assertEqual((entry["action"], entry["actor"], entry["detail"]), ("lot.close", "Operator B", {"forced": True, "running_runs": [self.running]}))
        self.assertEqual(self.lot("close", LOT)[0], EXIT_USAGE, "already closed")

    def test_reopen(self):
        with Store(self.db) as store:
            store.finish_run(self.running, "aborted", "test")
        self.ok("close", LOT)
        self.assertEqual(self.lot("show", LOT, "--json")[0], EXIT_PASS)
        self.assertIsNotNone(json.loads(self.ok("show", LOT, "--json"))["lot"]["closed_at"])
        self.ok("reopen", LOT)
        lot = json.loads(self.ok("show", LOT, "--json"))["lot"]
        self.assertEqual((lot["status"], lot["closed_at"]), ("open", None))
        self.assertEqual(self.lot("reopen", LOT)[0], EXIT_USAGE)
        self.assertEqual([e["action"] for e in self.audit()], ["lot.create", "lot.close", "lot.reopen"])
        self.assertEqual(self.audit()[1]["detail"], {"forced": False})


class ShowAndReport(Case):
    def setUp(self):
        super().setUp()
        self.open_lot("--quantity", "6")
        self.sim_run("SN-1001", "--lot", LOT)
        self.sim_run("=1+1", "--lot", LOT)
        self.sim_run("SN-1003", "--lot", LOT, "--dut", "high-output")
        self.sim_run("SN-1003", "--lot", LOT, "--seed", "4")
        self.sim_run("SN-1004", "--lot", LOT, "--fault", "dmm:drop:1")

    def test_export_and_archive_export_choose_the_runs_of_a_lot(self):
        self.sim_run("SN-9001")  # Not in the lot.
        out_file = self.folder / "lot.jsonl"
        code, out, err = call("export", "--lot", LOT.lower(), "--format", "jsonl", "--out", out_file, "--db", self.db)
        self.assertEqual(code, EXIT_PASS, out + err)
        self.assertIn("Wrote 5 runs", out)
        serials = {json.loads(line)["serial"] for line in out_file.read_text(encoding="utf-8").splitlines()}
        self.assertEqual(serials, {"SN-1001", "=1+1", "SN-1003", "SN-1004"})
        code, out, err = call("archive", "export", "--lot", "1", "--status", "pass", "--out", self.folder / "lot.zip", "--db", self.db)
        self.assertEqual(code, EXIT_PASS, out + err)
        self.assertIn("Wrote 3 runs", out)
        code, _, err = call("export", "--lot", "L-NONE", "--out", self.folder / "none.csv", "--db", self.db)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("no lot named 'L-NONE'", err)
        self.assertFalse((self.folder / "none.csv").exists())

    def test_show(self):
        out = self.ok("show", LOT, "--units")
        self.assertIn("Units: 3 of 6 units tested, 3 to go; 1 more with no complete run yet", out)
        self.assertIn("First-pass yield 66.7% (2 of 3 units), final yield 100.0% (3 of 3)", out)
        self.assertIn("Runs: 5: 3 pass, 1 fail, 1 aborted, 0 running", out)
        self.assertIn("output_voltage  vout", out)
        self.assertIn("fail, pass", out)
        data = json.loads(self.ok("show", LOT, "--json"))
        self.assertEqual(data["units"]["to_test"], 3)
        self.assertEqual(data["pareto"]["rows"][0]["name"], "vout")
        self.assertEqual([u["serial"] for u in data["unit_results"]], ["=1+1", "SN-1001", "SN-1003", "SN-1004"])
        self.assertEqual(data["unit_results"][3]["final_result"], None)

    def test_report_writes_html_csv_and_json(self):
        out_dir = self.folder / "lot-report"
        out = self.ok("report", LOT, "--out", out_dir)
        stem = out_dir / "lot-000001-L2026-11-01"
        self.assertEqual(out.count("Report:"), 3)
        html = stem.with_suffix(".html").read_text(encoding="utf-8")
        self.assertIn("<svg", html)
        self.assertIn("First-pass yield", html)
        self.assertIn("1 failed run of 4 complete runs", html)
        self.assertNotIn("http", html.replace("http://www.w3.org", ""))
        self.assertNotIn("<script", html)
        self.assertIn("=1+1", html)
        rows = list(csv.DictReader(io.StringIO(stem.with_suffix(".csv").read_text(encoding="utf-8"))))
        self.assertEqual([r["serial"] for r in rows], ["'=1+1", "SN-1001", "SN-1003", "SN-1004"])
        self.assertEqual(rows[2]["results"], "fail pass")
        report = json.loads(stem.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual((report["format"], report["format_version"]), ("benchrun-lot", 1))
        self.assertEqual(report["units"]["first_pass_units"], 2)
        self.assertEqual(len([m for m in report["measurements"] if m["histogram"]]), 3, "one per sweep point of vout")

    def test_report_does_not_overwrite_without_force(self):
        out_dir = self.folder / "lot-report"
        self.ok("report", LOT, "--out", out_dir, "--format", "csv")
        self.assertEqual(sorted(p.suffix for p in out_dir.iterdir()), [".csv"])
        code, _, err = self.lot("report", LOT, "--out", out_dir)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("already exists; add --force", err)
        self.assertEqual(sorted(p.suffix for p in out_dir.iterdir()), [".csv"], "nothing written when one file is taken")
        self.ok("report", LOT, "--out", out_dir, "--force")
        self.assertEqual(len(list(out_dir.iterdir())), 3)

    def test_a_lot_name_cannot_steer_the_file_name(self):
        self.open_lot(name="../../x<y>")
        out_dir = self.folder / "lot-report"
        self.ok("report", "../../x<y>", "--out", out_dir, "--format", "json")
        self.assertEqual([p.name for p in out_dir.iterdir()], ["lot-000002-x_y.json"])

    def test_an_empty_lot_reports(self):
        self.open_lot(name="L-EMPTY")
        out = self.ok("show", "L-EMPTY")
        self.assertIn("no yield", out)
        self.ok("report", "L-EMPTY", "--out", self.folder / "empty")
        html = (self.folder / "empty" / "lot-000002-L-EMPTY.html").read_text(encoding="utf-8")
        self.assertIn("No limited measurement has a value", html)


if __name__ == "__main__":
    unittest.main()
