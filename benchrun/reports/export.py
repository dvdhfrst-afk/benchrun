"""Many runs in one file: CSV, JSON Lines or an Excel workbook.

Runs arrive one at a time, and each is written before the next is read, so the
size of an export is limited by the disk, not by memory. The file appears whole
or not at all.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import IO, Any, Callable, Dict, Iterable, Iterator, Union

from benchrun.files import atomic_open
from benchrun.reports.csv_report import report_rows, write_csv
from benchrun.reports.summary import finite, is_partial
from benchrun.reports.xlsx import write_xlsx


def jsonl_rows(detail: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    """The rows the CSV export has, with types kept: one per measurement, plus a row for
    each test step that measured nothing and for a run with no steps at all."""
    run = {
        "run_id": detail["id"],
        "run_uuid": detail.get("uuid"),
        "serial": detail["serial"],
        "product": detail.get("product") or "",
        "station": detail.get("station") or "",
        "plan": detail["plan_name"],
        "plan_version": detail["plan_version"],
        "plan_sha256": detail["plan_hash"],
        "run_status": detail["status"],
        "partial": is_partial(detail),
        "run_reason": detail["reason"],
        "operator": detail["operator"],
        "run_note": detail.get("note") or "",
        "run_started_at": detail["started_at"],
        "run_finished_at": detail["finished_at"],
    }
    for step, m in report_rows(detail):
        row = dict(run)
        row.update(
            step=step and step["name"],
            step_kind=step and step["kind"],
            step_status=step and step["status"],
            step_message=step and step["message"],
        )
        m = m or {}
        row.update(
            measurement=m.get("name"),
            kind=m.get("kind"),
            point={key: finite(value) for key, value in m["point"].items()} if m else None,
            value=finite(m.get("value")),
            units=m.get("units"),
            low=finite(m.get("low")),
            high=finite(m.get("high")),
            verdict=m.get("verdict"),
            note=m.get("note"),
            instrument=m.get("instrument"),
            samples=m.get("samples"),
            taken_at=m.get("taken_at"),
        )
        yield row


def write_jsonl(handle: IO[str], details: Iterable[Dict[str, Any]]) -> None:
    # ASCII only, so no reader can mistake a character inside a value for the end of a line.
    for detail in details:
        for row in jsonl_rows(detail):
            handle.write(json.dumps(row, allow_nan=False, separators=(",", ":")) + "\n")


_WRITERS: Dict[str, Callable[[Any, Iterable[Dict[str, Any]]], None]] = {
    "csv": write_csv,
    "jsonl": write_jsonl,
    "xlsx": write_xlsx,
}
EXPORT_FORMATS = tuple(_WRITERS)


def export_runs(details: Iterable[Dict[str, Any]], out: Union[str, Path], export_format: str) -> None:
    """Write every run ``details`` yields into ``out`` in ``export_format``."""
    with atomic_open(out, binary=export_format == "xlsx") as handle:
        _WRITERS[export_format](handle, details)
