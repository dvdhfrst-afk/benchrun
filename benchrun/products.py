"""Products: part numbers, the plan each is tested with, and what differs per part.

A product names a plan file and may carry its own values for the plan's variables,
its own limits for single measurements, and a pattern every serial must match.
Nothing here changes a plan file: the values are put into the loaded plan by
``benchrun.plan.apply_overrides`` just before a run, after all of them are checked.
"""

from __future__ import annotations

import csv
import io
import math
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from benchrun import audit
from benchrun.console import CONTROL, check_label
from benchrun.errors import PlanError
from benchrun.limits import Limit
from benchrun.plan import LimitPair, Plan, apply_overrides, load_plan, override_problems
from benchrun.reports.csv_report import _text as csv_text
from benchrun.store import now_iso

MAX_PART_NUMBER_CHARS = 64
MAX_PATTERN_CHARS = 200
MAX_OPEN_REPEATS = 3  # Repeats of varying length, such as * or {2,6}. Each one multiplies the work of a failed match.
MAX_ALTERNATIVES = 16  # The alternatives of every | group, multiplied together.
MAX_SERIAL_CHARS = 128  # The longest serial ``check_serial`` accepts; a pattern is never tried on more.
MAX_CSV_BYTES = 1024 * 1024
MAX_CSV_ROWS = 10000
CSV_COLUMNS = ("step", "measurement", "low", "high", "units")

LimitKey = Tuple[str, str]
_COUNT_RE = re.compile(r"\{(\d{1,3})(,(\d{0,3}))?\}")
_ESCAPES = "dDwWsS"
_BINARY_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")  # Anything but tab and line ends.


@dataclass
class Product:
    id: int
    part_number: str
    description: str
    plan_file: str
    serial_pattern: str
    created_at: str
    updated_at: str
    variables: Dict[str, float] = field(default_factory=dict)
    limits: Dict[LimitKey, LimitPair] = field(default_factory=dict)

    @property
    def subject(self) -> str:
        return f"product:{self.part_number}"

    def check_serial(self, serial: str) -> None:
        if self.serial_pattern and not pattern_matches(self.serial_pattern, serial):
            raise PlanError([f"serial {serial!r} does not match the pattern for product {self.part_number}: {self.serial_pattern}"])

    def load_plan(self, path: Optional[str] = None, variables: Optional[Mapping[str, float]] = None) -> Plan:
        """The product's plan, or the one at ``path``, with this product's values in it."""
        plan = load_plan(path or self.plan_file, variables)
        try:
            apply_overrides(plan, self.variables, self.limits, fixed=list(variables or {}))
        except PlanError as exc:
            raise PlanError([f"product {self.part_number}: {problem}" for problem in exc.problems]) from None
        return plan

    def as_json(self) -> Dict[str, Any]:
        return {
            "part_number": self.part_number,
            "description": self.description,
            "plan_file": self.plan_file,
            "serial_pattern": self.serial_pattern,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "variables": dict(sorted(self.variables.items())),
            "limits": [
                {"step": step, "measurement": name, "low": low, "high": high}
                for (step, name), (low, high) in sorted(self.limits.items())
            ],
        }


# Checking what people type ----------------------------------------------


def check_part_number(text: str) -> str:
    if not text or len(text) > MAX_PART_NUMBER_CHARS or not text.isprintable() or text != text.strip():
        raise PlanError(
            [f"part number {text!r} must be 1 to {MAX_PART_NUMBER_CHARS} printable characters, with no space at either end"]
        )
    return text


def check_pattern(text: str) -> str:
    """Accept a serial pattern only if no serial can make matching it slow. An empty one means any serial."""
    if not text:
        return ""
    if len(text) > MAX_PATTERN_CHARS or CONTROL.search(text) or "\n" in text or "\t" in text:
        raise PlanError([f"serial pattern must be one line of at most {MAX_PATTERN_CHARS} characters"])
    trouble = _pattern_problem(text)
    if trouble is None:
        try:
            re.compile(text)
        except re.error as exc:
            trouble = f"it is not a valid regular expression: {exc}"
    if trouble:
        raise PlanError([f"serial pattern {text!r}: {trouble}"])
    return text


