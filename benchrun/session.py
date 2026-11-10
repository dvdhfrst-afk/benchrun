"""Testing units on the bench: what ``run`` and every unit of ``lot run`` share.

A stop request (Ctrl+C, a service stop, a closed console window) ends a unit the way
it always has: outputs go off first, then the result is saved, then reports are
written. Operator prompts wait in another thread so a stop is never held off with an
output on. The plan, its addresses and any private simulators are set up once and
used for every unit tested in the same command.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import IO, Any, Callable, Dict, Iterator, List, Optional, Tuple

from benchrun.console import (
    DEFAULT_DB,
    EXIT_ABORTED,
    EXIT_FAIL,
    EXIT_PASS,
    check_label,
    complain,
    not_negative_number,
    parse_pairs,
    say,
)
from benchrun.errors import OperatorUnavailable, PlanError
from benchrun.plan import Plan, apply_addresses, load_addresses, load_plan, match_station
from benchrun.product_commands import load_product
from benchrun.products import Product
from benchrun.reports import CHOICES, resolve_formats, write_reports
from benchrun.runner import RUN_ABORTED, RUN_FAIL, RUN_PASS, Runner, RunResult
from benchrun.sim.bench import SimBench, parse_fault
from benchrun.sim.model import DUT_VARIANTS
from benchrun.stations import add_station_name_arg, use_station
from benchrun.store import Store

EXIT_BY_STATUS = {RUN_PASS: EXIT_PASS, RUN_FAIL: EXIT_FAIL, RUN_ABORTED: EXIT_ABORTED}

_WINDOW_GOING_EVENTS = (2, 5, 6)  # Console closed, user logging off, system shutting down.
_WINDOW_WAIT_S = 4.5
MAX_LINE_BYTES = 4096  # Longer typed lines are cut here; no serial or answer is that long.


# Stop requests ---------------------------------------------------------


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


class StopRequests:
    """Whether anyone asked to stop while a unit was on the bench, including a stop held off until its result was saved."""

    def __init__(self) -> None:
        self.requested = False


@contextlib.contextmanager
def stop_requests_interrupt(past_saving: Callable[[], bool]) -> Iterator[StopRequests]:
    """A service stop or a closed window ends a run the way Ctrl+C does: outputs go off first.

    Once ``past_saving`` says the bench is safe, a stop is held off until the result
    and the reports are written. Stopping then would only lose them.
    """
    stops = StopRequests()

    def interrupt(signum: int, frame: Any) -> None:
        stops.requested = True
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
        yield stops
    finally:
        finished.set()
        release_window()
        for number, handler in previous.items():
            signal.signal(number, handler)


# Reading the terminal --------------------------------------------------


def _read_line() -> Optional[bytes]:
    """Wait for one line on standard input, without its line end. None if input has ended, or there is none.

    Reads the file descriptor a byte at a time. A buffered read would take later
    answers from a pipe with it, and would hold a lock that can stop Python exiting.
    """
    try:
        descriptor = sys.stdin.fileno()
        line = bytearray()
        got_any = False
        while True:
            byte = os.read(descriptor, 1)
            if not byte:
                return bytes(line) if got_any else None
            if byte == b"\n":
                return bytes(line).rstrip(b"\r")
            got_any = True
            if len(line) < MAX_LINE_BYTES:
                line += byte
    except (AttributeError, OSError, ValueError):
        return None


def read_answer() -> Optional[bytes]:
    """One line from the terminal, read in another thread. A read on the main thread would hold off every stop request."""
    answers: List[Optional[bytes]] = []
    reader = threading.Thread(target=lambda: answers.append(_read_line()), daemon=True)
    reader.start()
    while reader.is_alive():
        reader.join(0.1)
    answer = answers[0] if answers else None
    if answer is None:
        time.sleep(0.2)  # On Windows, Ctrl+C also ends the read. Give it the moment it needs to arrive.
    return answer


def ask_operator(text: str) -> None:
    say(f"OPERATOR: {text}")
    say("Press Enter to go on, or Ctrl+C to stop the run.")
    if read_answer() is None:
        raise OperatorUnavailable(f"the plan asks the operator to {text!r}, but no one can answer here; use --auto-confirm in a script")


def confirm_automatically(text: str) -> None:
    say(f"OPERATOR: {text} (continued automatically)")


# The plan and the bench ------------------------------------------------


def product_and_plan_path(args: argparse.Namespace) -> Tuple[Optional[Product], str]:
    """The product a command names, if any, and the plan to load: the one given, else the product's."""
    product = load_product(args.db, args.product) if args.product else None
    if args.plan:
        return product, args.plan
    if product is None:
        raise PlanError(["give a plan file, or --product PART_NUMBER to use the plan that part number is tested with"])
    return product, product.plan_file


