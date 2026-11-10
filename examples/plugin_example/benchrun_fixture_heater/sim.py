"""The FH-1 fixture heater on the simulated bench, so that ``run --sim`` works with its driver."""

from __future__ import annotations

from typing import List

from benchrun.sim.instruments import SimInstrument
from benchrun.sim.scpi import format_real, parse_bool, parse_number, scpi


class SimFixtureHeater(SimInstrument):
    """The plate reaches its set point at once while the heater is on, and room temperature while it is off."""

    kind = "fixture_heater"  # The name of the driver it simulates.
    idn = "benchrun-example,FH-1,SIM0100,1.0"
    ROOM_C = 23.0
    NOISE_C = 0.05

    def reset(self) -> None:
        self.setpoint = 25.0
        self.on = False

    @scpi("TEMPerature", nargs=(1, 1))
    def _set_temperature(self, args: List[str], suffixes: List[int]) -> None:
        self.setpoint = parse_number(args[0], low=0.0, high=125.0, default=25.0)

    @scpi("TEMPerature", query=True)
    def _get_temperature(self, args: List[str], suffixes: List[int]) -> str:
        return format_real(self.setpoint)

    @scpi("OUTPut[:STATe]", nargs=(1, 1))
    def _set_output(self, args: List[str], suffixes: List[int]) -> None:
        self.on = parse_bool(args[0])

    @scpi("OUTPut[:STATe]", query=True)
    def _get_output(self, args: List[str], suffixes: List[int]) -> str:
        return "1" if self.on else "0"

    @scpi("MEASure:TEMPerature", query=True)
    def _measure(self, args: List[str], suffixes: List[int]) -> str:
        plate = self.setpoint if self.on else self.ROOM_C
        return self.measured(plate + self.gauss(self.NOISE_C))
