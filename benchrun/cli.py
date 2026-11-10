"""The command line: ``python -m benchrun <command>``."""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import re
import sqlite3
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from benchrun import __version__, lots, plugins, session
from benchrun.drivers import DRIVERS
from benchrun.errors import BenchrunError, InstrumentFault, PlanError
from benchrun.files import atomic_write_text
from benchrun.plan import safety_notes, select_steps
from benchrun.product_commands import print_effective_limits
from benchrun.reports import CHOICES, describe_formats, resolve_formats, write_reports
from benchrun.reports.export import EXPORT_FORMATS, export_runs
from benchrun.runner import default_transport
from benchrun.sim.bench import DEFAULT_BASE_PORT, SimBench, parse_fault, simulator_classes
from benchrun.stations import add_station_name_arg, read_identity, use_station
from benchrun.store import Store
from benchrun.console import (
    say as _say,
    complain as _complain,
    parse_variables as _parse_variables,
    check_serial as _check_serial,
    utc_time as _utc_time,
    positive_number as _positive_number,
    not_negative_whole as _not_negative_whole,
    open_store as _open_store,
    count as _count,
    table as _table,
    fmt as _fmt,
    DEFAULT_DB,
    EXIT_ABORTED,
    EXIT_FAIL,  # noqa: F401 (scripts and tests take every exit code from here)
    EXIT_PASS,
    EXIT_USAGE,
)


# Commands --------------------------------------------------------------


