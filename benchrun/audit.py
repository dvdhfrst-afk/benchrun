"""The audit log: who changed shared records outside a run, and what changed.

Runs record themselves. Everything else that changes what a later run will do, such as
an instrument, a station or a product, writes one entry here in the same transaction.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Mapping, Optional

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
) -> List[Dict[str, Any]]:
    """Newest first. ``subject`` matches exactly, or as a prefix when it ends with ``:``, such as ``station:``."""
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
        f"SELECT id, at, actor, action, subject, detail FROM audit_log{where} ORDER BY id DESC LIMIT ?",
        (*values, max(0, int(limit))),
    ).fetchall()
    result = []
    for row in rows:
        entry = dict(zip(("id", "at", "actor", "action", "subject", "detail"), tuple(row)))
        entry["detail"] = json.loads(entry["detail"])
        result.append(entry)
    return result