def _pattern_problem(text: str) -> Optional[str]:
    """Refuse what can make a regular expression take very long: repeats inside repeats, and too many choices.

    Python's matcher backtracks, so ``(a+)+`` or ``(a|a)*`` can take years on a
    serial of 30 characters. Without nested repeats, and with few repeats of
    varying length and few alternatives, the worst case stays small.
    """
    groups: List[Dict[str, Any]] = [{"repeat": False, "choices": 1, "product": 1}]
    last: Any = None  # What a repeat would apply to: "atom", a closed group, or None.
    varying = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            if i + 1 >= len(text):
                return "it ends with a lone backslash"
            if text[i + 1].isalnum() and text[i + 1] not in _ESCAPES:
                return f"\\{text[i + 1]} is not supported; use \\d, \\w or \\s, or a backslash before punctuation"
            i, last = i + 2, "atom"
        elif ch == "[":
            end = i + 1 + (text[i + 1 : i + 2] == "^")
            end += text[end : end + 1] == "]"
            while end < len(text) and text[end] != "]":
                end += 2 if text[end] == "\\" else 1
            if end >= len(text):
                return "a [ has no closing ]"
            i, last = end + 1, "atom"
        elif ch == "(":
            if text.startswith("(?", i) and not text.startswith("(?:", i):
                return "only plain groups, ( ) or (?: ), are supported"
            i += 3 if text.startswith("(?:", i) else 1
            groups.append({"repeat": False, "choices": 1, "product": 1})
            last = None
        elif ch == ")":
            if len(groups) == 1:
                return "a ) has no opening ("
            inner = groups.pop()
            inner["product"] *= inner["choices"]
            groups[-1]["product"] *= inner["product"]
            groups[-1]["repeat"] = groups[-1]["repeat"] or inner["repeat"]
            i, last = i + 1, inner
        elif ch == "|":
            groups[-1]["choices"] += 1
            i, last = i + 1, None
        elif ch in "*+?{":
            count = _COUNT_RE.match(text, i) if ch == "{" else None
            if ch == "{" and count is None:
                return "a { must be a repeat count such as {4} or {2,6}"
            if last is None or last == "repeat":
                return f"the repeat at position {i + 1} has nothing to repeat, or repeats a repeat"
            if isinstance(last, dict) and (last["repeat"] or last["product"] > 1):
                return "a group that is repeated must not hold a repeat or a |, because matching it can take very long"
            if count is None:
                varying += 1
                i += 1
            else:
                varying += count.group(2) is not None and count.group(1) != count.group(3)
                i = count.end()
            i += text[i : i + 1] == "?"  # A lazy repeat is still one repeat.
            groups[-1]["repeat"] = True
            last = "repeat"
        else:
            i, last = i + 1, None if ch in "^$" else "atom"
    if len(groups) > 1:
        return "a ( has no closing )"
    if varying > MAX_OPEN_REPEATS:
        return f"it has {varying} repeats of varying length; the most allowed is {MAX_OPEN_REPEATS}"
    if groups[0]["product"] * groups[0]["choices"] > MAX_ALTERNATIVES:
        return f"its | choices multiply to more than {MAX_ALTERNATIVES}"
    return None


def pattern_matches(pattern: str, serial: str) -> bool:
    """The whole serial must match. Patterns are checked when they are set, and serials are short."""
    return len(serial) <= MAX_SERIAL_CHARS and re.fullmatch(pattern, serial) is not None


def parse_limit_side(text: Optional[str], label: str) -> Optional[float]:
    """A limit from text: empty means that side is open."""
    if text is None:
        return None
    cleaned = text.strip()
    if cleaned.startswith("'"):  # A spreadsheet's mark that a cell is text.
        cleaned = cleaned[1:].strip()
    if not cleaned:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        raise PlanError([f"{label} {text!r} is not a number"]) from None
    if not math.isfinite(value):
        raise PlanError([f"{label} {text!r} is not a finite number"])
    return value


def describe(pair: Optional[LimitPair], units: str = "") -> str:
    if pair is None:
        return ""
    return Limit(pair[0], pair[1]).describe(units)


# Reading the database ----------------------------------------------------


