"""The instrument inventory commands, their import and export files, and the audit command."""

import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from benchrun import audit, inventory
from benchrun.cli import EXIT_PASS, EXIT_USAGE, build_parser
from tests.test_cli import call


class InventoryCase(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="benchrun-inventory-"))
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.db = self.folder / "r.sqlite"

    def cmd(self, *argv):
        return call(*argv, "--db", self.db)

    def ok(self, *argv):
        code, out, err = self.cmd(*argv)
        self.assertEqual(code, EXIT_PASS, out + err)
        return out

    def refused(self, *argv):
        code, out, err = self.cmd(*argv)
        self.assertEqual(code, EXIT_USAGE, out + err)
        self.assertNotIn(", line ", err)  # No stack dump.
        return out + err

    def add(self, tag, driver="psu", *extra):
        return self.ok("instrument", "add", tag, "--driver", driver, *extra)

    def record(self, tag):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM instruments WHERE asset_tag = ?", (tag,)).fetchone()
            return None if row is None else dict(row)
        finally:
            conn.close()

    def entries(self, **filters):
        if not self.db.exists():
            return []
        conn = sqlite3.connect(self.db)
        try:
            return audit.entries(conn, **filters)
        finally:
            conn.close()

    def write(self, name, text, encoding="utf-8"):
        path = self.folder / name
        path.write_bytes(text.encode(encoding) if isinstance(text, str) else text)
        return path


