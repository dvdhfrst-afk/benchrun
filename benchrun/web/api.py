"""The JSON API under ``/api/``: the same data the pages show, for scripts and other systems.

Every answer is a JSON object. A list comes with ``paging`` where it can be long. A
value that is not a finite number, such as an overloaded reading, is null, because
JSON has no way to write it. Errors are ``{"error": {"status", "code", "message"}}``.
"""

from __future__ import annotations

import json
import math
import sqlite3
from typing import Any, Dict, Mapping

from benchrun import lots as lots_module
from benchrun import products
from benchrun.errors import PlanError
from benchrun.stats import engine
from benchrun.stats.command import build_report
from benchrun.web import data
from benchrun.web.pages import NotFound, audit_entries, stats_selection

VIEWS = ("summary", "yield", "pareto", "histogram", "control")

ENDPOINTS = (
    "/api/runs",
    "/api/runs/<id>",
    "/api/serials/<serial>",
    "/api/plans",
    "/api/stats?plan=NAME",
    "/api/lots",
    "/api/lots/<id>",
    "/api/stations",
    "/api/instruments",
    "/api/instruments/<asset tag>",
    "/api/products",
    "/api/products/<part number>",
    "/api/audit",
)


def finite_only(value: Any) -> Any:
    """The same structure with every infinite or NaN number replaced by None."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: finite_only(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [finite_only(item) for item in value]
    return value


def encode(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(finite_only(dict(payload)), indent=2, allow_nan=False) + "\n").encode("utf-8")


def error_body(status: int, code: str, message: str) -> bytes:
    return encode({"error": {"status": status, "code": code, "message": message}})


def index(conn: sqlite3.Connection, query: Mapping[str, str]) -> Dict[str, Any]:
    return {"endpoints": list(ENDPOINTS)}


def runs(conn: sqlite3.Connection, query: Mapping[str, str]) -> Dict[str, Any]:
    chosen = data.run_filter(conn, query)
    paging = data.paging(query)
    total = data.count_runs(conn, chosen)
    rows = data.list_runs(conn, chosen, paging.per_page, paging.offset)
    return {"filter": chosen.params(), "runs": rows, "paging": paging.describe(len(rows), total)}


def run(conn: sqlite3.Connection, run_id: int) -> Dict[str, Any]:
    detail = data.ReadOnlyStore(conn).run_detail(run_id)
    if detail is None:
        raise NotFound(f"no run with id {run_id}")
    detail["lot"] = lots_module.name_of(conn, detail.get("lot_id")) or None
    return {"run": detail}


def serial(conn: sqlite3.Connection, serial_number: str, query: Mapping[str, str]) -> Dict[str, Any]:
    chosen = data.RunFilter(serial=serial_number)
    paging = data.paging(query)
    total = data.count_runs(conn, chosen)
    if not total:
        raise NotFound(f"no runs of serial {serial_number!r}")
    rows = data.list_runs(conn, chosen, paging.per_page, paging.offset)
    return {"serial": serial_number, "runs": rows, "paging": paging.describe(len(rows), total)}


def plans(conn: sqlite3.Connection, query: Mapping[str, str]) -> Dict[str, Any]:
    return {"plans": data.plans_seen(conn)}


def stats(conn: sqlite3.Connection, query: Mapping[str, str]) -> Dict[str, Any]:
    """What ``benchrun stats --json`` gives for the same choices; ``view`` picks which part."""
    view = query.get("view") or "summary"
    if view not in VIEWS:
        raise PlanError([f"view must be one of {', '.join(VIEWS)}"])
    selection = stats_selection(conn, query)
    problems = []
    if selection.plan is None and selection.product is None:
        problems.append("say which plan with plan=NAME, or which part with product=PART")
    if view in ("histogram", "control") and selection.measurement is None:
        problems.append(f"view={view} needs measurement=NAME")
    bins = data.whole_param(query, "bins", 0, 1, engine.MAX_BINS) or None
    if bins is not None and view != "histogram":
        problems.append("bins applies to view=histogram")
    if problems:
        raise PlanError(problems)
    return build_report(conn, selection, view, bins)


def lots(conn: sqlite3.Connection, query: Mapping[str, str]) -> Dict[str, Any]:
    return {"lots": lots_module.listing(conn)}


def lot(conn: sqlite3.Connection, name_or_id: str) -> Dict[str, Any]:
    """What ``lot show --json`` gives: the lot, its runs, units, yield and failures by measurement."""
    found = data.lot_summary(conn, name_or_id)
    if found is None:
        raise NotFound(f"no lot named {name_or_id!r} and none with that id")
    found["unit_results"] = lots_module.unit_results(conn, found["lot"]["id"])
    return found


def stations(conn: sqlite3.Connection, query: Mapping[str, str]) -> Dict[str, Any]:
    return {"stations": data.station_list(conn)}


def instruments(conn: sqlite3.Connection, query: Mapping[str, str]) -> Dict[str, Any]:
    return {"instruments": data.instrument_list(conn, query)}


def instrument(conn: sqlite3.Connection, tag: str) -> Dict[str, Any]:
    found = data.instrument_detail(conn, tag)
    if found is None:
        raise NotFound(f"no instrument with asset tag {tag!r}")
    return {"instrument": found}


def product_list(conn: sqlite3.Connection, query: Mapping[str, str]) -> Dict[str, Any]:
    return {"products": products.summaries(conn)}


def product(conn: sqlite3.Connection, part_number: str) -> Dict[str, Any]:
    found = data.product_detail(conn, part_number)
    if found is None:
        raise NotFound(f"no product {part_number!r}")
    return {"product": found}


def audit(conn: sqlite3.Connection, query: Mapping[str, str]) -> Dict[str, Any]:
    return audit_entries(conn, query)