def get(conn: sqlite3.Connection, part_number: str) -> Optional[Product]:
    row = conn.execute("SELECT * FROM products WHERE part_number = ?", (part_number,)).fetchone()
    if row is None:
        return None
    product = Product(
        row["id"], row["part_number"], row["description"], row["plan_file"], row["serial_pattern"], row["created_at"], row["updated_at"]
    )
    for item in conn.execute("SELECT name, value FROM product_variables WHERE product_id = ? ORDER BY name", (product.id,)):
        product.variables[item["name"]] = item["value"]
    for item in conn.execute(
        "SELECT step, measurement, low, high FROM product_limits WHERE product_id = ? ORDER BY step, measurement", (product.id,)
    ):
        product.limits[(item["step"], item["measurement"])] = (item["low"], item["high"])
    return product


def require(conn: sqlite3.Connection, part_number: str) -> Product:
    product = get(conn, part_number)
    if product is None:
        raise PlanError([f"no product {part_number!r}; see: benchrun product list"])
    return product


def run_count(conn: sqlite3.Connection, part_number: str) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM runs WHERE product = ?", (part_number,)).fetchone()[0])


def summaries(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT p.part_number, p.description, p.plan_file, p.serial_pattern, p.updated_at,"
        " (SELECT COUNT(*) FROM product_variables v WHERE v.product_id = p.id) AS variables,"
        " (SELECT COUNT(*) FROM product_limits l WHERE l.product_id = p.id) AS limits,"
        " (SELECT COUNT(*) FROM runs r WHERE r.product = p.part_number) AS runs"
        " FROM products p ORDER BY p.part_number"
    ).fetchall()
    return [dict(row) for row in rows]


# Changing the database ---------------------------------------------------


def _check_plan(product: Product, plan_file: str) -> Plan:
    """Load ``plan_file`` and check the product's values against it."""
    plan = load_plan(plan_file)
    problems = override_problems(plan, product.variables, product.limits)
    if problems:
        raise PlanError([f"{plan_file} does not fit what product {product.part_number} already has: {p}" for p in problems])
    return plan


def plan_of(product: Product) -> Plan:
    """The product's own plan, loaded and checked, without its values applied."""
    try:
        return load_plan(product.plan_file)
    except PlanError as exc:
        raise PlanError([f"product {product.part_number} uses plan {product.plan_file}, which cannot be used:"] + exc.problems) from None


def add(conn: sqlite3.Connection, part_number: str, description: str, plan_file: str, serial_pattern: str, actor: str) -> None:
    check_part_number(part_number)
    check_label(description, "--description")
    check_pattern(serial_pattern)
    load_plan(plan_file)
    stamp = now_iso()
    try:
        with conn:
            conn.execute(
                "INSERT INTO products (part_number, description, plan_file, serial_pattern, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (part_number, description, plan_file, serial_pattern, stamp, stamp),
            )
            detail = {"description": description, "plan_file": plan_file, "serial_pattern": serial_pattern}
            audit.record(conn, "product.add", f"product:{part_number}", detail, actor)
    except sqlite3.IntegrityError:
        raise PlanError([f"product {part_number!r} already exists; change it with: benchrun product edit"]) from None


def edit(conn: sqlite3.Connection, product: Product, changes: Mapping[str, str], actor: str) -> Dict[str, List[str]]:
    """Change description, plan_file or serial_pattern. Returns what changed, as old and new."""
    unknown = set(changes) - {"description", "plan_file", "serial_pattern"}
    if unknown:
        raise ValueError(f"a product has no field {sorted(unknown)[0]!r} to edit")
    if "description" in changes:
        check_label(changes["description"], "--description")
    if "serial_pattern" in changes:
        check_pattern(changes["serial_pattern"])
    if "plan_file" in changes:
        _check_plan(product, changes["plan_file"])
    changed = {key: [getattr(product, key), value] for key, value in changes.items() if getattr(product, key) != value}
    if not changed:
        return changed
    with conn:
        assignments = ", ".join(f"{key} = ?" for key in changed)
        conn.execute(
            f"UPDATE products SET {assignments}, updated_at = ? WHERE id = ?",
            (*[new for _, new in changed.values()], now_iso(), product.id),
        )
        audit.record(conn, "product.edit", product.subject, changed, actor)
    return changed


def delete(conn: sqlite3.Connection, product: Product, actor: str) -> None:
    """Remove the product and its values. Runs keep the part number they were tested as."""
    with conn:
        conn.execute("DELETE FROM product_variables WHERE product_id = ?", (product.id,))
        conn.execute("DELETE FROM product_limits WHERE product_id = ?", (product.id,))
        conn.execute("DELETE FROM products WHERE id = ?", (product.id,))
        detail = product.as_json()
        detail["runs"] = run_count(conn, product.part_number)
        audit.record(conn, "product.delete", product.subject, detail, actor)


