"""Stations: building them from the inventory, station files, checks, and runs on a named station."""

import json
import socket
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from benchrun import inventory
from benchrun.cli import EXIT_ABORTED, EXIT_FAIL, EXIT_PASS, EXIT_USAGE
from benchrun.plan import load_addresses, load_plan
from benchrun.sim.bench import SimBench
from benchrun.stations import bind_plan
from benchrun.store import Store
from tests.test_cli import PLAN, QUICK, call
from tests.test_inventory import InventoryCase

ROOT = Path(__file__).parent.parent
EXAMPLE_STATION = ROOT / "examples" / "station_example.json"


class StationCase(InventoryCase):
    @classmethod
    def setUpClass(cls):
        cls.bench = SimBench(noise=0.0).start()

    @classmethod
    def tearDownClass(cls):
        cls.bench.stop()

    def address(self, kind):
        return self.bench.addresses[kind]

    def quick_station(self, name="LINE-1"):
        """A station for the quick plan: supply and dmm, with inventory records that match the simulators."""
        self.add("PSU-0001", "psu", "--address", self.address("psu"), "--manufacturer", "benchrun", "--model", "SIM-PSU", "--serial-number", "SIM0002")
        self.add("DMM-0001", "dmm", "--address", self.address("dmm"), "--manufacturer", "benchrun", "--model", "SIM-DMM")
        self.ok("station", "create", name, "--description", "Final test", "--location", "Lab 1")
        self.ok("station", "assign", name, "supply", "PSU-0001")
        self.ok("station", "assign", name, "dmm", "DMM-0001")

    def run_quick(self, *extra, serial="SN-1001", plan=QUICK):
        return call("run", plan, "--serial", serial, "--db", self.db, *extra)

    def query(self, sql, *params):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()


