"""The audit log: who changed shared records outside a run, and what changed.

Runs record themselves. Everything else that changes what a later run will do, such as
an instrument, a station or a product, writes one entry here in the same transaction.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from benchrun.console import DEFAULT_DB, EXIT_PASS, not_negative_whole, open_store, say, table, utc_time
from benchrun.errors import PlanError
from benchrun.store import now_iso

MAX_ENTRIES = 10000


def record(conn: sqlite3.Connection, action: str, subject: str, detail: Optional[Mapping[str, Any]] = None, actor: str = "") -> None:
    """Add one entry. Call inside the caller's transaction, so the change and its entry land together."""
    conn.execute(
        "INSERT INTO audit_log (at, actor, action, subject, detail) VALUES (?, ?, ?, ?, ?)",
        (now_iso(), actor, action, subject, json.dumps(dict(detail or {}), sort_keys=True, default=str)),
    )


def entries(
    conn: sqlite3.Connection,
    subject: Optional[str] = None,
    action: Optional[str] = None,
    actor: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = MAX_ENTRIES,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """Newest first, leaving out the newest ``offset``. ``subject`` matches exactly, or as a prefix when it ends with ``:``, such as ``station:``."""
    clauses: List[str] = []
    values: List[Any] = []
    if subject is not None:
        if subject.endswith(":"):
            clauses.append("substr(subject, 1, ?) = ?")
            values += [len(subject), subject]
        else:
            clauses.append("subject = ?")
            values.append(subject)
    for column, value in (("action", action), ("actor", actor)):
        if value is not None:
            clauses.append(f"{column} = ?")
            values.append(value)
    if since is not None:
        clauses.append("at >= ?")
        values.append(since)
    if until is not None:
        clauses.append("at <= ?")
        values.append(until)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT id, at, actor, action, subject, detail FROM audit_log{where} ORDER BY id DESC LIMIT ? OFFSET ?",
        (*values, max(0, int(limit)), max(0, int(offset))),
    ).fetchall()
    result = []
    for row in rows:
        entry = dict(zip(("id", "at", "actor", "action", "subject", "detail"), tuple(row)))
        entry["detail"] = json.loads(entry["detail"])
        result.append(entry)
    return result


# The audit command ----------------------------------------------------


def cmd_audit(args: argparse.Namespace) -> int:
    if not Path(args.db).exists():
        raise PlanError([f"no results database at {args.db}"])
    since = utc_time(args.since, "--since")
    until = utc_time(args.until, "--until", end_of_day=True)
    with open_store(args.db) as store:
        found = entries(store.conn, args.subject, args.action, args.actor, since, until, args.limit)
    if args.json:
        say(json.dumps({"entries": found}, indent=2))
        return EXIT_PASS
    if not found:
        say("No matching audit entries.")
        return EXIT_PASS
    rows = [(e["id"], e["at"][:19].replace("T", " "), e["action"], e["subject"], e["actor"]) for e in found]
    lines = table(("entry", "at (UTC)", "action", "subject", "actor"), rows).splitlines()
    say("\n".join(lines[:2]))
    for line, entry in zip(lines[2:], found):
        say(line)
        if entry["detail"]:
            say("    " + ", ".join(f"{key}={json.dumps(value, sort_keys=True)}" for key, value in sorted(entry["detail"].items())))
    if len(found) == args.limit:
        say(f"Showing the newest {args.limit}; use --limit for more.")
    return EXIT_PASS


def add_commands(subparsers: Any) -> None:
    audit = subparsers.add_parser("audit", help="list changes made to instruments, stations and other shared records")
    audit.add_argument("--subject", help="only this subject, such as instrument:PSU-0001; ending in : matches every one, such as station:")
    audit.add_argument("--action", help="only this action, such as instrument.add, station.assign, product.set-limit or lot.close")
    audit.add_argument("--actor", help="only changes made by this actor; an empty value finds changes with none")
    audit.add_argument("--since", metavar="TIME", help="only entries at or after this UTC date or time, YYYY-MM-DD[THH:MM]")
    audit.add_argument("--until", metavar="TIME", help="only entries at or before this UTC date or time")
    audit.add_argument("--limit", type=not_negative_whole, default=100, help="at most this many, newest first (default: 100)")
    audit.add_argument("--json", action="store_true", help="machine-readable output")
    audit.add_argument("--db", default=DEFAULT_DB, help=f"results database (default: {DEFAULT_DB})")
    audit.set_defaults(func=cmd_audit)
