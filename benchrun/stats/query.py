"""Reading recorded runs for statistics, a row at a time, without loading any run's plan source.

Measurements count only from complete runs: those that reached a verdict (pass or
fail) on every step of the plan. An aborted run says nothing about the unit, a run
still going has no result, and a run of only some steps was not the whole test.
Each of those is counted, and reported, apart.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import math
import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple, Union

from benchrun.stats import engine

COMPLETE = "r.status IN ('pass', 'fail') AND r.selected_steps IS NULL"
_STATUSES = ("pass", "fail", "aborted", "running")
_CHUNK = 500  # Run ids per IN (...) list, well under SQLite's limit on parameters.


@dataclass(frozen=True)
class Selection:
    """Which runs and measurements to look at. Every field left as None matches everything."""

    plan: Optional[str] = None
    product: Optional[str] = None
    station: Optional[str] = None
    lot_id: Optional[int] = None
    serial: Optional[str] = None  # Exactly this serial.
    serial_prefix: Optional[str] = None
    since: Optional[str] = None  # UTC, in the form the database stores.
    until: Optional[str] = None
    step: Optional[str] = None
    measurement: Optional[str] = None
    limit_runs: Optional[int] = None  # Only the newest N matching runs, by start time.

    def runs_cte(self) -> Tuple[str, List[Any]]:
        """``WITH sel AS (...)``: the chosen runs, for the queries below to join on."""
        clauses: List[str] = []
        params: List[Any] = []
        for column, value in (
            ("plan_name", self.plan),
            ("lot_id", self.lot_id),
            ("serial", self.serial),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        for column, value in (("product", self.product), ("station", self.station)):
            if value is not None:  # Part numbers and station names are unique ignoring case.
                clauses.append(f"{column} = ? COLLATE NOCASE")
                params.append(value)
        if self.serial_prefix is not None:
            # Not LIKE, which ignores case and treats % and _ in a serial as wildcards.
            clauses.append("substr(serial, 1, ?) = ?")
            params.extend([len(self.serial_prefix), self.serial_prefix])
        if self.since is not None:
            clauses.append("started_at >= ?")
            params.append(self.since)
        if self.until is not None:
            clauses.append("started_at <= ?")
            params.append(self.until)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        tail = ""
        if self.limit_runs is not None:
            tail = " ORDER BY started_at DESC, id DESC LIMIT ?"
            params.append(self.limit_runs)
        cte = f"WITH sel AS (SELECT id, serial, plan_name, status, selected_steps, started_at FROM runs{where}{tail}) "
        return cte, params

    def measurement_filter(self) -> Tuple[str, List[Any]]:
        clauses, params = "", []
        if self.step is not None:
            clauses += " AND s.name = ?"
            params.append(self.step)
        if self.measurement is not None:
            clauses += " AND m.name = ?"
            params.append(self.measurement)
        return clauses, params


class _Chosen:
    """A selection whose runs were already written to a temporary table, so later queries skip the search."""

    def __init__(self, selection: Selection):
        self.selection = selection

    def runs_cte(self) -> Tuple[str, List[Any]]:
        return f"WITH sel AS (SELECT * FROM temp.{_TABLE}) ", []

    def measurement_filter(self) -> Tuple[str, List[Any]]:
        return self.selection.measurement_filter()


Chosen = Union[Selection, _Chosen]
_TABLE = "benchrun_stats_runs"


@contextlib.contextmanager
def chosen_runs(conn: sqlite3.Connection, selection: Selection) -> Iterator[_Chosen]:
    """Find the runs once, for several queries. Only the connection's own temporary database is written."""
    cte, params = selection.runs_cte()
    with conn:
        conn.execute(f"DROP TABLE IF EXISTS temp.{_TABLE}")
        conn.execute(
            f"CREATE TEMP TABLE {_TABLE} (id INTEGER PRIMARY KEY, serial TEXT, plan_name TEXT, status TEXT,"
            " selected_steps TEXT, started_at TEXT)"
        )
        conn.execute(cte + f"INSERT INTO temp.{_TABLE} SELECT * FROM sel", params)
    try:
        yield _Chosen(selection)
    finally:
        with conn:
            conn.execute(f"DROP TABLE IF EXISTS temp.{_TABLE}")


