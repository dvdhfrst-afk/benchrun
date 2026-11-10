"""Commands added for everyday use, examples, packaging facts and what the README says."""

import _thread
import contextlib
import csv
import io
import json
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import benchrun
from benchrun.cli import EXIT_ABORTED, EXIT_FAIL, EXIT_PASS, EXIT_USAGE, build_parser, main
from benchrun.drivers import DRIVERS
from benchrun.drivers.dmm import Dmm
from benchrun.drivers.psu import PowerSupply
from benchrun.errors import BadResponse
from benchrun.files import atomic_write_text
from benchrun.limits import Limit
from benchrun.reports.csv_report import COLUMNS
from benchrun.runner import Runner
from benchrun.sim.bench import PORT_ORDER
from benchrun.sim.instruments import Fault
from benchrun.store import Store
from benchrun.units import convert
from tests.support import BenchTestCase, FakeTransport, all_measurements, base_plan, clone

ROOT = Path(__file__).parent.parent
EXAMPLES = ROOT / "examples"
QUICK = EXAMPLES / "regulator_quick.json"


def call(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main([str(a) for a in argv])
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class Case(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="benchrun-more-"))
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.db = self.folder / "r.sqlite"

    def sim_run(self, *extra, serial="A", plan=QUICK):
        return call("run", plan, "--serial", serial, "--sim", "--db", self.db, *extra)


class Show(Case):
    def test_show_latest_and_by_id(self):
        self.sim_run(serial="A")
        self.sim_run(serial="B")
        code, out, _ = call("show", "--latest", "--db", self.db)
        self.assertEqual(code, EXIT_PASS)
        self.assertIn("B", out)
        code, out, _ = call("show", "--run", 1, "--db", self.db)
        self.assertEqual(code, EXIT_PASS)
        self.assertIn("A", out)

    def test_show_lists_failures_without_being_asked(self):
        self.sim_run("--dut", "high-output")
        code, out, _ = call("show", "--latest", "--db", self.db)
        self.assertEqual(code, EXIT_PASS)
        self.assertIn("FAIL", out.upper())
        self.assertIn("vout", out)

    def test_show_all_measurements_and_json(self):
        self.sim_run()
        _, short, _ = call("show", "--latest", "--db", self.db)
        _, full, _ = call("show", "--latest", "--all-measurements", "--db", self.db)
        self.assertGreater(len(full), len(short))
        _, text, _ = call("show", "--latest", "--json", "--db", self.db)
        self.assertEqual(json.loads(text)["status"], "pass")

    def test_show_problems(self):
        self.sim_run()
        self.assertEqual(call("show", "--db", self.db)[0], EXIT_USAGE)
        self.assertEqual(call("show", "--run", 99, "--db", self.db)[0], EXIT_USAGE)
        self.assertEqual(call("show", "--latest", "--db", self.folder / "missing.sqlite")[0], EXIT_USAGE)
        self.assertFalse((self.folder / "missing.sqlite").exists())


class Export(Case):
    def test_export_many_runs(self):
        self.sim_run(serial="A")
        self.sim_run("--dut", "high-output", serial="B")
        out_file = self.folder / "all.csv"
        code, _, err = call("export", "--out", out_file, "--db", self.db)
        self.assertEqual(code, EXIT_PASS, err)
        rows = list(csv.DictReader(io.StringIO(out_file.read_text(encoding="utf-8"))))
        self.assertEqual({row["serial"] for row in rows}, {"A", "B"})

    def test_export_filters(self):
        self.sim_run(serial="A")
        self.sim_run("--dut", "high-output", serial="B")
        out_file = self.folder / "fail.csv"
        call("export", "--out", out_file, "--status", "fail", "--db", self.db)
        rows = list(csv.DictReader(io.StringIO(out_file.read_text(encoding="utf-8"))))
        self.assertEqual({row["serial"] for row in rows}, {"B"})

    def test_export_will_not_replace_a_file_without_force(self):
        self.sim_run()
        out_file = self.folder / "keep.csv"
        out_file.write_text("mine", encoding="utf-8")
        self.assertEqual(call("export", "--out", out_file, "--db", self.db)[0], EXIT_USAGE)
        self.assertEqual(out_file.read_text(encoding="utf-8"), "mine")
        self.assertEqual(call("export", "--out", out_file, "--force", "--db", self.db)[0], EXIT_PASS)
        self.assertNotEqual(out_file.read_text(encoding="utf-8"), "mine")

    def test_export_with_nothing_to_write_says_so(self):
        self.sim_run()
        out_file = self.folder / "none.csv"
        code, _, err = call("export", "--out", out_file, "--serial", "NOPE", "--db", self.db)
        self.assertEqual(code, EXIT_USAGE)
        self.assertTrue(err)
        self.assertFalse(out_file.exists())


