"""Plan loading and validation."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from benchrun.errors import PlanError
from benchrun.plan import apply_addresses, load_addresses, load_plan, match_station, parse_plan
from tests.support import base_plan, clone

EXAMPLES = Path(__file__).parent.parent / "examples"


class Problems(unittest.TestCase):
    def assertProblem(self, data, text):
        with self.assertRaises(PlanError) as caught:
            parse_plan(data)
        joined = "\n".join(caught.exception.problems)
        self.assertIn(text, joined)
        return caught.exception.problems

    def change(self, func):
        data = clone(base_plan())
        func(data)
        return data

    def test_base_plan_is_valid(self):
        plan = parse_plan(base_plan())
        self.assertEqual(plan.name, "small")
        self.assertEqual([s.name for s in plan.steps], ["vout_vs_vin", "final_check"])

    def test_not_an_object(self):
        self.assertProblem([], "expected an object")

    def test_missing_required_keys(self):
        problems = self.assertProblem({}, "missing required key 'name'")
        joined = "\n".join(problems)
        self.assertIn("missing required key 'instruments'", joined)
        self.assertIn("missing required key 'steps'", joined)

    def test_every_problem_is_reported_together(self):
        def spoil(d):
            d["bogus"] = 1
            d["instruments"]["dmm"]["driver"] = "nope"
            d["steps"][1]["settle_s"] = -1

        problems = self.assertProblem(self.change(spoil), "unknown key 'bogus'")
        joined = "\n".join(problems)
        self.assertIn("unknown driver 'nope'", joined)
        self.assertIn("settle_s", joined)

    def test_typo_in_a_limit_key_is_caught(self):
        def spoil(d):
            d["steps"][1]["measure"][0]["hihg"] = 5.1

        self.assertProblem(self.change(spoil), "unknown key 'hihg'")

    def test_unknown_instrument_in_a_call(self):
        def spoil(d):
            d["setup"][0]["do"] = "laser.fire"

        self.assertProblem(self.change(spoil), "unknown instrument 'laser'")

    def test_unknown_action(self):
        def spoil(d):
            d["setup"][0]["do"] = "supply.explode"

        self.assertProblem(self.change(spoil), "has no action 'explode'")

    def test_reading_is_not_an_action(self):
        def spoil(d):
            d["setup"][0] = {"do": "supply.measure_voltage"}

        self.assertProblem(self.change(spoil), "has no action")

    def test_wrong_arguments_to_an_action(self):
        def spoil(d):
            d["setup"][0] = {"do": "supply.set_current_limit", "volts": 1}

        self.assertProblem(self.change(spoil), "supply.set_current_limit")

    def test_literal_of_the_wrong_type_is_caught_before_any_run(self):
        for bad in ("twelve", True, None, [1]):

            def spoil(d, bad=bad):
                d["setup"][0] = {"do": "supply.set_current_limit", "amps": bad}

            self.assertProblem(self.change(spoil), "must be a number")

    def test_reading_argument_of_the_wrong_type(self):
        def spoil(d):
            d["steps"][1]["measure"][0]["args"] = {"range_v": "ten"}

        self.assertProblem(self.change(spoil), "must be a number")

    def test_text_arguments_stay_text(self):
        def good(d):
            d["setup"][0] = {"do": "dmm.write", "command": "VOLT:DC:NPLC 1"}

        parse_plan(self.change(good))

    def test_call_needs_do_or_wait(self):
        def spoil(d):
            d["setup"][0] = {"nothing": 1}

        self.assertProblem(self.change(spoil), "expected 'do' or 'wait_s'")

    def test_negative_wait(self):
        def spoil(d):
            d["setup"][0] = {"wait_s": -1}

        self.assertProblem(self.change(spoil), "at least 0")

    def test_bad_target_shape(self):
        def spoil(d):
            d["setup"][0]["do"] = "supply"

        self.assertProblem(self.change(spoil), "expected 'instrument.method'")

    def test_unknown_reading(self):
        def spoil(d):
            d["steps"][1]["measure"][0]["read"] = "dmm.measure_tea"

        self.assertProblem(self.change(spoil), "has no reading 'measure_tea'")

    def test_units_must_fit_the_reading(self):
        def spoil(d):
            d["steps"][1]["measure"][0]["units"] = "A"

        self.assertProblem(self.change(spoil), "do not fit")

    def test_compatible_units_are_fine(self):
        def good(d):
            m = d["steps"][1]["measure"][0]
            m["units"], m["low"], m["high"] = "mV", 4900.0, 5100.0

        parse_plan(self.change(good))

    def test_units_default_to_the_reading(self):
        def good(d):
            del d["steps"][1]["measure"][0]["units"]

        plan = parse_plan(self.change(good))
        self.assertEqual(plan.steps[1].measures[0].units, "V")

    def test_low_above_high(self):
        def spoil(d):
            d["steps"][1]["measure"][0]["low"] = 6.0

        self.assertProblem(self.change(spoil), "is above high limit")

    def test_limit_must_be_a_number(self):
        def spoil(d):
            d["steps"][1]["measure"][0]["low"] = "4.9"

        self.assertProblem(self.change(spoil), "expected a number")

    def test_read_and_calc_are_exclusive(self):
        def spoil(d):
            d["steps"][1]["measure"][0]["calc"] = "1"

        self.assertProblem(self.change(spoil), "exactly one of 'read' or 'calc'")

    def test_summary_cannot_read(self):
        def spoil(d):
            d["steps"][0]["summary"][0] = {"name": "x", "read": "dmm.measure_dc_voltage", "units": "V", "high": 1}

        self.assertProblem(self.change(spoil), "can only use 'calc'")

    def test_unknown_name_in_an_expression(self):
        def spoil(d):
            d["steps"][1]["measure"][0]["low"] = "=vmon - 1"

        self.assertProblem(self.change(spoil), "unknown names: vmon")

    def test_measure_can_use_only_earlier_measurements(self):
        def spoil(d):
            d["steps"][1]["measure"].insert(0, {"name": "early", "calc": "vout * 2", "units": "V"})

        self.assertProblem(self.change(spoil), "unknown names: vout")

    def test_calc_can_use_earlier_measurements(self):
        def good(d):
            d["steps"][1]["measure"].append({"name": "double", "calc": "vout * 2", "units": "V"})

        parse_plan(self.change(good))

    def test_summary_list_misuse_is_a_plan_problem(self):
        def spoil(d):
            d["steps"][0]["summary"][0]["calc"] = "vout * 1000"

        self.assertProblem(self.change(spoil), "is a list here")

    def test_list_function_on_a_scalar_inside_a_point(self):
        def spoil(d):
            d["steps"][1]["measure"].append({"name": "avg", "calc": "mean(vout)", "units": "V"})

        self.assertProblem(self.change(spoil), "needs one measurement")

    def test_sweep_variable_is_usable_in_a_summary(self):
        def good(d):
            d["steps"][0]["summary"].append({"name": "per_volt", "calc": "span(vout) / span(vin)", "units": "V/V"})

        parse_plan(self.change(good))

    def test_duplicate_measurement_name(self):
        def spoil(d):
            d["steps"][1]["measure"].append(dict(d["steps"][1]["measure"][0]))

        self.assertProblem(self.change(spoil), "already used")

    def test_measurement_cannot_shadow_a_variable_or_function(self):
        for name in ("vnom", "max"):

            def spoil(d, name=name):
                d["steps"][1]["measure"][0]["name"] = name

            self.assertProblem(self.change(spoil), "already used")

    def test_duplicate_step_name(self):
        def spoil(d):
            d["steps"][1]["name"] = "vout_vs_vin"

        self.assertProblem(self.change(spoil), "used twice")

    def test_bad_names(self):
        def spoil(d):
            d["steps"][1]["name"] = "has space"

        self.assertProblem(self.change(spoil), "must start with")

    def test_no_steps(self):
        def spoil(d):
            d["steps"] = []

        self.assertProblem(self.change(spoil), "at least one step")

    def test_no_instruments(self):
        def spoil(d):
            d["instruments"] = {}

        self.assertProblem(self.change(spoil), "at least one instrument")

    def test_step_with_nothing_to_measure(self):
        def spoil(d):
            d["steps"][1]["measure"] = []

        self.assertProblem(self.change(spoil), "nothing to measure")

    def test_plan_with_no_limits_at_all(self):
        def spoil(d):
            d["steps"] = [d["steps"][1]]
            del d["steps"][0]["measure"][0]["low"]
            del d["steps"][0]["measure"][0]["high"]

        self.assertProblem(self.change(spoil), "could ever pass or fail")

    def test_bad_samples(self):
        for bad in (0, -1, 1.5, True):

            def spoil(d, bad=bad):
                d["steps"][1]["measure"][0]["samples"] = bad

            self.assertProblem(self.change(spoil), "samples must be")

    def test_bad_idn_pattern(self):
        def spoil(d):
            d["instruments"]["dmm"]["expect_idn"] = "("

        self.assertProblem(self.change(spoil), "regular expression")

    def test_bad_address_in_a_plan(self):
        def spoil(d):
            d["instruments"]["dmm"]["address"] = "host:notaport"

        self.assertProblem(self.change(spoil), "bad port")

    def test_bad_timeout(self):
        def spoil(d):
            d["instruments"]["dmm"]["timeout_s"] = 0

        self.assertProblem(self.change(spoil), "timeout_s")

    def test_command_override_is_checked(self):
        def spoil(d):
            d["instruments"]["dmm"]["commands"] = {"nope": "X"}

        self.assertProblem(self.change(spoil), "unknown command key")

    def test_good_command_override(self):
        def good(d):
            d["instruments"]["supply"]["commands"] = {"set_voltage": "APPL {volts:.3f}"}

        plan = parse_plan(self.change(good))
        self.assertEqual(plan.instruments["supply"].commands["set_voltage"], "APPL {volts:.3f}")

    def test_variable_clash_with_function(self):
        def spoil(d):
            d["vars"]["max"] = 1

        self.assertProblem(self.change(spoil), "clashes with a built-in")

    def test_stop_on_fail_must_be_boolean(self):
        def spoil(d):
            d["steps"][1]["stop_on_fail"] = "yes"

        self.assertProblem(self.change(spoil), "stop_on_fail")


class Sweeps(unittest.TestCase):
    def points(self, sweep):
        data = clone(base_plan())
        data["steps"][0]["sweep"] = sweep
        return parse_plan(data).steps[0].points()

    def values(self, sweep):
        return [p["vin"] for p in self.points(sweep)]

    def test_explicit_values(self):
        self.assertEqual(self.values({"var": "vin", "values": [1, 3, 2]}), [1.0, 3.0, 2.0])

    def test_start_stop_step_includes_the_end(self):
        self.assertEqual(self.values({"var": "vin", "start": 7, "stop": 10, "step": 1}), [7.0, 8.0, 9.0, 10.0])

    def test_fractional_step_stays_clean(self):
        values = self.values({"var": "vin", "start": 0, "stop": 1, "step": 0.1})
        self.assertEqual(len(values), 11)
        self.assertEqual(values[3], 0.3)
        self.assertEqual(values[-1], 1.0)

    def test_step_that_misses_the_end_stops_before_it(self):
        self.assertEqual(self.values({"var": "vin", "start": 0, "stop": 1, "step": 0.4}), [0.0, 0.4, 0.8])

    def test_downward_sweep(self):
        self.assertEqual(self.values({"var": "vin", "start": 3, "stop": 1, "step": -1}), [3.0, 2.0, 1.0])

    def test_points(self):
        self.assertEqual(self.values({"var": "vin", "start": 0, "stop": 1, "points": 3}), [0.0, 0.5, 1.0])

    def test_nested_sweep_outer_first(self):
        data = clone(base_plan())
        data["steps"][0]["sweep"] = [
            {"var": "vin", "values": [8, 12]},
            {"var": "iload", "values": [0, 1]},
        ]
        points = parse_plan(data).steps[0].points()
        self.assertEqual(
            points,
            [
                {"vin": 8.0, "iload": 0.0},
                {"vin": 8.0, "iload": 1.0},
                {"vin": 12.0, "iload": 0.0},
                {"vin": 12.0, "iload": 1.0},
            ],
        )

    def test_no_sweep_is_one_empty_point(self):
        self.assertEqual(parse_plan(base_plan()).steps[1].points(), [{}])

    def test_bad_sweeps(self):
        for sweep, text in (
            ({"var": "vin", "start": 1, "stop": 2, "step": 0}, "never gets"),
            ({"var": "vin", "start": 1, "stop": 2, "step": -1}, "never gets"),
            ({"var": "vin", "start": 1, "stop": 2}, "exactly one of"),
            ({"var": "vin", "start": 1, "stop": 2, "step": 1, "points": 2}, "exactly one of"),
            ({"var": "vin", "start": 1, "stop": 2, "points": 1}, "points must be"),
            ({"var": "vin", "values": []}, "no values"),
            ({"var": "vin", "values": [1], "start": 1}, "not both"),
            ({"var": "vnom", "values": [1]}, "already defined"),
            ({"var": "max", "values": [1]}, "clashes"),
            ({"var": "vin", "start": 0, "stop": 1e9, "step": 1}, "most allowed"),
        ):
            with self.assertRaises(PlanError, msg=sweep) as caught:
                self.points(sweep)
            self.assertIn(text, "\n".join(caught.exception.problems), sweep)

    def test_duplicate_nested_variable(self):
        data = clone(base_plan())
        data["steps"][0]["sweep"] = [{"var": "vin", "values": [1]}, {"var": "vin", "values": [2]}]
        with self.assertRaisesRegex(PlanError, "already defined"):
            parse_plan(data)


class Files(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)

    def write(self, name, text):
        path = self.folder / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_examples_are_valid(self):
        for name in ("regulator_line_load.json", "regulator_quick.json"):
            plan = load_plan(EXAMPLES / name)
            self.assertTrue(plan.source_hash)
            self.assertIn('"steps"', plan.source_text)

    def test_missing_file(self):
        with self.assertRaisesRegex(PlanError, "cannot read"):
            load_plan(self.folder / "absent.json")

    def test_invalid_json(self):
        with self.assertRaisesRegex(PlanError, "not valid JSON"):
            load_plan(self.write("bad.json", "{ not json"))

    def test_hash_changes_with_the_file(self):
        one = load_plan(self.write("a.json", json.dumps(base_plan())))
        changed = clone(base_plan())
        changed["steps"][1]["measure"][0]["high"] = 5.2
        two = load_plan(self.write("b.json", json.dumps(changed)))
        self.assertNotEqual(one.source_hash, two.source_hash)

    def test_variable_override(self):
        path = self.write("a.json", json.dumps(base_plan()))
        self.assertEqual(load_plan(path, {"vnom": 3.3}).variables["vnom"], 3.3)
        with self.assertRaisesRegex(PlanError, "no variable named 'nope'"):
            load_plan(path, {"nope": 1})

    def test_station_file(self):
        path = self.write("s.json", json.dumps({"addresses": {"dmm": "192.0.2.1:5025"}}))
        self.assertEqual(load_addresses(path), {"dmm": "192.0.2.1:5025"})

    def test_bad_station_files(self):
        with self.assertRaisesRegex(PlanError, "cannot read"):
            load_addresses(self.folder / "none.json")
        with self.assertRaisesRegex(PlanError, "not valid JSON"):
            load_addresses(self.write("a.json", "nope"))
        with self.assertRaisesRegex(PlanError, "needs an object"):
            load_addresses(self.write("b.json", "{}"))

    def test_station_example_matches_the_example_plan(self):
        plan = load_plan(EXAMPLES / "regulator_line_load.json")
        addresses = load_addresses(EXAMPLES / "station_example.json")
        apply_addresses(plan, addresses)
        self.assertEqual(plan.instruments["dmm"].address, "192.0.2.13:5025")


class Addresses(unittest.TestCase):
    def test_changing_addresses_changes_nothing_else(self):
        data = clone(base_plan())
        plan = parse_plan(data)
        apply_addresses(plan, {"supply": "192.0.2.1:5025", "dmm": "192.0.2.2"})
        self.assertEqual(plan.instruments["supply"].address, "192.0.2.1:5025")
        self.assertEqual(plan.instruments["dmm"].driver, "dmm")

    def test_station_may_describe_more_than_the_plan_uses(self):
        plan = parse_plan(base_plan())
        addresses, unused = match_station(plan, {"supply": "a", "dmm": "b", "load": "c", "scope": "d"})
        self.assertEqual(addresses, {"supply": "a", "dmm": "b"})
        self.assertEqual(sorted(unused), ["load"])

    def test_station_keys_may_be_driver_names(self):
        plan = parse_plan(base_plan())
        addresses, unused = match_station(plan, {"psu": "a", "dmm": "b", "scope": "c"})
        self.assertEqual(addresses, {"supply": "a", "dmm": "b"})
        self.assertEqual(unused, [])

    def test_instrument_name_beats_driver_name(self):
        plan = parse_plan(base_plan())
        addresses, _ = match_station(plan, {"psu": "a", "supply": "z", "dmm": "b"})
        self.assertEqual(addresses["supply"], "z")

    def test_unknown_instrument(self):
        plan = parse_plan(base_plan())
        with self.assertRaisesRegex(PlanError, "unknown instrument 'nope'"):
            apply_addresses(plan, {"nope": "x"})

    def test_missing_address(self):
        plan = parse_plan(base_plan())
        with self.assertRaises(PlanError) as caught:
            apply_addresses(plan, {"dmm": "x"})
        self.assertIn("'supply' has no address", caught.exception.problems[0])

    def test_bad_address(self):
        plan = parse_plan(base_plan())
        with self.assertRaisesRegex(PlanError, "bad port"):
            apply_addresses(plan, {"dmm": "x:y", "supply": "z"})


if __name__ == "__main__":
    unittest.main()
