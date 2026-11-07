"""Oscilloscope driver."""

from __future__ import annotations

import math
from typing import List, Tuple

from benchrun.drivers.base import Instrument, action, reading, whole_number
from benchrun.errors import BadResponse


class Oscilloscope(Instrument):
    """An oscilloscope using automatic measurements on a chosen channel."""

    COMMANDS = {
        **Instrument.COMMANDS,
        "channel_scale": "CHAN{channel:d}:SCAL {volts_per_div:.12g}",
        "channel_offset": "CHAN{channel:d}:OFFS {offset_v:.12g}",
        "channel_coupling": "CHAN{channel:d}:COUP {coupling}",
        "timebase_scale": "TIM:SCAL {seconds_per_div:.12g}",
        "measure_vpp": "MEAS:VPP? CHAN{channel:d}",
        "measure_vrms": "MEAS:VRMS? CHAN{channel:d}",
        "measure_vavg": "MEAS:VAV? CHAN{channel:d}",
        "measure_frequency": "MEAS:FREQ? CHAN{channel:d}",
        "waveform_source": "WAV:SOUR CHAN{channel:d}",
        "waveform_xincrement": "WAV:XINC?",
        "waveform_data": "WAV:DATA?",
    }

    @action
    def setup_channel(self, channel: int, volts_per_div: float, offset_v: float = 0.0, coupling: str = "DC") -> None:
        """Set vertical scale, offset and coupling (``"DC"`` or ``"AC"``)."""
        channel = whole_number(channel, "channel", self.name)
        self.command("channel_coupling", channel=channel, coupling=coupling)
        self.command("channel_scale", channel=channel, volts_per_div=volts_per_div)
        self.command("channel_offset", channel=channel, offset_v=offset_v)

    @action
    def set_timebase(self, seconds_per_div: float) -> None:
        self.command("timebase_scale", seconds_per_div=seconds_per_div)

    @reading("Vpp")
    def measure_vpp(self, channel: int = 1) -> float:
        return self.number("measure_vpp", channel=whole_number(channel, "channel", self.name))

    @reading("Vrms")
    def measure_vrms(self, channel: int = 1) -> float:
        return self.number("measure_vrms", channel=whole_number(channel, "channel", self.name))

    @reading("V")
    def measure_vavg(self, channel: int = 1) -> float:
        return self.number("measure_vavg", channel=whole_number(channel, "channel", self.name))

    @reading("Hz")
    def measure_frequency(self, channel: int = 1) -> float:
        return self.number("measure_frequency", channel=whole_number(channel, "channel", self.name))

    def fetch_waveform(self, channel: int = 1) -> Tuple[float, List[float]]:
        """Return the sample interval in seconds and the samples in volts.

        For scripts built on the driver. A plan's measurements are single
        numbers, so this is not exposed as a plan reading.
        """
        self.command("waveform_source", channel=whole_number(channel, "channel", self.name))
        increment = self.number("waveform_xincrement")
        query = self.commands["waveform_data"]
        response = self._ask(query)
        try:
            samples = [float(piece) for piece in response.split(",")]
        except ValueError:
            raise BadResponse(f"non-numeric waveform data in response to {query!r}", self.name) from None
        if not all(math.isfinite(sample) for sample in samples):
            raise BadResponse(f"non-finite waveform data in response to {query!r}", self.name)
        self.check_errors(query)
        return increment, samples
