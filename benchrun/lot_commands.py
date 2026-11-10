"""The ``lot`` commands: open a batch of units, test them one after another, and see how it went."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Dict, Iterator, List, Optional, Tuple

from benchrun import lots, session
from benchrun.console import (
    DEFAULT_DB,
    EXIT_ABORTED,
    EXIT_FAIL,
    EXIT_PASS,
    actor_of,
    add_actor_arg,
    check_label,
    check_serial,
    complain,
    count,
    open_store,
    say,
    table,
)
from benchrun.errors import PlanError
from benchrun.files import atomic_write_text
from benchrun.inventory import existing_store
from benchrun.plan import load_plan
from benchrun.product_commands import load_product
from benchrun.products import Product
from benchrun.reports import lot_report
from benchrun.stats import charts
from benchrun.store import Store

MAX_SERIALS_BYTES = 1024 * 1024
MAX_SERIALS = 10000
MAX_PROBLEMS_SHOWN = 20
MAX_QUANTITY = 10_000_000
END_OF_INPUT = (b"\x04", b"\x1a")  # Ctrl+D or Ctrl+Z typed as a line, as a Windows console sends them.


def _quantity(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from None
    if not 1 <= value <= MAX_QUANTITY:
        raise argparse.ArgumentTypeError(f"{text} must be from 1 to {MAX_QUANTITY}")
    return value


def _when(text: Optional[str]) -> str:
    return text[:19].replace("T", " ") if text else ""


def _yield_line(units: Dict[str, Any]) -> str:
    tested = units["units_with_verdict"]
    if not tested:
        return "No unit has a complete run yet, so there is no yield"
    return (
        f"First-pass yield {charts.percent(units['first_pass_yield'])} ({units['first_pass_units']} of {count(tested, 'unit')}), "
        f"final yield {charts.percent(units['final_yield'])} ({units['final_pass_units']} of {tested})"
    )


def _counted(units: Dict[str, Any]) -> str:
    tested = units["units_with_verdict"]
    if units["quantity"] is None:
        return f"{count(tested, 'unit')} tested"
    return f"{tested} of {count(units['quantity'], 'unit')} tested, {units['to_test']} to go"


def _progress(lot: Dict[str, Any], units: Dict[str, Any]) -> str:
    return f"Lot {lot['name']}: {_counted(units)}"


# open, list, show, close, reopen ------------------------------------------


def cmd_open(args: argparse.Namespace) -> int:
    name = lots.check_name(args.name)
    note = check_label(args.note, "--note")
    actor = actor_of(args)
    if args.product:
        product = load_product(args.db, args.product)
        plan_name = product.load_plan().name
        part_number = product.part_number
    else:
        plan_name, part_number = load_plan(args.plan).name, ""
    with open_store(args.db) as store:
        lots.create(store.conn, name, part_number, plan_name, args.quantity, note, actor)
    tested_as = f"product {part_number} (plan {plan_name})" if part_number else f"plan {plan_name}"
    size = f" of {count(args.quantity, 'unit')}" if args.quantity else ""
    say(f"Opened lot {name}{size}, tested as {tested_as}.")
    command = f"benchrun lot run {name}" + ("" if part_number else f" --plan {args.plan}")
    say(f"Test its units with: {command}")
    return EXIT_PASS


def cmd_list(args: argparse.Namespace) -> int:
    with existing_store(args.db) as store:
        found = lots.listing(store.conn, args.status)
    if args.json:
        say(json.dumps({"lots": found}, indent=2))
        return EXIT_PASS
    if not found:
        say("No lots yet. Open one with: benchrun lot open NAME --plan FILE")
        return EXIT_PASS
    rows = [
        (
            lot["name"],
            lot["status"],
            lot["product"],
            lot["plan_name"],
            "" if lot["quantity"] is None else lot["quantity"],
            lot["units"]["units_with_verdict"],
            "" if lot["units"]["to_test"] is None else lot["units"]["to_test"],
            lot["runs"],
            charts.percent(lot["units"]["first_pass_yield"]),
            charts.percent(lot["units"]["final_yield"]),
            _when(lot["created_at"]),
        )
        for lot in found
    ]
    headers = ("lot", "status", "product", "plan", "quantity", "tested", "to test", "runs", "first-pass", "final", "opened (UTC)")
    say(table(headers, rows))
    return EXIT_PASS


def cmd_show(args: argparse.Namespace) -> int:
    with existing_store(args.db) as store:
        lot = lots.find(store.conn, args.name)
        found = lots.summary(store.conn, lot)
        results = lots.unit_results(store.conn, lot["id"]) if args.units or args.json else []
    if args.json:
        found["unit_results"] = results
        say(json.dumps(found, indent=2))
        return EXIT_PASS
    units, counts = found["units"], found["runs"]["counts"]
    say(f"Lot {lot['name']}: {lot['status'].upper()}")
    for label, value in (("Product", lot["product"]), ("Plan", lot["plan_name"]), ("Note", lot["note"])):
        if value:
            say(f"{label}: {value}")
    say(f"Opened {lot['created_at']}" + (f", closed {lot['closed_at']}" if lot["closed_at"] else "") + " (UTC)")
    waiting = units["serials"] - units["units_with_verdict"]
    say(f"Units: {_counted(units)}" + (f"; {waiting} more with no complete run yet" if waiting > 0 else ""))
    say(_yield_line(units) + ".")
    if units["retests"]:
        say(f"Retests: {count(units['retests'], 'run')} on {count(units['retested_units'], 'unit')}")
    say(f"Runs: {found['runs']['selected']}: {counts['pass']} pass, {counts['fail']} fail, {counts['aborted']} aborted, {counts['running']} running")
    if found["running_runs"]:
        say(f"Still running: {', '.join(f'run {run_id}' for run_id in found['running_runs'])}")
    say()
    pareto = found["pareto"]
    if pareto["rows"]:
        say("Failures by measurement:")
        rows = [(r["step"], r["name"], r["runs"], r["readings"], charts.percent(r["share"]), charts.percent(r["cumulative"])) for r in pareto["rows"]]
        say(table(("step", "measurement", "runs", "readings", "share", "cumulative"), rows))
    else:
        say("No failing measurements in the complete runs of this lot.")
    if args.units:
        say()
        rows = [(u["serial"], u["runs"], ", ".join(u["results"]), u["final_result"] or "", u["last_run"], ", ".join(u["failed_measurements"])) for u in results]
        say(table(("serial", "runs", "results in order", "final", "last run", "failed in the final run"), rows) if rows else "No units tested yet.")
    return EXIT_PASS


def cmd_close(args: argparse.Namespace) -> int:
    with existing_store(args.db) as store:
        lot = lots.find(store.conn, args.name)
        running = lots.close(store.conn, lot, args.force, actor_of(args))
    if running:
        say(f"Closed lot {lot['name']} with {count(len(running), 'run')} still running ({', '.join(str(r) for r in running)}); the audit log records that.")
    else:
        say(f"Closed lot {lot['name']}.")
    return EXIT_PASS


def cmd_reopen(args: argparse.Namespace) -> int:
    with existing_store(args.db) as store:
        lot = lots.find(store.conn, args.name)
        lots.reopen(store.conn, lot, actor_of(args))
    say(f"Reopened lot {lot['name']}.")
    return EXIT_PASS


# report -------------------------------------------------------------------


def cmd_report(args: argparse.Namespace) -> int:
    formats = list(dict.fromkeys(args.format or lot_report.FORMATS))
    with existing_store(args.db) as store:
        lot = lots.find(store.conn, args.name)
        paths = [Path(args.out) / f"{lot_report.file_stem(lot)}.{name}" for name in formats]
        taken = [str(path) for path in paths if path.exists()]
        if taken and not args.force:
            raise PlanError([f"{path} already exists; add --force to replace it" for path in taken])
        report = lot_report.build(store.conn, lot)
    Path(args.out).mkdir(parents=True, exist_ok=True)
    for name, path in zip(formats, paths):
        atomic_write_text(path, lot_report.RENDERERS[name](report))
        say(f"Report: {path}")
    return EXIT_PASS


# run ----------------------------------------------------------------------


def read_serials(path: str) -> List[str]:
    """Serials from a file, one per line. Blank lines and lines starting with # are skipped; a repeat is refused."""
    try:
        file_path = Path(path)
        if file_path.stat().st_size > MAX_SERIALS_BYTES:
            raise PlanError([f"{path} is larger than {MAX_SERIALS_BYTES // 1024} KiB, which is too big for a list of serials"])
        raw = file_path.read_bytes()
    except OSError as exc:
        raise PlanError([f"cannot read {path}: {exc}"]) from exc
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PlanError([f"{path} is not UTF-8 text ({exc.reason} at byte {exc.start}); save it as UTF-8"]) from exc
    serials: List[str] = []
    first_line: Dict[str, int] = {}
    problems: List[str] = []
    for number, line in enumerate(text.split("\n"), 1):
        cleaned = line.strip()
        if not cleaned or cleaned.startswith("#"):
            continue
        try:
            serial = check_serial(cleaned)
        except PlanError as exc:
            problems.append(f"{path} line {number}: {exc.problems[0]}")
            continue
        if serial in first_line:
            problems.append(f"{path} line {number}: serial {serial} is already on line {first_line[serial]}")
            continue
        first_line[serial] = number
        serials.append(serial)
    if len(serials) > MAX_SERIALS:
        problems.append(f"{path} holds {len(serials)} serials; one session takes at most {MAX_SERIALS}")
    if not serials and not problems:
        problems.append(f"{path} holds no serials")
    _refuse(problems)
    return serials


