"""Results databases from older versions, and files that are not ours to change."""

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from benchrun.store import SCHEMA_VERSION, Store

# The schema benchrun 1.0.0 created, exactly, with one finished run in it.
VERSION_1 = """
CREATE TABLE runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    serial TEXT NOT NULL,
    plan_name TEXT NOT NULL,
    plan_version TEXT NOT NULL,
    plan_hash TEXT NOT NULL,
    plan_source TEXT NOT NULL,
    variables TEXT NOT NULL,
    operator TEXT NOT NULL,
    software_version TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX runs_serial ON runs (serial);
CREATE INDEX runs_plan ON runs (plan_name);

CREATE TABLE run_instruments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs (id),
    name TEXT NOT NULL,
    driver TEXT NOT NULL,
    address TEXT NOT NULL,
    idn TEXT
);
CREATE INDEX run_instruments_run ON run_instruments (run_id);

CREATE TABLE steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs (id),
    ordinal INTEGER NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX steps_run ON steps (run_id);

CREATE TABLE measurements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs (id),
    step_id INTEGER NOT NULL REFERENCES steps (id),
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    point TEXT NOT NULL,
    instrument TEXT,
    samples INTEGER NOT NULL,
    value REAL,
    units TEXT NOT NULL,
    low REAL,
    high REAL,
    verdict TEXT NOT NULL,
    note TEXT,
    taken_at TEXT NOT NULL
);
CREATE INDEX measurements_run ON measurements (run_id);

INSERT INTO runs VALUES (7, 'OLD-1', 'old-plan', '1', 'abc', '{}', '{"vnom": 5.0}', 'Operator A', '1.0.0', 'pass',
    NULL, '2026-01-02T03:04:05.000+00:00', '2026-01-02T03:05:05.000+00:00');
INSERT INTO run_instruments VALUES (1, 7, 'dmm', 'dmm', '127.0.0.1:5025', 'benchrun,SIM-DMM,SIM0000,1.0');
INSERT INTO steps VALUES (1, 7, 0, 'first', 'step', 'pass', NULL, '2026-01-02T03:04:05.000+00:00',
    '2026-01-02T03:05:05.000+00:00');
INSERT INTO measurements VALUES (1, 7, 1, 'vout', 'read', '{"vin": 12.0}', 'dmm', 1, 5.001, 'V', 4.9, 5.1, 'pass',
    NULL, '2026-01-02T03:04:06.000+00:00');
PRAGMA user_version = 1;
"""


