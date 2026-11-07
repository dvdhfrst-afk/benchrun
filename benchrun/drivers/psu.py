"""Programmable power supply driver."""

from __future__ import annotations

from typing import Optional

from benchrun.drivers.base import Instrument, action, reading


class PowerSupply(Instrument):
    """A single-output supply using ``VOLT``, ``CURR``, ``OUTP`` and ``MEAS``."""

    HAS_OUTPUT = True

    COMMANDS = {
        **Instrument.COMMANDS,
        "set_voltage": "VOLT {volts:.12g}",
        "set_current_limit": "CURR {amps:.12g}",
        "output": "OUTP {state}",
        "output_state": "OUTP?",
        "measure_voltage": "MEAS:VOLT?",
        "measure_current": "MEAS:CURR?",
        "operation_condition": "STAT:OPER:COND?",
        "constant_current_bit": "10",
    }

    @classmethod
    def override_problem(cls, key: str, template: str) -> Optional[str]:
        if key == "constant_current_bit" and not (template.isdigit() and int(template) < 32):
            return f"constant_current_bit must be a bit number from 0 to 31, got {template!r}"
        return super().override_problem(key, template)

    @action
    def set_voltage(self, volts: float) -> None:
        self.command("set_voltage", volts=volts)

    @action
    def set_current_limit(self, amps: float) -> None:
        self.command("set_current_limit", amps=amps)

    @action
    def output_on(self) -> None:
        self.command("output", state="ON")

    @action
    def output_off(self) -> None:
        self.command("output", state="OFF")

    @reading("V")
    def measure_voltage(self) -> float:
        return self.number("measure_voltage")

    @reading("A")
    def measure_current(self) -> float:
        return self.number("measure_current")

    @reading("")
    def current_limited(self) -> float:
        """1 when the supply is in constant current, else 0. Use ``"high": 0``."""
        condition = int(self.number("operation_condition"))
        return 1.0 if condition >> int(self.commands["constant_current_bit"]) & 1 else 0.0

    def safe_state(self) -> None:
        self.output_off()
        self._confirm_off("output_state")
