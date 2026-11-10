"""Markdown report: a summary to paste into a ticket or a pull request.

Every piece of text from outside (a serial number, a note, an instrument's
answer) is escaped, so it shows as written and cannot add formatting, link
text, images, mentions, maths, HTML or table cells where it is pasted.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Sequence

from benchrun.reports.summary import (
    failed_measurements,
    limits_text,
    measurement_counts,
    number,
    point_text,
    step_counts,
    value_text,
    verdict,
)

_CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029]")
_LINE_BREAK = re.compile(r"\r\n|[\r\n]")
_SPECIAL = re.compile(r"([\\`*_{}\[\]<>#!|~&@$])")


def md(value: Any) -> str:
    """One line of literal text, whatever it holds."""
    text = _LINE_BREAK.sub(" ", "" if value is None else str(value))
    return _SPECIAL.sub(r"\\\1", _CONTROL.sub("", text))


def _table(headers: Sequence[str], rows: List[Sequence[str]]) -> List[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return lines


def _facts(pairs: Sequence[Any]) -> List[str]:
    return [f"- **{label}:** {md(value)}" for label, value in pairs if value not in (None, "")]


def render_markdown(detail: Dict[str, Any]) -> str:
    word, sentence = verdict(detail)
    word = md(word)
    counts = measurement_counts(detail)
    out = [
        f"# Run {detail['id']}: {md(detail['serial'])}: {word}",
        "",
        f"**Result: {word}.** {md(sentence)}",
        "",
        f"{counts['measurements']} measurements: {counts['pass']} passed, {counts['fail']} failed, "
        f"{counts['error']} could not be taken, {counts['info']} recorded without limits.",
        "",
        "## Unit",
        "",
    ]
    out += _facts(
        (
            ("Serial", detail["serial"]),
            ("Product", detail.get("product")),
            ("Station", detail.get("station")),
            ("Operator", detail["operator"]),
            ("Note", detail.get("note")),
            ("Started (UTC)", detail["started_at"]),
            ("Finished (UTC)", detail["finished_at"] or "never"),
            ("Run", f"{detail['id']}" + (f" ({detail['uuid']})" if detail.get("uuid") else "")),
        )
    )
    variables = ", ".join(f"{key}={number(value)}" for key, value in detail["variables"].items())
    out += ["", "## Plan", ""]
    out += _facts(
        (
            ("Name", detail["plan_name"]),
            ("Version", detail["plan_version"]),
            ("SHA-256", detail["plan_hash"]),
            ("Variables", variables),
            ("Steps selected", ", ".join(detail["selected_steps"]) if detail.get("selected_steps") is not None else None),
            ("benchrun version", detail["software_version"]),
        )
    )
    out += ["", "## Instruments", ""]
    if detail["instruments"]:
        out += _table(
            ("Name", "Driver", "Address", "Asset tag", "Identity"),
            [
                [md(i["name"]), md(i["driver"]), md(i["address"]), md(i.get("asset_tag")), md(i["idn"])]
                for i in detail["instruments"]
            ],
        )
    else:
        out.append("No instruments were recorded.")
    out += ["", "## Steps", ""]
    if detail["steps"]:
        rows = []
        for step in detail["steps"]:
            taken, bad = step_counts(step)
            rows.append([str(step["ordinal"]), md(step["name"]), md(step["kind"]), md(step["status"]), str(taken), str(bad), md(step["message"])])
        out += _table(("#", "Step", "Kind", "Status", "Measured", "Failed", "Message"), rows)
    else:
        out.append("No steps were recorded.")
    out += ["", "## Failed measurements", ""]
    failed = [
        [md(step["name"]), md(m["name"]), md(point_text(m["point"])), md(value_text(m)), md(limits_text(m)), md(m["verdict"]), md(m["note"])]
        for step, m in failed_measurements(detail)
    ]
    if failed:
        out += _table(("Step", "Measurement", "Point", "Value", "Limits", "Verdict", "Note"), failed)
    else:
        out.append("None.")
    out += ["", "## All measurements", ""]
    every = [
        [
            md(step["name"]),
            md(m["name"]),
            md(point_text(m["point"])),
            md(number(m["value"])),
            md(m["units"]),
            md(number(m["low"])),
            md(number(m["high"])),
            md(m["verdict"]),
        ]
        for step in detail["steps"]
        for m in step["measurements"]
    ]
    if every:
        out += _table(("Step", "Measurement", "Point", "Value", "Units", "Low", "High", "Verdict"), every)
    else:
        out.append("No measurements were recorded.")
    return "\n".join(out) + "\n"
