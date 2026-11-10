"""A printable one-page test certificate for one unit, with lines for a reviewer to sign.

It is a self-contained HTML file: no scripts and nothing loaded from elsewhere.
Only a complete run of the whole plan that passed is shown as a pass. Anything
else carries its verdict in large type and a "NOT PASSED" mark across the page,
in ink rather than background colour, so it survives a black and white printer.
"""

from __future__ import annotations

import re
from html import escape
from typing import Any, Dict, List

from benchrun.reports.summary import passed, step_counts, verdict

# A plan this long would not fit on a page; the steps that did not pass are always listed.
MAX_STEPS_LISTED = 20

_CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f]")

STYLE = """
@page { size: auto; margin: 14mm; }
:root { color-scheme: light; --ink: #1d2433; --muted: #5d6779; --line: #c9d0db; --pass: #1b7a43; --fail: #b3261e; }
* { box-sizing: border-box; }
body { margin: 0 auto; max-width: 52rem; padding: 1.25rem 1rem; color: var(--ink); background: #fbfcfe;
       font: 12.5px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif; position: relative; }
header { display: flex; justify-content: space-between; align-items: baseline; border-bottom: 2px solid var(--ink);
         padding-bottom: .4rem; margin-bottom: .9rem; }
h1 { font-size: 1.45rem; margin: 0; letter-spacing: .02em; }
h2 { font-size: .95rem; margin: 1rem 0 .35rem; text-transform: uppercase; letter-spacing: .05em; }
.verdict { border: 4px solid var(--fail); color: var(--fail); padding: .6rem .9rem; margin: 0 0 .9rem; }
.verdict.passed { border-color: var(--pass); color: var(--pass); }
.verdict strong { display: block; font-size: 1.9rem; letter-spacing: .06em; }
.verdict p { margin: .2rem 0 0; color: var(--ink); font-size: .95rem; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: .2rem .45rem; border-bottom: 1px solid var(--line); vertical-align: top; }
thead th { border-bottom: 1.5px solid var(--ink); }
.facts th { width: 11rem; font-weight: 600; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
code { font-family: ui-monospace, Consolas, monospace; font-size: .92em; word-break: break-all; }
.muted { color: var(--muted); }
.review { display: grid; grid-template-columns: 2fr 2fr 1fr; gap: 1.2rem; margin-top: 1.6rem; }
.review div { border-top: 1px solid var(--ink); padding-top: .25rem; }
.mark { position: absolute; top: 40%; left: 0; right: 0; text-align: center; font-size: 5.5rem; font-weight: 800;
        color: rgba(179, 38, 30, .16); transform: rotate(-24deg); pointer-events: none; letter-spacing: .08em; }
footer { margin-top: 1.2rem; font-size: .85rem; }
@media print { body { background: none; padding: 0; max-width: none; } }
"""


def _esc(value: Any) -> str:
    return escape(_CONTROL.sub("", "" if value is None else str(value)))


def _facts(detail: Dict[str, Any]) -> str:
    rows = [
        ("Unit serial", detail["serial"]),
        ("Product", detail.get("product")),
        ("Test plan", detail["plan_name"]),
        ("Plan version", detail["plan_version"]),
        ("Plan SHA-256", detail["plan_hash"]),
        ("Steps selected", ", ".join(detail["selected_steps"]) if detail.get("selected_steps") is not None else None),
        ("Started (UTC)", detail["started_at"]),
        ("Finished (UTC)", detail["finished_at"] or "never finished"),
        ("Operator", detail["operator"] or "not recorded"),
        ("Station", detail.get("station")),
    ]
    cells = []
    for label, value in rows:
        if value in (None, ""):
            continue
        shown = f"<code>{_esc(value)}</code>" if label == "Plan SHA-256" else _esc(value)
        cells.append(f"<tr><th>{_esc(label)}</th><td>{shown}</td></tr>")
    return '<table class="facts"><tbody>' + "".join(cells) + "</tbody></table>"


def _instruments(detail: Dict[str, Any]) -> str:
    if not detail["instruments"]:
        return '<p class="muted">No instruments were recorded.</p>'
    missing = '<span class="muted">not recorded</span>'
    rows = "".join(
        f"<tr><td>{_esc(i['name'])}</td><td>{_esc(i['driver'])}</td><td><code>{_esc(i['idn'])}</code></td>"
        f"<td>{_esc(i.get('asset_tag')) or missing}</td></tr>"
        for i in detail["instruments"]
    )
    return (
        "<table><thead><tr><th>Instrument</th><th>Driver</th><th>Identity (*IDN? response)</th><th>Asset tag</th>"
        f"</tr></thead><tbody>{rows}</tbody></table>"
    )


def _steps(detail: Dict[str, Any]) -> str:
    steps = detail["steps"]
    if not steps:
        return '<p class="muted">No steps were recorded.</p>'
    shown: List[Dict[str, Any]] = steps
    if len(steps) > MAX_STEPS_LISTED:
        shown = [s for s in steps if s["status"] != "pass"][:MAX_STEPS_LISTED]
    rows = []
    for step in shown:
        taken, bad = step_counts(step)
        rows.append(
            f"<tr><td>{_esc(step['name'])}</td><td>{_esc(step['status'])}</td>"
            f'<td class="num">{taken}</td><td class="num">{bad}</td></tr>'
        )
    note = ""
    if len(shown) < len(steps):
        passing = sum(1 for s in steps if s["status"] == "pass")
        note = (
            f'<p class="muted">{passing} of the {len(steps)} steps passed; '
            f"{len(steps) - len(shown)} steps are not listed here. The full report lists every step.</p>"
        )
    if not rows:
        return note
    return (
        '<table><thead><tr><th>Step</th><th>Status</th><th class="num">Measured</th><th class="num">Failed</th>'
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table>{note}"
    )


def render_certificate(detail: Dict[str, Any]) -> str:
    word, sentence = verdict(detail)
    good = passed(detail)
    title = f"Test certificate: {detail['serial']}: {word if good else 'NOT PASSED'}"
    out = [
        '<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{_esc(title)}</title><style>{STYLE}</style></head><body>",
    ]
    if not good:
        out.append('<div class="mark" aria-hidden="true">NOT PASSED</div>')
    out.append(f'<header><h1>Test certificate</h1><span class="muted">Run {detail["id"]}</span></header>')
    out.append(
        f'<div class="verdict{" passed" if good else ""}" role="status"><strong>{_esc(word)}</strong>'
        f"<p>{_esc(sentence)}</p></div>"
    )
    out.append(_facts(detail))
    out.append("<h2>Instruments</h2>")
    out.append(_instruments(detail))
    out.append("<h2>Steps</h2>")
    out.append(_steps(detail))
    out.append(
        '<h2>Review</h2><div class="review"><div>Reviewer name</div><div>Signature</div><div>Date</div></div>'
    )
    uuid = f", run UUID <code>{_esc(detail['uuid'])}</code>" if detail.get("uuid") else ""
    out.append(
        f'<footer class="muted">Written by benchrun {_esc(detail["software_version"])} from the results database '
        f"record of run {detail['id']}{uuid}.</footer>"
    )
    out.append("</body></html>\n")
    return "".join(out)
