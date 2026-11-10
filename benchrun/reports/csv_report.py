"""CSV report: one row per measurement, ready for a spreadsheet or a script.

A test step with no measurements still gets a row, as does a setup or teardown
that went wrong and a run with no steps, so the file always shows how far the
run got and why it stopped.

Free text that a spreadsheet would run as a formula gets a leading apostrophe.
Numbers are left alone. A serial number, a reason or a note can come from
outside, and opening a report must not be able to do anything but show it.
"""

from __future__ import annotations

import csv
import io
import json
from typing import IO, Any, Dict, Iterable, Iterator, Optional, Tuple

COLUMNS = (
    "run_id",
    "serial",
    "plan",
    "plan_version",
    "run_status",
    "run_reason",
    "step",
    "step_status",
    "measurement",
    "kind",
    "point",
    "value",
    "units",
    "low",
    "high",
    "verdict",
    "note",
    "instrument",
    "samples",
    "taken_at",
    "run_note",
)


def _cell(value: Any) -> Any:
    return "" if value is None else value


def _text(value: Any) -> str:
    text = "" if value is None else str(value)
    return f"'{text}" if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def report_rows(detail: Dict[str, Any]) -> Iterator[Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]]:
    """Each row a run gets, as (step, measurement): either can be None where there is nothing to show."""
    if not detail["steps"]:
        yield None, None
    for step in detail["steps"]:
        if not step["measurements"] and (step["kind"] == "test" or step["status"] != "pass"):
            yield step, None
        for m in step["measurements"]:
            yield step, m


def write_rows(writer: Any, detail: Dict[str, Any]) -> None:
    """Add one run's rows to a ``csv.writer``."""
    run_cells = [
        detail["id"],
        _text(detail["serial"]),
        _text(detail["plan_name"]),
        _text(detail["plan_version"]),
        detail["status"],
        _text(detail["reason"]),
    ]
    run_note = _text(detail.get("note"))
    blank = [""] * (len(COLUMNS) - len(run_cells) - 4)
    for step, m in report_rows(detail):
        if step is None:
            writer.writerow(run_cells + ["", "", ""] + blank + [run_note])
        elif m is None:
            row = run_cells + [step["name"], step["status"]] + blank + ["", run_note]
            row[COLUMNS.index("note")] = _text(step["message"])
            writer.writerow(row)
        else:
            writer.writerow(
                run_cells
                + [
                    step["name"],
                    step["status"],
                    m["name"],
                    m["kind"],
                    json.dumps(m["point"], sort_keys=True) if m["point"] else "",
                    _cell(None if m["value"] is None else repr(m["value"])),
                    _text(m["units"]),
                    _cell(m["low"]),
                    _cell(m["high"]),
                    m["verdict"],
                    _text(m["note"]),
                    _cell(m["instrument"]),
                    m["samples"],
                    m["taken_at"],
                    run_note,
                ]
            )


def render_csv(detail: Dict[str, Any]) -> str:
    return render_many([detail])


def write_csv(handle: IO[str], details: Iterable[Dict[str, Any]]) -> None:
    """One header, then the rows of every run given, taking one run at a time."""
    writer = csv.writer(handle, lineterminator="\n")
    writer.writerow(COLUMNS)
    for detail in details:
        write_rows(writer, detail)


def render_many(details: Iterable[Dict[str, Any]]) -> str:
    buffer = io.StringIO()
    write_csv(buffer, details)
    return buffer.getvalue()
