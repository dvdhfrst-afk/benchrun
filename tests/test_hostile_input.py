"""Files and values that are empty, huge, malformed or written to do harm.

Every case must end in a plain message and the right exit code. None may print a
stack trace, run for long, write outside the chosen folder, or pass something it
should not.
"""

import contextlib
import csv
import io
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from benchrun.cli import EXIT_FAIL, EXIT_PASS, EXIT_USAGE, _say, main
from benchrun.errors import PlanError
from benchrun.plan import MAX_PLAN_BYTES, MAX_SWEEP_POINTS, parse_plan
from benchrun.runner import Runner
from benchrun.reports import csv_report, html_report, junit_report, report_stem
from tests.support import BenchTestCase, base_plan, clone

ROOT = Path(__file__).parent.parent
QUICK = ROOT / "examples" / "regulator_quick.json"


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
        self.folder = Path(tempfile.mkdtemp(prefix="benchrun-hostile-"))
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)

    def plan_file(self, content):
        path = self.folder / "plan.json"
        path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        return path

    def assertRefused(self, content, *words):
        path = self.plan_file(content)
        started = time.monotonic()
        code, out, err = call("validate", path)
        self.assertLess(time.monotonic() - started, 10.0)
        self.assertEqual(code, EXIT_USAGE, out + err)
        self.assertNotIn("Traceback", out + err)
        self.assertTrue(err.strip(), "a refusal must say why")
        for word in words:
            self.assertIn(word, err)


class BadFiles(Case):
    def test_things_that_are_not_a_plan(self):
        for content in ("", "   \n\t ", "[]", "null", "42", '"plan"', "true", "{", "{]", "{'name': 1}", "<plan/>"):
            with self.subTest(content=content):
                self.assertRefused(content)

    def test_byte_order_mark_alone(self):
        self.assertRefused(b"\xef\xbb\xbf")

    def test_byte_order_mark_before_a_good_plan_is_fine(self):
        path = self.plan_file(b"\xef\xbb\xbf" + json.dumps(base_plan()).encode("utf-8"))
        self.assertEqual(call("validate", path)[0], EXIT_PASS)

    def test_wrong_encodings(self):
        text = json.dumps(base_plan())
        for content in (text.encode("utf-32"), '{"name": "caf\xe9"}'.encode("latin-1"), b"\xff\xfe\x00"):
            self.assertRefused(content)

    def test_utf16_with_a_mark_is_read_because_windows_tools_write_it(self):
        path = self.plan_file(json.dumps(base_plan()).encode("utf-16"))
        self.assertEqual(call("validate", path)[0], EXIT_PASS)

    def test_binary_junk(self):
        self.assertRefused(bytes(range(256)) * 16)

    def test_folder_given_as_a_plan(self):
        code, out, err = call("validate", self.folder)
        self.assertEqual(code, EXIT_USAGE)
        self.assertNotIn("Traceback", out + err)

    def test_missing_file(self):
        code, _, err = call("validate", self.folder / "nope.json")
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("nope.json", err)

    def test_deep_nesting(self):
        self.assertRefused("[" * 200000 + "]" * 200000)
        self.assertRefused('{"a":' * 100000 + "1" + "}" * 100000)

    def test_a_file_past_the_size_limit(self):
        path = self.folder / "huge.json"
        with open(path, "wb") as out:
            out.seek(MAX_PLAN_BYTES + 10)
            out.write(b" ")
        code, _, err = call("validate", path)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("too big", err)

    def test_non_numbers_in_json(self):
        for word in ("NaN", "Infinity", "-Infinity"):
            text = json.dumps(base_plan()).replace('"vnom": 5.0', f'"vnom": {word}')
            self.assertRefused(text, word)

    def test_number_too_large_to_hold(self):
        self.assertRefused(json.dumps(base_plan()).replace('"vnom": 5.0', '"vnom": 1e999'))
        self.assertRefused(json.dumps(base_plan()).replace('"vnom": 5.0', '"vnom": 1' + "0" * 5000))

    def test_half_a_character_in_a_plan(self):
        self.assertRefused(json.dumps(base_plan()).replace('"small"', '"sm\\ud800all"'), "broken character")

    def test_duplicate_keys_never_drop_a_limit(self):
        text = '{"name": "a", "name": "b", "version": "1", "instruments": {}, "steps": []}'
        self.assertRefused(text, "name")