class AddAndList(InventoryCase):
    def test_add_records_everything_and_one_audit_entry(self):
        out = self.add(
            "PSU-0001", "psu", "--manufacturer", "Maker A", "--model", "PS-100", "--serial-number", "SN-1001",
            "--address", "192.0.2.11", "--location", "Lab 1", "--notes", "spare fan", "--actor", "Operator A",
        )  # fmt: skip
        self.assertIn("Added PSU-0001: psu at 192.0.2.11:5025, active", out)
        row = self.record("PSU-0001")
        self.assertEqual(
            {k: row[k] for k in inventory.FIELDS},
            {
                "asset_tag": "PSU-0001", "driver": "psu", "manufacturer": "Maker A", "model": "PS-100",
                "serial_number": "SN-1001", "address": "192.0.2.11:5025", "location": "Lab 1", "status": "active",
                "notes": "spare fan",
            },
        )  # fmt: skip
        [entry] = self.entries()
        self.assertEqual((entry["action"], entry["subject"], entry["actor"]), ("add", "instrument:PSU-0001", "Operator A"))
        self.assertEqual(entry["detail"]["model"], "PS-100")

    def test_addresses_are_checked_like_plan_addresses_and_written_in_full(self):
        self.add("A-1", "dmm", "--address", "[2001:db8::5]:6000")
        self.assertEqual(self.record("A-1")["address"], "[2001:db8::5]:6000")
        for bad in ("host:0", "host:99999", "host:abc", "[::1", "a:b:c"):
            with self.subTest(bad):
                self.assertIn("address", self.refused("instrument", "add", "B-1", "--driver", "dmm", "--address", bad))
        self.assertIsNone(self.record("B-1"))

    def test_tags_must_be_short_printable_and_unique_ignoring_case(self):
        self.add("PSU-0001")
        self.assertIn("already in the inventory", self.refused("instrument", "add", "psu-0001", "--driver", "psu"))
        for bad in ("x" * 65, " PSU", "PSU ", "PSU\x07", "PSU\n2", ""):
            with self.subTest(repr(bad)):
                self.assertIn("printable", self.refused("instrument", "add", bad, "--driver", "psu"))
        self.add("x" * 64)
        self.assertEqual(len(self.entries()), 2)

    def test_unknown_driver_and_bad_text_are_refused(self):
        self.assertIn("unknown driver", self.refused("instrument", "add", "X-1", "--driver", "toaster"))
        self.assertIn("model must be one line", self.refused("instrument", "add", "X-1", "--driver", "psu", "--model", "a\nb"))
        self.assertIn("--actor", self.refused("instrument", "add", "X-1", "--driver", "psu", "--actor", "a\x1bb"))
        self.assertFalse(self.entries())

    def test_an_address_in_use_is_refused_but_a_retired_one_is_free(self):
        self.add("PSU-0001", "psu", "--address", "192.0.2.11:5025")
        self.assertIn("already belongs to PSU-0001", self.refused("instrument", "add", "PSU-0002", "--driver", "psu", "--address", "192.0.2.11"))
        self.ok("instrument", "retire", "PSU-0001")
        self.add("PSU-0002", "psu", "--address", "192.0.2.11")
        self.assertIn("already belongs to PSU-0002", self.refused("instrument", "reactivate", "PSU-0001"))

    def test_list_filters_and_json(self):
        self.add("PSU-0001", "psu", "--location", "Lab 1", "--model", "PS-100")
        self.add("PSU-0002", "psu", "--location", "Lab 2", "--status", "in-repair")
        self.add("DMM-0001", "dmm", "--location", "lab 1", "--notes", "Front panel scratched")
        out = self.ok("instrument", "list")
        self.assertIn("3 instruments", out)
        self.assertLess(out.index("DMM-0001"), out.index("PSU-0001"))

        def tags(*filters):
            return [row["asset_tag"] for row in json.loads(self.ok("instrument", "list", "--json", *filters))["instruments"]]

        self.assertEqual(tags("--driver", "psu"), ["PSU-0001", "PSU-0002"])
        self.assertEqual(tags("--status", "in-repair"), ["PSU-0002"])
        self.assertEqual(tags("--location", "LAB 1"), ["DMM-0001", "PSU-0001"])
        self.assertEqual(tags("--search", "scratch"), ["DMM-0001"])
        self.assertEqual(tags("--search", "ps-1"), ["PSU-0001"])
        self.assertEqual(tags("--driver", "psu", "--location", "lab 2"), ["PSU-0002"])
        self.assertIn("No matching instruments.", self.ok("instrument", "list", "--search", "nothing"))
        self.assertIn("unknown driver", self.refused("instrument", "list", "--driver", "toaster"))

    def test_reading_commands_need_an_existing_database(self):
        for argv in (("instrument", "list"), ("instrument", "show", "X"), ("instrument", "export", str(self.folder / "x.csv")), ("audit",)):
            with self.subTest(argv):
                self.assertIn("no results database", self.refused(*argv))
        self.assertFalse(self.db.exists())

    def test_show(self):
        self.add("PSU-0001", "psu", "--model", "PS-100", "--address", "192.0.2.11")
        out = self.ok("instrument", "show", "psu-0001")
        self.assertIn("PSU-0001: psu, active", out)
        self.assertIn("Model: PS-100", out)
        self.assertIn("Stations: none", out)
        self.assertIn("No recorded runs used it.", out)
        data = json.loads(self.ok("instrument", "show", "PSU-0001", "--json"))
        self.assertEqual((data["instrument"]["address"], data["stations"], data["runs"]), ("192.0.2.11:5025", [], []))
        self.assertIn("no instrument with asset tag", self.refused("instrument", "show", "PSU-9999"))


