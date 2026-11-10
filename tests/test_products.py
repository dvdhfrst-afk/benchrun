"""Products: part numbers with their own plan, variables, limits and serial pattern."""

import json
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from benchrun import audit, products
from benchrun.cli import build_parser
from benchrun.errors import PlanError
from benchrun.plan import apply_overrides, describe_limit_side, find_measure, load_plan, override_problems, parse_plan
from benchrun.reports.html_report import render_html
from benchrun.store import Store
from tests.support import base_plan, clone
from tests.test_cli import call

ROOT = Path(__file__).parent.parent
QUICK = str(ROOT / "examples" / "regulator_quick.json")
LINE_LOAD = str(ROOT / "examples" / "regulator_line_load.json")
EXIT_PASS, EXIT_FAIL, EXIT_USAGE, EXIT_ABORTED = 0, 1, 2, 3


class Overrides(unittest.TestCase):
    """The plan function that puts a product's values into a loaded plan."""

    def setUp(self):
        self.plan = parse_plan(base_plan(), "the source text")

    def test_limits_replace_both_sides_and_an_open_side_stays_open(self):
        apply_overrides(self.plan, {}, {("vout_vs_vin", "vout"): (4.95, None), ("final_check", "vout"): (None, 5.05)})
        sweep = find_measure(self.plan, "vout_vs_vin", "vout")
        self.assertEqual((sweep.low, sweep.high), (4.95, None))
        final = find_measure(self.plan, "final_check", "vout")
        self.assertEqual((final.low, final.high), (None, 5.05))
        self.assertEqual(self.plan.source_text, "the source text")

    def test_summary_values_can_be_given_limits(self):
        apply_overrides(self.plan, {}, {("vout_vs_vin", "span_mv"): (None, 5.0)})
        self.assertEqual(find_measure(self.plan, "vout_vs_vin", "span_mv").high, 5.0)

    def test_variables_apply_unless_fixed_by_the_command_line(self):
        apply_overrides(self.plan, {"vnom": 3.3}, {})
        self.assertEqual(self.plan.variables["vnom"], 3.3)
        plan = parse_plan(base_plan(), variables={"vnom": 12.0})
        apply_overrides(plan, {"vnom": 3.3}, {}, fixed=["vnom"])
        self.assertEqual(plan.variables["vnom"], 12.0)

    def test_every_problem_is_reported_and_nothing_changes(self):
        data = clone(base_plan())
        del data["steps"][0]["summary"][0]["units"]
        plan = parse_plan(data)
        limits = {
            ("nope", "vout"): (1.0, 2.0),
            ("vout_vs_vin", "nope"): (1.0, 2.0),
            ("vout_vs_vin", "span_mv"): (1.0, 2.0),
            ("vout_vs_vin", "vout"): (6.0, 5.0),
            ("final_check", "vout"): (None, None),
        }
        with self.assertRaises(PlanError) as caught:
            apply_overrides(plan, {"missing": 1.0, "vnom": float("nan")}, limits)
        text = "\n".join(caught.exception.problems)
        for expected in (
            "the plan has no variable of that name",
            "not a finite number",
            "the plan has no step 'nope'",
            "has no measurement 'nope'",
            "no units",
            "low limit 6 is above high limit 5",
            "give a low limit, a high limit or both",
        ):
            self.assertIn(expected, text)
        self.assertEqual(len(caught.exception.problems), 7)
        self.assertEqual(plan.variables["vnom"], 5.0)
        self.assertIsNot(find_measure(plan, "vout_vs_vin", "vout").low, 6.0)

    def test_limits_must_be_numbers(self):
        self.assertTrue(override_problems(self.plan, {}, {("final_check", "vout"): ("5", None)}))
        self.assertTrue(override_problems(self.plan, {}, {("final_check", "vout"): (True, None)}))
        self.assertTrue(override_problems(self.plan, {}, {("final_check", "vout"): (float("inf"), None)}))

    def test_limits_are_described_as_numbers_where_the_variables_are_enough(self):
        sweep = find_measure(self.plan, "vout_vs_vin", "vout")
        self.assertEqual(describe_limit_side(sweep.low, self.plan.variables), "4.9")
        self.assertEqual(describe_limit_side(sweep.low, {}), "=vnom - 0.1")
        self.assertEqual(describe_limit_side(None, {}), "")