def _empty_yield(plan: str) -> Dict[str, Any]:
    return {
        "plan": plan,
        "runs": 0,
        "counts": {status: 0 for status in _STATUSES},
        "partial_runs": 0,
        "serials": 0,
        "units_with_verdict": 0,
        "first_pass_units": 0,
        "first_pass_yield": None,
        "final_pass_units": 0,
        "final_yield": None,
        "retests": 0,
        "retested_units": 0,
    }


def yield_by_plan(conn: sqlite3.Connection, selection: Chosen) -> List[Dict[str, Any]]:
    """Run counts and yield per plan.

    ``counts`` covers runs of the whole plan by status; runs of only some steps are
    ``partial_runs``. Yield counts each serial once. First-pass yield takes its first
    complete run, final yield its last. ``retests`` is every complete run after a
    serial's first. Aborted and unfinished runs are in ``counts`` but not in yield.
    """
    cte, params = selection.runs_cte()
    plans: Dict[str, Dict[str, Any]] = {}
    for row in conn.execute(
        cte + "SELECT plan_name, status, selected_steps IS NOT NULL AS partial, COUNT(*) AS n FROM sel"
        " GROUP BY plan_name, status, partial ORDER BY plan_name",
        params,
    ):
        entry = plans.setdefault(row["plan_name"], _empty_yield(row["plan_name"]))
        entry["runs"] += row["n"]
        if row["partial"]:
            entry["partial_runs"] += row["n"]
        else:
            entry["counts"][row["status"]] = entry["counts"].get(row["status"], 0) + row["n"]
    for row in conn.execute(cte + "SELECT plan_name, COUNT(DISTINCT serial) AS n FROM sel GROUP BY plan_name", params):
        plans[row["plan_name"]]["serials"] = row["n"]
    # Sorted by serial, so only one serial's runs are in hand at a time.
    rows = conn.execute(
        cte + "SELECT plan_name, serial, status FROM sel r WHERE " + COMPLETE + " ORDER BY plan_name, serial, started_at, id",
        params,
    )
    for (plan, _serial), runs in itertools.groupby(rows, key=lambda row: (row["plan_name"], row["serial"])):
        verdicts = [row["status"] for row in runs]
        entry = plans[plan]
        entry["units_with_verdict"] += 1
        entry["first_pass_units"] += verdicts[0] == "pass"
        entry["final_pass_units"] += verdicts[-1] == "pass"
        entry["retests"] += len(verdicts) - 1
        entry["retested_units"] += len(verdicts) > 1
    for entry in plans.values():
        units = entry["units_with_verdict"]
        if units:
            entry["first_pass_yield"] = entry["first_pass_units"] / units
            entry["final_yield"] = entry["final_pass_units"] / units
    return list(plans.values())


def plan_summary(conn: sqlite3.Connection, plan: str) -> Dict[str, Any]:
    with chosen_runs(conn, Selection(plan=plan)) as chosen:
        found = yield_by_plan(conn, chosen)
    return found[0] if found else _empty_yield(plan)


