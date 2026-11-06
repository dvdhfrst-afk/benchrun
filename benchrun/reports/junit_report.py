"""JUnit XML report, so a test system or build server can consume results."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any, Dict, List, Optional

# XML 1.0 cannot hold most control characters; instrument responses might.
_ILLEGAL_XML = re.compile("[^\t\n\r\u0020-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")


def _clean(text: Any) -> str:
    return _ILLEGAL_XML.sub("?", str(text))


def _seconds(start: Optional[str], end: Optional[str]) -> float:
    if not start or not end:
        return 0.0
    try:
        return max((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds(), 0.0)
    except ValueError:
        return 0.0


def _point_label(point: Dict[str, float]) -> str:
    if not point:
        return ""
    return "[" + ",".join(f"{key}={value:g}" for key, value in point.items()) + "]"


def _describe(m: Dict[str, Any]) -> str:
    value = "no value" if m["value"] is None else f"{m['value']:.9g} {m['units']}".rstrip()
    low = "-inf" if m["low"] is None else f"{m['low']:g}"
    high = "+inf" if m["high"] is None else f"{m['high']:g}"
    text = f"{value} outside limits {low} to {high}"
    return f"{text} ({m['note']})" if m["note"] else text


def render_junit(detail: Dict[str, Any]) -> str:
    cases: List[ET.Element] = []
    counts = {"failures": 0, "errors": 0, "skipped": 0}
    plan = detail["plan_name"]

    def add_case(classname: str, name: str, problem: Optional[str] = None, message: str = "") -> None:
        case = ET.Element("testcase", {"classname": _clean(classname), "name": _clean(name), "time": "0"})
        if problem:
            ET.SubElement(case, problem, {"message": _clean(message)}).text = _clean(message)
            counts[{"failure": "failures", "error": "errors", "skipped": "skipped"}[problem]] += 1
        cases.append(case)

    for step in detail["steps"]:
        classname = f"{plan}.{step['name']}"
        for m in step["measurements"]:
            name = m["name"] + _point_label(m["point"])
            if m["verdict"] == "fail":
                add_case(classname, name, "failure", _describe(m))
            elif m["verdict"] == "error":
                add_case(classname, name, "error", m["note"] or "measurement could not be taken")
            else:
                add_case(classname, name)
        if step["status"] == "skipped":
            add_case(classname, step["name"], "skipped", step["message"] or "not run")
        elif step["status"] == "error" and not any(m["verdict"] == "error" for m in step["measurements"]):
            # A fault outside a reading, such as a rejected setting or lost connection.
            add_case(classname, step["name"], "error", step["message"] or "step ended with an error")

    if detail["status"] == "aborted" and counts["errors"] == 0:
        # Every aborted run must show as a non-pass even if no step recorded it.
        add_case(f"{plan}.run", "run", "error", detail["reason"] or "run aborted")
    elif detail["status"] == "running":
        add_case(f"{plan}.run", "run", "error", "run never finished")

    suite = ET.Element(
        "testsuite",
        {
            "name": _clean(plan),
            "tests": str(len(cases)),
            "failures": str(counts["failures"]),
            "errors": str(counts["errors"]),
            "skipped": str(counts["skipped"]),
            "time": f"{_seconds(detail['started_at'], detail['finished_at']):.3f}",
            "timestamp": _clean(detail["started_at"]),
        },
    )
    properties = ET.SubElement(suite, "properties")

    def add_property(name: str, value: Any) -> None:
        ET.SubElement(properties, "property", {"name": name, "value": _clean("" if value is None else value)})

    add_property("run_id", detail["id"])
    add_property("serial", detail["serial"])
    add_property("run_status", detail["status"])
    add_property("reason", detail["reason"])
    add_property("operator", detail["operator"])
    add_property("plan_version", detail["plan_version"])
    add_property("plan_sha256", detail["plan_hash"])
    for instrument in detail["instruments"]:
        add_property(f"idn.{instrument['name']}", instrument["idn"])
        add_property(f"address.{instrument['name']}", instrument["address"])
    suite.extend(cases)

    root = ET.Element("testsuites")
    root.append(suite)
    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"
