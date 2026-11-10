"""What the human-readable reports say about a run, worded once.

The verdict is the part that matters most: a run that did not cover the whole
plan, or did not end with every limited measurement passing, must never read
as a pass in any report, whoever opens it and however quickly.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Iterator, Optional, Tuple

VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"
VERDICT_ABORTED = "ABORTED"
VERDICT_PARTIAL = "PARTIAL"
VERDICT_UNFINISHED = "NOT FINISHED"


def is_partial(detail: Dict[str, Any]) -> bool:
    return detail.get("selected_steps") is not None


def passed(detail: Dict[str, Any]) -> bool:
    """True only for a complete run of the whole plan that passed."""
    return detail["status"] == "pass" and not is_partial(detail)


def verdict(detail: Dict[str, Any]) -> Tuple[str, str]:
    """A one-word verdict and a plain sentence saying what it means for the unit."""
    status = detail["status"]
    selected = ", ".join(detail.get("selected_steps") or [])
    only_some = f" Only some steps were run ({selected}), so this is not a full test." if is_partial(detail) else ""
    if status == "pass" and not only_some:
        return VERDICT_PASS, "The unit passed: every limited measurement in every step of the plan was within its limits."
    if status == "pass":
        return VERDICT_PARTIAL, f"The unit has not passed.{only_some} The steps that were run passed."
    if status == "fail":
        return VERDICT_FAIL, f"The unit did not pass: at least one measurement was outside its limits.{only_some}"
    if status == "aborted":
        why = f" Reason: {detail['reason']}" if detail.get("reason") else ""
        return VERDICT_ABORTED, f"The unit did not pass: the run was aborted, so it has no verdict.{only_some}{why}"
    if status == "running":
        return VERDICT_UNFINISHED, (
            "The unit did not pass: the run never finished. It is still going, or the process stopped "
            f"before it could record a result.{only_some}"
        )
    return status.upper() or "UNKNOWN", f"The unit did not pass: the recorded result is {status!r}.{only_some}"


def measurement_counts(detail: Dict[str, Any]) -> Dict[str, int]:
    counts = {"measurements": 0, "pass": 0, "fail": 0, "error": 0, "info": 0}
    for step in detail["steps"]:
        for m in step["measurements"]:
            counts["measurements"] += 1
            counts[m["verdict"]] = counts.get(m["verdict"], 0) + 1
    return counts


def step_counts(step: Dict[str, Any]) -> Tuple[int, int]:
    """How many measurements a step took, and how many of them failed or could not be taken."""
    measurements = step["measurements"]
    return len(measurements), sum(1 for m in measurements if m["verdict"] in ("fail", "error"))


def failed_measurements(detail: Dict[str, Any]) -> Iterator[Tuple[Dict[str, Any], Dict[str, Any]]]:
    for step in detail["steps"]:
        for m in step["measurements"]:
            if m["verdict"] in ("fail", "error"):
                yield step, m


def finite(value: Optional[float]) -> Optional[float]:
    """A number a file format can hold, or None. Databases written elsewhere could hold anything."""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def number(value: Optional[float]) -> str:
    value = finite(value)
    return "" if value is None else f"{value:.6g}"


def point_text(point: Dict[str, Any]) -> str:
    return ", ".join(f"{key}={number(value)}" for key, value in point.items())


def limits_text(m: Dict[str, Any]) -> str:
    if m["low"] is None and m["high"] is None:
        return "no limits"
    return f"{number(m['low']) or '-inf'} to {number(m['high']) or '+inf'}"


def value_text(m: Dict[str, Any]) -> str:
    return "no value" if finite(m["value"]) is None else f"{number(m['value'])} {m['units']}".rstrip()