def _refuse(problems: List[str]) -> None:
    if len(problems) > MAX_PROBLEMS_SHOWN:
        problems = problems[:MAX_PROBLEMS_SHOWN] + [f"and {len(problems) - MAX_PROBLEMS_SHOWN} more"]
    if problems:
        raise PlanError(problems)


def _typed_serials() -> Iterator[str]:
    """Serials typed at the terminal, one at a time, until an empty line or the end of input."""
    while True:
        say()
        say("Serial number of the next unit (an empty line or Ctrl+D ends the session):")
        raw = session.read_answer()
        if raw is None or raw.strip() in END_OF_INPUT:
            return
        try:
            text = raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            complain("That is not valid UTF-8 text. Type the serial again.")
            continue
        if not text:
            return
        yield text


def _unit_problem(serial: str, lot: Dict[str, Any], product: Optional[Product], passed: Any, retest: bool) -> Optional[str]:
    if product is not None:
        try:
            product.check_serial(serial)
        except PlanError as exc:
            return exc.problems[0]
    return lots.serial_problem(lot, serial, passed, retest)


@dataclass
class _Tested:
    serial: str
    outcome: session.UnitOutcome


def _session(
    store: Store,
    bench: session.Bench,
    lot: Dict[str, Any],
    product: Optional[Product],
    options: session.UnitOptions,
    log: Optional[IO[str]],
    serials: Optional[List[str]],
    retest: bool,
) -> Tuple[List[_Tested], Optional[str]]:
    """Test each unit in turn. Returns what was tested, and why the session ended early, if it did."""
    tested: List[_Tested] = []
    try:
        for text in serials if serials is not None else _typed_serials():
            if serials is None:
                try:
                    serial = check_serial(text)
                    problem = _unit_problem(serial, lot, product, lots.passed_serials(store.conn, lot["id"]), retest)
                except PlanError as exc:
                    problem = exc.problems[0]
                if problem:
                    complain(f"{problem}. Type another serial.")
                    continue
            else:
                serial = text
            units = lots.units(store.conn, lot)
            say()
            say(f"Unit {len(tested) + 1} of this session: {serial}. {_progress(lot, units)}.")
            if units["to_test"] == 0:
                say(f"Note: lot {lot['name']} already has all {units['quantity']} units tested; this is one more.")
            outcome = session.run_unit(store, bench, serial, options, log)
            tested.append(_Tested(serial, outcome))
            if outcome.stop_requested:
                return tested, "stopped by the operator"
            if outcome.bench_in_doubt:
                return tested, outcome.bench_in_doubt
    except KeyboardInterrupt:
        return tested, "stopped by the operator"
    return tested, None