class SerialPatterns(unittest.TestCase):
    def test_ordinary_patterns_are_accepted_and_match_the_whole_serial(self):
        for pattern, good, bad in (
            (r"SN-\d{4}", "SN-1001", "SN-10011"),
            (r"PN100-[A-Z]{2}\d{6}", "PN100-AB123456", "PN100-ab123456"),
            (r"(?:REV-)?[0-9]{3,5}", "REV-123", "REV-12"),
            (r"(A|B)-\d+", "B-77", "C-77"),
            (r"^SN-\d{4}$", "SN-1234", "XSN-1234"),
        ):
            with self.subTest(pattern):
                self.assertEqual(products.check_pattern(pattern), pattern)
                self.assertTrue(products.pattern_matches(pattern, good))
                self.assertFalse(products.pattern_matches(pattern, bad))

    def test_patterns_that_can_be_slow_or_are_unsupported_are_refused(self):
        for pattern in (
            r"(a+)+$",
            r"(a|aa)*",
            r"((a*))*",
            r"(a|b){20}",
            r"\d*\d*\d*\d*X",
            r"(a|b)(c|d)(e|f)(g|h)(i|j)",
            r"(\w)\1",
            r"(?=SN)SN",
            r"(?P<x>a)",
            "a**",
            "*a",
            "a{,3}",
            "[abc",
            "(abc",
            "abc)",
            "abc\\",
            "x" * 201,
            "SN\n1",
        ):
            with self.subTest(pattern):
                with self.assertRaises(PlanError):
                    products.check_pattern(pattern)

    def test_the_worst_accepted_pattern_answers_fast_on_the_longest_serial(self):
        pattern = r"\d*\d*\d*(1|2|3|4)(5|6|7|8)X"
        products.check_pattern(pattern)
        started = time.perf_counter()
        self.assertFalse(products.pattern_matches(pattern, "1" * products.MAX_SERIAL_CHARS))
        self.assertLess(time.perf_counter() - started, 1.0)

    def test_a_serial_longer_than_any_accepted_never_matches(self):
        self.assertFalse(products.pattern_matches(r".*", "1" * (products.MAX_SERIAL_CHARS + 1)))


class ProductCase(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="benchrun-products-"))
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.db = self.folder / "r.sqlite"

    def product(self, *args):
        return call("product", *args, "--db", self.db)

    def ok(self, *args):
        code, out, err = self.product(*args)
        self.assertEqual(code, EXIT_PASS, out + err)
        return out

    def add_quick(self, part="PN-100-A", *extra, plan=QUICK):
        return self.ok("add", part, "--plan", plan, "--description", "5 V regulator", "--actor", "Operator A", *extra)

    def audit_entries(self, subject):
        with Store(self.db) as store:
            return audit.entries(store.conn, subject=subject)

    def run_count(self):
        with sqlite3.connect(self.db) as conn:
            return conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]