class NonAscii(Case):
    def test_text_a_terminal_could_not_decode_is_refused_plainly(self):
        db = self.folder / "r.sqlite"
        for command in (
            ("run", QUICK, "--serial", "S\udcff1", "--sim", "--db", db),
            ("run", QUICK, "--serial", "A", "--note", "\udcc3", "--sim", "--db", db),
            ("history", "--serial", "S\udcff1", "--db", db),
            ("show", "--serial", "S\udcff1", "--db", db),
        ):
            with self.subTest(command=command[0]):
                code, out, err = call(*command)
                self.assertEqual(code, EXIT_USAGE, out + err)
                self.assertIn("not valid text", err)
                self.assertNotIn("Traceback", out + err)

    def test_host_name_that_cannot_be_one_is_a_connection_error(self):
        for host in ("a" * 70 + ".example", "x..y"):
            code, out, err = call("scpi", f"{host}:5025", "*IDN?", "--timeout", "1")
            self.assertEqual(code, 3, out + err)
            self.assertNotIn("Traceback", out + err)

    def test_plan_serial_note_and_operator_survive(self):
        data = json.loads(QUICK.read_text(encoding="utf-8"))
        data["name"] = "r\u00e9gulateur \u7a33\u538b\u5668"
        db = self.folder / "r.sqlite"
        reports = self.folder / "rep"
        code, out, err = call(
            "run", self.plan_file(json.dumps(data, ensure_ascii=False)), "--serial", "\u00c9-\u7a33-1", "--sim", "--db", db,
            "--note", "ca\u00f1\u00f3n \U0001f600", "--operator", "\u0416", "--report-dir", reports,
        )  # fmt: skip
        self.assertEqual(code, EXIT_PASS, out + err)
        self.assertEqual(len(list(reports.iterdir())), 3)
        code, out, _ = call("show", "--latest", "--json", "--db", db)
        detail = json.loads(out)
        self.assertEqual(detail["serial"], "\u00c9-\u7a33-1")
        self.assertEqual(detail["note"], "ca\u00f1\u00f3n \U0001f600")
        self.assertEqual(detail["plan_name"], "r\u00e9gulateur \u7a33\u538b\u5668")

    def test_printing_to_a_terminal_that_cannot_show_it_does_not_stop_a_run(self):
        raw = io.BytesIO()
        narrow = io.TextIOWrapper(raw, encoding="ascii", errors="strict")
        with contextlib.redirect_stdout(narrow):
            _say("caf\u00e9 \u7a33")
        narrow.flush()
        self.assertIn(b"caf", raw.getvalue())


class BigPlans(Case):
    def spoil(self, change):
        data = clone(base_plan())
        change(data)
        with self.assertRaises(PlanError) as caught:
            parse_plan(data)
        return "\n".join(caught.exception.problems)

    def test_sweep_bomb_is_refused_without_building_it(self):
        started = time.monotonic()

        def bomb(d):
            d["steps"][0]["sweep"] = {"var": "vin", "start": 0, "stop": 1e12, "step": 1e-6}

        self.assertIn("sweep", self.spoil(bomb))
        self.assertLess(time.monotonic() - started, 2.0)

    def test_nested_sweeps_count_as_their_product(self):
        def bomb(d):
            many = list(range(1000))
            d["steps"][0]["sweep"] = [{"var": "vin", "values": many}, {"var": "b", "values": many}]

        self.assertIn(str(MAX_SWEEP_POINTS), self.spoil(bomb))

    def test_samples_cap(self):
        def bomb(d):
            d["steps"][1]["measure"][0]["samples"] = 10 ** 12

        self.assertIn("samples", self.spoil(bomb))

    def test_tiny_sweep_steps_stay_distinct(self):
        data = clone(base_plan())
        data["steps"][0]["sweep"] = {"var": "vin", "start": 0, "stop": 5e-13, "step": 1e-13}
        data["steps"][0]["summary"] = []
        step = parse_plan(data).steps[0]
        self.assertEqual(len(set(step.sweeps[0].values)), 6)


