"""Statistics as one self-contained HTML page: inline CSS and SVG, no scripts, everything escaped."""

from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Optional, Sequence

from benchrun.console import count
from benchrun.reports.html_report import STYLE, _esc, _num

_WIDTH, _HEIGHT = 640, 230
_LEFT, _RIGHT, _TOP, _BOTTOM = 70, 16, 36, 34
_MAX_DOTS = 300  # Above this many points only those that broke a rule get a dot.

EXTRA_STYLE = """
.flag { color: var(--fail); }
section.measurement { border-top: 1px solid var(--line); margin-top: 2rem; }
"""


def _scale(low: float, high: float, start: float, end: float) -> Callable[[float], float]:
    """Map low..high onto start..end. Halved first, so a span wider than the largest float cannot overflow."""
    half = high * 0.5 - low * 0.5
    if not math.isfinite(half) or half <= 0:
        middle = (start + end) / 2.0
        return lambda value: middle
    return lambda value: start + (value * 0.5 - low * 0.5) / half * (end - start)


def _padded(values: Sequence[float]) -> List[float]:
    low, high = min(values), max(values)
    if low == high:
        span = abs(low) * 0.1 or 1.0
        return [low - span, high + span]
    margin = (high * 0.5 - low * 0.5) * 0.16
    padded = [low - margin, high + margin]
    return [p if math.isfinite(p) else v for p, v in zip(padded, (low, high))]


def _frame(label: str, title: str, y_low: float, y_high: float, x_left: str, x_right: str, body: str) -> str:
    return (
        f'<svg viewBox="0 0 {_WIDTH} {_HEIGHT}" role="img" aria-label="{_esc(label)}">'
        f'<rect x="{_LEFT}" y="{_TOP}" width="{_WIDTH - _LEFT - _RIGHT}" height="{_HEIGHT - _TOP - _BOTTOM}" '
        'fill="none" stroke="#d9dee7"/>'
        f'<text x="{_LEFT}" y="17" font-size="12" font-weight="600" fill="#1d2433">{_esc(title)}</text>'
        f'<text x="{_LEFT - 6}" y="{_TOP + 10}" font-size="11" text-anchor="end" fill="#5d6779">{_esc(y_high)}</text>'
        f'<text x="{_LEFT - 6}" y="{_HEIGHT - _BOTTOM}" font-size="11" text-anchor="end" fill="#5d6779">{_esc(y_low)}</text>'
        f'<text x="{_LEFT}" y="{_HEIGHT - 18}" font-size="11" fill="#5d6779">{_esc(x_left)}</text>'
        f'<text x="{_WIDTH - _RIGHT}" y="{_HEIGHT - 18}" font-size="11" text-anchor="end" fill="#5d6779">{_esc(x_right)}</text>'
        f"{body}</svg>"
    )


def _vertical(x: float, colour: str, label: str, dashed: bool = True) -> str:
    dash = ' stroke-dasharray="5 4"' if dashed else ""
    return (
        f'<line x1="{x:.1f}" y1="{_TOP}" x2="{x:.1f}" y2="{_HEIGHT - _BOTTOM}" stroke="{colour}"{dash}/>'
        f'<text x="{x:.1f}" y="{_TOP - 3}" font-size="10" text-anchor="middle" fill="{colour}">{_esc(label)}</text>'
    )


def _horizontal(y: float, colour: str, label: str, dash: str = "5 4", width: float = 1.0) -> str:
    pattern = f' stroke-dasharray="{dash}"' if dash else ""
    return (
        f'<line x1="{_LEFT}" y1="{y:.1f}" x2="{_WIDTH - _RIGHT}" y2="{y:.1f}" stroke="{colour}" stroke-width="{width}"{pattern}/>'
        + (f'<text x="{_WIDTH - _RIGHT - 3}" y="{y - 3:.1f}" font-size="10" text-anchor="end" fill="{colour}">{_esc(label)}</text>' if label else "")
    )


def histogram_svg(summary: Dict[str, Any], histogram: Dict[str, Any]) -> str:
    """Bars of the histogram, with the limits and the mean drawn over them."""
    edges, counts = histogram["edges"], histogram["counts"]
    limits = [v for v in (summary["low"], summary["high"]) if v is not None]
    x_low, x_high = _padded(list(edges) + limits)
    px = _scale(x_low, x_high, _LEFT, _WIDTH - _RIGHT)
    tallest = max(counts)
    py = _scale(0.0, float(tallest), _HEIGHT - _BOTTOM, _TOP + 12)
    bars = []
    for index, number in enumerate(counts):
        left, right = px(edges[index]), px(edges[index + 1])
        if right - left < 2.0:  # A bin with no spread still gets a visible bar.
            left, right = left - 2.0, right + 2.0
        top = py(float(number))
        bars.append(
            f'<rect x="{left:.1f}" y="{top:.1f}" width="{max(right - left - 1.0, 1.0):.1f}" '
            f'height="{_HEIGHT - _BOTTOM - top:.1f}" fill="#2a5db0" fill-opacity="0.75"/>'
        )
    marks = [_vertical(px(value), "#b3261e", f"{name} {value:.6g}") for name, value in (("low", summary["low"]), ("high", summary["high"])) if value is not None]
    if summary["mean"] is not None:
        marks.append(_vertical(px(summary["mean"]), "#1b7a43", "mean", dashed=False))
    title = f"{describe(summary)}: {histogram['bins']} bins ({histogram['rule']})"
    return _frame(f"Histogram of {describe(summary)}", title, 0, tallest, f"{x_low:.5g}", f"{x_high:.5g}", "".join(bars + marks))


