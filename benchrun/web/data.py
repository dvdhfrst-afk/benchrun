"""What the pages and the API read from the results database, always through a read-only connection.

Every request opens its own connection, so the threads of the server never share one,
and a run being recorded by another process is seen as soon as it commits.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union

from benchrun import inventory, lots, products, stations
from benchrun.archive import RUN_STATUSES
from benchrun.console import check_label, check_serial, utc_time
from benchrun.errors import PlanError
from benchrun.stats import query as stats_query
from benchrun.store import SCHEMA_VERSION, Store

READ_WAIT_S = 5.0  # A run being recorded holds the write lock only for a moment.
MAX_PER_PAGE = 500
DEFAULT_PER_PAGE = 50
MAX_TEXT = 200  # Longest filter value accepted from a request.


def connect_read_only(path: Union[str, Path]) -> sqlite3.Connection:
    """A connection that cannot change the file. Its own temporary tables, which statistics use, still work."""
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=READ_WAIT_S, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    except sqlite3.Error:
        conn.close()
        raise
    if version != SCHEMA_VERSION:
        conn.close()
        raise sqlite3.DatabaseError(
            f"results database {path} has schema {version}, not {SCHEMA_VERSION}; restart benchrun serve to upgrade it"
        )
    return conn


class ReadOnlyStore(Store):
    """A ``Store`` over a read-only connection someone else closes, for the readers that take a store."""

    def __init__(self, conn: sqlite3.Connection):  # The parent would open the file for writing.
        self.path = ""
        self.upgrade_note = None
        self.conn = conn


# Choosing runs -------------------------------------------------------------


@dataclass(frozen=True)
class RunFilter:
    """Which runs a list, an export or an archive covers. Every field left as None matches everything."""

    serial: Optional[str] = None
    plan: Optional[str] = None
    product: Optional[str] = None
    station: Optional[str] = None
    lot: Optional[int] = None
    status: Optional[str] = None
    since: Optional[str] = None  # As given, YYYY-MM-DD[THH:MM], UTC.
    until: Optional[str] = None

    def where(self) -> Tuple[str, List[Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        for column, value in (
            ("serial", self.serial),
            ("plan_name", self.plan),
            ("lot_id", self.lot),
            ("status", self.status),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        for column, value in (("product", self.product), ("station", self.station)):
            if value is not None:  # Part numbers and station names are unique ignoring case.
                clauses.append(f"{column} = ? COLLATE NOCASE")
                params.append(value)
        since = utc_time(self.since, "since")
        until = utc_time(self.until, "until", end_of_day=True)
        if since is not None:
            clauses.append("started_at >= ?")
            params.append(since)
        if until is not None:
            clauses.append("started_at <= ?")
            params.append(until)
        return (f" WHERE {' AND '.join(clauses)}" if clauses else ""), params

    def params(self) -> Dict[str, str]:
        """The filter as query parameters, for links that keep it."""
        return {f.name: str(getattr(self, f.name)) for f in fields(self) if getattr(self, f.name) is not None}


def text_param(query: Mapping[str, str], name: str, limit: int = MAX_TEXT) -> Optional[str]:
    value = query.get(name, "").strip()
    if not value:
        return None
    check_label(value, name)
    if len(value) > limit:
        raise PlanError([f"{name} must be at most {limit} characters"])
    return value


def whole_param(query: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    text = query.get(name, "").strip()
    if not text:
        return default
    if len(text) > 12 or not (text.isascii() and text.isdigit()) or not low <= int(text) <= high:
        raise PlanError([f"{name} must be a whole number from {low} to {high}"])
    return int(text)


def time_params(query: Mapping[str, str]) -> Tuple[Optional[str], Optional[str]]:
    """``since`` and ``until`` from a request, in the form the database stores."""
    since = utc_time(text_param(query, "since", 19), "since")
    return since, utc_time(text_param(query, "until", 19), "until", end_of_day=True)


def run_filter(conn: sqlite3.Connection, query: Mapping[str, str]) -> RunFilter:
    serial = query.get("serial", "").strip()
    status = text_param(query, "status")
    if status is not None and status not in RUN_STATUSES:
        raise PlanError([f"status must be one of {', '.join(RUN_STATUSES)}"])
    lot = text_param(query, "lot")
    chosen = RunFilter(
        serial=check_serial(serial) if serial else None,
        plan=text_param(query, "plan"),
        product=text_param(query, "product"),
        station=text_param(query, "station"),
        lot=lots.find(conn, lot)["id"] if lot is not None else None,
        status=status,
        since=text_param(query, "since", 19),
        until=text_param(query, "until", 19),
    )
    chosen.where()  # Refuses a time that is not one before anything is read.
    return chosen


@dataclass(frozen=True)
class Paging:
    page: int
    per_page: int

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.per_page

    def describe(self, shown: int, total: Optional[int] = None, more: bool = False) -> Dict[str, Any]:
        """``has_more`` is always there; ``total`` and ``pages`` only when the total is known."""
        found: Dict[str, Any] = {"page": self.page, "per_page": self.per_page, "has_more": more}
        if total is not None:
            found["total"] = total
            found["pages"] = max(1, math.ceil(total / self.per_page))
            found["has_more"] = self.offset + shown < total
        return found


def paging(query: Mapping[str, str], per_page: int = DEFAULT_PER_PAGE) -> Paging:
    return Paging(whole_param(query, "page", 1, 1, 10**6), whole_param(query, "per_page", per_page, 1, MAX_PER_PAGE))


_RUN_COLUMNS = (
    "r.id, r.uuid, r.serial, r.plan_name, r.plan_version, r.product, r.station, r.lot_id, l.name AS lot, r.operator, r.note,"
    " r.selected_steps IS NOT NULL AS partial, r.status, r.reason, r.started_at, r.finished_at, r.imported_at"
)


def count_runs(conn: sqlite3.Connection, chosen: RunFilter) -> int:
    where, params = chosen.where()
    return int(conn.execute(f"SELECT COUNT(*) FROM runs{where}", params).fetchone()[0])


def list_runs(conn: sqlite3.Connection, chosen: RunFilter, limit: int, offset: int = 0) -> List[Dict[str, Any]]:
    """Newest first, each with its pass, fail and error counts. Choosing the page first keeps it fast."""
    where, params = chosen.where()
    rows = conn.execute(
        f"SELECT {_RUN_COLUMNS}, COUNT(m.id) AS measurements, COALESCE(SUM(m.verdict = 'pass'), 0) AS passed,"
        " COALESCE(SUM(m.verdict = 'fail'), 0) AS failed, COALESCE(SUM(m.verdict = 'error'), 0) AS errors"
        f" FROM (SELECT * FROM runs{where} ORDER BY id DESC LIMIT ? OFFSET ?) r"
        " LEFT JOIN lots l ON l.id = r.lot_id LEFT JOIN measurements m ON m.run_id = r.id GROUP BY r.id ORDER BY r.id DESC",
        params + [limit, offset],
    )
    return [_run_row(row) for row in rows]


def _run_row(row: sqlite3.Row) -> Dict[str, Any]:
    item = dict(row)
    item["partial"] = bool(item["partial"])
    return item


def run_ids(conn: sqlite3.Connection, chosen: RunFilter) -> List[int]:
    """Oldest first, the order exports and archives use."""
    where, params = chosen.where()
    return [row[0] for row in conn.execute(f"SELECT id FROM runs{where} ORDER BY id", params)]


# The dashboard and plans -----------------------------------------------------


def start_of_day(now: datetime) -> datetime:
    return now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def start_of_week(now: datetime) -> datetime:
    """Monday, 00:00 UTC."""
    day = start_of_day(now)
    return day - timedelta(days=day.weekday())


def yield_entries(conn: sqlite3.Connection, selection: stats_query.Selection) -> List[Dict[str, Any]]:
    with stats_query.chosen_runs(conn, selection) as chosen:
        return stats_query.yield_by_plan(conn, chosen)


def yield_since(conn: sqlite3.Connection, since: datetime) -> List[Dict[str, Any]]:
    return yield_entries(conn, stats_query.Selection(since=since.isoformat(timespec="milliseconds")))


def plans_seen(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Every plan that has a run, with its versions, its last run and its yield over all runs."""
    rows = conn.execute(
        "SELECT plan_name, COUNT(*) AS runs, COUNT(DISTINCT serial) AS serials, MIN(started_at) AS first_run,"
        " MAX(started_at) AS last_run FROM runs GROUP BY plan_name ORDER BY plan_name"
    ).fetchall()
    versions: Dict[str, List[str]] = {}
    for row in conn.execute("SELECT DISTINCT plan_name, plan_version FROM runs ORDER BY plan_name, plan_version"):
        versions.setdefault(row[0], []).append(row[1])
    yields = {entry["plan"]: entry for entry in yield_entries(conn, stats_query.Selection())}
    found = []
    for row in rows:
        item = dict(row)
        entry = yields.get(item["plan_name"])
        item["versions"] = versions.get(item["plan_name"], [])
        item["first_pass_yield"] = entry["first_pass_yield"] if entry else None
        item["final_yield"] = entry["final_yield"] if entry else None
        item["units_with_verdict"] = entry["units_with_verdict"] if entry else 0
        found.append(item)
    return found


