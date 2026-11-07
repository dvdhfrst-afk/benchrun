"""Digital multimeter driver.

The ``CONFigure`` forms, ``READ?`` and the overload value follow the 34401A User's
Guide, document 34401-90004 (February 1996). See docs/drivers.md. Never run on the hardware.
"""

from __future__ import annotations

from typing import Optional, Tuple

from benchrun.drivers.base import Instrument, reading


class Dmm(Instrument):
    """A multimeter using the common ``CONFigure`` and ``READ?`` commands.

    Range and integration time are arguments of each reading. Settings are only
    sent when they change, so tight sweeps do not reconfigure on every point.
    """

    COMMANDS = {
        **Instrument.COMMANDS,
        "configure_dc_voltage": "CONF:VOLT:DC {range}",
        "configure_dc_current": "CONF:CURR:DC {range}",
        "configure_ac_voltage": "CONF:VOLT:AC {range}",
        "configure_ac_current": "CONF:CURR:AC {range}",
        "configure_resistance": "CONF:RES {range}",
        "configure_resistance_4w": "CONF:FRES {range}",
        "configure_frequency": "CONF:FREQ",
        "set_voltage_nplc": "VOLT:DC:NPLC {nplc:.12g}",
        "set_current_nplc": "CURR:DC:NPLC {nplc:.12g}",
        "read": "READ?",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._configured: Optional[Tuple[str, Optional[float], Optional[float]]] = None

    def reset(self) -> None:
        super().reset()
        self._configured = None

    def _forget_settings(self) -> None:
        self._configured = None

    def _configure(self, function: str, range_value: Optional[float], nplc: Optional[float]) -> None:
        wanted = (function, range_value, nplc)
        if self._configured == wanted:
            return
        self._configured = None
        range_text = "DEF" if range_value is None else f"{range_value:.12g}"
        self.command(f"configure_{function}", range=range_text)
        if nplc is not None:
            if function == "dc_voltage":
                self.command("set_voltage_nplc", nplc=nplc)
            elif function == "dc_current":
                self.command("set_current_nplc", nplc=nplc)
        self._configured = wanted

    @reading("V")
    def measure_dc_voltage(self, range_v: Optional[float] = None, nplc: Optional[float] = None) -> float:
        """DC volts. ``range_v`` omitted means autorange; ``nplc`` is integration time."""
        self._configure("dc_voltage", range_v, nplc)
        return self.number("read")

    @reading("A")
    def measure_dc_current(self, range_a: Optional[float] = None, nplc: Optional[float] = None) -> float:
        """DC amperes."""
        self._configure("dc_current", range_a, nplc)
        return self.number("read")

    @reading("Vrms")
    def measure_ac_voltage(self, range_v: Optional[float] = None) -> float:
        """AC volts rms."""
        self._configure("ac_voltage", range_v, None)
        return self.number("read")

    @reading("A")
    def measure_ac_current(self, range_a: Optional[float] = None) -> float:
        """AC amperes rms."""
        self._configure("ac_current", range_a, None)
        return self.number("read")

    @reading("Ohm")
    def measure_resistance(self, range_ohm: Optional[float] = None) -> float:
        """Two-wire resistance. The test leads are included in the reading."""
        self._configure("resistance", range_ohm, None)
        return self.number("read")

    @reading("Ohm")
    def measure_resistance_4w(self, range_ohm: Optional[float] = None) -> float:
        """Four-wire resistance, which leaves the leads out."""
        self._configure("resistance_4w", range_ohm, None)
        return self.number("read")

    @reading("Hz")
    def measure_frequency(self) -> float:
        """Frequency of the AC signal on the voltage input. With no signal the instrument reads 0."""
        self._configure("frequency", None, None)
        return self.number("read")
