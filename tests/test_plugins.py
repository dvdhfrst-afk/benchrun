"""Driver plugins from installed packages, loaded here through fake entry points."""

import importlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from benchrun import plugins
from benchrun.drivers import DRIVERS
from benchrun.drivers.base import Instrument, action, reading
from benchrun.errors import PlanError
from benchrun.plan import parse_plan
from benchrun.sim.bench import PORT_ORDER, SimBench
from benchrun.sim.instruments import SimInstrument
from tests.test_cli import call

ROOT = Path(__file__).parent.parent
EXAMPLE = ROOT / "examples" / "plugin_example"
EXAMPLE_PLAN = str(EXAMPLE / "heated_regulator.json")
QUICK = str(ROOT / "examples" / "regulator_quick.json")
EXIT_PASS, EXIT_FAIL, EXIT_USAGE, EXIT_ABORTED = 0, 1, 2, 3
READ_ENTRY_POINTS = plugins._entry_points  # The real lookup, before any test replaces it.


class FakeEntryPoint:
    """What ``importlib.metadata`` hands back: a name, a target, and ``load``."""

    def __init__(self, name, target, load):
        self.name = name
        self.value = target
        self._load = load

    def load(self):
        return self._load()


def gives(obj):
    return lambda: obj


def fails(exc):
    def load():
        raise exc

    return load


class Probe(Instrument):
    """A probe with nothing to switch off."""

    COMMANDS = {**Instrument.COMMANDS, "level": "LEV?"}

    @reading("V")
    def level(self) -> float:
        return self.number("level")


class NoWayOff(Instrument):
    """Says it has an output and never says how to switch it off."""

    HAS_OUTPUT = True

    @action
    def on(self) -> None:
        self.command("reset")


class SneakyCommands(Instrument):
    COMMANDS = {**Instrument.COMMANDS, "go": "GO\nOUTP ON"}


class ForgetsBase(Instrument):
    COMMANDS = {"level": "LEV?"}


class WrongKind(SimInstrument):
    kind = "something_else"


def plan_with(driver):
    return {
        "name": "plugin-plan",
        "instruments": {"probe": {"driver": driver, "address": "127.0.0.1:5025"}},
        "steps": [{"name": "s", "measure": [{"name": "v", "read": "probe.level", "units": "V", "high": 1.0}]}],
    }


class PluginCase(unittest.TestCase):
    def setUp(self):
        plugins.forget()
        self.addCleanup(plugins.forget)
        self.groups = {plugins.DRIVER_GROUP: [], plugins.SIMULATOR_GROUP: []}
        patcher = mock.patch.object(plugins, "_entry_points", lambda group: self.groups[group])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.folder = Path(tempfile.mkdtemp(prefix="benchrun-plugins-"))
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.db = self.folder / "r.sqlite"

    def offer(self, name, load, group=plugins.DRIVER_GROUP, target=None):
        self.groups[group].append(FakeEntryPoint(name, target or f"fake_package.module:{name}", load))

    def offer_example(self):
        """The real example package, imported from its folder instead of installed."""
        sys.path.insert(0, str(EXAMPLE))
        self.addCleanup(sys.path.remove, str(EXAMPLE))
        for module in ("benchrun_fixture_heater", "benchrun_fixture_heater.driver", "benchrun_fixture_heater.sim"):
            self.addCleanup(sys.modules.pop, module, None)

        def target(module, name):
            return lambda: getattr(importlib.import_module(module), name)

        self.offer("fixture_heater", target("benchrun_fixture_heater.driver", "FixtureHeater"), target="benchrun_fixture_heater.driver:FixtureHeater")
        self.offer(
            "fixture_heater",
            target("benchrun_fixture_heater.sim", "SimFixtureHeater"),
            plugins.SIMULATOR_GROUP,
            "benchrun_fixture_heater.sim:SimFixtureHeater",
        )

    def state(self):
        code, out, _ = call("drivers", "--plugins", "--json")
        return code, {(p["name"], p["kind"]): p for p in json.loads(out)["plugins"]}


