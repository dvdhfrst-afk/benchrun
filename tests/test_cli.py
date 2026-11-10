"""The command line, including the two-process flow a test engineer follows."""

import contextlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from benchrun.cli import EXIT_ABORTED, EXIT_FAIL, EXIT_PASS, EXIT_USAGE, _say, main

ROOT = Path(__file__).parent.parent
PLAN = str(ROOT / "examples" / "regulator_line_load.json")
QUICK = str(ROOT / "examples" / "regulator_quick.json")


def call(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main([str(a) for a in argv])
        except SystemExit as exc:  # argparse
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class CliCase(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="benchrun-cli-"))
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.db = self.folder / "r.sqlite"

    def run_sim(self, *extra, plan=PLAN, serial="SN-1"):
        return call("run", plan, "--serial", serial, "--sim", "--db", self.db, *extra)


class RunCommand(CliCase):
    def test_pass_exit_code_and_output(self):
        code, out, _ = self.run_sim("--operator", "Operator A")
        self.assertEqual(code, EXIT_PASS, out)
        self.assertIn("RESULT: PASS", out)
        self.assertIn("SIM-DMM", out)

    def test_every_bundled_bad_unit_is_caught(self):
        for dut in ("high-output", "poor-line", "poor-load", "high-dropout", "high-iq", "poor-psrr", "dead"):
            code, out, _ = self.run_sim("--dut", dut, serial=dut)
            self.assertEqual(code, EXIT_FAIL, f"{dut}: {out}")

    def test_abort_exit_code(self):
        code, out, _ = self.run_sim("--fault", "dmm:drop:3")
        self.assertEqual(code, EXIT_ABORTED)
        self.assertIn("RESULT: ABORTED", out)

    def test_unreachable_instruments_abort_cleanly(self):
        code, out, _ = call("run", QUICK, "--serial", "X", "--db", self.db, "--address", "supply=127.0.0.1:1")
        self.assertEqual(code, EXIT_ABORTED)
        self.assertIn("127.0.0.1:1", out)

    def test_report_dir_writes_all_formats(self):
        reports = self.folder / "out"
        code, out, _ = self.run_sim("--report-dir", reports)
        self.assertEqual(code, EXIT_PASS)
        self.assertEqual(sorted(p.suffix for p in reports.iterdir()), [".csv", ".html", ".xml"])

    def test_reports_written_for_an_aborted_run(self):
        reports = self.folder / "out"
        code, _, _ = self.run_sim("--report-dir", reports, "--fault", "dmm:drop:0")
        self.assertEqual(code, EXIT_ABORTED)
        self.assertEqual(len(list(reports.iterdir())), 3)

    def test_scpi_log(self):
        log = self.folder / "traffic.scpi.log"
        self.run_sim("--scpi-log", log, plan=QUICK)
        text = log.read_text(encoding="utf-8")
        self.assertIn("*IDN?", text)
        self.assertIn("READ?", text)

    def test_set_changes_limits_and_is_recorded(self):
        code, out, _ = self.run_sim("--set", "vnom=3.3", plan=QUICK)
        self.assertEqual(code, EXIT_FAIL, out)
        conn = sqlite3.connect(str(self.db))
        self.addCleanup(conn.close)
        self.assertEqual(json.loads(conn.execute("SELECT variables FROM runs").fetchone()[0]), {"vnom": 3.3})

    def test_bad_plan_is_a_usage_error_with_no_run_recorded(self):
        bad = self.folder / "bad.json"
        bad.write_text('{"name": "x"}', encoding="utf-8")
        code, _, err = self.run_sim(plan=str(bad))
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("missing required key", err)
        self.assertFalse(self.db.exists())

    def test_bad_values_are_usage_errors(self):
        for extra, text in (
            (("--set", "vnom"), "name=value"),
            (("--set", "vnom=abc"), "not a number"),
            (("--set", "nope=1"), "no variable"),
            (("--fault", "dmm:melt"), "bad fault"),
        ):
            code, _, err = self.run_sim(*extra, plan=QUICK)
            self.assertEqual(code, EXIT_USAGE, extra)
            self.assertIn(text, err, extra)

    def test_blank_serial_is_refused(self):
        code, _, err = self.run_sim(plan=QUICK, serial="  ")
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("serial", err)

    def test_sim_options_without_sim_are_refused(self):
        code, _, err = call("run", QUICK, "--serial", "X", "--db", self.db, "--dut", "dead")
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("--sim", err)

    def test_missing_address(self):
        plan = self.folder / "noaddr.json"
        data = json.loads(Path(QUICK).read_text(encoding="utf-8"))
        del data["instruments"]["dmm"]["address"]
        plan.write_text(json.dumps(data), encoding="utf-8")
        code, _, err = call("run", plan, "--serial", "X", "--db", self.db)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("no address", err)

    def test_unknown_subcommand_is_a_usage_error(self):
        code, _, _ = call("explode")
        self.assertEqual(code, 2)


