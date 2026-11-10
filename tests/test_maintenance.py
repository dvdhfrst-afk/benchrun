"""The db commands: info, check, backup, vacuum and mark-abandoned."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone

from benchrun.archive import database_id
from benchrun.cli import EXIT_FAIL, EXIT_PASS, EXIT_USAGE
from benchrun.maintenance import ABANDONED, check_database
from benchrun.store import Store
from tests.support import BenchTestCase
from tests.test_archive import count_rows, record_runs, running_run
from tests.test_cli_more import call


def hours_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="milliseconds")


class Maintenance(BenchTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db = self.folder / "results.sqlite"

    def started(self, run_id: int, hours: float) -> None:
        """Move a run's start, and everything recorded for it, back in time."""
        moved = hours_ago(hours)
        with self.store.conn:
            self.store.conn.execute("UPDATE runs SET started_at = ? WHERE id = ?", (moved, run_id))
            self.store.conn.execute(
                "UPDATE steps SET started_at = ? WHERE run_id = ? AND started_at IS NOT NULL", (moved, run_id)
            )
            self.store.conn.execute(
                "UPDATE steps SET finished_at = ? WHERE run_id = ? AND finished_at IS NOT NULL", (moved, run_id)
            )
            self.store.conn.execute("UPDATE measurements SET taken_at = ? WHERE run_id = ?", (moved, run_id))


class Info(Maintenance):
    def test_counts_and_identity(self):
        record_runs(self, self.store)
        running_run(self.store)
        with self.store.conn:
            self.store.conn.execute("INSERT INTO lots (name, created_at) VALUES ('L2026-11-01', '2026-11-01T08:00:00.000+00:00')")
        code, out, err = call("db", "info", "--db", self.db, "--json")
        self.assertEqual(code, EXIT_PASS, err)
        info = json.loads(out)
        self.assertEqual(info["schema_version"], 3)
        self.assertEqual(info["database_id"], database_id(self.store.conn))
        self.assertEqual(info["runs"], 4)
        self.assertEqual(info["runs_by_status"], {"pass": 1, "fail": 1, "aborted": 1, "running": 1})
        self.assertEqual((info["lots"], info["instruments"], info["stations"], info["products"]), (1, 0, 0, 0))
        self.assertEqual(info["oldest_run"]["serial"], "SN-1001")
        self.assertEqual(info["newest_run"]["serial"], "SN-2001")
        self.assertGreater(info["size_bytes"], 0)
        code, out, _ = call("db", "info", "--db", self.db)
        self.assertEqual(code, EXIT_PASS)
        self.assertIn("Schema version:  3", out)
        self.assertIn("4 (1 pass, 1 fail, 1 aborted, 1 running)", out)

    def test_an_empty_database(self):
        code, out, _ = call("db", "info", "--db", self.db, "--json")
        info = json.loads(out)
        self.assertEqual((info["runs"], info["oldest_run"]), (0, None))
        self.assertIn("Oldest run:      none", call("db", "info", "--db", self.db)[1])

    def test_a_missing_database_is_not_created(self):
        missing = self.folder / "nowhere.sqlite"
        for command in ("info", "check", "vacuum"):
            code, _, err = call("db", command, "--db", missing)
            self.assertEqual(code, EXIT_USAGE, command)
            self.assertIn("no results database", err)
        self.assertEqual(call("db", "mark-abandoned", "--older-than", 1, "--db", missing)[0], EXIT_USAGE)
        self.assertFalse(missing.exists())