class EditRetireReactivate(InventoryCase):
    def setUp(self):
        super().setUp()
        self.add("PSU-0001", "psu", "--model", "PS-100", "--location", "Lab 1")

    def test_edit_records_old_and_new_values(self):
        out = self.ok("instrument", "edit", "psu-0001", "--model", "PS-200", "--location", "", "--actor", "Operator B")
        self.assertIn("model 'PS-100' -> 'PS-200'", out)
        row = self.record("PSU-0001")
        self.assertEqual((row["model"], row["location"]), ("PS-200", ""))
        entry = self.entries(action="edit")[0]
        self.assertEqual(entry["detail"], {"model": ["PS-100", "PS-200"], "location": ["Lab 1", ""]})
        self.assertEqual(entry["actor"], "Operator B")

    def test_edit_with_the_same_values_writes_nothing(self):
        self.assertIn("Nothing changed", self.ok("instrument", "edit", "PSU-0001", "--model", "PS-100"))
        self.assertFalse(self.entries(action="edit"))

    def test_edit_needs_something_to_change_and_valid_values(self):
        self.assertIn("nothing to change", self.refused("instrument", "edit", "PSU-0001"))
        self.assertIn("unknown driver", self.refused("instrument", "edit", "PSU-0001", "--driver", "nope"))
        self.assertIn("no instrument", self.refused("instrument", "edit", "NOPE-1", "--model", "x"))
        self.add("PSU-0002", "psu", "--address", "192.0.2.20")
        self.assertIn("already belongs", self.refused("instrument", "edit", "PSU-0001", "--address", "192.0.2.20:5025"))
        code, _, _ = self.cmd("instrument", "edit", "PSU-0001", "--status", "retired")
        self.assertEqual(code, EXIT_USAGE)  # argparse: retiring has its own command.

    def test_retire_and_reactivate(self):
        out = self.ok("instrument", "retire", "PSU-0001", "--reason", "Fan noise", "--actor", "Operator A")
        self.assertIn("PSU-0001 is now retired (was active)", out)
        self.assertIn("is already retired", self.ok("instrument", "retire", "PSU-0001"))
        self.assertIn("is retired", self.refused("instrument", "edit", "PSU-0001", "--status", "active"))
        self.ok("instrument", "edit", "PSU-0001", "--notes", "kept for parts")
        self.ok("instrument", "reactivate", "PSU-0001")
        self.ok("instrument", "edit", "PSU-0001", "--status", "in-repair")
        self.ok("instrument", "reactivate", "PSU-0001", "--reason", "repaired")
        self.assertEqual(self.record("PSU-0001")["status"], "active")
        actions = [(e["action"], e["detail"].get("status"), e["detail"].get("reason")) for e in reversed(self.entries())]
        self.assertEqual(
            actions,
            [
                ("add", "active", None),
                ("retire", ["active", "retired"], "Fan noise"),
                ("edit", None, None),
                ("reactivate", ["retired", "active"], None),
                ("edit", ["active", "in-repair"], None),
                ("reactivate", ["in-repair", "active"], "repaired"),
            ],
        )