class Init(Case):
    def test_init_writes_a_plan_that_validates(self):
        plan = self.folder / "new.json"
        self.assertEqual(call("init", plan)[0], EXIT_PASS)
        self.assertEqual(call("validate", plan)[0], EXIT_PASS)

    def test_init_will_not_replace_a_file_without_force(self):
        plan = self.folder / "new.json"
        plan.write_text("mine", encoding="utf-8")
        self.assertEqual(call("init", plan)[0], EXIT_USAGE)
        self.assertEqual(plan.read_text(encoding="utf-8"), "mine")
        self.assertEqual(call("init", plan, "--force")[0], EXIT_PASS)
        self.assertEqual(call("validate", plan)[0], EXIT_PASS)


class HistoryFilters(Case):
    def test_filters(self):
        self.sim_run(serial="A")
        self.sim_run("--dut", "high-output", serial="B")
        _, out, _ = call("history", "--status", "fail", "--json", "--db", self.db)
        self.assertEqual([r["serial"] for r in json.loads(out)["runs"]], ["B"])
        _, out, _ = call("history", "--instrument", "SIM-DMM", "--json", "--db", self.db)
        self.assertEqual(len(json.loads(out)["runs"]), 2)
        _, out, _ = call("history", "--instrument", "NOT-THERE", "--json", "--db", self.db)
        self.assertEqual(json.loads(out)["runs"], [])
        _, out, _ = call("history", "--until", "2000-01-01", "--json", "--db", self.db)
        self.assertEqual(json.loads(out)["runs"], [])
        _, out, _ = call("history", "--since", "2000-01-01", "--json", "--db", self.db)
        self.assertEqual(len(json.loads(out)["runs"]), 2)

    def test_bad_time(self):
        self.sim_run()
        code, _, err = call("history", "--since", "yesterday", "--db", self.db)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("yesterday", err)