class NeverReadsAsAFailedUnit(CliCase):
    def test_plan_in_another_encoding(self):
        path = self.folder / "plan.json"
        path.write_bytes('{"name": "25 \u00b0C"}'.encode("cp1252"))
        for command in ("validate", "probe"):
            code, _, err = call(command, path)
            self.assertEqual(code, EXIT_USAGE)
            self.assertIn("not UTF-8", err)

    def test_port_out_of_range(self):
        for port in ("70000", "65535", "-1"):
            code, _, err = call("sim", "--base-port", port)
            self.assertEqual(code, EXIT_USAGE, port)
            self.assertIn("out of range", err)

    def test_internal_error_is_not_exit_1(self):
        with mock.patch("benchrun.cli.cmd_validate", side_effect=RuntimeError("boom")):
            code, _, err = call("validate", QUICK)
        self.assertEqual(code, EXIT_ABORTED)
        self.assertIn("boom", err)
        self.assertIn("says nothing about the unit", err)


class Console(unittest.TestCase):
    def test_unencodable_text_cannot_stop_a_run(self):
        raw = io.BytesIO()
        narrow = io.TextIOWrapper(raw, encoding="ascii", errors="strict")
        with contextlib.redirect_stdout(narrow):
            _say("idn \ufffd end")
        narrow.flush()
        self.assertIn(b"idn \\ufffd end", raw.getvalue())


