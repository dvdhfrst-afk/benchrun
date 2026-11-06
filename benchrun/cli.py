"""The command line: ``python -m benchrun <command>``."""

from __future__ import annotations

import argparse
import inspect
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from benchrun import __version__
from benchrun.drivers import DRIVERS
from benchrun.errors import BenchrunError, InstrumentFault, PlanError
from benchrun.plan import Plan, apply_addresses, load_addresses, load_plan, match_station
from benchrun.reports import FORMATS, write_reports
from benchrun.runner import RUN_ABORTED, RUN_FAIL, RUN_PASS, Runner, default_transport
from benchrun.sim.bench import DEFAULT_BASE_PORT, PORT_ORDER, SimBench, parse_fault
from benchrun.sim.model import DUT_VARIANTS
from benchrun.store import Store

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_ABORTED = 3

DEFAULT_DB = "benchrun.sqlite"
_EXIT_BY_STATUS = {RUN_PASS: EXIT_PASS, RUN_FAIL: EXIT_FAIL, RUN_ABORTED: EXIT_ABORTED}


def _say(text: str = "") -> None:
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        # An instrument's response must not be able to stop a run at the console.
        print(text.encode("ascii", "backslashreplace").decode("ascii"), flush=True)


def _parse_pairs(items: Optional[Iterable[str]], flag: str) -> Dict[str, str]:
    pairs: Dict[str, str] = {}
    for item in items or []:
        name, sep, value = item.partition("=")
        if not sep or not name or not value:
            raise PlanError([f"{flag} expects name=value, got {item!r}"])
        pairs[name] = value
    return pairs


def _parse_variables(items: Optional[Iterable[str]]) -> Dict[str, float]:
    variables = {}
    for name, value in _parse_pairs(items, "--set").items():
        try:
            variables[name] = float(value)
        except ValueError:
            raise PlanError([f"--set {name}: {value!r} is not a number"]) from None
    return variables


def _check_serial(serial: str) -> str:
    cleaned = serial.strip()
    if not cleaned or len(cleaned) > 128 or any(ord(ch) < 32 for ch in cleaned):
        raise PlanError([f"serial number {serial!r} must be 1 to 128 printable characters"])
    return cleaned


