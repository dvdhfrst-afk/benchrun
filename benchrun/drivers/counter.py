"""Frequency counter driver.

Command forms follow the 53220A/53230A User's Guide, document 53220-90001,
Edition 3 (September 2017). See docs/drivers.md. Never run on the hardware.
"""

from __future__ import annotations

from typing import Optional, Tuple

from benchrun.drivers.base import Instrument, action, reading, whole_number


class FrequencyCounter(Instrument):
    """A frequency counter: ``CONF:FREQ``, a gate time, and ``READ?``."""

    COMMANDS = {
        **Instrument.COMMANDS,
        "configure_frequency": "CONF:FREQ {expected},{resolution},(@{channel:d})",
        "gate_time": "SENS:FREQ:GATE:TIME {seconds:.12g}",
        "read": "READ?",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._configured: Optional[Tuple[int, Optional[float], Optional[float]]] = None
        self._gate_s: Optional[float] = None

    def reset(self) -> None:
        super().reset()
        self._configured = None
        self._gate_s = None

    def _forget_settings(self) -> None:
        self._configured = None

    @action
    def set_gate_time(self, seconds: float) -> None:
        """How long each reading counts for. Kept when a new frequency setup is made."""
        self.command("gate_time", seconds=seconds)
        self._gate_s = seconds

    @reading("Hz")
    def measure_frequency(
        self, channel: int = 1, expected_hz: Optional[float] = None, resolution_hz: Optional[float] = None
    ) -> float:
        """Frequency in Hz. Leaving out ``expected_hz`` and ``resolution_hz`` sends the instrument's defaults."""
        number = whole_number(channel, "channel", self.name)
        wanted = (number, expected_hz, resolution_hz)
        if self._configured != wanted:
            self._configured = None
            self.command(
                "configure_frequency",
                expected="DEF" if expected_hz is None else f"{expected_hz:.12g}",
                resolution="DEF" if resolution_hz is None else f"{resolution_hz:.12g}",
                channel=number,
            )
            # Configuring may return the gate time to its default, so a chosen one is sent again.
            if self._gate_s is not None:
                self.command("gate_time", seconds=self._gate_s)
            self._configured = wanted
        return self.number("read")
