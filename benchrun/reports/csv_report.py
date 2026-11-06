"""CSV report: one row per measurement, ready for a spreadsheet or a script.

A test step with no measurements still gets a row, as does a setup or teardown
that went wrong and a run with no steps, so the file always shows how far the
run got and why it stopped.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any, Dict

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
)


def _cell(value: Any) -> Any:
    return "" if value is None else value


def render_csv(detail: Dict[str, Any]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(COLUMNS)
    run_cells = [
        detail["id"],
        detail["serial"],
        detail["plan_name"],
        detail["plan_version"],
        detail["status"],
        _cell(detail["reason"]),
    ]
    blank = [""] * (len(COLUMNS) - len(run_cells) - 3)
    if not detail["steps"]:
        writer.writerow(run_cells + ["", "", ""] + blank)
    for step in detail["steps"]:
        if not step["measurements"] and (step["kind"] == "test" or step["status"] != "pass"):
            row = run_cells + [step["name"], step["status"]] + blank + [""]
            row[COLUMNS.index("note")] = _cell(step["message"])
            writer.writerow(row)
        for m in step["measurements"]:
            writer.writerow(
                run_cells
                + [
                    step["name"],
                    step["status"],
                    m["name"],
                    m["kind"],
                    json.dumps(m["point"], sort_keys=True) if m["point"] else "",
                    _cell(None if m["value"] is None else repr(m["value"])),
                    m["units"],
                    _cell(m["low"]),
                    _cell(m["high"]),
                    m["verdict"],
                    _cell(m["note"]),
                    _cell(m["instrument"]),
                    m["samples"],
                    m["taken_at"],
                ]
            )
    return buffer.getvalue()