def _table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    text_rows = [["" if cell is None else str(cell) for cell in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in text_rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip()]
    lines.append("  ".join("-" * w for w in widths))
    for row in text_rows:
        lines.append("  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip())
    return "\n".join(lines)


def _fmt(value: Optional[float]) -> str:
    return "" if value is None else f"{value:.6g}"


# Commands --------------------------------------------------------------


def cmd_sim(args: argparse.Namespace) -> int:
    faults: Dict[str, Any] = {}
    for text in args.fault or []:
        faults.update(parse_fault(text))
    bench = SimBench(dut=args.dut, seed=args.seed, noise=args.noise, base_port=args.base_port, faults=faults)
    try:
        bench.start()
    except OSError as exc:
        _say(f"cannot start simulators: {exc}")
        _say("Another process may hold those ports. Try --base-port with a different number, or 0 for any free port.")
        return EXIT_USAGE
    try:
        _say(f"benchrun simulators on 127.0.0.1 (simulated unit: {args.dut}, seed: {args.seed}, noise: {args.noise:g})")
        for kind in PORT_ORDER:
            device = bench.devices[kind]
            _say(f"  {kind:<7} {bench.servers[kind].address:<20} {device.idn}")
        if args.write_addresses:
            payload = {"addresses": bench.addresses}
            temporary = f"{args.write_addresses}.tmp"
            Path(temporary).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, args.write_addresses)
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


def _load_for_bench(args: argparse.Namespace, variables: Optional[Dict[str, float]] = None) -> Plan:
    plan = load_plan(args.plan, variables)
    addresses: Dict[str, str] = {}
    if args.station:
        addresses, unused = match_station(plan, load_addresses(args.station))
        if unused:
            _say(f"Note: station entries not used by this plan: {', '.join(sorted(unused))}")
    addresses.update(_parse_pairs(args.address, "--address"))
    apply_addresses(plan, addresses)
    return plan


def cmd_run(args: argparse.Namespace) -> int:
    serial = _check_serial(args.serial)
    variables = _parse_variables(args.set)
    bench: Optional[SimBench] = None
    if args.sim:
        faults: Dict[str, Any] = {}
        for text in args.fault or []:
            faults.update(parse_fault(text))
        plan = load_plan(args.plan, variables)
        bench = SimBench(dut=args.dut, seed=args.seed, noise=args.noise, faults=faults).start()
        try:
            apply_addresses(plan, bench.addresses_for((s.name, s.driver) for s in plan.instruments.values()))
        except PlanError:
            bench.stop()
            raise
    else:
        if args.fault or args.dut != "good":
            raise PlanError(["--dut and --fault only apply with --sim; otherwise set them on the sim command"])
        plan = _load_for_bench(args, variables)

    log_handle = open(args.scpi_log, "w", encoding="utf-8") if args.scpi_log else None
    try:
        with Store(args.db) as store:
            result = Runner(plan, store, serial, operator=args.operator, console=_say, scpi_log=log_handle).run()
            detail = store.run_detail(result.run_id)
        if args.report_dir:
            for path in write_reports(detail, args.report_dir):
                _say(f"Report: {path}")
    finally:
        if log_handle is not None:
            log_handle.close()
        if bench is not None:
            bench.stop()
    return _EXIT_BY_STATUS[result.status]


def cmd_validate(args: argparse.Namespace) -> int:
    plan = load_plan(args.plan, _parse_variables(args.set))
    points = sum(len(step.points()) * len(step.measures) + len(step.summary) for step in plan.steps)
    _say(f"{args.plan}: OK")
    _say(f"  {plan.name} {plan.version}".rstrip())
    _say(f"  {len(plan.instruments)} instruments, {len(plan.steps)} steps, {points} measurements per run")
    return EXIT_PASS


def cmd_probe(args: argparse.Namespace) -> int:
    """Connect to each instrument in a plan without changing its settings."""
    plan = _load_for_bench(args)
    rows = []
    problems = 0
    for spec in plan.instruments.values():
        driver = DRIVERS[spec.driver](default_transport(spec.address, spec.timeout_s), spec.name, spec.commands)
        state = "ok"
        idn = ""
        try:
            driver.connect()
            idn = driver.identify()
            driver.check_errors()
            if spec.expect_idn:
                if not re.search(spec.expect_idn, idn):
                    state = f"*IDN? does not match {spec.expect_idn!r}"
        except InstrumentFault as exc:
            state = str(exc)
        finally:
            driver.close()
        problems += state != "ok"
        rows.append((spec.name, spec.driver, spec.address, idn, state))
    _say(_table(("name", "driver", "address", "*IDN?", "state"), rows))
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
    if args.all:
        return [row["id"] for row in reversed(rows)]
    return [rows[0]["id"]]


def cmd_report(args: argparse.Namespace) -> int:
    if not Path(args.db).exists():
        raise PlanError([f"no results database at {args.db}"])
    formats = FORMATS if args.format == "all" else (args.format,)
    with Store(args.db) as store:
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


def _print_runs(rows: List[Dict[str, Any]]) -> None:
    table_rows = [
        (
            row["id"],
            row["started_at"][:19].replace("T", " "),
            row["serial"],
            f"{row['plan_name']} {row['plan_version']}".rstrip(),
            row["status"].upper(),
            row["passed"],
            row["failed"],
            row["errors"],
            row["reason"] or "",
        )
        for row in rows
    ]
    headers = ("run", "started (UTC)", "serial", "plan", "status", "pass", "fail", "error", "reason")
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
            "" if row["cpk"] is None else f"{row['cpk']:.2f}",
        )
        for row in rows
    ]
    headers = ("step", "name", "point", "units", "n", "fails", "mean", "stdev", "min", "max", "low", "high", "cpk")
    _say(_table(headers, table_rows))


