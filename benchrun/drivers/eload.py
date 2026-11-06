"""Electronic load driver."""

from __future__ import annotations

from benchrun.drivers.base import Instrument, action, reading


class ElectronicLoad(Instrument):
    """A constant-current electronic load."""

    COMMANDS = {
        **Instrument.COMMANDS,
        "set_current": "CURR {amps:.12g}",
        "input": "INP {state}",
        "measure_voltage": "MEAS:VOLT?",
        "measure_current": "MEAS:CURR?",
    }

    @action
    def set_current(self, amps: float) -> None:
        self.command("set_current", amps=amps)

    @action
    def input_on(self) -> None:
        self.command("input", state="ON")

    @action
    def input_off(self) -> None:
        self.command("input", state="OFF")

    @reading("V")
    def measure_voltage(self) -> float:
        return self.number("measure_voltage")

    @reading("A")
    def measure_current(self) -> float:
        return self.number("measure_current")

    def safe_state(self) -> None:
        self.input_off()