class RunOptions(Case):
    def test_note_is_kept_and_shown(self):
        self.sim_run("--note", "after rework")
        _, out, _ = call("history", "--db", self.db)
        self.assertIn("after rework", out)
        _, out, _ = call("show", "--latest", "--db", self.db)
        self.assertIn("after rework", out)

    def test_multi_line_note_refused(self):
        code, _, err = self.sim_run("--note", "two\nlines")
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("--note", err)

    def test_selected_steps_are_recorded_and_left_out_of_yield(self):
        data = clone(base_plan())
        data["instruments"]["supply"]["address"] = "127.0.0.1:1"
        data["instruments"]["dmm"]["address"] = "127.0.0.1:1"
        plan = self.folder / "p.json"
        plan.write_text(json.dumps(data), encoding="utf-8")
        code, out, err = call("run", plan, "--serial", "A", "--sim", "--db", self.db, "--step", "final_check")
        self.assertEqual(code, EXIT_PASS, out + err)
        with Store(self.db) as store:
            detail = store.run_detail(1)
        self.assertEqual(detail["selected_steps"], ["final_check"])
        self.assertEqual([s["name"] for s in detail["steps"] if s["kind"] == "test" and s["status"] != "skipped"], ["final_check"])
        code, out, _ = call("history", "--plan", "small", "--db", self.db)
        self.assertIn("of only some steps (not counted)", out)

    def test_unknown_step(self):
        code, _, err = self.sim_run("--step", "nope")
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("nope", err)
        self.assertFalse(self.db.exists() and Store(self.db).find_runs())

    def test_verbose_prints_each_measurement(self):
        _, quiet, _ = self.sim_run()
        _, loud, _ = self.sim_run("--verbose")
        self.assertGreater(loud.count("vout"), quiet.count("vout"))

    def test_scpi_log_keeps_what_was_there(self):
        log = self.folder / "traffic.log"
        log.write_text("earlier\n", encoding="utf-8")
        self.sim_run("--scpi-log", log)
        text = log.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("earlier\n"))
        self.assertIn("*IDN?", text)

    def prompt_plan(self):
        return EXAMPLES / "fixture_with_operator.json"

    def test_prompt_waits_for_the_operator(self):
        with mock.patch("benchrun.cli._read_line", return_value=True) as asked:
            code, out, _ = self.sim_run(plan=self.prompt_plan())
        self.assertEqual(code, EXIT_PASS, out)
        asked.assert_called_once()
        self.assertIn("Seat the unit", out)

    def test_prompt_with_nobody_there_aborts_and_says_how(self):
        with mock.patch("benchrun.cli._read_line", return_value=False):
            code, out, err = self.sim_run(plan=self.prompt_plan())
        self.assertEqual(code, EXIT_ABORTED)
        self.assertIn("--auto-confirm", out + err)

    def test_prompt_ctrl_c_aborts(self):
        def pressed() -> bool:
            _thread.interrupt_main()
            time.sleep(5)  # A read that never returns: the run must not wait for it.
            return True

        started = time.monotonic()
        with mock.patch("benchrun.cli._read_line", pressed):
            code, _, _ = self.sim_run(plan=self.prompt_plan())
        self.assertEqual(code, EXIT_ABORTED)
        self.assertLess(time.monotonic() - started, 4)

    def test_auto_confirm(self):
        with mock.patch("benchrun.cli._read_line", side_effect=AssertionError("must not ask")):
            code, out, _ = self.sim_run("--auto-confirm", plan=self.prompt_plan())
        self.assertEqual(code, EXIT_PASS, out)
        self.assertIn("continued automatically", out)