class OtherCommands(CliCase):
    def test_validate(self):
        code, out, _ = call("validate", PLAN)
        self.assertEqual(code, EXIT_PASS)
        self.assertIn("5 instruments, 5 steps", out)

    def test_drivers_listing(self):
        code, out, _ = call("drivers")
        self.assertEqual(code, EXIT_PASS)
        self.assertIn("measure_dc_voltage(range_v, nplc) [V]", out)
        self.assertIn("set_voltage(volts)", out)

    def test_driver_commands_listing(self):
        code, out, _ = call("drivers", "psu", "--commands")
        self.assertEqual(code, EXIT_PASS)
        self.assertIn("set_voltage = VOLT", out)
        self.assertEqual(call("drivers", "nope")[0], EXIT_USAGE)

    def test_history_and_report_without_a_database(self):
        code, _, err = call("history", "--db", self.db)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("no results database", err)
        self.assertFalse(self.db.exists(), "reading must not create an empty database")
        code, _, err = call("report", "--latest", "--db", self.db)
        self.assertEqual(code, EXIT_USAGE)

    def test_history_by_serial_and_plan(self):
        call("run", QUICK, "--serial", "SN-1", "--sim", "--db", self.db)
        call("run", QUICK, "--serial", "SN-2", "--sim", "--db", self.db, "--dut", "high-output")
        call("run", QUICK, "--serial", "SN-3", "--sim", "--db", self.db, "--fault", "dmm:drop:0")
        _, out, _ = call("history", "--serial", "SN-2", "--db", self.db)
        self.assertIn("SN-2", out)
        self.assertNotIn("SN-1", out)
        self.assertIn("FAIL", out)
        _, out, _ = call("history", "--plan", "regulator-quick", "--db", self.db)
        self.assertIn("3 runs on 3 serials: 1 pass, 1 fail, 1 aborted", out)
        self.assertIn("First-pass yield: 50.0% (1 of 2 units)", out)
        _, out, _ = call("history", "--status", "aborted", "--db", self.db)
        self.assertIn("SN-3", out)
        self.assertNotIn("SN-1", out)
        _, out, _ = call("history", "--serial", "SN-9", "--db", self.db)
        self.assertIn("No matching runs", out)

    def test_history_stats_and_json(self):
        for seed in (1, 2, 3):
            call("run", QUICK, "--serial", f"S{seed}", "--sim", "--seed", seed, "--db", self.db)
        _, out, _ = call("history", "--plan", "regulator-quick", "--stats", "--db", self.db)
        self.assertIn("ppk", out)
        self.assertIn("vin=12", out)
        code, out, _ = call("history", "--plan", "regulator-quick", "--stats", "--json", "--db", self.db)
        self.assertEqual(code, EXIT_PASS)
        payload = json.loads(out)
        self.assertEqual(len(payload["runs"]), 3)
        self.assertEqual(payload["summary"]["counts"]["pass"], 3)
        self.assertEqual(payload["stats"][0]["count"], 3)

    def test_stats_needs_plan(self):
        call("run", QUICK, "--serial", "S", "--sim", "--db", self.db)
        code, _, err = call("history", "--stats", "--db", self.db)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("--plan", err)

    def test_report_selection(self):
        call("run", QUICK, "--serial", "A", "--sim", "--db", self.db)
        call("run", QUICK, "--serial", "A", "--sim", "--db", self.db)
        call("run", QUICK, "--serial", "B", "--sim", "--db", self.db)
        out_dir = self.folder / "rep"

        def names():
            return sorted(p.name for p in out_dir.iterdir()) if out_dir.exists() else []

        self.assertEqual(call("report", "--run", 1, "--format", "csv", "--out", out_dir, "--db", self.db)[0], EXIT_PASS)
        self.assertRegex(" ".join(names()), r"^run-000001-A-\d{8}T\d{6}\.csv$")
        shutil.rmtree(out_dir)
        call("report", "--latest", "--format", "html", "--out", out_dir, "--db", self.db)
        self.assertRegex(" ".join(names()), r"^run-000003-B-\d{8}T\d{6}\.html$")
        shutil.rmtree(out_dir)
        call("report", "--serial", "A", "--format", "junit", "--out", out_dir, "--db", self.db)
        self.assertRegex(" ".join(names()), r"^run-000002-A-\d{8}T\d{6}\.xml$")
        shutil.rmtree(out_dir)
        call("report", "--serial", "A", "--all", "--format", "csv", "--out", out_dir, "--db", self.db)
        self.assertEqual([name[:12] for name in names()], ["run-000001-A", "run-000002-A"])
        shutil.rmtree(out_dir)
        call("report", "--plan", "regulator-quick", "--out", out_dir, "--db", self.db)
        self.assertEqual(len(names()), 3)

    def test_report_errors(self):
        call("run", QUICK, "--serial", "A", "--sim", "--db", self.db)
        self.assertEqual(call("report", "--db", self.db)[0], EXIT_USAGE)
        self.assertEqual(call("report", "--run", 99, "--db", self.db, "--out", self.folder)[0], EXIT_USAGE)
        self.assertEqual(call("report", "--serial", "Z", "--db", self.db, "--out", self.folder)[0], EXIT_USAGE)


