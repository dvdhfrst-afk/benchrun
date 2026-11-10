"""A self-contained HTML report: one file, no scripts, no external resources."""

from __future__ import annotations

from html import escape
from typing import Any, Dict, List, Optional

STYLE = """
:root { color-scheme: light; --ink: #1d2433; --muted: #5d6779; --line: #d9dee7; --band: #f3f5f9;
        --pass: #1b7a43; --fail: #b3261e; --abort: #a15c00; --info: #5d6779; --accent: #2a5db0; }
* { box-sizing: border-box; }
body { margin: 0 auto; max-width: 70rem; padding: 1.5rem 1rem 3rem; color: var(--ink); background: #fbfcfe;
       font: 15px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
h1 { font-size: 1.5rem; margin: 0 0 .25rem; }
h2 { font-size: 1.1rem; margin: 2rem 0 .5rem; }
.badge { display: inline-block; padding: .15rem .6rem; border-radius: .3rem; color: #fbfcfe; font-weight: 600;
         font-size: .85rem; text-transform: uppercase; letter-spacing: .03em; vertical-align: middle; }
.pass { background: var(--pass); } .fail { background: var(--fail); } .aborted, .error { background: var(--abort); }
.running, .skipped, .info, .pending { background: var(--info); }
.reason { border-left: 4px solid var(--abort); padding: .5rem .75rem; background: var(--band); margin: 1rem 0; }
table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: .3rem .6rem; border-bottom: 1px solid var(--line); vertical-align: top; }
th { background: var(--band); font-weight: 600; }
td.num, th.num { text-align: right; }
tr.bad td { background: #fdf0ef; }
.scroll { overflow-x: auto; }
.muted { color: var(--muted); }
svg { display: block; margin: .75rem 0; max-width: 100%; height: auto; }
code { font-family: ui-monospace, Consolas, monospace; font-size: .9em; }
"""


def _esc(value: Any) -> str:
    return escape("" if value is None else str(value))


def _badge(status: str) -> str:
    return f'<span class="badge {_esc(status)}">{_esc(status)}</span>'


def _num(value: Optional[float]) -> str:
    return "" if value is None else f"{value:.6g}"


def _point(point: Dict[str, float]) -> str:
    return ", ".join(f"{_esc(k)}={v:g}" for k, v in point.items())


def _plot(step: Dict[str, Any]) -> str:
    """Inline plots of each limited measurement over a single-variable sweep."""
    series: Dict[str, List[Dict[str, Any]]] = {}
    for m in step["measurements"]:
        if len(m["point"]) == 1 and m["value"] is not None:
            series.setdefault(m["name"], []).append(m)
    parts = []
    for name, rows in series.items():
        if len(rows) >= 3 and any(r["low"] is not None or r["high"] is not None for r in rows):
            parts.append(_plot_series(name, rows))
    return "".join(parts)


def _plot_series(name: str, rows: List[Dict[str, Any]]) -> str:
    width, height = 640, 200
    left, right, top, bottom = 70, 16, 24, 34
    var = next(iter(rows[0]["point"]))
    xs = [r["point"][var] for r in rows]
    ys = [r["value"] for r in rows]
    lows = [r["low"] for r in rows if r["low"] is not None]
    highs = [r["high"] for r in rows if r["high"] is not None]
    y_min = min(ys + lows)
    y_max = max(ys + highs)
    if y_max == y_min:
        y_min, y_max = y_min - 1.0, y_max + 1.0
    margin = (y_max - y_min) * 0.08
    y_min -= margin
    y_max += margin
    x_min, x_max = min(xs), max(xs)
    x_span = (x_max - x_min) or 1.0

    def px(x: float) -> float:
        return left + (x - x_min) / x_span * (width - left - right)

    def py(y: float) -> float:
        return top + (y_max - y) / (y_max - y_min) * (height - top - bottom)

    line = " ".join(f"{px(x):.1f},{py(y):.1f}" for x, y in zip(xs, ys))
    marks = []
    for limit_key in ("low", "high"):
        points = [(r["point"][var], r[limit_key]) for r in rows if r[limit_key] is not None]
        if points:
            joined = " ".join(f"{px(x):.1f},{py(y):.1f}" for x, y in points)
            marks.append(f'<polyline points="{joined}" fill="none" stroke="#b3261e" stroke-dasharray="5 4"/>')
    dots = "".join(
        f'<circle cx="{px(r["point"][var]):.1f}" cy="{py(r["value"]):.1f}" r="2.6" '
        f'fill="{"#b3261e" if r["verdict"] == "fail" else "#2a5db0"}"/>'
        for r in rows
    )
    units = _esc(rows[0]["units"])
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{_esc(name)} versus {_esc(var)}">'
        f'<rect x="{left}" y="{top}" width="{width - left - right}" height="{height - top - bottom}" '
        'fill="none" stroke="#d9dee7"/>'
        f'<text x="{left}" y="16" font-size="12" font-weight="600" fill="#1d2433">{_esc(name)} ({units})</text>'
        f'<text x="{left - 6}" y="{top + 10}" font-size="11" text-anchor="end" fill="#5d6779">{y_max:.5g}</text>'
        f'<text x="{left - 6}" y="{height - bottom}" font-size="11" text-anchor="end" fill="#5d6779">{y_min:.5g}</text>'
        f'<text x="{left}" y="{height - 18}" font-size="11" fill="#5d6779">{x_min:g}</text>'
        f'<text x="{width - right}" y="{height - 18}" font-size="11" text-anchor="end" fill="#5d6779">{x_max:g}</text>'
        f'<text x="{(left + width - right) / 2:.0f}" y="{height - 6}" font-size="11" text-anchor="middle" '
        f'fill="#5d6779">{_esc(var)}</text>'
        f"{''.join(marks)}"
        f'<polyline points="{line}" fill="none" stroke="#2a5db0" stroke-width="1.5"/>{dots}</svg>'
    )


