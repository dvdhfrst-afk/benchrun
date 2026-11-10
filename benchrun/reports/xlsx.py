"""An Excel workbook of many runs, written with nothing but ``zipfile`` and generated XML.

The workbook is Office Open XML (.xlsx) with three sheets: Runs, Steps and
Measurements. Each sheet's rows go to a temporary file as the runs are read,
one run at a time, and the files are then copied into the archive, so a large
export never holds every measurement in memory.

Text is stored as inline strings, which a spreadsheet shows and never runs.
Text that looks like a formula also gets the "quote prefix" style, so editing
the cell later keeps it text. Numbers are stored as numbers and times as ISO
8601 text. Characters XML cannot hold are removed.
"""

from __future__ import annotations

import shutil
import tempfile
import zipfile
from typing import IO, Any, Dict, Iterable, List, Optional, Sequence, Tuple
from xml.sax.saxutils import escape

from benchrun.errors import PlanError
from benchrun.reports.junit_report import ILLEGAL_XML
from benchrun.reports.summary import finite, is_partial, measurement_counts, point_text, step_counts

MAX_ROWS = 1_048_576  # The most rows a worksheet can have, the header row included.
MAX_CELL_CHARS = 32_767  # The most characters a cell can hold.
_FIXED_TIME = (1980, 1, 1, 0, 0, 0)  # The same input gives the same file.

_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'

_ENTITIES = {"\r": "&#13;"}  # A bare carriage return would be read back as a line feed.

STYLE_HEADER = 1
STYLE_QUOTED = 2

RUN_COLUMNS: Tuple[Tuple[str, float], ...] = (
    ("run_id", 8), ("uuid", 34), ("serial", 18), ("product", 14), ("station", 12), ("plan", 22),
    ("plan_version", 12), ("plan_sha256", 20), ("status", 10), ("partial", 8), ("selected_steps", 20),
    ("reason", 40), ("operator", 16), ("note", 30), ("started_at", 30), ("finished_at", 30),
    ("measurements", 13), ("passed", 8), ("failed", 8), ("errors", 8), ("software_version", 16),
)  # fmt: skip
STEP_COLUMNS: Tuple[Tuple[str, float], ...] = (
    ("run_id", 8), ("serial", 18), ("ordinal", 8), ("step", 24), ("kind", 10), ("status", 10),
    ("message", 50), ("started_at", 30), ("finished_at", 30), ("measurements", 13), ("failed", 8),
)  # fmt: skip
MEASUREMENT_COLUMNS: Tuple[Tuple[str, float], ...] = (
    ("run_id", 8), ("serial", 18), ("step", 24), ("measurement", 22), ("kind", 10), ("point", 18),
    ("value", 14), ("units", 8), ("low", 12), ("high", 12), ("verdict", 9), ("note", 36),
    ("instrument", 14), ("samples", 8), ("taken_at", 30),
)  # fmt: skip


def column_letters(index: int) -> str:
    """0 is A, 25 is Z, 26 is AA."""
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def clean_text(value: Any) -> str:
    return ILLEGAL_XML.sub("", str(value))[:MAX_CELL_CHARS]


def cell_xml(ref: str, value: Any, style: int = 0) -> str:
    """One cell. None leaves the cell out, as an empty cell is stored in a workbook."""
    if value is None:
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = finite(value)
        if number is None:
            return ""
        shown = str(number) if isinstance(number, int) else repr(float(number))
        styled = f' s="{style}"' if style else ""
        return f'<c r="{ref}"{styled}><v>{shown}</v></c>'
    text = clean_text(value)
    if not style and text[:1] in ("=", "+", "-", "@"):
        style = STYLE_QUOTED
    styled = f' s="{style}"' if style else ""
    return f'<c r="{ref}" t="inlineStr"{styled}><is><t xml:space="preserve">{escape(text, _ENTITIES)}</t></is></c>'


class Sheet:
    """One worksheet, built in a temporary file a row at a time."""

    def __init__(self, name: str, columns: Sequence[Tuple[str, float]]):
        self.name = name
        self.letters = [column_letters(index) for index in range(len(columns))]
        self.rows = 0
        self.file: IO[bytes] = tempfile.TemporaryFile()
        widths = "".join(
            f'<col min="{n}" max="{n}" width="{width}" customWidth="1"/>' for n, (_, width) in enumerate(columns, 1)
        )
        self.file.write(
            (
                f'{_HEAD}<worksheet xmlns="{_MAIN}" xmlns:r="{_REL}"><sheetViews><sheetView workbookViewId="0">'
                '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
                '<selection pane="bottomLeft" activeCell="A2" sqref="A2"/></sheetView></sheetViews>'
                f'<sheetFormatPr defaultRowHeight="15"/><cols>{widths}</cols><sheetData>'
            ).encode("utf-8")
        )
        self.add([header for header, _ in columns], STYLE_HEADER)

    def add(self, values: Sequence[Any], style: int = 0) -> None:
        if self.rows >= MAX_ROWS:
            raise PlanError(
                [
                    f"the {self.name} sheet would need more than {MAX_ROWS:,} rows, the most a worksheet can hold",
                    "narrow the export with --serial, --plan, --product, --lot, --station, --status, --since or --until, or use --format csv or jsonl",
                ]
            )
        self.rows += 1
        cells = "".join(cell_xml(f"{letter}{self.rows}", value, style) for letter, value in zip(self.letters, values))
        self.file.write(f'<row r="{self.rows}">{cells}</row>'.encode("utf-8"))

    def finish(self) -> IO[bytes]:
        self.file.write(b"</sheetData></worksheet>")
        self.file.seek(0)
        return self.file