class ImportFiles(InventoryCase):
    HEADER = "asset_tag,driver,manufacturer,model,serial_number,address,location,status,notes\n"

    def import_file(self, path, *extra):
        return self.cmd("instrument", "import", path, *extra)

    def test_csv_adds_changes_and_leaves_alone(self):
        self.add("PSU-0001", "psu", "--model", "PS-100")
        self.add("DMM-0001", "dmm", "--model", "DM-1")
        path = self.write(
            "inv.csv",
            self.HEADER
            + "psu-0001,,,PS-200,,192.0.2.11,,,\n"
            + "DMM-0001,dmm,,DM-1,,,,active,\n"
            + "\n"
            + "LOAD-0001,eload,Maker A,EL-1,SN-1001,192.0.2.12,Lab 1,in-repair,new\n",
        )
        code, out, _ = self.import_file(path, "--dry-run")
        self.assertEqual(code, EXIT_PASS, out)
        self.assertIn("Would add", out)
        self.assertIn("1 would be added, 1 would change, 1 unchanged, 0 refused", out)
        self.assertEqual(self.record("PSU-0001")["model"], "PS-100")
        self.assertIsNone(self.record("LOAD-0001"))
        self.assertEqual(len(self.entries()), 2)

        code, out, _ = self.import_file(path, "--actor", "Operator A")
        self.assertEqual(code, EXIT_PASS, out)
        self.assertIn("Added      LOAD-0001: eload at 192.0.2.12:5025", out)
        self.assertIn("Changed    PSU-0001: model 'PS-100' -> 'PS-200', address '' -> '192.0.2.11:5025'", out)
        self.assertEqual(self.record("PSU-0001")["driver"], "psu")  # An empty driver cell keeps it.
        self.assertEqual(self.record("LOAD-0001")["status"], "in-repair")
        new = self.entries(actor="Operator A")
        self.assertEqual(sorted((e["action"], e["subject"]) for e in new), [("add", "instrument:LOAD-0001"), ("edit", "instrument:PSU-0001")])
        self.assertTrue(all(e["detail"]["file"] == "inv.csv" for e in new))
        code, out, _ = self.import_file(path)
        self.assertIn("0 added, 0 changed, 3 unchanged", out)

    def test_one_bad_row_means_nothing_is_imported(self):
        path = self.write(
            "inv.csv",
            self.HEADER
            + "PSU-0001,psu,,,,,,,\n"
            + "PSU-0002,toaster,,,,,,,\n"
            + "PSU-0003,psu,,,,,,broken,\n"
            + "PSU-0004,,,,,,,,\n"
            + "PSU-0005,psu,,,,not:an:address,,,\n"
            + ",psu,,,,,,,\n"
            + "PSU-0006,psu\n",
        )
        code, out, _ = self.import_file(path)
        self.assertEqual(code, EXIT_USAGE)
        for text in ("line 3: unknown driver 'toaster'", "line 4: status 'broken'", "line 5: PSU-0004 is new, so it needs a driver",
                     "line 6: address", "line 7: asset tag ''", "line 8: has 2 cells", "Would add  PSU-0001", "6 refused",
                     "Nothing was imported"):  # fmt: skip
            self.assertIn(text, out)
        self.assertFalse(self.entries())
        self.assertIsNone(self.record("PSU-0001"))

    def test_duplicate_tags_in_one_file_are_refused(self):
        path = self.write("inv.csv", "asset_tag,driver\nPSU-0001,psu\nDMM-0001,dmm\npsu-0001,psu\n")
        code, out, _ = self.import_file(path)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("line 2: asset tag PSU-0001 appears more than once in this file (also at line 4)", out)
        self.assertIn("line 4: asset tag psu-0001 appears more than once in this file (also at line 2)", out)
        self.assertIsNone(self.record("DMM-0001"))

    def test_two_instruments_may_not_end_up_at_one_address(self):
        self.add("PSU-0001", "psu", "--address", "192.0.2.11")
        path = self.write("inv.csv", "asset_tag,driver,address\nPSU-0002,psu,192.0.2.11\nDMM-0001,dmm,192.0.2.30\nDMM-0002,dmm,192.0.2.30:5025\n")
        code, out, _ = self.import_file(path)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("address 192.0.2.11:5025 would also belong to psu-0001", out)
        self.assertIn("would also belong to dmm-0002", out)
        # Moving the old one away in the same file frees the address.
        path = self.write("move.csv", "asset_tag,driver,address\nPSU-0001,psu,192.0.2.12\nPSU-0002,psu,192.0.2.11\n")
        code, out, _ = self.import_file(path)
        self.assertEqual(code, EXIT_PASS, out)

    def test_bad_headers_are_refused_before_any_row(self):
        for name, text, expected in (
            ("unknown.csv", "asset_tag,driver,colour\nA,psu,red\n", "unknown column 'colour'"),
            ("repeat.csv", "asset_tag,driver,driver\nA,psu,psu\n", "column 'driver' appears more than once"),
            ("notag.csv", "driver,model\npsu,x\n", "needs an asset_tag column"),
            ("empty.csv", "", "is empty"),
            ("header.csv", "asset_tag,driver\n", "lists no instruments"),
        ):
            with self.subTest(name):
                code, out, err = self.import_file(self.write(name, text))
                self.assertEqual(code, EXIT_USAGE)
                self.assertIn(expected, err)

    def test_hostile_csv_files(self):
        cases = (
            ("nul.csv", b"asset_tag,driver\nA\x00,psu\n", None),  # Older Pythons stop at the NUL; newer ones refuse the tag.
            ("latin1.csv", "asset_tag,driver,notes\nA,psu,caf\xe9\n".encode("latin-1"), "not UTF-8"),
            ("quote.csv", b'asset_tag,driver\n"A,psu\n', None),
            ("control.csv", b"asset_tag,driver,notes\nA,psu,\x1b[2J\n", "notes must be one line"),
            ("newline.csv", b'asset_tag,driver,model\nA,psu,"two\nlines"\n', "model must be one line"),
        )
        for name, data, expected in cases:
            with self.subTest(name):
                code, out, err = self.import_file(self.write(name, data))
                self.assertEqual(code, EXIT_USAGE, out + err)
                self.assertNotIn(", line ", err)  # No stack dump.
                if expected:
                    self.assertIn(expected, out + err)
        self.assertFalse(self.entries())

    def test_large_files_are_refused(self):
        path = self.write("big.csv", "asset_tag,driver\n")
        with mock.patch.object(inventory, "MAX_IMPORT_BYTES", 10):
            code, _, err = self.import_file(path)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("too big", err)
        path = self.write("many.csv", "asset_tag,driver\n" + "".join(f"T-{i},psu\n" for i in range(4)))
        with mock.patch.object(inventory, "MAX_IMPORT_ROWS", 3):
            code, _, err = self.import_file(path)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("more than 3 rows", err)

    def test_files_with_a_byte_order_mark_are_read(self):
        for encoding in ("utf-8-sig", "utf-16"):
            with self.subTest(encoding):
                path = self.write(f"{encoding}.csv", "asset_tag,driver\nBOM-1,psu\n", encoding)
                code, out, err = self.import_file(path)
                self.assertEqual(code, EXIT_PASS, out + err)

    def test_json_import(self):
        path = self.write("inv.json", json.dumps({"instruments": [{"asset_tag": "DMM-0001", "driver": "dmm", "address": "192.0.2.13"}]}))
        code, out, _ = self.import_file(path)
        self.assertEqual(code, EXIT_PASS, out)
        self.assertEqual(self.record("DMM-0001")["address"], "192.0.2.13:5025")
        path = self.write("data.txt", json.dumps({"instruments": [{"asset_tag": "DMM-0002", "driver": "dmm"}]}))
        self.assertIn("--format", self.import_file(path)[2])
        self.assertEqual(self.import_file(path, "--format", "json")[0], EXIT_PASS)

    def test_hostile_json_files(self):
        cases = (
            ("list.json", "[]", "must hold one object"),
            ("extra.json", '{"instruments": [], "more": 1}', "must hold one object"),
            ("twice.json", '{"instruments": [{"asset_tag": "A", "asset_tag": "B", "driver": "psu"}]}', "appears twice"),
            ("nan.json", '{"instruments": [{"asset_tag": "A", "driver": "psu", "notes": NaN}]}', "not valid JSON"),
            ("deep.json", '{"instruments": ' + "[" * 100000 + "]" * 100000 + "}", None),
            ("types.json", '{"instruments": [{"asset_tag": 7, "driver": "psu"}, {"asset_tag": "B", "driver": "psu", "model": 5}, "x", {"asset_tag": "C", "driver": "psu", "colour": "red"}, {"asset_tag": "D", "driver": "psu", "notes": null}]}', None),
            ("broken.json", '{"instruments": [{"asset_tag": "\\ud800", "driver": "psu"}]}', None),
            ("garbage.json", "\x00\x01not json", "not valid JSON"),
        )
        for name, text, expected in cases:
            with self.subTest(name):
                code, out, err = self.import_file(self.write(name, text))
                self.assertEqual(code, EXIT_USAGE, out + err)
                self.assertNotIn(", line ", err)  # No stack dump.
                if expected:
                    self.assertIn(expected, out + err)
        code, out, err = self.import_file(self.folder / "types.json")
        for text in ("instruments[0]: asset tag 7", "instruments[1]: model must be text", "instruments[2]: must be an object",
                     "instruments[3]: unknown key 'colour'", "instruments[4]: notes must be text"):  # fmt: skip
            self.assertIn(text, out)
        self.assertFalse(self.entries())

    def test_a_missing_file_is_a_plain_error(self):
        code, _, err = self.import_file(self.folder / "absent.csv")
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("cannot read import file", err)


