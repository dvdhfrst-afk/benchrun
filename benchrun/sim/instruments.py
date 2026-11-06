"""The simulated instruments. Each one is a SCPI device reading a shared bench model."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import List, Optional, Tuple

from benchrun.sim.model import BenchModel
from benchrun.sim.scpi import (
    OVERLOAD_VALUE,
    DeviceDisconnect,
    DeviceHang,
    ScpiDevice,
    ScpiError,
    format_real,
    parse_bool,
    parse_choice,
    parse_number,
    scpi,
)

FAULT_KINDS = ("drop", "hang", "error", "garbage", "overload")


@dataclass
class Fault:
    """A fault to inject after ``after`` good readings. See ``SimInstrument.measured``."""

    kind: str
    after: int = 0


class SimInstrument(ScpiDevice):
    """A SCPI device with seeded noise and fault injection on its readings."""

    kind = "instrument"

    def __init__(self, model: BenchModel, seed: int = 1, noise: float = 1.0, fault: Optional[Fault] = None):
        self.model = model
        self.noise = noise
        self.fault = fault
        self.readings_taken = 0
        self.dead = False
        self.hung = False
        # Seeding from a string is stable across processes and Python versions.
        self.rng = random.Random(f"{seed}:{self.kind}")
        super().__init__()
        self.reset()

    def gauss(self, sigma: float) -> float:
        """Zero-mean noise. Always draws, so scaling noise keeps the sequence aligned."""
        return self.rng.gauss(0.0, 1.0) * sigma * self.noise

    def measured(self, value: float) -> str:
        """Format a reading, applying an injected fault when one is due."""
        self.readings_taken += 1
        fault = self.fault
        if fault is not None and self.readings_taken > fault.after:
            if fault.kind == "drop":
                self.dead = True
                raise DeviceDisconnect()
            if fault.kind == "hang":
                self.hung = True
                raise DeviceHang()
            if fault.kind == "error":
                self.push_error(-300, "Device-specific error; simulated hardware fault")
            elif fault.kind == "garbage":
                return "#BAD#"
            elif fault.kind == "overload":
                return format_real(OVERLOAD_VALUE)
        return format_real(value)


class SimDmm(SimInstrument):
    """Digital multimeter. Voltage across VOUT; current in series with the load."""

    kind = "dmm"
    idn = "benchrun,SIM-DMM,SIM0001,1.0"
    VOLTAGE_RANGES = (0.1, 1.0, 10.0, 100.0, 1000.0)
    CURRENT_RANGES = (0.01, 0.1, 1.0, 3.0, 10.0)
    NOISE_V = 30e-6
    NOISE_A = 20e-6

    def reset(self) -> None:
        self.function = "VOLT"
        self.range_v: Optional[float] = None  # None means autorange.
        self.range_a: Optional[float] = None
        self.nplc = 1.0

    def _noise_factor(self) -> float:
        return 1.0 / math.sqrt(self.nplc)

    @staticmethod
    def _pick_range(text: str, ranges: Tuple[float, ...], unit: str) -> Optional[float]:
        if text.strip().upper() in ("AUTO", "DEF", "DEFAULT"):
            return None
        wanted = parse_number(text, unit, low=0.0, high=ranges[-1] * 1.2)
        for candidate in ranges:
            if wanted <= candidate * 1.2:
                return candidate
        raise ScpiError(-222, f"Data out of range; {wanted:g}")

    @staticmethod
    def _over_range(value: float, chosen: Optional[float]) -> bool:
        return chosen is not None and abs(value) > chosen * 1.2

    def _voltage(self) -> str:
        vout = self.model.solve().vout
        value = vout + self.gauss(self.NOISE_V * self._noise_factor())
        return self.measured(OVERLOAD_VALUE if self._over_range(vout, self.range_v) else value)

    def _current(self) -> str:
        iout = self.model.solve().iout
        value = iout + self.gauss(self.NOISE_A * self._noise_factor())
        return self.measured(OVERLOAD_VALUE if self._over_range(iout, self.range_a) else value)

    def _ac_voltage(self) -> str:
        op = self.model.solve()
        rms = op.ripple_out_vpp / (2.0 * math.sqrt(2.0))
        return self.measured(abs(rms + self.gauss(self.NOISE_V)))

    @scpi("CONFigure[:VOLTage][:DC]", nargs=(0, 2))
    def _conf_volt(self, args: List[str], suffixes: List[int]) -> None:
        self.range_v = self._pick_range(args[0], self.VOLTAGE_RANGES, "V") if args else None
        self.function = "VOLT"
        self.nplc = 1.0

    @scpi("CONFigure:CURRent[:DC]", nargs=(0, 2))
    def _conf_curr(self, args: List[str], suffixes: List[int]) -> None:
        self.range_a = self._pick_range(args[0], self.CURRENT_RANGES, "A") if args else None
        self.function = "CURR"
        self.nplc = 1.0

    @scpi("CONFigure:VOLTage:AC", nargs=(0, 2))
    def _conf_volt_ac(self, args: List[str], suffixes: List[int]) -> None:
        self.function = "VOLT:AC"

    @scpi("CONFigure", query=True)
    def _conf_query(self, args: List[str], suffixes: List[int]) -> str:
        return f'"{self.function}"'

    @scpi("MEASure[:VOLTage][:DC]", query=True, nargs=(0, 2))
    def _meas_volt(self, args: List[str], suffixes: List[int]) -> str:
        self._conf_volt(args, suffixes)
        return self._voltage()

    @scpi("MEASure:CURRent[:DC]", query=True, nargs=(0, 2))
    def _meas_curr(self, args: List[str], suffixes: List[int]) -> str:
        self._conf_curr(args, suffixes)
        return self._current()

    @scpi("MEASure:VOLTage:AC", query=True, nargs=(0, 2))
    def _meas_volt_ac(self, args: List[str], suffixes: List[int]) -> str:
        self._conf_volt_ac(args, suffixes)
        return self._ac_voltage()

    @scpi("READ", query=True)
    def _read(self, args: List[str], suffixes: List[int]) -> str:
        if self.function == "CURR":
            return self._current()
        if self.function == "VOLT:AC":
            return self._ac_voltage()
        return self._voltage()

    @scpi("[SENSe]:VOLTage[:DC]:NPLCycles", nargs=(1, 1))
    @scpi("[SENSe]:CURRent[:DC]:NPLCycles", nargs=(1, 1))
    def _nplc(self, args: List[str], suffixes: List[int]) -> None:
        self.nplc = parse_number(args[0], low=0.02, high=100.0, default=1.0)

    @scpi("[SENSe]:VOLTage[:DC]:NPLCycles", query=True)
    @scpi("[SENSe]:CURRent[:DC]:NPLCycles", query=True)
    def _nplc_query(self, args: List[str], suffixes: List[int]) -> str:
        return format_real(self.nplc)

    @scpi("[SENSe]:VOLTage[:DC]:RANGe[:UPPer]", nargs=(1, 1))
    def _range_volt(self, args: List[str], suffixes: List[int]) -> None:
        self.range_v = self._pick_range(args[0], self.VOLTAGE_RANGES, "V")

    @scpi("[SENSe]:CURRent[:DC]:RANGe[:UPPer]", nargs=(1, 1))
    def _range_curr(self, args: List[str], suffixes: List[int]) -> None:
        self.range_a = self._pick_range(args[0], self.CURRENT_RANGES, "A")


class SimSupply(SimInstrument):
    """Programmable power supply, 0 to 30 V and 0 to 5 A, feeding VIN."""

    kind = "psu"
    idn = "benchrun,SIM-PSU,SIM0002,1.0"
    MAX_V = 30.0
    MAX_A = 5.0
    NOISE_V = 500e-6
    NOISE_A = 50e-6
    CV_BIT = 0x100
    CC_BIT = 0x400

    def reset(self) -> None:
        self.model.supply_on = False
        self.model.supply_volts = 0.0
        self.model.supply_amps = 1.0

    @scpi("[SOURce]:VOLTage[:LEVel][:IMMediate][:AMPLitude]", nargs=(1, 1))
    def _set_volts(self, args: List[str], suffixes: List[int]) -> None:
        self.model.supply_volts = parse_number(args[0], "V", low=0.0, high=self.MAX_V, default=0.0)

    @scpi("[SOURce]:VOLTage[:LEVel][:IMMediate][:AMPLitude]", query=True)
    def _get_volts(self, args: List[str], suffixes: List[int]) -> str:
        return format_real(self.model.supply_volts)

    @scpi("[SOURce]:CURRent[:LEVel][:IMMediate][:AMPLitude]", nargs=(1, 1))
    def _set_amps(self, args: List[str], suffixes: List[int]) -> None:
        self.model.supply_amps = parse_number(args[0], "A", low=0.0, high=self.MAX_A, default=1.0)

    @scpi("[SOURce]:CURRent[:LEVel][:IMMediate][:AMPLitude]", query=True)
    def _get_amps(self, args: List[str], suffixes: List[int]) -> str:
        return format_real(self.model.supply_amps)

    @scpi("OUTPut[:STATe]", nargs=(1, 1))
    def _set_output(self, args: List[str], suffixes: List[int]) -> None:
        self.model.supply_on = parse_bool(args[0])

    @scpi("OUTPut[:STATe]", query=True)
    def _get_output(self, args: List[str], suffixes: List[int]) -> str:
        return "1" if self.model.supply_on else "0"

    @scpi("MEASure[:SCALar]:VOLTage[:DC]", query=True)
    def _meas_volts(self, args: List[str], suffixes: List[int]) -> str:
        return self.measured(self.model.solve().vin + self.gauss(self.NOISE_V))

    @scpi("MEASure[:SCALar]:CURRent[:DC]", query=True)
    def _meas_amps(self, args: List[str], suffixes: List[int]) -> str:
        return self.measured(self.model.solve().iin + self.gauss(self.NOISE_A))

    @scpi("STATus:OPERation:CONDition", query=True)
    def _condition(self, args: List[str], suffixes: List[int]) -> str:
        if not self.model.supply_on:
            return "0"
        limited = self.model.solve().supply_current_limited
        return str(self.CC_BIT if limited else self.CV_BIT)


class SimLoad(SimInstrument):
    """Constant-current electronic load, 0 to 5 A, connected across VOUT."""

    kind = "eload"
    idn = "benchrun,SIM-ELOAD,SIM0003,1.0"
    MAX_A = 5.0
    NOISE_V = 500e-6
    NOISE_A = 100e-6

    def reset(self) -> None:
        self.model.load_on = False
        self.model.load_amps = 0.0

    @scpi("[SOURce]:CURRent[:LEVel][:IMMediate][:AMPLitude]", nargs=(1, 1))
    def _set_amps(self, args: List[str], suffixes: List[int]) -> None:
        self.model.load_amps = parse_number(args[0], "A", low=0.0, high=self.MAX_A, default=0.0)

    @scpi("[SOURce]:CURRent[:LEVel][:IMMediate][:AMPLitude]", query=True)
    def _get_amps(self, args: List[str], suffixes: List[int]) -> str:
        return format_real(self.model.load_amps)

    @scpi("INPut[:STATe]", nargs=(1, 1))
    def _set_input(self, args: List[str], suffixes: List[int]) -> None:
        self.model.load_on = parse_bool(args[0])

    @scpi("INPut[:STATe]", query=True)
    def _get_input(self, args: List[str], suffixes: List[int]) -> str:
        return "1" if self.model.load_on else "0"

    @scpi("MEASure[:SCALar]:VOLTage[:DC]", query=True)
    def _meas_volts(self, args: List[str], suffixes: List[int]) -> str:
        return self.measured(self.model.solve().vout + self.gauss(self.NOISE_V))

    @scpi("MEASure[:SCALar]:CURRent[:DC]", query=True)
    def _meas_amps(self, args: List[str], suffixes: List[int]) -> str:
        return self.measured(self.model.solve().iout + self.gauss(self.NOISE_A))


class SimGenerator(SimInstrument):
    """Signal generator. Its output is coupled onto VIN as ripple."""

    kind = "siggen"
    idn = "benchrun,SIM-SIGGEN,SIM0004,1.0"

    def reset(self) -> None:
        self.model.gen_on = False
        self.model.gen_vpp = 0.1
        self.model.gen_hz = 1000.0
        self.model.gen_offset = 0.0
        self.model.gen_shape = "SIN"

    @scpi("[SOURce]:FUNCtion[:SHAPe]", nargs=(1, 1))
    def _set_shape(self, args: List[str], suffixes: List[int]) -> None:
        self.model.gen_shape = parse_choice(args[0], ("SINusoid", "SQUare", "TRIangle"))

    @scpi("[SOURce]:FUNCtion[:SHAPe]", query=True)
    def _get_shape(self, args: List[str], suffixes: List[int]) -> str:
        return self.model.gen_shape

    @scpi("[SOURce]:FREQuency[:CW]", nargs=(1, 1))
    def _set_freq(self, args: List[str], suffixes: List[int]) -> None:
        self.model.gen_hz = parse_number(args[0], "HZ", low=0.001, high=20e6, default=1000.0)

    @scpi("[SOURce]:FREQuency[:CW]", query=True)
    def _get_freq(self, args: List[str], suffixes: List[int]) -> str:
        return format_real(self.model.gen_hz)

    @scpi("[SOURce]:VOLTage[:LEVel][:IMMediate][:AMPLitude]", nargs=(1, 1))
    def _set_vpp(self, args: List[str], suffixes: List[int]) -> None:
        self.model.gen_vpp = parse_number(args[0], "V", low=0.002, high=20.0, default=0.1)

    @scpi("[SOURce]:VOLTage[:LEVel][:IMMediate][:AMPLitude]", query=True)
    def _get_vpp(self, args: List[str], suffixes: List[int]) -> str:
        return format_real(self.model.gen_vpp)

    @scpi("[SOURce]:VOLTage[:LEVel][:IMMediate]:OFFSet", nargs=(1, 1))
    def _set_offset(self, args: List[str], suffixes: List[int]) -> None:
        self.model.gen_offset = parse_number(args[0], "V", low=-10.0, high=10.0, default=0.0)

    @scpi("[SOURce]:VOLTage[:LEVel][:IMMediate]:OFFSet", query=True)
    def _get_offset(self, args: List[str], suffixes: List[int]) -> str:
        return format_real(self.model.gen_offset)

    @scpi("OUTPut[:STATe]", nargs=(1, 1))
    def _set_output(self, args: List[str], suffixes: List[int]) -> None:
        self.model.gen_on = parse_bool(args[0])

    @scpi("OUTPut[:STATe]", query=True)
    def _get_output(self, args: List[str], suffixes: List[int]) -> str:
        return "1" if self.model.gen_on else "0"


class SimScope(SimInstrument):
    """Two-channel oscilloscope. CH1 is VOUT, CH2 is VIN.

    Measurements are computed from a simulated 8-bit acquisition, so vertical
    scale, coupling and timebase matter: a trace off screen reads as overload,
    and too few cycles on screen leaves the frequency undefined.
    """

    kind = "scope"
    idn = "benchrun,SIM-SCOPE,SIM0005,1.0"
    POINTS = 1000
    DIVISIONS_Y = 8
    DIVISIONS_X = 10
    NOISE_DIV = 0.01  # Trace noise, in divisions rms.

    def reset(self) -> None:
        self.scale = {1: 1.0, 2: 1.0}
        self.offset = {1: 0.0, 2: 0.0}
        self.coupling = {1: "DC", 2: "DC"}
        self.timebase = 1e-3
        self.wave_source = 1

    def _channel(self, number: int) -> int:
        if number not in self.scale:
            raise ScpiError(-114, f"Header suffix out of range; channel {number}")
        return number

    def _source(self, args: List[str]) -> int:
        """Channel named by a query parameter such as ``CHANnel2``; default 1."""
        if not args:
            return 1
        word = args[0].strip().upper()
        for prefix in ("CHANNEL", "CHAN", "CH"):
            if word.startswith(prefix) and word[len(prefix) :].isdigit():
                return self._channel(int(word[len(prefix) :]))
        raise ScpiError(-224, f"Illegal parameter value; {args[0]}")

    def _shape(self, name: str, phase: float) -> float:
        if name == "SQU":
            return 1.0 if math.sin(phase) >= 0.0 else -1.0
        if name == "TRI":
            return 2.0 / math.pi * math.asin(math.sin(phase))
        return math.sin(phase)

    def _acquire(self, channel: int) -> Tuple[List[float], bool]:
        """Return one record of volts, and whether the trace ran off screen."""
        op = self.model.solve()
        if channel == 1:
            dc, ripple = op.vout, op.ripple_out_vpp
        else:
            dc, ripple = op.vin, op.ripple_in_vpp
        if self.coupling[channel] == "AC":
            dc = 0.0
        scale = self.scale[channel]
        offset = self.offset[channel]
        lsb = self.DIVISIONS_Y * scale / 256.0
        half_screen = self.DIVISIONS_Y * scale / 2.0
        dt = self.timebase * self.DIVISIONS_X / self.POINTS
        start = self.rng.random() * 2.0 * math.pi * min(self.noise, 1.0)
        samples: List[float] = []
        clipped = False
        for index in range(self.POINTS):
            value = dc + self.gauss(self.NOISE_DIV * scale)
            if ripple > 0.0:
                phase = start + 2.0 * math.pi * op.ripple_hz * index * dt
                value += ripple / 2.0 * self._shape(op.ripple_shape, phase)
            relative = value - offset
            if abs(relative) > half_screen:
                clipped = True
                relative = math.copysign(half_screen, relative)
            samples.append(round(relative / lsb) * lsb + offset)
        return samples, clipped

    @staticmethod
    def _frequency(samples: List[float], dt: float) -> Optional[float]:
        mean = sum(samples) / len(samples)
        high = max(samples)
        low = min(samples)
        if high - low <= 0.0:
            return None
        band = (high - low) * 0.1
        crossings: List[int] = []
        armed = False
        for index, value in enumerate(samples):
            if value < mean - band:
                armed = True
            elif armed and value > mean + band:
                crossings.append(index)
                armed = False
        if len(crossings) < 2:
            return None
        period = (crossings[-1] - crossings[0]) * dt / (len(crossings) - 1)
        return 1.0 / period if period > 0.0 else None

    def _measurement(self, args: List[str], compute) -> str:
        samples, clipped = self._acquire(self._source(args))
        return self.measured(OVERLOAD_VALUE if clipped else compute(samples))

    @scpi("CHANnel#:SCALe", nargs=(1, 1))
    def _set_scale(self, args: List[str], suffixes: List[int]) -> None:
        channel = self._channel(suffixes[0])
        self.scale[channel] = parse_number(args[0], "V", low=500e-6, high=10.0)

    @scpi("CHANnel#:SCALe", query=True)
    def _get_scale(self, args: List[str], suffixes: List[int]) -> str:
        return format_real(self.scale[self._channel(suffixes[0])])

    @scpi("CHANnel#:OFFSet", nargs=(1, 1))
    def _set_offset(self, args: List[str], suffixes: List[int]) -> None:
        channel = self._channel(suffixes[0])
        self.offset[channel] = parse_number(args[0], "V", low=-40.0, high=40.0)

    @scpi("CHANnel#:OFFSet", query=True)
    def _get_offset(self, args: List[str], suffixes: List[int]) -> str:
        return format_real(self.offset[self._channel(suffixes[0])])

    @scpi("CHANnel#:COUPling", nargs=(1, 1))
    def _set_coupling(self, args: List[str], suffixes: List[int]) -> None:
        channel = self._channel(suffixes[0])
        self.coupling[channel] = parse_choice(args[0], ("DC", "AC"))

    @scpi("CHANnel#:COUPling", query=True)
    def _get_coupling(self, args: List[str], suffixes: List[int]) -> str:
        return self.coupling[self._channel(suffixes[0])]

    @scpi("TIMebase[:MAIN]:SCALe", nargs=(1, 1))
    def _set_timebase(self, args: List[str], suffixes: List[int]) -> None:
        self.timebase = parse_number(args[0], "S", low=1e-9, high=50.0)

    @scpi("TIMebase[:MAIN]:SCALe", query=True)
    def _get_timebase(self, args: List[str], suffixes: List[int]) -> str:
        return format_real(self.timebase)

    @scpi("MEASure:VPP", query=True, nargs=(0, 1))
    def _meas_vpp(self, args: List[str], suffixes: List[int]) -> str:
        return self._measurement(args, lambda s: max(s) - min(s))

    @scpi("MEASure:VAVerage", query=True, nargs=(0, 1))
    def _meas_avg(self, args: List[str], suffixes: List[int]) -> str:
        return self._measurement(args, lambda s: sum(s) / len(s))

    @scpi("MEASure:VRMS", query=True, nargs=(0, 1))
    def _meas_rms(self, args: List[str], suffixes: List[int]) -> str:
        return self._measurement(args, lambda s: math.sqrt(sum(v * v for v in s) / len(s)))

    @scpi("MEASure:FREQuency", query=True, nargs=(0, 1))
    def _meas_freq(self, args: List[str], suffixes: List[int]) -> str:
        dt = self.timebase * self.DIVISIONS_X / self.POINTS

        def compute(samples: List[float]) -> float:
            found = self._frequency(samples, dt)
            return OVERLOAD_VALUE if found is None else found

        return self._measurement(args, compute)

    @scpi("WAVeform:SOURce", nargs=(1, 1))
    def _set_wave_source(self, args: List[str], suffixes: List[int]) -> None:
        self.wave_source = self._source(args)

    @scpi("WAVeform:XINCrement", query=True)
    def _wave_xinc(self, args: List[str], suffixes: List[int]) -> str:
        return format_real(self.timebase * self.DIVISIONS_X / self.POINTS)

    @scpi("WAVeform:DATA", query=True)
    def _wave_data(self, args: List[str], suffixes: List[int]) -> str:
        samples, _ = self._acquire(self.wave_source)
        return ",".join(f"{value:.6E}" for value in samples)


INSTRUMENT_CLASSES = {cls.kind: cls for cls in (SimDmm, SimSupply, SimLoad, SimScope, SimGenerator)}