def control_svg(summary: Dict[str, Any], points: Sequence[Dict[str, Any]], chart: Dict[str, Any]) -> str:
    """The individuals chart: every value in run order, centre line, 1, 2 and 3 sigma lines, and points that broke a rule."""
    centre, sigma = chart["centre"], chart["sigma"]
    values = [p["value"] for p in points]
    lines = [v for v in (chart["lcl"], chart["ucl"]) if v is not None]
    y_low, y_high = _padded(list(values) + lines)
    total = len(values)
    px = _scale(1.0, float(max(total, 2)), _LEFT + 4, _WIDTH - _RIGHT - 4)
    py = _scale(y_low, y_high, _HEIGHT - _BOTTOM, _TOP)
    body = []
    for k in (1.0, 2.0):
        for side in (-1.0, 1.0):
            level = centre + side * k * sigma
            if math.isfinite(level):
                body.append(_horizontal(py(level), "#d9dee7", "", dash="2 3"))
    body.append(_horizontal(py(centre), "#1b7a43", f"CL {centre:.6g}", dash=""))
    for name, level in (("UCL", chart["ucl"]), ("LCL", chart["lcl"])):
        if level is not None:
            body.append(_horizontal(py(level), "#b3261e", f"{name} {level:.6g}"))
    path = " ".join(f"{px(i + 1.0):.1f},{py(v):.1f}" for i, v in enumerate(values))
    body.append(f'<polyline points="{path}" fill="none" stroke="#2a5db0" stroke-width="1.2"/>')
    flagged = {i for i, p in enumerate(points) if p["rules"]}
    for i, value in enumerate(values):
        if i in flagged:
            body.append(f'<circle cx="{px(i + 1.0):.1f}" cy="{py(value):.1f}" r="3.4" fill="#b3261e"/>')
        elif total <= _MAX_DOTS:
            body.append(f'<circle cx="{px(i + 1.0):.1f}" cy="{py(value):.1f}" r="2.2" fill="#2a5db0"/>')
    title = f"{describe(summary)}: individuals chart, {total} points"
    return _frame(f"Control chart of {describe(summary)}", title, f"{y_low:.5g}", f"{y_high:.5g}", "point 1", f"point {total}", "".join(body))


def describe(summary: Dict[str, Any]) -> str:
    """step / name at point (units), as plain text; escaped where it is used."""
    where = ", ".join(f"{k}={v:g}" for k, v in summary["point"].items())
    units = f" ({summary['units']})" if summary["units"] else ""
    return f"{summary['step']} / {summary['name']}{' at ' + where if where else ''}{units}"


def limits_text(summary: Dict[str, Any]) -> str:
    text = f"limits {_num(summary['low']) or '-inf'} to {_num(summary['high']) or '+inf'}"
    if summary["limit_sets"] > 1:
        text += f" (limit set {summary['limit_set']} of {summary['limit_sets']})"
    return text


def ratio(value: Optional[float]) -> str:
    return "" if value is None else f"{value:.2f}"


def percent(value: Optional[float]) -> str:
    return "" if value is None else f"{value * 100:.1f}%"


def _table(headers: Sequence[str], rows: List[Sequence[Any]], numeric_from: int) -> str:
    def cell(tag: str, index: int, text: Any) -> str:
        css = ' class="num"' if index >= numeric_from else ""
        return f"<{tag}{css}>{_esc(text)}</{tag}>"

    head = "".join(cell("th", i, h) for i, h in enumerate(headers))
    body = "".join("<tr>" + "".join(cell("td", i, c) for i, c in enumerate(row)) + "</tr>" for row in rows)
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _yield_section(entries: List[Dict[str, Any]]) -> str:
    rows = [
        (
            e["plan"], e["runs"], e["counts"]["pass"], e["counts"]["fail"], e["counts"]["aborted"], e["counts"]["running"],
            e["partial_runs"], e["units_with_verdict"], percent(e["first_pass_yield"]), percent(e["final_yield"]), e["retests"],
        )
        for e in entries
    ]
    headers = ("Plan", "Runs", "Pass", "Fail", "Aborted", "Running", "Some steps", "Units", "First-pass yield", "Final yield", "Retests")
    return "<h2>Yield</h2>" + _table(headers, rows, 1)