class Check(Maintenance):
    def test_a_sound_database_passes(self):
        record_runs(self, self.store)
        code, out, _ = call("db", "check", "--db", self.db)
        self.assertEqual(code, EXIT_PASS)
        self.assertIn("no problems found", out)
        result = json.loads(call("db", "check", "--db", self.db, "--json")[1])
        self.assertEqual((result["ok"], result["problems"], result["running"]), (True, [], []))

    def test_a_run_in_progress_is_listed_but_not_a_problem(self):
        running_run(self.store)
        code, out, _ = call("db", "check", "--db", self.db)
        self.assertEqual(code, EXIT_PASS)
        self.assertIn("Runs still marked running:", out)
        self.assertIn("SN-2001", out)

    def test_a_run_left_running_too_long_is_a_problem(self):
        run_id = running_run(self.store)
        self.started(run_id, 30)
        code, out, _ = call("db", "check", "--db", self.db)
        self.assertEqual(code, EXIT_FAIL)
        self.assertIn("has been marked running for 30.0 hours", out)
        self.assertIn("benchrun db mark-abandoned --older-than 24", out)
        self.assertEqual(call("db", "check", "--db", self.db, "--older-than", 48)[0], EXIT_PASS)
        result = json.loads(call("db", "check", "--db", self.db, "--json")[1])
        self.assertAlmostEqual(result["running"][0]["age_hours"], 30.0, delta=0.1)

    def test_orphaned_and_misfiled_rows(self):
        ids = record_runs(self, self.store, serials=("SN-1001", "SN-1002"))
        conn = self.store.conn
        conn.execute("PRAGMA foreign_keys = OFF")
        with conn:
            other_step = conn.execute("SELECT MIN(id) FROM steps WHERE run_id = ?", (ids[1],)).fetchone()[0]
            conn.execute("UPDATE measurements SET step_id = ? WHERE id = (SELECT MIN(id) FROM measurements WHERE run_id = ?)", (other_step, ids[0]))
            conn.execute("INSERT INTO steps (run_id, ordinal, name, kind, status) VALUES (999, 0, 'lost', 'test', 'pass')")
            conn.execute("UPDATE steps SET status = 'running' WHERE id = (SELECT MAX(id) FROM steps WHERE run_id = ?)", (ids[0],))
            conn.execute("UPDATE runs SET uuid = NULL WHERE id = ?", (ids[1],))
        code, out, _ = call("db", "check", "--db", self.db)
        self.assertEqual(code, EXIT_FAIL)
        self.assertIn("1 row in steps point to a missing row in runs", out)
        self.assertIn("1 measurement filed under a step of a different run", out)
        self.assertIn("1 step still pending or running in runs that have finished", out)
        self.assertIn("1 run without a uuid", out)

    def test_a_damaged_file_fails_its_integrity_check(self):
        record_runs(self, self.store, serials=("SN-1001",))
        conn = self.store.conn
        real = conn.execute

        class Wrapped:
            """The integrity check of a damaged file, which cannot be made portably by corrupting bytes."""

            def __getattr__(self, name):
                return getattr(conn, name)

            def execute(self, sql, *args):
                if sql == "PRAGMA integrity_check":
                    return iter([("*** in database main ***",), ("Page 7: btreeInitPage() returns error code 11",)])
                return real(sql, *args)

        result = check_database(Wrapped(), 24.0, datetime.now(timezone.utc))
        self.assertFalse(result["ok"])
        self.assertIn("integrity check: Page 7: btreeInitPage() returns error code 11", result["problems"])


class Backup(Maintenance):
    def test_a_backup_is_a_whole_working_copy(self):
        record_runs(self, self.store)
        out = self.folder / "copy.sqlite"
        code, text, err = call("db", "backup", "--db", self.db, "--out", out)
        self.assertEqual(code, EXIT_PASS, err)
        self.assertIn("3 runs", text)
        with Store(out) as copy:
            self.assertEqual(copy.conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0], count_rows(self.db, "measurements"))
            self.assertEqual(database_id(copy.conn), database_id(self.store.conn))
        self.assertEqual(sorted(p.name for p in self.folder.iterdir() if p.name.startswith(".")), [])

    def test_it_never_overwrites_without_force(self):
        out = self.folder / "copy.sqlite"
        out.write_text("keep", encoding="utf-8")
        code, _, err = call("db", "backup", "--db", self.db, "--out", out)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("--force", err)
        self.assertEqual(out.read_text(encoding="utf-8"), "keep")
        self.assertEqual(call("db", "backup", "--db", self.db, "--out", out, "--force")[0], EXIT_PASS)
        self.assertEqual(count_rows(out, "runs"), 0)

    def test_it_refuses_the_database_itself_and_missing_folders(self):
        code, _, err = call("db", "backup", "--db", self.db, "--out", self.db, "--force")
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("is the database itself", err)
        code, _, err = call("db", "backup", "--db", self.db, "--out", self.folder / "no" / "copy.sqlite")
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("does not exist", err)