class Expressions(Case):
    def plan_with(self, expression):
        data = clone(base_plan())
        data["steps"][0]["measure"][0]["high"] = expression
        return data

    def test_code_and_attribute_access_are_refused(self):
        for expression in (
            "=__import__('os').system('echo hi')",
            "=vnom.__class__",
            "=().__class__.__bases__",
            "=open('x')",
            "=[1,2][0]",
            "=lambda: 1",
            "=exec('1')",
            "=vnom if 1 else 2",
        ):
            with self.subTest(expression=expression):
                with self.assertRaises(PlanError):
                    parse_plan(self.plan_with(expression))

    def test_huge_powers_end_quickly(self):
        started = time.monotonic()
        for expression in ("=9**9**9", "=9**9**9**9", "=10**400"):
            with self.subTest(expression=expression):
                with self.assertRaises(PlanError):
                    parse_plan(self.plan_with(expression))
        self.assertLess(time.monotonic() - started, 2.0)

    def test_long_expression_refused(self):
        with self.assertRaises(PlanError):
            parse_plan(self.plan_with("=" + "+".join(["1"] * 5000)))

    def test_deep_expression_refused(self):
        with self.assertRaises(PlanError):
            parse_plan(self.plan_with("=" + "(" * 500 + "1" + ")" * 500))


class CommandLineValues(Case):
    def test_override_that_is_not_a_finite_number(self):
        for value in ("nan", "inf", "-inf", "1e999", "ten", ""):
            with self.subTest(value=value):
                code, out, err = call("run", QUICK, "--serial", "A", "--sim", "--db", self.folder / "r.sqlite", "--set", f"vnom={value}")
                self.assertEqual(code, EXIT_USAGE, out + err)
                self.assertNotIn("Traceback", out + err)

    def test_unit_cannot_pass_because_of_a_nan_limit(self):
        code, out, _ = call("validate", QUICK, "--set", "vnom=nan")
        self.assertEqual(code, EXIT_USAGE, out)

    def test_serial_with_path_traversal_stays_in_the_report_folder(self):
        reports = self.folder / "deep" / "rep"
        for serial in ("../../escaped", "..\\..\\escaped", "/etc/passwd", "C:\\Windows\\x", "a/../../b", "..", "CON", "\u0000x"):
            with self.subTest(serial=serial):
                call("run", QUICK, "--serial", serial, "--sim", "--db", self.folder / "r.sqlite", "--report-dir", reports)
        self.assertFalse((self.folder / "escaped.html").exists())
        found = [p for p in self.folder.rglob("*") if p.is_file() and p.suffix in (".html", ".csv", ".xml")]
        self.assertTrue(found)
        for path in found:
            self.assertEqual(path.parent, reports)

    def test_report_name_is_a_single_plain_file_name(self):
        for serial in ("../x", "a\\b", "a:b", "a*b", "a?b", 'a"b', "a<b>", "a|b", "   ", "", "x" * 500, "\n\r\t"):
            stem = report_stem({"id": 1, "serial": serial, "started_at": "2026-11-07T10:00:00+00:00"})
            self.assertNotRegex(stem, r'[\\/:*?"<>|\s]')
            self.assertLess(len(stem), 120)

    def test_terminal_escapes_in_a_plan_do_not_reach_the_screen(self):
        data = json.loads(QUICK.read_text(encoding="utf-8"))
        data["name"] = "evil\x1b]0;owned\x07\x1b[2J"
        path = self.plan_file(json.dumps(data))
        for command in (("validate", path), ("run", path, "--serial", "A\x1b[31m", "--sim", "--db", self.folder / "r.sqlite")):
            _, out, err = call(*command)
            self.assertNotIn("\x1b", out + err)

    def test_terminal_escapes_in_history_do_not_reach_the_screen(self):
        db = self.folder / "r.sqlite"
        call("run", QUICK, "--serial", "A\x1b[2J", "--sim", "--db", db, "--note", "n\x1b[2J")
        for command in (("history", "--db", db), ("show", "--latest", "--db", db)):
            _, out, err = call(*command)
            self.assertNotIn("\x1b", out + err)


