"""Looking after a results database: what is in it, whether it is sound, copies, and runs left running.

A run whose process was killed, or whose computer lost power, stays marked
``running`` for ever. ``db mark-abandoned`` closes such runs as aborted, the way
the runner would have, with one audit entry each.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from benchrun import audit
from benchrun.archive import RUN_STATUSES, database_id
from benchrun.console import (
    DEFAULT_DB,
    EXIT_FAIL,
    EXIT_PASS,
    actor_of,
    add_actor_arg,
    count,
    open_store,
    positive_number,
    say,
    table,
)
from benchrun.errors import BenchrunError, PlanError
from benchrun.store import now_iso

ABANDONED = "abandoned: the process ended without finishing"
DEFAULT_STALE_HOURS = 24.0


def _existing(path: str) -> str:
    if not Path(path).is_file():
        raise PlanError([f"no results database at {path}"])
    return path


def _file_size(path: str) -> int:
    """The database and any write-ahead log beside it."""
    return sum(Path(name).stat().st_size for name in (path, f"{path}-wal") if Path(name).is_file())


def _size_text(size: int) -> str:
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KiB ({size} bytes)"
    return f"{size / (1024 * 1024):.1f} MiB ({size} bytes)"


def _age_hours(started_at: str, now: datetime) -> Optional[float]:
    try:
        started = datetime.fromisoformat(started_at)
    except (TypeError, ValueError):
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return (now - started).total_seconds() / 3600.0


def _one_row(conn: sqlite3.Connection, order: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(f"SELECT id, uuid, serial, started_at FROM runs ORDER BY started_at {order}, id {order} LIMIT 1").fetchone()
    return None if row is None else dict(row)


def database_info(conn: sqlite3.Connection, path: str) -> Dict[str, Any]:
    statuses = {status: 0 for status in RUN_STATUSES}
    for status, number in conn.execute("SELECT status, COUNT(*) FROM runs GROUP BY status"):
        statuses[status] = number
    counts = {table_name: conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0] for table_name in ("instruments", "stations", "products", "lots")}
    return {
        "path": str(Path(path).resolve()),
        "schema_version": conn.execute("PRAGMA user_version").fetchone()[0],
        "database_id": database_id(conn),
        "size_bytes": _file_size(path),
        "runs": sum(statuses.values()),
        "runs_by_status": statuses,
        "imported_runs": conn.execute("SELECT COUNT(*) FROM runs WHERE imported_at IS NOT NULL").fetchone()[0],
        **counts,
        "oldest_run": _one_row(conn, "ASC"),
        "newest_run": _one_row(conn, "DESC"),
    }


def running_runs(conn: sqlite3.Connection, now: datetime) -> List[Dict[str, Any]]:
    rows = conn.execute("SELECT id, uuid, serial, plan_name, started_at FROM runs WHERE status = 'running' ORDER BY id").fetchall()
    result = []
    for row in rows:
        item = dict(row)
        age = _age_hours(item["started_at"], now)
        item["age_hours"] = None if age is None else round(age, 2)
        result.append(item)
    return result


def check_database(conn: sqlite3.Connection, stale_hours: float, now: datetime) -> Dict[str, Any]:
    """Every problem found, and the runs still marked running."""
    problems: List[str] = []
    integrity = [row[0] for row in conn.execute("PRAGMA integrity_check")]
    if integrity != ["ok"]:
        problems += [f"integrity check: {line}" for line in integrity]
    missing: Dict[tuple, int] = {}
    for row in conn.execute("PRAGMA foreign_key_check"):
        missing[(row[0], row[2])] = missing.get((row[0], row[2]), 0) + 1
    for (child, parent), number in sorted(missing.items()):
        problems.append(f"{count(number, 'row')} in {child} point to a missing row in {parent}")
    queries = (
        (
            "SELECT COUNT(*) FROM measurements m JOIN steps s ON s.id = m.step_id WHERE s.run_id != m.run_id",
            "filed under a step of a different run",
            "measurement",
        ),
        ("SELECT COUNT(*) FROM runs WHERE uuid IS NULL OR uuid = ''", "without a uuid, so they cannot be archived", "run"),
        (
            "SELECT COUNT(*) FROM steps s JOIN runs r ON r.id = s.run_id"
            " WHERE r.status != 'running' AND s.status IN ('pending', 'running')",
            "still pending or running in runs that have finished",
            "step",
        ),
        (
            "SELECT COUNT(*) FROM runs WHERE status NOT IN ('pass', 'fail', 'aborted', 'running')",
            "with a status benchrun does not know",
            "run",
        ),
    )
    for sql, text, word in queries:
        number = conn.execute(sql).fetchone()[0]
        if number:
            problems.append(f"{count(number, word)} {text}")
    running = running_runs(conn, now)
    for run in running:
        if run["age_hours"] is None:
            problems.append(f"run {run['id']} is marked running and its start time {run['started_at']!r} cannot be read")
        elif run["age_hours"] > stale_hours:
            problems.append(f"run {run['id']} (serial {run['serial']}) has been marked running for {run['age_hours']:.1f} hours")
    return {"ok": not problems, "problems": problems, "running": running, "stale_hours": stale_hours}


def _last_activity(conn: sqlite3.Connection, run_id: int, started_at: str) -> str:
    """The last time anything was recorded for a run: the best guess at when its process ended."""
    times = [started_at]
    for sql in (
        "SELECT MAX(started_at), MAX(finished_at) FROM steps WHERE run_id = ?",
        "SELECT MAX(taken_at), NULL FROM measurements WHERE run_id = ?",
    ):
        times += [value for value in conn.execute(sql, (run_id,)).fetchone() if value]
    return max(times)


def mark_abandoned(conn: sqlite3.Connection, older_than_hours: float, now: datetime, dry_run: bool, actor: str = "") -> List[Dict[str, Any]]:
    """Close runs left running longer than ``older_than_hours`` as aborted. One transaction and audit entry per run."""
    marked = []
    for run in running_runs(conn, now):
        if run["age_hours"] is None or run["age_hours"] <= older_than_hours:
            continue
        run["finished_at"] = _last_activity(conn, run["id"], run["started_at"])
        open_steps = conn.execute(
            "SELECT COUNT(*) FROM steps WHERE run_id = ? AND status IN ('pending', 'running')", (run["id"],)
        ).fetchone()[0]
        run["steps_closed"] = open_steps
        if not dry_run:
            with conn:
                changed = conn.execute(
                    "UPDATE runs SET status = 'aborted', reason = ?, finished_at = ? WHERE id = ? AND status = 'running'",
                    (ABANDONED, run["finished_at"], run["id"]),
                ).rowcount
                if not changed:
                    continue  # It finished while we looked.
                conn.execute("UPDATE steps SET status = 'error', message = ? WHERE run_id = ? AND status = 'running'", (ABANDONED, run["id"]))
                conn.execute(
                    "UPDATE steps SET status = 'skipped', message = ? WHERE run_id = ? AND status = 'pending'",
                    (f"not run: {ABANDONED}", run["id"]),
                )
                audit.record(
                    conn,
                    "run.mark-abandoned",
                    f"run:{run['uuid']}",
                    {"run_id": run["id"], "serial": run["serial"], "started_at": run["started_at"], "age_hours": run["age_hours"], "steps_closed": open_steps},
                    actor,
                )
        marked.append(run)
    return marked


# Commands --------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def cmd_info(args: argparse.Namespace) -> int:
    with open_store(_existing(args.db)) as store:
        info = database_info(store.conn, args.db)
    if args.json:
        say(json.dumps(info, indent=2))
        return EXIT_PASS
    by_status = ", ".join(f"{number} {status}" for status, number in info["runs_by_status"].items())
    say(f"Database:        {info['path']}")
    say(f"Schema version:  {info['schema_version']}")
    say(f"Database id:     {info['database_id'] or '(none)'}")
    say(f"Size:            {_size_text(info['size_bytes'])}")
    say(f"Runs:            {info['runs']} ({by_status}); {info['imported_runs']} imported")
    for key in ("instruments", "stations", "products", "lots"):
        say(f"{key.capitalize() + ':':<17}{info[key]}")
    for label, key in (("Oldest run:", "oldest_run"), ("Newest run:", "newest_run")):
        run = info[key]
        say(f"{label:<17}" + ("none" if run is None else f"run {run['id']}, serial {run['serial']}, started {run['started_at']}"))
    return EXIT_PASS


def cmd_check(args: argparse.Namespace) -> int:
    with open_store(_existing(args.db)) as store:
        result = check_database(store.conn, args.older_than, _now())
    if args.json:
        say(json.dumps(result, indent=2))
        return EXIT_PASS if result["ok"] else EXIT_FAIL
    if result["running"]:
        say("Runs still marked running:")
        rows = [(r["id"], r["serial"], r["plan_name"], r["started_at"][:19].replace("T", " "), "" if r["age_hours"] is None else f"{r['age_hours']:.1f}") for r in result["running"]]
        say(table(("run", "serial", "plan", "started (UTC)", "age (hours)"), rows))
        say()
    if result["ok"]:
        say(f"{args.db}: no problems found")
        return EXIT_PASS
    say(f"{args.db}: {count(len(result['problems']), 'problem')}")
    for problem in result["problems"]:
        say(f"  {problem}")
    if any("marked running for" in problem for problem in result["problems"]):
        say(f"Runs left running by a process that ended can be closed with: benchrun db mark-abandoned --older-than {args.older_than:g}")
    return EXIT_FAIL


def cmd_backup(args: argparse.Namespace) -> int:
    _existing(args.db)
    out = Path(args.out)
    if out.exists() and out.resolve() == Path(args.db).resolve():
        raise PlanError([f"{args.out} is the database itself; choose another file"])
    if out.exists() and not args.force:
        raise PlanError([f"{args.out} already exists; choose another name or add --force to replace it"])
    if not out.parent.is_dir():
        raise PlanError([f"the folder {out.parent} does not exist"])
    temporary = out.with_name(f".{out.name}.{os.getpid()}.tmp")
    try:
        with open_store(args.db) as store:
            copy = sqlite3.connect(str(temporary))
            try:
                store.conn.backup(copy)  # SQLite's online backup: other processes may go on writing.
                verdict = copy.execute("PRAGMA quick_check").fetchone()[0]
                runs = copy.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            finally:
                copy.close()
        if verdict != "ok":
            raise BenchrunError(f"the copy failed its check ({verdict}); {args.out} was not written")
        with temporary.open("rb+") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, out)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass
    say(f"Backed up {args.db} to {args.out}: {count(runs, 'run')}, {_size_text(out.stat().st_size)}")
    return EXIT_PASS


def cmd_vacuum(args: argparse.Namespace) -> int:
    before = _file_size(_existing(args.db))
    with open_store(args.db) as store:
        store.conn.execute("VACUUM")
    after = _file_size(args.db)
    say(f"Vacuumed {args.db}: {_size_text(before)} before, {_size_text(after)} after")
    return EXIT_PASS


def cmd_mark_abandoned(args: argparse.Namespace) -> int:
    actor = actor_of(args)
    with open_store(_existing(args.db)) as store:
        marked = mark_abandoned(store.conn, args.older_than, _now(), args.dry_run, actor)
    if args.json:
        say(json.dumps({"dry_run": args.dry_run, "reason": ABANDONED, "marked_at": now_iso(), "runs": marked}, indent=2))
        return EXIT_PASS
    if not marked:
        say(f"No run has been marked running for more than {args.older_than:g} hours.")
        return EXIT_PASS
    rows = [(r["id"], r["serial"], r["plan_name"], r["started_at"][:19].replace("T", " "), f"{r['age_hours']:.1f}", r["steps_closed"]) for r in marked]
    say(table(("run", "serial", "plan", "started (UTC)", "age (hours)", "steps closed"), rows))
    if args.dry_run:
        say(f"Dry run: would mark {count(len(marked), 'run')} aborted ({ABANDONED}); nothing was changed.")
    else:
        say(f"Marked {count(len(marked), 'run')} aborted ({ABANDONED}).")
    return EXIT_PASS


def add_commands(subparsers: Any) -> None:
    db = subparsers.add_parser(
        "db",
        help="look after a results database: info, check, backup, vacuum, mark-abandoned",
        description="Look after a results database.",
        epilog="Exit codes: 0 done, 1 db check found problems, 2 bad arguments, 3 could not complete.",
    )
    commands = db.add_subparsers(dest="db_command", required=True, help="what to do with the database")

    def command(name: str, help_text: str, func: Any) -> argparse.ArgumentParser:
        parser = commands.add_parser(name, help=help_text, description=help_text[0].upper() + help_text[1:] + ".")
        parser.add_argument("--db", default=DEFAULT_DB, help=f"results database (default: {DEFAULT_DB})")
        parser.set_defaults(func=func)
        return parser

    info = command("info", "show what a results database holds", cmd_info)
    info.add_argument("--json", action="store_true", help="machine-readable output")

    check = command("check", "check integrity, foreign keys, orphaned rows and runs left running; exit 1 if anything is wrong", cmd_check)
    check.add_argument(
        "--older-than",
        type=positive_number,
        default=DEFAULT_STALE_HOURS,
        metavar="HOURS",
        help=f"a run marked running for longer than this is a problem (default: {DEFAULT_STALE_HOURS:g})",
    )
    check.add_argument("--json", action="store_true", help="machine-readable output")

    backup = command("backup", "copy the database safely while it may be in use", cmd_backup)
    backup.add_argument("--out", required=True, help="the copy to write")
    backup.add_argument("--force", action="store_true", help="replace the file if it already exists")

    command("vacuum", "rebuild the database file to give back unused space", cmd_vacuum)

    abandoned = command("mark-abandoned", "close runs left running by a process that ended, as aborted", cmd_mark_abandoned)
    abandoned.add_argument("--older-than", type=positive_number, required=True, metavar="HOURS", help="only runs that started more than this many hours ago")
    abandoned.add_argument("--dry-run", action="store_true", help="list the runs and change nothing")
    abandoned.add_argument("--json", action="store_true", help="machine-readable output")
    add_actor_arg(abandoned)
