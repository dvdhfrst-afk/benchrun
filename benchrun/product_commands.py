"""The ``product`` commands: part numbers, their plans, variables and limits."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
from typing import Any, Dict, List

from benchrun import products
from benchrun.console import (
    DEFAULT_DB,
    EXIT_PASS,
    check_label,
    count,
    fmt,
    open_store,
    say,
    table,
)
from benchrun.errors import PlanError
from benchrun.files import atomic_write_text
from benchrun.plan import describe_limit_side


def actor_of(args: argparse.Namespace) -> str:
    """Who to name in the audit log: ``--actor``, or the login name of whoever runs the command."""
    name = args.actor if args.actor is not None else os.environ.get("USER") or os.environ.get("USERNAME") or ""
    return check_label(name, "--actor")


def _existing_store(path: str):
    if not Path(path).exists():
        raise PlanError([f"no results database at {path}"])
    return open_store(path)


def _number(text: str, flag: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise PlanError([f"{flag}: {text!r} is not a number"]) from None
    if not math.isfinite(value):
        raise PlanError([f"{flag}: {text!r} is not a finite number"])
    return value


# Commands --------------------------------------------------------------


def cmd_add(args: argparse.Namespace) -> int:
    with open_store(args.db) as store:
        products.add(store.conn, args.part_number, args.description, args.plan, args.serial_pattern, actor_of(args))
    say(f"Added product {args.part_number}, tested with {args.plan}.")
    return EXIT_PASS


def cmd_list(args: argparse.Namespace) -> int:
    with _existing_store(args.db) as store:
        rows = products.summaries(store.conn)
    if args.json:
        say(json.dumps({"products": rows}, indent=2))
        return EXIT_PASS
    if not rows:
        say("No products yet. Add one with: benchrun product add PART_NUMBER --plan FILE")
        return EXIT_PASS
    headers = ("part number", "description", "plan", "serial pattern", "variables", "limits", "runs")
    keys = ("part_number", "description", "plan_file", "serial_pattern", "variables", "limits", "runs")
    say(table(headers, [[row[key] for key in keys] for row in rows]))
    return EXIT_PASS


def cmd_show(args: argparse.Namespace) -> int:
    with _existing_store(args.db) as store:
        product = products.require(store.conn, args.part_number)
        runs = products.run_count(store.conn, args.part_number)
    if args.json:
        payload = product.as_json()
        payload["runs"] = runs
        say(json.dumps(payload, indent=2))
        return EXIT_PASS
    say(f"Product {product.part_number}" + (f": {product.description}" if product.description else ""))
    say(f"Plan: {product.plan_file}")
    say(f"Serial pattern: {product.serial_pattern or 'none (any serial)'}")
    say(f"Runs recorded: {runs}")
    say(f"Added {product.created_at}, last changed {product.updated_at} (UTC)")
    say()
    if product.variables:
        say(table(("variable", "value"), [(name, fmt(value)) for name, value in product.variables.items()]))
    else:
        say("No variables of its own: the plan's values apply.")
    say()
    if product.limits:
        rows = [(step, name, fmt(low), fmt(high)) for (step, name), (low, high) in product.limits.items()]
        say(table(("step", "measurement", "low", "high"), rows))
    else:
        say("No limits of its own: the plan's limits apply.")
    return EXIT_PASS


def cmd_edit(args: argparse.Namespace) -> int:
    changes = {
        key: value
        for key, value in (("description", args.description), ("plan_file", args.plan), ("serial_pattern", args.serial_pattern))
        if value is not None
    }
    if not changes:
        raise PlanError(["say what to change: --description, --plan or --serial-pattern"])
    with _existing_store(args.db) as store:
        product = products.require(store.conn, args.part_number)
        changed = products.edit(store.conn, product, changes, actor_of(args))
    if not changed:
        say(f"Product {args.part_number} already has those values; nothing was changed.")
    for key, (old, new) in changed.items():
        say(f'{key.replace("_", " ")}: "{old}" -> "{new}"')
    return EXIT_PASS


def cmd_delete(args: argparse.Namespace) -> int:
    with _existing_store(args.db) as store:
        product = products.require(store.conn, args.part_number)
        runs = products.run_count(store.conn, args.part_number)
        if runs and not args.force:
            raise PlanError(
                [
                    f"product {args.part_number} is named by {count(runs, 'recorded run')}; "
                    "add --force to delete the product anyway (its runs are kept, and still name it)"
                ]
            )
        products.delete(store.conn, product, actor_of(args))
    kept = f" {count(runs, 'run')} still name it." if runs else ""
    say(f"Deleted product {args.part_number}.{kept}")
    return EXIT_PASS


def cmd_set_var(args: argparse.Namespace) -> int:
    value = _number(args.value, args.name)
    with _existing_store(args.db) as store:
        product = products.require(store.conn, args.part_number)
        products.set_variable(store.conn, product, args.name, value, actor_of(args))
    say(f"Product {args.part_number}: {args.name} = {value:g}")
    return EXIT_PASS


def cmd_unset_var(args: argparse.Namespace) -> int:
    with _existing_store(args.db) as store:
        product = products.require(store.conn, args.part_number)
        products.unset_variable(store.conn, product, args.name, actor_of(args))
    say(f"Product {args.part_number}: {args.name} now comes from the plan.")
    return EXIT_PASS


def cmd_set_limit(args: argparse.Namespace) -> int:
    pair = (products.parse_limit_side(args.low, "--low"), products.parse_limit_side(args.high, "--high"))
    key = (args.step, args.measurement)
    with _existing_store(args.db) as store:
        product = products.require(store.conn, args.part_number)
        products.set_limit(store.conn, product, key, pair, actor_of(args))
    say(f"Product {args.part_number}: {args.step}.{args.measurement} limit {products.describe(pair)}")
    return EXIT_PASS


def cmd_unset_limit(args: argparse.Namespace) -> int:
    key = (args.step, args.measurement)
    with _existing_store(args.db) as store:
        product = products.require(store.conn, args.part_number)
        products.unset_limit(store.conn, product, key, actor_of(args))
    say(f"Product {args.part_number}: {args.step}.{args.measurement} now has the plan's limits.")
    return EXIT_PASS


def cmd_import_limits(args: argparse.Namespace) -> int:
    with _existing_store(args.db) as store:
        product = products.require(store.conn, args.part_number)
        plan = products.plan_of(product)
        incoming = products.read_limits_csv(args.file, plan)
        report = products.compare_limits(product.limits, incoming, args.replace)
        if not args.dry_run:
            products.import_limits(store.conn, product, report, args.file, actor_of(args))
    units = {(step.name, m.name): m.units for step in plan.steps for m in step.measures + step.summary}
    rows = [
        (action, key[0], key[1], products.describe(new, units.get(key, "")), products.describe(old, units.get(key, "")))
        for action, key, old, new in report
    ]
    say(f"Limits for product {args.part_number} from {args.file}:")
    if rows:
        say(table(("action", "step", "measurement", "limit", "was"), rows))
    tally: Dict[str, int] = {}
    for action, *_ in report:
        tally[action] = tally.get(action, 0) + 1
    say(", ".join(f"{tally.get(action, 0)} {action}" for action in ("added", "changed", "unchanged", "removed")))
    if args.dry_run:
        say("Dry run: nothing was changed.")
    elif any(action != "unchanged" for action, *_ in report):
        say("Saved.")
    else:
        say("Nothing to change.")
    return EXIT_PASS


def cmd_export_limits(args: argparse.Namespace) -> int:
    if args.out and Path(args.out).exists() and not args.force:
        raise PlanError([f"{args.out} already exists; choose another name or add --force to replace it"])
    with _existing_store(args.db) as store:
        product = products.require(store.conn, args.part_number)
    text = products.render_limits_csv(product, products.plan_of(product))
    if not args.out:
        say(text.rstrip("\n"))
        return EXIT_PASS
    atomic_write_text(args.out, text)
    say(f"Wrote {count(len(product.limits), 'limit')} for product {args.part_number} to {args.out}")
    return EXIT_PASS


# What validate --product shows -------------------------------------------


def effective_limits(plan: Any, product: products.Product) -> List[Dict[str, Any]]:
    """Every limited measurement of a plan with a product's values in it, and where its limit came from."""
    rows = []
    for step in plan.steps:
        for measure in step.measures + step.summary:
            if not measure.is_limited:
                continue
            rows.append(
                {
                    "step": step.name,
                    "measurement": measure.name,
                    "units": measure.units,
                    "low": describe_limit_side(measure.low, plan.variables),
                    "high": describe_limit_side(measure.high, plan.variables),
                    "from": "product" if (step.name, measure.name) in product.limits else "plan",
                }
            )
    return rows