class SharedDatabase(Case):
    def test_several_processes_share_one_database_with_nothing_lost(self):
        args = [sys.executable, "-m", "benchrun", "run", str(QUICK), "--sim", "--db", str(self.db)]
        running = [
            subprocess.Popen(args + ["--serial", f"P-{index}"], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for index in range(6)
        ]
        for process in running:
            output, _ = process.communicate(timeout=120)
            self.assertEqual(process.returncode, EXIT_PASS, output)
        with Store(self.db) as store:
            runs = store.find_runs()
            self.assertEqual({run["serial"] for run in runs}, {f"P-{index}" for index in range(6)})
            for run in runs:
                self.assertEqual(run["status"], "pass")
                self.assertEqual(len(all_measurements(store.run_detail(run["id"]))), 3)

    def test_a_first_open_by_several_processes_at_once_makes_one_good_database(self):
        args = [sys.executable, "-m", "benchrun", "history", "--db", str(self.db)]
        code = "from benchrun.store import Store; Store(%r).close()" % str(self.db)
        running = [subprocess.Popen([sys.executable, "-c", code], cwd=ROOT, stderr=subprocess.PIPE, text=True) for _ in range(6)]
        for process in running:
            _, err = process.communicate(timeout=60)
            self.assertEqual(process.returncode, 0, err)
        self.assertEqual(subprocess.run(args, cwd=ROOT, capture_output=True, timeout=60).returncode, EXIT_PASS)


class Rounding(unittest.TestCase):
    def test_a_reading_on_a_limit_after_a_unit_change_passes(self):
        self.assertEqual(convert(0.0009, "A", "mA"), 0.9)
        self.assertEqual(convert(0.0031, "A", "mA"), 3.1)
        for milliamps in range(1, 3000):
            amps = float(f"{milliamps / 1000 / 1000:.6e}")
            self.assertEqual(Limit(milliamps / 1000, milliamps / 1000).evaluate(convert(amps, "A", "mA")), "pass", amps)

    def test_a_limit_worked_out_in_binary_still_means_what_it_says(self):
        self.assertEqual(Limit(low=0.1 + 0.2).evaluate(0.3), "pass")
        self.assertEqual(Limit(high=0.3).evaluate(0.1 + 0.2), "pass")

    def test_a_real_difference_still_fails(self):
        self.assertEqual(Limit(high=5.0).evaluate(5.00000000001), "fail")
        self.assertEqual(Limit(low=5.0).evaluate(4.99999999999), "fail")
        self.assertEqual(Limit(high=1e-12).evaluate(1.00000000001e-12), "fail")


class ReplyPattern(unittest.TestCase):
    def meter(self, *replies):
        return Dmm(FakeTransport(list(replies)), "x", None, r"([-+.0-9eE]+) V")

    def test_it_reads_what_it_describes(self):
        self.assertEqual(self.meter("5.000 V").measure_dc_voltage(), 5.0)

    def test_a_comma_or_a_space_or_another_unit_cannot_become_a_wrong_number(self):
        for reply in ("0,500 V", "12 345 V", "1000.0 mV", "5.000 V extra"):
            with self.subTest(reply=reply), self.assertRaises(BadResponse):
                self.meter(reply).measure_dc_voltage()

    def test_plain_numbers_and_status_queries_still_work(self):
        self.assertEqual(self.meter("5.0").measure_dc_voltage(), 5.0)
        meter = self.meter("1")
        meter.wait_complete()


class SafetyNotes(Case):
    def write_plan(self, driver, commands=None):
        data = {
            "name": "notes",
            "instruments": {"x": {"driver": driver, "address": "127.0.0.1:1", "commands": commands or {}}},
            "steps": [{"name": "one", "setup": [{"do": "x.write", "command": "OUTP ON"}], "measure": [{"name": "m", "calc": "1", "high": 2}]}],
        }
        path = self.folder / "notes.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_validate_says_what_the_switch_off_cannot_cover(self):
        for driver, word in (("scpi", "scpi driver"), ("switch", "open_all"), ("psu_multi", "all_off")):
            code, out, _ = call("validate", self.write_plan(driver))
            self.assertEqual(code, EXIT_PASS)
            self.assertIn(word, out)

    def test_no_note_when_it_is_covered(self):
        _, out, _ = call("validate", self.write_plan("switch", {"open_all": "ROUT:OPEN (@101:120)"}))
        self.assertNotIn("Note:", out)
        _, out, _ = call("validate", self.write_plan("psu"))
        self.assertNotIn("Note:", out)


class HelpAndVersion(Case):
    def subcommands(self):
        parser = build_parser()
        action = next(a for a in parser._actions if hasattr(a, "choices") and isinstance(a.choices, dict))
        return action.choices

    def test_help_on_every_command_and_every_option_is_described(self):
        commands = self.subcommands()
        self.assertGreaterEqual(len(commands), 11)
        for name, sub in commands.items():
            code, out, _ = call(name, "--help")
            self.assertEqual(code, 0, name)
            self.assertIn("usage:", out, name)
            for action in sub._actions:
                self.assertTrue(action.help, f"{name} {action.dest} has no help text")

    def test_version(self):
        code, out, _ = call("--version")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), f"benchrun {benchrun.__version__}")

    def test_version_agrees_everywhere(self):
        version = benchrun.__version__
        project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertRegex(project, r'version\s*=\s*\{\s*attr\s*=\s*"benchrun.__version__"\s*\}')
        self.assertIn(f"## {version} ", (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))

    def test_no_command_shows_usage(self):
        code, out, err = call()
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("usage", (out + err).lower())