class Records(ProductCase):
    def test_add_list_show_and_json(self):
        self.add_quick("PN-100-A", "--serial-pattern", r"SN-\d{4}")
        self.ok("set-var", "PN-100-A", "vnom", "5.0")
        self.ok("set-limit", "PN-100-A", "output_voltage", "vout", "--low", "4.95", "--high", "5.05")
        listing = json.loads(self.ok("list", "--json"))["products"]
        self.assertEqual(len(listing), 1)
        self.assertEqual((listing[0]["part_number"], listing[0]["variables"], listing[0]["limits"], listing[0]["runs"]), ("PN-100-A", 1, 1, 0))
        self.assertIn("PN-100-A", self.ok("list"))
        shown = json.loads(self.ok("show", "PN-100-A", "--json"))
        self.assertEqual(shown["plan_file"], QUICK)
        self.assertEqual(shown["serial_pattern"], r"SN-\d{4}")
        self.assertEqual(shown["variables"], {"vnom": 5.0})
        self.assertEqual(shown["limits"], [{"step": "output_voltage", "measurement": "vout", "low": 4.95, "high": 5.05}])
        text = self.ok("show", "PN-100-A")
        self.assertIn("vnom", text)
        self.assertIn("4.95", text)

    def test_every_action_and_option_has_help(self):
        def choices(parser):
            return next(a for a in parser._actions if isinstance(getattr(a, "choices", None), dict)).choices

        actions = choices(choices(build_parser())["product"])
        self.assertEqual(len(actions), 11)
        for name, sub in actions.items():
            code, out, _ = call("product", name, "--help")
            self.assertEqual(code, EXIT_PASS, name)
            for action in sub._actions:
                self.assertTrue(action.help, f"product {name} {action.dest} has no help text")

    def test_an_empty_list_says_how_to_add_one(self):
        Store(self.db).close()
        self.assertIn("product add", self.ok("list"))

    def test_add_refuses_bad_input(self):
        for args in (
            ("add", "", "--plan", QUICK),
            ("add", " PN", "--plan", QUICK),
            ("add", "P" * 65, "--plan", QUICK),
            ("add", "PN\x07", "--plan", QUICK),
            ("add", "PN-1", "--plan", str(self.folder / "missing.json")),
            ("add", "PN-1", "--plan", QUICK, "--serial-pattern", "(a+)+"),
            ("add", "PN-1", "--plan", QUICK, "--description", "two\nlines"),
        ):
            with self.subTest(args=args):
                code, _, err = self.product(*args)
                self.assertEqual(code, EXIT_USAGE, err)
                self.assertNotIn("most recent call last", err)
        self.add_quick()
        code, _, err = self.product("add", "PN-100-A", "--plan", QUICK)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("already exists", err)

    def test_unknown_products_and_missing_databases_are_plain_errors(self):
        code, _, err = self.product("show", "PN-1")
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("no results database", err)
        self.add_quick()
        for args in (("show", "PN-1"), ("edit", "PN-1", "--description", "x"), ("delete", "PN-1"), ("set-var", "PN-1", "vnom", "1")):
            with self.subTest(args=args):
                code, _, err = self.product(*args)
                self.assertEqual(code, EXIT_USAGE)
                self.assertIn("no product 'PN-1'", err)

    def test_edit_changes_only_what_is_given_and_checks_a_new_plan(self):
        self.add_quick()
        self.ok("set-limit", "PN-100-A", "output_voltage", "vout", "--low", "4.9")
        out = self.ok("edit", "PN-100-A", "--description", "Five volt part", "--serial-pattern", r"SN-\d+")
        self.assertIn("Five volt part", out)
        self.assertIn("already has those values", self.ok("edit", "PN-100-A", "--description", "Five volt part"))
        code, _, err = self.product("edit", "PN-100-A", "--plan", LINE_LOAD)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("output_voltage", err)
        self.assertEqual(self.product("edit", "PN-100-A")[0], EXIT_USAGE)
        self.ok("edit", "PN-100-A", "--serial-pattern", "")
        shown = json.loads(self.ok("show", "PN-100-A", "--json"))
        self.assertEqual((shown["description"], shown["plan_file"], shown["serial_pattern"]), ("Five volt part", QUICK, ""))

    def test_variables_and_limits_are_checked_against_the_plan(self):
        self.add_quick()
        for args, expected in (
            (("set-var", "PN-100-A", "vmissing", "1"), "no variable of that name"),
            (("set-var", "PN-100-A", "vnom", "five"), "is not a number"),
            (("set-var", "PN-100-A", "vnom", "nan"), "not a finite number"),
            (("set-limit", "PN-100-A", "output_voltage", "nope", "--high", "1"), "has no measurement 'nope'"),
            (("set-limit", "PN-100-A", "nope", "vout", "--high", "1"), "has no step 'nope'"),
            (("set-limit", "PN-100-A", "output_voltage", "vout", "--high", "1x"), "is not a number"),
            (("set-limit", "PN-100-A", "output_voltage", "vout", "--low", "6", "--high", "5"), "above high limit"),
            (("set-limit", "PN-100-A", "output_voltage", "vout"), "give a low limit"),
            (("unset-var", "PN-100-A", "vnom"), "no value of its own"),
            (("unset-limit", "PN-100-A", "output_voltage", "vout"), "no limit of its own"),
        ):
            with self.subTest(args=args):
                code, _, err = self.product(*args)
                self.assertEqual(code, EXIT_USAGE, err)
                self.assertIn(expected, err)
        self.ok("set-limit", "PN-100-A", "output_voltage", "vout", "--low", "4.9", "--high", "")
        self.ok("set-var", "PN-100-A", "vnom", "3.3")
        self.ok("unset-var", "PN-100-A", "vnom")
        self.ok("unset-limit", "PN-100-A", "output_voltage", "vout")
        shown = json.loads(self.ok("show", "PN-100-A", "--json"))
        self.assertEqual((shown["variables"], shown["limits"]), ({}, []))

    def test_every_change_writes_one_audit_entry_with_the_actor(self):
        self.add_quick()
        self.ok("set-var", "PN-100-A", "vnom", "5.1", "--actor", "Operator B")
        self.ok("set-limit", "PN-100-A", "output_voltage", "vout", "--high", "5.2", "--actor", "Operator B")
        self.ok("unset-limit", "PN-100-A", "output_voltage", "vout", "--actor", "Operator B")
        self.ok("unset-var", "PN-100-A", "vnom", "--actor", "Operator B")
        self.ok("edit", "PN-100-A", "--description", "x", "--actor", "Operator B")
        self.ok("delete", "PN-100-A", "--actor", "Operator B")
        entries = list(reversed(self.audit_entries("product:PN-100-A")))
        self.assertEqual(
            [e["action"] for e in entries],
            ["product.add", "product.set-var", "product.set-limit", "product.unset-limit", "product.unset-var", "product.edit", "product.delete"],
        )
        self.assertEqual(entries[0]["actor"], "Operator A")
        self.assertEqual({e["actor"] for e in entries[1:]}, {"Operator B"})
        self.assertEqual(entries[1]["detail"], {"name": "vnom", "old": None, "new": 5.1})
        self.assertEqual(entries[2]["detail"]["new"], [None, 5.2])
        self.assertEqual(entries[5]["detail"], {"description": ["5 V regulator", "x"]})

    def test_a_refused_change_writes_no_audit_entry(self):
        self.add_quick()
        self.product("set-var", "PN-100-A", "vmissing", "1")
        self.assertEqual(len(self.audit_entries("product:PN-100-A")), 1)