class TwoProcessFlow(CliCase):
    """What the README tells an engineer to do: simulators in one process, run in another."""

    def start_sim(self, *extra):
        station = self.folder / "addresses.json"
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        process = subprocess.Popen(
            [sys.executable, "-m", "benchrun", "sim", "--base-port", "0", "--write-addresses", str(station), *extra],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
        )

        def stop():
            process.kill()
            process.wait(timeout=10)
            process.stdout.close()

        self.addCleanup(stop)
        deadline = time.monotonic() + 30
        while not station.exists():
            self.assertIsNone(process.poll(), "simulators exited early")
            self.assertLess(time.monotonic(), deadline, "simulators did not start")
            time.sleep(0.02)
        return station, process

    def cli(self, *argv):
        return subprocess.run(
            [sys.executable, "-m", "benchrun", *[str(a) for a in argv]],
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            timeout=120,
        )

    def test_full_flow_with_separate_simulator_process(self):
        mapped, _ = self.start_sim()

        probe = self.cli("probe", PLAN, "--station", mapped)
        self.assertEqual(probe.returncode, EXIT_PASS, probe.stdout)
        self.assertIn("SIM-SCOPE", probe.stdout)

        run = self.cli("run", PLAN, "--serial", "SN-100", "--station", mapped, "--db", self.db, "--operator", "Operator A")
        self.assertEqual(run.returncode, EXIT_PASS, run.stdout)

        again = self.cli("run", PLAN, "--serial", "SN-101", "--station", mapped, "--db", self.db)
        self.assertEqual(again.returncode, EXIT_PASS, again.stdout)

        history = self.cli("history", "--plan", "regulator-line-load", "--db", self.db)
        self.assertIn("SN-100", history.stdout)
        self.assertIn("SN-101", history.stdout)

        reports = self.folder / "reports"
        report = self.cli("report", "--serial", "SN-100", "--out", reports, "--db", self.db)
        self.assertEqual(report.returncode, EXIT_PASS, report.stdout)
        self.assertEqual(len(list(reports.iterdir())), 3)

    def test_raw_scpi_command(self):
        station, _ = self.start_sim()
        dmm = json.loads(station.read_text(encoding="utf-8"))["addresses"]["dmm"]
        result = self.cli("scpi", dmm, "*IDN?", "*RST", "SYST:ERR?")
        self.assertEqual(result.returncode, EXIT_PASS, result.stdout)
        self.assertEqual(result.stdout.splitlines(), ["benchrun,SIM-DMM,SIM0001,1.0", '+0,"No error"'])

    def test_raw_scpi_failure_is_readable(self):
        result = self.cli("scpi", "127.0.0.1:1", "*IDN?", "--timeout", "0.5")
        self.assertEqual(result.returncode, EXIT_ABORTED)
        self.assertIn("cannot connect", result.stdout)

    def test_station_note_for_a_mistyped_name(self):
        mapped, _ = self.start_sim()
        data = json.loads(mapped.read_text(encoding="utf-8"))
        data["addresses"]["dmn"] = "127.0.0.1:1"
        mapped.write_text(json.dumps(data), encoding="utf-8")
        result = self.cli("probe", QUICK, "--station", mapped)
        self.assertEqual(result.returncode, EXIT_PASS, result.stdout)
        self.assertIn("not used by this plan: dmn", result.stdout)

    def test_probe_reports_a_dead_instrument(self):
        mapped, _ = self.start_sim()
        result = self.cli("probe", PLAN, "--station", mapped, "--address", "dmm=127.0.0.1:1")
        self.assertEqual(result.returncode, EXIT_ABORTED)
        self.assertIn("cannot connect", result.stdout)

    def test_sim_faults_and_units_from_the_command_line(self):
        mapped, _ = self.start_sim("--dut", "poor-load", "--seed", "4")
        result = self.cli("run", PLAN, "--serial", "SN", "--station", mapped, "--db", self.db)
        self.assertEqual(result.returncode, EXIT_FAIL, result.stdout)
        self.assertIn("load_reg", result.stdout)

    def test_port_in_use_is_explained(self):
        import socket

        with socket.socket() as holder:
            holder.bind(("127.0.0.1", 0))
            holder.listen(1)
            result = self.cli("sim", "--base-port", holder.getsockname()[1])
        self.assertEqual(result.returncode, EXIT_USAGE, result.stdout)
        self.assertIn("--base-port", result.stdout)


if __name__ == "__main__":
    unittest.main()
