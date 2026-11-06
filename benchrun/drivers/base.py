"""The instrument base class: safe SCPI exchange and what a plan may call.

Every driver keeps its SCPI strings in a ``COMMANDS`` table so that a plan can
override single entries for an instrument with a different dialect, without any
code. Every write is followed by an error queue check, and so is every reading,
so a command the instrument rejected can never turn into a silent false pass.
"""

from __future__ import annotations

import inspect
import math
import re
import string
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from benchrun.errors import (
    BadResponse,
    InstrumentFault,
    ReadingOverload,
    ResponseTimeout,
    ScpiErrorReported,
)
from benchrun.transport import Transport

OVERLOAD_THRESHOLD = 9.9e37
MAX_ERRORS_DRAINED = 32

_ERROR_RE = re.compile(r'^\s*([+-]?\d+)\s*,\s*"?(.*?)"?\s*$')


def action(func: Callable) -> Callable:
    """Mark a driver method as callable from a plan with ``{"do": ...}``."""
    func._bench_kind = "action"  # type: ignore[attr-defined]
    return func


def reading(unit: Optional[str]) -> Callable:
    """Mark a driver method as readable from a plan, returning a value in ``unit``.

    ``None`` means the unit is whatever the plan declares (raw queries).
    """

    def mark(func: Callable) -> Callable:
        func._bench_kind = "reading"  # type: ignore[attr-defined]
        func._bench_unit = unit  # type: ignore[attr-defined]
        return func

    return mark


def parse_error_line(line: str) -> Optional[Tuple[int, str]]:
    """Parse a ``SYST:ERR?`` response into code and message."""
    found = _ERROR_RE.match(line)
    if not found:
        return None
    return int(found.group(1)), found.group(2)


