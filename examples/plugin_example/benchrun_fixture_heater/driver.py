"""Driver for the FH-1 fixture heater, a made-up instrument that warms the plate a unit sits on."""

from __future__ import annotations

from benchrun.drivers.base import Instrument, action, reading
from benchrun.errors import CommandRefused


class FixtureHeater(Instrument):
    """FH-1 fixture heater (made up): ``TEMP`` sets the plate, ``OUTP`` switches the heater, ``MEAS:TEMP?`` reads it."""

    # It can drive something, so benchrun switches it off at the end of every run through safe_state.
    HAS_OUTPUT = True
    MAX_CELSIUS = 125.0

    # Every command it sends, so that a plan can replace one for a heater with another dialect.
    COMMANDS = {
        **Instrument.COMMANDS,
        "set_temperature": "TEMP {celsius:.6g}",
        "output": "OUTP {state}",
        "output_state": "OUTP?",
        "measure_temperature": "MEAS:TEMP?",
    }

    @action
    def set_temperature(self, celsius: float) -> None:
        """Set the plate temperature in degrees Celsius."""
        if not 0.0 <= celsius <= self.MAX_CELSIUS:
            raise CommandRefused(f"plate temperature must be from 0 to {self.MAX_CELSIUS:g} C, got {celsius:g}", self.name)
        self.command("set_temperature", celsius=celsius)

    @action
    def heater_on(self) -> None:
        self.command("output", state="ON")

    @action
    def heater_off(self) -> None:
        self.command("output", state="OFF")

    @reading("degC")
    def measure_temperature(self) -> float:
        return self.number("measure_temperature")

    def safe_state(self) -> None:
        """Switch the heater off, then ask whether it really is off."""
        self.heater_off()
        self._confirm_off("output_state")