def cmd_history(args: argparse.Namespace) -> int:
    if not Path(args.db).exists():
        raise PlanError([f"no results database at {args.db}"])
    with Store(args.db) as store:
        rows = store.find_runs(serial=args.serial, plan=args.plan, status=args.status, limit=args.limit)
        if args.stats and not args.plan:
            raise PlanError(["--stats needs --plan, because statistics are per plan"])
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
        _say(
            f"Plan {summary['plan']}: {summary['runs']} runs on {summary['serials']} serials: "
            f"{counts['pass']} pass, {counts['fail']} fail, {counts['aborted']} aborted"
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


def cmd_drivers(args: argparse.Namespace) -> int:
    names = [args.driver] if args.driver else sorted(DRIVERS)
    for name in names:
        if name not in DRIVERS:
            raise PlanError([f"unknown driver {name!r}; available: {', '.join(sorted(DRIVERS))}"])
        driver = DRIVERS[name]
        _say(f"{name}: {driver.__doc__.strip().splitlines()[0] if driver.__doc__ else ''}")
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


def _add_address_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--station", help='JSON file like {"addresses": {"dmm": "192.0.2.10:5025"}}; keys are instrument or driver names')
    parser.add_argument(
        "--address", action="append", metavar="NAME=HOST:PORT", help="set one instrument's address (repeatable)"
    )


def _add_sim_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dut", default="good", choices=sorted(DUT_VARIANTS), help="simulated unit (default: good)")
    parser.add_argument("--seed", type=int, default=1, help="noise seed (default: 1)")
    parser.add_argument("--noise", type=float, default=1.0, help="noise scale, 0 for none (default: 1)")
    parser.add_argument(
        "--fault",
        action="append",
        metavar="INSTRUMENT:KIND[:AFTER]",
        help="inject a fault after AFTER good readings, such as dmm:drop:5; "
        "kinds: drop, hang, error, garbage, overload (repeatable)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="benchrun", description="Automated bench test runner.")
    parser.add_argument("--version", action="version", version=f"benchrun {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    sim = commands.add_parser("sim", help="serve the simulated instruments until Ctrl+C")
    _add_sim_args(sim)
    sim.add_argument(
        "--base-port",
        type=int,
        default=DEFAULT_BASE_PORT,
        help=f"first port; instruments take consecutive ports (default: {DEFAULT_BASE_PORT}; 0 means any free ports)",
    )
    sim.add_argument("--write-addresses", metavar="FILE", help="write a station file with the actual addresses")
    sim.set_defaults(func=cmd_sim)

    run = commands.add_parser("run", help="run a test plan on one unit")
    run.add_argument("plan", help="path to a plan JSON file")
    run.add_argument("--serial", required=True, help="unit serial number")
    run.add_argument("--operator", default="", help="operator label to record")
    run.add_argument("--db", default=DEFAULT_DB, help=f"results database (default: {DEFAULT_DB})")
    run.add_argument("--set", action="append", metavar="VAR=NUMBER", help="override a plan variable (repeatable)")
    run.add_argument("--report-dir", help="also write HTML, CSV and JUnit XML reports here")
    run.add_argument("--scpi-log", metavar="FILE", help="write all SCPI traffic to this file")
    _add_address_args(run)
    run.add_argument("--sim", action="store_true", help="start private simulators for just this run")
    _add_sim_args(run)
    run.set_defaults(func=cmd_run)

    validate = commands.add_parser("validate", help="check a plan without touching any instrument")
    validate.add_argument("plan")
    validate.add_argument("--set", action="append", metavar="VAR=NUMBER")
    validate.set_defaults(func=cmd_validate)

    probe = commands.add_parser("probe", help="connect to a plan's instruments and show their *IDN? responses")
    probe.add_argument("plan")
    _add_address_args(probe)
    probe.set_defaults(func=cmd_probe)

    report = commands.add_parser("report", help="write reports for recorded runs")
    report.add_argument("--db", default=DEFAULT_DB)
    report.add_argument("--run", type=int, help="run id")
    report.add_argument("--latest", action="store_true", help="the newest run")
    report.add_argument("--serial", help="the newest run for this serial")
    report.add_argument("--plan", help="the newest run of this plan")
    report.add_argument("--all", action="store_true", help="with --serial or --plan, every matching run")
    report.add_argument("--format", default="all", choices=FORMATS + ("all",))
    report.add_argument("--out", default="reports", help="output folder (default: reports)")
    report.set_defaults(func=cmd_report)

    history = commands.add_parser("history", help="list recorded runs by serial number or plan")
    history.add_argument("--db", default=DEFAULT_DB)
    history.add_argument("--serial")
    history.add_argument("--plan")
    history.add_argument("--status", choices=("pass", "fail", "aborted", "running"))
    history.add_argument("--limit", type=int)
    history.add_argument("--stats", action="store_true", help="per-measurement statistics; needs --plan")
    history.add_argument("--json", action="store_true", help="machine-readable output")
    history.set_defaults(func=cmd_history)

    drivers = commands.add_parser("drivers", help="list drivers and what plans can call on them")
    drivers.add_argument("driver", nargs="?")
    drivers.add_argument("--commands", action="store_true", help="also show SCPI templates plans can override")
    drivers.set_defaults(func=cmd_drivers)

    raw = commands.add_parser("scpi", help="send raw SCPI commands to one instrument")
    raw.add_argument("address", help="host:port")
    raw.add_argument("commands", nargs="+", help="commands; those containing ? read a response")
    raw.add_argument("--timeout", type=float, default=5.0, help="seconds to wait for a response")
    raw.set_defaults(func=cmd_scpi)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except PlanError as exc:
        print("Cannot continue:", file=sys.stderr)
        for problem in exc.problems:
            print(f"  {problem}", file=sys.stderr)
        return EXIT_USAGE
    except BenchrunError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ABORTED
    except (sqlite3.Error, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ABORTED
    except KeyboardInterrupt:
        return EXIT_ABORTED