def _touch(conn: sqlite3.Connection, product: Product) -> None:
    conn.execute("UPDATE products SET updated_at = ? WHERE id = ?", (now_iso(), product.id))


def set_variable(conn: sqlite3.Connection, product: Product, name: str, value: float, actor: str) -> None:
    problems = override_problems(plan_of(product), {name: value}, {})
    if problems:
        raise PlanError(problems)
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO product_variables (product_id, name, value) VALUES (?, ?, ?)", (product.id, name, value)
        )
        _touch(conn, product)
        audit.record(conn, "product.set-var", product.subject, {"name": name, "old": product.variables.get(name), "new": value}, actor)


def unset_variable(conn: sqlite3.Connection, product: Product, name: str, actor: str) -> None:
    if name not in product.variables:
        raise PlanError([f"product {product.part_number} has no value of its own for {name!r}"])
    with conn:
        conn.execute("DELETE FROM product_variables WHERE product_id = ? AND name = ?", (product.id, name))
        _touch(conn, product)
        audit.record(conn, "product.unset-var", product.subject, {"name": name, "old": product.variables[name]}, actor)


def set_limit(conn: sqlite3.Connection, product: Product, key: LimitKey, pair: LimitPair, actor: str) -> None:
    problems = override_problems(plan_of(product), {}, {key: pair})
    if problems:
        raise PlanError(problems)
    with conn:
        _write_limit(conn, product, key, pair)
        _touch(conn, product)
        old = product.limits.get(key)
        detail = {"step": key[0], "measurement": key[1], "old": None if old is None else list(old), "new": list(pair)}
        audit.record(conn, "product.set-limit", product.subject, detail, actor)


def _write_limit(conn: sqlite3.Connection, product: Product, key: LimitKey, pair: LimitPair) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO product_limits (product_id, step, measurement, low, high) VALUES (?, ?, ?, ?, ?)",
        (product.id, key[0], key[1], pair[0], pair[1]),
    )


def unset_limit(conn: sqlite3.Connection, product: Product, key: LimitKey, actor: str) -> None:
    if key not in product.limits:
        raise PlanError([f"product {product.part_number} has no limit of its own for {key[0]}.{key[1]}"])
    with conn:
        conn.execute(
            "DELETE FROM product_limits WHERE product_id = ? AND step = ? AND measurement = ?", (product.id, key[0], key[1])
        )
        _touch(conn, product)
        detail = {"step": key[0], "measurement": key[1], "old": list(product.limits[key])}
        audit.record(conn, "product.unset-limit", product.subject, detail, actor)


# Limits in CSV files -----------------------------------------------------


def read_limits_csv(path: str, plan: Plan) -> Dict[LimitKey, LimitPair]:
    """Every limit in the file, checked against the plan. Any bad row refuses the whole file."""
    file_path = Path(path)
    try:
        if file_path.stat().st_size > MAX_CSV_BYTES:
            raise PlanError([f"{path} is larger than {MAX_CSV_BYTES // 1024} KiB, which is too big for a limit file"])
        text = file_path.read_bytes().decode("utf-8-sig")
    except OSError as exc:
        raise PlanError([f"cannot read {path}: {exc}"]) from None
    except UnicodeDecodeError as exc:
        raise PlanError([f"{path} is not UTF-8 text ({exc.reason} at byte {exc.start}); save it as UTF-8 CSV"]) from None
    odd = _BINARY_RE.search(text)
    if odd:
        line = text.count("\n", 0, odd.start()) + 1
        raise PlanError([f"{path} holds a control character on line {line}, so it is not a usable CSV file"])
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        rows = [(reader.line_num, row) for row in reader if any(cell.strip() for cell in row)]
    except csv.Error as exc:
        raise PlanError([f"{path} is not a usable CSV file: {exc}"]) from None
    if not rows:
        raise PlanError([f"{path} is empty; the first row must be: {','.join(CSV_COLUMNS)}"])
    header = [cell.strip().lower() for cell in rows[0][1]]
    if sorted(header) != sorted(CSV_COLUMNS):
        raise PlanError([f"{path}: the first row must name the columns {','.join(CSV_COLUMNS)} (in any order), got {','.join(header)[:200]}"])
    if len(rows) - 1 > MAX_CSV_ROWS:
        raise PlanError([f"{path} has more than {MAX_CSV_ROWS} rows"])
    found: Dict[LimitKey, LimitPair] = {}
    seen_on: Dict[LimitKey, int] = {}
    problems: List[str] = []
    for line, cells in rows[1:]:
        try:
            key, pair, units = _limit_row(header, cells)
        except PlanError as exc:
            problems += [f"row {line}: {problem}" for problem in exc.problems]
            continue
        if key in seen_on:
            problems.append(f"row {line}: {key[0]}.{key[1]} is also on row {seen_on[key]}")
            continue
        seen_on[key] = line
        trouble = override_problems(plan, {}, {key: pair})
        if not trouble:
            measure_units = next(m.units for s in plan.steps if s.name == key[0] for m in s.measures + s.summary if m.name == key[1])
            if units != measure_units:
                trouble = [f"units {units!r} are not the plan's units for {key[0]}.{key[1]}, which are {measure_units!r}"]
        problems += [f"row {line}: {problem}" for problem in trouble]
        found[key] = pair
    if problems:
        raise PlanError([f"{path} was not used, because of these rows:"] + problems)
    return found