class Building(StationCase):
    def test_create_list_show(self):
        self.quick_station()
        out = self.ok("station", "show", "line-1")
        self.assertIn("Station LINE-1: Final test", out)
        self.assertIn("Location: Lab 1", out)
        self.assertIn("PSU-0001", out)
        listing = json.loads(self.ok("station", "list", "--json"))["stations"]
        self.assertEqual([(s["name"], s["roles"], s["runs"]) for s in listing], [("LINE-1", 2, 0)])
        self.assertIn("LINE-1", self.ok("station", "list"))
        data = json.loads(self.ok("station", "show", "LINE-1", "--json"))
        self.assertEqual({r["role"]: r["asset_tag"] for r in data["roles"]}, {"supply": "PSU-0001", "dmm": "DMM-0001"})
        self.assertIn("already exists", self.refused("station", "create", "line-1"))
        self.assertIn("no station named", self.refused("station", "show", "LINE-9"))
        self.assertIn("printable", self.refused("station", "create", "bad\tname"))

    def test_assign_rules(self):
        self.quick_station()
        self.add("PSU-0002", "psu")
        self.assertIn("has no address", self.refused("station", "assign", "LINE-1", "supply", "PSU-0002"))
        self.assertIn("already the supply", self.refused("station", "assign", "LINE-1", "supply2", "PSU-0001"))
        self.assertIn("role '2bad'", self.refused("station", "assign", "LINE-1", "2bad", "DMM-0001"))
        self.assertIn("not a valid regular expression", self.refused("station", "assign", "LINE-1", "dmm", "DMM-0001", "--expect-idn", "("))
        self.assertIn("from 0.001", self.refused("station", "assign", "LINE-1", "dmm", "DMM-0001", "--timeout", "99999"))
        self.assertIn("no instrument", self.refused("station", "assign", "LINE-1", "dmm", "DMM-9999"))
        self.ok("instrument", "retire", "PSU-0002")
        self.assertIn("is retired", self.refused("station", "assign", "LINE-1", "supply", "PSU-0002"))
        self.assertIn("Nothing changed", self.ok("station", "assign", "LINE-1", "dmm", "DMM-0001"))

    def test_reassigning_and_unassigning_are_audited(self):
        self.quick_station()
        self.add("PSU-0002", "psu", "--address", "192.0.2.50", "--status", "in-repair")
        out = self.ok("station", "assign", "LINE-1", "supply", "PSU-0002", "--timeout", "2.5", "--expect-idn", "PSU", "--actor", "Operator A")
        self.assertIn("replacing PSU-0001", out)
        self.assertIn("is in-repair", out)
        entry = self.entries(action="station.assign")[0]
        self.assertEqual(entry["actor"], "Operator A")
        self.assertEqual(entry["detail"]["replaced"], {"asset_tag": "PSU-0001", "timeout_s": None, "expect_idn": None})
        self.assertEqual((entry["detail"]["asset_tag"], entry["detail"]["timeout_s"]), ("PSU-0002", 2.5))
        self.ok("station", "unassign", "LINE-1", "supply")
        self.assertEqual(self.entries(action="station.unassign")[0]["detail"], {"role": "supply", "asset_tag": "PSU-0002"})
        self.assertIn("has no role 'supply'", self.refused("station", "unassign", "LINE-1", "supply"))
        self.assertEqual(len(self.entries(subject="station:LINE-1")), 5)

    def test_rename_keeps_old_runs_under_the_old_name(self):
        self.quick_station()
        self.assertEqual(self.run_quick("--station-name", "LINE-1")[0], EXIT_PASS)
        out = self.ok("station", "rename", "LINE-1", "LINE-2", "--actor", "Operator A")
        self.assertIn("keep the name LINE-1", out)
        self.assertEqual(self.entries(action="station.rename")[0]["subject"], "station:LINE-2")
        self.assertIn("SN-1001", self.ok("history", "--station", "LINE-1"))
        self.assertIn("No matching runs", self.ok("history", "--station", "LINE-2"))
        self.ok("station", "create", "LINE-3")
        self.assertIn("already exists", self.refused("station", "rename", "LINE-2", "line-3"))
        self.ok("station", "rename", "LINE-2", "line-2")  # Only the case changes.
        self.assertIn("line-2", self.ok("station", "list"))

    def test_delete_refuses_while_runs_reference_it_and_keeps_history(self):
        self.quick_station()
        self.assertEqual(self.run_quick("--station-name", "LINE-1")[0], EXIT_PASS)
        self.assertIn("--force", self.refused("station", "delete", "LINE-1"))
        out = self.ok("station", "delete", "LINE-1", "--force", "--actor", "Operator A")
        self.assertIn("1 run recorded on it are kept", out)
        self.assertEqual(self.query("SELECT COUNT(*) FROM station_roles"), [(0,)])
        self.assertEqual(self.query("SELECT station FROM runs"), [("LINE-1",)])
        self.assertIn("SN-1001", self.ok("history", "--station", "LINE-1"))
        self.assertIsNotNone(self.record("PSU-0001"))
        detail = self.entries(action="station.delete")[0]["detail"]
        self.assertEqual((detail["roles"], detail["runs"]), ({"dmm": "DMM-0001", "supply": "PSU-0001"}, 1))
        self.ok("station", "create", "EMPTY")
        self.assertIn("Deleted station EMPTY.", self.ok("station", "delete", "EMPTY"))


