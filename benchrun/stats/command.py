"""``benchrun stats``: production statistics over recorded runs, as text, JSON, CSV or an HTML page."""

from __future__ import annotations

import argparse
import csv
import dataclasses
import io
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from benchrun.console import (
    DEFAULT_DB,
    EXIT_PASS,
    EXIT_USAGE,
    complain,
    count,
    fmt,
    not_negative_whole,
    open_store,
    say,
    table,
    utc_time,
)
from benchrun import lots
from benchrun.errors import PlanError
from benchrun.files import atomic_write_text
from benchrun.reports.csv_report import _text as csv_text
from benchrun.stats import charts, engine, query

FORMATS = ("text", "json", "csv", "html")
_BAR_WIDTH = 40


def _bins(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from None
    if not 1 <= value <= engine.MAX_BINS:
        raise argparse.ArgumentTypeError(f"{text} must be from 1 to {engine.MAX_BINS}")
    return value


def add_commands(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "stats",
        help="production statistics: capability, yield, failure Pareto, histograms and control charts",
        description="Statistics over recorded runs. Only complete runs (pass or fail, every step) give measurements; "
        "aborted runs, runs still going and runs of only some steps are counted and left out. "
        "Each sweep point and each set of limits is its own population.",
        epilog="Formulas and exclusions: docs/statistics.md.",
    )
    parser.add_argument("--db", default=DEFAULT_DB, help=f"results database (default: {DEFAULT_DB})")
    chosen = parser.add_argument_group("which runs")
    chosen.add_argument("--plan", help="plan name; needed unless --product is given")
    chosen.add_argument("--product", metavar="PART", help="only runs of this part number")
    chosen.add_argument("--station", help="only runs on this station")
    chosen.add_argument("--lot", help="only runs in this lot, by name or numeric id")
    chosen.add_argument("--serial", metavar="PREFIX", help="only serials starting with this")
    chosen.add_argument("--since", metavar="TIME", help="only runs started at or after this UTC date or time, YYYY-MM-DD[THH:MM]")
    chosen.add_argument("--until", metavar="TIME", help="only runs started at or before this UTC date or time")
    chosen.add_argument("--limit-runs", type=not_negative_whole, metavar="N", help="only the newest N matching runs, by start time")
    chosen.add_argument("--step", help="only measurements of this step")
    chosen.add_argument("--measurement", metavar="NAME", help="only measurements with this name")
    views = parser.add_argument_group("what to show (default: a summary per measurement)").add_mutually_exclusive_group()
    views.add_argument("--yield", dest="yield_view", action="store_true", help="first-pass and final yield, retests and runs per result")
    views.add_argument("--pareto", action="store_true", help="failing measurements, most often first")
    views.add_argument("--histogram", metavar="NAME", help="histogram of measurement NAME, with its limits")
    views.add_argument("--control", metavar="NAME", help="individuals control chart of NAME with Western Electric rules 1 to 4")
    parser.add_argument("--bins", type=_bins, help="histogram bins (default: Sturges' rule)")
    parser.add_argument("--format", choices=FORMATS, default=None, help="output format (default: text)")
    parser.add_argument("--json", action="store_true", help="the same as --format json")
    parser.add_argument("--out", metavar="FILE", help="write to this file instead of the terminal")
    parser.add_argument("--force", action="store_true", help="with --out, replace the file if it already exists")
    parser.set_defaults(func=cmd_stats)


def _view(args: argparse.Namespace) -> str:
    if args.yield_view:
        return "yield"
    if args.pareto:
        return "pareto"
    if args.histogram is not None:
        return "histogram"
    if args.control is not None:
        return "control"
    return "summary"


def _check(args: argparse.Namespace, view: str) -> str:
    problems = []
    if not args.plan and not args.product:
        problems.append("say which plan with --plan NAME, or which part with --product PART")
    output = args.format or "text"
    if args.json:
        if args.format not in (None, "json"):
            problems.append(f"--json and --format {args.format} ask for different formats")
        output = "json"
    if args.bins is not None and view != "histogram" and output != "html":
        problems.append("--bins applies to --histogram and to --format html")
    named = args.histogram if view == "histogram" else args.control
    if named is not None and args.measurement is not None and args.measurement != named:
        problems.append(f"--measurement {args.measurement} and --{view} {named} name different measurements")
    if args.force and not args.out:
        problems.append("--force only applies with --out")
    if not Path(args.db).exists():
        problems.append(f"no results database at {args.db}")
    if args.out and Path(args.out).exists() and not args.force:
        problems.append(f"{args.out} already exists; choose another name or add --force to replace it")
    if problems:
        raise PlanError(problems)
    return output


def cmd_stats(args: argparse.Namespace) -> int:
    view = _view(args)
    output = _check(args, view)
    since = utc_time(args.since, "--since")
    until = utc_time(args.until, "--until", end_of_day=True)
    with open_store(args.db) as store:
        selection = query.Selection(
            plan=args.plan,
            product=args.product,
            station=args.station,
            lot_id=lots.find(store.conn, args.lot)["id"] if args.lot is not None else None,
            serial_prefix=args.serial,
            since=since,
            until=until,
            step=args.step,
            measurement={"histogram": args.histogram, "control": args.control}.get(view, args.measurement),
            limit_runs=args.limit_runs,
        )
        report = build_report(store.conn, selection, view, args.bins, everything=output == "html" and view == "summary")
    if not report["runs"]["selected"]:
        complain("No matching runs.")
        return EXIT_USAGE
    if view in ("histogram", "control") and not report["charts"]:
        complain(f"No values of measurement {selection.measurement!r} in the complete runs selected. {charts.runs_sentence(report['runs'])}")
        return EXIT_USAGE
    text = {"text": render_text, "json": render_json, "csv": render_csv, "html": charts.render_html}[output](report)
    if args.out:
        atomic_write_text(args.out, text)
        say(f"Wrote {args.out}")
    else:
        say(text.rstrip("\n"))
    return EXIT_PASS


# Building the report ------------------------------------------------------


def build_report(conn: Any, selection: query.Selection, view: str, bins: Optional[int] = None, everything: bool = False) -> Dict[str, Any]:
    """What one view shows, as plain data. ``everything`` adds yield, Pareto and charts to the summary, for the HTML page."""
    with query.chosen_runs(conn, selection) as chosen:
        return _build(conn, selection, chosen, view, bins, everything)


def _build(conn: Any, selection: query.Selection, chosen: Any, view: str, bins: Optional[int], everything: bool) -> Dict[str, Any]:
    entries = query.yield_by_plan(conn, chosen)
    runs = query.run_totals(entries)
    report: Dict[str, Any] = {"view": view, "selection": dataclasses.asdict(selection), "runs": runs}
    if view == "yield" or everything:
        report["yield"] = entries
    if view == "pareto" or everything:
        report["pareto"] = {
            "failed_runs": sum(entry["counts"]["fail"] for entry in entries),
            "complete_runs": runs["complete"],
            "rows": query.failure_pareto(conn, chosen),
        }
    if view in ("summary", "histogram", "control"):
        groups = query.measurement_groups(conn, chosen)
        summaries = [query.measurement_summary(group) for group in groups]
        if view == "summary":
            report["measurements"] = summaries
        if view != "summary" or everything:
            report["charts"] = [
                _chart_item(conn, chosen, group, summary, view in ("histogram", "summary"), view in ("control", "summary"), bins)
                for group, summary in zip(groups, summaries)
                if group["values"]
            ]
    return report


def _chart_item(
    conn: Any, chosen: Any, group: Dict[str, Any], summary: Dict[str, Any], with_histogram: bool, with_control: bool, bins: Optional[int]
) -> Dict[str, Any]:
    item: Dict[str, Any] = {"measurement": summary}
    values = group["values"]
    if with_histogram:
        item["histogram"] = engine.histogram(values, bins)
    if with_control:
        chart = engine.control_chart(values)
        rules = chart.pop("rules", None)
        ranges = chart.pop("moving_ranges", None)
        item["chart"] = chart
        if rules is not None:
            broken: Dict[int, List[int]] = {}
            for rule, indices in rules.items():
                for index in indices:
                    broken.setdefault(index, []).append(rule)
            chart["rules"] = [{"rule": rule, "description": engine.RULES[rule], "points": [i + 1 for i in rules[rule]]} for rule in sorted(rules)]
            labels = query.run_labels(conn, chosen, group["run_ids"])
            item["points"] = [
                {
                    "number": index + 1,
                    "run_id": run_id,
                    "serial": labels[run_id][0],
                    "started_at": labels[run_id][1],
                    "value": value,
                    "moving_range": ranges[index],
                    "rules": broken.get(index, []),
                }
                for index, (run_id, value) in enumerate(zip(group["run_ids"], values))
            ]
    return item


# Text ---------------------------------------------------------------------


def _point(point: Dict[str, float]) -> str:
    return ", ".join(f"{k}={v:g}" for k, v in point.items())


def _heading(summary: Dict[str, Any]) -> str:
    return f"{charts.describe(summary)}, plan {summary['plan']}, {charts.limits_text(summary)}"


def _summary_text(report: Dict[str, Any]) -> List[str]:
    measurements = report["measurements"]
    if not measurements:
        return ["No measurements in the complete runs selected."]
    several_plans = len({m["plan"] for m in measurements}) > 1
    rows = []
    for m in measurements:
        c = m["capability"]
        notes = [f"limit set {m['limit_set']} of {m['limit_sets']}" if m["limit_sets"] > 1 else "", c["note"] if c["note"] != "no limits" else ""]
        cells = [m["step"], m["name"], _point(m["point"]), m["units"], fmt(m["low"]), fmt(m["high"]), m["count"], m["no_value"], m["fails"]]
        cells += [fmt(m["mean"]), fmt(m["stdev"]), fmt(m["minimum"]), fmt(m["median"]), fmt(m["maximum"])]
        cells += [charts.ratio(c["cp"]), charts.ratio(c["cpk"]), charts.ratio(c["pp"]), charts.ratio(c["ppk"]), "; ".join(n for n in notes if n)]
        rows.append(([m["plan"]] if several_plans else []) + cells)
    headers = ["step", "name", "point", "units", "low", "high", "n", "no value", "fails", "mean", "stdev", "min", "median", "max", "cp", "cpk", "pp", "ppk", "note"]
    lines = [table((["plan"] if several_plans else []) + headers, rows)]
    split = len({(m["plan"], m["step"], m["name"], m["units"], json.dumps(m["point"])) for m in measurements if m["limit_sets"] > 1})
    if split:
        lines += ["", f"Limits changed between runs for {count(split, 'measurement')}: each set of limits has its own row, and capability is never computed across them."]
    return lines


def _yield_text(report: Dict[str, Any]) -> List[str]:
    rows = [
        (
            e["plan"], e["runs"], e["counts"]["pass"], e["counts"]["fail"], e["counts"]["aborted"], e["counts"]["running"], e["partial_runs"],
            e["units_with_verdict"], charts.percent(e["first_pass_yield"]), charts.percent(e["final_yield"]), e["retests"], e["retested_units"],
        )
        for e in report["yield"]
    ]
    headers = ("plan", "runs", "pass", "fail", "aborted", "running", "some steps", "units", "first-pass yield", "final yield", "retests", "units retested")
    return [table(headers, rows), "", "Units are serials with at least one complete run. Aborted, unfinished and some-steps runs are not in yield."]


def _pareto_text(report: Dict[str, Any]) -> List[str]:
    pareto = report["pareto"]
    lines = [charts.pareto_sentence(pareto)]
    if not pareto["rows"]:
        return lines + ["No failing measurements in the complete runs selected."]
    rows = [(r["plan"], r["step"], r["name"], r["runs"], r["readings"], charts.percent(r["share"]), charts.percent(r["cumulative"])) for r in pareto["rows"]]
    return lines + ["", table(("plan", "step", "measurement", "runs", "readings", "share", "cumulative"), rows)]


def _histogram_text(item: Dict[str, Any]) -> List[str]:
    summary, histogram = item["measurement"], item["histogram"]
    lines = [_heading(summary), f"  {count(summary['count'], 'value')}, {count(histogram['bins'], 'bin')} ({histogram['rule']})"]
    edges, counts = histogram["edges"], histogram["counts"]
    tallest = max(counts)
    limits = sorted((value, name) for name, value in (("low", summary["low"]), ("high", summary["high"])) if value is not None)
    width = max(len(fmt(edge)) for edge in edges)
    for index, number in enumerate(counts):
        while limits and limits[0][0] <= edges[index]:
            value, name = limits.pop(0)
            lines.append(f"  --- {name} limit {fmt(value)} ---")
        bar = "#" * max(1 if number else 0, round(number / tallest * _BAR_WIDTH))
        lines.append(f"  {fmt(edges[index]).rjust(width)} to {fmt(edges[index + 1]).rjust(width)}  {str(number).rjust(len(str(tallest)))}  {bar}".rstrip())
    for value, name in limits:
        lines.append(f"  --- {name} limit {fmt(value)} ---")
    return lines


def _control_text(item: Dict[str, Any]) -> List[str]:
    summary, chart = item["measurement"], item["chart"]
    lines = [_heading(summary), f"  {count(summary['count'], 'value')} in run order"]
    if chart["note"]:
        return lines + [f"  No control chart: {chart['note']}."]
    lines.append(
        f"  Centre {fmt(chart['centre'])}, sigma {fmt(chart['sigma'])} (average moving range {fmt(chart['mr_centre'])} / {engine.D2})"
    )
    lines.append(f"  LCL {fmt(chart['lcl'])}, UCL {fmt(chart['ucl'])}; moving range UCL {fmt(chart['mr_ucl'])}")
    for rule in chart["rules"]:
        points = ", ".join(str(p) for p in rule["points"]) or "none"
        lines.append(f"  Rule {rule['rule']} ({rule['description']}): {points}")
    rows = [
        (p["number"], p["run_id"], p["serial"], p["started_at"][:19].replace("T", " "), fmt(p["value"]), fmt(p["moving_range"]), ",".join(str(r) for r in p["rules"]))
        for p in item["points"]
    ]
    lines += ["", table(("point", "run", "serial", "started (UTC)", "value", "moving range", "rules"), rows)]
    return lines


def render_text(report: Dict[str, Any]) -> str:
    lines = [charts.runs_sentence(report["runs"]), ""]
    view = report["view"]
    if view == "summary":
        lines += _summary_text(report)
    elif view == "yield":
        lines += _yield_text(report)
    elif view == "pareto":
        lines += _pareto_text(report)
    else:
        render = _histogram_text if view == "histogram" else _control_text
        for item in report["charts"]:
            lines += render(item) + [""]
    return "\n".join(lines).rstrip("\n") + "\n"


def render_json(report: Dict[str, Any]) -> str:
    return json.dumps(report, indent=2) + "\n"


# CSV ----------------------------------------------------------------------


def _number(value: Any) -> Any:
    if value is None:
        return ""
    return repr(value) if isinstance(value, float) else value


_IDENTITY = ("plan", "step", "measurement", "point", "units", "low", "high", "limit_set", "limit_sets")


def _identity(summary: Dict[str, Any]) -> List[Any]:
    texts = [summary["plan"], summary["step"], summary["name"], json.dumps(summary["point"], sort_keys=True) if summary["point"] else "", summary["units"]]
    return [csv_text(t) for t in texts] + [_number(summary[k]) for k in ("low", "high", "limit_set", "limit_sets")]


def _csv_rows(report: Dict[str, Any]) -> Sequence[Sequence[Any]]:
    view = report["view"]
    if view == "summary":
        keys = ("readings", "no_value", "fails", "count", "mean", "stdev", "minimum", "median", "maximum")
        capability = ("cp", "cpk", "pp", "ppk", "sigma_within", "sigma_overall")
        quantiles = tuple(f"p{q}" for q in engine.PERCENTILES)
        rows: List[Sequence[Any]] = [_IDENTITY + keys + quantiles + capability + ("note",)]
        for m in report["measurements"]:
            c = m["capability"]
            rows.append(
                _identity(m) + [_number(m[k]) for k in keys] + [_number(m["percentiles"][q]) for q in quantiles]
                + [_number(c[k]) for k in capability] + [csv_text(c["note"])]
            )
        return rows
    if view == "yield":
        keys = ("runs", "partial_runs", "serials", "units_with_verdict", "first_pass_units", "first_pass_yield", "final_pass_units", "final_yield", "retests", "retested_units")
        rows = [("plan", "pass", "fail", "aborted", "running") + keys]
        for e in report["yield"]:
            rows.append([csv_text(e["plan"])] + [e["counts"][s] for s in ("pass", "fail", "aborted", "running")] + [_number(e[k]) for k in keys])
        return rows
    if view == "pareto":
        rows = [("plan", "step", "measurement", "runs", "readings", "share", "cumulative")]
        for r in report["pareto"]["rows"]:
            rows.append([csv_text(r["plan"]), csv_text(r["step"]), csv_text(r["name"])] + [_number(r[k]) for k in ("runs", "readings", "share", "cumulative")])
        return rows
    if view == "histogram":
        rows = [_IDENTITY + ("bin", "lower_edge", "upper_edge", "count")]
        for item in report["charts"]:
            h = item["histogram"]
            for index, number in enumerate(h["counts"]):
                rows.append(_identity(item["measurement"]) + [index + 1, _number(h["edges"][index]), _number(h["edges"][index + 1]), number])
        return rows
    rows = [_IDENTITY + ("point_number", "run_id", "serial", "started_at", "value", "moving_range", "centre", "lcl", "ucl", "rules")]
    for item in report["charts"]:
        chart = item["chart"]
        for p in item.get("points", []):
            rows.append(
                _identity(item["measurement"])
                + [p["number"], p["run_id"], csv_text(p["serial"]), p["started_at"], _number(p["value"]), _number(p["moving_range"])]
                + [_number(chart[k]) for k in ("centre", "lcl", "ucl")]
                + [" ".join(str(r) for r in p["rules"])]
            )
    return rows


def render_csv(report: Dict[str, Any]) -> str:
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerows(_csv_rows(report))
    return buffer.getvalue()
