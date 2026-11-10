"""What every command shares: exit codes, safe printing, argument checks and tables.

Command groups outside the core import from here, never from ``benchrun.cli``.
"""

from __future__ import annotations

import argparse
import math
import os
import re
import sys
from datetime import datetime
from typing import Any, Dict, Iterable, Optional, Sequence

from benchrun.errors import PlanError
from benchrun.store import Store

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_ABORTED = 3

DEFAULT_DB = "benchrun.sqlite"


CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f]")
MAX_TEXT_CHARS = 2000


def print_to(stream_name: str, text: str) -> None:
    """Text can hold what an instrument or a file said, so nothing in it may steer the terminal."""
    cleaned = CONTROL.sub("?", text)
    stream = getattr(sys, stream_name)
    try:
        try:
            print(cleaned, file=stream, flush=True)
        except UnicodeEncodeError:
            print(cleaned.encode("ascii", "backslashreplace").decode("ascii"), file=stream, flush=True)
    except (OSError, ValueError):
        # The reader went away, as with "| head". That must not change a result.
        setattr(sys, stream_name, open(os.devnull, "w", encoding="utf-8"))


def say(text: str = "") -> None:
    print_to("stdout", text)


def complain(text: str) -> None:
    print_to("stderr", text)


def parse_pairs(items: Optional[Iterable[str]], flag: str) -> Dict[str, str]:
    pairs: Dict[str, str] = {}
    for item in items or []:
        name, sep, value = item.partition("=")
        if not sep or not name or not value:
            raise PlanError([f"{flag} expects name=value, got {item!r}"])
        pairs[name] = value
    return pairs


def parse_variables(items: Optional[Iterable[str]]) -> Dict[str, float]:
    variables = {}
    for name, value in parse_pairs(items, "--set").items():
        try:
            variables[name] = float(value)
        except ValueError:
            raise PlanError([f"--set {name}: {value!r} is not a number"]) from None
    return variables


def check_serial(serial: str) -> str:
    cleaned = serial.strip()
    if not cleaned or len(cleaned) > 128 or CONTROL.search(cleaned) or "\n" in cleaned or "\t" in cleaned:
        raise PlanError([f"serial number {serial!r} must be 1 to 128 printable characters"])
    return cleaned


def check_label(text: str, flag: str) -> str:
    if len(text) > MAX_TEXT_CHARS or CONTROL.search(text) or "\n" in text:
        raise PlanError([f"{flag} must be one line of at most {MAX_TEXT_CHARS} printable characters"])
    return text


def add_actor_arg(parser: argparse.ArgumentParser) -> None:
    """``--actor`` for a command that changes shared records and so writes the audit log."""
    parser.add_argument("--actor", metavar="NAME", help="who is making the change, kept in the audit log (default: your login name)")


def add_record_filter_args(parser: argparse.ArgumentParser) -> None:
    """``--product``, ``--lot`` and ``--station``, the same on every command that chooses recorded runs."""
    parser.add_argument("--product", metavar="PART_NUMBER", help="only runs tested as this product")
    parser.add_argument("--lot", metavar="NAME", help="only runs in this lot, by name or numeric id")
    parser.add_argument("--station", metavar="NAME", help="only runs recorded on this station")


def actor_of(args: argparse.Namespace) -> str:
    """Who to name in the audit log: ``--actor``, or the login name of whoever runs the command."""
    name = args.actor if args.actor is not None else os.environ.get("USER") or os.environ.get("USERNAME") or ""
    return check_label(name, "--actor")


def utc_time(text: Optional[str], flag: str, end_of_day: bool = False) -> Optional[str]:
    """Turn a date or a date and time, taken as UTC, into the form the database stores."""
    if text is None:
        return None
    for pattern in ("%Y-%m-%d", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            parsed = datetime.strptime(text, pattern)
        except ValueError:
            continue
        if pattern == "%Y-%m-%d" and end_of_day:
            return parsed.strftime("%Y-%m-%dT23:59:59.999+00:00")
        return parsed.strftime("%Y-%m-%dT%H:%M:%S.000+00:00")
    raise PlanError([f"{flag}: {text!r} is not a time; use YYYY-MM-DD or YYYY-MM-DDTHH:MM (UTC)"])


def positive_number(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(f"{text} must be a finite number above 0")
    return value


def not_negative_number(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError(f"{text} must be a finite number, 0 or above")
    return value


def not_negative_whole(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from None
    if value < 0:
        raise argparse.ArgumentTypeError(f"{text} must be 0 or above")
    return value


def open_store(path: str) -> Store:
    store = Store(path)
    if store.upgrade_note:
        complain(store.upgrade_note)
    return store


def count(number: int, word: str) -> str:
    return f"{number} {word}" if number == 1 else f"{number} {word}s"


def table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
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


def fmt(value: Optional[float]) -> str:
    return "" if value is None else f"{value:.6g}"
