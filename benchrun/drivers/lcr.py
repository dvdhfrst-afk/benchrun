"""LCR meter driver.

Command forms and the reply format follow the E4980A/E4980AL User's Guide,
document E4980-90230, Edition 16 (October 2019). See docs/drivers.md. Never run
on the hardware.
"""

from __future__ import annotations

from typing import Optional, Tuple

from benchrun.drivers.base import Instrument, action, reading
from benchrun.errors import BadResponse, CommandRefused, ReadingOverload

FUNCTIONS = (
    "CPD", "CPQ", "CPG", "CPRP", "CSD", "CSQ", "CSRS",
    "LPD", "LPQ", "LPG", "LPRP", "LPRD", "LSD", "LSQ", "LSRS", "LSRD",
    "RX", "ZTD", "ZTR", "GB", "YTD", "YTR", "VDID",
)  # fmt: skip

_STATUS_TEXT = {
    -1: "no data in the buffer",
    3: "the signal source is overloaded",
    4: "the automatic level control cannot regulate",
}


class LcrMeter(Instrument):
    """An LCR meter: choose what to measure, trigger, and fetch two values and a status."""

    COMMANDS = {
        **Instrument.COMMANDS,
        "function": "FUNC:IMP {function}",
        "frequency": "FREQ {hertz:.12g}",
        "level": "VOLT {volts:.12g}",
        "trigger_source": "TRIG:SOUR {source}",
        "initiate": "INIT",
        "trigger": "TRIG:IMM",
        "fetch": "FETC?",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._function: Optional[str] = None

    def reset(self) -> None:
        super().reset()
        self._function = None

    def _forget_settings(self) -> None:
        self._function = None

    @action
    def configure(self, function: str, hertz: float, volts: float) -> None:
        """Choose the pair of values (``CPD``, ``LSQ``, ``RX`` and others), test frequency and level."""
        if function not in FUNCTIONS:
            raise CommandRefused(f"unknown LCR function {function!r}; available: {', '.join(FUNCTIONS)}", self.name)
        self.command("function", function=function)
        self.command("frequency", hertz=hertz)
        self.command("level", volts=volts)
        self.command("trigger_source", source="BUS")
        self._function = function

    def _fetch(self) -> Tuple[float, float]:
        self.command("initiate")
        self.command("trigger")
        query = self.commands["fetch"]
        reply = self._ask(query)
        fields = reply.split(",")
        # A fourth field, the bin number, appears when the comparator is on.
        if len(fields) not in (3, 4):
            raise BadResponse(f"expected primary,secondary,status from {query!r}, got {reply[:80]!r}", self.name)
        status = self._parse_number(fields[2], query)
        if status == 1:
            raise ReadingOverload(f"the meter reports overload (status {fields[2].strip()})", self.name)
        if status != 0:
            why = _STATUS_TEXT.get(int(status), "unknown status")
            raise BadResponse(f"the meter reports status {fields[2].strip()}: {why}", self.name)
        values = self._parse_number(fields[0], query), self._parse_number(fields[1], query)
        self.check_errors(query)
        return values

    def _primary(self, wanted: str, starts: Tuple[str, ...]) -> float:
        """The first value, once it is sure that value means what the plan says."""
        if self._function is None:
            raise CommandRefused(f"{wanted} needs configure first", self.name)
        if not self._function.startswith(starts):
            raise CommandRefused(f"{wanted} cannot come from function {self._function}", self.name)
        return self._fetch()[0]

    @reading("F")
    def measure_capacitance(self) -> float:
        return self._primary("capacitance", ("C",))

    @reading("H")
    def measure_inductance(self) -> float:
        return self._primary("inductance", ("L",))

    @reading("Ohm")
    def measure_impedance(self) -> float:
        """The resistance or impedance value of ``RX``, ``ZTD`` or ``ZTR``."""
        return self._primary("impedance", ("R", "Z"))

    @reading("")
    def measure_primary(self) -> float:
        """The first value of any function, such as G with ``GB``. Its unit depends on the function."""
        if self._function is None:
            raise CommandRefused("the first value needs configure first", self.name)
        return self._fetch()[0]

    @reading("")
    def measure_secondary(self) -> float:
        """The second value, such as D with ``CPD`` or Q with ``LSQ``. Its unit depends on the function."""
        if self._function is None:
            raise CommandRefused("the second value needs configure first", self.name)
        return self._fetch()[1]
