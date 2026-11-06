"""SQLite results database: runs, instruments, steps, measurements and queries."""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Union

SCHEMA_VERSION = 1

_SCHEMA = """
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
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Store:
    """Thin wrapper around a SQLite file. Every write commits immediately, so a
    crash or power loss keeps all the measurements taken up to that point."""

    def __init__(self, path: Union[str, Path]):
        self.path = str(path)
        try:
            self.conn = sqlite3.connect(self.path)
        except sqlite3.Error as exc:
            raise sqlite3.OperationalError(f"cannot open results database {self.path}: {exc}") from exc
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def _migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            with self.conn:
                self.conn.executescript(_SCHEMA)
                self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        elif version != SCHEMA_VERSION:
            raise sqlite3.DatabaseError(
                f"results database {self.path} has schema {version}; this benchrun expects {SCHEMA_VERSION}"
            )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # Writing -------------------------------------------------------------

    def create_run(
        self,
        serial: str,
        plan_name: str,
        plan_version: str,
        plan_hash: str,
        plan_source: str,
        variables: Mapping[str, float],
        operator: str,
        software_version: str,
    ) -> int:
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO runs (serial, plan_name, plan_version, plan_hash, plan_source, variables,"
                " operator, software_version, status, started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', ?)",
                (
                    serial,
                    plan_name,
                    plan_version,
                    plan_hash,
                    plan_source,
                    json.dumps(dict(variables), sort_keys=True),
                    operator,
                    software_version,
                    now_iso(),
                ),
            )
        return int(cursor.lastrowid)

    def add_instrument(self, run_id: int, name: str, driver: str, address: str) -> int:
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO run_instruments (run_id, name, driver, address) VALUES (?, ?, ?, ?)",
                (run_id, name, driver, address),
            )
        return int(cursor.lastrowid)

    def set_instrument_idn(self, row_id: int, idn: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE run_instruments SET idn = ? WHERE id = ?", (idn, row_id))

    def add_step(self, run_id: int, ordinal: int, name: str, kind: str) -> int:
        """Register a step before it starts, so unreached steps stay visible."""
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO steps (run_id, ordinal, name, kind, status) VALUES (?, ?, ?, ?, 'pending')",
                (run_id, ordinal, name, kind),
            )
        return int(cursor.lastrowid)

    def begin_step(self, step_id: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE steps SET status = 'running', started_at = ? WHERE id = ?", (now_iso(), step_id))

    def finish_step(self, step_id: int, status: str, message: Optional[str] = None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE steps SET status = ?, message = ?, finished_at = ? WHERE id = ?",
                (status, message, now_iso(), step_id),
            )

    def skip_pending_steps(self, run_id: int, message: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE steps SET status = 'skipped', message = ? WHERE run_id = ? AND status = 'pending'",
                (message, run_id),
            )

    def add_measurement(
        self,
        run_id: int,
        step_id: int,
        name: str,
        kind: str,
        point: Mapping[str, float],
        value: Optional[float],
        units: str,
        low: Optional[float],
        high: Optional[float],
        verdict: str,
        note: Optional[str] = None,
        instrument: Optional[str] = None,
        samples: int = 1,
    ) -> int:
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO measurements (run_id, step_id, name, kind, point, instrument, samples, value,"
                " units, low, high, verdict, note, taken_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    step_id,
                    name,
                    kind,
                    json.dumps(dict(point)),
                    instrument,
                    samples,
                    value,
                    units,
                    low,
                    high,
                    verdict,
                    note,
                    now_iso(),
                ),
            )
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, status: str, reason: Optional[str] = None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE runs SET status = ?, reason = ?, finished_at = ? WHERE id = ?",
                (status, reason, now_iso(), run_id),
            )

    # Reading -------------------------------------------------------------

    def find_runs(
        self,
        serial: Optional[str] = None,
        plan: Optional[str] = None,
        status: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Runs, newest first, with pass, fail and total measurement counts."""
        clauses = []
        params: List[Any] = []
        if serial is not None:
            clauses.append("r.serial = ?")
            params.append(serial)
        if plan is not None:
            clauses.append("r.plan_name = ?")
            params.append(plan)
        if status is not None:
            clauses.append("r.status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        tail = ""
        if limit is not None:
            tail = "LIMIT ?"
            params.append(limit)
        rows = self.conn.execute(
            "SELECT r.id, r.serial, r.plan_name, r.plan_version, r.plan_hash, r.operator, r.status, r.reason,"
            " r.started_at, r.finished_at,"
            " COUNT(m.id) AS measurements,"
            " COALESCE(SUM(m.verdict = 'pass'), 0) AS passed,"
            " COALESCE(SUM(m.verdict = 'fail'), 0) AS failed,"
            " COALESCE(SUM(m.verdict = 'error'), 0) AS errors"
            " FROM runs r LEFT JOIN measurements m ON m.run_id = r.id "
            f"{where} GROUP BY r.id ORDER BY r.id DESC {tail}",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def run_detail(self, run_id: int) -> Optional[Dict[str, Any]]:
        """Everything recorded about one run, ready for a report."""
        row = self.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        detail = dict(row)
        detail["variables"] = json.loads(detail["variables"])
        detail["instruments"] = [
            dict(r)
            for r in self.conn.execute("SELECT * FROM run_instruments WHERE run_id = ? ORDER BY id", (run_id,))
        ]
        measurements: Dict[int, List[Dict[str, Any]]] = {}
        for m in self.conn.execute("SELECT * FROM measurements WHERE run_id = ? ORDER BY id", (run_id,)):
            item = dict(m)
            item["point"] = json.loads(item["point"])
            measurements.setdefault(item["step_id"], []).append(item)
        steps = []
        for s in self.conn.execute("SELECT * FROM steps WHERE run_id = ? ORDER BY ordinal", (run_id,)):
            item = dict(s)
            item["measurements"] = measurements.get(item["id"], [])
            steps.append(item)
        detail["steps"] = steps
        return detail

    def plan_summary(self, plan: str) -> Dict[str, Any]:
        """Run counts and first-pass yield for a plan.

        First-pass yield counts each serial once: its first run that reached a
        verdict. Aborted runs say nothing about the unit, so they are excluded.
        """
        rows = self.conn.execute(
            "SELECT id, serial, status FROM runs WHERE plan_name = ? ORDER BY id", (plan,)
        ).fetchall()
        counts = {"pass": 0, "fail": 0, "aborted": 0, "running": 0}
        first_verdict: Dict[str, str] = {}
        for row in rows:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
            if row["status"] in ("pass", "fail") and row["serial"] not in first_verdict:
                first_verdict[row["serial"]] = row["status"]
        units = len(first_verdict)
        first_pass = sum(1 for verdict in first_verdict.values() if verdict == "pass")
        return {
            "plan": plan,
            "runs": len(rows),
            "counts": counts,
            "serials": len({row["serial"] for row in rows}),
            "units_with_verdict": units,
            "first_pass_units": first_pass,
            "first_pass_yield": first_pass / units if units else None,
        }

    def measurement_stats(self, plan: str, serial: Optional[str] = None) -> List[Dict[str, Any]]:
        """Per-measurement statistics across completed runs of a plan.

        Grouped by step, measurement and sweep point, because a value measured
        at 7 V and at 20 V are different populations, and by units and limits,
        because capability only means something against one set of limits.
        Aborted runs are excluded.
        """
        sql = (
            "SELECT s.name AS step, m.name, m.point, m.units, m.value, m.low, m.high, m.verdict"
            " FROM measurements m JOIN steps s ON s.id = m.step_id JOIN runs r ON r.id = m.run_id"
            " WHERE r.plan_name = ? AND r.status IN ('pass', 'fail')"
        )
        params: List[Any] = [plan]
        if serial is not None:
            sql += " AND r.serial = ?"
            params.append(serial)
        groups: Dict[tuple, Dict[str, Any]] = {}
        for row in self.conn.execute(sql + " ORDER BY m.id", params):
            key = (row["step"], row["name"], row["point"], row["units"], row["low"], row["high"])
            group = groups.setdefault(
                key,
                {
                    "step": row["step"],
                    "name": row["name"],
                    "point": json.loads(row["point"]),
                    "units": row["units"],
                    "low": row["low"],
                    "high": row["high"],
                    "values": [],
                    "fails": 0,
                },
            )
            if row["value"] is not None:
                group["values"].append(row["value"])
            if row["verdict"] == "fail":
                group["fails"] += 1
        return [_finish_stats(group) for group in groups.values()]


def _finish_stats(group: Dict[str, Any]) -> Dict[str, Any]:
    values = group.pop("values")
    count = len(values)
    mean = sum(values) / count if count else None
    stdev = None
    if count >= 2:
        stdev = math.sqrt(sum((v - mean) ** 2 for v in values) / (count - 1))
    cpk = None
    if stdev and (group["low"] is not None or group["high"] is not None):
        sides = []
        if group["high"] is not None:
            sides.append((group["high"] - mean) / (3.0 * stdev))
        if group["low"] is not None:
            sides.append((mean - group["low"]) / (3.0 * stdev))
        cpk = min(sides)
    group.update(
        count=count,
        mean=mean,
        stdev=stdev,
        minimum=min(values) if values else None,
        maximum=max(values) if values else None,
        cpk=cpk,
    )
    return group