def print_effective_limits(plan: Any, product: products.Product) -> None:
    say(f"  Product {product.part_number}: {count(len(product.variables), 'variable')} and {count(len(product.limits), 'limit')} of its own")
    for name, value in product.variables.items():
        say(f"    {name} = {value:g}")
    rows = effective_limits(plan, product)
    say()
    say(table(("step", "measurement", "units", "low", "high", "from"), [list(row.values()) for row in rows]))


def load_product(db: str, part_number: str) -> products.Product:
    """The product a run or a check names. Its database must already exist."""
    with _existing_store(db) as store:
        return products.require(store.conn, part_number)


# Argument parsing ------------------------------------------------------


def _common(parser: argparse.ArgumentParser, changes: bool = True) -> None:
    parser.add_argument("--db", default=DEFAULT_DB, help=f"results database (default: {DEFAULT_DB})")
    if changes:
        parser.add_argument("--actor", help="who to name in the audit log (default: your login name)")


def _part(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("part_number", help="the part number")


def add_commands(subparsers: Any) -> None:
    product = subparsers.add_parser(
        "product",
        help="part numbers: the plan each is tested with, its variables and its limits",
        description="Part numbers with their own plan, variables, limits and serial pattern. "
        "Use them with: benchrun run --product PART_NUMBER --serial SN",
    )
    commands = product.add_subparsers(dest="product_command", required=True, metavar="ACTION", help="what to do")

    add = commands.add_parser("add", help="add a part number")
    add.add_argument("part_number", help="the part number, up to 64 printable characters")
    add.add_argument("--plan", required=True, help="the plan file it is tested with; checked now")
    add.add_argument("--description", default="", help="one line about it")
    add.add_argument("--serial-pattern", default="", metavar="REGEX", help="a regular expression every whole serial must match")
    _common(add)
    add.set_defaults(func=cmd_add)

    listing = commands.add_parser("list", help="list part numbers")
    listing.add_argument("--json", action="store_true", help="machine-readable output")
    _common(listing, changes=False)
    listing.set_defaults(func=cmd_list)

    show = commands.add_parser("show", help="one part number with its variables and limits")
    _part(show)
    show.add_argument("--json", action="store_true", help="machine-readable output")
    _common(show, changes=False)
    show.set_defaults(func=cmd_show)

    edit = commands.add_parser("edit", help="change a part number's description, plan or serial pattern")
    _part(edit)
    edit.add_argument("--description", help="one line about it")
    edit.add_argument("--plan", help="another plan file; its variables and limits must fit the product's own")
    edit.add_argument("--serial-pattern", metavar="REGEX", help='a new pattern, or "" for any serial')
    _common(edit)
    edit.set_defaults(func=cmd_edit)

    delete = commands.add_parser("delete", help="delete a part number; its runs are kept")
    _part(delete)
    delete.add_argument("--force", action="store_true", help="delete it even though recorded runs name it")
    _common(delete)
    delete.set_defaults(func=cmd_delete)

    set_var = commands.add_parser("set-var", help="give a plan variable this product's own value")
    _part(set_var)
    set_var.add_argument("name", help="a variable the plan has")
    set_var.add_argument("value", help="a number")
    _common(set_var)
    set_var.set_defaults(func=cmd_set_var)

    unset_var = commands.add_parser("unset-var", help="go back to the plan's value for a variable")
    _part(unset_var)
    unset_var.add_argument("name", help="the variable")
    _common(unset_var)
    unset_var.set_defaults(func=cmd_unset_var)

    set_limit = commands.add_parser("set-limit", help="replace the plan's limits for one measurement")
    _part(set_limit)
    set_limit.add_argument("step", help="the step's name in the plan")
    set_limit.add_argument("measurement", help="the measurement's name in that step")
    set_limit.add_argument("--low", help="lowest passing value, in the measurement's units; leave out for no low limit")
    set_limit.add_argument("--high", help="highest passing value; leave out for no high limit")
    _common(set_limit)
    set_limit.set_defaults(func=cmd_set_limit)

    unset_limit = commands.add_parser("unset-limit", help="go back to the plan's limits for one measurement")
    _part(unset_limit)
    unset_limit.add_argument("step", help="the step's name in the plan")
    unset_limit.add_argument("measurement", help="the measurement's name in that step")
    _common(unset_limit)
    unset_limit.set_defaults(func=cmd_unset_limit)

    importer = commands.add_parser(
        "import-limits",
        help="set limits from a CSV file with columns step, measurement, low, high, units",
        description="Every row is checked against the product's plan first. One bad row and nothing is changed.",
    )
    _part(importer)
    importer.add_argument("file", help="the CSV file")
    importer.add_argument("--dry-run", action="store_true", help="show what would change, and change nothing")
    importer.add_argument("--replace", action="store_true", help="also remove the product's limits that are not in the file")
    _common(importer)
    importer.set_defaults(func=cmd_import_limits)

    exporter = commands.add_parser("export-limits", help="write the product's own limits as CSV, ready to import")
    _part(exporter)
    exporter.add_argument("--out", help="the CSV file to write (default: print it)")
    exporter.add_argument("--force", action="store_true", help="replace the file if it already exists")
    _common(exporter, changes=False)
    exporter.set_defaults(func=cmd_export_limits)