class ExportFiles(InventoryCase):
    def setUp(self):
        super().setUp()
        self.add("PSU-0001", "psu", "--model", "=HYPERLINK(\"http://192.0.2.1\")", "--address", "192.0.2.11", "--notes", "-5 V rail")
        self.add("DMM-0001", "dmm", "--location", "Lab 1", "--status", "in-repair")

    def test_csv_neutralises_formulas_and_imports_back_unchanged(self):
        path = self.folder / "inv.csv"
        self.assertIn("Wrote 2 instruments", self.ok("instrument", "export", path))
        text = path.read_text(encoding="utf-8")
        self.assertIn("'=HYPERLINK", text)
        self.assertIn("'-5 V rail", text)
        self.assertNotIn("\r\n", text)
        out = self.ok("instrument", "import", path)
        self.assertIn("0 added, 0 changed, 2 unchanged", out)
        other = self.folder / "other.sqlite"
        code, out, _ = call("instrument", "import", path, "--db", other)
        self.assertEqual(code, EXIT_PASS, out)
        conn = sqlite3.connect(other)
        try:
            row = conn.execute("SELECT model, notes, status FROM instruments WHERE asset_tag = 'PSU-0001'").fetchone()
        finally:
            conn.close()
        self.assertEqual(row, ('=HYPERLINK("http://192.0.2.1")', "-5 V rail", "active"))

    def test_json_export_with_filters_round_trips(self):
        path = self.folder / "inv.json"
        self.ok("instrument", "export", path, "--status", "in-repair")
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual([row["asset_tag"] for row in data["instruments"]], ["DMM-0001"])
        self.assertEqual(set(data["instruments"][0]), set(inventory.FIELDS))
        self.assertIn("1 unchanged", self.ok("instrument", "import", path))

    def test_export_refuses_to_replace_a_file_and_to_write_nothing(self):
        path = self.write("inv.csv", "keep me")
        self.assertIn("--force", self.refused("instrument", "export", path))
        self.assertEqual(path.read_text(encoding="utf-8"), "keep me")
        self.ok("instrument", "export", path, "--force")
        self.assertIn("nothing was written", self.refused("instrument", "export", self.folder / "none.csv", "--driver", "smu"))
        self.assertFalse((self.folder / "none.csv").exists())
        self.assertIn("--format", self.refused("instrument", "export", self.folder / "inv.txt"))