class Running(ProductCase):
    def run_product(self, *extra, serial="SN-1001"):
        return call("run", "--product", "PN-100-A", "--serial", serial, "--sim", "--db", self.db, *extra)

    def test_the_plan_variables_and_limits_come_from_the_product(self):
        self.add_quick()
        self.ok("set-var", "PN-100-A", "vnom", "5.25")
        self.ok("set-limit", "PN-100-A", "output_voltage", "vout", "--low", "5.2", "--high", "5.3")
        code, out, _ = self.run_product("--dut", "high-output")
        self.assertEqual(code, EXIT_PASS, out)
        with Store(self.db) as store:
            detail = store.run_detail(store.find_runs()[0]["id"])
        self.assertEqual(detail["product"], "PN-100-A")
        self.assertEqual(detail["variables"], {"vnom": 5.25})
        self.assertEqual(detail["plan_source"], Path(QUICK).read_text(encoding="utf-8"))
        limits = {(m["low"], m["high"]) for step in detail["steps"] for m in step["measurements"]}
        self.assertEqual(limits, {(5.2, 5.3)})
        self.assertEqual(self.run_product("--dut", "good")[0], EXIT_FAIL)

    def test_set_on_the_command_line_wins_over_the_product(self):
        self.add_quick()
        self.ok("set-var", "PN-100-A", "vnom", "3.3")
        code, out, _ = self.run_product("--set", "vnom=5")
        self.assertEqual(code, EXIT_PASS, out)

    def test_a_plan_given_as_well_is_used_with_the_product_values(self):
        self.add_quick()
        self.ok("set-limit", "PN-100-A", "output_voltage", "vout", "--low", "4.9", "--high", "5.1")
        other = self.folder / "other.json"
        data = json.loads(Path(QUICK).read_text(encoding="utf-8"))
        data["name"] = "other-plan"
        other.write_text(json.dumps(data), encoding="utf-8")
        code, out, _ = call("run", other, "--product", "PN-100-A", "--serial", "SN-1", "--sim", "--db", self.db)
        self.assertEqual(code, EXIT_PASS, out)
        self.assertIn("other-plan", out)

    def test_values_that_do_not_fit_the_given_plan_stop_the_run_before_it_starts(self):
        self.add_quick()
        self.ok("set-limit", "PN-100-A", "output_voltage", "vout", "--low", "4.9")
        code, _, err = call("run", LINE_LOAD, "--product", "PN-100-A", "--serial", "SN-1", "--sim", "--db", self.db)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("product PN-100-A: limit output_voltage.vout: the plan has no step", err)
        self.assertEqual(self.run_count(), 0)

    def test_a_serial_that_does_not_fit_the_pattern_is_refused_before_anything_is_commanded(self):
        self.add_quick("PN-100-A", "--serial-pattern", r"SN-\d{4}")
        code, out, err = self.run_product(serial="SN-12")
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("does not match the pattern", err)
        self.assertNotIn("Run ", out)
        self.assertEqual(self.run_count(), 0)

    def test_a_missing_product_or_plan_is_refused(self):
        Store(self.db).close()
        code, _, err = self.run_product()
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("no product", err)
        code, _, err = call("run", "--serial", "SN-1", "--sim", "--db", self.db)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("give a plan file", err)

    def test_history_show_and_reports_name_the_product(self):
        self.add_quick()
        self.run_product()
        call("run", QUICK, "--serial", "SN-2", "--sim", "--db", self.db)
        code, out, _ = call("history", "--product", "PN-100-A", "--db", self.db, "--json")
        runs = json.loads(out)["runs"]
        self.assertEqual([(r["serial"], r["product"]) for r in runs], [("SN-1001", "PN-100-A")])
        code, out, _ = call("history", "--db", self.db)
        self.assertIn("product", out.splitlines()[0])
        self.assertIn("PN-100-A", out)
        code, out, _ = call("show", "--serial", "SN-1001", "--db", self.db)
        self.assertIn("Product: PN-100-A", out)
        code, out, _ = call("show", "--serial", "SN-1001", "--db", self.db, "--json")
        self.assertEqual(json.loads(out)["product"], "PN-100-A")
        with Store(self.db) as store:
            html = render_html(store.run_detail(store.find_runs(serial="SN-1001")[0]["id"]))
        self.assertIn("<th>Product</th><td>PN-100-A</td>", html)

    def test_history_without_products_keeps_its_columns(self):
        call("run", QUICK, "--serial", "SN-2", "--sim", "--db", self.db)
        code, out, _ = call("history", "--db", self.db)
        self.assertNotIn("product", out.splitlines()[0])

    def test_validate_shows_the_limits_that_apply(self):
        self.add_quick()
        self.ok("set-var", "PN-100-A", "vnom", "3.3")
        code, out, _ = call("validate", "--product", "PN-100-A", "--db", self.db)
        self.assertEqual(code, EXIT_PASS, out)
        self.assertIn("vnom = 3.3", out)
        row = next(line for line in out.splitlines() if line.startswith("output_voltage"))
        self.assertEqual(row.split(), ["output_voltage", "vout", "V", "3.234", "3.366", "plan"])
        self.ok("set-limit", "PN-100-A", "output_voltage", "vout", "--high", "3.4")
        code, out, _ = call("validate", QUICK, "--product", "PN-100-A", "--db", self.db)
        row = next(line for line in out.splitlines() if line.startswith("output_voltage"))
        self.assertEqual(row.split(), ["output_voltage", "vout", "V", "3.4", "product"])

    def test_delete_refuses_while_runs_name_the_product_and_force_keeps_the_runs(self):
        self.add_quick()
        self.run_product()
        code, _, err = self.product("delete", "PN-100-A")
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("--force", err)
        self.assertIn("1 run still name it", self.ok("delete", "PN-100-A", "--force"))
        self.assertEqual(self.run_count(), 1)
        with Store(self.db) as store:
            self.assertEqual(store.find_runs(product="PN-100-A")[0]["serial"], "SN-1001")
            left = [store.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("products", "product_variables", "product_limits")]
        self.assertEqual(left, [0, 0, 0])
        self.assertEqual(self.audit_entries("product:PN-100-A")[0]["detail"]["runs"], 1)


