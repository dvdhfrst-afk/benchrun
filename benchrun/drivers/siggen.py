"""Signal generator driver."""

from __future__ import annotations

from benchrun.drivers.base import Instrument, action


class SignalGenerator(Instrument):
    """A function generator using ``FUNC``, ``FREQ``, ``VOLT`` and ``OUTP``."""

    HAS_OUTPUT = True

    COMMANDS = {
        **Instrument.COMMANDS,
        "function": "FUNC {shape}",
        "frequency": "FREQ {hertz:.12g}",
        "amplitude": "VOLT {vpp:.12g}",
        "offset": "VOLT:OFFS {offset_v:.12g}",
        "output": "OUTP {state}",
        "output_state": "OUTP?",
    }

    @action
    def set_waveform(self, shape: str = "SIN", hertz: float = 1000.0, vpp: float = 1.0, offset_v: float = 0.0) -> None:
        """Set shape (``SIN``, ``SQU``, ``TRI``), frequency, amplitude and offset."""
        self.command("function", shape=shape)
        self.command("frequency", hertz=hertz)
        self.command("amplitude", vpp=vpp)
        self.command("offset", offset_v=offset_v)

    @action
    def set_frequency(self, hertz: float) -> None:
        self.command("frequency", hertz=hertz)

    @action
    def set_amplitude(self, vpp: float) -> None:
        self.command("amplitude", vpp=vpp)

    @action
    def output_on(self) -> None:
        self.command("output", state="ON")

    @action
    def output_off(self) -> None:
        self.command("output", state="OFF")

    def safe_state(self) -> None:
        self.output_off()
        self._confirm_off("output_state")
