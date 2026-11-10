"""JSON report: the whole run record in one file, for a script or another system to read.

Keys always come in the same order, numbers stay numbers, and anything missing
is null. ``format`` and ``format_version`` let a reader refuse a file it does not
understand instead of guessing. docs/reports.md describes every field.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from benchrun.reports.summary import finite, is_partial, measurement_counts

FORMAT_NAME = "benchrun-run"
FORMAT_VERSION = 1


def _measurement(m: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": m["name"],
        "kind": m["kind"],
        "point": {key: finite(value) for key, value in m["point"].items()},
        "value": finite(m["value"]),
        "units": m["units"],
        "low": finite(m["low"]),
        "high": finite(m["high"]),
        "verdict": m["verdict"],
        "note": m["note"],
        "instrument": m["instrument"],
        "samples": m["samples"],
        "taken_at": m["taken_at"],
    }


def _steps(detail: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        {
            "ordinal": step["ordinal"],
            "name": step["name"],
            "kind": step["kind"],
            "status": step["status"],
            "message": step["message"],
            "started_at": step["started_at"],
            "finished_at": step["finished_at"],
            "measurements": [_measurement(m) for m in step["measurements"]],
        }
        for step in detail["steps"]
    ]


def run_record(detail: Dict[str, Any]) -> Dict[str, Any]:
    """The document the file holds, as Python data."""
    return {
        "format": FORMAT_NAME,
        "format_version": FORMAT_VERSION,
        "run": {
            "id": detail["id"],
            "uuid": detail.get("uuid"),
            "serial": detail["serial"],
            "product": detail.get("product") or "",
            "station": detail.get("station") or "",
            "lot_id": detail.get("lot_id"),
            "status": detail["status"],
            "reason": detail["reason"],
            "partial": is_partial(detail),
            "selected_steps": detail.get("selected_steps"),
            "operator": detail["operator"],
            "note": detail.get("note") or "",
            "started_at": detail["started_at"],
            "finished_at": detail["finished_at"],
            "software_version": detail["software_version"],
            "origin": detail.get("origin") or "",
            "imported_at": detail.get("imported_at"),
        },
        "plan": {
            "name": detail["plan_name"],
            "version": detail["plan_version"],
            "sha256": detail["plan_hash"],
            "variables": {key: finite(value) for key, value in detail["variables"].items()},
            "source": detail["plan_source"],
        },
        "instruments": [
            {
                "name": instrument["name"],
                "driver": instrument["driver"],
                "address": instrument["address"],
                "idn": instrument["idn"],
                "asset_tag": instrument.get("asset_tag"),
            }
            for instrument in detail["instruments"]
        ],
        "counts": measurement_counts(detail),
        "steps": _steps(detail),
    }


def render_json(detail: Dict[str, Any]) -> str:
    return json.dumps(run_record(detail), indent=2, ensure_ascii=False, allow_nan=False) + "\n"
