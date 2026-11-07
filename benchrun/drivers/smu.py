"""Source measure unit driver.

Command forms follow the Series 2400 SourceMeter User's Manual, document
2400S-900-01 Rev. K (September 2011). See docs/drivers.md. Never run on the hardware.
"""

from __future__ import annotations

from typing import Optional

from benchrun.drivers.base import Instrument, action, reading
from benchrun.errors import CommandRefused


class SourceMeasureUnit(Instrument):
    """A source measure unit: source volts or amps with a limit, and measure the other."""

    COMMANDS = {
        **Instrument.COMMANDS,
        "source_function": ":SOUR:FUNC {function}",
        "source_voltage": ":SOUR:VOLT:LEV {volts:.12g}",
        "source_current": ":SOUR:CURR:LEV {amps:.12g}",
        "current_limit": ":SENS:CURR:PROT {amps:.12g}",
        "voltage_limit": ":SENS:VOLT:PROT {volts:.12g}",
        "sense_function": ':SENS:FUNC "{function}"',
        "elements": ":FORM:ELEM {function}",
        "output": ":OUTP {state}",
        "output_state": ":OUTP?",
        "read": ":READ?",
        "current_tripped": ":SENS:CURR:PROT:TRIP?",
        "voltage_tripped": ":SENS:VOLT:PROT:TRIP?",
    }

    HAS_OUTPUT = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sensing: Optional[str] = None
        self._sourcing: Optional[str] = None

    def reset(self) -> None:
        super().reset()
        self._sensing = None
        self._sourcing = None

    def _forget_settings(self) -> None:
        self._sensing = None

    @action
    def source_voltage(self, volts: float, current_limit_a: float) -> None:
        """Source volts, never letting the current pass the limit."""
        self.command("source_function", function="VOLT")
        self.command("current_limit", amps=current_limit_a)
        self.command("source_voltage", volts=volts)
        self._sourcing = "VOLT"

    @action
    def source_current(self, amps: float, voltage_limit_v: float) -> None:
        """Source amps, never letting the voltage pass the limit."""
        self.command("source_function", function="CURR")
        self.command("voltage_limit", volts=voltage_limit_v)
        self.command("source_current", amps=amps)
        self._sourcing = "CURR"

    @action
    def output_on(self) -> None:
        self.command("output", state="ON")

    @action
    def output_off(self) -> None:
        self.command("output", state="OFF")

    def _read_as(self, function: str) -> float:
        if self._sensing != function:
            self._sensing = None
            self.command("sense_function", function=function)
            self.command("elements", function=function)
            self._sensing = function
        return self.number("read")

    @reading("A")
    def measure_current(self) -> float:
        return self._read_as("CURR")

    @reading("V")
    def measure_voltage(self) -> float:
        return self._read_as("VOLT")

    @reading("")
    def at_limit(self) -> float:
        """1 when the output is being held back by its limit, else 0. Use ``"high": 0``."""
        if self._sourcing is None:
            raise CommandRefused("at_limit needs source_voltage or source_current first", self.name)
        return self.number("current_tripped" if self._sourcing == "VOLT" else "voltage_tripped")

    def safe_state(self) -> None:
        self.output_off()
        self._confirm_off("output_state")
