"""The HTML pages. Each takes the site, a read-only connection and the request's query, and returns a whole page."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

from benchrun import audit, maintenance, products
from benchrun import lots as lots_module
from benchrun.console import count
from benchrun.inventory import STATUSES
from benchrun.reports import REGISTRY
from benchrun.reports.html_report import render_body
from benchrun.stats import charts, engine
from benchrun.stats import query as stats_query
from benchrun.stats.command import build_report
from benchrun.web import data
from benchrun.web.launcher import Launcher
from benchrun.web.markup import (
    Markup,
    badge,
    esc,
    facts,
    filter_form,
    hidden,
    item_target,
    item_url,
    join,
    link,
    number,
    page,
    pager,
    post_button,
    problems,
    select,
    share,
    table,
    text_input,
    url,
    when,
)

RECENT_RUNS = 15
LOT_RUNS_SHOWN = 200
LAUNCH_REFRESH_S = 2


class NotFound(Exception):
    """The thing a request names does not exist. Its text says what was looked for."""


@dataclass
class Site:
    """What every page needs to know about the server."""

    token: str
    launcher: Optional[Launcher] = None

    @property
    def runs_allowed(self) -> bool:
        return self.launcher is not None


def _page(site: Site, title: str, here: str, *parts: Any, refresh_s: Optional[int] = None) -> str:
    return page(title, join(parts), here, site.runs_allowed, refresh_s)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# Shared pieces ---------------------------------------------------------------


def _result(row: Mapping[str, Any]) -> Markup:
    return join([badge(row["status"]), " (some steps)" if row.get("partial") else ""])


def serial_link(serial: str) -> Markup:
    return link(item_url("/serials", serial, "serial"), serial)


def runs_table(rows: Sequence[Mapping[str, Any]]) -> Markup:
    if not rows:
        return Markup('<p class="muted">No matching runs.</p>')
    body = [
        (
            link(f"/runs/{row['id']}", row["id"]),
            when(row["started_at"]),
            serial_link(row["serial"]),
            f"{row['plan_name']} {row['plan_version']}".rstrip(),
            row["product"],
            row["station"],
            link(f"/lots/{row['lot_id']}", row["lot"]) if row.get("lot") else "",
            _result(row),
            row["passed"],
            row["failed"],
            row["errors"],
            "; ".join(part for part in (row["reason"], row["note"]) if part),
        )
        for row in rows
    ]
    headers = ("Run", "Started", "Serial", "Plan", "Product", "Station", "Lot", "Result", "Pass", "Fail", "Error", "Reason or note")
    return table(headers, body, numeric=(0, 8, 9, 10))


def _yield_table(entries: Sequence[Mapping[str, Any]], empty: str) -> Markup:
    if not entries:
        return Markup(f'<p class="muted">{esc(empty)}</p>')
    rows = [
        (
            link(url("/stats", {"plan": e["plan"]}), e["plan"]),
            e["runs"],
            e["counts"]["pass"],
            e["counts"]["fail"],
            e["counts"]["aborted"],
            e["counts"]["running"],
            e["units_with_verdict"],
            share(e["first_pass_yield"]),
            share(e["final_yield"]),
        )
        for e in entries
    ]
    headers = ("Plan", "Runs", "Pass", "Fail", "Aborted", "Running", "Units", "First-pass yield", "Final yield")
    return table(headers, rows, numeric=range(1, 9))


def _run_filters(conn: sqlite3.Connection, query: Mapping[str, str]) -> Markup:
    found = data.choices(conn)
    fields = [
        text_input("serial", "Serial", query.get("serial")),
        select("plan", "Plan", ((p, p) for p in found["plan"]), query.get("plan")),
        select("product", "Product", ((p, p) for p in found["product"]), query.get("product")),
        select("station", "Station", ((s, s) for s in found["station"]), query.get("station")),
        select("lot", "Lot", found["lot"], query.get("lot")),
        select("status", "Result", ((s, s) for s in ("pass", "fail", "aborted", "running")), query.get("status")),
        text_input("since", "Started from (UTC)", query.get("since"), kind="date"),
        text_input("until", "Started until (UTC)", query.get("until"), kind="date"),
    ]
    return filter_form("/runs", fields)


def _downloads(chosen: data.RunFilter) -> Markup:
    params = chosen.params()
    parts = [Markup("<strong>Download these runs:</strong>")]
    for name, text in (("csv", "CSV"), ("jsonl", "JSON Lines"), ("xlsx", "Excel workbook")):
        parts.append(link(url("/download/export", {**params, "format": name}), text))
    parts.append(link(url("/download/archive", params), "archive (zip)"))
    parts.append(link(url("/api/runs", params), "JSON"))
    return join([Markup('<div class="links">'), *parts, Markup("</div>")])


# Pages -----------------------------------------------------------------------


def dashboard(site: Site, conn: sqlite3.Connection, query: Mapping[str, str]) -> str:
    now = _now()
    running = maintenance.running_runs(conn, now)
    running_rows = [
        (link(f"/runs/{r['id']}", r["id"]), serial_link(r["serial"]), r["plan_name"], when(r["started_at"]), number(r["age_hours"]))
        for r in running
    ]
    total = data.count_runs(conn, data.RunFilter())
    parts: List[Any] = [Markup("<h1>Dashboard</h1>"), Markup(f'<p class="muted">{total} runs recorded.</p>')]
    parts.append(Markup("<h2>Runs still marked running</h2>"))
    if running_rows:
        parts.append(table(("Run", "Serial", "Plan", "Started", "Hours since start"), running_rows, numeric=(0, 4)))
        parts.append(
            Markup(
                '<p class="muted">A run is marked running from its start until it records a result. One that has been '
                "running for hours most likely stopped without recording; check the bench, then see "
                "<code>benchrun db mark-abandoned</code>.</p>"
            )
        )
    else:
        parts.append(Markup('<p class="muted">None.</p>'))
    today, week = data.start_of_day(now), data.start_of_week(now)
    parts.append(Markup(f"<h2>Yield today (since {esc(when(today.isoformat()))})</h2>"))
    parts.append(_yield_table(data.yield_since(conn, today), "No runs today."))
    parts.append(Markup(f"<h2>Yield this week (since {esc(when(week.isoformat()))})</h2>"))
    parts.append(_yield_table(data.yield_since(conn, week), "No runs this week."))
    parts.append(Markup('<p class="muted">Yield counts each serial once, from its complete runs: first-pass from its first, final from its last.</p>'))
    parts.append(Markup("<h2>Recent runs</h2>"))
    parts.append(runs_table(data.list_runs(conn, data.RunFilter(), RECENT_RUNS)))
    parts.append(link("/runs", "All runs"))
    return _page(site, "Dashboard", "dashboard", *parts)


def runs(site: Site, conn: sqlite3.Connection, query: Mapping[str, str]) -> str:
    chosen = data.run_filter(conn, query)
    paging = data.paging(query)
    total = data.count_runs(conn, chosen)
    rows = data.list_runs(conn, chosen, paging.per_page, paging.offset)
    described = paging.describe(len(rows), total)
    return _page(
        site,
        "Runs",
        "runs",
        Markup("<h1>Runs</h1>"),
        _run_filters(conn, query),
        _downloads(chosen) if total else "",
        runs_table(rows),
        pager("/runs", {**chosen.params(), "per_page": query.get("per_page")}, described),
    )


def run(site: Site, conn: sqlite3.Connection, run_id: int) -> str:
    detail = data.ReadOnlyStore(conn).run_detail(run_id)
    if detail is None:
        raise NotFound(f"no run with id {run_id}")
    lot = lots_module.name_of(conn, detail.get("lot_id")) or None
    reports = [link(f"/runs/{run_id}/report/{name}", name) for name in REGISTRY]
    related = [
        link(f"/api/runs/{run_id}", "JSON"),
        link(item_url("/serials", detail["serial"], "serial"), "history of this unit"),
        link(url("/stats", {"plan": detail["plan_name"]}), "statistics for this plan"),
    ]
    if lot is not None:
        related.append(link(f"/lots/{detail['lot_id']}", f"lot {lot}"))
    return _page(
        site,
        f"Run {run_id}",
        "runs",
        join([Markup('<div class="links"><strong>Reports:</strong>'), *reports, Markup("</div>")]),
        join([Markup('<div class="links"><strong>Also:</strong>'), *related, Markup("</div>")]),
        Markup(f'<p class="muted">Lot: {esc(lot)}</p>') if lot is not None else "",
        Markup(render_body(detail)),
    )


def serial(site: Site, conn: sqlite3.Connection, serial_number: str, query: Mapping[str, str]) -> str:
    chosen = data.RunFilter(serial=serial_number)
    paging = data.paging(query)
    total = data.count_runs(conn, chosen)
    if not total:
        raise NotFound(f"no runs of serial {serial_number!r}")
    rows = data.list_runs(conn, chosen, paging.per_page, paging.offset)
    latest = data.list_runs(conn, chosen, 1)[0]
    return _page(
        site,
        f"Unit {serial_number}",
        "runs",
        Markup(f"<h1>Unit {esc(serial_number)}</h1>"),
        join([Markup("<p>Latest result: "), _result(latest), f" in run {latest['id']}, {when(latest['started_at'])}, {count(total, 'run')} in all.", Markup("</p>")]),
        _downloads(chosen),
        runs_table(rows),
        pager(*item_target("/serials", serial_number, "serial"), paging.describe(len(rows), total)),
    )


def plans(site: Site, conn: sqlite3.Connection, query: Mapping[str, str]) -> str:
    rows = [
        (
            link(url("/stats", {"plan": p["plan_name"]}), p["plan_name"]),
            ", ".join(p["versions"]),
            link(url("/runs", {"plan": p["plan_name"]}), p["runs"]),
            p["serials"],
            p["units_with_verdict"],
            share(p["first_pass_yield"]),
            share(p["final_yield"]),
            when(p["first_run"]),
            when(p["last_run"]),
        )
        for p in data.plans_seen(conn)
    ]
    headers = ("Plan", "Versions", "Runs", "Serials", "Units with a result", "First-pass yield", "Final yield", "First run", "Last run")
    body = table(headers, rows, numeric=(2, 3, 4, 5, 6)) if rows else Markup('<p class="muted">No runs recorded yet.</p>')
    return _page(site, "Plans", "plans", Markup("<h1>Plans seen</h1>"), body)


def stats_selection(conn: sqlite3.Connection, query: Mapping[str, str]) -> stats_query.Selection:
    lot = data.text_param(query, "lot")
    since, until = data.time_params(query)
    return stats_query.Selection(
        plan=data.text_param(query, "plan"),
        product=data.text_param(query, "product"),
        station=data.text_param(query, "station"),
        lot_id=lots_module.find(conn, lot)["id"] if lot is not None else None,
        serial_prefix=data.text_param(query, "serial"),
        since=since,
        until=until,
        step=data.text_param(query, "step"),
        measurement=data.text_param(query, "measurement"),
        limit_runs=data.whole_param(query, "limit_runs", 0, 1, 10**9) or None,
    )


def stats(site: Site, conn: sqlite3.Connection, query: Mapping[str, str]) -> str:
    found = data.choices(conn)
    fields = [
        select("plan", "Plan", ((p, p) for p in found["plan"]), query.get("plan"), blank="choose"),
        select("product", "Product", ((p, p) for p in found["product"]), query.get("product")),
        select("station", "Station", ((s, s) for s in found["station"]), query.get("station")),
        select("lot", "Lot", found["lot"], query.get("lot")),
        text_input("serial", "Serials starting with", query.get("serial")),
        text_input("since", "Started from (UTC)", query.get("since"), kind="date"),
        text_input("until", "Started until (UTC)", query.get("until"), kind="date"),
        text_input("measurement", "Only measurement", query.get("measurement")),
    ]
    parts: List[Any] = [Markup("<h1>Statistics</h1>"), filter_form("/stats", fields)]
    selection = stats_selection(conn, query)
    bins = data.whole_param(query, "bins", 0, 1, engine.MAX_BINS) or None
    if selection.plan is None and selection.product is None:
        parts.append(Markup('<p class="muted">Choose a plan or a product. Plans with runs:</p>'))
        parts.append(join([Markup("<ul>"), *(join([Markup("<li>"), link(url("/stats", {"plan": p}), p), Markup("</li>")]) for p in found["plan"]), Markup("</ul>")]))
        return _page(site, "Statistics", "stats", *parts)
    report = build_report(conn, selection, "summary", bins, everything=True)
    if not report["runs"]["selected"]:
        parts.append(Markup('<p class="muted">No matching runs.</p>'))
    else:
        api = url("/api/stats", {k: v for k, v in query.items() if k != "view"})
        parts.append(join([Markup('<div class="links">'), link(api, "JSON"), Markup("</div>")]))
        parts.append(Markup(charts.render_sections(report)))
    return _page(site, "Statistics", "stats", *parts)


def lots(site: Site, conn: sqlite3.Connection, query: Mapping[str, str]) -> str:
    rows = [
        (
            link(f"/lots/{lot['id']}", lot["name"]),
            lot["product"],
            lot["plan_name"],
            lot["status"],
            lot["quantity"],
            link(url("/runs", {"lot": lot["id"]}), lot["runs"]),
            lot["units"]["units_with_verdict"],
            lot["units"]["to_test"],
            share(lot["units"]["first_pass_yield"]),
            share(lot["units"]["final_yield"]),
            when(lot["created_at"]),
            when(lot["closed_at"]),
        )
        for lot in lots_module.listing(conn)
    ]
    headers = ("Lot", "Product", "Plan", "Status", "Quantity", "Runs", "Units tested", "Still to test", "First-pass yield", "Final yield", "Opened", "Closed")
    body = table(headers, rows, numeric=range(4, 10)) if rows else Markup('<p class="muted">No lots in this database.</p>')
    return _page(site, "Lots", "lots", Markup("<h1>Lots</h1>"), body)


def lot(site: Site, conn: sqlite3.Connection, name_or_id: str) -> str:
    summary = data.lot_summary(conn, name_or_id)
    if summary is None:
        raise NotFound(f"no lot named {name_or_id!r} and none with that id")
    found, units, total = summary["lot"], summary["units"], summary["runs"]["selected"]
    lot_id = found["id"]
    info = facts(
        (
            ("Status", found["status"]),
            ("Product", found["product"]),
            ("Plan", found["plan_name"]),
            ("Quantity", found["quantity"]),
            ("Note", found["note"]),
            ("Opened (UTC)", found["created_at"]),
            ("Closed (UTC)", found["closed_at"]),
            ("Runs", total),
            ("Runs still marked running", len(summary["running_runs"]) or None),
            ("Units tested (with a complete run)", units["units_with_verdict"]),
            ("Units still to test", units["to_test"]),
            ("First-pass yield", share(units["first_pass_yield"])),
            ("Final yield", share(units["final_yield"])),
            ("Retests", units["retests"]),
        )
    )
    report = build_report(conn, stats_query.Selection(lot_id=lot_id), "summary", everything=True)
    chosen = data.RunFilter(lot=lot_id)
    shown = data.list_runs(conn, chosen, LOT_RUNS_SHOWN)
    parts: List[Any] = [Markup(f"<h1>Lot {esc(found['name'])}</h1>"), info, _downloads(chosen) if shown else ""]
    parts.append(join([Markup('<div class="links">'), link(f"/api/lots/{lot_id}", "JSON"), link(url("/runs", {"lot": lot_id}), "all runs in this lot"), Markup("</div>")]))
    if report["runs"]["selected"]:
        parts.append(Markup(charts.render_sections(report)))
    parts.append(Markup("<h2>Runs</h2>"))
    parts.append(runs_table(shown))
    if total > LOT_RUNS_SHOWN:
        parts.append(Markup(f'<p class="muted">The newest {LOT_RUNS_SHOWN} of {total} runs.</p>'))
    return _page(site, f"Lot {found['name']}", "lots", *parts)


def _role_rows(roles: Sequence[Mapping[str, Any]]) -> List[Sequence[Any]]:
    return [
        (r["role"], link(f"/instruments/{r['asset_tag']}", r["asset_tag"]), r["driver"], r["address"], r["status"], number(r["timeout_s"]), r["expect_idn"])
        for r in roles
    ]


def station_list(site: Site, conn: sqlite3.Connection, query: Mapping[str, str]) -> str:
    parts: List[Any] = [Markup("<h1>Stations</h1>")]
    found = data.station_list(conn)
    if not found:
        parts.append(Markup('<p class="muted">No stations yet. Make one with <code>benchrun station create NAME</code>.</p>'))
    for station in found:
        parts.append(Markup(f"<h2>{esc(station['name'])}</h2>"))
        parts.append(facts((("Description", station["description"]), ("Location", station["location"]), ("Last changed (UTC)", station["updated_at"]))))
        parts.append(join([Markup("<p>"), link(url("/runs", {"station": station["name"]}), f"{station['runs']} runs recorded on it"), Markup("</p>")]))
        if station["roles"]:
            parts.append(table(("Role", "Asset tag", "Driver", "Address", "Status", "Timeout (s)", "Expected *IDN?"), _role_rows(station["roles"])))
        else:
            parts.append(Markup('<p class="muted">No instruments assigned yet.</p>'))
    return _page(site, "Stations", "stations", *parts)


def _used_by(item: Mapping[str, Any]) -> str:
    return ", ".join(f"{s['station']} ({s['role']})" for s in item["stations"])


def instrument_list(site: Site, conn: sqlite3.Connection, query: Mapping[str, str]) -> str:
    found = data.instrument_list(conn, query)
    drivers = sorted({row[0] for row in conn.execute("SELECT DISTINCT driver FROM instruments")})
    form = filter_form(
        "/instruments",
        [
            select("driver", "Driver", ((d, d) for d in drivers), query.get("driver")),
            select("status", "Status", ((s, s) for s in STATUSES), query.get("status")),
            text_input("search", "Search", query.get("search")),
        ],
    )
    rows = [
        (
            link(f"/instruments/{i['asset_tag']}", i["asset_tag"]),
            i["driver"],
            i["manufacturer"],
            i["model"],
            i["serial_number"],
            i["address"],
            i["location"],
            i["status"],
            _used_by(i),
            i["notes"],
        )
        for i in found
    ]
    headers = ("Asset tag", "Driver", "Manufacturer", "Model", "Serial", "Address", "Location", "Status", "Stations", "Notes")
    body = table(headers, rows) if rows else Markup('<p class="muted">No matching instruments.</p>')
    return _page(site, "Instruments", "instruments", Markup("<h1>Instruments</h1>"), form, body)


def instrument(site: Site, conn: sqlite3.Connection, tag: str) -> str:
    found = data.instrument_detail(conn, tag)
    if found is None:
        raise NotFound(f"no instrument with asset tag {tag!r}")
    info = facts(
        [(label, found[key]) for label, key in (
            ("Driver", "driver"), ("Manufacturer", "manufacturer"), ("Model", "model"), ("Serial number", "serial_number"),
            ("Address", "address"), ("Location", "location"), ("Status", "status"), ("Notes", "notes"),
            ("Added (UTC)", "added_at"), ("Last changed (UTC)", "updated_at"),
        )] + [("Stations", _used_by(found))]
    )  # fmt: skip
    rows = [
        (link(f"/runs/{r['id']}", r["id"]), when(r["started_at"]), serial_link(r["serial"]), f"{r['plan_name']} {r['plan_version']}".rstrip(), r["role"], r["station"], badge(r["status"]))
        for r in found["recent_runs"]
    ]
    runs_part = table(("Run", "Started", "Serial", "Plan", "As", "Station", "Result"), rows, numeric=(0,)) if rows else Markup('<p class="muted">No runs recorded with this asset tag.</p>')
    return _page(
        site,
        f"Instrument {found['asset_tag']}",
        "instruments",
        Markup(f"<h1>Instrument {esc(found['asset_tag'])}</h1>"),
        info,
        Markup("<h2>Recent runs</h2>"),
        runs_part,
        link(url("/audit", {"subject": f"instrument:{found['asset_tag']}"}), "changes in the audit log"),
    )


def product_list(site: Site, conn: sqlite3.Connection, query: Mapping[str, str]) -> str:
    rows = [
        (
            link(item_url("/products", p["part_number"], "part"), p["part_number"]),
            p["description"],
            p["plan_file"],
            p["serial_pattern"],
            p["variables"],
            p["limits"],
            link(url("/runs", {"product": p["part_number"]}), p["runs"]),
            when(p["updated_at"]),
        )
        for p in products.summaries(conn)
    ]
    headers = ("Part number", "Description", "Plan", "Serial pattern", "Variables", "Limits", "Runs", "Last changed")
    body = table(headers, rows, numeric=(4, 5, 6)) if rows else Markup('<p class="muted">No products in this database.</p>')
    return _page(site, "Products", "products", Markup("<h1>Products</h1>"), body)


def product(site: Site, conn: sqlite3.Connection, part_number: str) -> str:
    found = data.product_detail(conn, part_number)
    if found is None:
        raise NotFound(f"no product {part_number!r}")
    part_number = found["part_number"]  # As it was added, whatever case the address used.
    info = facts(
        (
            ("Description", found["description"]),
            ("Plan", found["plan_file"]),
            ("Serial pattern", found["serial_pattern"]),
            ("Created (UTC)", found["created_at"]),
            ("Last changed (UTC)", found["updated_at"]),
        )
    )
    variables = [(name, number(value)) for name, value in found["variables"].items()]
    limits = [(item["step"], item["measurement"], number(item["low"]), number(item["high"])) for item in found["limits"]]
    return _page(
        site,
        f"Product {part_number}",
        "products",
        Markup(f"<h1>Product {esc(part_number)}</h1>"),
        info,
        join([Markup("<p>"), link(url("/runs", {"product": part_number}), f"{found['runs']} runs recorded as this product"), Markup("</p>")]),
        Markup("<h2>Variables</h2>"),
        table(("Variable", "Value"), variables, numeric=(1,)) if variables else Markup('<p class="muted">None; the plan\'s own values apply.</p>'),
        Markup("<h2>Limits</h2>"),
        table(("Step", "Measurement", "Low", "High"), limits, numeric=(2, 3)) if limits else Markup('<p class="muted">None; the plan\'s own limits apply.</p>'),
        link(url("/audit", {"subject": f"product:{part_number}"}), "changes in the audit log"),
    )


def audit_entries(conn: sqlite3.Connection, query: Mapping[str, str]) -> Dict[str, Any]:
    """One page of entries, newest first, and whether there are more."""
    since, until = data.time_params(query)
    paging = data.paging(query)
    found = audit.entries(
        conn,
        subject=data.text_param(query, "subject"),
        action=data.text_param(query, "action"),
        actor=query.get("actor") or None,
        since=since,
        until=until,
        limit=paging.per_page + 1,
        offset=paging.offset,
    )
    more = len(found) > paging.per_page
    found = found[: paging.per_page]
    return {"entries": found, "paging": paging.describe(len(found), more=more)}


def audit_log(site: Site, conn: sqlite3.Connection, query: Mapping[str, str]) -> str:
    found = audit_entries(conn, query)
    form = filter_form(
        "/audit",
        [
            text_input("subject", "Subject (ending in : for all of a kind)", query.get("subject")),
            text_input("action", "Action", query.get("action")),
            text_input("actor", "Actor", query.get("actor")),
            text_input("since", "From (UTC)", query.get("since"), kind="date"),
            text_input("until", "Until (UTC)", query.get("until"), kind="date"),
        ],
    )
    rows = [
        (e["id"], when(e["at"]), e["action"], e["subject"], e["actor"], ", ".join(f"{k}={json.dumps(v, sort_keys=True)}" for k, v in sorted(e["detail"].items())))
        for e in found["entries"]
    ]
    body = table(("Entry", "At", "Action", "Subject", "Actor", "Detail"), rows, numeric=(0,)) if rows else Markup('<p class="muted">No matching audit entries.</p>')
    keep = {k: query.get(k) for k in ("subject", "action", "actor", "since", "until", "per_page")}
    return _page(site, "Audit log", "audit", Markup("<h1>Audit log</h1>"), form, body, pager("/audit", keep, found["paging"]))


# Starting runs ----------------------------------------------------------------


def _launch_form(site: Site, conn: sqlite3.Connection, launcher: Launcher, form: Mapping[str, str]) -> Markup:
    found = data.choices(conn)
    plan_names = launcher.plans()
    sim, retest = (" checked" if form.get(name) == "on" else "" for name in ("sim", "retest"))
    open_lots = [lot["name"] for lot in lots_module.listing(conn, lots_module.OPEN)]
    fields = [
        select("plan", "Plan file", ((p, p) for p in plan_names), form.get("plan"), blank="the product's own plan"),
        text_input("serial", "Serial number", form.get("serial"), required=True),
        text_input("operator", "Operator", form.get("operator")),
        select("product", "Product", ((p, p) for p in found["product"]), form.get("product"), blank="none, or the lot's"),
        select("lot", "Open lot", ((name, name) for name in open_lots), form.get("lot"), blank="none"),
        Markup(f'<label class="check"><input type="checkbox" name="retest" value="on"{retest}> Retest a unit that already passed in the lot</label>'),
        select("station", "Station", ((s, s) for s in found["station"]), form.get("station"), blank="the plan's own addresses"),
        Markup(f'<label class="check"><input type="checkbox" name="sim" value="on"{sim}> Use private simulators instead of instruments</label>'),
    ]
    note = "" if plan_names else Markup(f'<p class="muted">There are no .json files in {esc(str(launcher.plans_dir))}.</p>')
    return join(
        [
            note,
            Markup('<form class="launch" method="post" action="/launch">'),
            hidden("token", site.token),
            *fields,
            Markup('<div><button type="submit">Start the run</button></div></form>'),
        ]
    )


def launch(site: Site, conn: sqlite3.Connection, form: Optional[Mapping[str, str]] = None, found: Sequence[str] = (), said: str = "") -> str:
    launcher = site.launcher
    if launcher is None:
        raise NotFound("starting runs from the browser is switched off; start the server with --allow-runs --plans-dir DIR")
    state = launcher.snapshot()
    parts: List[Any] = [Markup("<h1>Start a run</h1>")]
    if said:
        parts.append(Markup(f"<p><strong>{esc(said)}</strong></p>"))
    if found:
        parts.append(problems(found))
    if state is not None:
        parts.append(_launch_state(site, state))
    if state is None or not state["running"]:
        parts.append(Markup(f'<p class="muted">Plans come from {esc(str(launcher.plans_dir))}. One run at a time.</p>'))
        parts.append(_launch_form(site, conn, launcher, form or {}))
    refresh = LAUNCH_REFRESH_S if state is not None and state["running"] and not found else None
    return _page(site, "Start a run", "launch", *parts, refresh_s=refresh)


def _launch_state(site: Site, state: Mapping[str, Any]) -> Markup:
    form = state["form"]
    what = f"Serial {form['serial']}, plan {form['plan'] or 'of the product'}"
    what += (f" in lot {form['lot']}" if form.get("lot") else "") + (" on simulators" if form["sim"] else "")
    parts: List[Any] = [Markup(f"<h2>{'Running' if state['running'] else 'Last run'}: {esc(what)}</h2>")]
    parts.append(Markup(f"<p>Started {esc(when(state['started_at']))}. Result: <strong>{esc(state['outcome'])}</strong>"))
    if state["exit_code"] is not None:
        parts.append(Markup(f" (exit code {state['exit_code']}), finished {esc(when(state['finished_at']))}"))
    parts.append(Markup(".</p>"))
    if state["run_id"] is not None:
        parts.append(join([Markup("<p>"), link(f"/runs/{state['run_id']}", f"Run {state['run_id']} in the database"), Markup("</p>")]))
    if state["running"]:
        if state["prompt"] is not None:
            parts.append(Markup(f'<div class="reason"><strong>The operator is asked:</strong> {esc(state["prompt"])}</div>'))
            parts.append(post_button("/launch/continue", "Done, go on", site.token))
        parts.append(post_button("/launch/stop", "Stop the run", site.token, css="stop"))
        if state["stop_sent"]:
            parts.append(Markup('<p class="muted">Stop sent; the run is switching its outputs off.</p>'))
    if state["stop_problem"]:
        parts.append(problems([state["stop_problem"]]))
    if state["dropped_lines"]:
        parts.append(Markup(f'<p class="muted">The first {state["dropped_lines"]} lines are not shown.</p>'))
    parts.append(Markup(f'<pre class="output">{esc(chr(10).join(state["output"]))}</pre>'))
    return join(parts)