class LimitFiles(ProductCase):
    def setUp(self):
        super().setUp()
        self.add_quick("PN-200-B", plan=LINE_LOAD)

    def write(self, text, name="limits.csv", encoding="utf-8"):
        path = self.folder / name
        path.write_bytes(text.encode(encoding))
        return path

    def limits(self):
        return json.loads(self.ok("show", "PN-200-B", "--json"))["limits"]

    def test_import_shows_a_report_and_dry_run_changes_nothing(self):
        path = self.write(
            "step,measurement,low,high,units\n"
            "line_regulation,line_reg,,20,mV\n"
            "quiescent_current,iq,0.5,4,mA\n"
        )
        out = self.ok("import-limits", "PN-200-B", path, "--dry-run")
        self.assertIn("2 added, 0 changed, 0 unchanged, 0 removed", out)
        self.assertIn("Dry run", out)
        self.assertEqual(self.limits(), [])
        out = self.ok("import-limits", "PN-200-B", path)
        self.assertIn("Saved.", out)
        self.assertEqual(len(self.limits()), 2)
        self.assertIn("0 added, 0 changed, 2 unchanged", self.ok("import-limits", "PN-200-B", path))
        entries = self.audit_entries("product:PN-200-B")
        self.assertEqual([e["action"] for e in entries].count("product.import-limits"), 1)
        self.assertEqual(len(entries[0]["detail"]["changes"]), 2)

    def test_changed_and_replaced_limits_are_reported(self):
        self.ok("set-limit", "PN-200-B", "line_regulation", "line_reg", "--high", "25")
        self.ok("set-limit", "PN-200-B", "load_regulation", "load_reg", "--high", "25")
        path = self.write("units,step,measurement,high,low\nmV,line_regulation,line_reg,20,\n")
        out = self.ok("import-limits", "PN-200-B", path, "--replace")
        self.assertIn("0 added, 1 changed, 0 unchanged, 1 removed", out)
        self.assertIn("<= 25 mV", out)
        self.assertEqual(self.limits(), [{"step": "line_regulation", "measurement": "line_reg", "low": None, "high": 20.0}])

    def test_one_bad_row_refuses_the_whole_file(self):
        path = self.write(
            "step,measurement,low,high,units\n"
            "line_regulation,line_reg,,20,mV\n"
            "line_regulation,line_reg,,21,mV\n"
            "quiescent_current,iq,5,4,mA\n"
            "quiescent_current,iq_missing,,4,mA\n"
            "low_headroom,vout,4.9,5.1,mV\n"
            "load_regulation,pdiss,=1+1,8,W\n"
            "line_regulation,supply_limited,,0,\n"
            "ripple_rejection,psrr,40\n"
            ",,,,\n"
        )
        code, _, err = self.product("import-limits", "PN-200-B", path)
        self.assertEqual(code, EXIT_USAGE)
        for expected in (
            "row 3: line_regulation.line_reg is also on row 2",
            "row 4: limit quiescent_current.iq: low limit 5 is above high limit 4",
            "row 5: limit quiescent_current.iq_missing",
            "row 6: units 'mV' are not the plan's units for low_headroom.vout, which are 'V'",
            "row 7: low '=1+1' is not a number",
            "row 8: limit line_regulation.supply_limited: the plan gives this measurement no units",
            "row 9: has 3 cells",
        ):
            self.assertIn(expected, err)
        self.assertEqual(self.limits(), [])
        self.assertEqual([e["action"] for e in self.audit_entries("product:PN-200-B")], ["product.add"])

    def test_hostile_files_are_refused_plainly(self):
        cases = (
            (self.write("step,measurement,low,high\nline_regulation,line_reg,,20\n", "short.csv"), "first row must name"),
            (self.write("step,measurement,low,high,units,extra\n", "extra.csv"), "first row must name"),
            (self.write("", "empty.csv"), "is empty"),
            (self.write("step,measurement,low,high,units\n" + chr(0xE9) + ",x,1,2,V\n", "latin.csv", "latin-1"), "not UTF-8"),
            (self.write("step,measurement,low,high,units\na\x00b,x,1,2,V\n", "nul.csv"), "control character on line 2"),
            (self.write("step,measurement,low,high,units\n" + "x" * 200000 + ",x,1,2,V\n", "wide.csv"), "not a usable CSV"),
            (self.write("x" * (products.MAX_CSV_BYTES + 1), "big.csv"), "too big"),
            (self.folder / "missing.csv", "cannot read"),
        )
        for path, expected in cases:
            with self.subTest(path.name):
                code, _, err = self.product("import-limits", "PN-200-B", path)
                self.assertEqual(code, EXIT_USAGE, err)
                self.assertIn(expected, err)
                self.assertNotIn("most recent call last", err)

    def test_a_byte_order_mark_and_spreadsheet_text_marks_are_accepted(self):
        path = self.write(chr(0xFEFF) + "step,measurement,low,high,units\r\nquiescent_current,iq,'0.5,'4,'mA\r\n")
        self.ok("import-limits", "PN-200-B", path)
        self.assertEqual(self.limits(), [{"step": "quiescent_current", "measurement": "iq", "low": 0.5, "high": 4.0}])

    def test_export_round_trips_and_never_overwrites_without_force(self):
        self.ok("set-limit", "PN-200-B", "low_headroom", "vout", "--low", "4.85", "--high", "5.15")
        self.ok("set-limit", "PN-200-B", "quiescent_current", "iq", "--high", "0.1")
        printed = self.ok("export-limits", "PN-200-B")
        self.assertEqual(
            printed.splitlines(),
            ["step,measurement,low,high,units", "quiescent_current,iq,,0.1,mA", "low_headroom,vout,4.85,5.15,V"],
        )
        out = self.folder / "out.csv"
        self.ok("export-limits", "PN-200-B", "--out", out)
        self.assertEqual(out.read_text(encoding="utf-8"), printed)
        code, _, err = self.product("export-limits", "PN-200-B", "--out", out)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("--force", err)
        self.ok("export-limits", "PN-200-B", "--out", out, "--force")
        self.assertIn("2 unchanged", self.ok("import-limits", "PN-200-B", out))

    def test_export_neutralises_text_a_spreadsheet_would_run(self):
        plan_path = self.folder / "plan.json"
        data = json.loads(Path(QUICK).read_text(encoding="utf-8"))
        data["steps"][0]["summary"] = [{"name": "spread", "calc": "span(vout) * 1000", "units": "=mV", "high": 50}]
        plan_path.write_text(json.dumps(data), encoding="utf-8")
        self.ok("add", "PN-300", "--plan", plan_path)
        self.ok("set-limit", "PN-300", "output_voltage", "spread", "--high", "-1")
        printed = self.ok("export-limits", "PN-300")
        self.assertIn("output_voltage,spread,,-1.0,'=mV", printed)
        self.assertEqual(load_plan(plan_path).steps[0].summary[0].units, "=mV")
        exported = self.folder / "neutral.csv"
        self.ok("export-limits", "PN-300", "--out", exported)
        self.assertIn("1 unchanged", self.ok("import-limits", "PN-300", exported))


if __name__ == "__main__":
    unittest.main()
