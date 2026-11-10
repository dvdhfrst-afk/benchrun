"""The command line: ``python -m benchrun <command>``."""

from __future__ import annotations

import argparse
import contextlib
import importlib
import inspect
import json
import os
import re
import signal
import sqlite3
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from benchrun import __version__
from benchrun.drivers import DRIVERS
from benchrun.errors import BenchrunError, InstrumentFault, OperatorUnavailable, PlanError
from benchrun.files import atomic_write_text
from benchrun.plan import Plan, apply_addresses, load_addresses, load_plan, match_station, safety_notes, select_steps
from benchrun.reports import FORMATS, write_reports
from benchrun.reports.csv_report import render_many
from benchrun.runner import RUN_ABORTED, RUN_FAIL, RUN_PASS, Runner, default_transport
from benchrun.sim.bench import DEFAULT_BASE_PORT, PORT_ORDER, SimBench, parse_fault
from benchrun.sim.model import DUT_VARIANTS
from benchrun.stations import add_station_name_arg, read_identity, use_station
from benchrun.store import Store
from benchrun.console import (
    say as _say,
    complain as _complain,
    parse_pairs as _parse_pairs,
    parse_variables as _parse_variables,
    check_serial as _check_serial,
    check_label as _check_label,
    utc_time as _utc_time,
    positive_number as _positive_number,
    not_negative_number as _not_negative_number,
    not_negative_whole as _not_negative_whole,
    open_store as _open_store,
    count as _count,
    table as _table,
    fmt as _fmt,
    DEFAULT_DB,
    EXIT_ABORTED,
    EXIT_FAIL,
    EXIT_PASS,
    EXIT_USAGE,
)

_EXIT_BY_STATUS = {RUN_PASS: EXIT_PASS, RUN_FAIL: EXIT_FAIL, RUN_ABORTED: EXIT_ABORTED}

_WINDOW_GOING_EVENTS = (2, 5, 6)  # Console closed, user logging off, system shutting down.
_WINDOW_WAIT_S = 4.5


def _hold_window_open(finished: threading.Event) -> Callable[[], None]:
    """On Windows, closing the console reaches no signal handler, and the process ends when this returns.

    So interrupt the run the way Ctrl+C does, and hold the process open while it
    switches outputs off and records the result. Windows allows about 5 seconds.
    """
    if os.name != "nt":
        return lambda: None
    import _thread
    import ctypes
    from ctypes import wintypes

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)  # type: ignore[attr-defined]
    def on_console_event(event: int) -> bool:
        if event not in _WINDOW_GOING_EVENTS:
            return False  # Ctrl+C and Ctrl+Break go on to Python's own handling.
        _thread.interrupt_main()
        finished.wait(_WINDOW_WAIT_S)
        return True

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.SetConsoleCtrlHandler(on_console_event, True)
    return lambda: kernel32.SetConsoleCtrlHandler(on_console_event, False)


@contextlib.contextmanager
def _stop_requests_interrupt(past_saving: Callable[[], bool]) -> Iterator[None]:
    """A service stop or a closed window ends a run the way Ctrl+C does: outputs go off first.

    Once ``past_saving`` says the bench is safe, a stop is held off until the result
    and the reports are written. Stopping then would only lose them.
    """

    def interrupt(signum: int, frame: Any) -> None:
        if not past_saving():
            raise KeyboardInterrupt()

    previous = {}
    for name in ("SIGINT", "SIGTERM", "SIGBREAK", "SIGHUP"):
        number = getattr(signal, name, None)
        if number is not None:
            with contextlib.suppress(ValueError, OSError):
                previous[number] = signal.signal(number, interrupt)
    finished = threading.Event()
    release_window = _hold_window_open(finished)
    try:
        yield
    finally:
        finished.set()
        release_window()
        for number, handler in previous.items():
            signal.signal(number, handler)


def _read_line() -> bool:
    """Wait for one line on standard input. False if it has ended, or there is none.

    Reads the file descriptor a byte at a time. A buffered read would take later
    answers from a pipe with it, and would hold a lock that can stop Python exiting.
    """
    try:
        descriptor = sys.stdin.fileno()
        got_any = False
        while True:
            byte = os.read(descriptor, 1)
            if not byte:
                return got_any
            if byte == b"\n":
                return True
            got_any = True
    except (AttributeError, OSError, ValueError):
        return False


def _ask_operator(text: str) -> None:
    """Wait in another thread. A read on the main thread would hold off every stop request, with outputs on."""
    _say(f"OPERATOR: {text}")
    _say("Press Enter to go on, or Ctrl+C to stop the run.")
    answers = []
    reader = threading.Thread(target=lambda: answers.append(_read_line()), daemon=True)
    reader.start()
    while reader.is_alive():
        reader.join(0.1)
    if not answers or not answers[0]:
        time.sleep(0.2)  # On Windows, Ctrl+C also ends the read. Give it the moment it needs to arrive.
        raise OperatorUnavailable(f"the plan asks the operator to {text!r}, but no one can answer here; use --auto-confirm in a script")