def _pareto_section(pareto: Dict[str, Any]) -> str:
    out = [f'<h2>Failures</h2><p class="muted">{_esc(pareto_sentence(pareto))}</p>']
    if pareto["rows"]:
        rows = [(r["plan"], r["step"], r["name"], r["runs"], r["readings"], percent(r["share"]), percent(r["cumulative"])) for r in pareto["rows"]]
        out.append(_table(("Plan", "Step", "Measurement", "Runs", "Readings", "Share", "Cumulative"), rows, 3))
    return "".join(out)


def _summary_section(measurements: List[Dict[str, Any]]) -> str:
    rows = []
    for s in measurements:
        c = s["capability"]
        rows.append(
            (
                s["plan"], s["step"], s["name"], ", ".join(f"{k}={v:g}" for k, v in s["point"].items()), s["units"],
                _num(s["low"]), _num(s["high"]), s["count"], s["no_value"], s["fails"], _num(s["mean"]), _num(s["stdev"]),
                _num(s["minimum"]), _num(s["median"]), _num(s["maximum"]),
                ratio(c["cp"]), ratio(c["cpk"]), ratio(c["pp"]), ratio(c["ppk"]),
            )
        )
    headers = ("Plan", "Step", "Measurement", "Point", "Units", "Low", "High", "n", "No value", "Fails", "Mean", "Stdev", "Min", "Median", "Max", "Cp", "Cpk", "Pp", "Ppk")
    return "<h2>Measurements</h2>" + _table(headers, rows, 5)


def _rules_list(chart: Dict[str, Any], points: Sequence[Dict[str, Any]]) -> str:
    items = []
    for rule in chart["rules"]:
        broke = [points[n - 1] for n in rule["points"]]
        shown = ", ".join(f"{p['number']} (run {p['run_id']}, {p['serial']})" for p in broke[:20])
        if len(broke) > 20:
            shown += f" and {len(broke) - 20} more"
        css = ' class="flag"' if broke else ""
        items.append(f"<li{css}>Rule {rule['rule']} ({_esc(rule['description'])}): {_esc(shown or 'none')}</li>")
    return f"<ul>{''.join(items)}</ul>"


def _measurement_section(item: Dict[str, Any]) -> str:
    summary = item["measurement"]
    out = [f'<section class="measurement"><h2>{_esc(describe(summary))}</h2>']
    out.append(f'<p class="muted">Plan {_esc(summary["plan"])}, {_esc(limits_text(summary))}, {summary["count"]} values</p>')
    if item.get("histogram"):
        out.append(histogram_svg(summary, item["histogram"]))
    chart = item.get("chart")
    if chart is not None:
        if chart["note"]:
            out.append(f'<p class="muted">No control chart: {_esc(chart["note"])}.</p>')
        else:
            out.append(control_svg(summary, item["points"], chart))
            out.append(
                f'<p class="muted">Centre {_num(chart["centre"])}, sigma {_num(chart["sigma"])} from the average moving range '
                f'{_num(chart["mr_centre"])}; moving range limit {_num(chart["mr_ucl"])}.</p>'
            )
            out.append(_rules_list(chart, item["points"]))
    out.append("</section>")
    return "".join(out)


def render_html(report: Dict[str, Any]) -> str:
    title = "Production statistics"
    chosen = ", ".join(f"{k} {v}" for k, v in report["selection"].items() if v is not None)
    return (
        '<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_esc(title)}</title><style>{STYLE}{EXTRA_STYLE}</style></head><body>"
        f'<h1>{_esc(title)}</h1><p class="muted">{_esc(chosen)}</p>{render_sections(report)}</body></html>\n'
    )


def render_sections(report: Dict[str, Any]) -> str:
    """The page below its heading: how many runs were used, then each part the report holds."""
    out = [f"<p>{_esc(runs_sentence(report['runs']))}</p>"]
    if "yield" in report:
        out.append(_yield_section(report["yield"]))
    if "pareto" in report:
        out.append(_pareto_section(report["pareto"]))
    if "measurements" in report:
        out.append(_summary_section(report["measurements"]))
    for item in report.get("charts", []):
        out.append(_measurement_section(item))
    return "".join(out)


def pareto_sentence(pareto: Dict[str, Any]) -> str:
    return f"{count(pareto['failed_runs'], 'failed run')} of {count(pareto['complete_runs'], 'complete run')}."


def runs_sentence(runs: Dict[str, int]) -> str:
    """One line saying how many runs were used and what was left out."""
    left_out = [
        text
        for number, text in (
            (runs["aborted"], f"{runs['aborted']} aborted"),
            (runs["running"], f"{runs['running']} never finished"),
            (runs["partial"], f"{runs['partial']} of only some steps"),
        )
        if number
    ]
    sentence = f"{runs['selected']} runs selected, {runs['complete']} complete runs used"
    return sentence + (f"; left out: {', '.join(left_out)}." if left_out else ".")