def cmd_sim(args: argparse.Namespace) -> int:
    faults: Dict[str, Any] = {}
    for text in args.fault or []:
        faults.update(parse_fault(text))
    if not 0 <= args.base_port <= 65536 - len(simulator_classes()):
        raise PlanError([f"--base-port {args.base_port} is out of range; the top port must not pass 65535"])
    bench = SimBench(dut=args.dut, seed=args.seed, noise=args.noise, base_port=args.base_port, faults=faults)
    try:
        bench.start()
    except OSError as exc:
        _say(f"cannot start simulators: {exc}")
        _say("Another process may hold those ports. Try --base-port with a different number, or 0 for any free port.")
        return EXIT_USAGE
    try:
        _say(f"benchrun simulators on 127.0.0.1 (simulated unit: {args.dut}, seed: {args.seed}, noise: {args.noise:g})")
        width = max(7, *(len(kind) for kind in bench.devices))
        for kind, device in bench.devices.items():
            _say(f"  {kind:<{width}} {bench.servers[kind].address:<20} {device.idn}")
        if args.write_addresses:
            atomic_write_text(args.write_addresses, json.dumps({"addresses": bench.addresses}, indent=2) + "\n")
            _say(f"Addresses written to {args.write_addresses}")
        _say("Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(0.25)
        except KeyboardInterrupt:
            _say("Stopping.")
        return EXIT_PASS
    finally:
        bench.stop()


def _lot_for_run(args: argparse.Namespace) -> Optional[Dict[str, Any]]:
    """The open lot ``run --lot`` names. A product lot gives the product when the run names none."""
    if not args.lot:
        if args.retest:
            raise PlanError(["--retest goes with --lot"])
        return None
    if not Path(args.db).exists():
        raise PlanError([f"no results database at {args.db}, so there is no lot {args.lot!r} in it; give the right one with --db"])
    with _open_store(args.db) as store:
        lot = lots.find_open(store.conn, args.lot)
    if args.product is None and lot["product"]:
        args.product = lot["product"]
    return lot


def cmd_run(args: argparse.Namespace) -> int:
    serial = _check_serial(args.serial)
    variables = _parse_variables(args.set)
    options = session.unit_options(args)
    lot = _lot_for_run(args)
    product, plan_path = session.product_and_plan_path(args)
    if product is not None:
        product.check_serial(serial)
        options.product = product.part_number
    with session.bench_for(args, plan_path, product, variables) as bench:
        options.only_steps = select_steps(bench.plan, args.step) if args.step else None
        with session.scpi_log(args.scpi_log) as log, _open_store(args.db) as store:
            if lot is not None:
                lots.check_unit(lot, bench.plan.name, options.product)
                problem = lots.serial_problem(lot, serial, lots.passed_serials(store.conn, lot["id"]), args.retest)
                if problem:
                    raise PlanError([problem])
                options.lot_id = lot["id"]
                _say(f"Lot {lot['name']}")
            outcome = session.run_unit(store, bench, serial, options, log)
    return outcome.code


def cmd_validate(args: argparse.Namespace) -> int:
    variables = _parse_variables(args.set)
    product, plan_path = session.product_and_plan_path(args)
    plan = session.load(plan_path, variables, product)
    station, asset_tags = use_station(args.db, args.station_name, plan) if args.station_name else ("", {})
    points = sum(len(step.points()) * len(step.measures) + len(step.summary) for step in plan.steps)
    _say(f"{plan_path}: OK")
    _say(f"  {plan.name} {plan.version}".rstrip())
    _say(f"  {len(plan.instruments)} instruments, {len(plan.steps)} steps, {points} measurements per run")
    if station:
        used = ", ".join(f"{name}={tag} ({plan.instruments[name].address})" for name, tag in asset_tags.items())
        _say(f"  Station {station}: {used}")
    unaddressed = [spec.name for spec in plan.instruments.values() if not spec.address]
    if unaddressed:
        _say(f"  Needs an address from --station, --address or --sim: {', '.join(unaddressed)}")
    for note in safety_notes(plan):
        _say(f"  Note: {note}")
    if product is not None:
        print_effective_limits(plan, product)
    return EXIT_PASS


def cmd_probe(args: argparse.Namespace) -> int:
    """Connect to each instrument in a plan without changing its settings."""
    plan, station, asset_tags = session.load_for_bench(args)
    rows = []
    problems = 0
    for spec in plan.instruments.values():
        idn, fault = read_identity(spec.driver, spec.address or "", spec.timeout_s, spec.name, spec.commands, spec.reply_pattern)
        state = fault or "ok"
        if fault is None and spec.expect_idn and not re.search(spec.expect_idn, idn):
            state = f"*IDN? does not match {spec.expect_idn!r}"
        problems += state != "ok"
        rows.append((spec.name, spec.driver, spec.address, idn, state) + ((asset_tags.get(spec.name),) if station else ()))
    _say(_table(("name", "driver", "address", "*IDN?", "state") + (("asset tag",) if station else ()), rows))
    return EXIT_ABORTED if problems else EXIT_PASS


def cmd_scpi(args: argparse.Namespace) -> int:
    """Send raw commands to one instrument. Anything with a ? reads a response."""
    transport = default_transport(args.address, args.timeout)
    try:
        transport.open()
        for command in args.commands:
            transport.write_line(command)
            if "?" in command:
                _say(transport.read_line())
    except InstrumentFault as exc:
        _say(f"error: {exc}")
        return EXIT_ABORTED
    finally:
        transport.close()
    return EXIT_PASS


def _pick_run_ids(args: argparse.Namespace, store: Store) -> List[int]:
    if args.run is not None:
        return [args.run]
    if not args.serial and not args.plan and not args.latest:
        raise PlanError(["say which run: --run ID, --latest, --serial SN or --plan NAME"])
    rows = store.find_runs(serial=args.serial, plan=args.plan)
    if not rows:
        return []
    if getattr(args, "all", False):
        return [row["id"] for row in reversed(rows)]
    return [rows[0]["id"]]


def cmd_report(args: argparse.Namespace) -> int:
    if not Path(args.db).exists():
        raise PlanError([f"no results database at {args.db}"])
    formats = resolve_formats(args.format or ["all"])
    with _open_store(args.db) as store:
        run_ids = _pick_run_ids(args, store)
        if not run_ids:
            _say("No matching runs.")
            return EXIT_USAGE
        for run_id in run_ids:
            detail = store.run_detail(run_id)
            if detail is None:
                _say(f"No run with id {run_id}.")
                return EXIT_USAGE
            for path in write_reports(detail, args.out, formats):
                _say(f"Report: {path}")
    return EXIT_PASS


def cmd_show(args: argparse.Namespace) -> int:
    """One run in the terminal: enough to see what went wrong without opening a report."""
    if not Path(args.db).exists():
        raise PlanError([f"no results database at {args.db}"])
    with _open_store(args.db) as store:
        run_ids = _pick_run_ids(args, store)
        detail = store.run_detail(run_ids[0]) if run_ids else None
        lot_name = lots.name_of(store.conn, detail["lot_id"]) if detail is not None else ""
    if detail is None:
        _say("No matching run.")
        return EXIT_USAGE
    if args.json:
        _say(json.dumps(detail, indent=2))
        return EXIT_PASS
    _say(f"Run {detail['id']}: serial {detail['serial']}, plan {detail['plan_name']} {detail['plan_version']}".rstrip())
    _say(f"Result: {detail['status'].upper()}" + (f" ({detail['reason']})" if detail["reason"] else ""))
    if detail["status"] == "running":
        _say("It never finished: it is still going, or the process stopped. Check the bench; an output may still be on.")
    if detail["selected_steps"] is not None:
        _say(f"Only these steps were selected: {', '.join(detail['selected_steps'])}")
    _say(f"Started {detail['started_at']}, finished {detail['finished_at'] or 'never'} (UTC)")
    for label, value in (
        ("Product", detail["product"]),
        ("Lot", lot_name),
        ("Station", detail["station"]),
        ("Operator", detail["operator"]),
        ("Note", detail["note"]),
    ):
        if value:
            _say(f"{label}: {value}")
    _say()
    instruments = [(i["name"], i["driver"], i["address"], i["asset_tag"], i["idn"]) for i in detail["instruments"]]
    _say(_table(("instrument", "driver", "address", "asset tag", "*IDN?"), instruments))
    _say()
    for step in detail["steps"]:
        counts = step["measurements"]
        bad = sum(1 for m in counts if m["verdict"] in ("fail", "error"))
        tail = f": {step['message']}" if step["message"] and not counts else ""
        _say(f"[{step['status'].upper()}] {step['name']} ({len(counts)} measured, {bad} failed){tail}")
        for m in counts:
            if args.all_measurements or m["verdict"] in ("fail", "error"):
                where = " ".join(f"{k}={v:g}" for k, v in m["point"].items())
                shown = "no value" if m["value"] is None else f"{m['value']:.6g} {m['units']}".rstrip()
                limits = f"{_fmt(m['low']) or '-inf'} to {_fmt(m['high']) or '+inf'}"
                extra = f" ({m['note']})" if m["note"] else ""
                _say(f"    {m['verdict'].upper():<5} {m['name']}{' at ' + where if where else ''}: {shown}, limits {limits}{extra}")
    return EXIT_PASS


def cmd_export(args: argparse.Namespace) -> int:
    """Every matching run in one CSV, JSON Lines or Excel file, for a spreadsheet or a script."""
    if not Path(args.db).exists():
        raise PlanError([f"no results database at {args.db}"])
    if Path(args.out).exists() and not args.force:
        raise PlanError([f"{args.out} already exists; choose another name or add --force to replace it"])
    since = _utc_time(args.since, "--since")
    until = _utc_time(args.until, "--until", end_of_day=True)
    with _open_store(args.db) as store:
        rows = store.find_runs(serial=args.serial, plan=args.plan, status=args.status, since=since, until=until)
        run_ids = [row["id"] for row in reversed(rows)]
        if not run_ids:
            _complain("No matching runs, so nothing was written.")
            return EXIT_USAGE
        # One run in memory at a time, however many there are.
        export_runs((store.run_detail(run_id) for run_id in run_ids), args.out, args.format)
    _say(f"Wrote {_count(len(run_ids), 'run')} to {args.out}")
    return EXIT_PASS


STARTER_PLAN = {
    "name": "my-first-plan",
    "version": "1.0",
    "description": "Sets an input voltage and checks an output voltage. Replace the limits and addresses with your own.",
    "vars": {"vnom": 5.0, "tolerance": 0.1},
    "instruments": {
        "supply": {"driver": "psu", "address": "127.0.0.1:5026", "timeout_s": 5.0},
        "dmm": {"driver": "dmm", "address": "127.0.0.1:5025", "timeout_s": 5.0},
    },
    "setup": [
        {"do": "supply.set_current_limit", "amps": 0.5},
        {"do": "supply.set_voltage", "volts": 12.0},
        {"do": "supply.output_on"},
        {"wait_s": 0.1},
    ],
    "steps": [
        {
            "name": "output_voltage",
            "description": "The output stays in tolerance across the input range.",
            "sweep": {"var": "vin", "values": [8.0, 12.0, 16.0]},
            "apply": [{"do": "supply.set_voltage", "volts": "=vin"}],
            "settle_s": 0.05,
            "measure": [
                {
                    "name": "vout",
                    "read": "dmm.measure_dc_voltage",
                    "units": "V",
                    "low": "=vnom - tolerance",
                    "high": "=vnom + tolerance",
                }
            ],
        }
    ],
    "teardown": [{"do": "supply.output_off"}],
}


def cmd_init(args: argparse.Namespace) -> int:
    """Write a starter plan that runs as it is against the simulators."""
    if Path(args.plan).exists() and not args.force:
        raise PlanError([f"{args.plan} already exists; choose another name or add --force to replace it"])
    atomic_write_text(args.plan, json.dumps(STARTER_PLAN, indent=2) + "\n")
    _say(f"Wrote {args.plan}. Try it on the simulators:")
    _say(f"  python -m benchrun run {args.plan} --serial TRY-1 --sim")
    return EXIT_PASS


def _print_runs(rows: List[Dict[str, Any]]) -> None:
    table_rows = [
        (
            row["id"],
            row["started_at"][:19].replace("T", " "),
            row["serial"],
            f"{row['plan_name']} {row['plan_version']}".rstrip(),
            row["status"].upper() + ("" if row["selected_steps"] is None else " (some steps)"),
            row["passed"],
            row["failed"],
            row["errors"],
            "; ".join(part for part in (row["reason"], row["note"]) if part),
        )
        for row in rows
    ]
    headers: Tuple[str, ...] = ("run", "started (UTC)", "serial", "plan", "status", "pass", "fail", "error", "reason or note")
    if any(row["product"] for row in rows):
        headers = headers[:3] + ("product",) + headers[3:]
        table_rows = [cells[:3] + (row["product"],) + cells[3:] for cells, row in zip(table_rows, rows)]
    _say(_table(headers, table_rows))


def _print_stats(rows: List[Dict[str, Any]]) -> None:
    table_rows = [
        (
            row["step"],
            row["name"],
            ", ".join(f"{k}={v:g}" for k, v in row["point"].items()),
            row["units"],
            row["count"],
            row["fails"],
            _fmt(row["mean"]),
            _fmt(row["stdev"]),
            _fmt(row["minimum"]),
            _fmt(row["maximum"]),
            _fmt(row["low"]),
            _fmt(row["high"]),
            "" if row["ppk"] is None else f"{row['ppk']:.2f}",
        )
        for row in rows
    ]
    headers = ("step", "name", "point", "units", "n", "fails", "mean", "stdev", "min", "max", "low", "high", "ppk")
    _say(_table(headers, table_rows))


def cmd_history(args: argparse.Namespace) -> int:
    if not Path(args.db).exists():
        raise PlanError([f"no results database at {args.db}"])
    since = _utc_time(args.since, "--since")
    until = _utc_time(args.until, "--until", end_of_day=True)
    if args.stats and not args.plan:
        raise PlanError(["--stats needs --plan, because statistics are per plan"])
    with _open_store(args.db) as store:
        lot_id = lots.find(store.conn, args.lot)["id"] if args.lot is not None else None
        rows = store.find_runs(
            serial=args.serial,
            plan=args.plan,
            status=args.status,
            limit=args.limit,
            instrument=args.instrument,
            since=since,
            until=until,
            station=args.station,
            product=args.product,
            lot_id=lot_id,
        )
        summary = store.plan_summary(args.plan) if args.plan else None
        stats = store.measurement_stats(args.plan, args.serial) if args.stats else None
    if args.json:
        payload: Dict[str, Any] = {"runs": rows}
        if summary is not None:
            payload["summary"] = summary
        if stats is not None:
            payload["stats"] = stats
        _say(json.dumps(payload, indent=2))
        return EXIT_PASS
    if not rows:
        _say("No matching runs.")
    else:
        _print_runs(rows)
    if summary is not None and summary["runs"]:
        counts = summary["counts"]
        _say()
        extras = ""
        if counts["running"]:
            extras += f", {counts['running']} never finished"
        if summary["partial_runs"]:
            extras += f", {summary['partial_runs']} of only some steps (not counted)"
        _say(
            f"Plan {summary['plan']}: {_count(summary['runs'], 'run')} on {_count(summary['serials'], 'serial')}: "
            f"{counts['pass']} pass, {counts['fail']} fail, {counts['aborted']} aborted{extras}"
        )
        if summary["first_pass_yield"] is not None:
            _say(
                f"First-pass yield: {summary['first_pass_yield'] * 100:.1f}% "
                f"({summary['first_pass_units']} of {summary['units_with_verdict']} units)"
            )
    if stats:
        _say()
        _print_stats(stats)
    return EXIT_PASS


def _print_plugins(as_json: bool) -> int:
    """Every plugin found, usable or not, and why not. Exit 3 when any cannot be used."""
    drivers, simulators = plugins.load()
    found = list(drivers.values()) + list(simulators.values())
    rows = [
        {
            "name": p.name,
            "kind": "driver" if p.group == plugins.DRIVER_GROUP else "simulator",
            "package": p.package,
            "target": p.target,
            "usable": p.problem is None,
            "problem": p.problem,
        }
        for p in found
    ]
    if as_json:
        _say(json.dumps({"plugins": rows}, indent=2))
    elif not rows:
        _say(f"No driver plugins are installed (entry point groups {plugins.DRIVER_GROUP} and {plugins.SIMULATOR_GROUP}).")
    else:
        cells = [(r["name"], r["kind"], r["package"], r["target"], "ok" if r["usable"] else r["problem"]) for r in rows]
        _say(_table(("name", "kind", "package", "from", "state"), cells))
    return EXIT_ABORTED if any(not r["usable"] for r in rows) else EXIT_PASS


def cmd_drivers(args: argparse.Namespace) -> int:
    if args.plugins:
        return _print_plugins(args.json)
    if args.json:
        raise PlanError(["--json goes with --plugins"])
    plugins.load()
    names = [args.driver] if args.driver else sorted(DRIVERS)
    for name in names:
        trouble = plugins.driver_problem(name)
        if trouble:
            raise PlanError([trouble])
        driver = DRIVERS[name]
        summary = (driver.__doc__ or "").strip().splitlines()
        _say(f"{name} ({plugins.origin(name)}): {summary[0] if summary else ''}")
        for label, members in (("action", driver.actions()), ("reading", driver.readings())):
            for method, func in sorted(members.items()):
                params = [p for p in inspect.signature(func).parameters if p != "self"]
                unit = f" [{func._bench_unit or 'any'}]" if label == "reading" else ""
                _say(f"  {label:<8} {method}({', '.join(params)}){unit}")
        if args.commands:
            for key, template in sorted(driver.COMMANDS.items()):
                _say(f"  command  {key} = {template}")
    return EXIT_PASS


# Argument parsing ------------------------------------------------------


# Each module named here has ``add_commands(subparsers)``, which adds its commands and sets ``func``.
COMMAND_GROUPS: Tuple[str, ...] = (
    "benchrun.inventory",
    "benchrun.stations",
    "benchrun.audit",
    "benchrun.archive",
    "benchrun.maintenance",
    "benchrun.stats.command",
    "benchrun.product_commands",
    "benchrun.lot_commands",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="benchrun",
        description="Automated bench test runner: SCPI over TCP, JSON test plans, SQLite results, simulators included.",
        epilog="Exit codes: 0 pass or success, 1 the unit failed, 2 bad plan or arguments, 3 run aborted or a file or instrument could not be used.",
    )
    parser.add_argument("--version", action="version", version=f"benchrun {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    sim = commands.add_parser("sim", help="serve the simulated instruments until Ctrl+C")
    session.add_sim_args(sim)
    sim.add_argument(
        "--base-port",
        type=int,
        default=DEFAULT_BASE_PORT,
        help=f"first port; instruments take consecutive ports (default: {DEFAULT_BASE_PORT}; 0 means any free ports)",
    )
    sim.add_argument("--write-addresses", metavar="FILE", help="write a station file with the actual addresses")
    sim.set_defaults(func=cmd_sim)

    run = commands.add_parser("run", help="run a test plan on one unit")
    run.add_argument("plan", nargs="?", help="path to a plan JSON file; with --product, the product's plan if left out")
    run.add_argument("--serial", required=True, help="unit serial number")
    run.add_argument("--product", metavar="PART_NUMBER", help="test as this product: its plan, variables, limits and serial pattern")
    run.add_argument("--note", default="", help="one line to keep with the run, such as why it is being retested")
    run.add_argument(
        "--step",
        action="append",
        metavar="NAME",
        help="run only this step (repeatable); the result is marked and left out of yield and statistics",
    )
    run.add_argument("--set", action="append", metavar="VAR=NUMBER", help="override a plan variable (repeatable)")
    run.add_argument("--lot", metavar="NAME", help="add this run to an open lot (see: benchrun lot --help)")
    run.add_argument("--retest", action="store_true", help="with --lot, test a serial that already passed in that lot")
    session.add_unit_args(run, "start private simulators for just this run")
    run.set_defaults(func=cmd_run)

    validate = commands.add_parser("validate", help="check a plan without touching any instrument")
    validate.add_argument("plan", nargs="?", help="path to a plan JSON file; with --product, the product's plan if left out")
    validate.add_argument("--set", action="append", metavar="VAR=NUMBER", help="override a plan variable (repeatable)")
    add_station_name_arg(validate)
    validate.add_argument("--product", metavar="PART_NUMBER", help="check with this product's values, and show the limits that apply")
    validate.add_argument("--db", default=DEFAULT_DB, help=f"results database holding the station or product (default: {DEFAULT_DB})")
    validate.set_defaults(func=cmd_validate)

    probe = commands.add_parser("probe", help="connect to a plan's instruments and show their *IDN? responses")
    probe.add_argument("plan", help="path to a plan JSON file")
    session.add_address_args(probe)
    add_station_name_arg(probe)
    probe.add_argument("--db", default=DEFAULT_DB, help=f"results database holding the station (default: {DEFAULT_DB})")
    probe.set_defaults(func=cmd_probe)

    report = commands.add_parser(
        "report",
        help="write reports for recorded runs",
        epilog=describe_formats(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    report.add_argument("--db", default=DEFAULT_DB, help=f"results database (default: {DEFAULT_DB})")
    report.add_argument("--run", type=int, help="run id")
    report.add_argument("--latest", action="store_true", help="the newest run")
    report.add_argument("--serial", help="the newest run for this serial")
    report.add_argument("--plan", help="the newest run of this plan")
    report.add_argument("--all", action="store_true", help="with --serial or --plan, every matching run")
    report.add_argument(
        "--format",
        action="append",
        choices=CHOICES,
        metavar="NAME",
        help="which format (repeatable; default: all, meaning html, csv and junit; every means every format); listed below",
    )
    report.add_argument("--out", default="reports", help="output folder (default: reports)")
    report.set_defaults(func=cmd_report)

    history = commands.add_parser("history", help="list recorded runs by serial number or plan")
    history.add_argument("--db", default=DEFAULT_DB, help=f"results database (default: {DEFAULT_DB})")
    history.add_argument("--serial", help="only this unit")
    history.add_argument("--plan", help="only this plan; also shows yield")
    history.add_argument("--product", metavar="PART_NUMBER", help="only runs tested as this product")
    history.add_argument("--lot", metavar="NAME", help="only runs in this lot, by name or numeric id")
    history.add_argument("--status", choices=("pass", "fail", "aborted", "running"), help="only runs with this result")
    history.add_argument("--station", metavar="NAME", help="only runs recorded on this station")
    history.add_argument("--instrument", metavar="TEXT", help="only runs where an instrument's *IDN? response contains this")
    history.add_argument("--since", metavar="TIME", help="only runs started at or after this UTC date or time, YYYY-MM-DD[THH:MM]")
    history.add_argument("--until", metavar="TIME", help="only runs started at or before this UTC date or time")
    history.add_argument("--limit", type=_not_negative_whole, help="only the newest N runs")
    history.add_argument("--stats", action="store_true", help="per-measurement statistics; needs --plan")
    history.add_argument("--json", action="store_true", help="machine-readable output")
    history.set_defaults(func=cmd_history)

    show = commands.add_parser("show", help="print one run: its result, its steps and what failed")
    show.add_argument("--db", default=DEFAULT_DB, help=f"results database (default: {DEFAULT_DB})")
    show.add_argument("--run", type=int, help="run id")
    show.add_argument("--latest", action="store_true", help="the newest run")
    show.add_argument("--serial", help="the newest run for this serial")
    show.add_argument("--plan", help="the newest run of this plan")
    show.add_argument("--all-measurements", action="store_true", help="list every measurement, not only those that failed")
    show.add_argument("--json", action="store_true", help="everything recorded, as JSON")
    show.set_defaults(func=cmd_show)

    export = commands.add_parser("export", help="write many runs into one CSV, JSON Lines or Excel file")
    export.add_argument("--out", required=True, help="the file to write")
    export.add_argument(
        "--format",
        choices=EXPORT_FORMATS,
        default="csv",
        help="csv (one row per measurement), jsonl (one JSON object per row) or xlsx (workbook with Runs, Steps "
        "and Measurements sheets); default: csv",
    )
    export.add_argument("--force", action="store_true", help="replace the file if it already exists")
    export.add_argument("--db", default=DEFAULT_DB, help=f"results database (default: {DEFAULT_DB})")
    export.add_argument("--serial", help="only this unit")
    export.add_argument("--plan", help="only this plan")
    export.add_argument("--status", choices=("pass", "fail", "aborted", "running"), help="only runs with this result")
    export.add_argument("--since", metavar="TIME", help="only runs started at or after this UTC date or time")
    export.add_argument("--until", metavar="TIME", help="only runs started at or before this UTC date or time")
    export.set_defaults(func=cmd_export)

    init = commands.add_parser("init", help="write a starter plan to edit")
    init.add_argument("plan", help="path of the plan file to create")
    init.add_argument("--force", action="store_true", help="replace the file if it already exists")
    init.set_defaults(func=cmd_init)

    drivers = commands.add_parser("drivers", help="list drivers and what plans can call on them")
    drivers.add_argument("driver", nargs="?", help="just this driver")
    drivers.add_argument("--commands", action="store_true", help="also show SCPI templates plans can override")
    drivers.add_argument(
        "--plugins", action="store_true", help="list driver plugins from installed packages, and why any cannot be used (exit 3)"
    )
    drivers.add_argument("--json", action="store_true", help="with --plugins, machine-readable output")
    drivers.set_defaults(func=cmd_drivers)

    raw = commands.add_parser("scpi", help="send raw SCPI commands to one instrument")
    raw.add_argument("address", help="host:port")
    raw.add_argument("commands", nargs="+", help="commands; those containing ? read a response")
    raw.add_argument("--timeout", type=_positive_number, default=5.0, help="seconds to wait for a response")
    raw.set_defaults(func=cmd_scpi)

    for module_name in COMMAND_GROUPS:
        importlib.import_module(module_name).add_commands(commands)
    return parser


def _unusable_text(args: argparse.Namespace) -> Optional[str]:
    """Name an argument that holds bytes this terminal's encoding could not decode."""
    for name, value in vars(args).items():
        for text in value if isinstance(value, list) else [value]:
            if isinstance(text, str) and any(0xD800 <= ord(ch) <= 0xDFFF for ch in text):
                return name
    return None


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    bad = _unusable_text(args)
    if bad:
        _complain("Cannot continue:")
        _complain(f"  the value of {bad} is not valid text here; use a UTF-8 terminal or plain ASCII")
        return EXIT_USAGE
    try:
        return args.func(args)
    except PlanError as exc:
        _complain("Cannot continue:")
        for problem in exc.problems:
            _complain(f"  {problem}")
        return EXIT_USAGE
    except BenchrunError as exc:
        _complain(f"error: {exc}")
        return EXIT_ABORTED
    except (sqlite3.Error, OSError) as exc:
        _complain(f"error: {exc}")
        return EXIT_ABORTED
    except (MemoryError, RecursionError):
        _complain("error: the input is too large or too deeply nested to handle")
        return EXIT_ABORTED
    except KeyboardInterrupt:
        return EXIT_ABORTED
    except Exception:  # Python's own exit code 1 would read as a failed unit.
        traceback.print_exc()
        _complain("error: internal error in benchrun; this says nothing about the unit")
        return EXIT_ABORTED
