"""Stations: named benches that give each instrument role a plan uses an inventory instrument.

A role is matched the way a station file's keys are: by the plan's instrument
name, or else by its driver name. A station can be written out as a station
file for ``--station FILE``, and station files from benchrun 1.x can be read in.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from benchrun import audit
from benchrun.console import EXIT_ABORTED, EXIT_PASS, check_label, count, open_store, parse_pairs, positive_number, say, table
from benchrun.drivers import DRIVERS
from benchrun.errors import InstrumentFault, PlanError
from benchrun.files import atomic_write_text
from benchrun.inventory import (
    add_actor_arg,
    add_db_arg,
    address_owner,
    check_driver,
    check_name,
    check_text,
    existing_store,
    get_instrument,
    insert_instrument,
    normalize_address,
    read_input_json,
    refuse_existing,
    require_instrument,
    writing,
)
from benchrun.plan import _NAME_RE, DEFAULT_TIMEOUT_S, MAX_TIMEOUT_S, Plan, load_plan
from benchrun.runner import default_transport
from benchrun.store import now_iso

MAX_ROLES = 200
_SECTION_KEYS = ("name", "description", "location", "roles")
_ROLE_KEYS = ("asset_tag", "driver", "timeout_s", "expect_idn")


# Checks ----------------------------------------------------------------


def check_role(text: Any) -> str:
    if not isinstance(text, str) or not _NAME_RE.match(text):
        raise PlanError([f"role {text!r} must start with a letter or underscore and use only letters, digits and underscores, like a plan's instrument names"])
    return text


def check_timeout(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.001 <= value <= MAX_TIMEOUT_S:
        raise PlanError([f"timeout {value!r} must be a number of seconds from 0.001 to {MAX_TIMEOUT_S:g}"])
    return float(value)


def check_pattern(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    text = check_text(value, "expected *IDN? pattern")
    try:
        re.compile(text)
    except re.error as exc:
        raise PlanError([f"expected *IDN? pattern {text!r} is not a valid regular expression: {exc}"]) from None
    return text


# Database --------------------------------------------------------------


def get_station(conn: sqlite3.Connection, name: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM stations WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    return None if row is None else dict(row)


def require_station(conn: sqlite3.Connection, name: str) -> Dict[str, Any]:
    found = get_station(conn, name)
    if found is None:
        raise PlanError([f"no station named {name!r}; see: benchrun station list"])
    return found


def station_roles(conn: sqlite3.Connection, station_id: int) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT r.role, r.timeout_s, r.expect_idn, i.id AS instrument_id, i.asset_tag, i.driver, i.address, i.status,"
        " i.manufacturer, i.model, i.serial_number FROM station_roles r JOIN instruments i ON i.id = r.instrument_id"
        " WHERE r.station_id = ? ORDER BY r.role",
        (station_id,),
    )
    return [dict(row) for row in rows]


def station_runs(conn: sqlite3.Connection, name: str) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM runs WHERE station = ?", (name,)).fetchone()[0])


def _touch(conn: sqlite3.Connection, station_id: int) -> None:
    conn.execute("UPDATE stations SET updated_at = ? WHERE id = ?", (now_iso(), station_id))


def insert_station(conn: sqlite3.Connection, name: str, description: str, location: str) -> int:
    now = now_iso()
    cursor = conn.execute(
        "INSERT INTO stations (name, description, location, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (name, description, location, now, now),
    )
    return int(cursor.lastrowid)


def _refuse_existing_station(conn: sqlite3.Connection, name: str) -> None:
    existing = get_station(conn, name)
    if existing is not None:
        raise PlanError([f"station {existing['name']} already exists; choose another name"])


# Using a station for a plan --------------------------------------------


def bind_plan(conn: sqlite3.Connection, name: str, plan: Plan) -> Tuple[str, Dict[str, str], List[str]]:
    """Point a plan's instruments at a station's, before anything is commanded.

    Returns the station's name, each plan instrument's asset tag, and the roles the
    plan did not use. Addresses always come from the station; a timeout and an
    expected identity pattern do when the station sets them.
    """
    station = require_station(conn, name)
    roles = {row["role"]: row for row in station_roles(conn, station["id"])}
    problems: List[str] = []
    tags: Dict[str, str] = {}
    chosen: Dict[str, str] = {}
    used = set()
    for spec in plan.instruments.values():
        role = spec.name if spec.name in roles else spec.driver if spec.driver in roles else None
        if role is None:
            problems.append(
                f"station {station['name']} has no role for instrument {spec.name!r} ({spec.driver}); "
                f"assign one with: benchrun station assign {station['name']} {spec.name} TAG"
            )
            continue
        used.add(role)
        row = roles[role]
        tag = row["asset_tag"]
        trouble = []
        if row["status"] != "active":
            trouble.append(f"{spec.name}: {tag} is {row['status']}, so it cannot be used")
        if row["driver"] != spec.driver:
            trouble.append(f"{spec.name}: the plan uses the {spec.driver} driver, but {tag} (role {role}) is a {row['driver']}")
        if not row["address"]:
            trouble.append(f"{spec.name}: {tag} has no address; set one with: benchrun instrument edit {tag} --address HOST:PORT")
        if tag in chosen:
            trouble.append(f"{spec.name} and {chosen[tag]} would both use {tag}; give each its own role")
        chosen.setdefault(tag, spec.name)
        problems += trouble
        if trouble:
            continue
        spec.address = row["address"]
        if row["timeout_s"] is not None:
            spec.timeout_s = row["timeout_s"]
        if row["expect_idn"] is not None:
            spec.expect_idn = row["expect_idn"]
        tags[spec.name] = tag
    if problems:
        raise PlanError(problems)
    return station["name"], tags, sorted(set(roles) - used)


def use_station(db: str, name: str, plan: Plan) -> Tuple[str, Dict[str, str]]:
    """``--station-name`` on run, probe and validate."""
    if not Path(db).exists():
        raise PlanError([f"no results database at {db}, so there is no station {name!r} in it; give the right one with --db"])
    with open_store(db) as store:
        station, tags, unused = bind_plan(store.conn, name, plan)
    if unused:
        say(f"Note: roles on station {station} not used by this plan: {', '.join(unused)}")
    return station, tags


def read_identity(
    driver: str, address: str, timeout_s: float, name: str, commands: Optional[Mapping[str, str]] = None, reply_pattern: Optional[str] = None
) -> Tuple[str, Optional[str]]:
    """Connect, ask ``*IDN?``, read the error queue and close. No setting is changed.

    Returns the response, which may be empty, and what went wrong, if anything.
    """
    instrument = DRIVERS[driver](default_transport(address, timeout_s), name, dict(commands or {}), reply_pattern)
    idn = ""
    try:
        instrument.connect()
        idn = instrument.identify()
        instrument.check_errors()
        return idn, None
    except InstrumentFault as exc:
        return idn, str(exc)
    finally:
        instrument.close()


def identity_problems(row: Mapping[str, Any], idn: str) -> List[str]:
    """Compare ``*IDN?`` (maker, model, serial, firmware) with what the inventory says, ignoring case."""
    parts = [part.strip() for part in idn.split(",")]
    problems = []
    for index, key in enumerate(("manufacturer", "model", "serial_number")):
        expected = (row[key] or "").strip()
        got = parts[index] if index < len(parts) else ""
        if expected and expected.lower() != got.lower():
            problems.append(f"inventory {key.replace('_', ' ')} is {expected!r}, *IDN? says {got!r}")
    if row["expect_idn"] and not re.search(row["expect_idn"], idn):
        problems.append(f"*IDN? does not match {row['expect_idn']!r}")
    return problems


# Station files ---------------------------------------------------------


def station_file(station: Mapping[str, Any], roles: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """The 1.x station file, which ``--station FILE`` reads, plus what it takes to import it again unchanged."""
    return {
        "addresses": {row["role"]: row["address"] for row in roles},
        "station": {
            "name": station["name"],
            "description": station["description"],
            "location": station["location"],
            "roles": {row["role"]: {key: row[key] for key in _ROLE_KEYS} for row in roles},
        },
    }


def _read_station_file(path: str) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]], List[str]]:
    data = read_input_json(path, "station file")
    if not isinstance(data, dict) or not isinstance(data.get("addresses"), dict):
        raise PlanError([f'station file {path} needs an object like {{"addresses": {{"dmm": "host:5025"}}}}'])
    ignored = sorted(key for key in data if key not in ("addresses", "station"))
    addresses = data["addresses"]
    if len(addresses) > MAX_ROLES:
        raise PlanError([f"station file {path} has more than {MAX_ROLES} addresses"])
    section = data.get("station")
    if section is None:
        return addresses, None, ignored
    problems = []
    if not isinstance(section, dict):
        raise PlanError([f"station file {path}: station must be an object"])
    problems += [f"station file {path}: unknown key station.{key}" for key in section if key not in _SECTION_KEYS]
    roles = section.get("roles")
    if roles is not None:
        if not isinstance(roles, dict):
            raise PlanError([f"station file {path}: station.roles must be an object"])
        for role, entry in roles.items():
            if not isinstance(entry, dict):
                problems.append(f"station file {path}: station.roles.{role} must be an object")
                continue
            problems += [f"station file {path}: unknown key station.roles.{role}.{key}" for key in entry if key not in _ROLE_KEYS]
        if set(roles) != set(addresses):
            problems.append(f"station file {path}: station.roles must name the same roles as addresses")
    if problems:
        raise PlanError(problems)
    return addresses, section, ignored


def _next_tag(conn: sqlite3.Connection, driver: str, taken: Sequence[str]) -> str:
    """A new asset tag like PSU-0003: one more than the highest such tag already used."""
    prefix = driver.upper().replace("_", "-") + "-"
    numbers = [0]
    tags = [row[0] for row in conn.execute("SELECT asset_tag FROM instruments")] + list(taken)
    for tag in tags:
        if tag.upper().startswith(prefix) and tag[len(prefix):].isdigit():
            numbers.append(int(tag[len(prefix):]))
    return f"{prefix}{max(numbers) + 1:04d}"


def plan_station_import(
    conn: sqlite3.Connection,
    path: str,
    name: Optional[str],
    plan_drivers: Mapping[str, str],
    given_drivers: Mapping[str, str],
) -> Dict[str, Any]:
    """Work out the station and roles a file describes, and which instruments are new."""
    addresses, section, ignored = _read_station_file(path)
    section = section or {}
    problems: List[str] = []
    station: Dict[str, str] = {}
    for key, what in (("name", "station name"), ("description", "description"), ("location", "location")):
        value = name if key == "name" and name is not None else section.get(key, "")
        try:
            station[key] = check_name(value, what) if key == "name" else check_text(value, what)
        except PlanError as exc:
            if key == "name" and not value:
                problems.append(f"station file {path} does not name the station; give it a name with --name")
            else:
                problems += exc.problems
    if station.get("name") and get_station(conn, station["name"]) is not None:
        problems.append(f"station {station['name']} already exists; delete it first, or import under another --name")
    problems += [f"--driver {role}: station file {path} has no role {role!r}" for role in given_drivers if role not in addresses]
    roles: List[Dict[str, Any]] = []
    new_tags: List[str] = []
    seen_addresses: Dict[str, str] = {}
    for role, address_text in addresses.items():
        try:
            check_role(role)
        except PlanError as exc:
            problems += exc.problems
            continue
        entry = section.get("roles", {}).get(role, {})
        try:
            if not isinstance(address_text, str):
                raise PlanError(['the address must be a string like "host:5025"'])
            address = normalize_address(address_text)
            if not address:
                raise PlanError(["the address is empty"])
            driver = given_drivers.get(role) or entry.get("driver") or plan_drivers.get(role) or (role if role in DRIVERS else None)
            if driver is None:
                raise PlanError([f"say which driver it uses, with --driver {role}=DRIVER or --plan PLAN"])
            check_driver(driver)
            role_plan = {
                "role": role,
                "address": address,
                "driver": driver,
                "timeout_s": check_timeout(entry.get("timeout_s")),
                "expect_idn": check_pattern(entry.get("expect_idn")),
            }
            role_plan.update(_match_instrument(conn, role, address, driver, entry.get("asset_tag"), new_tags))
        except PlanError as exc:
            problems += [f"role {role}: {problem}" for problem in exc.problems]
            continue
        if address in seen_addresses:
            problems.append(f"roles {seen_addresses[address]} and {role} have the same address {address}")
        seen_addresses.setdefault(address, role)
        roles.append(role_plan)
    if problems:
        raise PlanError(problems)
    return {"station": station, "roles": roles, "ignored": ignored}


def _match_instrument(
    conn: sqlite3.Connection, role: str, address: str, driver: str, tag: Any, new_tags: List[str]
) -> Dict[str, Any]:
    """The inventory instrument at an address, or a new one to add."""
    owner = address_owner(conn, address)
    if tag is not None:
        tag = check_name(tag, "asset tag")
        found = get_instrument(conn, tag)
        if found is None:
            if owner is not None:
                raise PlanError([f"the file names {tag}, but {address} belongs to {owner} in the inventory"])
            if tag.lower() in (t.lower() for t in new_tags):
                raise PlanError([f"asset tag {tag} is named for more than one role"])
            new_tags.append(tag)
            return {"asset_tag": tag, "new": True}
        if found["address"] != address:
            raise PlanError([f"the file puts {found['asset_tag']} at {address}, but the inventory has it at {found['address'] or 'no address'}"])
        current = found
    elif owner is not None:
        current = require_instrument(conn, owner)
    else:
        tag = _next_tag(conn, driver, new_tags)
        new_tags.append(tag)
        return {"asset_tag": tag, "new": True}
    if current["status"] == "retired":
        raise PlanError([f"{current['asset_tag']} is retired"])
    if current["driver"] != driver:
        raise PlanError([f"{current['asset_tag']} at {address} is a {current['driver']} in the inventory, not a {driver}"])
    return {"asset_tag": current["asset_tag"], "new": False}


def apply_station_import(conn: sqlite3.Connection, planned: Mapping[str, Any], actor: str, source: str) -> None:
    station = planned["station"]
    for role in planned["roles"]:
        if role["new"]:
            fields = {"asset_tag": role["asset_tag"], "driver": role["driver"], "address": role["address"], "location": station["location"]}
            insert_instrument(conn, fields, actor, {"file": source, "station": station["name"]})
    station_id = insert_station(conn, station["name"], station["description"], station["location"])
    for role in planned["roles"]:
        instrument = require_instrument(conn, role["asset_tag"])
        conn.execute(
            "INSERT INTO station_roles (station_id, role, instrument_id, timeout_s, expect_idn) VALUES (?, ?, ?, ?, ?)",
            (station_id, role["role"], instrument["id"], role["timeout_s"], role["expect_idn"]),
        )
    detail = {
        "file": source,
        "description": station["description"],
        "location": station["location"],
        "roles": {role["role"]: role["asset_tag"] for role in planned["roles"]},
    }
    audit.record(conn, "import", f"station:{station['name']}", detail, actor)


# Commands --------------------------------------------------------------


def _role_rows(roles: Sequence[Mapping[str, Any]]) -> List[Sequence[Any]]:
    return [
        (r["role"], r["asset_tag"], r["driver"], r["address"], r["status"], "" if r["timeout_s"] is None else f"{r['timeout_s']:g}", r["expect_idn"] or "")
        for r in roles
    ]


_ROLE_HEADERS = ("role", "asset tag", "driver", "address", "status", "timeout (s)", "expected *IDN?")


def cmd_create(args: argparse.Namespace) -> int:
    actor = check_label(args.actor, "--actor")
    name = check_name(args.name, "station name")
    description = check_label(args.description, "--description")
    location = check_label(args.location, "--location")
    with open_store(args.db) as store, writing(store.conn) as conn:
        _refuse_existing_station(conn, name)
        insert_station(conn, name, description, location)
        audit.record(conn, "create", f"station:{name}", {"description": description, "location": location}, actor)
    say(f"Created station {name}. Give it instruments with: benchrun station assign {name} ROLE TAG")
    return EXIT_PASS


def cmd_list(args: argparse.Namespace) -> int:
    with existing_store(args.db) as store:
        conn = store.conn
        stations = [dict(row) for row in conn.execute("SELECT * FROM stations ORDER BY name COLLATE NOCASE")]
        for station in stations:
            station["roles"] = len(station_roles(conn, station["id"]))
            station["runs"] = station_runs(conn, station["name"])
    if args.json:
        say(json.dumps({"stations": stations}, indent=2))
        return EXIT_PASS
    if not stations:
        say("No stations yet. Make one with: benchrun station create NAME")
        return EXIT_PASS
    rows = [(s["name"], s["location"], s["roles"], s["runs"], s["description"]) for s in stations]
    say(table(("station", "location", "roles", "runs", "description"), rows))
    return EXIT_PASS


def cmd_show(args: argparse.Namespace) -> int:
    with existing_store(args.db) as store:
        station = require_station(store.conn, args.name)
        roles = station_roles(store.conn, station["id"])
        runs = station_runs(store.conn, station["name"])
    if args.json:
        say(json.dumps({"station": station, "roles": roles, "runs": runs}, indent=2))
        return EXIT_PASS
    say(f"Station {station['name']}" + (f": {station['description']}" if station["description"] else ""))
    if station["location"]:
        say(f"Location: {station['location']}")
    say(f"Created {station['created_at']}, last changed {station['updated_at']} (UTC); {count(runs, 'run')} recorded on it")
    say()
    say(table(_ROLE_HEADERS, _role_rows(roles)) if roles else "No instruments assigned yet.")
    return EXIT_PASS


def cmd_assign(args: argparse.Namespace) -> int:
    actor = check_label(args.actor, "--actor")
    role = check_role(args.role)
    timeout = check_timeout(args.timeout)
    pattern = check_pattern(args.expect_idn)
    with open_store(args.db) as store, writing(store.conn) as conn:
        station = require_station(conn, args.name)
        instrument = require_instrument(conn, args.tag)
        tag = instrument["asset_tag"]
        if instrument["status"] == "retired":
            raise PlanError([f"{tag} is retired; reactivate it first, or assign another instrument"])
        if not instrument["address"]:
            raise PlanError([f"{tag} has no address; set one with: benchrun instrument edit {tag} --address HOST:PORT"])
        roles = {row["role"]: row for row in station_roles(conn, station["id"])}
        for other, row in roles.items():
            if other != role and row["asset_tag"] == tag:
                raise PlanError([f"{tag} is already the {other} on station {station['name']}; one instrument cannot fill two roles"])
        before = roles.get(role)
        after = {"asset_tag": tag, "timeout_s": timeout, "expect_idn": pattern}
        if before is not None and all(before[key] == value for key, value in after.items()):
            say(f"Nothing changed: {tag} is already the {role} on station {station['name']} with those settings.")
            return EXIT_PASS
        conn.execute("DELETE FROM station_roles WHERE station_id = ? AND role = ?", (station["id"], role))
        conn.execute(
            "INSERT INTO station_roles (station_id, role, instrument_id, timeout_s, expect_idn) VALUES (?, ?, ?, ?, ?)",
            (station["id"], role, instrument["id"], timeout, pattern),
        )
        _touch(conn, station["id"])
        detail: Dict[str, Any] = {"role": role, **after}
        if before is not None:
            detail["replaced"] = {key: before[key] for key in after}
        audit.record(conn, "assign", f"station:{station['name']}", detail, actor)
    replaced = f", replacing {before['asset_tag']}" if before is not None and before["asset_tag"] != tag else ""
    say(f"Station {station['name']}: {role} is {tag} at {instrument['address']}{replaced}")
    if instrument["status"] != "active":
        say(f"Note: {tag} is {instrument['status']}; runs on this station are refused until it is active.")
    return EXIT_PASS


def cmd_unassign(args: argparse.Namespace) -> int:
    actor = check_label(args.actor, "--actor")
    with open_store(args.db) as store, writing(store.conn) as conn:
        station = require_station(conn, args.name)
        roles = {row["role"]: row for row in station_roles(conn, station["id"])}
        if args.role not in roles:
            raise PlanError([f"station {station['name']} has no role {args.role!r}; it has: {', '.join(roles) or 'none'}"])
        conn.execute("DELETE FROM station_roles WHERE station_id = ? AND role = ?", (station["id"], args.role))
        _touch(conn, station["id"])
        audit.record(conn, "unassign", f"station:{station['name']}", {"role": args.role, "asset_tag": roles[args.role]["asset_tag"]}, actor)
    say(f"Station {station['name']}: {args.role} no longer has an instrument (it was {roles[args.role]['asset_tag']})")
    return EXIT_PASS


def cmd_rename(args: argparse.Namespace) -> int:
    actor = check_label(args.actor, "--actor")
    new = check_name(args.new, "station name")
    with open_store(args.db) as store, writing(store.conn) as conn:
        station = require_station(conn, args.name)
        clash = get_station(conn, new)
        if clash is not None and clash["id"] != station["id"]:
            raise PlanError([f"station {clash['name']} already exists; choose another name"])
        if new == station["name"]:
            say(f"Nothing changed: the station is already called {new}.")
            return EXIT_PASS
        conn.execute("UPDATE stations SET name = ?, updated_at = ? WHERE id = ?", (new, now_iso(), station["id"]))
        audit.record(conn, "rename", f"station:{new}", {"from": station["name"], "to": new}, actor)
        runs = station_runs(conn, station["name"])
    say(f"Renamed station {station['name']} to {new}.")
    if runs:
        say(f"The {count(runs, 'run')} already recorded keep the name {station['name']}; find them with: benchrun history --station {station['name']}")
    return EXIT_PASS


def cmd_delete(args: argparse.Namespace) -> int:
    actor = check_label(args.actor, "--actor")
    with open_store(args.db) as store, writing(store.conn) as conn:
        station = require_station(conn, args.name)
        runs = station_runs(conn, station["name"])
        if runs and not args.force:
            raise PlanError([f"{count(runs, 'run')} recorded on station {station['name']}; add --force to delete the station anyway (the runs are kept)"])
        roles = station_roles(conn, station["id"])
        conn.execute("DELETE FROM station_roles WHERE station_id = ?", (station["id"],))
        conn.execute("DELETE FROM stations WHERE id = ?", (station["id"],))
        detail = {
            "description": station["description"],
            "location": station["location"],
            "roles": {row["role"]: row["asset_tag"] for row in roles},
            "runs": runs,
        }
        audit.record(conn, "delete", f"station:{station['name']}", detail, actor)
    kept = f"; the {count(runs, 'run')} recorded on it are kept" if runs else ""
    say(f"Deleted station {station['name']}{kept}. Its instruments stay in the inventory.")
    return EXIT_PASS


def cmd_export(args: argparse.Namespace) -> int:
    refuse_existing(args.file, args.force)
    with existing_store(args.db) as store:
        station = require_station(store.conn, args.name)
        roles = station_roles(store.conn, station["id"])
    atomic_write_text(args.file, json.dumps(station_file(station, roles), indent=2) + "\n")
    say(f"Wrote station {station['name']} ({count(len(roles), 'role')}) to {args.file}")
    unusable = [f"{r['role']} ({r['asset_tag']} is {r['status']})" for r in roles if r["status"] != "active"]
    if unusable:
        say(f"Note: not in use at the moment: {', '.join(unusable)}")
    return EXIT_PASS


def cmd_import(args: argparse.Namespace) -> int:
    actor = check_label(args.actor, "--actor")
    given = parse_pairs(args.driver, "--driver")
    plan_drivers = {}
    if args.plan:
        plan_drivers = {name: spec.driver for name, spec in load_plan(args.plan).instruments.items()}
    with open_store(args.db) as store, writing(store.conn) as conn:
        planned = plan_station_import(conn, args.file, args.name, plan_drivers, given)
        if args.description is not None:
            planned["station"]["description"] = check_label(args.description, "--description")
        if args.location is not None:
            planned["station"]["location"] = check_label(args.location, "--location")
        if not args.dry_run:
            apply_station_import(conn, planned, actor, Path(args.file).name)
    station = planned["station"]
    if planned["ignored"]:
        say(f"Note: ignored keys in {args.file}: {', '.join(planned['ignored'])}")
    rows = [
        (r["role"], r["asset_tag"], r["driver"], r["address"], "new instrument" if r["new"] else "already in the inventory")
        for r in planned["roles"]
    ]
    say(f"Station {station['name']} from {args.file}:")
    say(table(("role", "asset tag", "driver", "address", "inventory"), rows))
    added = sum(1 for r in planned["roles"] if r["new"])
    if args.dry_run:
        say(f"Dry run: nothing was changed. It would create the station and add {count(added, 'instrument')}.")
    else:
        say(f"Created station {station['name']} with {count(len(rows), 'role')}, and added {count(added, 'instrument')} to the inventory.")
    return EXIT_PASS


def cmd_check(args: argparse.Namespace) -> int:
    with existing_store(args.db) as store:
        station = require_station(store.conn, args.name)
        roles = station_roles(store.conn, station["id"])
    if not roles:
        raise PlanError([f"station {station['name']} has no instruments to check; assign some with: benchrun station assign"])
    results = []
    for row in roles:
        problems = [] if row["status"] == "active" else [f"inventory status is {row['status']}"]
        timeout = row["timeout_s"] if row["timeout_s"] is not None else DEFAULT_TIMEOUT_S
        idn, fault = read_identity(row["driver"], row["address"], timeout, row["role"]) if row["address"] else ("", "no address")
        if fault:
            problems.append(fault)
        elif idn:
            problems += identity_problems(row, idn)
        results.append({"role": row["role"], "asset_tag": row["asset_tag"], "address": row["address"], "idn": idn, "problems": problems})
    bad = sum(1 for r in results if r["problems"])
    if args.json:
        say(json.dumps({"station": station["name"], "instruments": results}, indent=2))
    else:
        rows = [(r["role"], r["asset_tag"], r["address"], r["idn"], "; ".join(r["problems"]) or "ok") for r in results]
        say(table(("role", "asset tag", "address", "*IDN?", "state"), rows))
        if bad:
            say(f"{count(bad, 'instrument')} of {len(results)} on station {station['name']} need attention.")
        else:
            say(f"All {count(len(results), 'instrument')} on station {station['name']} answered and match the inventory.")
    return EXIT_ABORTED if bad else EXIT_PASS


# Argument parsing ------------------------------------------------------


def add_station_name_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--station-name",
        metavar="NAME",
        help="take addresses, timeouts and expected *IDN? patterns from this station in --db, and record its asset tags",
    )


def add_commands(subparsers: Any) -> None:
    group = subparsers.add_parser("station", help="named stations built from inventory instruments")
    commands = group.add_subparsers(dest="station_command", required=True, help="what to do; each has its own --help")

    create = commands.add_parser("create", help="make a new, empty station")
    create.add_argument("name", help="station name: 1 to 64 printable characters, unique ignoring case")
    create.add_argument("--description", default="", help="one line about it")
    create.add_argument("--location", default="", help="where it is")
    add_actor_arg(create)
    add_db_arg(create)
    create.set_defaults(func=cmd_create)

    listing = commands.add_parser("list", help="list stations")
    listing.add_argument("--json", action="store_true", help="machine-readable output")
    add_db_arg(listing)
    listing.set_defaults(func=cmd_list)

    show = commands.add_parser("show", help="one station and the instrument in each role")
    show.add_argument("name", help="station name")
    show.add_argument("--json", action="store_true", help="machine-readable output")
    add_db_arg(show)
    show.set_defaults(func=cmd_show)

    assign = commands.add_parser("assign", help="put an inventory instrument in a role, replacing what was there")
    assign.add_argument("name", help="station name")
    assign.add_argument("role", help="a plan's instrument name, such as supply, or a driver name, which covers every instrument with that driver")
    assign.add_argument("tag", help="asset tag of the instrument")
    assign.add_argument("--timeout", type=positive_number, help="seconds to wait for a response; replaces the plan's timeout_s")
    assign.add_argument("--expect-idn", metavar="REGEX", help="regular expression the *IDN? response must contain; replaces the plan's expect_idn")
    add_actor_arg(assign)
    add_db_arg(assign)
    assign.set_defaults(func=cmd_assign)

    unassign = commands.add_parser("unassign", help="take the instrument out of a role")
    unassign.add_argument("name", help="station name")
    unassign.add_argument("role", help="role")
    add_actor_arg(unassign)
    add_db_arg(unassign)
    unassign.set_defaults(func=cmd_unassign)

    rename = commands.add_parser("rename", help="rename a station; runs already recorded keep the old name")
    rename.add_argument("name", help="current name")
    rename.add_argument("new", help="new name")
    add_actor_arg(rename)
    add_db_arg(rename)
    rename.set_defaults(func=cmd_rename)

    delete = commands.add_parser("delete", help="delete a station; its instruments and run history are kept")
    delete.add_argument("name", help="station name")
    delete.add_argument("--force", action="store_true", help="delete it even though runs were recorded on it")
    add_actor_arg(delete)
    add_db_arg(delete)
    delete.set_defaults(func=cmd_delete)

    export = commands.add_parser("export", help="write a station file that run --station FILE and station import read")
    export.add_argument("name", help="station name")
    export.add_argument("file", help="the JSON file to write")
    export.add_argument("--force", action="store_true", help="replace the file if it already exists")
    add_db_arg(export)
    export.set_defaults(func=cmd_export)

    importing = commands.add_parser("import", help="make a station from a station file, adding instruments the inventory does not have")
    importing.add_argument("file", help='station file like {"addresses": {"supply": "192.0.2.11:5025"}}')
    importing.add_argument("--name", help="station name (default: the name in the file, if it has one)")
    importing.add_argument("--plan", help="a plan whose instrument drivers tell what each role is")
    importing.add_argument("--driver", action="append", metavar="ROLE=DRIVER", help="the driver for one role (repeatable)")
    importing.add_argument("--description", help="one line about it (default: from the file)")
    importing.add_argument("--location", help="where it is (default: from the file)")
    importing.add_argument("--dry-run", action="store_true", help="report what would be made, and change nothing")
    add_actor_arg(importing)
    add_db_arg(importing)
    importing.set_defaults(func=cmd_import)

    check = commands.add_parser("check", help="connect to each instrument on a station and compare *IDN? with the inventory; changes no setting")
    check.add_argument("name", help="station name")
    check.add_argument("--json", action="store_true", help="machine-readable output")
    add_db_arg(check)
    check.set_defaults(func=cmd_check)
