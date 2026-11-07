"""Multi-output power supply driver.

Command forms follow the E36300 Series Programming Guide, document E36311-90008,
Edition 3 (October 2017). See docs/drivers.md. Never run on the hardware.
"""

from __future__ import annotations

from typing import Set

from benchrun.drivers.base import Instrument, action, reading, whole_number
from benchrun.errors import InstrumentFault

_MODE_CONSTANT_CURRENT = 1
_MODE_HARDWARE_FAILURE = 3


class MultiOutputSupply(Instrument):
    """A supply with several outputs, addressed with channel lists such as ``(@2)``.

    Every output this driver touched is switched off when a run ends, and each is
    confirmed off. An output turned on by raw ``write`` is not known to the driver.
    If a plan does that, set the ``all_off`` command, such as ``OUTP OFF,(@1:3)``,
    and it is sent as well.
    """

    COMMANDS = {
        **Instrument.COMMANDS,
        "set_voltage": "VOLT {volts:.12g},(@{channel:d})",
        "set_current_limit": "CURR {amps:.12g},(@{channel:d})",
        "output": "OUTP {state},(@{channel:d})",
        "output_state": "OUTP? (@{channel:d})",
        "measure_voltage": "MEAS:VOLT? (@{channel:d})",
        "measure_current": "MEAS:CURR? (@{channel:d})",
        "regulation_mode": "STAT:QUES:INST:ISUM{channel:d}:COND?",
        "all_off": "",
    }

    HAS_OUTPUT = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._used: Set[int] = set()

    def reset(self) -> None:
        super().reset()
        self._used.clear()

    def _channel(self, channel: float) -> int:
        number = whole_number(channel, "channel", self.name)
        self._used.add(number)
        return number

    @action
    def set_voltage(self, channel: int, volts: float) -> None:
        self.command("set_voltage", channel=self._channel(channel), volts=volts)

    @action
    def set_current_limit(self, channel: int, amps: float) -> None:
        self.command("set_current_limit", channel=self._channel(channel), amps=amps)

    @action
    def output_on(self, channel: int) -> None:
        self.command("output", channel=self._channel(channel), state="ON")

    @action
    def output_off(self, channel: int) -> None:
        self.command("output", channel=self._channel(channel), state="OFF")

    @reading("V")
    def measure_voltage(self, channel: int) -> float:
        return self.number("measure_voltage", channel=whole_number(channel, "channel", self.name))

    @reading("A")
    def measure_current(self, channel: int) -> float:
        return self.number("measure_current", channel=whole_number(channel, "channel", self.name))

    @reading("")
    def current_limited(self, channel: int) -> float:
        """1 when this output is in constant current, else 0. Use ``"high": 0``."""
        number = whole_number(channel, "channel", self.name)
        mode = int(self.number("regulation_mode", channel=number))
        if mode == _MODE_HARDWARE_FAILURE:
            raise InstrumentFault(f"output {number} reports a hardware failure", self.name)
        return 1.0 if mode == _MODE_CONSTANT_CURRENT else 0.0

    def safe_state(self) -> None:
        """Every output this run touched goes off. One refusal must not stop the rest."""
        problems = []
        if self.commands["all_off"]:
            try:
                self.command("all_off")
            except InstrumentFault as exc:
                problems.append(exc.message)
        for number in sorted(self._used):
            try:
                self.command("output", channel=number, state="OFF")
                self._confirm_off("output_state", channel=number)
            except InstrumentFault as exc:
                problems.append(f"output {number}: {exc.message}")
        if problems:
            raise InstrumentFault("; ".join(problems), self.name)
