"""A real SCPI command parser for the simulated instruments.

Handles what instruments are expected to handle on the wire:

* short and long mnemonic forms (``VOLT`` and ``VOLTAGE``), case-insensitive
* optional nodes (``[SOURce]:VOLTage[:LEVel]``) and numeric suffixes (``CHANnel2``)
* compound messages with ``;`` and the relative path rule (``SOUR:VOLT 5;CURR 1``)
* numeric parameters with engineering suffixes and ``MIN``, ``MAX``, ``DEF``
* IEEE 488.2 common commands (``*IDN?``, ``*RST``, ``*CLS``, ``*ESR?`` and friends)
* the error queue read through ``SYSTem:ERRor[:NEXT]?``

A query with a bad header produces no response, as on real instruments; the
controller finds out by timing out and reading the error queue.
"""

from __future__ import annotations

import inspect
import re
from collections import deque
from typing import Callable, Deque, List, Optional, Sequence, Tuple

ERROR_QUEUE_SIZE = 20
OVERLOAD_VALUE = 9.9e37

# *ESR? bits
ESR_OPC = 0x01
ESR_QUERY_ERROR = 0x04
ESR_DEVICE_ERROR = 0x08
ESR_EXECUTION_ERROR = 0x10
ESR_COMMAND_ERROR = 0x20

_HEADER_RE = re.compile(r"^:?[A-Za-z][A-Za-z0-9]*(:[A-Za-z][A-Za-z0-9]*)*\??$")
_COMMON_RE = re.compile(r"^\*[A-Za-z]+\??$")
_NUMBER_RE = re.compile(r"^([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*([A-Za-z]*)$")
_NODE_RE = re.compile(r"\[:?([A-Za-z#]+)\]|:?([A-Za-z#]+)")
_SUFFIX_RE = re.compile(r"^([A-Za-z]+?)(\d*)$")


class ScpiError(Exception):
    """A command could not be carried out. Goes onto the instrument's error queue."""

    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f'{code},"{message}"')


class DeviceDisconnect(Exception):
    """Raised by an injected fault: close the connection without answering."""


class DeviceHang(Exception):
    """Raised by an injected fault: stop answering."""


def scpi(pattern: str, query: bool = False, nargs: Tuple[int, int] = (0, 0)) -> Callable:
    """Register a device method as the handler for a SCPI command or query."""

    def mark(func: Callable) -> Callable:
        registrations = getattr(func, "_scpi", [])
        registrations.append((pattern, query, nargs))
        func._scpi = registrations  # type: ignore[attr-defined]
        return func

    return mark


class _Node:
    """One mnemonic of a command pattern, such as ``CHANnel#``."""

    def __init__(self, spec: str, optional: bool):
        self.optional = optional
        self.numeric = spec.endswith("#")
        spec = spec.rstrip("#")
        self.long = spec.upper()
        self.short = "".join(ch for ch in spec if ch.isupper())

    def match(self, token: str) -> Optional[List[int]]:
        """Return the numeric suffix list when the token matches, else None."""
        upper = token.upper()
        if not self.numeric:
            return [] if upper in (self.long, self.short) else None
        found = _SUFFIX_RE.match(upper)
        if not found or found.group(1) not in (self.long, self.short):
            return None
        return [int(found.group(2)) if found.group(2) else 1]


def _compile_pattern(pattern: str) -> List[_Node]:
    nodes = []
    for optional, required in _NODE_RE.findall(pattern):
        nodes.append(_Node(optional or required, bool(optional)))
    return nodes


def _match_nodes(nodes: Sequence[_Node], tokens: Sequence[str]) -> Optional[List[int]]:
    def walk(i: int, j: int, suffixes: List[int]) -> Optional[List[int]]:
        if i == len(nodes):
            return suffixes if j == len(tokens) else None
        node = nodes[i]
        if j < len(tokens):
            got = node.match(tokens[j])
            if got is not None:
                result = walk(i + 1, j + 1, suffixes + got)
                if result is not None:
                    return result
        if node.optional:
            return walk(i + 1, j, suffixes)
        return None

    return walk(0, 0, [])


