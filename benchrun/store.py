"""SQLite results database: runs, instruments, steps, measurements and queries.

The schema version lives in ``PRAGMA user_version``. A new database is built by
running the first schema and then every upgrade, so the path an old database
takes is the same path every test takes. An old database is copied first, and
the upgrade happens in one transaction, so a failure leaves it as it was.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from benchrun.stats import query as stats_query

SCHEMA_VERSION = 3
LOCK_WAIT_S = 30.0  # Another process may be writing to the same database.

_FIRST_SCHEMA = """
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

# What each schema version needs in order to become the next one.
_UPGRADES: Dict[int, Tuple[str, ...]] = {
    1: (
        "ALTER TABLE runs ADD COLUMN note TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE runs ADD COLUMN selected_steps TEXT",
        "CREATE INDEX runs_started ON runs (started_at)",
    ),
    2: (
        # Runs that can travel between databases, and what they were tested with.
        "ALTER TABLE runs ADD COLUMN uuid TEXT",
        "UPDATE runs SET uuid = lower(hex(randomblob(16))) WHERE uuid IS NULL",
        "CREATE UNIQUE INDEX runs_uuid ON runs (uuid)",
        "ALTER TABLE runs ADD COLUMN station TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE runs ADD COLUMN product TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE runs ADD COLUMN lot_id INTEGER REFERENCES lots (id)",
        "ALTER TABLE runs ADD COLUMN origin TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE runs ADD COLUMN imported_at TEXT",
        "CREATE INDEX runs_product ON runs (product)",
        "CREATE INDEX runs_lot ON runs (lot_id)",
        "ALTER TABLE run_instruments ADD COLUMN asset_tag TEXT",
        "CREATE INDEX measurements_step ON measurements (step_id)",
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        "INSERT INTO meta (key, value) VALUES ('database_id', lower(hex(randomblob(16))))",
        # The instruments a lab owns, and the stations built from them.
        """CREATE TABLE instruments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_tag TEXT NOT NULL UNIQUE,
            driver TEXT NOT NULL,
            manufacturer TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            serial_number TEXT NOT NULL DEFAULT '',
            address TEXT NOT NULL DEFAULT '',
            location TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            notes TEXT NOT NULL DEFAULT '',
            added_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE stations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL DEFAULT '',
            location TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE station_roles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            station_id INTEGER NOT NULL REFERENCES stations (id),
            role TEXT NOT NULL,
            instrument_id INTEGER NOT NULL REFERENCES instruments (id),
            timeout_s REAL,
            expect_idn TEXT,
            UNIQUE (station_id, role)
        )""",
        # Part numbers, with the plan they are tested by and what differs per part.
        """CREATE TABLE products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            part_number TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL DEFAULT '',
            plan_file TEXT NOT NULL DEFAULT '',
            serial_pattern TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE product_variables (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL REFERENCES products (id),
            name TEXT NOT NULL,
            value REAL NOT NULL,
            UNIQUE (product_id, name)
        )""",
        """CREATE TABLE product_limits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL REFERENCES products (id),
            step TEXT NOT NULL,
            measurement TEXT NOT NULL,
            low REAL,
            high REAL,
            UNIQUE (product_id, step, measurement)
        )""",
        # Batches of units tested together.
        """CREATE TABLE lots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            product TEXT NOT NULL DEFAULT '',
            plan_name TEXT NOT NULL DEFAULT '',
            quantity INTEGER,
            status TEXT NOT NULL DEFAULT 'open',
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            closed_at TEXT
        )""",
        # Who changed what outside a run.
        """CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at TEXT NOT NULL,
            actor TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL,
            subject TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '{}'
        )""",
        "CREATE INDEX audit_log_subject ON audit_log (subject)",
    ),
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Store:
    """Thin wrapper around a SQLite file. Every write commits immediately, so a
    crash or power loss keeps all the measurements taken up to that point."""

    def __init__(self, path: Union[str, Path]):
        self.path = str(path)
        self.upgrade_note: Optional[str] = None  # Set when an old database was upgraded.
        try:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(self.path, timeout=LOCK_WAIT_S)
        except (OSError, sqlite3.Error) as exc:
            raise sqlite3.OperationalError(f"cannot open results database {self.path}: {exc}") from exc
        self.conn.row_factory = sqlite3.Row
        try:
            self.conn.execute("PRAGMA foreign_keys = ON")
            self._migrate()
        except sqlite3.Error as exc:
            self.conn.close()
            if str(exc).startswith("results database"):
                raise
            raise sqlite3.DatabaseError(f"results database {self.path} cannot be used: {exc}") from exc

    def _version(self) -> int:
        return int(self.conn.execute("PRAGMA user_version").fetchone()[0])

    def _has_tables(self) -> bool:
        return self.conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] > 0

    def _migrate(self) -> None:
        version = self._version()
        if version == SCHEMA_VERSION:
            return
        if version > SCHEMA_VERSION:
            raise sqlite3.DatabaseError(
                f"results database {self.path} has schema {version}, newer than the {SCHEMA_VERSION} "
                "this benchrun knows. Upgrade benchrun; the file has not been changed."
            )
        if version == 0 and self._has_tables():
            raise sqlite3.DatabaseError(
                f"results database {self.path} is a SQLite file that benchrun did not create. "
                "Choose another file with --db; this one has not been changed."
            )
        backup = self._back_up(version) if version else None
        self._upgrade()
        if backup:
            self.upgrade_note = f"Upgraded {self.path} from schema {version} to {SCHEMA_VERSION}. Old copy: {backup}"

    def _back_up(self, version: int) -> str:
        """A full copy of the database, under a name no earlier copy holds."""
        candidate = f"{self.path}.schema{version}.bak"
        serial = 1
        while Path(candidate).exists():
            serial += 1
            candidate = f"{self.path}.schema{version}.{serial}.bak"
        target = sqlite3.connect(candidate)
        try:
            with target:
                self.conn.backup(target)
        finally:
            target.close()
        return candidate

    def _upgrade(self) -> None:
        """Bring the schema up to date in one transaction. Two processes may race here."""
        self.conn.isolation_level = None
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                current = self._version()
                if current == 0:
                    for statement in _FIRST_SCHEMA.split(";"):
                        if statement.strip():
                            self.conn.execute(statement)
                    current = 1
                while current < SCHEMA_VERSION:
                    for statement in _UPGRADES[current]:
                        self.conn.execute(statement)
                    current += 1
                self.conn.execute(f"PRAGMA user_version = {current}")
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
        finally:
            self.conn.isolation_level = ""

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
        note: str = "",
        selected_steps: Optional[Sequence[str]] = None,
        station: str = "",
        product: str = "",
        lot_id: Optional[int] = None,
    ) -> int:
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO runs (uuid, serial, plan_name, plan_version, plan_hash, plan_source, variables,"
                " operator, software_version, note, selected_steps, station, product, lot_id, status, started_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?)",
                (
                    uuid.uuid4().hex,
                    serial,
                    plan_name,
                    plan_version,
                    plan_hash,
                    plan_source,
                    json.dumps(dict(variables), sort_keys=True),
                    operator,
                    software_version,
                    note,
                    None if selected_steps is None else json.dumps(list(selected_steps)),
                    station,
                    product,
                    lot_id,
                    now_iso(),
                ),
            )
        return int(cursor.lastrowid)

    def add_instrument(self, run_id: int, name: str, driver: str, address: str, asset_tag: Optional[str] = None) -> int:
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO run_instruments (run_id, name, driver, address, asset_tag) VALUES (?, ?, ?, ?, ?)",
                (run_id, name, driver, address, asset_tag),
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

    def abort_running_steps(self, run_id: int, message: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE steps SET status = 'error', message = ? WHERE run_id = ? AND status = 'running'",
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
        instrument: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Runs, newest first, with pass, fail and total measurement counts.

        ``instrument`` is found anywhere in a recorded ``*IDN?`` response, which
        answers the question of which units a given instrument tested.
        ``since`` and ``until`` are UTC times in the form the database stores.
        """
        clauses = []
        params: List[Any] = []
        if serial is not None:
            clauses.append("serial = ?")
            params.append(serial)
        if plan is not None:
            clauses.append("plan_name = ?")
            params.append(plan)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if instrument is not None:
            clauses.append(
                "id IN (SELECT run_id FROM run_instruments WHERE instr(lower(COALESCE(idn, '')), lower(?)) > 0)"
            )
            params.append(instrument)
        if since is not None:
            clauses.append("started_at >= ?")
            params.append(since)
        if until is not None:
            clauses.append("started_at <= ?")
            params.append(until)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        tail = ""
        if limit is not None:
            tail = "LIMIT ?"
            params.append(limit)
        # Choosing the runs first keeps a short listing fast on a large database.
        rows = self.conn.execute(
            "SELECT r.id, r.serial, r.plan_name, r.plan_version, r.plan_hash, r.operator, r.note, r.selected_steps,"
            " r.status, r.reason, r.started_at, r.finished_at,"
            " COUNT(m.id) AS measurements,"
            " COALESCE(SUM(m.verdict = 'pass'), 0) AS passed,"
            " COALESCE(SUM(m.verdict = 'fail'), 0) AS failed,"
            " COALESCE(SUM(m.verdict = 'error'), 0) AS errors"
            f" FROM (SELECT * FROM runs {where} ORDER BY id DESC {tail}) r"
            " LEFT JOIN measurements m ON m.run_id = r.id GROUP BY r.id ORDER BY r.id DESC",
            params,
        ).fetchall()
        return [_decode_selection(dict(row)) for row in rows]

    def run_detail(self, run_id: int) -> Optional[Dict[str, Any]]:
        """Everything recorded about one run, ready for a report."""
        row = self.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        detail = _decode_selection(dict(row))
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
        """Run counts and yield for a plan; see ``benchrun.stats.query.yield_by_plan``."""
        return stats_query.plan_summary(self.conn, plan)

    def measurement_stats(self, plan: str, serial: Optional[str] = None) -> List[Dict[str, Any]]:
        """Per-measurement statistics across complete runs of a plan; see ``benchrun.stats.query``."""
        return stats_query.measurement_stats(self.conn, plan, serial)


def _decode_selection(row: Dict[str, Any]) -> Dict[str, Any]:
    if row.get("selected_steps") is not None:
        row["selected_steps"] = json.loads(row["selected_steps"])
    return row