def load(path: str, variables: Optional[Dict[str, float]], product: Optional[Product]) -> Plan:
    return product.load_plan(path, variables) if product is not None else load_plan(path, variables)


def load_for_bench(
    args: argparse.Namespace,
    variables: Optional[Dict[str, float]] = None,
    product: Optional[Product] = None,
    plan_path: Optional[str] = None,
) -> Tuple[Plan, str, Dict[str, str]]:
    """The plan with its addresses, the station it runs on, and each instrument's asset tag."""
    plan = load(plan_path or args.plan, variables, product)
    if args.station_name:
        if args.station or args.address:
            raise PlanError(["--station-name gives every address; leave out --station and --address"])
        station, asset_tags = use_station(args.db, args.station_name, plan)
        return plan, station, asset_tags
    addresses: Dict[str, str] = {}
    if args.station:
        addresses, unused = match_station(plan, load_addresses(args.station))
        if unused:
            say(f"Note: station entries not used by this plan: {', '.join(sorted(unused))}")
    addresses.update(parse_pairs(args.address, "--address"))
    apply_addresses(plan, addresses)
    return plan, "", {}


@dataclass
class Bench:
    """A plan with every address filled in, and the station it is on."""

    plan: Plan
    station: str = ""
    asset_tags: Dict[str, str] = field(default_factory=dict)


@contextlib.contextmanager
def bench_for(
    args: argparse.Namespace, plan_path: str, product: Optional[Product], variables: Optional[Dict[str, float]]
) -> Iterator[Bench]:
    """Addresses from --sim, --station-name, --station or --address. Private simulators run until the block ends."""
    if args.sim and args.station_name:
        raise PlanError(["--station-name and --sim cannot be combined: the simulators are not on any station"])
    if not args.sim:
        if args.fault or args.dut != "good":
            raise PlanError(["--dut and --fault only apply with --sim; otherwise set them on the sim command"])
        plan, station, asset_tags = load_for_bench(args, variables, product, plan_path)
        yield Bench(plan, station, asset_tags)
        return
    faults: Dict[str, Any] = {}
    for text in args.fault or []:
        faults.update(parse_fault(text))
    plan = load(plan_path, variables, product)
    simulators = SimBench(dut=args.dut, seed=args.seed, noise=args.noise, faults=faults).start()
    try:
        apply_addresses(plan, simulators.addresses_for((s.name, s.driver) for s in plan.instruments.values()))
        yield Bench(plan)
    finally:
        simulators.stop()


@contextlib.contextmanager
def scpi_log(path: Optional[str]) -> Iterator[Optional[IO[str]]]:
    """Added to, never replaced: an earlier run's traffic may be evidence."""
    if not path:
        yield None
        return
    with open(path, "a", encoding="utf-8") as handle:
        yield handle


# One unit --------------------------------------------------------------


@dataclass
class UnitOptions:
    """What stays the same for every unit a command tests."""

    db: str
    operator: str = ""
    note: str = ""
    verbose: bool = False
    auto_confirm: bool = False
    report_dir: Optional[str] = None
    report_formats: Tuple[str, ...] = ()
    product: str = ""
    lot_id: Optional[int] = None
    only_steps: Optional[List[str]] = None


def unit_options(args: argparse.Namespace) -> UnitOptions:
    """The options ``run`` and ``lot run`` share, checked before any instrument is touched."""
    if args.report_format and not args.report_dir:
        raise PlanError(["--report-format needs --report-dir to say where the reports go"])
    return UnitOptions(
        db=args.db,
        operator=check_label(args.operator, "--operator"),
        note=check_label(args.note, "--note"),
        verbose=args.verbose,
        auto_confirm=args.auto_confirm,
        report_dir=args.report_dir,
        report_formats=resolve_formats(args.report_format or ["all"]),
    )