def run_totals(entries: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    """What was selected and what was left out, across every plan's yield entry."""
    totals = {"selected": 0, "complete": 0, "aborted": 0, "running": 0, "partial": 0}
    for entry in entries:
        totals["selected"] += entry["runs"]
        totals["complete"] += entry["counts"]["pass"] + entry["counts"]["fail"]
        totals["aborted"] += entry["counts"]["aborted"]
        totals["running"] += entry["counts"]["running"]
        totals["partial"] += entry["partial_runs"]
    return totals


def measurement_groups(conn: sqlite3.Connection, selection: Chosen) -> List[Dict[str, Any]]:
    """Values from complete runs, grouped by plan, step, name, units, sweep point and limits, in run order.

    A value measured at 7 V and at 20 V are different populations, so each sweep point
    is its own group. When the limits changed between runs, each set of limits is its
    own group too, numbered by ``limit_set`` of ``limit_sets``, because capability only
    means something against one set. A missing or non-finite value, such as an
    overload, counts in ``no_value`` and never in the statistics.
    """
    cte, params = selection.runs_cte()
    extra, extra_params = selection.measurement_filter()
    groups: Dict[tuple, Dict[str, Any]] = {}
    rows = conn.execute(
        cte + "SELECT r.plan_name, s.name AS step, m.name, m.units, m.point, m.low, m.high, m.value, m.verdict, m.run_id"
        " FROM sel r JOIN measurements m ON m.run_id = r.id JOIN steps s ON s.id = m.step_id"
        f" WHERE {COMPLETE}{extra} ORDER BY r.started_at, r.id, m.id",
        params + extra_params,
    )
    for row in rows:
        key = (row["plan_name"], row["step"], row["name"], row["units"], row["point"], row["low"], row["high"])
        group = groups.get(key)
        if group is None:
            group = groups[key] = {
                "plan": row["plan_name"],
                "step": row["step"],
                "name": row["name"],
                "units": row["units"],
                "point": json.loads(row["point"]),
                "low": row["low"],
                "high": row["high"],
                "readings": 0,
                "no_value": 0,
                "fails": 0,
                "values": [],
                "run_ids": [],
            }
        group["readings"] += 1
        group["fails"] += row["verdict"] == "fail"
        value = row["value"]
        if value is None or not math.isfinite(value):
            group["no_value"] += 1
        else:
            group["values"].append(value)
            group["run_ids"].append(row["run_id"])
    # Number the limit sets of each measurement in the order they first appeared.
    sets: Dict[tuple, List[Dict[str, Any]]] = {}
    for key, group in groups.items():
        sets.setdefault(key[:5], []).append(group)
    for siblings in sets.values():
        for number, group in enumerate(siblings, 1):
            group["limit_set"], group["limit_sets"] = number, len(siblings)
    return list(groups.values())


def measurement_summary(group: Dict[str, Any]) -> Dict[str, Any]:
    """One group's identity, counts, descriptive statistics and capability, without its raw values."""
    summary = {key: value for key, value in group.items() if key not in ("values", "run_ids")}
    summary.update(engine.summarize(group["values"]))
    summary["capability"] = engine.capability(group["values"], group["low"], group["high"])
    return summary


def failure_pareto(conn: sqlite3.Connection, selection: Chosen) -> List[Dict[str, Any]]:
    """Failing measurements in complete runs, by plan, step and name, most runs first.

    ``runs`` counts each run once however many sweep points failed; ``readings``
    counts every failing reading. ``share`` and ``cumulative`` are of all ``runs``.
    """
    cte, params = selection.runs_cte()
    extra, extra_params = selection.measurement_filter()
    rows = [
        dict(row)
        for row in conn.execute(
            cte + "SELECT r.plan_name AS plan, s.name AS step, m.name, COUNT(DISTINCT m.run_id) AS runs, COUNT(*) AS readings"
            " FROM sel r JOIN measurements m ON m.run_id = r.id JOIN steps s ON s.id = m.step_id"
            f" WHERE {COMPLETE} AND m.verdict = 'fail'{extra}"
            " GROUP BY r.plan_name, s.name, m.name ORDER BY runs DESC, readings DESC, plan, step, m.name",
            params + extra_params,
        )
    ]
    total = sum(row["runs"] for row in rows)
    running = 0
    for row in rows:
        running += row["runs"]
        row["share"] = row["runs"] / total
        row["cumulative"] = running / total
    return rows


def run_labels(conn: sqlite3.Connection, chosen: _Chosen, run_ids: Sequence[int]) -> Dict[int, Tuple[str, str]]:
    """Serial and start time for each of these chosen runs."""
    labels: Dict[int, Tuple[str, str]] = {}
    unique = sorted(set(run_ids))
    for start in range(0, len(unique), _CHUNK):
        chunk = unique[start : start + _CHUNK]
        marks = ", ".join("?" * len(chunk))
        for row in conn.execute(f"SELECT id, serial, started_at FROM temp.{_TABLE} WHERE id IN ({marks})", chunk):
            labels[row["id"]] = (row["serial"], row["started_at"])
    return labels


def measurement_stats(conn: sqlite3.Connection, plan: str, serial: Optional[str] = None) -> List[Dict[str, Any]]:
    """The rows ``history --stats`` shows. Its capability index is Ppk, from the overall sigma."""
    rows = []
    for group in measurement_groups(conn, Selection(plan=plan, serial=serial)):
        summary = measurement_summary(group)
        rows.append(
            {
                "step": summary["step"],
                "name": summary["name"],
                "point": summary["point"],
                "units": summary["units"],
                "low": summary["low"],
                "high": summary["high"],
                "fails": summary["fails"],
                "count": summary["count"],
                "mean": summary["mean"],
                "stdev": summary["stdev"],
                "minimum": summary["minimum"],
                "maximum": summary["maximum"],
                "ppk": summary["capability"]["ppk"],
            }
        )
    return rows
