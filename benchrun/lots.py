"""Lots: batches of units of one plan, or of one product, tested and counted together.

A lot records the plan's name, not its file, and for a product lot the part number.
Every run added to a lot must be of that plan and that product, so a lot's yield is
always one population. Yield, failures and histograms come from ``benchrun.stats``.
"""

from __future__ import annotations

import itertools
import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Set

from benchrun import audit
from benchrun.errors import PlanError
from benchrun.inventory import writing
from benchrun.stats import query
from benchrun.store import now_iso

MAX_NAME_CHARS = 64
OPEN, CLOSED = "open", "closed"
_COLUMNS = "id, name, product, plan_name, quantity, status, note, created_at, closed_at"


def check_name(text: str) -> str:
    if not text or len(text) > MAX_NAME_CHARS or not text.isprintable() or text != text.strip():
        raise PlanError([f"lot name {text!r} must be 1 to {MAX_NAME_CHARS} printable characters, with no space at either end"])
    return text


def subject(lot: Dict[str, Any]) -> str:
    return f"lot:{lot['name']}"


# Reading ---------------------------------------------------------------


def get(conn: sqlite3.Connection, name: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(f"SELECT {_COLUMNS} FROM lots WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    return None if row is None else dict(row)


def lookup(conn: sqlite3.Connection, text: str) -> Optional[Dict[str, Any]]:
    """A lot by its name, or by its numeric id when no lot has that name; None if neither."""
    lot = get(conn, text)
    if lot is None and text.isascii() and text.isdigit() and len(text) <= 18:  # Longer would not fit an SQLite integer.
        row = conn.execute(f"SELECT {_COLUMNS} FROM lots WHERE id = ?", (int(text),)).fetchone()
        lot = None if row is None else dict(row)
    return lot


def find(conn: sqlite3.Connection, text: str) -> Dict[str, Any]:
    """A lot by its name or numeric id, as every command that takes a lot accepts it."""
    lot = lookup(conn, text)
    if lot is None:
        raise PlanError([f"no lot named {text!r} in this database; see: benchrun lot list"])
    return lot


def name_of(conn: sqlite3.Connection, lot_id: Optional[int]) -> str:
    """The name of the lot a run is in, or "" for none."""
    row = None if lot_id is None else conn.execute("SELECT name FROM lots WHERE id = ?", (lot_id,)).fetchone()
    return "" if row is None else row[0]


def find_open(conn: sqlite3.Connection, text: str) -> Dict[str, Any]:
    lot = find(conn, text)
    if lot["status"] != OPEN:
        raise PlanError([f"lot {lot['name']} is closed; reopen it first with: benchrun lot reopen {lot['name']}"])
    return lot


def running_runs(conn: sqlite3.Connection, lot_id: int) -> List[int]:
    return [row[0] for row in conn.execute("SELECT id FROM runs WHERE lot_id = ? AND status = 'running' ORDER BY id", (lot_id,))]


def passed_serials(conn: sqlite3.Connection, lot_id: int) -> Set[str]:
    """Serials with a passing run of the whole plan in this lot."""
    rows = conn.execute("SELECT DISTINCT serial FROM runs WHERE lot_id = ? AND status = 'pass' AND selected_steps IS NULL", (lot_id,))
    return {row[0] for row in rows}


def failed_measurements(conn: sqlite3.Connection, run_id: int) -> List[str]:
    """``step.measurement`` for each measurement that failed in a run, in the order first seen."""
    rows = conn.execute(
        "SELECT s.name AS step, m.name AS name FROM measurements m JOIN steps s ON s.id = m.step_id"
        " WHERE m.run_id = ? AND m.verdict = 'fail' GROUP BY s.name, m.name ORDER BY MIN(m.id)",
        (run_id,),
    )
    return [f"{row['step']}.{row['name']}" for row in rows]


def check_unit(lot: Dict[str, Any], plan_name: str, product: str) -> None:
    """Refuse a run that would mix another plan or another product into the lot."""
    problems = []
    if lot["plan_name"] and plan_name != lot["plan_name"]:
        problems.append(f"lot {lot['name']} is tested with plan {lot['plan_name']!r}; this plan is {plan_name!r}")
    if product != lot["product"]:
        wanted = f"product {lot['product']}" if lot["product"] else "no product"
        problems.append(f"lot {lot['name']} is for {wanted}; this run is for {f'product {product}' if product else 'no product'}")
    if problems:
        raise PlanError(problems)


def serial_problem(lot: Dict[str, Any], serial: str, passed: Set[str], retest: bool) -> Optional[str]:
    """Why a serial cannot be tested in the lot now, if it cannot. ``passed`` is from ``passed_serials``."""
    if not retest and serial in passed:
        return f"serial {serial} already passed in lot {lot['name']}; add --retest to test it again"
    return None


# Changing --------------------------------------------------------------


def create(
    conn: sqlite3.Connection, name: str, product: str, plan_name: str, quantity: Optional[int], note: str, actor: str
) -> Dict[str, Any]:
    with writing(conn):
        existing = get(conn, name)
        if existing is not None:  # Lot names are unique ignoring case.
            taken = existing["name"]
            raise PlanError([f"there is already a lot named {taken}; choose another name, or see: benchrun lot show {taken}"])
        conn.execute(
            "INSERT INTO lots (name, product, plan_name, quantity, status, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, product, plan_name, quantity, OPEN, note, now_iso()),
        )
        lot = get(conn, name)
        assert lot is not None
        detail = {"product": product, "plan_name": plan_name, "quantity": quantity, "note": note}
        audit.record(conn, "lot.create", subject(lot), detail, actor)
    return lot


def close(conn: sqlite3.Connection, lot: Dict[str, Any], force: bool, actor: str) -> List[int]:
    """Close a lot. Refused while one of its runs is still running, unless ``force``; returns those runs."""
    with writing(conn):
        current = get(conn, lot["name"])
        if current is None or current["status"] != OPEN:
            raise PlanError([f"lot {lot['name']} is not open"])
        running = running_runs(conn, lot["id"])
        if running and not force:
            ids = ", ".join(str(run_id) for run_id in running)
            raise PlanError(
                [
                    f"lot {lot['name']} has runs still running: {ids}. Let them finish, or mark runs whose process "
                    "ended with benchrun db mark-abandoned; add --force to close the lot anyway"
                ]
            )
        conn.execute("UPDATE lots SET status = ?, closed_at = ? WHERE id = ?", (CLOSED, now_iso(), lot["id"]))
        detail: Dict[str, Any] = {"forced": bool(running)}
        if running:
            detail["running_runs"] = running
        audit.record(conn, "lot.close", subject(lot), detail, actor)
    return running


def reopen(conn: sqlite3.Connection, lot: Dict[str, Any], actor: str) -> None:
    with writing(conn):
        current = get(conn, lot["name"])
        if current is None or current["status"] != CLOSED:
            raise PlanError([f"lot {lot['name']} is already open"])
        conn.execute("UPDATE lots SET status = ?, closed_at = NULL WHERE id = ?", (OPEN, lot["id"]))
        audit.record(conn, "lot.reopen", subject(lot), {"closed_at": current["closed_at"]}, actor)


# Summaries -------------------------------------------------------------


def _yield(entries: Sequence[Dict[str, Any]], quantity: Optional[int]) -> Dict[str, Any]:
    """The statistics engine's yield entries for one lot, added up. A lot normally holds one plan."""
    keys = ("serials", "units_with_verdict", "first_pass_units", "final_pass_units", "retests", "retested_units")
    units = {key: sum(entry[key] for entry in entries) for key in keys}
    tested = units["units_with_verdict"]
    units["first_pass_yield"] = units["first_pass_units"] / tested if tested else None
    units["final_yield"] = units["final_pass_units"] / tested if tested else None
    units["quantity"] = quantity
    units["to_test"] = None if quantity is None else max(0, quantity - tested)
    return units


def units(conn: sqlite3.Connection, lot: Dict[str, Any]) -> Dict[str, Any]:
    """Units tested, still to test, and yield, for one lot."""
    return _yield(query.yield_by_plan(conn, query.Selection(lot_id=lot["id"])), lot["quantity"])


def summary(conn: sqlite3.Connection, lot: Dict[str, Any]) -> Dict[str, Any]:
    """Runs, units, yield and failures by measurement for one lot."""
    selection = query.Selection(lot_id=lot["id"])
    entries = query.yield_by_plan(conn, selection)
    counts = {status: sum(entry["counts"][status] for entry in entries) for status in ("pass", "fail", "aborted", "running")}
    return {
        "lot": dict(lot),
        "runs": dict(query.run_totals(entries), counts=counts),
        "units": _yield(entries, lot["quantity"]),
        "running_runs": running_runs(conn, lot["id"]),
        "pareto": {
            "failed_runs": counts["fail"],
            "complete_runs": counts["pass"] + counts["fail"],
            "rows": query.failure_pareto(conn, selection),
        },
    }


def listing(conn: sqlite3.Connection, status: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every lot, newest first, with its run count and yield."""
    where, params = ("WHERE status = ?", [status]) if status else ("", [])
    lots = [dict(row) for row in conn.execute(f"SELECT {_COLUMNS} FROM lots {where} ORDER BY id DESC", params)]
    for lot in lots:
        entries = query.yield_by_plan(conn, query.Selection(lot_id=lot["id"]))
        lot["runs"] = query.run_totals(entries)["selected"]
        lot["units"] = _yield(entries, lot["quantity"])
    return lots


def unit_results(conn: sqlite3.Connection, lot_id: int) -> List[Dict[str, Any]]:
    """One entry per serial in the lot: every result in order, and the first and final verdicts."""
    failing: Dict[int, List[str]] = {}
    for row in conn.execute(
        "SELECT m.run_id, s.name AS step, m.name AS name FROM measurements m JOIN steps s ON s.id = m.step_id"
        " JOIN runs r ON r.id = m.run_id WHERE r.lot_id = ? AND m.verdict = 'fail'"
        " GROUP BY m.run_id, s.name, m.name ORDER BY m.run_id, MIN(m.id)",
        (lot_id,),
    ):
        failing.setdefault(row["run_id"], []).append(f"{row['step']}.{row['name']}")
    rows = conn.execute(
        "SELECT id, serial, status, selected_steps, started_at FROM runs WHERE lot_id = ? ORDER BY serial, started_at, id", (lot_id,)
    )
    results = []
    for serial, group in itertools.groupby(rows, key=lambda row: row["serial"]):
        runs = list(group)
        complete = [r for r in runs if r["status"] in ("pass", "fail") and r["selected_steps"] is None]
        last = runs[-1]
        final = complete[-1] if complete else None
        results.append(
            {
                "serial": serial,
                "runs": len(runs),
                "results": [r["status"] + ("" if r["selected_steps"] is None else " (some steps)") for r in runs],
                "first_result": complete[0]["status"] if complete else None,
                "final_result": final["status"] if final else None,
                "last_run": last["id"],
                "last_status": last["status"],
                "last_started_at": last["started_at"],
                "failed_measurements": failing.get(final["id"], []) if final else [],
            }
        )
    return results