@dataclass
class UnitOutcome:
    result: RunResult
    code: int
    stop_requested: bool = False
    nobody_answered: bool = False
    reports_failed: bool = False

    @property
    def bench_in_doubt(self) -> Optional[str]:
        """Why no further unit should be tested on this bench, if there is a reason."""
        if self.result.unsafe:
            return f"outputs not confirmed off: {', '.join(self.result.unsafe)}"
        if self.result.database_problem is not None:
            return "the results database failed"
        if self.reports_failed:
            return "the reports could not be written"
        if self.nobody_answered:
            return "no one answered the operator prompt"
        return None


def run_unit(store: Store, bench: Bench, serial: str, options: UnitOptions, log: Optional[IO[str]] = None) -> UnitOutcome:
    """Run the plan on one unit, record it, and write its reports."""
    nobody = [False]

    def prompt(text: str) -> None:
        if options.auto_confirm:
            confirm_automatically(text)
            return
        try:
            ask_operator(text)
        except OperatorUnavailable:
            nobody[0] = True
            raise

    runner = Runner(
        bench.plan,
        store,
        serial,
        operator=options.operator,
        console=say,
        scpi_log=log,
        note=options.note,
        only_steps=options.only_steps,
        prompt=prompt,
        verbose=options.verbose,
        station=bench.station,
        asset_tags=bench.asset_tags,
        product=options.product,
        lot_id=options.lot_id,
    )
    with stop_requests_interrupt(lambda: runner.past_saving) as stops:
        result = runner.run()
        outcome = UnitOutcome(result, EXIT_BY_STATUS[result.status], nobody_answered=nobody[0])
        if result.database_problem is not None:
            complain(
                f"error: results database {options.db} failed during the run: {result.database_problem}. "
                "Whatever reached it before that is still there. No report was written."
            )
        elif options.report_dir:
            try:
                for path in write_reports(store.run_detail(result.run_id), options.report_dir, options.report_formats):
                    say(f"Report: {path}")
            except OSError as exc:
                complain(f"error: cannot write reports to {options.report_dir}: {exc}")
                complain(f"The result of run {result.run_id} is safe in {options.db}; write it later with the report command.")
                outcome.code = EXIT_ABORTED
                outcome.reports_failed = True
    outcome.stop_requested = stops.requested
    return outcome


# Arguments -------------------------------------------------------------


def add_address_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--station", help='JSON file like {"addresses": {"dmm": "192.0.2.10:5025"}}; keys are instrument or driver names')
    parser.add_argument(
        "--address", action="append", metavar="NAME=HOST:PORT", help="set one instrument's address (repeatable)"
    )


def add_sim_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dut", default="good", choices=sorted(DUT_VARIANTS), help="simulated unit (default: good)")
    parser.add_argument("--seed", type=int, default=1, help="noise seed (default: 1)")
    parser.add_argument("--noise", type=not_negative_number, default=1.0, help="noise scale, 0 for none (default: 1)")
    parser.add_argument(
        "--fault",
        action="append",
        metavar="INSTRUMENT:KIND[:AFTER]",
        help="inject a fault after AFTER good readings, such as dmm:drop:5; "
        "kinds: drop, hang, error, garbage, overload (repeatable)",
    )


def add_unit_args(parser: argparse.ArgumentParser, sim_help: str) -> None:
    """The options of every command that tests units: who, what to print, reports, and where the instruments are."""
    parser.add_argument("--operator", default="", help="operator label to record")
    parser.add_argument("--verbose", "-v", action="store_true", help="print every measurement as it is taken")
    parser.add_argument("--auto-confirm", action="store_true", help="carry on past operator prompts without waiting")
    parser.add_argument("--db", default=DEFAULT_DB, help=f"results database (default: {DEFAULT_DB})")
    parser.add_argument("--report-dir", help="also write reports here (HTML, CSV and JUnit XML unless --report-format says otherwise)")
    parser.add_argument(
        "--report-format",
        action="append",
        choices=CHOICES,
        metavar="NAME",
        help=f"with --report-dir, which report to write (repeatable): {', '.join(CHOICES)}; default all",
    )
    parser.add_argument("--scpi-log", metavar="FILE", help="add all SCPI traffic to this file")
    add_address_args(parser)
    add_station_name_arg(parser)
    parser.add_argument("--sim", action="store_true", help=sim_help)
    add_sim_args(parser)