class Help(unittest.TestCase):
    def test_every_command_has_help(self):
        commands = [("audit",)]
        commands += [("instrument", name) for name in ("add", "list", "show", "edit", "retire", "reactivate", "import", "export")]
        commands += [("station", name) for name in ("create", "list", "show", "assign", "unassign", "rename", "delete", "export", "import", "check")]
        parser = build_parser()
        for argv in commands:
            with self.subTest(argv):
                code, out, _ = call(*argv, "--help")
                self.assertEqual(code, EXIT_PASS)
                self.assertIn("usage: benchrun", out)
                self.assertIn("--db", out)
                sub = parser
                for word in argv:
                    sub = next(a for a in sub._actions if isinstance(getattr(a, "choices", None), dict)).choices[word]
                for action in sub._actions:
                    self.assertTrue(action.help, f"{argv} {action.dest} has no help text")


class AuditCommand(InventoryCase):
    def setUp(self):
        super().setUp()
        self.add("PSU-0001", "psu", "--actor", "Operator A")
        self.add("DMM-0001", "dmm")
        self.ok("instrument", "retire", "PSU-0001", "--actor", "Operator B", "--reason", "worn")
        self.ok("station", "create", "LINE-1", "--actor", "Operator A")

    def entry_ids(self, *filters):
        return [(e["action"], e["subject"]) for e in json.loads(self.ok("audit", "--json", *filters))["entries"]]

    def test_filters(self):
        self.assertEqual(len(self.entry_ids()), 4)
        self.assertEqual(self.entry_ids("--subject", "instrument:PSU-0001"), [("retire", "instrument:PSU-0001"), ("add", "instrument:PSU-0001")])
        self.assertEqual(self.entry_ids("--subject", "station:"), [("create", "station:LINE-1")])
        self.assertEqual(self.entry_ids("--action", "add"), [("add", "instrument:DMM-0001"), ("add", "instrument:PSU-0001")])
        self.assertEqual(self.entry_ids("--actor", "Operator B"), [("retire", "instrument:PSU-0001")])
        self.assertEqual(self.entry_ids("--actor", ""), [("add", "instrument:DMM-0001")])
        self.assertEqual(len(self.entry_ids("--limit", "2")), 2)
        self.assertEqual(self.entry_ids("--since", "2999-01-01"), [])
        self.assertEqual(len(self.entry_ids("--until", "2999-01-01", "--since", "2000-01-01")), 4)
        self.assertEqual(self.entry_ids("--until", "2000-01-01"), [])

    def test_text_output(self):
        out = self.ok("audit", "--limit", "2")
        self.assertIn("station:LINE-1", out)
        self.assertIn('reason="worn", status=["active", "retired"]', out)
        self.assertIn("Showing the newest 2", out)
        self.assertIn("No matching audit entries.", self.ok("audit", "--action", "nothing"))

    def test_bad_times_are_refused(self):
        self.assertIn("is not a time", self.refused("audit", "--since", "yesterday"))


if __name__ == "__main__":
    unittest.main()
