"""Exception types shared across benchrun."""

from __future__ import annotations

from typing import Iterable, List, Optional, Tuple


class BenchrunError(Exception):
    """Base class for every error benchrun raises on purpose."""


class PlanError(BenchrunError):
    """A test plan or address file is not usable. Holds every problem found."""

    def __init__(self, problems: Iterable[str]):
        self.problems: List[str] = list(problems)
        super().__init__("; ".join(self.problems))


class ExpressionError(BenchrunError):
    """A plan expression could not be parsed or evaluated."""


class InstrumentFault(BenchrunError):
    """Something went wrong talking to an instrument.

    Any of these ends the current step with a readable result and marks the
    run as aborted, because readings after a fault cannot be trusted.
    """

    def __init__(self, message: str, instrument: Optional[str] = None):
        self.message = message
        self.instrument = instrument
        super().__init__(message)

    def __str__(self) -> str:
        if self.instrument:
            return f"{self.instrument}: {self.message}"
        return self.message


class ConnectionLost(InstrumentFault):
    """The socket could not be opened, or closed underneath us."""


class ResponseTimeout(InstrumentFault):
    """The instrument did not answer a query in time."""


class BadResponse(InstrumentFault):
    """The instrument answered with something that cannot be interpreted."""


class ReadingOverload(InstrumentFault):
    """The instrument returned the SCPI overload value (9.9E37)."""


class IdentityMismatch(InstrumentFault):
    """The ``*IDN?`` response does not match what the plan expects."""


class ScpiErrorReported(InstrumentFault):
    """The instrument's error queue held one or more errors."""

    def __init__(
        self,
        errors: List[Tuple[int, str]],
        command: Optional[str] = None,
        instrument: Optional[str] = None,
    ):
        self.errors = errors
        self.command = command
        text = "; ".join(f'{code},"{msg}"' for code, msg in errors)
        where = f" after {command!r}" if command else ""
        super().__init__(f"instrument reported error{where}: {text}", instrument)