def _limit_row(header: Sequence[str], cells: Sequence[str]) -> Tuple[LimitKey, LimitPair, str]:
    if len(cells) != len(header):
        raise PlanError([f"has {len(cells)} cells; every row needs {len(header)}"])
    values = dict(zip(header, cells))
    step, name = values["step"].strip(), values["measurement"].strip()
    if not step or not name:
        raise PlanError(["step and measurement must both be filled in"])
    units = values["units"].strip()
    if units.startswith("'"):
        units = units[1:]
    problems = []
    sides = []
    for label in ("low", "high"):
        try:
            sides.append(parse_limit_side(values[label], label))
        except PlanError as exc:
            problems += exc.problems
    if problems:
        raise PlanError(problems)
    return (step, name), (sides[0], sides[1]), units


def render_limits_csv(product: Product, plan: Plan) -> str:
    """The product's own limits, in the plan's order, ready to edit and import again."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    order = {}
    for step in plan.steps:
        for measure in step.measures + step.summary:
            order[(step.name, measure.name)] = (len(order), measure.units)
    for key in sorted(product.limits, key=lambda k: order.get(k, (len(order), ""))):
        low, high = product.limits[key]
        units = order.get(key, (0, ""))[1]
        writer.writerow([csv_text(key[0]), csv_text(key[1]), "" if low is None else repr(low), "" if high is None else repr(high), csv_text(units)])
    return buffer.getvalue()


def compare_limits(
    current: Mapping[LimitKey, LimitPair], incoming: Mapping[LimitKey, LimitPair], replace: bool
) -> List[Tuple[str, LimitKey, Optional[LimitPair], Optional[LimitPair]]]:
    """What an import would do: one (action, key, old, new) for each limit, the action being
    added, changed, unchanged or removed."""
    report = []
    for key, pair in incoming.items():
        old = current.get(key)
        action = "added" if old is None else "unchanged" if tuple(old) == tuple(pair) else "changed"
        report.append((action, key, old, pair))
    if replace:
        report += [("removed", key, pair, None) for key, pair in current.items() if key not in incoming]
    return report


def import_limits(
    conn: sqlite3.Connection,
    product: Product,
    report: Sequence[Tuple[str, LimitKey, Optional[LimitPair], Optional[LimitPair]]],
    source: str,
    actor: str,
) -> None:
    """Carry out a report from ``compare_limits`` in one transaction."""
    changes = [item for item in report if item[0] != "unchanged"]
    if not changes:
        return
    with conn:
        for action, key, _, new in changes:
            if action == "removed":
                conn.execute(
                    "DELETE FROM product_limits WHERE product_id = ? AND step = ? AND measurement = ?",
                    (product.id, key[0], key[1]),
                )
            elif new is not None:
                _write_limit(conn, product, key, new)
        _touch(conn, product)
        detail = {
            "file": source,
            "changes": [
                {"action": action, "step": key[0], "measurement": key[1], "old": None if old is None else list(old), "new": None if new is None else list(new)}
                for action, key, old, new in changes
            ],
        }
        audit.record(conn, "product.import-limits", product.subject, detail, actor)