class Upgrade(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="benchrun-upgrade-"))
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.path = self.folder / "old.sqlite"

    def make_old(self):
        conn = sqlite3.connect(str(self.path))
        conn.executescript(VERSION_1)
        conn.commit()
        conn.close()
        return self.path.read_bytes()

    def assertOldCopy(self, path):
        conn = sqlite3.connect(str(path))
        try:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT serial FROM runs").fetchall(), [("OLD-1",)])
            self.assertEqual(conn.execute("SELECT value FROM measurements").fetchall(), [(5.001,)])
            columns = [row[1] for row in conn.execute("PRAGMA table_info(runs)")]
            self.assertNotIn("note", columns)
        finally:
            conn.close()

    def version(self):
        conn = sqlite3.connect(str(self.path))
        try:
            return conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()

    def test_old_data_survives_the_upgrade(self):
        self.make_old()
        with Store(self.path) as store:
            detail = store.run_detail(7)
            self.assertIn("Upgraded", store.upgrade_note)
        self.assertEqual(self.version(), SCHEMA_VERSION)
        self.assertEqual(detail["serial"], "OLD-1")
        self.assertEqual(detail["operator"], "Operator A")
        self.assertEqual(detail["status"], "pass")
        self.assertEqual(detail["note"], "")
        self.assertIsNone(detail["selected_steps"])
        self.assertEqual(detail["instruments"][0]["idn"], "benchrun,SIM-DMM,SIM0000,1.0")
        measurement = detail["steps"][0]["measurements"][0]
        self.assertEqual(measurement["value"], 5.001)
        self.assertEqual(measurement["point"], {"vin": 12.0})
        self.assertEqual(detail["variables"], {"vnom": 5.0})

    def test_a_backup_of_the_old_file_is_kept(self):
        self.make_old()
        Store(self.path).close()
        self.assertOldCopy(self.folder / "old.sqlite.schema1.bak")

    def test_an_existing_backup_is_never_overwritten(self):
        self.make_old()
        backup = self.folder / "old.sqlite.schema1.bak"
        backup.write_bytes(b"earlier backup")
        Store(self.path).close()
        self.assertEqual(backup.read_bytes(), b"earlier backup")
        self.assertEqual(len(list(self.folder.glob("old.sqlite.schema1*"))), 2)

    def test_second_open_does_nothing(self):
        self.make_old()
        Store(self.path).close()
        before = sorted(p.name for p in self.folder.iterdir())
        with Store(self.path) as store:
            self.assertIsNone(store.upgrade_note)
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), before)

    def test_new_runs_go_in_after_old_ones(self):
        self.make_old()
        with Store(self.path) as store:
            new = store.create_run("NEW-1", "p", "1", "s", "{}", {}, "", "1.1.0", note="after")
            self.assertGreater(new, 7)
            self.assertEqual(len(store.find_runs()), 2)

    def test_a_failed_upgrade_rolls_back_and_leaves_old_data(self):
        self.make_old()
        import benchrun.store as module

        bad = {1: ("ALTER TABLE runs ADD COLUMN note TEXT", "THIS IS NOT SQL")}
        with mock.patch.object(module, "_UPGRADES", bad), self.assertRaises(sqlite3.Error) as caught:
            Store(self.path)
        self.assertIn(str(self.path), str(caught.exception))
        self.assertEqual(self.version(), 1)
        conn = sqlite3.connect(str(self.path))
        columns = [row[1] for row in conn.execute("PRAGMA table_info(runs)")]
        conn.close()
        self.assertNotIn("note", columns)
        self.assertOldCopy(self.folder / "old.sqlite.schema1.bak")

    def test_a_newer_database_is_refused_unchanged(self):
        self.make_old()
        conn = sqlite3.connect(str(self.path))
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 5}")
        conn.commit()
        conn.close()
        before = self.path.read_bytes()
        with self.assertRaises(sqlite3.Error) as caught:
            Store(self.path)
        self.assertIn("newer", str(caught.exception))
        self.assertEqual(self.path.read_bytes(), before)

    def test_someone_elses_sqlite_file_is_refused_unchanged(self):
        conn = sqlite3.connect(str(self.path))
        conn.execute("CREATE TABLE invoices (id INTEGER, total REAL)")
        conn.execute("INSERT INTO invoices VALUES (1, 9.5)")
        conn.commit()
        conn.close()
        before = self.path.read_bytes()
        with self.assertRaises(sqlite3.Error):
            Store(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ["old.sqlite"])

    def test_files_that_are_not_databases_are_refused_unchanged(self):
        for content in (b"this is a text file, not a database\n", b"SQLite format 3\x00" + b"\xff" * 200, bytes(5000)):
            self.path.write_bytes(content)
            with self.assertRaises(sqlite3.Error):
                Store(self.path)
            self.assertEqual(self.path.read_bytes(), content)

    def test_a_zero_length_file_becomes_a_database(self):
        self.path.write_bytes(b"")
        with Store(self.path) as store:
            self.assertEqual(store.find_runs(), [])
        self.assertEqual(self.version(), SCHEMA_VERSION)

    def test_folder_that_does_not_exist_yet_is_made(self):
        deep = self.folder / "a" / "b" / "r.sqlite"
        Store(deep).close()
        self.assertTrue(deep.exists())

    def test_a_folder_in_place_of_the_database(self):
        with self.assertRaises(sqlite3.Error):
            Store(self.folder)


if __name__ == "__main__":
    unittest.main()
