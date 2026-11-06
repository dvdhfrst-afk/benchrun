"""The runner: verdicts, aborts, teardown, and what gets recorded."""

import io
import socket
import unittest

from benchrun.plan import apply_addresses, parse_plan
from benchrun.runner import Runner
from benchrun.sim.instruments import Fault
from tests.support import BenchTestCase, all_measurements, base_plan, clone, step_named


class Verdicts(BenchTestCase):
    def test_good_unit_passes(self):
        result, detail, _ = self.run_plan(base_plan())
        self.assertEqual(result.status, "pass")
        self.assertEqual(detail["status"], "pass")
        self.assertIsNone(detail["reason"])
        self.assertEqual(result.failed, 0)
        self.assertEqual(result.passed, 5)
        self.assertIsNotNone(detail["finished_at"])

    def test_every_measurement_is_recorded_with_limits_and_verdict(self):
        _, detail, _ = self.run_plan(base_plan())
        measured = all_measurements(detail)
        self.assertEqual([m["name"] for m in measured], ["vout", "vout", "vout", "span_mv", "vout"])
        first = measured[0]
        self.assertEqual(first["point"], {"vin": 8.0})
        self.assertAlmostEqual(first["value"], 4.996, places=7)
        self.assertEqual(first["units"], "V")
        self.assertAlmostEqual(first["low"], 4.9)
        self.assertAlmostEqual(first["high"], 5.1)
        self.assertEqual(first["verdict"], "pass")
        self.assertEqual(first["instrument"], "dmm")
        self.assertEqual(first["kind"], "read")
        self.assertEqual(measured[3]["kind"], "calc")
        self.assertAlmostEqual(measured[3]["value"], 8.0, places=4)

    def test_idn_of_each_instrument_is_recorded(self):
        _, detail, bench = self.run_plan(base_plan())
        recorded = {i["name"]: i for i in detail["instruments"]}
        self.assertEqual(recorded["dmm"]["idn"], "benchrun,SIM-DMM,SIM0001,1.0")
        self.assertEqual(recorded["supply"]["idn"], "benchrun,SIM-PSU,SIM0002,1.0")
        self.assertEqual(recorded["dmm"]["address"], bench.addresses["dmm"])
        self.assertEqual(recorded["dmm"]["driver"], "dmm")

    def test_plan_is_kept_with_the_run(self):
        _, detail, _ = self.run_plan(base_plan())
        self.assertIn("vout_vs_vin", detail["plan_source"])
        self.assertEqual(len(detail["plan_hash"]), 64)
        self.assertEqual(detail["variables"], {"vnom": 5.0})

    def test_failing_unit_fails_and_still_finishes(self):
        result, detail, _ = self.run_plan(base_plan(), dut="high-output")
        self.assertEqual(result.status, "fail")
        self.assertIsNone(detail["reason"])
        self.assertEqual(result.failed, 4)
        self.assertEqual(step_named(detail, "vout_vs_vin")["status"], "fail")
        self.assertEqual(step_named(detail, "final_check")["status"], "fail")
        self.assertIn("3 of 4", step_named(detail, "vout_vs_vin")["message"])

    def test_measurement_without_limits_never_decides(self):
        data = clone(base_plan())
        data["steps"][1]["measure"].append({"name": "note", "read": "supply.measure_current", "units": "A"})
        result, detail, _ = self.run_plan(data, dut="high-iq")
        self.assertEqual(result.status, "pass")
        self.assertEqual(all_measurements(detail)[-1]["verdict"], "info")

    def test_limits_are_computed_at_each_point(self):
        data = clone(base_plan())
        data["steps"][0]["measure"][0]["low"] = "=vin - 7.05"
        data["steps"][0]["measure"][0]["high"] = "=vin"
        _, detail, _ = self.run_plan(data)
        vouts = all_measurements(detail)[:3]
        self.assertEqual([m["verdict"] for m in vouts], ["pass", "pass", "fail"])
        self.assertAlmostEqual(vouts[2]["low"], 8.95)

    def test_units_are_converted(self):
        data = clone(base_plan())
        m = data["steps"][1]["measure"][0]
        m["units"], m["low"], m["high"] = "mV", 4900.0, 5100.0
        _, detail, _ = self.run_plan(data)
        self.assertAlmostEqual(all_measurements(detail)[-1]["value"], 5000.0, places=3)
        self.assertEqual(all_measurements(detail)[-1]["verdict"], "pass")

    def test_samples_are_averaged(self):
        data = clone(base_plan())
        data["steps"][1]["measure"][0]["samples"] = 8
        _, detail, bench = self.run_plan(data, noise=1.0)
        last = all_measurements(detail)[-1]
        self.assertEqual(last["samples"], 8)
        self.assertEqual(bench.devices["dmm"].readings_taken, 3 + 8)
        self.assertAlmostEqual(last["value"], 5.0, delta=60e-6)

    def test_nested_sweep_measures_every_combination(self):
        data = clone(base_plan())
        data["instruments"]["load"] = {"driver": "eload"}
        data["setup"][0]["amps"] = 3.0
        data["setup"].append({"do": "load.input_on"})
        data["steps"][0]["sweep"] = [{"var": "vin", "values": [8, 12]}, {"var": "iload", "values": [0, 1]}]
        data["steps"][0]["apply"].append({"do": "load.set_current", "amps": "=iload"})
        _, detail, _ = self.run_plan(data)
        rows = [m for m in step_named(detail, "vout_vs_vin")["measurements"] if m["name"] == "vout"]
        self.assertEqual(
            [tuple(m["point"].values()) for m in rows], [(8.0, 0.0), (8.0, 1.0), (12.0, 0.0), (12.0, 1.0)]
        )
        self.assertAlmostEqual(rows[1]["value"], 5.0 - 0.004 - 0.015, places=7)

    def test_variables_can_be_overridden_for_a_run(self):
        bench = self.start_bench()
        plan = parse_plan(base_plan(), variables={"vnom": 3.3})
        apply_addresses(plan, bench.addresses_for((s.name, s.driver) for s in plan.instruments.values()))
        result = Runner(plan, self.store, "SN").run()
        self.assertEqual(result.status, "fail")
        self.assertEqual(self.store.run_detail(result.run_id)["variables"], {"vnom": 3.3})

    def test_overload_counts_against_the_unit_without_aborting(self):
        data = clone(base_plan())
        data["steps"][1]["measure"][0]["args"] = {"range_v": 1.0}
        result, detail, _ = self.run_plan(data)
        self.assertEqual(result.status, "fail")
        last = all_measurements(detail)[-1]
        self.assertIsNone(last["value"])
        self.assertEqual(last["verdict"], "fail")
        self.assertIn("overload", last["note"])

    def test_overload_on_a_measurement_without_limits_still_fails(self):
        data = clone(base_plan())
        data["steps"][1]["measure"].insert(
            0, {"name": "raw", "read": "dmm.measure_dc_voltage", "args": {"range_v": 1.0}, "units": "V"}
        )
        result, detail, _ = self.run_plan(data)
        self.assertEqual(result.status, "fail")
        self.assertEqual(step_named(detail, "final_check")["measurements"][0]["verdict"], "fail")

    def test_calculation_with_no_answer_fails_the_unit(self):
        data = clone(base_plan())
        data["steps"][1]["measure"].append({"name": "ratio", "calc": "1 / (vout - vout)", "units": "x"})
        result, detail, _ = self.run_plan(data)
        self.assertEqual(result.status, "fail")
        last = all_measurements(detail)[-1]
        self.assertIsNone(last["value"])
        self.assertIn("could not calculate", last["note"])

    def test_calculation_on_a_failed_value_fails_too(self):
        data = clone(base_plan())
        data["steps"][1]["measure"] = [
            {"name": "vout", "read": "dmm.measure_dc_voltage", "args": {"range_v": 1.0}, "units": "V", "low": 4.9},
            {"name": "double", "calc": "vout * 2", "units": "V"},
        ]
        result, detail, _ = self.run_plan(data)
        self.assertEqual(result.status, "fail")
        self.assertIn("could not calculate", all_measurements(detail)[-1]["note"])

    def test_stop_on_fail_skips_later_steps(self):
        data = clone(base_plan())
        data["steps"][0]["stop_on_fail"] = True
        result, detail, _ = self.run_plan(data, dut="high-output")
        self.assertEqual(result.status, "fail")
        final = step_named(detail, "final_check")
        self.assertEqual(final["status"], "skipped")
        self.assertIn("stop_on_fail", final["message"])
        self.assertEqual(step_named(detail, "(teardown)")["status"], "pass")

    def test_stop_on_fail_does_nothing_on_a_pass(self):
        data = clone(base_plan())
        data["steps"][0]["stop_on_fail"] = True
        result, detail, _ = self.run_plan(data)
        self.assertEqual(result.status, "pass")
        self.assertEqual(step_named(detail, "final_check")["status"], "pass")

    def test_settle_and_wait_are_honored(self):
        slept = []
        bench = self.start_bench()
        data = clone(base_plan())
        data["setup"].append({"wait_s": 0.25})
        data["steps"][0]["settle_s"] = 0.5
        plan = self.on_bench(data, bench)
        Runner(plan, self.store, "SN", sleep=slept.append).run()
        self.assertEqual(slept, [0.25, 0.5, 0.5, 0.5])

    def test_expression_in_a_wait(self):
        slept = []
        bench = self.start_bench()
        data = clone(base_plan())
        data["steps"][0]["apply"].append({"wait_s": "=vin / 100"})
        plan = self.on_bench(data, bench)
        Runner(plan, self.store, "SN", sleep=slept.append).run()
        self.assertEqual(slept, [0.08, 0.12, 0.16])

    def test_each_run_is_separate(self):
        self.run_plan(base_plan(), serial="A")
        result, detail, _ = self.run_plan(base_plan(), serial="B")
        self.assertEqual(result.run_id, 2)
        self.assertEqual(detail["serial"], "B")
        self.assertEqual(len(all_measurements(detail)), 5)

    def test_scpi_log(self):
        bench = self.start_bench()
        plan = self.on_bench(base_plan(), bench)
        log = io.StringIO()
        Runner(plan, self.store, "SN", scpi_log=log).run()
        text = log.getvalue()
        self.assertIn("dmm >> *IDN?", text)
        self.assertIn("supply >> OUTP ON", text)
        self.assertIn("dmm >> READ?", text)

    def test_console_shows_failures_readably(self):
        lines = []
        bench = self.start_bench(dut="high-output")
        plan = self.on_bench(base_plan(), bench)
        Runner(plan, self.store, "SN", console=lines.append).run()
        text = "\n".join(lines)
        self.assertIn("FAIL vout_vs_vin.vout at vin=8", text)
        self.assertIn("RESULT: FAIL", text)

    def test_seeded_runs_repeat_exactly(self):
        _, one, _ = self.run_plan(base_plan(), seed=11, noise=1.0)
        _, two, _ = self.run_plan(base_plan(), seed=11, noise=1.0)
        _, other, _ = self.run_plan(base_plan(), seed=12, noise=1.0)
        values = lambda d: [m["value"] for m in all_measurements(d)]  # noqa: E731
        self.assertEqual(values(one), values(two))
        self.assertNotEqual(values(one), values(other))