def _session_code(tested: List[_Tested]) -> int:
    codes = {item.outcome.code for item in tested}
    if EXIT_ABORTED in codes:
        return EXIT_ABORTED
    return EXIT_FAIL if EXIT_FAIL in codes else EXIT_PASS


def _print_session(store: Store, lot: Dict[str, Any], tested: List[_Tested], ended_by: Optional[str]) -> None:
    say()
    if ended_by:
        say(f"The lot session ended early: {ended_by}.")
    if not tested:
        say("No units were tested in this session.")
    else:
        rows = []
        for number, item in enumerate(tested, 1):
            result = item.outcome.result
            why = result.reason or ", ".join(lots.failed_measurements(store.conn, result.run_id))
            if item.outcome.reports_failed:
                why = "; ".join(part for part in (why, "reports not written") if part)
            rows.append((number, item.serial, result.run_id, result.status.upper(), why))
        say(table(("unit", "serial", "run", "result", "reason or failed measurements"), rows))
        statuses = [item.outcome.result.status for item in tested]
        say(
            f"Session: {count(len(tested), 'unit')} tested: {statuses.count('pass')} pass, "
            f"{statuses.count('fail')} fail, {statuses.count('aborted')} aborted."
        )
    units = lots.units(store.conn, lot)
    say(f"{_progress(lot, units)}. {_yield_line(units)}.")


def cmd_run(args: argparse.Namespace) -> int:
    options = session.unit_options(args)
    if not Path(args.db).exists():
        raise PlanError([f"no results database at {args.db}, so there is no lot {args.name!r} in it; give the right one with --db"])
    with open_store(args.db) as store:
        lot = lots.find_open(store.conn, args.name)
    product = load_product(args.db, lot["product"]) if lot["product"] else None
    plan_path = args.plan or (product.plan_file if product is not None else None)
    if not plan_path:
        raise PlanError(
            [f"lot {lot['name']} records the name of its plan ({lot['plan_name']}), not the file; give the file again with --plan FILE"]
        )
    serials = read_serials(args.serials) if args.serials else None
    options.product, options.lot_id = lot["product"], lot["id"]
    with session.bench_for(args, plan_path, product, None) as bench:
        lots.check_unit(lot, bench.plan.name, lot["product"])
        with session.scpi_log(args.scpi_log) as log, open_store(args.db) as store:
            if serials is not None:
                passed = lots.passed_serials(store.conn, lot["id"])
                _refuse([problem for problem in (_unit_problem(s, lot, product, passed, args.retest) for s in serials) if problem])
                say(f"Lot {lot['name']}: {count(len(serials), 'serial')} from {args.serials}")
            tested, ended_by = _session(store, bench, lot, product, options, log, serials, args.retest)
            _print_session(store, lot, tested, ended_by)
    return _session_code(tested)


