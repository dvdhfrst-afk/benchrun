"""CSV report: one row per measurement, ready for a spreadsheet or a script."""

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
    for step in detail["steps"]:
        for m in step["measurements"]:
            writer.writerow(
                [
                    detail["id"],
                    detail["serial"],
                    detail["plan_name"],
                    detail["plan_version"],
                    detail["status"],
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
