"""A lot summary as one self-contained HTML page, a CSV file with a row per unit, and JSON.

Yield, the failure Pareto and the histograms come from ``benchrun.stats``, counted
the same way as ``stats`` counts them: only complete runs give measurements.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
from typing import Any, Dict, List

from benchrun import lots
from benchrun.console import count
from benchrun.reports.csv_report import _text as csv_text
from benchrun.reports.html_report import STYLE, _esc, _num
from benchrun.stats import charts, engine, query

FORMAT_NAME = "benchrun-lot"
FORMAT_VERSION = 1
FORMATS = ("html", "csv", "json")
CSV_COLUMNS = (
    "lot", "serial", "runs", "results", "first_result", "final_result", "last_run", "last_status", "last_started_at", "failed_measurements",
)  # fmt: skip

_EXTRA_STYLE = "section.measurement { border-top: 1px solid var(--line); margin-top: 2rem; }"


def build(conn: Any, lot: Dict[str, Any]) -> Dict[str, Any]:
    """Everything the three files hold, as plain data."""
    report = {"format": FORMAT_NAME, "format_version": FORMAT_VERSION}
    report.update(lots.summary(conn, lot))
    report["unit_results"] = lots.unit_results(conn, lot["id"])
    measurements = []
    for group in query.measurement_groups(conn, query.Selection(lot_id=lot["id"])):
        item = query.measurement_summary(group)
        limited = group["low"] is not None or group["high"] is not None
        item["histogram"] = engine.histogram(group["values"]) if limited and group["values"] else None
        measurements.append(item)
    report["measurements"] = measurements
    return report


def file_stem(lot: Dict[str, Any]) -> str:
    """A file name base that is safe for any lot name, and unique within one database."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", lot["name"]).strip("._")[:64].strip("._") or "lot"
    return f"lot-{lot['id']:06d}-{name}"


# JSON ----------------------------------------------------------------------


def _finite(value: Any) -> Any:
    """JSON has no infinity or NaN; a database written elsewhere could hold either."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _finite(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_finite(item) for item in value]
    return value


def render_json(report: Dict[str, Any]) -> str:
    return json.dumps(_finite(report), indent=2, allow_nan=False) + "\n"


# CSV -----------------------------------------------------------------------


def render_csv(report: Dict[str, Any]) -> str:
    """One row per serial in the lot."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    name = csv_text(report["lot"]["name"])
    for unit in report["unit_results"]:
        writer.writerow(
            [
                name,
                csv_text(unit["serial"]),
                unit["runs"],
                " ".join(result.replace(" (some steps)", "*") for result in unit["results"]),
                unit["first_result"] or "",
                unit["final_result"] or "",
                unit["last_run"],
                unit["last_status"],
                unit["last_started_at"],
                csv_text("; ".join(unit["failed_measurements"])),
            ]
        )
    return buffer.getvalue()


# HTML ----------------------------------------------------------------------


def _details(report: Dict[str, Any]) -> str:
    lot, units = report["lot"], report["units"]
    quantity = "not set" if units["quantity"] is None else f"{units['quantity']} ({units['to_test']} still to test)"
    rows = (
        ("Product", lot["product"]),
        ("Plan", lot["plan_name"]),
        ("Quantity", quantity),
        ("Note", lot["note"]),
        ("Opened (UTC)", lot["created_at"]),
        ("Closed (UTC)", lot["closed_at"] or "still open"),
    )
    cells = "".join(f"<tr><th>{_esc(label)}</th><td>{_esc(value)}</td></tr>" for label, value in rows if value)
    return f'<h2>Lot</h2><div class="scroll"><table><tbody>{cells}</tbody></table></div>'


def _yield_section(report: Dict[str, Any]) -> str:
    units, counts = report["units"], report["runs"]["counts"]
    first = f"{charts.percent(units['first_pass_yield']) or 'none'} ({units['first_pass_units']} of {units['units_with_verdict']} units)"
    final = f"{charts.percent(units['final_yield']) or 'none'} ({units['final_pass_units']} of {units['units_with_verdict']} units)"
    rows = [
        ("Units tested", units["units_with_verdict"]),
        ("First-pass yield", first),
        ("Final yield", final),
        ("Retests", f"{count(units['retests'], 'run')} on {count(units['retested_units'], 'unit')}"),
        ("Runs", f"{counts['pass']} pass, {counts['fail']} fail, {counts['aborted']} aborted, {counts['running']} running"),
    ]
    if report["running_runs"]:
        rows.append(("Still running", ", ".join(f"run {run_id}" for run_id in report["running_runs"])))
    cells = "".join(f"<tr><th>{_esc(label)}</th><td>{_esc(value)}</td></tr>" for label, value in rows)
    return f'<h2>Yield</h2><p class="muted">{_esc(charts.runs_sentence(report["runs"]))}</p><div class="scroll"><table><tbody>{cells}</tbody></table></div>'


def _units_section(report: Dict[str, Any]) -> str:
    rows = [
        (u["serial"], u["runs"], ", ".join(u["results"]), u["first_result"] or "", u["final_result"] or "", u["last_run"], ", ".join(u["failed_measurements"]))
        for u in report["unit_results"]
    ]
    headers = ("Serial", "Runs", "Results in order", "First result", "Final result", "Last run", "Failed in the final run")
    return "<h2>Units</h2>" + charts._table(headers, rows, 9)


def _histograms(report: Dict[str, Any]) -> str:
    out: List[str] = []
    for item in report["measurements"]:
        if item["histogram"] is None:
            continue
        c = item["capability"]
        facts = f"{item['count']} values, {item['fails']} failed, mean {_num(item['mean'])}, stdev {_num(item['stdev'])}"
        if c["ppk"] is not None:
            facts += f", Ppk {charts.ratio(c['ppk'])}"
        out.append(
            f'<section class="measurement"><h3>{_esc(charts.describe(item))}</h3>'
            f'<p class="muted">{_esc(charts.limits_text(item))}; {_esc(facts)}</p>'
            f"{charts.histogram_svg(item, item['histogram'])}</section>"
        )
    if not out:
        return '<h2>Histograms</h2><p class="muted">No limited measurement has a value from a complete run yet.</p>'
    return "<h2>Histograms</h2>" + "".join(out)


def render_html(report: Dict[str, Any]) -> str:
    lot = report["lot"]
    title = f"Lot {lot['name']}"
    status = lot["status"]
    return "".join(
        [
            '<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            f"<title>{_esc(title)}</title><style>{STYLE}{_EXTRA_STYLE}</style></head><body>",
            f'<h1>{_esc(title)} <span class="badge {"pass" if status == lots.OPEN else "info"}">{_esc(status)}</span></h1>',
            _details(report),
            _yield_section(report),
            charts._pareto_section(report["pareto"]),
            _units_section(report),
            _histograms(report),
            "</body></html>\n",
        ]
    )


RENDERERS = {"html": render_html, "csv": render_csv, "json": render_json}