def split_outside_quotes(text: str, separator: str) -> List[str]:
    """Split on ``separator``, leaving quoted strings intact."""
    parts: List[str] = []
    current: List[str] = []
    quote = ""
    for ch in text:
        if quote:
            current.append(ch)
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
            current.append(ch)
        elif ch == separator:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if quote:
        raise ScpiError(-150, "String data error; unterminated string")
    parts.append("".join(current))
    return parts


def _suffix_multiplier(suffix: str, unit: str) -> float:
    upper = suffix.upper()
    if not upper:
        return 1.0
    base = unit.upper()
    if not base or not upper.endswith(base):
        raise ScpiError(-131, f"Invalid suffix; {suffix}")
    prefix = upper[: -len(base)]
    # SCPI suffixes are case-insensitive, so M means milli, except that MHZ and
    # MOHM conventionally mean mega; MA is mega everywhere else.
    if prefix == "M" and base in ("HZ", "OHM"):
        return 1e6
    table = {"": 1.0, "P": 1e-12, "N": 1e-9, "U": 1e-6, "M": 1e-3, "K": 1e3, "MA": 1e6, "G": 1e9}
    if prefix not in table:
        raise ScpiError(-131, f"Invalid suffix; {suffix}")
    return table[prefix]


def parse_number(
    text: str,
    unit: str = "",
    low: Optional[float] = None,
    high: Optional[float] = None,
    default: Optional[float] = None,
) -> float:
    """Parse a numeric parameter, range-checking it against ``low`` and ``high``."""
    word = text.strip().upper()
    if word in ("MIN", "MINIMUM") and low is not None:
        return low
    if word in ("MAX", "MAXIMUM") and high is not None:
        return high
    if word in ("DEF", "DEFAULT") and default is not None:
        return default
    found = _NUMBER_RE.match(text.strip())
    if not found:
        raise ScpiError(-104, f"Data type error; expected a number, got {text.strip()}")
    value = float(found.group(1)) * _suffix_multiplier(found.group(2), unit)
    if (low is not None and value < low) or (high is not None and value > high):
        raise ScpiError(-222, f"Data out of range; {value:g} not in {low} to {high}")
    return value


def parse_bool(text: str) -> bool:
    word = text.strip().upper()
    if word in ("ON", "1"):
        return True
    if word in ("OFF", "0"):
        return False
    raise ScpiError(-104, f"Data type error; expected ON or OFF, got {text.strip()}")


def parse_choice(text: str, choices: Sequence[str]) -> str:
    """Match a character parameter against ``choices`` given in ``LONGer`` form."""
    word = text.strip().upper()
    for choice in choices:
        short = "".join(ch for ch in choice if ch.isupper())
        if word in (choice.upper(), short):
            return short
    raise ScpiError(-224, f"Illegal parameter value; {text.strip()}")


def format_real(value: float) -> str:
    """Format a measured value in the usual NR3 style."""
    return f"{value:+.9E}"


