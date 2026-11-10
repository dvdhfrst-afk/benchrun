"""The instrument inventory: what a lab owns, where it is, and whether it may be used.

Asset tags are compared without regard to case, so ``PSU-0001`` and ``psu-0001``
are one instrument; the spelling it was added with is the one recorded. Every
change writes one audit entry in the same transaction as the change itself.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import io
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from benchrun import audit
from benchrun.console import (
    DEFAULT_DB,
    EXIT_PASS,
    EXIT_USAGE,
    check_label,
    count,
    not_negative_whole,
    open_store,
    say,
    table,
)
from benchrun.drivers import DRIVERS
from benchrun.errors import PlanError
from benchrun.files import atomic_write_text
from benchrun.plan import _decode_json, _read_text
from benchrun.reports.csv_report import _text as spreadsheet_safe
from benchrun.store import Store, now_iso
from benchrun.transport import parse_address

STATUSES = ("active", "in-repair", "retired")
FIELDS = ("asset_tag", "driver", "manufacturer", "model", "serial_number", "address", "location", "status", "notes")
MAX_NAME_CHARS = 64
MAX_IMPORT_BYTES = 4 * 1024 * 1024
MAX_IMPORT_ROWS = 10000
FILE_FORMATS = ("csv", "json")
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")  # What csv_report puts an apostrophe in front of.
_COLUMNS = "id, " + ", ".join(FIELDS) + ", added_at, updated_at"


# Checks ----------------------------------------------------------------


def check_name(text: Any, what: str) -> str:
    """Asset tags and station names: 1 to 64 printable characters, with no space at either end."""
    if not isinstance(text, str) or not text or len(text) > MAX_NAME_CHARS or not text.isprintable() or text != text.strip():
        raise PlanError([f"{what} {text!r} must be 1 to {MAX_NAME_CHARS} printable characters, with no space at either end"])
    return text


def check_text(value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise PlanError([f"{what} must be text, got {value!r}"])
    return check_label(value, what)


def check_driver(value: Any) -> str:
    if value not in DRIVERS:
        raise PlanError([f"unknown driver {value!r}; available: {', '.join(sorted(DRIVERS))}"])
    return str(value)


def check_status(value: Any) -> str:
    if value not in STATUSES:
        raise PlanError([f"status {value!r} must be one of {', '.join(STATUSES)}"])
    return str(value)


def normalize_address(value: Any, what: str = "address") -> str:
    """Checked as plan addresses are, then written in full, so two spellings of one socket are seen as one."""
    text = check_text(value, what).strip()
    if not text:
        return ""
    try:
        host, port = parse_address(text)
    except PlanError as exc:
        raise PlanError([f"{what}: {problem}" for problem in exc.problems]) from None
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


_CHECKS = {"asset_tag": lambda v: check_name(v, "asset tag"), "driver": check_driver, "status": check_status, "address": normalize_address}


def clean_fields(raw: Mapping[str, Any]) -> Tuple[Dict[str, str], List[str]]:
    """Check every field given, and return the clean values with every problem found."""
    cleaned: Dict[str, str] = {}
    problems: List[str] = []
    for key, value in raw.items():
        check = _CHECKS.get(key, lambda v, k=key: check_text(v, k.replace("_", " ")))
        try:
            cleaned[key] = check(value)
        except PlanError as exc:
            problems.extend(exc.problems)
    return cleaned, problems


# Database --------------------------------------------------------------


@contextlib.contextmanager
def writing(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One write transaction that holds the lock from its first read, so a check cannot go stale before the write."""
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        yield conn


def existing_store(path: str) -> Store:
    """For commands that only read: a missing database is a mistake, not something to create."""
    if not Path(path).exists():
        raise PlanError([f"no results database at {path}"])
    return open_store(path)


