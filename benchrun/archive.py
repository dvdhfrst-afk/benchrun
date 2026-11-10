"""Run archives: moving recorded runs from one results database to another.

An archive is a zip file holding ``manifest.json`` and one ``runs/<uuid>.json``
per run. The manifest gives the SHA-256 and size of every member, so a copy
damaged on the way is found before anything is imported. Every archive is read
as if it were hostile: member names, counts and sizes are limited, and sizes
are counted while reading, because a zip header can say anything. Nothing is
ever extracted to disk.

``archive import`` also reads the JSON that ``show --json`` writes, one run per
file, including files from benchrun 1.1.0, which gave runs no uuid. Such a run
gets a uuid derived from its content, so the same file imported twice is
skipped the second time.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import uuid
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple, Union

from benchrun import __version__, audit
from benchrun.console import (
    DEFAULT_DB,
    EXIT_FAIL,
    EXIT_PASS,
    EXIT_USAGE,
    MAX_TEXT_CHARS,
    complain,
    count,
    open_store,
    say,
    table,
    utc_time,
)
from benchrun.errors import PlanError
from benchrun.plan import MAX_PLAN_BYTES, _no_repeats
from benchrun.store import Store, now_iso

FORMAT = "benchrun-archive"
FORMAT_VERSION = 1
MANIFEST = "manifest.json"
MAX_MEMBERS = 100001  # 100,000 runs and the manifest.
MAX_MEMBER_BYTES = 64 * 1024 * 1024  # One run, or the manifest, or one show --json file.
MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024  # Everything read from one archive in one pass.
RUN_STATUSES = ("pass", "fail", "aborted", "running")
STEP_STATUSES = ("pending", "running", "pass", "fail", "error", "skipped")
VERDICTS = ("pass", "fail", "info", "error")

_CHUNK = 64 * 1024
_RUN_MEMBER = re.compile(r"runs/([0-9a-f]{32})\.json")
_UUID = re.compile(r"[0-9a-f]{32}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{3}|\.\d{6})?[+-]\d{2}:\d{2}")
_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06")
_FILE_NAMESPACE = uuid.UUID("5d1f0a8e-3c4b-4e7a-9b61-2f8d7c9e0a14")  # Fixed forever: derived uuids depend on it.
_SHORT = 1000
_LONG = 100000
_MAX_WHOLE = 2**53

# Kinds of field in a record.
TEXT, MAYBE_TEXT, TIME, MAYBE_TIME, WHOLE, MAYBE_WHOLE, REAL, ONE_OF, NUMBERS, NAMES, PATTERN = range(11)
Spec = Tuple[Tuple[str, int, Any], ...]

_RUN_FIELDS: Spec = (
    ("serial", TEXT, _SHORT),
    ("plan_name", TEXT, _SHORT),
    ("plan_version", TEXT, _SHORT),
    ("plan_hash", TEXT, _SHORT),
    ("plan_source", TEXT, MAX_PLAN_BYTES),
    ("variables", NUMBERS, None),
    ("operator", TEXT, MAX_TEXT_CHARS),
    ("software_version", TEXT, _SHORT),
    ("note", TEXT, MAX_TEXT_CHARS),
    ("selected_steps", NAMES, None),
    ("station", TEXT, _SHORT),
    ("product", TEXT, _SHORT),
    ("status", ONE_OF, RUN_STATUSES),
    ("reason", MAYBE_TEXT, _LONG),
    ("started_at", TIME, None),
    ("finished_at", MAYBE_TIME, None),
    ("origin", TEXT, _SHORT),
    ("imported_at", MAYBE_TIME, None),
)
_RECORD_FIELDS: Spec = (("uuid", PATTERN, _UUID),) + _RUN_FIELDS
_LOT_FIELDS: Spec = (
    ("name", TEXT, _SHORT),
    ("product", TEXT, _SHORT),
    ("plan_name", TEXT, _SHORT),
    ("quantity", MAYBE_WHOLE, None),
    ("status", TEXT, _SHORT),
    ("note", TEXT, _LONG),
    ("created_at", TIME, None),
    ("closed_at", MAYBE_TIME, None),
)
_INSTRUMENT_FIELDS: Spec = (
    ("name", TEXT, _SHORT),
    ("driver", TEXT, _SHORT),
    ("address", TEXT, _SHORT),
    ("idn", MAYBE_TEXT, _LONG),
    ("asset_tag", MAYBE_TEXT, _SHORT),
)
_STEP_FIELDS: Spec = (
    ("ordinal", WHOLE, None),
    ("name", TEXT, _SHORT),
    ("kind", TEXT, _SHORT),
    ("status", ONE_OF, STEP_STATUSES),
    ("message", MAYBE_TEXT, _LONG),
    ("started_at", MAYBE_TIME, None),
    ("finished_at", MAYBE_TIME, None),
)
_MEASUREMENT_FIELDS: Spec = (
    ("name", TEXT, _SHORT),
    ("kind", TEXT, _SHORT),
    ("point", NUMBERS, None),
    ("instrument", MAYBE_TEXT, _SHORT),
    ("samples", WHOLE, None),
    ("value", REAL, None),
    ("units", TEXT, _SHORT),
    ("low", REAL, None),
    ("high", REAL, None),
    ("verdict", ONE_OF, VERDICTS),
    ("note", MAYBE_TEXT, _LONG),
    ("taken_at", TIME, None),
)
_MANIFEST_FIELDS: Spec = (
    ("format", ONE_OF, (FORMAT,)),
    ("format_version", WHOLE, None),
    ("created_at", TIME, None),
    ("benchrun_version", TEXT, _SHORT),
    ("database_id", TEXT, _SHORT),
    ("run_count", WHOLE, None),
)
_MEMBER_FIELDS: Spec = (("name", TEXT, _SHORT), ("sha256", PATTERN, _SHA256), ("size", WHOLE, None))
# Run columns a show --json file from benchrun 1.1.0 does not have, and what they held there.
# Its instruments also had no asset_tag, which is null there.
_FILE_DEFAULTS = {"station": "", "product": "", "origin": "", "imported_at": None}


# Checking records ------------------------------------------------------


def _shown(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= 40 else text[:37] + "..."


def _is_time(text: str) -> bool:
    """The form the database stores, checked the same way on every Python version."""
    if not _TIME.fullmatch(text):
        return False
    try:
        datetime.fromisoformat(text)
    except ValueError:
        return False
    return True


class _Checker:
    """Collects every problem in one decoded JSON document, each named by where it is."""

    def __init__(self, where: str):
        self.where = where
        self.problems: List[str] = []

    def problem(self, path: str, text: str) -> None:
        self.problems.append(f"{self.where}: {path} {text}" if path else f"{self.where}: {text}")

    def fields(self, data: Any, spec: Spec, path: str, nested: Sequence[str] = ()) -> Dict[str, Any]:
        """The fields of one object, normalised. Unknown and missing fields are problems."""
        if not isinstance(data, dict):
            self.problem(path, "must be an object")
            return {}
        known = {name for name, _, _ in spec} | set(nested)
        for key in data:
            if key not in known:
                self.problem(path, f"has an unexpected field {_shown(key)}")
        result = {}
        for name, kind, rule in spec:
            where = f"{path}.{name}" if path else name
            if name not in data:
                self.problem(where, "is missing")
            else:
                result[name] = self.value(data[name], kind, rule, where)
        return result

    def items(self, data: Any, key: str, path: str) -> List[Any]:
        where = f"{path}.{key}" if path else key
        if key not in data:
            self.problem(where, "is missing")
            return []
        if not isinstance(data[key], list):
            self.problem(where, "must be a list")
            return []
        return data[key]

    def value(self, value: Any, kind: int, rule: Any, where: str) -> Any:
        if value is None and kind in (MAYBE_TEXT, MAYBE_TIME, MAYBE_WHOLE, REAL, NAMES):
            return None
        if kind in (TEXT, MAYBE_TEXT):
            return self.text(value, rule, where)
        if kind in (TIME, MAYBE_TIME):
            if not isinstance(value, str) or len(value) > 64 or not _is_time(value):
                self.problem(where, f"must be a time with a UTC offset, like 2026-01-31T12:00:00.000+00:00, got {_shown(value)}")
            return value
        if kind in (WHOLE, MAYBE_WHOLE):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_WHOLE:
                self.problem(where, f"must be a whole number from 0 to {_MAX_WHOLE}, got {_shown(value)}")
            return value
        if kind == REAL:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                self.problem(where, f"must be a number or null, got {_shown(value)}")
                return value
            try:
                return float(value)
            except OverflowError:
                self.problem(where, "is too large a number to store")
                return value
        if kind == ONE_OF:
            if not isinstance(value, str) or value not in rule:
                self.problem(where, f"must be one of {', '.join(rule)}, got {_shown(value)}")
            return value
        if kind == PATTERN:
            if not isinstance(value, str) or not rule.fullmatch(value):
                self.problem(where, f"must be lowercase hexadecimal of the right length, got {_shown(value)}")
            return value
        if kind == NUMBERS:
            return self.numbers(value, where)
        if not isinstance(value, list):  # NAMES
            self.problem(where, "must be a list of names or null")
            return value
        return [self.text(item, _SHORT, f"{where}[{index}]") for index, item in enumerate(value)]

    def text(self, value: Any, limit: int, where: str) -> Any:
        if not isinstance(value, str):
            self.problem(where, f"must be text, got {_shown(value)}")
        elif len(value) > limit:
            self.problem(where, f"is {len(value)} characters long; the most allowed is {limit}")
        else:
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                self.problem(where, "holds a broken character, written as a lone \\ud... escape")
        return value

    def numbers(self, value: Any, where: str) -> Any:
        if not isinstance(value, dict):
            self.problem(where, "must be an object of names and numbers")
            return value
        for key, number in value.items():
            self.text(key, _SHORT, f"{where} name")
            finite = isinstance(number, (int, float)) and not isinstance(number, bool) and abs(number) < float("inf")
            if not finite:
                self.problem(f"{where}.{key}", f"must be a finite number, got {_shown(number)}")
        return value


def check_record(data: Any, where: str) -> Tuple[Dict[str, Any], List[str]]:
    """A run record in the current form, normalised, and every problem with it."""
    checker = _Checker(where)
    nested = ("lot", "instruments", "steps")
    record = checker.fields(data, _RECORD_FIELDS, "", nested)
    if not isinstance(data, dict):
        return record, checker.problems
    if "lot" not in data:
        checker.problem("lot", "is missing")
    record["lot"] = None if data.get("lot") is None else checker.fields(data["lot"], _LOT_FIELDS, "lot")
    record["instruments"] = [
        checker.fields(item, _INSTRUMENT_FIELDS, f"instruments[{index}]")
        for index, item in enumerate(checker.items(data, "instruments", ""))
    ]
    steps = []
    ordinals = set()
    for index, item in enumerate(checker.items(data, "steps", "")):
        path = f"steps[{index}]"
        step = checker.fields(item, _STEP_FIELDS, path, ("measurements",))
        if step.get("ordinal") in ordinals:
            checker.problem(f"{path}.ordinal", f"{step['ordinal']} is used by another step of the run")
        ordinals.add(step.get("ordinal"))
        if isinstance(item, dict):
            step["measurements"] = [
                checker.fields(m, _MEASUREMENT_FIELDS, f"{path}.measurements[{n}]")
                for n, m in enumerate(checker.items(item, "measurements", path))
            ]
        steps.append(step)
    record["steps"] = steps
    return record, checker.problems


def content_text(record: Dict[str, Any], with_uuid: bool = True) -> str:
    """What makes two copies of a run the same run. Where and when it was imported does not."""
    body = {key: value for key, value in record.items() if key not in ("origin", "imported_at")}
    if not with_uuid:
        body.pop("uuid", None)
    body["lot"] = record["lot"]["name"] if record.get("lot") else None
    return json.dumps(body, sort_keys=True, ensure_ascii=False)


def _pick(item: Any, spec: Spec) -> Any:
    return {name: item[name] for name, _, _ in spec if name in item} if isinstance(item, dict) else item


def shape_record(detail: Dict[str, Any], lot: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """A record from what ``Store.run_detail`` returns, or ``show --json`` wrote: no row ids, which mean nothing elsewhere."""
    record = _pick(detail, _RECORD_FIELDS)
    record["lot"] = lot
    instruments = detail.get("instruments")
    record["instruments"] = [_pick(i, _INSTRUMENT_FIELDS) for i in instruments] if isinstance(instruments, list) else instruments
    steps = detail.get("steps")
    if isinstance(steps, list):
        shaped = []
        for step in steps:
            item = _pick(step, _STEP_FIELDS)
            if isinstance(step, dict) and "measurements" in step:
                measurements = step["measurements"]
                item["measurements"] = (
                    [_pick(m, _MEASUREMENT_FIELDS) for m in measurements] if isinstance(measurements, list) else measurements
                )
            shaped.append(item)
        steps = shaped
    record["steps"] = steps
    return record


def record_from_store(store: Store, run_id: int) -> Optional[Dict[str, Any]]:
    detail = store.run_detail(run_id)
    if detail is None:
        return None
    lot = None
    if detail.get("lot_id") is not None:
        names = ", ".join(name for name, _, _ in _LOT_FIELDS)
        row = store.conn.execute(f"SELECT {names} FROM lots WHERE id = ?", (detail["lot_id"],)).fetchone()
        lot = None if row is None else dict(row)
    return shape_record(detail, lot)


def database_id(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = 'database_id'").fetchone()
    return "" if row is None else str(row[0])


# Reading ---------------------------------------------------------------


def _constant(word: str) -> float:
    if word == "NaN":
        raise ValueError("NaN is not a value a run can hold")
    return float(word)  # A reading can be infinite, and SQLite stores that.


def _decode(data: bytes, where: str, any_encoding: bool = False) -> Tuple[Any, Optional[str]]:
    """Strict JSON from bytes: no repeated keys, no NaN. ``any_encoding`` also takes UTF-16 with a byte order mark."""
    encoding = "utf-16" if any_encoding and data[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"
    try:
        text = data.decode(encoding)
    except UnicodeDecodeError as exc:
        return None, f"{where}: is not UTF-8 text ({exc.reason} at byte {exc.start})"
    try:
        return json.loads(text, object_pairs_hook=_no_repeats, parse_constant=_constant), None
    except ValueError as exc:
        return None, f"{where}: is not valid JSON: {exc}"
    except RecursionError:
        return None, f"{where}: is too deeply nested"


def _unsafe_name(name: str) -> Optional[str]:
    parts = re.split(r"[\\/]", name)
    if name.startswith(("/", "\\")) or re.match(r"[A-Za-z]:", name):
        return "is an absolute path"
    if ".." in parts:
        return "climbs out of the archive with .."
    if "\\" in name or any(ord(ch) < 32 for ch in name):
        return "has a backslash or a control character in its name"
    return None


class _TooBig(Exception):
    pass


class Archive:
    """A run archive opened for reading. ``check`` first; ``records`` only after it found nothing wrong."""

    kind = "archive"

    def __init__(self, path: Union[str, Path]):
        self.path = Path(path)
        self.label = self.path.name
        self.manifest: Dict[str, Any] = {}
        self._listed: Dict[str, Dict[str, Any]] = {}
        self._runs: Dict[str, zipfile.ZipInfo] = {}
        self._infos: List[zipfile.ZipInfo] = []
        try:
            self._zip = zipfile.ZipFile(self.path)
        except (zipfile.BadZipFile, ValueError, EOFError, NotImplementedError, OverflowError) as exc:
            raise PlanError([f"{self.path} is not a zip file benchrun can read: {exc}"]) from exc

    def close(self) -> None:
        self._zip.close()

    @property
    def origin(self) -> str:
        return str(self.manifest.get("database_id", ""))

    def describe(self) -> str:
        made = self.manifest.get("created_at", "?")
        return (
            f"benchrun archive (format {self.manifest.get('format_version', '?')}) made {made} by benchrun "
            f"{self.manifest.get('benchrun_version', '?')} from database {self.origin or '(unknown)'}"
        )

    def check(self, visit: Optional[Callable[[Dict[str, Any]], None]] = None) -> List[str]:
        """Every problem with the archive. ``visit`` sees each run record that is sound."""
        problems, fatal = self._check_members()
        if fatal:
            return problems
        manifest_problems, newer = self._check_manifest()
        if newer:
            return manifest_problems
        problems += manifest_problems
        budget = [MAX_TOTAL_BYTES]
        for name in self._order():
            record, found = self._load(name, budget)
            problems += found
            if record is not None and visit is not None:
                visit(record)
            if budget[0] < 0:
                break
        return problems

    def records(self) -> Iterator[Dict[str, Any]]:
        budget = [MAX_TOTAL_BYTES]
        for name in self._order():
            record, problems = self._load(name, budget)
            if problems or record is None:
                raise PlanError(problems or [f"{self.label}: {name} could not be read"])
            yield record

    def _order(self) -> List[str]:
        """The runs in the order the manifest lists them, which export makes oldest first, so imported ids follow it."""
        listed = [name for name in self._listed if name in self._runs]
        return listed + sorted(set(self._runs) - set(listed))

    def _check_members(self) -> Tuple[List[str], bool]:
        """Problems with the members' names and headers, and whether they rule out reading any of it."""
        self._infos = self._zip.infolist()
        if len(self._infos) > MAX_MEMBERS:
            return [f"{self.label}: holds {len(self._infos)} members; an archive may hold at most {MAX_MEMBERS}"], True
        problems = []
        seen = set()
        declared = 0
        for info in self._infos:
            name = info.filename
            declared += info.file_size
            if name in seen:
                problems.append(f"{self.label}: the member {_shown(name)} appears more than once")
                self._runs.pop(name, None)
                continue
            seen.add(name)
            unsafe = _unsafe_name(name)
            if unsafe:
                problems.append(f"{self.label}: the member {_shown(name)} {unsafe}")
                continue
            if name == "runs/" and info.is_dir():
                continue
            if name != MANIFEST and not _RUN_MEMBER.fullmatch(name):
                problems.append(f"{self.label}: unexpected member {_shown(name)}; only {MANIFEST} and runs/<uuid>.json belong")
                continue
            if info.flag_bits & 0x1:
                problems.append(f"{self.label}: {name} is encrypted")
            elif info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                problems.append(f"{self.label}: {name} uses a compression method benchrun does not read")
            elif info.file_size > MAX_MEMBER_BYTES:
                problems.append(f"{self.label}: {name} says it is {info.file_size} bytes; the most allowed is {MAX_MEMBER_BYTES}")
            elif name != MANIFEST:
                self._runs[name] = info
        if declared > MAX_TOTAL_BYTES:
            problems.append(f"{self.label}: members say they add up to {declared} bytes; the most allowed is {MAX_TOTAL_BYTES}")
            return problems, True
        return problems, False

    def _read(self, info: zipfile.ZipInfo, budget: List[int]) -> bytes:
        """The member's bytes, counted as they come rather than taken from its header."""
        chunks = []
        size = 0
        with self._zip.open(info) as handle:
            while True:
                chunk = handle.read(_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                budget[0] -= len(chunk)
                if size > MAX_MEMBER_BYTES or budget[0] < 0:
                    raise _TooBig()
                chunks.append(chunk)
        return b"".join(chunks)

    def _member_bytes(self, info: zipfile.ZipInfo, budget: List[int]) -> Tuple[Optional[bytes], Optional[str]]:
        try:
            return self._read(info, budget), None
        except _TooBig:
            if budget[0] < 0:
                return None, f"{self.label}: reading {info.filename} passed {MAX_TOTAL_BYTES} bytes for the archive; stopped"
            return None, f"{self.label}: {info.filename} unpacks to more than {MAX_MEMBER_BYTES} bytes"
        except (zipfile.BadZipFile, EOFError, OSError, ValueError, NotImplementedError) as exc:
            return None, f"{self.label}: {info.filename} is damaged: {exc}"
        except Exception as exc:  # zlib.error and the like from a crafted stream.
            return None, f"{self.label}: {info.filename} is damaged: {type(exc).__name__}: {exc}"

    def _check_manifest(self) -> Tuple[List[str], bool]:
        infos = [info for info in self._infos if info.filename == MANIFEST]
        if len(infos) != 1 or infos[0].file_size > MAX_MEMBER_BYTES:
            return ([f"{self.label}: has no usable {MANIFEST}; it is not a benchrun archive"] if not infos else []), False
        data, problem = self._member_bytes(infos[0], [MAX_TOTAL_BYTES])
        if problem:
            return [problem], False
        manifest, problem = _decode(data, f"{self.label}: {MANIFEST}")
        if problem:
            return [problem], False
        version = manifest.get("format_version") if isinstance(manifest, dict) else None
        if isinstance(manifest, dict) and manifest.get("format") == FORMAT and isinstance(version, int) and not isinstance(version, bool):
            if version > FORMAT_VERSION:
                return [
                    f"{self.label}: is archive format version {version}, newer than the {FORMAT_VERSION} this benchrun "
                    "reads. Upgrade benchrun to read it; nothing was imported."
                ], True
        checker = _Checker(f"{self.label}: {MANIFEST}")
        self.manifest = checker.fields(manifest, _MANIFEST_FIELDS, "", ("members",))
        if self.manifest.get("format_version") not in (None, FORMAT_VERSION):
            checker.problem("format_version", f"must be {FORMAT_VERSION}")
        members = checker.items(manifest, "members", "") if isinstance(manifest, dict) else []
        for index, item in enumerate(members):
            entry = checker.fields(item, _MEMBER_FIELDS, f"members[{index}]")
            name = entry.get("name")
            if not isinstance(name, str):
                continue
            if name in self._listed:
                checker.problem(f"members[{index}]", f"lists {_shown(name)} twice")
            elif not _RUN_MEMBER.fullmatch(name):
                checker.problem(f"members[{index}]", f"names {_shown(name)}, which is not runs/<uuid>.json")
            else:
                self._listed[name] = entry
        problems = checker.problems
        present = {info.filename for info in self._infos}
        for name in sorted(set(self._listed) - present):
            problems.append(f"{self.label}: {name} is listed in the manifest but is not in the archive")
        for name in sorted(n for n in present if _RUN_MEMBER.fullmatch(n) and n not in self._listed):
            problems.append(f"{self.label}: {name} is in the archive but not listed in the manifest")
            self._runs.pop(name, None)
        run_count = self.manifest.get("run_count")
        if isinstance(run_count, int) and run_count != len(self._listed):
            problems.append(f"{self.label}: the manifest says {count(run_count, 'run')} but lists {len(self._listed)}")
        return problems, False

    def _load(self, name: str, budget: List[int]) -> Tuple[Optional[Dict[str, Any]], List[str]]:
        data, problem = self._member_bytes(self._runs[name], budget)
        if problem:
            return None, [problem]
        listed = self._listed.get(name)
        where = f"{self.label}: {name}"
        if listed is None:
            return None, [f"{where} is not listed in the manifest"]
        if len(data) != listed.get("size"):
            return None, [f"{where} is {len(data)} bytes; the manifest says {listed.get('size')}"]
        if hashlib.sha256(data).hexdigest() != listed.get("sha256"):
            return None, [f"{where} does not match its SHA-256 in the manifest; it was changed or damaged"]
        decoded, problem = _decode(data, where)
        if problem:
            return None, [problem]
        record, problems = check_record(decoded, where)
        if not problems and record["uuid"] != _RUN_MEMBER.fullmatch(name).group(1):
            problems.append(f"{where}: holds run {record['uuid']}, not the run its name says")
        return (None if problems else record), problems


class RunFile:
    """One run written by ``show --json``: by benchrun 1.1.0 without a uuid, or by a later version with one."""

    kind = "file"

    def __init__(self, path: Union[str, Path], data: bytes):
        self.path = Path(path)
        self.label = self.path.name
        self.origin = f"file:{self.label}"
        self.derived_uuid = False
        self._record: Optional[Dict[str, Any]] = None
        self._data = data

    def close(self) -> None:
        self._data = b""

    def describe(self) -> str:
        how = "uuid derived from its content" if self.derived_uuid else "uuid recorded in the file"
        return f"one run written by show --json ({how})"

    def check(self, visit: Optional[Callable[[Dict[str, Any]], None]] = None) -> List[str]:
        if len(self._data) > MAX_MEMBER_BYTES:
            return [f"{self.label}: is larger than {MAX_MEMBER_BYTES} bytes, too big to be one run"]
        decoded, problem = _decode(self._data, self.label, any_encoding=True)
        if problem:
            return [problem]
        if not isinstance(decoded, dict):
            return [f"{self.label}: must be one run as a JSON object, as show --json writes it"]
        self.derived_uuid = "uuid" not in decoded
        detail = dict(_FILE_DEFAULTS)
        detail.update(decoded)
        detail.setdefault("uuid", "0" * 32)
        if isinstance(detail.get("instruments"), list):
            detail["instruments"] = [dict({"asset_tag": None}, **i) if isinstance(i, dict) else i for i in detail["instruments"]]
        record, problems = check_record(shape_record(detail, None), self.label)
        if problems:
            return problems
        if self.derived_uuid:
            record["uuid"] = uuid.uuid5(_FILE_NAMESPACE, content_text(record, with_uuid=False)).hex
        self._record = record
        if visit is not None:
            visit(record)
        return []

    def records(self) -> Iterator[Dict[str, Any]]:
        if self._record is None:
            raise PlanError([f"{self.label}: was not checked"])
        yield self._record


Source = Union[Archive, RunFile]


def open_source(path: Union[str, Path]) -> Source:
    """An archive, or a single run file; which one is told by the first bytes."""
    file_path = Path(path)
    if not file_path.is_file():
        raise PlanError([f"no file at {path}"])
    with file_path.open("rb") as handle:
        head = handle.read(4)
        if head in _ZIP_MAGIC:
            return Archive(file_path)
        rest = handle.read(MAX_MEMBER_BYTES + 1 - len(head))
    return RunFile(file_path, head + rest)


# Writing ---------------------------------------------------------------


def write_archive(store: Store, run_ids: Sequence[int], out: Union[str, Path]) -> List[Dict[str, Any]]:
    """Write the runs to a new archive at ``out``, all at once or not at all. Returns their records."""
    target = Path(out)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    members = []
    summaries = []
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            for run_id in run_ids:
                record = record_from_store(store, run_id)
                if record is None:
                    raise PlanError([f"no run with id {run_id}"])
                _, problems = check_record(record, f"run {run_id}")
                if problems:
                    raise PlanError(problems + [f"run {run_id} cannot be archived as it is recorded; db check may say why"])
                data = (json.dumps(record, indent=1, ensure_ascii=False) + "\n").encode("utf-8")
                name = f"runs/{record['uuid']}.json"
                archive.writestr(name, data)
                members.append({"name": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
                summaries.append(_summary(record))
            manifest = {
                "format": FORMAT,
                "format_version": FORMAT_VERSION,
                "created_at": now_iso(),
                "benchrun_version": __version__,
                "database_id": database_id(store.conn),
                "run_count": len(members),
                "members": members,
            }
            archive.writestr(MANIFEST, json.dumps(manifest, indent=1) + "\n")
        with temporary.open("rb+") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass
    return summaries


def _summary(record: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "uuid": record["uuid"],
        "serial": record["serial"],
        "plan": f"{record['plan_name']} {record['plan_version']}".rstrip(),
        "status": record["status"],
        "started_at": record["started_at"],
        "measurements": sum(len(step["measurements"]) for step in record["steps"]),
    }


# Importing -------------------------------------------------------------

IMPORTED, SKIPPED, CONFLICT, UNFINISHED = "imported", "skipped", "conflict", "unfinished"


@dataclass
class Outcome:
    action: str
    uuid: str
    serial: str
    plan: str
    status: str
    started_at: str
    run_id: Optional[int]  # The new run, or the one already here.
    detail: str


def _decide(store: Store, record: Dict[str, Any], include_unfinished: bool, derived_uuid: bool) -> Tuple[str, Optional[int], str]:
    conn = store.conn
    row = conn.execute("SELECT id FROM runs WHERE uuid = ?", (record["uuid"],)).fetchone()
    if row is not None:
        here = record_from_store(store, row[0])
        if here is not None and content_text(here) == content_text(record):
            return SKIPPED, row[0], f"already here as run {row[0]}"
        return CONFLICT, row[0], f"a different run with this uuid is already here as run {row[0]}; nothing was merged or replaced"
    if derived_uuid:
        # The same run may be here under the uuid a schema upgrade gave it.
        same = conn.execute(
            "SELECT id FROM runs WHERE serial = ? AND started_at = ? AND plan_hash = ?",
            (record["serial"], record["started_at"], record["plan_hash"]),
        ).fetchall()
        for (run_id,) in same:
            here = record_from_store(store, run_id)
            if here is not None and content_text(here, with_uuid=False) == content_text(record, with_uuid=False):
                return SKIPPED, run_id, f"already here as run {run_id}, under uuid {here['uuid']}"
    if record["status"] == "running" and not include_unfinished:
        return UNFINISHED, None, "still marked running in the source; add --include-unfinished to import it as running"
    return IMPORTED, None, ""


def _local_lot(conn: sqlite3.Connection, lot: Optional[Dict[str, Any]], label: str, added: List[str]) -> Optional[int]:
    if lot is None:
        return None
    row = conn.execute("SELECT id FROM lots WHERE name = ?", (lot["name"],)).fetchone()
    if row is not None:
        return int(row[0])
    names = [name for name, _, _ in _LOT_FIELDS]
    cursor = conn.execute(
        f"INSERT INTO lots ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})", [lot[name] for name in names]
    )
    audit.record(conn, "lot.create", f"lot:{lot['name']}", {"imported_from": label, **lot})
    added.append(lot["name"])
    return int(cursor.lastrowid)


def _insert(conn: sqlite3.Connection, record: Dict[str, Any], origin: str, imported_at: str, lot_id: Optional[int]) -> int:
    run = {name: record[name] for name, _, _ in _RECORD_FIELDS}
    run["variables"] = json.dumps(run["variables"], sort_keys=True)
    run["selected_steps"] = None if run["selected_steps"] is None else json.dumps(run["selected_steps"])
    run.update(origin=record["origin"] or origin, imported_at=imported_at, lot_id=lot_id)
    cursor = conn.execute(f"INSERT INTO runs ({', '.join(run)}) VALUES ({', '.join('?' for _ in run)})", list(run.values()))
    run_id = int(cursor.lastrowid)
    names = [name for name, _, _ in _INSTRUMENT_FIELDS]
    conn.executemany(
        f"INSERT INTO run_instruments (run_id, {', '.join(names)}) VALUES (?{', ?' * len(names)})",
        [[run_id] + [item[name] for name in names] for item in record["instruments"]],
    )
    step_names = [name for name, _, _ in _STEP_FIELDS]
    measurement_names = [name for name, _, _ in _MEASUREMENT_FIELDS]
    for step in record["steps"]:
        cursor = conn.execute(
            f"INSERT INTO steps (run_id, {', '.join(step_names)}) VALUES (?{', ?' * len(step_names)})",
            [run_id] + [step[name] for name in step_names],
        )
        step_id = int(cursor.lastrowid)
        rows = []
        for m in step["measurements"]:
            values = [json.dumps(m[name]) if name == "point" else m[name] for name in measurement_names]
            rows.append([run_id, step_id] + values)
        conn.executemany(
            f"INSERT INTO measurements (run_id, step_id, {', '.join(measurement_names)})"
            f" VALUES (?, ?{', ?' * len(measurement_names)})",
            rows,
        )
    return run_id


def import_runs(store: Store, source: Source, include_unfinished: bool = False, dry_run: bool = False) -> Tuple[List[Outcome], List[str]]:
    """Import every run from a checked source in one transaction. A dry run does it all and rolls back."""
    conn = store.conn
    outcomes: List[Outcome] = []
    lots_added: List[str] = []
    imported_at = now_iso()
    derived = isinstance(source, RunFile) and source.derived_uuid
    conn.execute("BEGIN IMMEDIATE")
    try:
        for record in source.records():
            action, run_id, detail = _decide(store, record, include_unfinished, derived)
            if action == IMPORTED:
                lot_id = _local_lot(conn, record["lot"], source.label, lots_added)
                run_id = _insert(conn, record, source.origin, imported_at, lot_id)
            summary = _summary(record)
            del summary["measurements"]
            outcomes.append(Outcome(action, run_id=run_id, detail=detail, **summary))
        tally = {action: sum(1 for o in outcomes if o.action == action) for action in (IMPORTED, SKIPPED, CONFLICT, UNFINISHED)}
        refused = [o.uuid for o in outcomes if o.action in (CONFLICT, UNFINISHED)]
        audit.record(
            conn,
            "archive.import",
            f"archive:{source.label}",
            {"file": str(source.path), "kind": source.kind, "origin": source.origin, "refused": refused, **tally},
        )
    except BaseException:
        conn.rollback()
        raise
    if dry_run:
        conn.rollback()
        for outcome in outcomes:
            if outcome.action == IMPORTED:
                outcome.run_id = None
    else:
        conn.commit()
    return outcomes, lots_added


# Commands --------------------------------------------------------------


def _checked(path: str, visit: Optional[Callable[[Dict[str, Any]], None]] = None) -> Tuple[Source, List[str]]:
    source = open_source(path)
    try:
        return source, source.check(visit)
    except BaseException:
        source.close()
        raise


def _run_table(rows: Sequence[Dict[str, Any]]) -> str:
    return table(
        ("uuid", "serial", "plan", "status", "started (UTC)", "measured"),
        [(r["uuid"], r["serial"], r["plan"], r["status"].upper(), r["started_at"][:19].replace("T", " "), r["measurements"]) for r in rows],
    )


def cmd_export(args: argparse.Namespace) -> int:
    if not Path(args.db).exists():
        raise PlanError([f"no results database at {args.db}"])
    if Path(args.out).exists() and not args.force:
        raise PlanError([f"{args.out} already exists; choose another name or add --force to replace it"])
    since = utc_time(args.since, "--since")
    until = utc_time(args.until, "--until", end_of_day=True)
    with open_store(args.db) as store:
        if args.run:
            missing = [str(run_id) for run_id in args.run if store.conn.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone() is None]
            if missing:
                raise PlanError([f"no run with id {', '.join(missing)} in {args.db}"])
        rows = store.find_runs(serial=args.serial, plan=args.plan, status=args.status, since=since, until=until)
        run_ids = sorted(row["id"] for row in rows if not args.run or row["id"] in args.run)
        if not run_ids:
            complain("No matching runs, so nothing was written.")
            return EXIT_USAGE
        summaries = write_archive(store, run_ids, args.out)
    say(f"Wrote {count(len(summaries), 'run')} to {args.out}")
    running = sum(1 for s in summaries if s["status"] == "running")
    if running:
        say(f"Note: {count(running, 'run')} still marked running; archive import refuses those unless given --include-unfinished.")
    return EXIT_PASS


def cmd_list(args: argparse.Namespace) -> int:
    rows: List[Dict[str, Any]] = []
    source, problems = _checked(args.file, lambda record: rows.append(_summary(record)))
    try:
        if args.json:
            info = dict(source.manifest) if isinstance(source, Archive) else {}
            info.pop("members", None)
            say(json.dumps({"file": args.file, "kind": source.kind, "about": source.describe(), **info, "runs": rows, "problems": problems}, indent=2))
        else:
            say(f"{args.file}: {source.describe()}")
            say(f"{count(len(rows), 'run')} readable" + (f", {count(len(problems), 'problem')}" if problems else ""))
            if rows:
                say()
                say(_run_table(rows))
            for problem in problems:
                complain(f"problem: {problem}")
            if problems:
                complain(f"Run 'benchrun archive verify {args.file}' for the whole check.")
    finally:
        source.close()
    return EXIT_FAIL if problems else EXIT_PASS


def cmd_verify(args: argparse.Namespace) -> int:
    runs = [0]
    source, problems = _checked(args.file, lambda record: runs.__setitem__(0, runs[0] + 1))
    source.close()
    if args.json:
        say(json.dumps({"file": args.file, "ok": not problems, "runs": runs[0], "problems": problems}, indent=2))
    elif problems:
        say(f"{args.file}: {count(len(problems), 'problem')}")
        for problem in problems:
            say(f"  {problem}")
    else:
        what = "every member matches its SHA-256 and size, and every run record is sound" if source.kind == "archive" else "the run record is sound"
        say(f"{args.file}: OK, {count(runs[0], 'run')}; {what}")
    return EXIT_FAIL if problems else EXIT_PASS


_SHOWN_ACTION = {
    (IMPORTED, False): "imported",
    (IMPORTED, True): "would import",
    (SKIPPED, False): "skipped",
    (SKIPPED, True): "would skip",
    (CONFLICT, False): "REFUSED",
    (CONFLICT, True): "would refuse",
    (UNFINISHED, False): "REFUSED",
    (UNFINISHED, True): "would refuse",
}


def cmd_import(args: argparse.Namespace) -> int:
    source, problems = _checked(args.file)
    try:
        if problems:
            raise PlanError([f"{args.file} has problems, so nothing was imported:"] + problems)
        # A dry run must not leave a new database behind either.
        target = ":memory:" if args.dry_run and not Path(args.db).exists() else args.db
        with open_store(target) as store:
            outcomes, lots_added = import_runs(store, source, args.include_unfinished, args.dry_run)
    finally:
        source.close()
    tally = {action: sum(1 for o in outcomes if o.action == action) for action in (IMPORTED, SKIPPED, CONFLICT, UNFINISHED)}
    refused = tally[CONFLICT] + tally[UNFINISHED]
    if args.json:
        say(json.dumps({"file": args.file, "db": args.db, "dry_run": args.dry_run, "lots_added": lots_added, **tally, "runs": [asdict(o) for o in outcomes]}, indent=2))
        return EXIT_FAIL if refused else EXIT_PASS
    say(f"{args.file}: {source.describe()}")
    shown = [
        (_SHOWN_ACTION[(o.action, args.dry_run)], o.run_id, o.uuid, o.serial, o.plan, o.status.upper(), o.started_at[:19].replace("T", " "))
        for o in outcomes
    ]
    say(table(("outcome", "run here", "uuid", "serial", "plan", "status", "started (UTC)"), shown))
    for o in outcomes:
        if o.action in (CONFLICT, UNFINISHED):
            complain(f"Refused {o.uuid} (serial {o.serial}): {o.detail}")
    for name in lots_added:
        say(f"{'Would add' if args.dry_run else 'Added'} lot {name}, which the runs name and {args.db} did not have")
    verb = "Would import" if args.dry_run else "Imported"
    say(f"{verb} {count(tally[IMPORTED], 'run')} into {args.db}; {tally[SKIPPED]} already there, {refused} refused.")
    if args.dry_run:
        say("Dry run: nothing was changed.")
    return EXIT_FAIL if refused else EXIT_PASS


def add_commands(subparsers: Any) -> None:
    archive = subparsers.add_parser(
        "archive",
        help="move runs between results databases in a checked zip file",
        description="Move runs between results databases. An archive holds whole runs, with a manifest of SHA-256 hashes.",
        epilog="Exit codes: 0 done, 1 problems found or runs refused, 2 bad arguments or an unusable file, 3 could not complete.",
    )
    commands = archive.add_subparsers(dest="archive_command", required=True, help="what to do with an archive")

    export = commands.add_parser("export", help="write runs to a new archive")
    export.add_argument("--out", required=True, help="the archive (.zip) to write")
    export.add_argument("--force", action="store_true", help="replace the file if it already exists")
    export.add_argument("--db", default=DEFAULT_DB, help=f"results database (default: {DEFAULT_DB})")
    export.add_argument("--run", type=int, action="append", metavar="ID", help="only this run id (repeatable); other filters narrow it further")
    export.add_argument("--serial", help="only this unit")
    export.add_argument("--plan", help="only this plan")
    export.add_argument("--status", choices=RUN_STATUSES, help="only runs with this result")
    export.add_argument("--since", metavar="TIME", help="only runs started at or after this UTC date or time, YYYY-MM-DD[THH:MM]")
    export.add_argument("--until", metavar="TIME", help="only runs started at or before this UTC date or time")
    export.set_defaults(func=cmd_export)

    listing = commands.add_parser("list", help="show the runs in an archive without importing them")
    listing.add_argument("file", help="an archive, or a run file written by show --json")
    listing.add_argument("--json", action="store_true", help="machine-readable output")
    listing.set_defaults(func=cmd_list)

    verify = commands.add_parser("verify", help="check an archive's manifest, hashes, sizes and run records")
    verify.add_argument("file", help="an archive, or a run file written by show --json")
    verify.add_argument("--json", action="store_true", help="machine-readable output")
    verify.set_defaults(func=cmd_verify)

    imported = commands.add_parser(
        "import",
        help="add the runs in an archive, or in a show --json file, to a results database",
        description="Runs are matched by uuid: one already here is skipped, a different run with the same uuid is "
        "refused. New runs get new ids. Everything happens in one transaction.",
    )
    imported.add_argument("file", help="an archive, or a run file written by show --json (benchrun 1.1.0 or later)")
    imported.add_argument("--db", default=DEFAULT_DB, help=f"results database to add to (default: {DEFAULT_DB})")
    imported.add_argument("--dry-run", action="store_true", help="report what would happen and change nothing")
    imported.add_argument("--include-unfinished", action="store_true", help="also import runs still marked running; they stay marked running")
    imported.add_argument("--json", action="store_true", help="machine-readable output")
    imported.set_defaults(func=cmd_import)