class ScpiDevice:
    """Base class for a simulated instrument: parsing, dispatch and status."""

    idn = "benchrun,SIM-DEVICE,SIM0000,1.0"

    def __init__(self) -> None:
        self._errors: Deque[Tuple[int, str]] = deque()
        self._esr = 0
        self._ese = 0
        self._sre = 0
        self._commands: List[Tuple[List[_Node], bool, Tuple[int, int], Callable]] = []
        for _, method in inspect.getmembers(self, predicate=inspect.ismethod):
            for pattern, query, nargs in getattr(method, "_scpi", []):
                self._commands.append((_compile_pattern(pattern), query, nargs, method))

    def reset(self) -> None:
        """Return settings to their defaults. Overridden by each instrument."""

    # Message handling ----------------------------------------------------

    def process(self, line: str) -> Optional[str]:
        """Run one program message. Returns the response line, or None."""
        responses: List[str] = []
        path: List[str] = []
        try:
            units = split_outside_quotes(line, ";")
        except ScpiError as exc:
            self.push_error(exc.code, exc.message)
            return None
        for unit in units:
            unit = unit.strip()
            if not unit:
                continue
            try:
                response, path = self._execute(unit, path)
            except ScpiError as exc:
                # The rest of a message is not attempted after an error.
                self.push_error(exc.code, exc.message)
                break
            if response is not None:
                responses.append(response)
        return ";".join(responses) if responses else None

    def _execute(self, unit: str, path: List[str]) -> Tuple[Optional[str], List[str]]:
        pieces = unit.split(None, 1)
        header = pieces[0]
        params_text = pieces[1] if len(pieces) > 1 else ""
        args = self._parse_args(params_text)
        if header.startswith("*"):
            if not _COMMON_RE.match(header):
                raise ScpiError(-102, f"Syntax error; {header}")
            return self._execute_common(header.upper(), args), path
        if not _HEADER_RE.match(header):
            raise ScpiError(-102, f"Syntax error; {header}")
        is_query = header.endswith("?")
        absolute = header.startswith(":")
        tokens = header.strip(":").rstrip("?").split(":")
        full = tokens if absolute else path + tokens
        for nodes, query, nargs, method in self._commands:
            if query != is_query:
                continue
            suffixes = _match_nodes(nodes, full)
            if suffixes is None:
                continue
            if len(args) < nargs[0]:
                raise ScpiError(-109, f"Missing parameter; {header}")
            if len(args) > nargs[1]:
                raise ScpiError(-108, f"Parameter not allowed; {header}")
            try:
                response = method(args, suffixes)
            except (ScpiError, DeviceDisconnect, DeviceHang):
                raise
            except Exception as exc:
                raise ScpiError(-300, f"Device-specific error; {exc}") from exc
            return response, full[:-1]
        raise ScpiError(-113, f"Undefined header; {header}")

    @staticmethod
    def _parse_args(text: str) -> List[str]:
        if not text.strip():
            return []
        args = [piece.strip() for piece in split_outside_quotes(text, ",")]
        if any(not arg for arg in args):
            raise ScpiError(-104, "Data type error; empty parameter")
        return args

    def _execute_common(self, header: str, args: List[str]) -> Optional[str]:
        if header in ("*ESE", "*SRE"):
            if len(args) != 1:
                raise ScpiError(-109, f"Missing parameter; {header}")
            value = int(parse_number(args[0], low=0, high=255))
            if header == "*ESE":
                self._ese = value
            else:
                self._sre = value
            return None
        if args:
            raise ScpiError(-108, f"Parameter not allowed; {header}")
        if header == "*IDN?":
            return self.idn
        if header == "*RST":
            self.reset()
            return None
        if header == "*CLS":
            self._errors.clear()
            self._esr = 0
            return None
        if header == "*OPC":
            self._esr |= ESR_OPC
            return None
        if header == "*OPC?":
            return "1"
        if header == "*WAI":
            return None
        if header == "*TST?":
            return "0"
        if header == "*ESR?":
            value = self._esr
            self._esr = 0
            return str(value)
        if header == "*ESE?":
            return str(self._ese)
        if header == "*SRE?":
            return str(self._sre)
        if header == "*STB?":
            return str(self._status_byte())
        raise ScpiError(-113, f"Undefined header; {header}")

    def _status_byte(self) -> int:
        status = 0
        if self._errors:
            status |= 0x04
        if self._esr & self._ese:
            status |= 0x20
        return status

    # Error queue ---------------------------------------------------------

    def push_error(self, code: int, message: str) -> None:
        if -199 <= code <= -100:
            self._esr |= ESR_COMMAND_ERROR
        elif -299 <= code <= -200:
            self._esr |= ESR_EXECUTION_ERROR
        elif -399 <= code <= -300 or code > 0:
            self._esr |= ESR_DEVICE_ERROR
        elif -499 <= code <= -400:
            self._esr |= ESR_QUERY_ERROR
        if len(self._errors) >= ERROR_QUEUE_SIZE:
            # The standard keeps the oldest errors and flags the loss in the last slot.
            self._errors[-1] = (-350, "Queue overflow")
            return
        self._errors.append((code, message))

    @scpi("SYSTem:ERRor[:NEXT]", query=True)
    def _system_error(self, args: List[str], suffixes: List[int]) -> str:
        if not self._errors:
            return '+0,"No error"'
        code, message = self._errors.popleft()
        safe = message.replace('"', "'")
        return f'{code:+d},"{safe}"'

    @scpi("SYSTem:VERSion", query=True)
    def _system_version(self, args: List[str], suffixes: List[int]) -> str:
        return "1999.0"