def get_instrument(conn: sqlite3.Connection, tag: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(f"SELECT {_COLUMNS} FROM instruments WHERE asset_tag = ? COLLATE NOCASE", (tag,)).fetchone()
    return None if row is None else dict(row)


def require_instrument(conn: sqlite3.Connection, tag: str) -> Dict[str, Any]:
    found = get_instrument(conn, tag)
    if found is None:
        raise PlanError([f"no instrument with asset tag {tag!r} in the inventory; see: benchrun instrument list"])
    return found


def find_instruments(
    conn: sqlite3.Connection,
    driver: Optional[str] = None,
    status: Optional[str] = None,
    location: Optional[str] = None,
    search: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """``location`` matches whole, ignoring case. ``search`` is found anywhere in any text field."""
    clauses: List[str] = []
    values: List[Any] = []
    for column, value in (("driver", driver), ("status", status)):
        if value is not None:
            clauses.append(f"{column} = ?")
            values.append(value)
    if location is not None:
        clauses.append("location = ? COLLATE NOCASE")
        values.append(location)
    if search is not None:
        searched = [f for f in FIELDS if f not in ("driver", "status")]
        clauses.append("(" + " OR ".join(f"instr(lower({f}), lower(?)) > 0" for f in searched) + ")")
        values += [search] * len(searched)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(f"SELECT {_COLUMNS} FROM instruments{where} ORDER BY asset_tag COLLATE NOCASE", values)
    return [dict(row) for row in rows]


def address_owner(conn: sqlite3.Connection, address: str, own_id: int = -1) -> Optional[str]:
    """The instrument already answering at an address. A retired one has given its address up."""
    row = conn.execute(
        "SELECT asset_tag FROM instruments WHERE address = ? AND status != 'retired' AND id != ?", (address, own_id)
    ).fetchone()
    return None if row is None else row[0]


def stations_using(conn: sqlite3.Connection, instrument_id: int) -> List[Tuple[str, str]]:
    rows = conn.execute(
        "SELECT s.name, r.role FROM station_roles r JOIN stations s ON s.id = r.station_id"
        " WHERE r.instrument_id = ? ORDER BY s.name COLLATE NOCASE, r.role",
        (instrument_id,),
    )
    return [(row[0], row[1]) for row in rows]


def runs_using(conn: sqlite3.Connection, tag: str, limit: int) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT r.id, r.started_at, r.serial, r.plan_name, r.plan_version, r.status, r.station, i.name AS role"
        " FROM run_instruments i JOIN runs r ON r.id = i.run_id WHERE i.asset_tag = ? ORDER BY r.id DESC LIMIT ?",
        (tag, limit),
    )
    return [dict(row) for row in rows]


def _refuse_taken_address(conn: sqlite3.Connection, address: str, status: str, own_id: int = -1) -> None:
    if address and status != "retired":
        owner = address_owner(conn, address, own_id)
        if owner is not None:
            raise PlanError([f"address {address} already belongs to {owner}; retire {owner} or change its address first"])


def insert_instrument(
    conn: sqlite3.Connection, fields: Mapping[str, str], actor: str, extra: Optional[Mapping[str, Any]] = None
) -> Dict[str, Any]:
    record = {key: fields.get(key, "") for key in FIELDS}
    record["status"] = record["status"] or "active"
    now = now_iso()
    conn.execute(
        f"INSERT INTO instruments ({', '.join(FIELDS)}, added_at, updated_at) VALUES ({', '.join('?' * (len(FIELDS) + 2))})",
        [record[key] for key in FIELDS] + [now, now],
    )
    audit.record(conn, "add", f"instrument:{record['asset_tag']}", {**record, **(extra or {})}, actor)
    return record


def change_instrument(
    conn: sqlite3.Connection,
    current: Mapping[str, Any],
    changes: Mapping[str, str],
    actor: str,
    action: str = "edit",
    extra: Optional[Mapping[str, Any]] = None,
) -> Dict[str, List[str]]:
    """Apply what differs and record it as ``{field: [old, new]}``. Nothing differs, nothing is written."""
    diff = {key: [current[key], value] for key, value in changes.items() if key in FIELDS[1:] and current[key] != value}
    if not diff:
        return {}
    settings = ", ".join(f"{key} = ?" for key in diff)
    conn.execute(
        f"UPDATE instruments SET {settings}, updated_at = ? WHERE id = ?",
        [new for _, new in diff.values()] + [now_iso(), current["id"]],
    )
    audit.record(conn, action, f"instrument:{current['asset_tag']}", {**diff, **(extra or {})}, actor)
    return diff


def describe_changes(diff: Mapping[str, Sequence[str]]) -> str:
    return ", ".join(f"{key.replace('_', ' ')} {old!r} -> {new!r}" for key, (old, new) in diff.items())


# Import and export -----------------------------------------------------


def pick_format(path: str, given: Optional[str]) -> str:
    if given:
        return given
    suffix = Path(path).suffix.lower().lstrip(".")
    if suffix in FILE_FORMATS:
        return suffix
    raise PlanError([f"cannot tell the format of {path} from its name; add --format csv or --format json"])


def read_input_text(path: str, what: str) -> str:
    """A file from outside: bounded in size, and decoded as plans are, or refused with a reason."""
    try:
        size = Path(path).stat().st_size
        if size > MAX_IMPORT_BYTES:
            raise PlanError([f"{what} {path} is larger than {MAX_IMPORT_BYTES // (1024 * 1024)} MiB, which is too big"])
        return _read_text(path)
    except OSError as exc:
        raise PlanError([f"cannot read {what} {path}: {exc}"]) from exc


def read_input_json(path: str, what: str) -> Any:
    text = read_input_text(path, what)
    try:
        return _decode_json(text)
    except ValueError as exc:
        raise PlanError([f"{what} {path} is not valid JSON: {exc}"]) from None
    except RecursionError:
        raise PlanError([f"{what} {path} is too deeply nested"]) from None


ImportRow = Tuple[str, Dict[str, Any], List[str]]  # Where in the file, the fields given, problems already found.


def _undo_spreadsheet_quote(cell: str) -> str:
    """Our own export put an apostrophe in front of text a spreadsheet would run; take it off again."""
    return cell[1:] if cell[:1] == "'" and cell[1:2] in _FORMULA_START else cell


def _csv_rows(path: str, text: str) -> List[ImportRow]:
    reader = csv.reader(io.StringIO(text, newline=""))
    rows: List[ImportRow] = []
    try:
        header = [cell.strip() for cell in next(reader, [])]
        if not header:
            raise PlanError([f"{path} is empty; the first line must name the columns, such as {','.join(FIELDS)}"])
        unknown = [cell for cell in header if cell not in FIELDS]
        repeated = sorted({cell for cell in header if header.count(cell) > 1})
        problems = [f"{path}: unknown column {cell!r}; columns are {', '.join(FIELDS)}" for cell in unknown]
        problems += [f"{path}: column {cell!r} appears more than once" for cell in repeated]
        if "asset_tag" not in header:
            problems.append(f"{path}: needs an asset_tag column")
        if problems:
            raise PlanError(problems)
        for cells in reader:
            if not any(cell.strip() for cell in cells):
                continue
            where = f"line {reader.line_num}"
            if len(rows) >= MAX_IMPORT_ROWS:
                raise PlanError([f"{path} has more than {MAX_IMPORT_ROWS} rows; split it into smaller files"])
            if len(cells) != len(header):
                rows.append((where, {}, [f"has {len(cells)} cells, but the header names {len(header)} columns"]))
                continue
            raw = {key: _undo_spreadsheet_quote(cell) for key, cell in zip(header, cells)}
            for key in ("driver", "status"):  # An empty cell keeps what is there.
                if raw.get(key, None) == "":
                    del raw[key]
            rows.append((where, raw, []))
    except csv.Error as exc:
        raise PlanError([f"{path} line {reader.line_num} is not valid CSV: {exc}"]) from None
    return rows


def _json_rows(path: str, data: Any) -> List[ImportRow]:
    if not isinstance(data, dict) or set(data) != {"instruments"} or not isinstance(data["instruments"], list):
        raise PlanError([f'{path} must hold one object like {{"instruments": [{{"asset_tag": "PSU-0001", "driver": "psu"}}]}}'])
    items = data["instruments"]
    if len(items) > MAX_IMPORT_ROWS:
        raise PlanError([f"{path} has more than {MAX_IMPORT_ROWS} instruments; split it into smaller files"])
    rows: List[ImportRow] = []
    for index, item in enumerate(items):
        where = f"instruments[{index}]"
        if not isinstance(item, dict):
            rows.append((where, {}, [f"must be an object, got {item!r}"[:200]]))
            continue
        unknown = [key for key in item if key not in FIELDS]
        problems = [f"unknown key {key!r}; keys are {', '.join(FIELDS)}" for key in unknown]
        rows.append((where, {key: value for key, value in item.items() if key in FIELDS}, problems))
    return rows


def read_import_file(path: str, file_format: Optional[str]) -> List[ImportRow]:
    kind = pick_format(path, file_format)
    if kind == "csv":
        return _csv_rows(path, read_input_text(path, "import file"))
    return _json_rows(path, read_input_json(path, "import file"))


def plan_import(conn: sqlite3.Connection, rows: Sequence[ImportRow]) -> List[Dict[str, Any]]:
    """Work out what each row would do, refusing any row that cannot be applied as it stands."""
    planned: List[Dict[str, Any]] = []
    by_tag: Dict[str, List[str]] = {}
    for where, raw, _ in rows:
        if isinstance(raw.get("asset_tag"), str):
            by_tag.setdefault(raw["asset_tag"].lower(), []).append(where)
    for where, raw, found in rows:
        fields, problems = clean_fields(raw)
        problems = list(found) + problems
        tag = fields.get("asset_tag")
        entry: Dict[str, Any] = {"where": where, "tag": tag or raw.get("asset_tag"), "fields": fields, "problems": problems}
        planned.append(entry)
        if "asset_tag" not in raw and not found:
            problems.append("has no asset_tag")
        if tag is None:
            continue
        others = [w for w in by_tag.get(tag.lower(), []) if w != where]
        if others:
            problems.append(f"asset tag {tag} appears more than once in this file (also at {', '.join(others)})")
        current = get_instrument(conn, tag)
        entry["current"] = current
        if current is None:
            entry["outcome"] = "add"
            if "driver" not in fields and "driver" not in raw:
                problems.append(f"{tag} is new, so it needs a driver")
        else:
            entry["tag"] = current["asset_tag"]
            entry["changes"] = {k: v for k, v in fields.items() if k != "asset_tag" and current[k] != v}
            entry["outcome"] = "change" if entry["changes"] else "unchanged"
    _refuse_shared_addresses(conn, planned)
    return planned


def _refuse_shared_addresses(conn: sqlite3.Connection, planned: List[Dict[str, Any]]) -> None:
    """After the import, no two instruments in use may answer at one address."""
    final: Dict[str, Tuple[str, str]] = {row["asset_tag"].lower(): (row["address"], row["status"]) for row in find_instruments(conn)}
    touched: Dict[str, Dict[str, Any]] = {}
    for entry in planned:
        if entry["problems"] or entry.get("outcome") is None:
            continue
        key = entry["tag"].lower()
        address, status = final.get(key, ("", "active"))
        fields = entry["fields"]
        final[key] = (fields.get("address", address), fields.get("status", status))
        touched[key] = entry
    holders: Dict[str, List[str]] = {}
    for key, (address, status) in final.items():
        if address and status != "retired":
            holders.setdefault(address, []).append(key)
    for address, keys in holders.items():
        if len(keys) < 2:
            continue
        for key in keys:
            if key in touched:
                rest = sorted(k for k in keys if k != key)
                touched[key]["problems"].append(f"address {address} would also belong to {', '.join(rest)}")


def apply_import(conn: sqlite3.Connection, planned: Sequence[Dict[str, Any]], actor: str, source: str) -> None:
    extra = {"file": source}
    for entry in planned:
        if entry["outcome"] == "add":
            insert_instrument(conn, entry["fields"], actor, extra)
        elif entry["outcome"] == "change":
            change_instrument(conn, entry["current"], entry["changes"], actor, "edit", extra)


def render_export(rows: Sequence[Mapping[str, Any]], file_format: str) -> str:
    if file_format == "json":
        return json.dumps({"instruments": [{key: row[key] for key in FIELDS} for row in rows]}, indent=2) + "\n"
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(FIELDS)
    for row in rows:
        writer.writerow([spreadsheet_safe(row[key]) for key in FIELDS])
    return buffer.getvalue()


def refuse_existing(path: str, force: bool) -> None:
    if Path(path).exists() and not force:
        raise PlanError([f"{path} already exists; choose another name or add --force to replace it"])


# Commands --------------------------------------------------------------


def _given(args: argparse.Namespace, names: Sequence[str]) -> Dict[str, Any]:
    return {name: getattr(args, name) for name in names if getattr(args, name) is not None}


_DESCRIPTIVE = ("manufacturer", "model", "serial_number", "address", "location", "notes")


def cmd_add(args: argparse.Namespace) -> int:
    actor = check_label(args.actor, "--actor")
    fields, problems = clean_fields({"asset_tag": args.tag, "driver": args.driver, "status": args.status, **_given(args, _DESCRIPTIVE)})
    if problems:
        raise PlanError(problems)
    with open_store(args.db) as store, writing(store.conn) as conn:
        existing = get_instrument(conn, fields["asset_tag"])
        if existing is not None:
            raise PlanError([f"asset tag {existing['asset_tag']} is already in the inventory; change it with: benchrun instrument edit"])
        _refuse_taken_address(conn, fields.get("address", ""), fields["status"])
        record = insert_instrument(conn, fields, actor)
    where = f" at {record['address']}" if record["address"] else ", no address yet"
    say(f"Added {record['asset_tag']}: {record['driver']}{where}, {record['status']}")
    return EXIT_PASS


def cmd_list(args: argparse.Namespace) -> int:
    if args.driver is not None:
        check_driver(args.driver)
    with existing_store(args.db) as store:
        rows = find_instruments(store.conn, args.driver, args.status, args.location, args.search)
    if args.json:
        say(json.dumps({"instruments": rows}, indent=2))
        return EXIT_PASS
    if not rows:
        say("No matching instruments.")
        return EXIT_PASS
    headers = ("asset tag", "driver", "manufacturer", "model", "serial", "address", "location", "status")
    say(table(headers, [[row[key] for key in FIELDS[:8]] for row in rows]))
    say(count(len(rows), "instrument"))
    return EXIT_PASS


def cmd_show(args: argparse.Namespace) -> int:
    with existing_store(args.db) as store:
        record = require_instrument(store.conn, args.tag)
        used = stations_using(store.conn, record["id"])
        runs = runs_using(store.conn, record["asset_tag"], args.runs)
    if args.json:
        payload = {"instrument": record, "stations": [{"station": s, "role": r} for s, r in used], "runs": runs}
        say(json.dumps(payload, indent=2))
        return EXIT_PASS
    say(f"{record['asset_tag']}: {record['driver']}, {record['status']}")
    for key in _DESCRIPTIVE:
        if record[key]:
            say(f"  {key.replace('_', ' ').capitalize()}: {record[key]}")
    say(f"  Added {record['added_at']}, last changed {record['updated_at']} (UTC)")
    say("Stations: " + (", ".join(f"{station} as {role}" for station, role in used) if used else "none"))
    if not runs:
        say("No recorded runs used it.")
        return EXIT_PASS
    say(f"Newest runs that used it (up to {args.runs}):")
    rows = [
        (r["id"], r["started_at"][:19].replace("T", " "), r["serial"], f"{r['plan_name']} {r['plan_version']}".rstrip(), r["status"].upper(), r["role"], r["station"])
        for r in runs
    ]
    say(table(("run", "started (UTC)", "serial", "plan", "status", "as", "station"), rows))
    return EXIT_PASS


def cmd_edit(args: argparse.Namespace) -> int:
    actor = check_label(args.actor, "--actor")
    raw = _given(args, ("driver", "status") + _DESCRIPTIVE)
    if not raw:
        raise PlanError(["nothing to change; give at least one of --driver, --manufacturer, --model, --serial-number, --address, --location, --notes, --status"])
    changes, problems = clean_fields(raw)
    if problems:
        raise PlanError(problems)
    with open_store(args.db) as store, writing(store.conn) as conn:
        current = require_instrument(conn, args.tag)
        if current["status"] == "retired" and "status" in changes:
            raise PlanError([f"{current['asset_tag']} is retired; bring it back with: benchrun instrument reactivate {current['asset_tag']}"])
        _refuse_taken_address(conn, changes.get("address", current["address"]), changes.get("status", current["status"]), current["id"])
        diff = change_instrument(conn, current, changes, actor)
    if not diff:
        say(f"Nothing changed: {current['asset_tag']} already has those values.")
    else:
        say(f"Changed {current['asset_tag']}: {describe_changes(diff)}")
    return EXIT_PASS


def _set_status(args: argparse.Namespace, status: str, action: str) -> int:
    actor = check_label(args.actor, "--actor")
    reason = check_label(args.reason, "--reason")
    with open_store(args.db) as store, writing(store.conn) as conn:
        current = require_instrument(conn, args.tag)
        tag = current["asset_tag"]
        if current["status"] == status:
            say(f"Nothing changed: {tag} is already {status}.")
            return EXIT_PASS
        _refuse_taken_address(conn, current["address"], status, current["id"])
        change_instrument(conn, current, {"status": status}, actor, action, {"reason": reason} if reason else None)
        used = stations_using(conn, current["id"])
    say(f"{tag} is now {status} (was {current['status']}).")
    if status != "active" and used:
        names = ", ".join(f"{station} as {role}" for station, role in used)
        say(f"Note: it is still assigned to {names}, and runs there are refused until it is replaced or reactivated.")
    return EXIT_PASS


def cmd_retire(args: argparse.Namespace) -> int:
    return _set_status(args, "retired", "retire")


def cmd_reactivate(args: argparse.Namespace) -> int:
    return _set_status(args, "active", "reactivate")


def _import_line(entry: Mapping[str, Any], dry_run: bool) -> Optional[str]:
    tag = entry["tag"]
    if entry["problems"]:
        return f"  Refused    {entry['where']}: {'; '.join(entry['problems'])}"
    if entry["outcome"] == "add":
        fields = entry["fields"]
        where = f" at {fields['address']}" if fields.get("address") else ""
        return f"  {'Would add' if dry_run else 'Added':<10} {tag}: {fields.get('driver', '')}{where}"
    if entry["outcome"] == "change":
        diff = {key: [entry["current"][key], value] for key, value in entry["changes"].items()}
        return f"  {'Would change' if dry_run else 'Changed':<10} {tag}: {describe_changes(diff)}"
    return None


def cmd_import(args: argparse.Namespace) -> int:
    actor = check_label(args.actor, "--actor")
    rows = read_import_file(args.file, args.format)
    if not rows:
        raise PlanError([f"{args.file} lists no instruments"])
    with open_store(args.db) as store, writing(store.conn) as conn:
        planned = plan_import(conn, rows)
        refused = [entry for entry in planned if entry["problems"]]
        if not refused and not args.dry_run:
            apply_import(conn, planned, actor, Path(args.file).name)
    hypothetical = args.dry_run or bool(refused)
    for entry in planned:
        line = _import_line(entry, hypothetical)
        if line:
            say(line)
    tally = {outcome: sum(1 for e in planned if not e["problems"] and e.get("outcome") == outcome) for outcome in ("add", "change", "unchanged")}
    verbs = ("would be added", "would change") if hypothetical else ("added", "changed")
    say(f"{tally['add']} {verbs[0]}, {tally['change']} {verbs[1]}, {tally['unchanged']} unchanged, {len(refused)} refused.")
    if refused:
        say(f"Nothing was imported, because {count(len(refused), 'row')} cannot be used. Fix them and import the file again.")
        return EXIT_USAGE
    say("Dry run: nothing was changed." if args.dry_run else f"Imported {args.file}.")
    return EXIT_PASS


def cmd_export(args: argparse.Namespace) -> int:
    file_format = pick_format(args.file, args.format)
    refuse_existing(args.file, args.force)
    with existing_store(args.db) as store:
        rows = find_instruments(store.conn, args.driver, args.status, args.location, args.search)
    if not rows:
        raise PlanError(["no matching instruments, so nothing was written"])
    atomic_write_text(args.file, render_export(rows, file_format))
    say(f"Wrote {count(len(rows), 'instrument')} to {args.file}")
    return EXIT_PASS


# Argument parsing ------------------------------------------------------


def add_db_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", default=DEFAULT_DB, help=f"results database (default: {DEFAULT_DB})")


def add_actor_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--actor", default="", help="who is making the change, kept in the audit log (default: empty)")


def _add_descriptive_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manufacturer", help="maker's name")
    parser.add_argument("--model", help="model number")
    parser.add_argument("--serial-number", help="the instrument's own serial number")
    parser.add_argument("--address", help="host, host:port or [ipv6]:port; the port defaults to 5025")
    parser.add_argument("--location", help="where it is kept, such as a lab or bench")
    parser.add_argument("--notes", help="one line of free text")


def _add_filter_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--driver", help="only instruments with this driver")
    parser.add_argument("--status", choices=STATUSES, help="only instruments with this status")
    parser.add_argument("--location", help="only instruments at this location (whole name, any case)")
    parser.add_argument("--search", metavar="TEXT", help="only instruments with this text in a tag, maker, model, serial, address, location or notes")


def add_commands(subparsers: Any) -> None:
    group = subparsers.add_parser("instrument", help="the instrument inventory: add, list, edit, retire, import and export")
    commands = group.add_subparsers(dest="instrument_command", required=True, help="what to do; each has its own --help")

    add = commands.add_parser("add", help="add an instrument to the inventory")
    add.add_argument("tag", help="asset tag: 1 to 64 printable characters, unique ignoring case")
    add.add_argument("--driver", required=True, help=f"driver that talks to it: {', '.join(sorted(DRIVERS))}")
    add.add_argument("--status", default="active", choices=STATUSES[:2], help="active (default) or in-repair")
    _add_descriptive_args(add)
    add_actor_arg(add)
    add_db_arg(add)
    add.set_defaults(func=cmd_add)

    listing = commands.add_parser("list", help="list instruments, optionally filtered")
    _add_filter_args(listing)
    listing.add_argument("--json", action="store_true", help="machine-readable output")
    add_db_arg(listing)
    listing.set_defaults(func=cmd_list)

    show = commands.add_parser("show", help="one instrument, the stations it is on and the runs that used it")
    show.add_argument("tag", help="asset tag")
    show.add_argument("--runs", type=not_negative_whole, default=10, help="how many recent runs to list (default: 10)")
    show.add_argument("--json", action="store_true", help="machine-readable output")
    add_db_arg(show)
    show.set_defaults(func=cmd_show)

    edit = commands.add_parser("edit", help="change an instrument's details; an empty value clears a field")
    edit.add_argument("tag", help="asset tag")
    edit.add_argument("--driver", help="driver that talks to it")
    edit.add_argument("--status", choices=STATUSES[:2], help="active or in-repair; use retire to retire it")
    _add_descriptive_args(edit)
    add_actor_arg(edit)
    add_db_arg(edit)
    edit.set_defaults(func=cmd_edit)

    for name, func, text in (
        ("retire", cmd_retire, "retire an instrument: it stays in the inventory and in run history, and runs refuse it"),
        ("reactivate", cmd_reactivate, "bring a retired or in-repair instrument back into use"),
    ):
        command = commands.add_parser(name, help=text)
        command.add_argument("tag", help="asset tag")
        command.add_argument("--reason", default="", help="why, kept in the audit log")
        add_actor_arg(command)
        add_db_arg(command)
        command.set_defaults(func=func)

    importing = commands.add_parser("import", help="add or update instruments from a CSV or JSON file, all or nothing")
    importing.add_argument("file", help="CSV with a header row naming columns, or JSON like the export writes")
    importing.add_argument("--format", choices=FILE_FORMATS, help="file format (default: from the file name)")
    importing.add_argument("--dry-run", action="store_true", help="report what would change, and change nothing")
    add_actor_arg(importing)
    add_db_arg(importing)
    importing.set_defaults(func=cmd_import)

    export = commands.add_parser("export", help="write instruments to a CSV or JSON file that import reads")
    export.add_argument("file", help="the file to write")
    export.add_argument("--format", choices=FILE_FORMATS, help="file format (default: from the file name)")
    export.add_argument("--force", action="store_true", help="replace the file if it already exists")
    _add_filter_args(export)
    add_db_arg(export)
    export.set_defaults(func=cmd_export)