def _run_row(detail: Dict[str, Any]) -> List[Any]:
    counts = measurement_counts(detail)
    selected = detail.get("selected_steps")
    return [
        detail["id"], detail.get("uuid"), detail["serial"], detail.get("product") or None,
        detail.get("station") or None, detail["plan_name"], detail["plan_version"], detail["plan_hash"],
        detail["status"], "yes" if is_partial(detail) else "no", ", ".join(selected) if selected else None,
        detail["reason"], detail["operator"] or None, detail.get("note") or None, detail["started_at"],
        detail["finished_at"], counts["measurements"], counts["pass"], counts["fail"], counts["error"],
        detail["software_version"],
    ]  # fmt: skip


def _part(archive: zipfile.ZipFile, name: str, text: str) -> None:
    archive.writestr(zipfile.ZipInfo(name, _FIXED_TIME), text.encode("utf-8"), zipfile.ZIP_DEFLATED)


def _package(archive: zipfile.ZipFile, sheets: Sequence[Sheet]) -> None:
    sheet_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
    overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{n}.xml" ContentType="{sheet_type}"/>' for n in range(1, len(sheets) + 1)
    )
    _part(
        archive,
        "[Content_Types].xml",
        f'{_HEAD}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        f"{overrides}</Types>",
    )
    _part(
        archive,
        "_rels/.rels",
        f'{_HEAD}<Relationships xmlns="{_PACKAGE_REL}"><Relationship Id="rId1" '
        f'Type="{_REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
    )
    entries = "".join(
        f'<sheet name="{escape(sheet.name)}" sheetId="{n}" r:id="rId{n}"/>' for n, sheet in enumerate(sheets, 1)
    )
    _part(archive, "xl/workbook.xml", f'{_HEAD}<workbook xmlns="{_MAIN}" xmlns:r="{_REL}"><sheets>{entries}</sheets></workbook>')
    links = "".join(
        f'<Relationship Id="rId{n}" Type="{_REL}/worksheet" Target="worksheets/sheet{n}.xml"/>'
        for n in range(1, len(sheets) + 1)
    )
    styles_id = len(sheets) + 1
    _part(
        archive,
        "xl/_rels/workbook.xml.rels",
        f'{_HEAD}<Relationships xmlns="{_PACKAGE_REL}">{links}'
        f'<Relationship Id="rId{styles_id}" Type="{_REL}/styles" Target="styles.xml"/></Relationships>',
    )
    plain = 'numFmtId="0" fontId="0" fillId="0" borderId="0"'
    _part(
        archive,
        "xl/styles.xml",
        f'{_HEAD}<styleSheet xmlns="{_MAIN}">'
        '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="2"><fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        f'<cellStyleXfs count="1"><xf {plain}/></cellStyleXfs>'
        f'<cellXfs count="3"><xf {plain} xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
        f'<xf {plain} xfId="0" quotePrefix="1"/></cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>',
    )


def write_xlsx(handle: IO[bytes], details: Iterable[Dict[str, Any]]) -> None:
    """Write a workbook of every run given to a seekable binary ``handle``."""
    runs = Sheet("Runs", RUN_COLUMNS)
    steps = Sheet("Steps", STEP_COLUMNS)
    measurements = Sheet("Measurements", MEASUREMENT_COLUMNS)
    sheets = (runs, steps, measurements)
    try:
        for detail in details:
            runs.add(_run_row(detail))
            for step in detail["steps"]:
                taken, bad = step_counts(step)
                steps.add(
                    [
                        detail["id"], detail["serial"], step["ordinal"], step["name"], step["kind"], step["status"],
                        step["message"], step["started_at"], step["finished_at"], taken, bad,
                    ]
                )  # fmt: skip
                for m in step["measurements"]:
                    measurements.add(_measurement_row(detail, step, m))
        with zipfile.ZipFile(handle, "w", zipfile.ZIP_DEFLATED) as archive:
            _package(archive, sheets)
            for n, sheet in enumerate(sheets, 1):
                source = sheet.finish()
                size = source.seek(0, 2)
                source.seek(0)
                info = zipfile.ZipInfo(f"xl/worksheets/sheet{n}.xml", _FIXED_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                with archive.open(info, "w", force_zip64=size > 0x7FFF0000) as target:
                    shutil.copyfileobj(source, target, 1 << 20)
    finally:
        for sheet in sheets:
            sheet.file.close()


def _measurement_row(detail: Dict[str, Any], step: Dict[str, Any], m: Dict[str, Any]) -> List[Optional[Any]]:
    return [
        detail["id"], detail["serial"], step["name"], m["name"], m["kind"], point_text(m["point"]) or None,
        m["value"], m["units"] or None, m["low"], m["high"], m["verdict"], m["note"], m["instrument"],
        m["samples"], m["taken_at"],
    ]  # fmt: skip
