"""Report writers. Each takes the dictionary from ``Store.run_detail``.

Every per-run format is one module, registered here by name with the ending
its file gets and a line saying what it is for. Commands take their format
names, and the meaning of ``all`` and ``every``, from this one table.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Sequence, Tuple, Union

from benchrun.errors import PlanError
from benchrun.files import atomic_write_text
from benchrun.reports.certificate_report import render_certificate
from benchrun.reports.csv_report import render_csv
from benchrun.reports.html_report import render_html
from benchrun.reports.json_report import render_json
from benchrun.reports.junit_report import render_junit
from benchrun.reports.markdown_report import render_markdown


@dataclass(frozen=True)
class ReportFormat:
    name: str
    ending: str  # Added to the file name stem, such as ".html" or "-certificate.html".
    description: str
    render: Callable[[Dict[str, Any]], str]


REGISTRY: Dict[str, ReportFormat] = {}


def register(report_format: ReportFormat) -> None:
    if report_format.name in REGISTRY or report_format.name in GROUPS:
        raise ValueError(f"report format {report_format.name!r} is already registered")
    if any(known.ending == report_format.ending for known in REGISTRY.values()):
        raise ValueError(f"report file ending {report_format.ending!r} is already taken")
    REGISTRY[report_format.name] = report_format


# What ``all`` meant before there were more formats, so existing scripts get the same files.
CLASSIC_FORMATS = ("html", "csv", "junit")
GROUPS = {"all": "html, csv and junit", "every": "every format"}

register(ReportFormat("html", ".html", "full report for a browser, with plots of sweeps", render_html))
register(ReportFormat("csv", ".csv", "one row per measurement, for a spreadsheet or a script", render_csv))
register(ReportFormat("junit", ".xml", "JUnit XML for a build server or test system", render_junit))
register(ReportFormat("json", ".json", "the whole run record as JSON: format benchrun-run, version 1", render_json))
register(ReportFormat("markdown", ".md", "a summary to paste into a ticket or a pull request", render_markdown))
register(
    ReportFormat(
        "certificate", "-certificate.html", "printable one-page test certificate with reviewer sign-off lines", render_certificate
    )
)

FORMATS: Tuple[str, ...] = tuple(REGISTRY)
CHOICES: Tuple[str, ...] = FORMATS + tuple(GROUPS)


def resolve_formats(names: Iterable[str]) -> Tuple[str, ...]:
    """Format names in the order given, with ``all`` and ``every`` expanded and repeats dropped."""
    chosen: List[str] = []
    for name in names:
        if name == "all":
            expanded: Sequence[str] = CLASSIC_FORMATS
        elif name == "every":
            expanded = tuple(REGISTRY)
        elif name in REGISTRY:
            expanded = (name,)
        else:
            raise PlanError([f"unknown report format {name!r}; choose from {', '.join(CHOICES)}"])
        chosen.extend(item for item in expanded if item not in chosen)
    return tuple(chosen)


def describe_formats() -> str:
    """The format list for command help."""
    width = max(len(name) for name in CHOICES)
    lines = [f"  {f.name.ljust(width)}  {f.description} (*{f.ending})" for f in REGISTRY.values()]
    lines += [f"  {name.ljust(width)}  {meaning}" for name, meaning in GROUPS.items()]
    return "report formats:\n" + "\n".join(lines)


def report_stem(detail: Dict[str, Any]) -> str:
    """A file name base that is safe for any serial number.

    The start time keeps run 1 of one database from overwriting the report of
    run 1 of another, while writing a report again for the same run stays harmless.
    """
    serial = re.sub(r"[^A-Za-z0-9._-]+", "_", detail["serial"]).strip("._")[:64].strip("._") or "unit"
    started = re.sub(r"[^0-9T]", "", (detail.get("started_at") or "")[:19])
    return f"run-{detail['id']:06d}-{serial}" + (f"-{started}" if started else "")


def write_reports(detail: Dict[str, Any], out_dir: Union[str, Path], formats: Sequence[str] = CLASSIC_FORMATS) -> List[Path]:
    """Write the requested formats into ``out_dir``, creating it if needed. Each file appears whole or not at all."""
    folder = Path(out_dir)
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    for name in resolve_formats(formats):
        report_format = REGISTRY[name]
        path = folder / f"{report_stem(detail)}{report_format.ending}"
        atomic_write_text(path, report_format.render(detail))
        written.append(path)
    return written