class Loading(PluginCase):
    def test_a_good_driver_is_usable_by_plans_and_listed_with_its_origin(self):
        self.offer("probe", gives(Probe))
        plan = parse_plan(plan_with("probe"))
        self.assertIs(plan.driver_of("probe"), Probe)
        code, out, _ = call("drivers")
        self.assertEqual(code, EXIT_PASS)
        self.assertIn("probe (plugin: fake_package.module:probe): A probe with nothing to switch off.", out)
        self.assertIn("dmm (built-in):", out)
        code, found = self.state()
        self.assertEqual(code, EXIT_PASS)
        self.assertTrue(found[("probe", "driver")]["usable"])

    def test_nothing_installed_is_said_plainly(self):
        code, out, _ = call("drivers", "--plugins")
        self.assertEqual(code, EXIT_PASS)
        self.assertIn("No driver plugins are installed", out)

    def test_entry_points_are_read_on_python_39_and_on_later_versions(self):
        entry = FakeEntryPoint("probe", "x:y", gives(Probe))

        class Selectable:
            def select(self, group):
                return [entry, entry] if group == plugins.DRIVER_GROUP else []

        for shape in ({plugins.DRIVER_GROUP: (entry, entry)}, Selectable()):
            with self.subTest(type(shape).__name__), mock.patch("importlib.metadata.entry_points", return_value=shape):
                self.assertEqual(READ_ENTRY_POINTS(plugins.DRIVER_GROUP), [entry])
                self.assertEqual(READ_ENTRY_POINTS(plugins.SIMULATOR_GROUP), [])

    def test_a_broken_plugin_is_reported_and_refused_and_nothing_else_stops(self):
        self.offer("broken", fails(ImportError("No module named 'missing_dependency'")))
        self.offer("quits", fails(SystemExit(1)))
        self.offer("probe", gives(Probe))
        code, found = self.state()
        self.assertEqual(code, EXIT_ABORTED)
        self.assertIn("cannot be imported: ImportError", found[("broken", "driver")]["problem"])
        self.assertIn("SystemExit", found[("quits", "driver")]["problem"])
        self.assertTrue(found[("probe", "driver")]["usable"])
        with self.assertRaises(PlanError) as caught:
            parse_plan(plan_with("broken"))
        self.assertIn("comes from a plugin (fake_package.module:broken) that cannot be used: it cannot be imported", str(caught.exception))
        code, out, _ = call("run", QUICK, "--serial", "SN-1001", "--sim", "--db", self.db)
        self.assertEqual(code, EXIT_PASS, out)
        code, out, err = call("drivers", "broken")
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("cannot be imported", err)

    def test_what_is_not_a_proper_driver_is_refused_with_the_reason(self):
        for name, obj, expected in (
            ("not_a_class", 42, "it is not a driver"),
            ("not_a_driver", dict, "it is not a driver"),
            ("base_itself", Instrument, "it is not a driver"),
            ("no_way_off", NoWayOff, "must override safe_state"),
            ("sneaky", SneakyCommands, "has a line break"),
            ("forgets", ForgetsBase, "missing: clear, identify"),
            ("Bad-Name", Probe, "the name must be"),
        ):
            self.offer(name, gives(obj))
            plugins.forget()  # Look again, now that there is one more.
            with self.subTest(name):
                code, found = self.state()
                self.assertEqual(code, EXIT_ABORTED)
                self.assertIn(expected, found[(name, "driver")]["problem"])
                self.assertNotIn(name, DRIVERS)

    def test_a_plugin_cannot_replace_a_built_in_driver(self):
        original = DRIVERS["dmm"]
        self.offer("dmm", gives(Probe))
        code, found = self.state()
        self.assertEqual(code, EXIT_ABORTED)
        self.assertIn("built-in driver of that name", found[("dmm", "driver")]["problem"])
        self.assertIs(DRIVERS["dmm"], original)
        self.assertEqual(plugins.origin("dmm"), "built-in")

    def test_two_packages_offering_one_name_are_both_refused(self):
        self.offer("probe", gives(Probe), target="first_package:Probe")
        self.offer("probe", gives(Probe), target="second_package:Probe")
        code, found = self.state()
        self.assertEqual(code, EXIT_ABORTED)
        self.assertEqual(len(found), 2)
        for plugin in found.values():
            self.assertIn("more than one package offers this name", plugin["problem"])
        self.assertNotIn("probe", DRIVERS)
        with self.assertRaises(PlanError):
            parse_plan(plan_with("probe"))

    def test_simulators_must_fit_a_usable_driver(self):
        self.offer("probe", gives(Probe))
        self.offer("orphan", gives(WrongKind), plugins.SIMULATOR_GROUP)
        self.offer("probe", gives(WrongKind), plugins.SIMULATOR_GROUP)
        self.offer("dmm", gives(WrongKind), plugins.SIMULATOR_GROUP)
        code, found = self.state()
        self.assertEqual(code, EXIT_ABORTED)
        self.assertIn("its kind is 'something_else'", found[("probe", "simulator")]["problem"])
        self.assertIn("its kind is", found[("orphan", "simulator")]["problem"])
        self.assertIn("built-in simulator", found[("dmm", "simulator")]["problem"])
        self.assertEqual(plugins.simulators(), {})

    def test_a_simulator_without_its_driver_is_refused(self):
        class Lonely(SimInstrument):
            kind = "lonely"

        self.offer("lonely", gives(Lonely), plugins.SIMULATOR_GROUP)
        code, found = self.state()
        self.assertIn("no usable driver plugin", found[("lonely", "simulator")]["problem"])

    def test_plugins_are_looked_for_once(self):
        calls = []
        self.offer("probe", lambda: calls.append(1) or Probe)
        for _ in range(3):
            parse_plan(plan_with("probe"))
        self.assertEqual(calls, [1])