class Instrument:
    """Any SCPI instrument. Also usable directly through the ``scpi`` driver."""

    COMMANDS: Dict[str, str] = {
        "identify": "*IDN?",
        "reset": "*RST",
        "clear": "*CLS",
        "next_error": "SYST:ERR?",
    }

    def __init__(self, transport: Transport, name: str = "instrument", commands: Optional[Mapping[str, str]] = None):
        self.transport = transport
        self.name = name
        self.commands: Dict[str, str] = dict(self.COMMANDS)
        if commands:
            self.commands.update(commands)

    # Describing what plans can do ----------------------------------------

    @classmethod
    def _members(cls, kind: str) -> Dict[str, Callable]:
        found = {}
        for name, func in inspect.getmembers(cls, predicate=inspect.isfunction):
            if getattr(func, "_bench_kind", None) == kind:
                found[name] = func
        return found

    @classmethod
    def actions(cls) -> Dict[str, Callable]:
        return cls._members("action")

    @classmethod
    def readings(cls) -> Dict[str, Callable]:
        return cls._members("reading")

    @classmethod
    def bind_problem(cls, func: Callable, kwargs: Mapping[str, Any]) -> Optional[str]:
        """Explain why ``kwargs`` do not fit ``func``, or return None."""
        signature = inspect.signature(func)
        try:
            signature.bind(None, **kwargs)
        except TypeError as exc:
            return str(exc)
        for name, value in kwargs.items():
            wants_number = any(word in str(signature.parameters[name].annotation) for word in ("float", "int"))
            is_expression = isinstance(value, str) and value.startswith("=")
            if wants_number and not is_expression and (isinstance(value, bool) or not isinstance(value, (int, float))):
                return f"argument {name!r} must be a number or an expression, got {value!r}"
        return None

    @classmethod
    def override_problem(cls, key: str, template: str) -> Optional[str]:
        """Explain why a plan's command override is not usable, or return None."""
        if key not in cls.COMMANDS:
            return f"unknown command key {key!r}; available: {', '.join(sorted(cls.COMMANDS))}"
        allowed = {field for _, field, _, _ in string.Formatter().parse(cls.COMMANDS[key]) if field}
        try:
            used = {field for _, field, _, _ in string.Formatter().parse(template) if field}
        except ValueError as exc:
            return f"bad template for {key!r}: {exc}"
        extra = used - allowed
        if extra:
            return f"template for {key!r} uses unknown fields {sorted(extra)}; allowed: {sorted(allowed)}"
        return None

    # Connection ----------------------------------------------------------

    def _tag(self, exc: InstrumentFault) -> InstrumentFault:
        exc.instrument = self.name
        return exc

    def connect(self) -> None:
        try:
            self.transport.open()
            self.transport.discard_pending()  # Some instruments greet on connect.
        except InstrumentFault as exc:
            raise self._tag(exc)

    def close(self) -> None:
        self.transport.close()

    # Raw exchange --------------------------------------------------------

    def _render(self, key: str, **fields: Any) -> str:
        return self.commands[key].format(**fields)

    def _send(self, text: str) -> None:
        try:
            self._guard_stale()
            self.transport.write_line(text)
        except InstrumentFault as exc:
            raise self._tag(exc)

    def _guard_stale(self) -> None:
        """Late or unexpected data would misalign every response that follows."""
        stale = self.transport.discard_pending()
        if stale:
            raise BadResponse(f"unexpected data waiting from the instrument: {stale[:80]!r}", self.name)

    def _ask(self, text: str) -> str:
        self._send(text)
        try:
            return self.transport.read_line()
        except ResponseTimeout as exc:
            self._tag(exc)
            exc.message = f"no response to {text!r} ({exc.message})" + self._error_context()
            raise
        except InstrumentFault as exc:
            exc.message = f"{exc.message} (while waiting for a response to {text!r})"
            raise self._tag(exc)

    def _error_context(self) -> str:
        """After a timeout, ask the error queue why. Failure here adds nothing."""
        try:
            errors = self._drain_errors()
        except InstrumentFault:
            return ""
        if not errors:
            return ""
        return "; instrument error queue: " + "; ".join(f'{code},"{msg}"' for code, msg in errors)

    def _drain_errors(self) -> List[Tuple[int, str]]:
        errors: List[Tuple[int, str]] = []
        query = self.commands["next_error"]
        for _ in range(MAX_ERRORS_DRAINED):
            self._send(query)
            try:
                line = self.transport.read_line()
            except InstrumentFault as exc:
                raise self._tag(exc)
            parsed = parse_error_line(line)
            if parsed is None:
                raise BadResponse(f"cannot read error queue response {line!r}", self.name)
            if parsed[0] == 0:
                return errors
            errors.append(parsed)
        return errors

    def check_errors(self, command: Optional[str] = None) -> None:
        """Raise ``ScpiErrorReported`` when the error queue holds anything."""
        errors = self._drain_errors()
        if errors:
            raise ScpiErrorReported(errors, command, self.name)

    def command(self, key: str, **fields: Any) -> None:
        """Send a command from the table, then confirm the instrument accepted it."""
        text = self._render(key, **fields)
        self._send(text)
        self.check_errors(text)

    def _number(self, text: str) -> float:
        """Ask ``text``, parse a number, and confirm no error was raised meanwhile."""
        response = self._ask(text)
        value = self._parse_number(response, text)
        self.check_errors(text)
        return value

    def _parse_number(self, response: str, command: str) -> float:
        try:
            value = float(response.strip())
        except ValueError:
            raise BadResponse(f"non-numeric response {response[:80]!r} to {command!r}", self.name) from None
        if not math.isfinite(value):
            raise BadResponse(f"non-finite response {response[:80]!r} to {command!r}", self.name)
        if abs(value) >= OVERLOAD_THRESHOLD:
            raise ReadingOverload(f"reading overload ({response.strip()}) from {command!r}", self.name)
        return value

    def number(self, key: str, **fields: Any) -> float:
        return self._number(self._render(key, **fields))

    # Common commands -----------------------------------------------------

    def identify(self) -> str:
        return self._ask(self.commands["identify"]).strip()

    def reset(self) -> None:
        """Clear status, restore defaults, and confirm the queue is clean."""
        self.command("clear")
        self.command("reset")

    def safe_state(self) -> None:
        """Leave the instrument harmless. Run at the end of every run."""

    @action
    def write(self, command: str) -> None:
        """Send any SCPI command text, then check the error queue."""
        self._send(command)
        self.check_errors(command)

    @reading(None)
    def query_number(self, command: str) -> float:
        """Send any SCPI query text and read a number back."""
        return self._number(command)