class Examples(Case):
    def plans(self):
        return sorted(p for p in EXAMPLES.glob("*.json") if "station" not in p.name)

    def test_there_is_an_example_for_every_driver(self):
        used = set()
        for path in self.plans():
            for spec in json.loads(path.read_text(encoding="utf-8"))["instruments"].values():
                used.add(spec["driver"])
        self.assertEqual(set(DRIVERS) - used - {"scpi"}, set())

    def test_every_example_passes_on_the_simulators(self):
        for path in self.plans():
            with self.subTest(plan=path.name):
                code, out, err = call("run", path, "--serial", "EX", "--sim", "--auto-confirm", "--db", self.folder / f"{path.stem}.sqlite")
                self.assertEqual(code, EXIT_PASS, out + err)

    def test_every_example_fails_on_a_dead_unit_where_it_tests_the_unit(self):
        for name in ("regulator_quick", "regulator_line_load", "scan_through_switch"):
            with self.subTest(plan=name):
                code, out, err = call("run", EXAMPLES / f"{name}.json", "--serial", "EX", "--sim", "--dut", "dead", "--db", self.folder / f"{name}.sqlite")
                self.assertEqual(code, EXIT_FAIL, out + err)

    def test_addresses_in_examples_are_on_the_documented_ports(self):
        ports = {kind: 5025 + index for index, kind in enumerate(PORT_ORDER)}
        for path in self.plans():
            for spec in json.loads(path.read_text(encoding="utf-8"))["instruments"].values():
                if "address" in spec:
                    self.assertEqual(spec["address"], f"127.0.0.1:{ports[spec['driver']]}", path.name)

    def test_sample_csv_has_the_current_columns(self):
        sample = next((EXAMPLES / "sample_output").glob("*.csv"))
        with open(sample, encoding="utf-8", newline="") as handle:
            self.assertEqual(next(csv.reader(handle)), list(COLUMNS))


class Readme(unittest.TestCase):
    """Every command the README shows must at least be one the program accepts."""

    def commands(self, path):
        text = (ROOT / path).read_text(encoding="utf-8")
        for block in re.findall(r"```(?:bash|sh)\n(.*?)```", text, re.S):
            for line in block.splitlines():
                line = line.strip()
                if line.startswith("python -m benchrun") or line.startswith("benchrun "):
                    yield line

    def test_documented_commands_parse(self):
        parser = build_parser()
        seen = 0
        for path in ("README.md", "docs/troubleshooting.md", "docs/ci.md", "docs/plan_format.md", "docs/simulators.md", "docs/drivers.md", "docs/reports.md", "examples/README.md"):
            for line in self.commands(path):
                words = shlex.split(line)
                words = words[3:] if words[0] == "python" else words[1:]
                with self.subTest(line=line), contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                    try:
                        parser.parse_args(words)
                    except SystemExit as exc:
                        self.assertEqual(exc.code, 0, line)
                seen += 1
        self.assertGreater(seen, 15)

    def test_every_driver_is_documented_with_a_source(self):
        text = (ROOT / "docs" / "drivers.md").read_text(encoding="utf-8")
        for name in DRIVERS:
            self.assertIn(f"`{name}`", text)

    def test_troubleshooting_quotes_messages_the_program_really_prints(self):
        sources = "".join(p.read_text(encoding="utf-8") for p in (ROOT / "benchrun").rglob("*.py"))
        for quoted in ("Cannot continue:", "cannot connect to", "no response from", "non-numeric", "instrument reported error", "which does not match", "did not create", "newer than", "could not confirm these are off"):
            self.assertIn(quoted, sources)
            self.assertIn(quoted, (ROOT / "docs" / "troubleshooting.md").read_text(encoding="utf-8"))

    def test_readme_keeps_saying_it_has_only_met_simulators(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("has only ever been run against its own bundled simulators", " ".join(text.split()))


class AtomicWrites(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="benchrun-atomic-"))
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)

    def test_a_failed_write_leaves_the_old_file_and_no_litter(self):
        path = self.folder / "report.csv"
        path.write_text("old", encoding="utf-8")
        with mock.patch("os.replace", side_effect=PermissionError("in use")), self.assertRaises(OSError):
            atomic_write_text(path, "new")
        self.assertEqual(path.read_text(encoding="utf-8"), "old")
        self.assertEqual([p.name for p in self.folder.iterdir()], ["report.csv"])

    def test_text_that_cannot_be_encoded_leaves_the_old_file(self):
        path = self.folder / "report.csv"
        path.write_text("old", encoding="utf-8")
        with self.assertRaises(ValueError):
            atomic_write_text(path, "bad \ud800 surrogate")
        self.assertEqual(path.read_text(encoding="utf-8"), "old")
        self.assertEqual([p.name for p in self.folder.iterdir()], ["report.csv"])

    def test_keeps_line_endings_as_given(self):
        path = self.folder / "report.csv"
        atomic_write_text(path, "a\r\nb\n")
        self.assertEqual(path.read_bytes(), b"a\r\nb\n")