class ExamplePlugin(PluginCase):
    def setUp(self):
        super().setUp()
        self.offer_example()

    def test_the_example_plan_runs_on_the_plugin_simulator_and_the_heater_goes_off(self):
        log = self.folder / "traffic.scpi.log"
        code, out, _ = call("run", EXAMPLE_PLAN, "--serial", "SN-1001", "--sim", "--db", self.db, "--scpi-log", log)
        self.assertEqual(code, EXIT_PASS, out)
        self.assertIn("benchrun-example,FH-1", out)
        heater = [line for line in log.read_text(encoding="utf-8").splitlines() if " heater " in f" {line} "]
        sent = " ".join(heater)
        self.assertIn("TEMP 70", sent)
        self.assertLess(sent.rindex("OUTP OFF"), sent.rindex("OUTP?"))

    def test_a_fault_can_be_put_on_a_plugin_simulator(self):
        code, out, _ = call("run", EXAMPLE_PLAN, "--serial", "SN-1001", "--sim", "--db", self.db, "--fault", "fixture_heater:garbage:1")
        self.assertEqual(code, EXIT_ABORTED, out)
        self.assertIn("non-numeric response", out)

    def test_the_bench_serves_plugin_simulators_after_the_built_in_ones(self):
        bench = SimBench(base_port=0).start()
        self.addCleanup(bench.stop)
        self.assertEqual(list(bench.devices), list(PORT_ORDER) + ["fixture_heater"])
        self.assertIn("fixture_heater", bench.addresses)

    def test_the_example_plan_uses_the_port_the_plugin_simulator_is_served_on(self):
        ports = {kind: 5025 + index for index, kind in enumerate(list(PORT_ORDER) + ["fixture_heater"])}
        for spec in json.loads(Path(EXAMPLE_PLAN).read_text(encoding="utf-8"))["instruments"].values():
            self.assertEqual(spec["address"], f"127.0.0.1:{ports[spec['driver']]}")

    def test_the_driver_refuses_a_temperature_out_of_range_before_sending_it(self):
        data = json.loads(Path(EXAMPLE_PLAN).read_text(encoding="utf-8"))
        data["setup"][3]["celsius"] = 400.0
        path = self.folder / "too_hot.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        code, out, _ = call("run", path, "--serial", "SN-1001", "--sim", "--db", self.db)
        self.assertEqual(code, EXIT_ABORTED)
        self.assertIn("plate temperature must be from 0 to 125 C", out)

    def test_drivers_describes_the_example(self):
        code, out, _ = call("drivers", "fixture_heater", "--commands")
        self.assertEqual(code, EXIT_PASS)
        self.assertIn("reading  measure_temperature() [degC]", out)
        self.assertIn("command  set_temperature = TEMP {celsius:.6g}", out)


if __name__ == "__main__":
    unittest.main()