class Vacuum(Maintenance):
    def test_vacuum_keeps_every_run(self):
        record_runs(self, self.store)
        code, out, err = call("db", "vacuum", "--db", self.db)
        self.assertEqual(code, EXIT_PASS, err)
        self.assertIn("before", out)
        self.assertEqual(count_rows(self.db, "runs"), 3)
        self.assertEqual(call("db", "check", "--db", self.db)[0], EXIT_PASS)


class MarkAbandoned(Maintenance):
    def test_old_running_runs_are_closed_as_aborted(self):
        finished = record_runs(self, self.store, serials=("SN-1001",))[0]
        old = running_run(self.store, "SN-2001")
        young = running_run(self.store, "SN-2002")
        self.started(old, 10)
        self.started(finished, 10)
        last = self.store.conn.execute("SELECT MAX(taken_at) FROM measurements WHERE run_id = ?", (old,)).fetchone()[0]
        code, out, err = call("db", "mark-abandoned", "--older-than", 5, "--db", self.db, "--actor", "Operator B")
        self.assertEqual(code, EXIT_PASS, err)
        self.assertIn("Marked 1 run aborted", out)
        run = self.store.conn.execute("SELECT status, reason, finished_at FROM runs WHERE id = ?", (old,)).fetchone()
        self.assertEqual(tuple(run), ("aborted", ABANDONED, last))
        steps = self.store.conn.execute("SELECT status, message FROM steps WHERE run_id = ? ORDER BY ordinal", (old,)).fetchall()
        self.assertEqual([tuple(s) for s in steps], [("pass", None), ("error", ABANDONED), ("skipped", f"not run: {ABANDONED}")])
        self.assertEqual(self.store.conn.execute("SELECT status FROM runs WHERE id = ?", (young,)).fetchone()[0], "running")
        self.assertEqual(self.store.conn.execute("SELECT status FROM runs WHERE id = ?", (finished,)).fetchone()[0], "pass")
        entries = self.store.conn.execute("SELECT action, subject, detail FROM audit_log").fetchall()
        self.assertEqual(len(entries), 1)
        uuid = self.store.conn.execute("SELECT uuid FROM runs WHERE id = ?", (old,)).fetchone()[0]
        self.assertEqual((entries[0][0], entries[0][1]), ("run.mark-abandoned", f"run:{uuid}"))
        self.assertEqual(json.loads(entries[0][2])["steps_closed"], 2)
        self.assertEqual(self.store.conn.execute("SELECT actor FROM audit_log").fetchone()[0], "Operator B")
        self.assertIn("No run has been marked running", call("db", "mark-abandoned", "--older-than", 5, "--db", self.db)[1])

    def test_one_audit_entry_per_run(self):
        for serial in ("SN-2001", "SN-2002"):
            self.started(running_run(self.store, serial), 48)
        code, out, _ = call("db", "mark-abandoned", "--older-than", 24, "--db", self.db, "--json")
        self.assertEqual(code, EXIT_PASS)
        self.assertEqual(len(json.loads(out)["runs"]), 2)
        self.assertEqual(count_rows(self.db, "audit_log"), 2)
        self.assertEqual(call("db", "check", "--db", self.db)[0], EXIT_PASS)

    def test_dry_run_changes_nothing(self):
        run_id = running_run(self.store)
        self.started(run_id, 30)
        code, out, _ = call("db", "mark-abandoned", "--older-than", 24, "--db", self.db, "--dry-run")
        self.assertEqual(code, EXIT_PASS)
        self.assertIn("Dry run: would mark 1 run aborted", out)
        self.assertEqual(self.store.conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()[0], "running")
        self.assertEqual(count_rows(self.db, "audit_log"), 0)

    def test_the_age_must_be_given_and_above_zero(self):
        self.assertEqual(call("db", "mark-abandoned", "--db", self.db)[0], EXIT_USAGE)
        self.assertEqual(call("db", "mark-abandoned", "--older-than", 0, "--db", self.db)[0], EXIT_USAGE)
        self.assertEqual(call("db", "mark-abandoned", "--older-than", "soon", "--db", self.db)[0], EXIT_USAGE)


if __name__ == "__main__":
    unittest.main()
