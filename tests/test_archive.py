"""Run archives: round trips between databases, uuid matching, hostile archives and show --json files."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from unittest import mock

from benchrun import archive
from benchrun.archive import FORMAT, MANIFEST, Archive, RunFile, check_record, content_text, database_id, record_from_store
from benchrun.cli import EXIT_ABORTED, EXIT_FAIL, EXIT_PASS, EXIT_USAGE
from benchrun.runner import Runner
from benchrun.sim.bench import parse_fault
from benchrun.store import Store
from tests.support import BenchTestCase, base_plan
from tests.test_cli_more import call

FIXTURES = Path(__file__).parent / "fixtures" / "show_json_1_1_0"


def record_runs(case: BenchTestCase, store: Store, serials: Sequence[str] = ("SN-1001", "SN-1002", "SN-1003")) -> List[int]:
    """A passing, a failing and an aborted run of the small plan, on simulators."""
    kinds = ({}, {"dut": "high-output"}, {"faults": parse_fault("dmm:drop:2")})
    ids = []
    for serial, kwargs in zip(serials, kinds):
        bench = case.start_bench(**kwargs)
        plan = case.on_bench(base_plan(), bench)
        ids.append(Runner(plan, store, serial, operator="Operator A", sleep=lambda _: None).run().run_id)
    return ids


def running_run(store: Store, serial: str = "SN-2001") -> int:
    """A run left the way a killed process leaves it."""
    run_id = store.create_run(serial, "small", "1", "0" * 64, "{}", {"vnom": 5.0}, "Operator A", "1.1.0")
    store.add_instrument(run_id, "dmm", "dmm", "127.0.0.1:5025")
    setup = store.add_step(run_id, 0, "(setup)", "setup")
    store.begin_step(setup)
    store.finish_step(setup, "pass")
    step = store.add_step(run_id, 1, "vout", "test")
    store.begin_step(step)
    store.add_measurement(run_id, step, "vout", "read", {"vin": 8.0}, 5.0, "V", 4.9, 5.1, "pass", instrument="dmm")
    store.add_step(run_id, 2, "(teardown)", "teardown")
    return run_id


def count_rows(path: Path, table: str) -> int:
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def fixture_record(name: str = "run1.json") -> Dict[str, Any]:
    source = RunFile(FIXTURES / name, (FIXTURES / name).read_bytes())
    assert source.check() == []
    return next(source.records())


def run_bytes(record: Dict[str, Any]) -> bytes:
    return (json.dumps(record, indent=1) + "\n").encode("utf-8")


def build_archive(
    path: Path,
    members: Sequence[Tuple[str, bytes]],
    manifest: Optional[Dict[str, Any]] = None,
    edit: Optional[Any] = None,
) -> Path:
    """A zip with these members and, unless given, a manifest that agrees with them."""
    if manifest is None:
        listed = [{"name": n, "sha256": hashlib.sha256(d).hexdigest(), "size": len(d)} for n, d in members if n.startswith("runs/")]
        manifest = {
            "format": FORMAT,
            "format_version": 1,
            "created_at": "2026-11-01T08:00:00.000+00:00",
            "benchrun_version": "1.1.0",
            "database_id": "f" * 32,
            "run_count": len(listed),
            "members": listed,
        }
    if edit is not None:
        edit(manifest)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zipped:
        for name, data in members:
            zipped.writestr(name, data)
        if manifest is not False:
            zipped.writestr(MANIFEST, json.dumps(manifest) if isinstance(manifest, dict) else manifest)
    return path


def one_run(record: Optional[Dict[str, Any]] = None) -> Tuple[str, bytes]:
    record = record or fixture_record()
    return f"runs/{record['uuid']}.json", run_bytes(record)


class Folder(BenchTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db_a = self.folder / "results.sqlite"  # The store BenchTestCase opened.
        self.db_b = self.folder / "other.sqlite"
        self.zip = self.folder / "runs.zip"

    def other_store(self) -> Store:
        store = Store(self.db_b)
        self.addCleanup(store.close)
        return store


class RoundTrip(Folder):
    def test_runs_cross_to_another_database_unchanged(self):
        ids = record_runs(self, self.store)
        code, out, err = call("archive", "export", "--db", self.db_a, "--out", self.zip)
        self.assertEqual(code, EXIT_PASS, err)
        self.assertIn("Wrote 3 runs", out)
        code, out, err = call("archive", "import", self.zip, "--db", self.db_b)
        self.assertEqual(code, EXIT_PASS, err)
        self.assertIn("Imported 3 runs", out)
        other = self.other_store()
        source_id = database_id(self.store.conn)
        for run_id in ids:
            original = record_from_store(self.store, run_id)
            copy_id = other.conn.execute("SELECT id FROM runs WHERE uuid = ?", (original["uuid"],)).fetchone()[0]
            copied = record_from_store(other, copy_id)
            self.assertEqual(content_text(copied), content_text(original))
            self.assertEqual(copied["origin"], source_id)
            self.assertIsNotNone(copied["imported_at"])
            self.assertEqual(original["origin"], "")
            # Reports are made from run_detail, so it must read the same apart from row ids.
            before, after = self.store.run_detail(run_id), other.run_detail(copy_id)
            self.assertEqual(after["steps"][1]["measurements"][0]["value"], before["steps"][1]["measurements"][0]["value"])
            self.assertEqual([s["status"] for s in after["steps"]], [s["status"] for s in before["steps"]])
        statuses = sorted(r[0] for r in other.conn.execute("SELECT status FROM runs"))
        self.assertEqual(statuses, ["aborted", "fail", "pass"])

    def test_child_rows_get_new_ids_and_stay_with_their_run(self):
        record_runs(self, self.store)
        other = self.other_store()
        record_runs(self, other, serials=("SN-9001",))  # Takes the ids the imported rows would otherwise have.
        call("archive", "export", "--db", self.db_a, "--out", self.zip)
        code, _, err = call("archive", "import", self.zip, "--db", self.db_b)
        self.assertEqual(code, EXIT_PASS, err)
        mismatched = other.conn.execute(
            "SELECT COUNT(*) FROM measurements m JOIN steps s ON s.id = m.step_id WHERE s.run_id != m.run_id"
        ).fetchone()[0]
        self.assertEqual(mismatched, 0)
        for table in ("run_instruments", "steps", "measurements"):
            orphans = other.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE run_id NOT IN (SELECT id FROM runs)").fetchone()[0]
            self.assertEqual(orphans, 0, table)
        serials = [row[0] for row in other.conn.execute("SELECT serial FROM runs ORDER BY id")]
        self.assertEqual(serials, ["SN-9001", "SN-1001", "SN-1002", "SN-1003"])  # New ids follow the order the runs started.
        self.assertEqual(call("db", "check", "--db", self.db_b)[0], EXIT_PASS)

    def test_identical_runs_are_skipped_both_ways(self):
        record_runs(self, self.store)
        call("archive", "export", "--db", self.db_a, "--out", self.zip)
        call("archive", "import", self.zip, "--db", self.db_b)
        back = self.folder / "back.zip"
        self.assertEqual(call("archive", "export", "--db", self.db_b, "--out", back)[0], EXIT_PASS)
        code, out, err = call("archive", "import", back, "--db", self.db_a)
        self.assertEqual(code, EXIT_PASS, err)
        self.assertIn("Imported 0 runs", out)
        self.assertIn("3 already there", out)
        self.assertEqual(count_rows(self.db_a, "runs"), 3)
        code, out, _ = call("archive", "import", self.zip, "--db", self.db_b)
        self.assertEqual(code, EXIT_PASS)
        self.assertEqual(count_rows(self.db_b, "runs"), 3)

    def test_a_different_run_with_the_same_uuid_is_refused_and_left_alone(self):
        ids = record_runs(self, self.store)
        call("archive", "export", "--db", self.db_a, "--out", self.zip)
        call("archive", "import", self.zip, "--db", self.db_b)
        other = self.other_store()
        uuid = self.store.conn.execute("SELECT uuid FROM runs WHERE id = ?", (ids[0],)).fetchone()[0]
        with other.conn:
            other.conn.execute(
                "UPDATE measurements SET value = 9.5 WHERE id = (SELECT MIN(m.id) FROM measurements m JOIN runs r ON r.id = m.run_id WHERE r.uuid = ?)",
                (uuid,),
            )
        back = self.folder / "back.zip"
        call("archive", "export", "--db", self.db_b, "--out", back)
        before = content_text(record_from_store(self.store, ids[0]))
        code, out, err = call("archive", "import", back, "--db", self.db_a, "--json")
        self.assertEqual(code, EXIT_FAIL)
        payload = json.loads(out)
        self.assertEqual((payload["conflict"], payload["skipped"], payload["imported"]), (1, 2, 0))
        refused = [r for r in payload["runs"] if r["action"] == "conflict"][0]
        self.assertEqual(refused["run_id"], ids[0])
        self.assertIn("nothing was merged or replaced", refused["detail"])
        self.assertEqual(content_text(record_from_store(self.store, ids[0])), before)
        code, out, err = call("archive", "import", back, "--db", self.db_a)
        self.assertEqual(code, EXIT_FAIL)
        self.assertIn("REFUSED", out)
        self.assertIn("a different run with this uuid is already here", err)

    def test_running_runs_are_refused_unless_asked_for_and_then_stay_running(self):
        record_runs(self, self.store, serials=("SN-1001",))
        running = running_run(self.store)
        code, out, _ = call("archive", "export", "--db", self.db_a, "--out", self.zip)
        self.assertIn("1 run still marked running", out)
        code, out, err = call("archive", "import", self.zip, "--db", self.db_b)
        self.assertEqual(code, EXIT_FAIL)
        self.assertIn("--include-unfinished", err)
        self.assertEqual(count_rows(self.db_b, "runs"), 1)
        code, out, err = call("archive", "import", self.zip, "--db", self.db_b, "--include-unfinished")
        self.assertEqual(code, EXIT_PASS, err)
        uuid = self.store.conn.execute("SELECT uuid FROM runs WHERE id = ?", (running,)).fetchone()[0]
        other = self.other_store()
        status = other.conn.execute("SELECT status FROM runs WHERE uuid = ?", (uuid,)).fetchone()[0]
        self.assertEqual(status, "running")
        steps = [r[0] for r in other.conn.execute("SELECT s.status FROM steps s JOIN runs r ON r.id = s.run_id WHERE r.uuid = ? ORDER BY ordinal", (uuid,))]
        self.assertEqual(steps, ["pass", "running", "pending"])

    def test_dry_run_reports_and_changes_nothing(self):
        record_runs(self, self.store, serials=("SN-1001",))
        call("archive", "export", "--db", self.db_a, "--out", self.zip)
        code, out, _ = call("archive", "import", self.zip, "--db", self.db_b, "--dry-run")
        self.assertEqual(code, EXIT_PASS)
        self.assertIn("would import", out)
        self.assertIn("Dry run: nothing was changed.", out)
        self.assertFalse(self.db_b.exists(), "a dry run must not create the database")
        self.other_store().close()
        code, out, _ = call("archive", "import", self.zip, "--db", self.db_b, "--dry-run", "--json")
        self.assertEqual(json.loads(out)["imported"], 1)
        self.assertEqual(count_rows(self.db_b, "runs"), 0)
        self.assertEqual(count_rows(self.db_b, "audit_log"), 0)

    def test_each_import_writes_one_audit_entry_with_counts(self):
        record_runs(self, self.store)
        call("archive", "export", "--db", self.db_a, "--out", self.zip)
        call("archive", "import", self.zip, "--db", self.db_b)
        call("archive", "import", self.zip, "--db", self.db_b)
        other = self.other_store()
        rows = other.conn.execute("SELECT action, subject, detail FROM audit_log ORDER BY id").fetchall()
        self.assertEqual([r[0] for r in rows], ["archive.import", "archive.import"])
        self.assertEqual(rows[0][1], "archive:runs.zip")
        first, second = json.loads(rows[0][2]), json.loads(rows[1][2])
        self.assertEqual((first["imported"], first["skipped"]), (3, 0))
        self.assertEqual((second["imported"], second["skipped"]), (0, 3))
        self.assertEqual(first["origin"], database_id(self.store.conn))

    def test_a_lot_travels_with_its_runs(self):
        ids = record_runs(self, self.store, serials=("SN-1001",))
        with self.store.conn:
            cursor = self.store.conn.execute(
                "INSERT INTO lots (name, product, plan_name, quantity, created_at) VALUES ('L2026-11-01', 'PN-100-A', 'small', 50, '2026-11-01T08:00:00.000+00:00')"
            )
            self.store.conn.execute("UPDATE runs SET lot_id = ? WHERE id = ?", (cursor.lastrowid, ids[0]))
        call("archive", "export", "--db", self.db_a, "--out", self.zip)
        code, out, err = call("archive", "import", self.zip, "--db", self.db_b)
        self.assertEqual(code, EXIT_PASS, err)
        self.assertIn("Added lot L2026-11-01", out)
        other = self.other_store()
        lot = other.conn.execute("SELECT id, product, quantity FROM lots WHERE name = 'L2026-11-01'").fetchone()
        self.assertEqual((lot[1], lot[2]), ("PN-100-A", 50))
        self.assertEqual(other.conn.execute("SELECT lot_id FROM runs").fetchone()[0], lot[0])
        actions = [r[0] for r in other.conn.execute("SELECT action FROM audit_log ORDER BY id")]
        self.assertEqual(actions, ["lot.create", "archive.import"])
        subject = other.conn.execute("SELECT subject FROM audit_log WHERE action = 'lot.create'").fetchone()[0]
        self.assertEqual(subject, "lot:L2026-11-01")

    def test_a_failed_import_changes_nothing(self):
        record_runs(self, self.store)
        call("archive", "export", "--db", self.db_a, "--out", self.zip)
        self.other_store().close()
        real = archive._insert
        calls = []

        def failing(*args):
            calls.append(1)
            if len(calls) == 3:
                raise sqlite3.OperationalError("disk I/O error")
            return real(*args)

        with mock.patch.object(archive, "_insert", failing):
            code, _, err = call("archive", "import", self.zip, "--db", self.db_b)
        self.assertEqual(code, EXIT_ABORTED)
        self.assertIn("disk I/O error", err)
        for table in ("runs", "run_instruments", "steps", "measurements", "audit_log", "lots"):
            self.assertEqual(count_rows(self.db_b, table), 0, table)
        self.assertEqual(call("archive", "import", self.zip, "--db", self.db_b)[0], EXIT_PASS)
        self.assertEqual(count_rows(self.db_b, "runs"), 3)

    def test_the_manifest_describes_every_member(self):
        record_runs(self, self.store)
        call("archive", "export", "--db", self.db_a, "--out", self.zip)
        with zipfile.ZipFile(self.zip) as zipped:
            manifest = json.loads(zipped.read(MANIFEST))
            self.assertEqual(manifest["format"], "benchrun-archive")
            self.assertEqual(manifest["format_version"], 1)
            self.assertEqual(manifest["database_id"], database_id(self.store.conn))
            self.assertEqual(manifest["run_count"], 3)
            self.assertEqual(manifest["benchrun_version"], "1.1.0")
            for member in manifest["members"]:
                data = zipped.read(member["name"])
                self.assertEqual(hashlib.sha256(data).hexdigest(), member["sha256"])
                self.assertEqual(len(data), member["size"])
                record = json.loads(data)
                self.assertEqual(member["name"], f"runs/{record['uuid']}.json")
                self.assertIn("plan_source", record)
                self.assertNotIn("id", record)
            self.assertEqual(sorted(zipped.namelist()), sorted([MANIFEST] + [m["name"] for m in manifest["members"]]))

    def test_list_and_verify_read_without_importing(self):
        record_runs(self, self.store)
        call("archive", "export", "--db", self.db_a, "--out", self.zip)
        code, out, _ = call("archive", "list", self.zip)
        self.assertEqual(code, EXIT_PASS)
        self.assertIn("SN-1002", out)
        self.assertIn("3 runs readable", out)
        code, out, _ = call("archive", "list", self.zip, "--json")
        payload = json.loads(out)
        self.assertEqual([r["serial"] for r in payload["runs"]].count("SN-1003"), 1)
        self.assertEqual(payload["database_id"], database_id(self.store.conn))
        code, out, _ = call("archive", "verify", self.zip)
        self.assertEqual(code, EXIT_PASS)
        self.assertIn("OK, 3 runs", out)
        self.assertEqual(json.loads(call("archive", "verify", self.zip, "--json")[1])["ok"], True)
        self.assertFalse(self.db_b.exists())

    def test_export_filters(self):
        ids = record_runs(self, self.store)

        def exported(*extra: Any) -> List[str]:
            out = self.folder / "pick.zip"
            code, _, err = call("archive", "export", "--db", self.db_a, "--out", out, "--force", *extra)
            self.assertEqual(code, EXIT_PASS, err)
            return sorted(r["serial"] for r in json.loads(call("archive", "list", out, "--json")[1])["runs"])

        self.assertEqual(exported("--run", ids[0], "--run", ids[2]), ["SN-1001", "SN-1003"])
        self.assertEqual(exported("--status", "fail"), ["SN-1002"])
        self.assertEqual(exported("--serial", "SN-1003"), ["SN-1003"])
        self.assertEqual(exported("--run", ids[0], "--status", "pass"), ["SN-1001"])
        self.assertEqual(exported("--since", "2000-01-01", "--plan", "small"), ["SN-1001", "SN-1002", "SN-1003"])
        code, _, err = call("archive", "export", "--db", self.db_a, "--out", self.zip, "--run", 99)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("no run with id 99", err)
        code, _, err = call("archive", "export", "--db", self.db_a, "--out", self.zip, "--until", "2000-01-01")
        self.assertEqual(code, EXIT_USAGE)
        self.assertFalse(self.zip.exists())
        self.zip.write_text("keep", encoding="utf-8")
        code, _, err = call("archive", "export", "--db", self.db_a, "--out", self.zip)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("--force", err)
        self.assertEqual(self.zip.read_text(encoding="utf-8"), "keep")
        self.assertEqual(call("archive", "export", "--db", self.folder / "none.sqlite", "--out", self.folder / "x.zip")[0], EXIT_USAGE)

    def test_a_failed_write_leaves_no_archive_and_no_litter(self):
        record_runs(self, self.store, serials=("SN-1001",))
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            code, _, err = call("archive", "export", "--db", self.db_a, "--out", self.zip)
        self.assertEqual(code, EXIT_ABORTED)
        self.assertEqual(sorted(p.name for p in self.folder.iterdir() if "zip" in p.name), [])


class Hostile(unittest.TestCase):
    """Every way an archive can be wrong is reported, and an import of it changes nothing."""

    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp(prefix="benchrun-archive-"))
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.db = self.folder / "target.sqlite"
        self.record = fixture_record()

    def refused(self, path: Path, expected: str) -> List[str]:
        """verify reports ``expected``, and import refuses with nothing written."""
        code, out, _ = call("archive", "verify", path, "--json")
        self.assertEqual(code, EXIT_FAIL, out)
        problems = json.loads(out)["problems"]
        self.assertTrue(any(expected in p for p in problems), problems)
        code, _, err = call("archive", "import", path, "--db", self.db)
        self.assertEqual(code, EXIT_USAGE, err)
        self.assertIn(expected, err)
        self.assertIn("nothing was imported", err)
        if self.db.exists():
            self.assertEqual(count_rows(self.db, "runs"), 0)
            self.assertEqual(count_rows(self.db, "audit_log"), 0)
        return problems

    def archive_with(self, members: Sequence[Tuple[str, bytes]], **kwargs: Any) -> Path:
        return build_archive(self.folder / "bad.zip", members, **kwargs)

    def changed(self, change: Any) -> Path:
        record = json.loads(json.dumps(self.record))
        change(record)
        return self.archive_with([(f"runs/{self.record['uuid']}.json", run_bytes(record))])

    def test_a_sound_archive_built_here_passes(self):
        path = self.archive_with([one_run()])
        self.assertEqual(call("archive", "verify", path)[0], EXIT_PASS)
        self.assertEqual(call("archive", "import", path, "--db", self.db)[0], EXIT_PASS)

    def test_path_traversal_and_absolute_names(self):
        name, data = one_run()
        for bad, expected in (
            ("../evil.json", "climbs out"),
            ("runs/../../evil.json", "climbs out"),
            ("/etc/evil.json", "absolute path"),
            ("C:/evil.json", "absolute path"),
            ("C:\\Windows\\evil.json", "absolute path"),
            ("runs\\..\\..\\evil.json", "climbs out"),
        ):
            with self.subTest(bad):
                self.refused(self.archive_with([(name, data), (bad, b"{}")]), expected)

    def test_unexpected_members(self):
        self.refused(self.archive_with([one_run(), ("notes.txt", b"hello")]), "unexpected member")
        self.refused(self.archive_with([one_run(), ("runs/SN-1.json", b"{}")]), "unexpected member")

    def test_duplicate_member_names(self):
        name, data = one_run()
        stand_in = f"runs/{'e' * 32}.json"
        path = self.archive_with([(name, data), (stand_in, data)])
        # zipfile will not write a name twice without a warning, so rename the second member in its headers.
        forged = path.read_bytes()
        self.assertEqual(forged.count(stand_in.encode()), 2)
        path.write_bytes(forged.replace(stand_in.encode(), name.encode()))
        self.refused(path, "appears more than once")

    def test_too_many_members(self):
        other = dict(self.record, uuid="a" * 32)
        path = self.archive_with([one_run(self.record), one_run(other)])
        with mock.patch.object(archive, "MAX_MEMBERS", 2):
            self.refused(path, "may hold at most 2")

    def test_a_member_too_big_by_its_header(self):
        path = self.archive_with([one_run(self.record)])
        with mock.patch.object(archive, "MAX_MEMBER_BYTES", 1000):
            self.refused(path, "the most allowed is 1000")

    def test_members_too_big_together(self):
        other = dict(self.record, uuid="a" * 32)
        path = self.archive_with([one_run(self.record), one_run(other)])
        with mock.patch.object(archive, "MAX_TOTAL_BYTES", 6000):
            self.refused(path, "the most allowed is 6000")

    def test_a_header_that_understates_the_size_is_caught_while_reading(self):
        """A bomb whose header claims a few bytes. Sizes are counted as read, and the content must still check out."""
        name = f"runs/{'b' * 32}.json"
        bomb = b'{"pad": "' + b"0" * 5_000_000 + b'"}'
        path = self.archive_with([(name, bomb)])
        forged = bytearray(path.read_bytes())
        real = len(bomb).to_bytes(4, "little")
        claimed = (100).to_bytes(4, "little")
        self.assertEqual(forged.count(real), 2)  # Local header and central directory.
        path.write_bytes(bytes(forged).replace(real, claimed))
        problems = self.refused(path, name)
        self.assertTrue(any("damaged" in p or "SHA-256" in p or "bytes" in p for p in problems), problems)

    def test_reading_stops_when_the_archive_budget_runs_out(self):
        path = self.archive_with([one_run()])
        source = Archive(path)
        self.addCleanup(source.close)
        info = source._zip.getinfo(one_run()[0])
        data, problem = source._member_bytes(info, [10])
        self.assertIsNone(data)
        self.assertIn("stopped", problem)
        with mock.patch.object(archive, "MAX_MEMBER_BYTES", 10):
            data, problem = source._member_bytes(info, [10**9])
        self.assertIn("unpacks to more than 10 bytes", problem)

    def test_invalid_json(self):
        name = f"runs/{self.record['uuid']}.json"
        self.refused(self.archive_with([(name, b'{"uuid": ')]), "is not valid JSON")
        self.refused(self.archive_with([(name, b"\xff\xfe\x00")]), "is not UTF-8")
        self.refused(self.archive_with([one_run()], manifest="{not json"), "manifest.json: is not valid JSON")
        repeated = run_bytes(self.record).replace(b'"serial": "SN-1001",', b'"serial": "SN-1001", "serial": "SN-9",')
        self.refused(self.archive_with([(name, repeated)]), "appears twice")
        nan = run_bytes(self.record).replace(b'"value": 4.995959022', b'"value": NaN', 1)
        self.assertNotEqual(nan, run_bytes(self.record))
        self.refused(self.archive_with([(name, nan)]), "NaN is not a value")

    def test_wrong_types(self):
        cases = (
            (lambda r: r.update(serial=5), "serial must be text"),
            (lambda r: r.update(steps={}), "steps must be a list"),
            (lambda r: r.update(variables={"vnom": "five"}), "variables.vnom must be a finite number"),
            (lambda r: r.update(status="won"), "status must be one of"),
            (lambda r: r.update(started_at="yesterday"), "started_at must be a time"),
            (lambda r: r.update(selected_steps="all"), "selected_steps must be a list"),
            (lambda r: r["steps"][1]["measurements"][0].update(value="4.99"), "value must be a number"),
            (lambda r: r["steps"][1]["measurements"][0].update(samples=True), "samples must be a whole number"),
            (lambda r: r["steps"][1]["measurements"][0].update(verdict="maybe"), "verdict must be one of"),
            (lambda r: r["steps"][1].update(ordinal=0), "is used by another step"),
            (lambda r: r["steps"][1].pop("status"), "steps[1].status is missing"),
            (lambda r: r["instruments"][0].update(extra=1), "unexpected field 'extra'"),
            (lambda r: r.update(lot={"name": "L1"}), "lot.created_at is missing"),
            (lambda r: r.update(plan_name="\ud800"), "broken character"),
        )
        for change, expected in cases:
            with self.subTest(expected):
                self.refused(self.changed(change), expected)

    def test_oversized_text_fields(self):
        self.refused(self.changed(lambda r: r.update(serial="S" * 5000)), "serial is 5000 characters long")
        self.refused(self.changed(lambda r: r.update(note="n" * 3000)), "the most allowed is 2000")
        message = lambda r: r["steps"][1].update(message="m" * 200000)  # noqa: E731
        self.refused(self.changed(message), "the most allowed is 100000")

    def test_a_manifest_that_disagrees_with_the_members(self):
        name, data = one_run()
        other_name, other_data = one_run(dict(self.record, uuid="a" * 32))
        self.refused(self.archive_with([(name, data)], edit=lambda m: m["members"][0].update(sha256="0" * 64)), "SHA-256")
        self.refused(self.archive_with([(name, data)], edit=lambda m: m["members"][0].update(size=5)), "the manifest says 5")
        self.refused(self.archive_with([(name, data)], edit=lambda m: m.update(run_count=2)), "says 2 runs but lists 1")
        missing = lambda m: m["members"].append(dict(m["members"][0], name=f"runs/{'c' * 32}.json"))  # noqa: E731
        self.refused(self.archive_with([(name, data)], edit=missing), "listed in the manifest but is not in the archive")
        unlisted = lambda m: m.update(members=m["members"][:1], run_count=1)  # noqa: E731
        self.refused(self.archive_with([(name, data), (other_name, other_data)], edit=unlisted), "not listed in the manifest")
        twice = lambda m: m.update(members=m["members"] * 2, run_count=2)  # noqa: E731
        self.refused(self.archive_with([(name, data)], edit=twice), "twice")
        self.refused(self.archive_with([(name, data)], edit=lambda m: m.update(format="zip")), "format must be one of")
        self.refused(self.archive_with([(name, data)], edit=lambda m: m.pop("database_id")), "database_id is missing")
        self.refused(self.archive_with([(name, data)], manifest=False), "has no usable manifest.json")

    def test_a_record_under_another_runs_name(self):
        self.refused(self.archive_with([(f"runs/{'d' * 32}.json", run_bytes(self.record))]), "not the run its name says")

    def test_a_newer_format_is_refused_plainly(self):
        path = self.archive_with([one_run()], edit=lambda m: m.update(format_version=2, surprise=True))
        problems = self.refused(path, "newer than the 1 this benchrun reads")
        self.assertEqual(len(problems), 1)
        self.assertIn("Upgrade benchrun", problems[0])

    def test_files_that_are_not_archives(self):
        broken = self.folder / "broken.zip"
        broken.write_bytes(b"PK\x03\x04" + b"\x00" * 40)
        code, _, err = call("archive", "verify", broken)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("not a zip file", err)
        empty = self.folder / "empty.zip"
        empty.write_bytes(b"")
        self.refused(empty, "is not valid JSON")
        code, _, err = call("archive", "list", self.folder / "missing.zip")
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("no file at", err)


class ShowJsonFiles(BenchTestCase):
    """Files written by show --json, by benchrun 1.1.0 (fixtures made by running it) and by this version."""

    def test_each_1_1_0_file_upgrades_and_imports_once(self):
        db = self.folder / "target.sqlite"
        for name in ("run1.json", "run2.json", "run3.json"):
            with self.subTest(name):
                legacy = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
                self.assertNotIn("uuid", legacy)
                code, out, err = call("archive", "import", FIXTURES / name, "--db", db, "--json")
                self.assertEqual(code, EXIT_PASS, err)
                payload = json.loads(out)
                self.assertEqual(payload["imported"], 1)
                run_id = payload["runs"][0]["run_id"]
                with Store(db) as store:
                    detail = store.run_detail(run_id)
                for key in ("serial", "plan_source", "variables", "status", "reason", "started_at", "note", "selected_steps"):
                    self.assertEqual(detail[key], legacy[key], key)
                self.assertEqual(detail["origin"], f"file:{name}")
                self.assertEqual(detail["uuid"], payload["runs"][0]["uuid"])
                values = [m["value"] for s in detail["steps"] for m in s["measurements"]]
                self.assertEqual(values, [m["value"] for s in legacy["steps"] for m in s["measurements"]])
                self.assertEqual([i["idn"] for i in detail["instruments"]], [i["idn"] for i in legacy["instruments"]])
                code, out, _ = call("archive", "import", FIXTURES / name, "--db", db, "--json")
                self.assertEqual(code, EXIT_PASS)
                self.assertEqual(json.loads(out)["skipped"], 1)
        self.assertEqual(count_rows(db, "runs"), 3)

    def test_the_derived_uuid_depends_on_content_not_on_encoding(self):
        original = (FIXTURES / "run2.json").read_bytes()
        variants = {
            "crlf.json": original.replace(b"\n", b"\r\n"),
            "utf16.json": original.decode("utf-8").encode("utf-16"),
            "bom.json": b"\xef\xbb\xbf" + original,
        }
        uuids = {fixture_record("run2.json")["uuid"]}
        for name, data in variants.items():
            source = RunFile(self.folder / name, data)
            self.assertEqual(source.check(), [], name)
            uuids.add(next(source.records())["uuid"])
        self.assertEqual(len(uuids), 1)
        changed = json.loads(original)
        changed["operator"] = "Operator B"
        source = RunFile(self.folder / "changed.json", json.dumps(changed).encode("utf-8"))
        self.assertEqual(source.check(), [])
        self.assertNotIn(next(source.records())["uuid"], uuids)

    def test_a_1_1_0_file_finds_its_run_in_an_upgraded_database(self):
        """A database upgraded from schema 2 gave the run a random uuid; the file's run is still the same run."""
        ids = record_runs(self, self.store, serials=("SN-1001",))
        code, out, _ = call("show", "--run", ids[0], "--json", "--db", self.folder / "results.sqlite")
        current = json.loads(out)
        legacy = {key: value for key, value in current.items() if key not in ("uuid", "station", "product", "lot_id", "origin", "imported_at")}
        for item in legacy["instruments"]:
            item.pop("asset_tag")
        path = self.folder / "old.json"
        path.write_text(json.dumps(legacy, indent=2), encoding="utf-8")
        code, out, err = call("archive", "import", path, "--db", self.folder / "results.sqlite", "--json")
        self.assertEqual(code, EXIT_PASS, err)
        outcome = json.loads(out)["runs"][0]
        self.assertEqual((outcome["action"], outcome["run_id"]), ("skipped", ids[0]))
        self.assertIn(current["uuid"], outcome["detail"])

    def test_a_current_show_json_file_keeps_its_uuid(self):
        ids = record_runs(self, self.store, serials=("SN-1001",))
        code, out, _ = call("show", "--run", ids[0], "--json", "--db", self.folder / "results.sqlite")
        path = self.folder / "run.json"
        path.write_text(out, encoding="utf-8")
        other = self.folder / "other.sqlite"
        code, out, err = call("archive", "import", path, "--db", other, "--json")
        self.assertEqual(code, EXIT_PASS, err)
        self.assertEqual(json.loads(out)["runs"][0]["uuid"], json.loads(path.read_text(encoding="utf-8"))["uuid"])

    def test_broken_files_are_reported(self):
        legacy = json.loads((FIXTURES / "run1.json").read_text(encoding="utf-8"))
        del legacy["plan_source"]
        legacy["steps"][1]["measurements"][0]["value"] = [1, 2]
        path = self.folder / "broken.json"
        path.write_text(json.dumps(legacy), encoding="utf-8")
        code, out, _ = call("archive", "verify", path)
        self.assertEqual(code, EXIT_FAIL)
        self.assertIn("plan_source is missing", out)
        self.assertIn("steps[1].measurements[0].value must be a number", out)
        listing = self.folder / "list.json"
        listing.write_text("[]", encoding="utf-8")
        code, _, err = call("archive", "import", listing, "--db", self.folder / "t.sqlite")
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("must be one run as a JSON object", err)
        self.assertFalse((self.folder / "t.sqlite").exists() and count_rows(self.folder / "t.sqlite", "runs"))

    def test_check_record_keeps_numbers_exact(self):
        record = fixture_record()
        again, problems = check_record(json.loads(json.dumps(record)), "copy")
        self.assertEqual(problems, [])
        self.assertEqual(content_text(again), content_text(record))


if __name__ == "__main__":
    unittest.main()