def _confirm_automatically(text: str) -> None:
    _say(f"OPERATOR: {text} (continued automatically)")


# Commands --------------------------------------------------------------


def cmd_sim(args: argparse.Namespace) -> int:
    faults: Dict[str, Any] = {}
    for text in args.fault or []:
        faults.update(parse_fault(text))
    if not 0 <= args.base_port <= 65536 - len(PORT_ORDER):
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
        for kind in PORT_ORDER:
            device = bench.devices[kind]
            _say(f"  {kind:<7} {bench.servers[kind].address:<20} {device.idn}")
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


def _load_for_bench(args: argparse.Namespace, variables: Optional[Dict[str, float]] = None) -> Tuple[Plan, str, Dict[str, str]]:
    """The plan with its addresses, the station it runs on, and each instrument's asset tag."""
    plan = load_plan(args.plan, variables)
    if args.station_name:
        if args.station or args.address:
            raise PlanError(["--station-name gives every address; leave out --station and --address"])
        station, asset_tags = use_station(args.db, args.station_name, plan)
        return plan, station, asset_tags
    addresses: Dict[str, str] = {}
    if args.station:
        addresses, unused = match_station(plan, load_addresses(args.station))
        if unused:
            _say(f"Note: station entries not used by this plan: {', '.join(sorted(unused))}")
    addresses.update(_parse_pairs(args.address, "--address"))
    apply_addresses(plan, addresses)
    return plan, "", {}


def cmd_run(args: argparse.Namespace) -> int:
    serial = _check_serial(args.serial)
    operator = _check_label(args.operator, "--operator")
    note = _check_label(args.note, "--note")
    variables = _parse_variables(args.set)
    bench: Optional[SimBench] = None
    station, asset_tags = "", {}
    if args.sim and args.station_name:
        raise PlanError(["--station-name and --sim cannot be combined: the simulators are not on any station"])
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
        plan, station, asset_tags = _load_for_bench(args, variables)

    log_handle = None
    try:
        only_steps = select_steps(plan, args.step) if args.step else None
        # Added to, never replaced: an earlier run's traffic may be evidence.
        log_handle = open(args.scpi_log, "a", encoding="utf-8") if args.scpi_log else None
        with _open_store(args.db) as store:
            runner = Runner(
                plan,
                store,
                serial,
                operator=operator,
                console=_say,
                scpi_log=log_handle,
                note=note,
                only_steps=only_steps,
                prompt=_confirm_automatically if args.auto_confirm else _ask_operator,
                verbose=args.verbose,
                station=station,
                asset_tags=asset_tags,
            )
            with _stop_requests_interrupt(lambda: runner.past_saving):
                result = runner.run()
                detail = None
                if result.database_problem is None:
                    detail = store.run_detail(result.run_id)
                if result.database_problem is not None:
                    _complain(
                        f"error: results database {args.db} failed during the run: {result.database_problem}. "
                        "Whatever reached it before that is still there. No report was written."
                    )
                elif args.report_dir:
                    try:
                        for path in write_reports(detail, args.report_dir):
                            _say(f"Report: {path}")
                    except OSError as exc:
                        _complain(f"error: cannot write reports to {args.report_dir}: {exc}")
                        _complain(f"The result of run {result.run_id} is safe in {args.db}; write it later with the report command.")
                        return EXIT_ABORTED
    finally:
        if log_handle is not None:
            log_handle.close()
        if bench is not None:
            bench.stop()
    return _EXIT_BY_STATUS[result.status]


def cmd_validate(args: argparse.Namespace) -> int:
    plan = load_plan(args.plan, _parse_variables(args.set))
    station, asset_tags = use_station(args.db, args.station_name, plan) if args.station_name else ("", {})
    points = sum(len(step.points()) * len(step.measures) + len(step.summary) for step in plan.steps)
    _say(f"{args.plan}: OK")
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
    return EXIT_PASS


