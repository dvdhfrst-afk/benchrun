"""Report writers. Each takes the dictionary from ``Store.run_detail``."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Sequence, Union

from benchrun.reports.csv_report import render_csv
from benchrun.reports.html_report import render_html
from benchrun.reports.junit_report import render_junit

FORMATS = ("html", "csv", "junit")
_EXTENSIONS = {"html": "html", "csv": "csv", "junit": "xml"}
_RENDERERS = {"html": render_html, "csv": render_csv, "junit": render_junit}


def report_stem(detail: Dict[str, Any]) -> str:
    """A file name base that is safe for any serial number."""
    serial = re.sub(r"[^A-Za-z0-9._-]+", "_", detail["serial"]).strip("._") or "unit"
    return f"run-{detail['id']:06d}-{serial}"


def write_reports(detail: Dict[str, Any], out_dir: Union[str, Path], formats: Sequence[str] = FORMATS) -> List[Path]:
    """Write the requested formats into ``out_dir``, creating it if needed."""
    folder = Path(out_dir)
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    for name in formats:
        path = folder / f"{report_stem(detail)}.{_EXTENSIONS[name]}"
        # newline="" keeps the writer's own line endings on every platform.
        with path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(_RENDERERS[name](detail))
        written.append(path)
    return written