def choices(conn: sqlite3.Connection) -> Dict[str, List[Any]]:
    """The values the filter forms offer."""
    return {
        "plan": [row[0] for row in conn.execute("SELECT DISTINCT plan_name FROM runs ORDER BY plan_name")],
        "product": [row[0] for row in conn.execute("SELECT part_number FROM products ORDER BY part_number")],
        "station": [row[0] for row in conn.execute("SELECT name FROM stations ORDER BY name COLLATE NOCASE")],
        "lot": [(row[0], row[1]) for row in conn.execute("SELECT id, name FROM lots ORDER BY id DESC")],
    }


# Lots ----------------------------------------------------------------------


def lot_summary(conn: sqlite3.Connection, name_or_id: str) -> Optional[Dict[str, Any]]:
    """What ``lot show`` reports, for the lot with this name or id, as the command line takes it."""
    lot = lots.lookup(conn, name_or_id)
    return None if lot is None else lots.summary(conn, lot)


# Stations, instruments, products ----------------------------------------------


def station_list(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    found = []
    for row in conn.execute("SELECT * FROM stations ORDER BY name COLLATE NOCASE").fetchall():
        item = dict(row)
        item["roles"] = stations.station_roles(conn, item["id"])
        item["runs"] = stations.station_runs(conn, item["name"])
        found.append(item)
    return found


def instrument_list(conn: sqlite3.Connection, query: Mapping[str, str]) -> List[Dict[str, Any]]:
    status = text_param(query, "status")
    if status is not None and status not in inventory.STATUSES:
        raise PlanError([f"status must be one of {', '.join(inventory.STATUSES)}"])
    found = inventory.find_instruments(conn, driver=text_param(query, "driver"), status=status, search=text_param(query, "search"))
    for item in found:
        item["stations"] = [{"station": name, "role": role} for name, role in inventory.stations_using(conn, item["id"])]
    return found


def instrument_detail(conn: sqlite3.Connection, tag: str, recent: int = 50) -> Optional[Dict[str, Any]]:
    item = inventory.get_instrument(conn, tag)
    if item is None:
        return None
    item["stations"] = [{"station": name, "role": role} for name, role in inventory.stations_using(conn, item["id"])]
    item["recent_runs"] = inventory.runs_using(conn, item["asset_tag"], recent)
    return item


def product_detail(conn: sqlite3.Connection, part_number: str) -> Optional[Dict[str, Any]]:
    product = products.get(conn, part_number)
    if product is None:
        return None
    item = product.as_json()
    item["runs"] = products.run_count(conn, product.part_number)
    return item