def cmd_probe(args: argparse.Namespace) -> int:
    """Connect to each instrument in a plan without changing its settings."""
    plan, station, asset_tags = _load_for_bench(args)
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
    formats = FORMATS if args.format == "all" else (args.format,)
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
    for label, value in (("Station", detail["station"]), ("Operator", detail["operator"]), ("Note", detail["note"])):
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
    """Every matching run in one CSV file, for a spreadsheet or a script."""
    if not Path(args.db).exists():
        raise PlanError([f"no results database at {args.db}"])
    if Path(args.out).exists() and not args.force:
        raise PlanError([f"{args.out} already exists; choose another name or add --force to replace it"])
    since = _utc_time(args.since, "--since")
    until = _utc_time(args.until, "--until", end_of_day=True)
    with _open_store(args.db) as store:
        rows = store.find_runs(serial=args.serial, plan=args.plan, status=args.status, since=since, until=until)
        details = [store.run_detail(row["id"]) for row in reversed(rows)]
    if not details:
        _complain("No matching runs, so nothing was written.")
        return EXIT_USAGE
    atomic_write_text(args.out, render_many(details))
    _say(f"Wrote {len(details)} runs to {args.out}")
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
    headers = ("run", "started (UTC)", "serial", "plan", "status", "pass", "fail", "error", "reason or note")
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
    since = _utc_time(args.since, "--since")
    until = _utc_time(args.until, "--until", end_of_day=True)
    if args.stats and not args.plan:
        raise PlanError(["--stats needs --plan, because statistics are per plan"])
    with _open_store(args.db) as store:
        rows = store.find_runs(
            serial=args.serial,
            plan=args.plan,
            status=args.status,
            limit=args.limit,
            instrument=args.instrument,
            since=since,
            until=until,
            station=args.station,
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
    parser.add_argument("--noise", type=_not_negative_number, default=1.0, help="noise scale, 0 for none (default: 1)")
    parser.add_argument(
        "--fault",
        action="append",
        metavar="INSTRUMENT:KIND[:AFTER]",
        help="inject a fault after AFTER good readings, such as dmm:drop:5; "
        "kinds: drop, hang, error, garbage, overload (repeatable)",
    )


# Each module named here has ``add_commands(subparsers)``, which adds its commands and sets ``func``.
COMMAND_GROUPS: Tuple[str, ...] = ("benchrun.inventory", "benchrun.stations", "benchrun.audit")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="benchrun",
        description="Automated bench test runner: SCPI over TCP, JSON test plans, SQLite results, simulators included.",
        epilog="Exit codes: 0 pass or success, 1 the unit failed, 2 bad plan or arguments, 3 run aborted or a file or instrument could not be used.",
    )
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
    run.add_argument("--note", default="", help="one line to keep with the run, such as why it is being retested")
    run.add_argument(
        "--step",
        action="append",
        metavar="NAME",
        help="run only this step (repeatable); the result is marked and left out of yield and statistics",
    )
    run.add_argument("--verbose", "-v", action="store_true", help="print every measurement as it is taken")
    run.add_argument("--auto-confirm", action="store_true", help="carry on past operator prompts without waiting")
    run.add_argument("--db", default=DEFAULT_DB, help=f"results database (default: {DEFAULT_DB})")
    run.add_argument("--set", action="append", metavar="VAR=NUMBER", help="override a plan variable (repeatable)")
    run.add_argument("--report-dir", help="also write HTML, CSV and JUnit XML reports here")
    run.add_argument("--scpi-log", metavar="FILE", help="add all SCPI traffic to this file")
    _add_address_args(run)
    add_station_name_arg(run)
    run.add_argument("--sim", action="store_true", help="start private simulators for just this run")
    _add_sim_args(run)
    run.set_defaults(func=cmd_run)

    validate = commands.add_parser("validate", help="check a plan without touching any instrument")
    validate.add_argument("plan", help="path to a plan JSON file")
    validate.add_argument("--set", action="append", metavar="VAR=NUMBER", help="override a plan variable (repeatable)")
    add_station_name_arg(validate)
    validate.add_argument("--db", default=DEFAULT_DB, help=f"results database holding the station (default: {DEFAULT_DB})")
    validate.set_defaults(func=cmd_validate)

    probe = commands.add_parser("probe", help="connect to a plan's instruments and show their *IDN? responses")
    probe.add_argument("plan", help="path to a plan JSON file")
    _add_address_args(probe)
    add_station_name_arg(probe)
    probe.add_argument("--db", default=DEFAULT_DB, help=f"results database holding the station (default: {DEFAULT_DB})")
    probe.set_defaults(func=cmd_probe)

    report = commands.add_parser("report", help="write reports for recorded runs")
    report.add_argument("--db", default=DEFAULT_DB, help=f"results database (default: {DEFAULT_DB})")
    report.add_argument("--run", type=int, help="run id")
    report.add_argument("--latest", action="store_true", help="the newest run")
    report.add_argument("--serial", help="the newest run for this serial")
    report.add_argument("--plan", help="the newest run of this plan")
    report.add_argument("--all", action="store_true", help="with --serial or --plan, every matching run")
    report.add_argument("--format", default="all", choices=FORMATS + ("all",), help="which format (default: all)")
    report.add_argument("--out", default="reports", help="output folder (default: reports)")
    report.set_defaults(func=cmd_report)

    history = commands.add_parser("history", help="list recorded runs by serial number or plan")
    history.add_argument("--db", default=DEFAULT_DB, help=f"results database (default: {DEFAULT_DB})")
    history.add_argument("--serial", help="only this unit")
    history.add_argument("--plan", help="only this plan; also shows yield")
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

    export = commands.add_parser("export", help="write many runs into one CSV file")
    export.add_argument("--out", required=True, help="the CSV file to write")
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