class FailuresDuringARun(BenchTestCase):
    def test_a_database_failure_mid_run_still_turns_outputs_off(self):
        bench = self.start_bench()
        plan = self.on_bench(base_plan(), bench)
        original = self.store.add_measurement
        calls = []

        def fail_on_third(*args, **kwargs):
            calls.append(1)
            if len(calls) == 3:
                raise sqlite3.OperationalError("disk I/O error")
            return original(*args, **kwargs)

        with mock.patch.object(self.store, "add_measurement", fail_on_third):
            result = Runner(plan, self.store, "SN-1", sleep=lambda _: None, console=lambda _: None).run()
        self.assertIn("disk I/O error", result.database_problem)
        self.assertEqual(result.status, "aborted")
        self.assertFalse(bench.model.supply_on)

    def test_ctrl_c_during_the_switch_off_does_not_stop_the_switch_off(self):
        bench = self.start_bench()
        plan = self.on_bench(base_plan(), bench)
        runner = Runner(plan, self.store, "SN-1", sleep=lambda _: None, console=lambda _: None)
        pressed = []
        real_safe = PowerSupply.make_safe

        def wrapped(instrument):
            if not pressed:
                pressed.append(1)
                raise KeyboardInterrupt()
            return real_safe(instrument)

        with mock.patch.object(PowerSupply, "make_safe", wrapped):
            result = runner.run()
        self.assertEqual(pressed, [1])
        self.assertFalse(bench.model.supply_on)
        self.assertEqual(result.unsafe, [])

    def test_outputs_off_after_every_kind_of_fault_on_every_step(self):
        for kind in ("drop", "hang", "error", "garbage"):
            for after in (0, 1, 3):
                with self.subTest(kind=kind, after=after):
                    data = clone(base_plan())
                    data["instruments"]["dmm"]["timeout_s"] = 0.1
                    result, detail, bench = self.run_plan(data, faults={"dmm": Fault(kind, after)})
                    self.assertIn(result.status, ("aborted", "fail"))
                    self.assertFalse(bench.model.supply_on)
                    self.assertEqual(self.store.run_detail(result.run_id)["status"], result.status)
                    self.assertTrue(detail["steps"])

    def test_limit_depending_on_a_failed_reading_fails_the_unit_and_keeps_going(self):
        data = clone(base_plan())
        data["steps"][1]["measure"] = [
            {"name": "a", "read": "dmm.measure_dc_voltage", "units": "V"},
            {"name": "b", "read": "dmm.measure_dc_voltage", "units": "V", "low": "=a - 1", "high": "=a + 1"},
        ]
        data["steps"][0]["measure"] = data["steps"][0]["measure"][:1]
        result, detail, _ = self.run_plan(data, faults={"dmm": Fault("overload", 3)})
        self.assertEqual(result.status, "fail")
        by_name = [m for m in all_measurements(detail) if m["name"] == "b"]
        self.assertEqual(by_name[-1]["verdict"], "fail")


if __name__ == "__main__":
    unittest.main()