def _measurement_table(step: Dict[str, Any]) -> str:
    rows = []
    for m in step["measurements"]:
        bad = ' class="bad"' if m["verdict"] in ("fail", "error") else ""
        rows.append(
            f"<tr{bad}><td>{_esc(m['name'])}</td><td>{_point(m['point'])}</td>"
            f'<td class="num">{_num(m["value"])}</td><td>{_esc(m["units"])}</td>'
            f'<td class="num">{_num(m["low"])}</td><td class="num">{_num(m["high"])}</td>'
            f"<td>{_badge(m['verdict'])}</td><td>{_esc(m['note'])}</td></tr>"
        )
    return (
        '<div class="scroll"><table><thead><tr><th>Measurement</th><th>Point</th><th class="num">Value</th>'
        '<th>Units</th><th class="num">Low</th><th class="num">High</th><th>Verdict</th><th>Note</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


def render_html(detail: Dict[str, Any]) -> str:
    counts = {"pass": 0, "fail": 0, "error": 0, "info": 0}
    for step in detail["steps"]:
        for m in step["measurements"]:
            counts[m["verdict"]] = counts.get(m["verdict"], 0) + 1
    title = f"Run {detail['id']}: {detail['serial']}"
    out = [
        '<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{_esc(title)}</title><style>{STYLE}</style></head><body>",
        f"<h1>{_esc(title)} {_badge(detail['status'])}</h1>",
        f'<p class="muted">{_esc(detail["plan_name"])} {_esc(detail["plan_version"])}: '
        f'{counts["pass"]} passed, {counts["fail"]} failed, {counts["error"]} errors, '
        f'{counts["info"]} recorded without limits</p>',
    ]
    if detail["reason"]:
        out.append(f'<div class="reason"><strong>Why the run was aborted:</strong> {_esc(detail["reason"])}</div>')
    if detail["status"] == "running":
        out.append(
            '<div class="reason"><strong>This run never finished.</strong> Either it is still going, or the process '
            "stopped before it could record a result. Everything below was saved as it was measured. Check the "
            "bench: an output may still be on.</div>"
        )
    if detail.get("selected_steps") is not None:
        out.append(
            '<div class="reason"><strong>Only some steps were run:</strong> '
            f'{_esc(", ".join(detail["selected_steps"]))}. This result does not cover the whole plan, and it is left '
            "out of yield and statistics.</div>"
        )
    out.append('<h2>Run</h2><div class="scroll"><table><tbody>')
    variables = ", ".join(f"{k}={v:g}" for k, v in detail["variables"].items())
    for label, value in (
        ("Serial number", detail["serial"]),
        ("Station", detail.get("station")),
        ("Operator", detail["operator"]),
        ("Note", detail.get("note")),
        ("Started (UTC)", detail["started_at"]),
        ("Finished (UTC)", detail["finished_at"]),
        ("Plan variables", variables),
        ("Plan SHA-256", detail["plan_hash"]),
        ("benchrun version", detail["software_version"]),
    ):
        out.append(f"<tr><th>{_esc(label)}</th><td>{_esc(value)}</td></tr>")
    out.append("</tbody></table></div><h2>Instruments</h2>")
    out.append(
        '<div class="scroll"><table><thead><tr><th>Name</th><th>Driver</th><th>Address</th>'
        "<th>Asset tag</th><th>*IDN? response</th></tr></thead><tbody>"
    )
    for instrument in detail["instruments"]:
        out.append(
            f"<tr><td>{_esc(instrument['name'])}</td><td>{_esc(instrument['driver'])}</td>"
            f"<td>{_esc(instrument['address'])}</td><td>{_esc(instrument.get('asset_tag'))}</td>"
            f"<td><code>{_esc(instrument['idn'])}</code></td></tr>"
        )
    out.append("</tbody></table></div>")
    for step in detail["steps"]:
        quiet = step["kind"] != "test" and step["status"] == "pass"
        if quiet:
            continue
        out.append(f"<h2>{_esc(step['name'])} {_badge(step['status'])}</h2>")
        if step["message"]:
            out.append(f"<p>{_esc(step['message'])}</p>")
        if step["measurements"]:
            out.append(_plot(step))
            out.append(_measurement_table(step))
    out.append("</body></html>\n")
    return "".join(out)