class StationFiles(StationCase):
    def test_export_writes_a_file_that_run_station_file_reads(self):
        self.quick_station()
        self.ok("station", "assign", "LINE-1", "dmm", "DMM-0001", "--timeout", "3", "--expect-idn", "SIM-DMM")
        path = self.folder / "line1.json"
        self.ok("station", "export", "LINE-1", "--out", path)
        self.assertEqual(load_addresses(path), {"dmm": self.address("dmm"), "supply": self.address("psu")})
        self.assertIn("--force", self.refused("station", "export", "LINE-1", "--out", path))
        code, out, _ = self.run_quick("--station", path)
        self.assertEqual(code, EXIT_PASS, out)

    def test_export_then_import_into_another_database_is_lossless(self):
        self.quick_station()
        self.ok("station", "assign", "LINE-1", "dmm", "DMM-0001", "--timeout", "3", "--expect-idn", "SIM-DMM")
        path = self.folder / "line1.json"
        self.ok("station", "export", "LINE-1", "--out", path)
        other = self.folder / "other.sqlite"
        code, out, err = call("station", "import", path, "--db", other)
        self.assertEqual(code, EXIT_PASS, out + err)
        self.assertIn("Created station LINE-1 with 2 roles, and added 2 instruments", out)
        code, out, _ = call("station", "show", "LINE-1", "--json", "--db", other)
        data = json.loads(out)
        self.assertEqual((data["station"]["description"], data["station"]["location"]), ("Final test", "Lab 1"))
        dmm = next(r for r in data["roles"] if r["role"] == "dmm")
        self.assertEqual((dmm["asset_tag"], dmm["timeout_s"], dmm["expect_idn"]), ("DMM-0001", 3.0, "SIM-DMM"))
        code, out, _ = call("station", "import", path, "--name", "LINE-2", "--db", other)
        self.assertEqual(code, EXIT_PASS, out)
        self.assertIn("added 0 instruments", out)

    def test_importing_a_1x_file_needs_drivers_for_instrument_names(self):
        code, out, err = self.cmd("station", "import", EXAMPLE_STATION, "--name", "BENCH-A")
        self.assertEqual(code, EXIT_USAGE)
        for role in ("supply", "load", "gen"):
            self.assertIn(f"role {role}: say which driver", err)
        self.assertNotIn("role dmm", err)
        code, out, err = self.cmd("station", "import", EXAMPLE_STATION, "--name", "BENCH-A", "--plan", PLAN, "--dry-run")
        self.assertEqual(code, EXIT_PASS, out + err)
        self.assertIn("Dry run: nothing was changed. It would create the station and add 5 instruments.", out)
        self.assertFalse(self.entries())
        code, out, err = self.cmd(
            "station", "import", EXAMPLE_STATION, "--name", "BENCH-A", "--driver", "supply=psu", "--driver", "load=eload",
            "--driver", "gen=siggen", "--location", "Lab 2", "--actor", "Operator A",
        )  # fmt: skip
        self.assertEqual(code, EXIT_PASS, out + err)
        for tag in ("PSU-0001", "ELOAD-0001", "DMM-0001", "SCOPE-0001", "SIGGEN-0001"):
            self.assertIn(tag, out)
            self.assertEqual(self.record(tag)["location"], "Lab 2")
        self.assertEqual(self.entries(action="station.import")[0]["detail"]["roles"]["gen"], "SIGGEN-0001")
        self.assertEqual(len(self.entries(action="instrument.add", actor="Operator A")), 5)
        self.assertIn("already exists", self.refused("station", "import", EXAMPLE_STATION, "--name", "bench-a", "--plan", PLAN))

    def test_import_reuses_instruments_already_at_an_address_and_numbers_new_tags_on(self):
        self.add("PSU-0007", "psu", "--address", "192.0.2.11")
        self.add("DMM-0001", "dmm", "--address", "192.0.2.99")
        path = self.write("s.json", json.dumps({"addresses": {"supply": "192.0.2.11:5025", "psu": "192.0.2.12", "dmm": "192.0.2.13"}}))
        out = self.ok("station", "import", path, "--name", "S", "--plan", QUICK)
        self.assertIn("PSU-0007", out)
        self.assertIn("PSU-0008", out)
        self.assertIn("DMM-0002", out)
        self.assertEqual(self.record("PSU-0008")["address"], "192.0.2.12:5025")

    def test_import_refuses_files_that_disagree_with_the_inventory(self):
        self.add("PSU-0001", "psu", "--address", "192.0.2.11")
        self.add("OLD-1", "dmm", "--address", "192.0.2.40")
        self.ok("instrument", "retire", "OLD-1")
        section = {"name": "S", "roles": {"supply": {"asset_tag": "PSU-0001", "driver": "psu"}}}
        cases = (
            ({"addresses": {"supply": "192.0.2.12"}, "station": section}, [], "puts PSU-0001 at 192.0.2.12:5025, but the inventory has it at 192.0.2.11:5025"),
            ({"addresses": {"dmm": "192.0.2.11"}}, [], "PSU-0001 at 192.0.2.11:5025 is a psu in the inventory, not a dmm"),
            ({"addresses": {"supply": "192.0.2.11"}, "station": {"roles": {"supply": {"asset_tag": "NEW-1", "driver": "psu"}}}}, [], "the file names NEW-1, but 192.0.2.11:5025 belongs to PSU-0001"),
            ({"addresses": {"dmm": "192.0.2.20", "dmm2": "192.0.2.20:5025"}}, ["--driver", "dmm2=dmm"], "roles dmm and dmm2 have the same address"),
            ({"addresses": {"dmm": "192.0.2.40"}, "station": {"roles": {"dmm": {"asset_tag": "OLD-1"}}}}, [], "OLD-1 is retired"),
            ({"addresses": {"supply": "192.0.2.11"}}, ["--driver", "supply=psu"], "does not name the station"),
        )
        for index, (data, extra, expected) in enumerate(cases):
            with self.subTest(expected):
                path = self.write(f"case{index}.json", json.dumps(data))
                name = [] if "name the station" in expected else ["--name", f"S{index}"]
                out = self.refused("station", "import", path, *extra, *name)
                self.assertIn(expected, out)
                self.assertEqual(out.count("\n  "), 1, out)  # Only that problem.
        self.assertEqual(self.query("SELECT COUNT(*) FROM stations"), [(0,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM instruments"), [(2,)])

    def test_hostile_station_files(self):
        cases = (
            ("notjson.json", "{", "not valid JSON"),
            ("twice.json", '{"addresses": {"dmm": "192.0.2.1", "dmm": "192.0.2.2"}}', "appears twice"),
            ("list.json", '{"addresses": ["192.0.2.1"]}', "needs an object like"),
            ("number.json", '{"addresses": {"dmm": 5025}}', "must be a string"),
            ("badaddress.json", '{"addresses": {"dmm": "192.0.2.1:70000"}}', "bad port"),
            ("empty.json", '{"addresses": {"dmm": "  "}}', "the address is empty"),
            ("control.json", '{"addresses": {"dmm": "192.0.2.1\\u001b[2J"}}', "address must be one line"),
            ("role.json", '{"addresses": {"two words": "192.0.2.1"}}', "role 'two words' must start"),
            ("section.json", '{"addresses": {"dmm": "192.0.2.1"}, "station": {"name": "S", "colour": 1}}', "unknown key station.colour"),
            ("rolekey.json", '{"addresses": {"dmm": "192.0.2.1"}, "station": {"roles": {"dmm": {"tag": "X"}}}}', "unknown key station.roles.dmm.tag"),
            ("mismatch.json", '{"addresses": {"dmm": "192.0.2.1"}, "station": {"roles": {}}}', "same roles as addresses"),
            ("timeout.json", '{"addresses": {"dmm": "192.0.2.1"}, "station": {"roles": {"dmm": {"timeout_s": "slow"}}}}', "timeout 'slow'"),
            ("pattern.json", '{"addresses": {"dmm": "192.0.2.1"}, "station": {"roles": {"dmm": {"expect_idn": "("}}}}', "not a valid regular expression"),
            ("driver.json", '{"addresses": {"dmm": "192.0.2.1"}, "station": {"roles": {"dmm": {"driver": "toaster"}}}}', "unknown driver"),
            ("tag.json", '{"addresses": {"dmm": "192.0.2.1"}, "station": {"roles": {"dmm": {"asset_tag": " X"}}}}', "printable"),
            ("name.json", '{"addresses": {"dmm": "192.0.2.1"}, "station": {"name": 12}}', "station name 12"),
            ("deep.json", '{"addresses": {"dmm": ' + "[" * 100000 + "]" * 100000 + "}}", None),
            ("latin1.json", '{"addresses": {"dmm": "caf\xe9"}}'.encode("latin-1"), "not UTF-8"),
        )
        for name, text, expected in cases:
            with self.subTest(name):
                given = [] if name == "name.json" else ["--name", "S"]  # --name would replace the bad one.
                out = self.refused("station", "import", self.write(name, text), *given)
                if expected:
                    self.assertIn(expected, out)
        with mock.patch.object(inventory, "MAX_IMPORT_BYTES", 5):
            self.assertIn("too big", self.refused("station", "import", self.folder / "twice.json", "--name", "S"))
        self.assertIn("has no role 'gen'", self.refused("station", "import", self.write("ok.json", '{"addresses": {"dmm": "192.0.2.1"}}'), "--name", "S", "--driver", "gen=siggen"))
        self.assertIn("cannot read station file", self.refused("station", "import", self.folder / "absent.json", "--name", "S"))
        self.assertFalse(self.entries())

    def test_ignored_top_level_keys_are_pointed_out(self):
        path = self.write("s.json", '{"addresses": {"dmm": "192.0.2.1"}, "comment": "bench A"}')
        self.assertIn("Note: ignored keys in", self.ok("station", "import", path, "--name", "S"))


class Checking(StationCase):
    def test_check_passes_when_everything_matches(self):
        self.quick_station()
        out = self.ok("station", "check", "LINE-1")
        self.assertIn("All 2 instruments on station LINE-1 answered and match the inventory.", out)
        data = json.loads(self.ok("station", "check", "LINE-1", "--json"))
        self.assertEqual([r["problems"] for r in data["instruments"]], [[], []])

    def test_check_reports_identity_that_differs_from_the_inventory(self):
        self.quick_station()
        self.ok("instrument", "edit", "PSU-0001", "--serial-number", "SN-OTHER")
        self.ok("station", "assign", "LINE-1", "dmm", "DMM-0001", "--expect-idn", "^Maker B")
        self.ok("instrument", "edit", "DMM-0001", "--status", "in-repair")
        code, out, _ = self.cmd("station", "check", "LINE-1")
        self.assertEqual(code, EXIT_ABORTED, out)
        self.assertIn("inventory serial number is 'SN-OTHER', *IDN? says 'SIM0002'", out)
        self.assertIn("*IDN? does not match '^Maker B'", out)
        self.assertIn("inventory status is in-repair", out)
        self.assertIn("2 instruments of 2 on station LINE-1 need attention", out)

    def test_check_reports_an_instrument_that_does_not_answer(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.close()  # Nothing listens there now.
        self.add("DMM-0009", "dmm", "--address", f"127.0.0.1:{port}")
        self.ok("station", "create", "DEAD")
        self.ok("station", "assign", "DEAD", "dmm", "DMM-0009", "--timeout", "0.5")
        code, out, _ = self.cmd("station", "check", "DEAD")
        self.assertEqual(code, EXIT_ABORTED)
        self.assertIn("cannot connect", out)

    def test_check_changes_no_setting(self):
        self.quick_station()
        call("scpi", self.address("psu"), "VOLT 7.25")
        self.ok("station", "check", "LINE-1")
        code, out, _ = call("scpi", self.address("psu"), "VOLT?")
        self.assertEqual(float(out), 7.25)
        self.assertEqual(self.query("SELECT COUNT(*) FROM audit_log WHERE action NOT IN ('instrument.add', 'station.create', 'station.assign', 'instrument.edit')"), [(0,)])

    def test_check_needs_instruments(self):
        self.ok("station", "create", "EMPTY")
        self.assertIn("no instruments to check", self.refused("station", "check", "EMPTY"))


class RunsOnStations(StationCase):
    def test_run_records_the_station_and_asset_tags(self):
        self.quick_station()
        code, out, err = self.run_quick("--station-name", "line-1", "--report-dir", self.folder / "reports")
        self.assertEqual(code, EXIT_PASS, out + err)
        rows = self.query("SELECT r.station, i.name, i.asset_tag, i.address FROM runs r JOIN run_instruments i ON i.run_id = r.id ORDER BY i.id")
        self.assertEqual(rows, [("LINE-1", "supply", "PSU-0001", self.address("psu")), ("LINE-1", "dmm", "DMM-0001", self.address("dmm"))])
        shown = self.ok("show", "--latest")
        self.assertIn("Station: LINE-1", shown)
        self.assertIn("PSU-0001", shown)
        detail = json.loads(self.ok("show", "--latest", "--json"))
        self.assertEqual((detail["station"], [i["asset_tag"] for i in detail["instruments"]]), ("LINE-1", ["PSU-0001", "DMM-0001"]))
        html = next((self.folder / "reports").glob("*.html")).read_text(encoding="utf-8")
        self.assertIn("<th>Asset tag</th>", html)
        self.assertIn("<td>DMM-0001</td>", html)
        self.assertIn("<td>LINE-1</td>", html)
        out = self.ok("instrument", "show", "DMM-0001")
        self.assertIn("Stations: LINE-1 as dmm", out)
        self.assertIn("SN-1001", out)
        runs = json.loads(self.ok("instrument", "show", "PSU-0001", "--json"))["runs"]
        self.assertEqual([(r["serial"], r["role"], r["station"]) for r in runs], [("SN-1001", "supply", "LINE-1")])

    def test_history_filters_by_station(self):
        self.quick_station()
        self.assertEqual(self.run_quick("--station-name", "LINE-1", serial="SN-1")[0], EXIT_PASS)
        self.assertEqual(call("run", QUICK, "--serial", "SN-2", "--sim", "--db", self.db)[0], EXIT_PASS)
        runs = json.loads(self.ok("history", "--station", "LINE-1", "--json"))["runs"]
        self.assertEqual([r["serial"] for r in runs], ["SN-1"])
        self.assertEqual(self.query("SELECT serial, station FROM runs ORDER BY id"), [("SN-1", "LINE-1"), ("SN-2", "")])

    def test_a_failing_unit_on_a_station_still_fails(self):
        bench = SimBench(noise=0.0, dut="high-output").start()
        self.addCleanup(bench.stop)
        self.add("PSU-0001", "psu", "--address", bench.addresses["psu"])
        self.add("DMM-0001", "dmm", "--address", bench.addresses["dmm"])
        self.ok("station", "create", "BAD")
        self.ok("station", "assign", "BAD", "psu", "PSU-0001")
        self.ok("station", "assign", "BAD", "dmm", "DMM-0001")
        self.assertEqual(self.run_quick("--station-name", "BAD")[0], EXIT_FAIL)

    def refused_run(self, *extra, plan=QUICK):
        code, out, err = self.run_quick(*extra, plan=plan)
        self.assertEqual(code, EXIT_USAGE, out + err)
        self.assertNotIn("Run ", out)  # Nothing was recorded or commanded.
        return err

    def test_plain_errors_before_anything_is_commanded(self):
        self.quick_station()
        self.ok("station", "unassign", "LINE-1", "dmm")
        self.assertIn("has no role for instrument 'dmm'", self.refused_run("--station-name", "LINE-1"))
        self.ok("station", "assign", "LINE-1", "dmm", "DMM-0001")
        self.ok("instrument", "edit", "DMM-0001", "--status", "in-repair")
        self.assertIn("dmm: DMM-0001 is in-repair, so it cannot be used", self.refused_run("--station-name", "LINE-1"))
        self.ok("instrument", "reactivate", "DMM-0001")
        self.ok("instrument", "retire", "PSU-0001")
        self.assertIn("PSU-0001 is retired", self.refused_run("--station-name", "LINE-1"))
        self.ok("instrument", "reactivate", "PSU-0001")
        self.ok("instrument", "edit", "PSU-0001", "--driver", "smu")
        self.assertIn("the plan uses the psu driver, but PSU-0001 (role supply) is a smu", self.refused_run("--station-name", "LINE-1"))
        self.ok("instrument", "edit", "PSU-0001", "--driver", "psu", "--address", "")
        self.assertIn("PSU-0001 has no address", self.refused_run("--station-name", "LINE-1"))
        self.assertIn("no station named", self.refused_run("--station-name", "LINE-9"))
        self.assertEqual(self.query("SELECT COUNT(*) FROM runs"), [(0,)])

    def test_station_name_cannot_be_combined_with_other_address_sources(self):
        self.quick_station()
        self.assertIn("cannot be combined", self.refused_run("--station-name", "LINE-1", "--sim"))
        self.assertIn("leave out --station", self.refused_run("--station-name", "LINE-1", "--station", EXAMPLE_STATION))
        self.assertIn("leave out --station", self.refused_run("--station-name", "LINE-1", "--address", "dmm=192.0.2.1"))
        code, _, err = call("run", QUICK, "--serial", "SN-1", "--station-name", "LINE-1", "--db", self.folder / "absent.sqlite")
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("no results database", err)
        self.assertFalse((self.folder / "absent.sqlite").exists())

    def test_one_instrument_cannot_fill_two_plan_instruments(self):
        self.add("DMM-0001", "dmm", "--address", self.address("dmm"))
        self.ok("station", "create", "S")
        self.ok("station", "assign", "S", "dmm", "DMM-0001")
        plan = self.write("two.json", json.dumps({
            "name": "two", "instruments": {"dmm_a": {"driver": "dmm"}, "dmm_b": {"driver": "dmm"}},
            "steps": [{"name": "s", "measure": [{"name": "v", "read": "dmm_a.measure_dc_voltage", "high": 100}]}],
        }))  # fmt: skip
        self.assertIn("dmm_b and dmm_a would both use DMM-0001", self.refused_run("--station-name", "S", plan=plan))

    def test_expected_identity_from_the_station_is_enforced(self):
        self.quick_station()
        self.ok("station", "assign", "LINE-1", "dmm", "DMM-0001", "--expect-idn", "^Maker B")
        code, out, _ = self.run_quick("--station-name", "LINE-1")
        self.assertEqual(code, EXIT_ABORTED)
        self.assertIn("which does not match '^Maker B'", out)

    def test_bind_plan_takes_timeouts_and_patterns_from_the_station_when_set(self):
        self.quick_station()
        self.ok("station", "assign", "LINE-1", "dmm", "DMM-0001", "--timeout", "1.5", "--expect-idn", "SIM-DMM")
        self.ok("station", "create", "OTHER")
        self.ok("station", "assign", "OTHER", "dmm", "DMM-0001")
        self.ok("station", "assign", "OTHER", "psu", "PSU-0001")
        with Store(self.db) as store:
            plan = load_plan(QUICK)
            plan.instruments["supply"].expect_idn = "SIM-PSU"
            name, tags, unused = bind_plan(store.conn, "LINE-1", plan)
            self.assertEqual((name, tags, unused), ("LINE-1", {"supply": "PSU-0001", "dmm": "DMM-0001"}, []))
            self.assertEqual((plan.instruments["dmm"].timeout_s, plan.instruments["dmm"].expect_idn), (1.5, "SIM-DMM"))
            self.assertEqual((plan.instruments["supply"].timeout_s, plan.instruments["supply"].expect_idn), (5.0, "SIM-PSU"))
            plan = load_plan(QUICK)
            self.assertEqual(bind_plan(store.conn, "OTHER", plan)[1], {"supply": "PSU-0001", "dmm": "DMM-0001"})

    def test_validate_and_probe_with_a_station(self):
        self.quick_station()
        code, out, err = call("validate", QUICK, "--station-name", "LINE-1", "--db", self.db)
        self.assertEqual(code, EXIT_PASS, out + err)
        self.assertIn(f"Station LINE-1: supply=PSU-0001 ({self.address('psu')}), dmm=DMM-0001", out)
        code, out, err = call("probe", QUICK, "--station-name", "LINE-1", "--db", self.db)
        self.assertEqual(code, EXIT_PASS, out + err)
        self.assertIn("asset tag", out)
        self.assertIn("PSU-0001", out)
        self.ok("instrument", "retire", "DMM-0001")
        code, _, err = call("validate", QUICK, "--station-name", "LINE-1", "--db", self.db)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("DMM-0001 is retired", err)
        code, _, err = call("probe", QUICK, "--station-name", "LINE-1", "--db", self.db)
        self.assertEqual(code, EXIT_USAGE)

    def test_unused_roles_are_pointed_out(self):
        self.quick_station()
        self.add("LOAD-0001", "eload", "--address", self.address("eload"))
        self.ok("station", "assign", "LINE-1", "load", "LOAD-0001")
        code, out, _ = self.run_quick("--station-name", "LINE-1")
        self.assertEqual(code, EXIT_PASS)
        self.assertIn("roles on station LINE-1 not used by this plan: load", out)


if __name__ == "__main__":
    unittest.main()