# Argument parsing ---------------------------------------------------------


def _db(parser: argparse.ArgumentParser, changes: bool = False) -> None:
    parser.add_argument("--db", default=DEFAULT_DB, help=f"results database (default: {DEFAULT_DB})")
    if changes:
        add_actor_arg(parser)


def _name(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("name", help="the lot's name (or its numeric id)")


def add_commands(subparsers: Any) -> None:
    group = subparsers.add_parser(
        "lot",
        help="lots: batches of units tested and counted together",
        description="A lot is a batch of units of one plan, or of one product, with its own yield. "
        "Open one, test its units with lot run (or run --lot), then see it with lot show or lot report.",
        epilog="More: docs/lots.md.",
    )
    commands = group.add_subparsers(dest="lot_command", required=True, metavar="ACTION", help="what to do; each has its own --help")

    opener = commands.add_parser("open", help="open a new lot")
    opener.add_argument("name", help=f"a name for the lot, up to {lots.MAX_NAME_CHARS} printable characters, such as L2026-11-01")
    source = opener.add_mutually_exclusive_group(required=True)
    source.add_argument("--plan", metavar="FILE", help="the plan its units are tested with; the lot records the plan's name")
    source.add_argument("--product", metavar="PART_NUMBER", help="the product its units are; tested with that product's plan")
    opener.add_argument("--quantity", type=_quantity, metavar="N", help="how many units the lot holds, to count those still to test")
    opener.add_argument("--note", default="", help="one line to keep with the lot, such as the work order")
    _db(opener, changes=True)
    opener.set_defaults(func=cmd_open)

    listing = commands.add_parser("list", help="list lots, newest first, with their yield")
    listing.add_argument("--status", choices=(lots.OPEN, lots.CLOSED), help="only open or only closed lots")
    listing.add_argument("--json", action="store_true", help="machine-readable output")
    _db(listing)
    listing.set_defaults(func=cmd_list)

    show = commands.add_parser("show", help="one lot: units tested and to test, yield, and failures by measurement")
    _name(show)
    show.add_argument("--units", action="store_true", help="also list every serial with its results")
    show.add_argument("--json", action="store_true", help="machine-readable output, with every serial")
    _db(show)
    show.set_defaults(func=cmd_show)

    close = commands.add_parser("close", help="close a lot so no more runs can be added")
    _name(close)
    close.add_argument("--force", action="store_true", help="close it even though a run in it is still running; the audit log records that")
    _db(close, changes=True)
    close.set_defaults(func=cmd_close)

    reopen = commands.add_parser("reopen", help="open a closed lot again")
    _name(reopen)
    _db(reopen, changes=True)
    reopen.set_defaults(func=cmd_reopen)

    run = commands.add_parser(
        "run",
        help="test units of a lot one after another on the same bench",
        description="Each unit is one ordinary run in the lot. Serials come from --serials FILE, or are asked for "
        "one at a time; an empty line or Ctrl+D ends the session. Ctrl+C stops the unit on the bench safely and "
        "ends the session. Exit code: 0 if every unit tested passed, 1 if any failed, 3 if any aborted.",
    )
    _name(run)
    run.add_argument("--plan", metavar="FILE", help="the plan file; needed for a lot opened with --plan, optional for a product lot")
    run.add_argument("--serials", metavar="FILE", help="test these serials, one per line; # starts a comment line; a repeat is refused")
    run.add_argument("--retest", action="store_true", help="also test serials that already passed in this lot")
    run.add_argument("--note", default="", help="one line to keep with every run of this session")
    session.add_unit_args(run, "start private simulators for the whole session")
    run.set_defaults(func=cmd_run)

    report = commands.add_parser("report", help="write a lot summary as HTML, CSV and JSON")
    _name(report)
    report.add_argument("--out", required=True, metavar="FOLDER", help="the folder to write into; made if needed")
    report.add_argument(
        "--format", action="append", choices=lot_report.FORMATS, help="only this format (repeatable; default: html, csv and json)"
    )
    report.add_argument("--force", action="store_true", help="replace files that already exist")
    _db(report)
    report.set_defaults(func=cmd_report)