class SafeReports(BenchTestCase):
    def nasty_run(self):
        data = clone(base_plan())
        data["name"] = "<script>alert(1)</script>"
        plan = self.on_bench(data, self.start_bench())
        result = Runner(plan, self.store, "=cmd|'/c calc'!A1", operator="<img src=x onerror=alert(1)>", note="+1<b>", sleep=lambda _: None).run()
        return self.store.run_detail(result.run_id)

    def test_html_is_escaped(self):
        text = html_report.render_html(self.nasty_run())
        self.assertNotIn("<script>alert", text)
        self.assertNotIn("<img src=x", text)
        self.assertIn("&lt;script&gt;", text)

    def test_csv_cannot_start_a_formula(self):
        rows = list(csv.reader(io.StringIO(csv_report.render_csv(self.nasty_run()))))
        for row in rows[1:]:
            for cell in row:
                if cell[:1] in ("=", "@"):
                    self.fail(f"cell starts a formula: {cell!r}")
                if cell[:1] in ("+", "-"):
                    float(cell)  # Only real numbers may start with a sign.

    def test_junit_is_well_formed_and_refuses_control_characters(self):
        import xml.etree.ElementTree as ElementTree

        detail = self.nasty_run()
        detail["note"] = "bad\x01char"
        detail["serial"] = "x\x0by"
        ElementTree.fromstring(junit_report.render_junit(detail))


class WhatRunnersMustNotGetWrong(BenchTestCase):
    def test_newline_in_a_raw_command_is_refused_before_anything_is_sent(self):
        data = clone(base_plan())
        data["setup"].insert(0, {"do": "supply.write", "command": "VOLT 1\nOUTP ON"})
        with self.assertRaises(PlanError):
            parse_plan(data)

    def test_newline_arriving_through_a_template_is_refused(self):
        data = clone(base_plan())
        data["instruments"]["supply"]["commands"] = {"output": "OUTP {state}\n*RST"}
        with self.assertRaises(PlanError):
            parse_plan(data)

    def test_template_cannot_reach_python_objects(self):
        for template in ("{volts.__class__}", "{volts.__class__.__mro__}", "{0}", "{}", "{volts!r:>9999999999}"):
            data = clone(base_plan())
            data["instruments"]["supply"]["commands"] = {"set_voltage": template}
            with self.subTest(template=template), self.assertRaises(PlanError):
                parse_plan(data)

    def test_station_file_values_must_be_text(self):
        for content in ('{"addresses": {"dmm": 5025}}', '{"addresses": ["dmm"]}', "[]", ""):
            path = self.folder / "station.json"
            path.write_text(content, encoding="utf-8")
            plan = self.folder / "plan.json"
            plan.write_text(QUICK.read_text(encoding="utf-8"), encoding="utf-8")
            code, out, err = call("probe", plan, "--station", path)
            self.assertEqual(code, EXIT_USAGE, out + err)
            self.assertNotIn("Traceback", out + err)

    def test_negative_wait_from_an_expression_is_an_abort_with_outputs_off(self):
        data = clone(base_plan())
        data["steps"][0]["apply"].append({"wait_s": "=0 - vin"})
        result, detail, bench = self.run_plan(data)
        self.assertEqual(result.status, "aborted")
        self.assertIn("wait", result.reason)
        self.assertFalse(bench.model.supply_on)
        self.assertEqual(detail["status"], "aborted")

    def test_limits_crossed_at_run_time_abort_and_do_not_blame_the_unit(self):
        data = clone(base_plan())
        data["steps"][0]["measure"][0]["low"] = "=vnom + 1"
        data["steps"][0]["measure"][0]["high"] = "=vnom - 1"
        result, detail, _ = self.run_plan(data)
        self.assertEqual(result.status, "aborted")
        self.assertEqual(detail["status"], "aborted")

    def test_exit_code_of_a_failed_unit(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        code, _, _ = call("run", QUICK, "--serial", "A", "--sim", "--dut", "dead", "--db", Path(folder) / "r.sqlite")
        self.assertEqual(code, EXIT_FAIL)


if __name__ == "__main__":
    unittest.main()