class Aborts(BenchTestCase):
    def fault_plan(self, timeout_s=0.3):
        data = clone(base_plan())
        for spec in data["instruments"].values():
            spec["timeout_s"] = timeout_s
        return data

    def check_aborted(self, detail, result, text):
        self.assertEqual(result.status, "aborted")
        self.assertEqual(detail["status"], "aborted")
        self.assertIn(text, detail["reason"])
        self.assertIsNotNone(detail["finished_at"])

    def test_lost_connection(self):
        result, detail, bench = self.run_plan(self.fault_plan(), faults={"dmm": Fault("drop", 1)})
        self.assertRegex(detail["reason"], "closed by the instrument|lost")
        self.check_aborted(detail, result, "dmm")
        self.assertIn("step vout_vs_vin", detail["reason"])
        self.assertIn("dmm", detail["reason"])
        step = step_named(detail, "vout_vs_vin")
        self.assertEqual(step["status"], "error")
        self.assertEqual(step["measurements"][0]["verdict"], "pass")
        self.assertEqual(step["measurements"][1]["verdict"], "error")
        self.assertIsNone(step["measurements"][1]["value"])
        self.assertEqual(step_named(detail, "final_check")["status"], "skipped")
        self.assertFalse(bench.model.supply_on, "supply must be turned off")

    def test_hung_instrument(self):
        result, detail, _ = self.run_plan(self.fault_plan(0.1), faults={"dmm": Fault("hang", 1)})
        self.check_aborted(detail, result, "no response")

    def test_instrument_error_during_a_reading(self):
        result, detail, _ = self.run_plan(self.fault_plan(), faults={"dmm": Fault("error", 1)})
        self.check_aborted(detail, result, "-300")
        self.assertEqual(all_measurements(detail)[-1]["verdict"], "error")

    def test_garbage_reading(self):
        result, detail, _ = self.run_plan(self.fault_plan(), faults={"dmm": Fault("garbage", 0)})
        self.check_aborted(detail, result, "non-numeric")

    def test_no_false_pass_from_a_rejected_setting(self):
        """A supply refusing 99 V must stop the run, not measure the old setting."""
        data = self.fault_plan()
        data["steps"][0]["sweep"] = {"var": "vin", "values": [12, 99]}
        result, detail, bench = self.run_plan(data)
        self.check_aborted(detail, result, "-222")
        self.assertIn("supply", detail["reason"])
        self.assertEqual(step_named(detail, "vout_vs_vin")["status"], "error")
        self.assertEqual(len(all_measurements(detail)), 1)
        self.assertFalse(bench.model.supply_on)

    def test_no_false_pass_from_an_unknown_query(self):
        data = self.fault_plan(0.1)
        data["steps"][1]["measure"][0] = {
            "name": "vout",
            "read": "dmm.query_number",
            "args": {"command": "NOT:A:COMMAND?"},
            "units": "V",
            "low": 4.9,
        }
        result, detail, _ = self.run_plan(data)
        self.check_aborted(detail, result, "-113")

    def test_connection_refused_names_the_address(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
        probe.close()
        bench = self.start_bench()
        plan = self.on_bench(base_plan(), bench)
        plan.instruments["dmm"].address = f"127.0.0.1:{dead_port}"
        result = Runner(plan, self.store, "SN").run()
        detail = self.store.run_detail(result.run_id)
        self.check_aborted(detail, result, str(dead_port))
        self.assertEqual(step_named(detail, "(setup)")["status"], "error")
        self.assertEqual(step_named(detail, "vout_vs_vin")["status"], "skipped")
        self.assertEqual(all_measurements(detail), [])
        self.assertFalse(bench.model.supply_on)

    def test_setup_rejection(self):
        data = self.fault_plan()
        data["setup"].insert(0, {"do": "supply.set_current_limit", "amps": 99})
        result, detail, _ = self.run_plan(data)
        self.check_aborted(detail, result, "-222")
        self.assertEqual(step_named(detail, "(setup)")["status"], "error")

    def test_identity_mismatch_stops_before_any_measurement(self):
        data = self.fault_plan()
        data["instruments"]["dmm"]["expect_idn"] = "ACME,DMM-9000"
        result, detail, _ = self.run_plan(data)
        self.check_aborted(detail, result, "does not match")
        self.assertEqual(all_measurements(detail), [])
        recorded = {i["name"]: i for i in detail["instruments"]}
        self.assertEqual(recorded["dmm"]["idn"], "benchrun,SIM-DMM,SIM0001,1.0")

    def test_identity_match_passes(self):
        data = self.fault_plan()
        data["instruments"]["dmm"]["expect_idn"] = "SIM-DMM"
        result, _, _ = self.run_plan(data)
        self.assertEqual(result.status, "pass")

    def test_operator_interrupt_is_recorded_and_goes_safe(self):
        def interrupt(_):
            raise KeyboardInterrupt

        bench = self.start_bench()
        data = clone(base_plan())
        data["steps"][0]["settle_s"] = 0.01
        plan = self.on_bench(data, bench)
        result = Runner(plan, self.store, "SN", sleep=interrupt).run()
        detail = self.store.run_detail(result.run_id)
        self.check_aborted(detail, result, "interrupted")
        self.assertEqual(step_named(detail, "vout_vs_vin")["status"], "error")
        self.assertFalse(bench.model.supply_on)

    def test_internal_error_is_recorded_not_a_verdict(self):
        bench = self.start_bench()
        plan = self.on_bench(base_plan(), bench)
        plan.steps[0].apply[0].args = {"volts": "not a number"}
        result = Runner(plan, self.store, "SN").run()
        detail = self.store.run_detail(result.run_id)
        self.check_aborted(detail, result, "internal error")
        self.assertFalse(bench.model.supply_on)

    def test_failed_teardown_is_never_a_pass(self):
        data = self.fault_plan()
        data["teardown"] = [{"do": "supply.set_voltage", "volts": 99}]
        result, detail, bench = self.run_plan(data)
        self.check_aborted(detail, result, "teardown failed")
        self.assertEqual(step_named(detail, "(teardown)")["status"], "error")
        self.assertFalse(bench.model.supply_on, "safe state still runs after a teardown failure")

    def test_teardown_keeps_the_original_reason(self):
        data = self.fault_plan()
        result, detail, _ = self.run_plan(data, faults={"psu": Fault("drop", 0), "dmm": Fault("drop", 0)})
        self.check_aborted(detail, result, "dmm")

    def test_teardown_problem_on_a_dead_supply_is_recorded(self):
        data = self.fault_plan()
        data["steps"][1]["measure"].append({"name": "iin", "read": "supply.measure_current", "units": "A", "high": 1.0})
        result, detail, _ = self.run_plan(data, faults={"psu": Fault("drop", 0)})
        self.check_aborted(detail, result, "supply")
        self.assertEqual(step_named(detail, "(teardown)")["status"], "error")
        self.assertIn("supply", step_named(detail, "(teardown)")["message"])

    def test_safe_state_runs_even_with_an_empty_teardown(self):
        data = clone(base_plan())
        data["teardown"] = []
        _, _, bench = self.run_plan(data)
        self.assertFalse(bench.model.supply_on)

    def test_connections_are_closed_after_the_run(self):
        bench = self.start_bench()
        plan = self.on_bench(base_plan(), bench)
        runner = Runner(plan, self.store, "SN")
        runner.run()
        for instrument in runner.instruments.values():
            self.assertIsNone(instrument.transport._sock)

    def test_failure_before_the_abort_still_reports_aborted(self):
        result, detail, _ = self.run_plan(self.fault_plan(), dut="high-output", faults={"dmm": Fault("drop", 2)})
        self.check_aborted(detail, result, "dmm")
        self.assertEqual(all_measurements(detail)[0]["verdict"], "fail")


if __name__ == "__main__":
    unittest.main()
