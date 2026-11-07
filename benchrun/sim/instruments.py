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
    NOISE_OHM = 1e-3
    RESISTANCE_RANGES = (100.0, 1e3, 1e4, 1e5, 1e6, 1e7, 1e8)

    def reset(self) -> None:
        self.function = "VOLT"
        self.range_v: Optional[float] = None  # None means autorange.
        self.range_a: Optional[float] = None
        self.range_ohm: Optional[float] = None
        self.nplc = 1.0

    def _noise_factor(self) -> float:
        return 1.0 / math.sqrt(self.nplc)

    @staticmethod
    def _pick_range(text: str, ranges: Tuple[float, ...], unit: str) -> Optional[float]:
        if text.strip().upper() in ("AUTO", "DEF", "DEFAULT"):
            return None  # The default range is autorange.
        wanted = parse_number(text, unit, low=0.0, high=ranges[-1] * 1.2)
        for candidate in ranges:
            if wanted <= candidate * 1.2:
                return candidate
        raise ScpiError(-222, f"Data out of range; {wanted:g}")

    @staticmethod
    def _over_range(value: float, chosen: Optional[float]) -> bool:
        return chosen is not None and abs(value) > chosen * 1.2

    def _voltage(self) -> str:
        vout = self.model.meter_volts()
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

    @scpi("CONFigure:CURRent:AC", nargs=(0, 2))
    def _conf_curr_ac(self, args: List[str], suffixes: List[int]) -> None:
        self.function = "CURR:AC"

    @scpi("CONFigure:RESistance", nargs=(0, 2))
    def _conf_res(self, args: List[str], suffixes: List[int]) -> None:
        self.range_ohm = self._pick_range(args[0], self.RESISTANCE_RANGES, "OHM") if args else None
        self.function = "RES"

    @scpi("CONFigure:FRESistance", nargs=(0, 2))
    def _conf_fres(self, args: List[str], suffixes: List[int]) -> None:
        self.range_ohm = self._pick_range(args[0], self.RESISTANCE_RANGES, "OHM") if args else None
        self.function = "FRES"

    @scpi("CONFigure:FREQuency", nargs=(0, 2))
    def _conf_freq(self, args: List[str], suffixes: List[int]) -> None:
        self.function = "FREQ"

    def _resistance(self, leads: float) -> str:
        ohms = self.model.params.sense_ohms + leads
        value = ohms + self.gauss(self.NOISE_OHM)
        return self.measured(OVERLOAD_VALUE if self._over_range(ohms, self.range_ohm) else value)

    def _frequency(self) -> str:
        op = self.model.solve()
        if op.ripple_out_vpp <= 0.0:
            return self.measured(0.0)  # With no signal this kind of meter reads 0.
        return self.measured(op.ripple_hz * (1.0 + self.gauss(1e-6)))

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
        if self.function == "CURR:AC":
            return self.measured(abs(self.gauss(self.NOISE_A)))
        if self.function == "RES":
            return self._resistance(self.model.params.lead_ohms)
        if self.function == "FRES":
            return self._resistance(0.0)
        if self.function == "FREQ":
            return self._frequency()
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

    def _count_setting(self) -> None:
        """A generator has no readings, so its faults count settings instead."""
        self.measured(0.0)

    @scpi("[SOURce]:FUNCtion[:SHAPe]", nargs=(1, 1))
    def _set_shape(self, args: List[str], suffixes: List[int]) -> None:
        self._count_setting()
        self.model.gen_shape = parse_choice(args[0], ("SINusoid", "SQUare", "TRIangle"))

    @scpi("[SOURce]:FUNCtion[:SHAPe]", query=True)
    def _get_shape(self, args: List[str], suffixes: List[int]) -> str:
        return self.model.gen_shape

    @scpi("[SOURce]:FREQuency[:CW]", nargs=(1, 1))
    def _set_freq(self, args: List[str], suffixes: List[int]) -> None:
        self._count_setting()
        self.model.gen_hz = parse_number(args[0], "HZ", low=0.001, high=20e6, default=1000.0)

    @scpi("[SOURce]:FREQuency[:CW]", query=True)
    def _get_freq(self, args: List[str], suffixes: List[int]) -> str:
        return format_real(self.model.gen_hz)

    @scpi("[SOURce]:VOLTage[:LEVel][:IMMediate][:AMPLitude]", nargs=(1, 1))
    def _set_vpp(self, args: List[str], suffixes: List[int]) -> None:
        self._count_setting()
        self.model.gen_vpp = parse_number(args[0], "V", low=0.002, high=20.0, default=0.1)

    @scpi("[SOURce]:VOLTage[:LEVel][:IMMediate][:AMPLitude]", query=True)
    def _get_vpp(self, args: List[str], suffixes: List[int]) -> str:
        return format_real(self.model.gen_vpp)

    @scpi("[SOURce]:VOLTage[:LEVel][:IMMediate]:OFFSet", nargs=(1, 1))
    def _set_offset(self, args: List[str], suffixes: List[int]) -> None:
        self._count_setting()
        self.model.gen_offset = parse_number(args[0], "V", low=-10.0, high=10.0, default=0.0)

    @scpi("[SOURce]:VOLTage[:LEVel][:IMMediate]:OFFSet", query=True)
    def _get_offset(self, args: List[str], suffixes: List[int]) -> str:
        return format_real(self.model.gen_offset)

    @scpi("OUTPut[:STATe]", nargs=(1, 1))
    def _set_output(self, args: List[str], suffixes: List[int]) -> None:
        self._count_setting()
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


def parse_channels(args: List[str]) -> List[int]:
    """Read a channel list such as ``(@101)``, ``(@101:103)`` or ``(@101,105)``."""
    text = ",".join(args).strip()
    if not (text.startswith("(@") and text.endswith(")")):
        raise ScpiError(-104, f"Data type error; expected a channel list, got {text}")
    channels: List[int] = []
    for piece in text[2:-1].split(","):
        first, _, last = piece.strip().partition(":")
        if not first.isdigit() or (last and not last.isdigit()):
            raise ScpiError(-170, f"Expression error; {piece.strip()}")
        channels.extend(range(int(first), int(last or first) + 1))
    return channels


class SimSmu(SimInstrument):
    """Source measure unit across the fixture resistor. Its limit holds back the output."""

    kind = "smu"
    idn = "benchrun,SIM-SMU,SIM0006,1.0"
    MAX_V = 210.0
    MAX_A = 1.05
    NOISE_V = 50e-6
    NOISE_A = 5e-9

    def reset(self) -> None:
        self.source = "VOLT"
        self.level_v = 0.0
        self.level_a = 0.0
        self.limit_a = 0.1
        self.limit_v = 20.0
        self.output = False
        self.sense = "CURR"
        self.elements = "CURR"

    def _solved(self) -> Tuple[float, float, bool]:
        """Volts across the resistor, amps through it, and whether a limit is holding it back."""
        if not self.output:
            return 0.0, 0.0, False
        ohms = self.model.params.sense_ohms
        if self.source == "VOLT":
            amps = self.level_v / ohms
            if abs(amps) > self.limit_a:
                amps = math.copysign(self.limit_a, amps)
                return amps * ohms, amps, True
            return self.level_v, amps, False
        volts = self.level_a * ohms
        if abs(volts) > self.limit_v:
            volts = math.copysign(self.limit_v, volts)
            return volts, volts / ohms, True
        return volts, self.level_a, False

    @scpi("[SOURce]:FUNCtion[:MODE]", nargs=(1, 1))
    def _set_source(self, args: List[str], suffixes: List[int]) -> None:
        self.source = parse_choice(args[0], ("VOLTage", "CURRent"))

    @scpi("[SOURce]:VOLTage[:LEVel][:IMMediate][:AMPLitude]", nargs=(1, 1))
    def _set_volts(self, args: List[str], suffixes: List[int]) -> None:
        self.level_v = parse_number(args[0], "V", low=-self.MAX_V, high=self.MAX_V)

    @scpi("[SOURce]:CURRent[:LEVel][:IMMediate][:AMPLitude]", nargs=(1, 1))
    def _set_amps(self, args: List[str], suffixes: List[int]) -> None:
        self.level_a = parse_number(args[0], "A", low=-self.MAX_A, high=self.MAX_A)

    @scpi("[SENSe]:CURRent[:DC]:PROTection[:LEVel]", nargs=(1, 1))
    def _set_limit_a(self, args: List[str], suffixes: List[int]) -> None:
        self.limit_a = parse_number(args[0], "A", low=1e-9, high=self.MAX_A)

    @scpi("[SENSe]:VOLTage[:DC]:PROTection[:LEVel]", nargs=(1, 1))
    def _set_limit_v(self, args: List[str], suffixes: List[int]) -> None:
        self.limit_v = parse_number(args[0], "V", low=1e-3, high=self.MAX_V)

    @scpi("[SENSe]:CURRent[:DC]:PROTection:TRIPped", query=True)
    def _tripped_a(self, args: List[str], suffixes: List[int]) -> str:
        return "1" if self.source == "VOLT" and self._solved()[2] else "0"

    @scpi("[SENSe]:VOLTage[:DC]:PROTection:TRIPped", query=True)
    def _tripped_v(self, args: List[str], suffixes: List[int]) -> str:
        return "1" if self.source == "CURR" and self._solved()[2] else "0"

    @scpi("[SENSe]:FUNCtion[:ON]", nargs=(1, 1))
    def _set_sense(self, args: List[str], suffixes: List[int]) -> None:
        self.sense = parse_choice(args[0].strip("\"'"), ("VOLTage", "CURRent"))

    @scpi("FORMat:ELEMents", nargs=(1, 1))
    def _set_elements(self, args: List[str], suffixes: List[int]) -> None:
        self.elements = parse_choice(args[0], ("VOLTage", "CURRent"))

    @scpi("OUTPut[:STATe]", nargs=(1, 1))
    def _set_output(self, args: List[str], suffixes: List[int]) -> None:
        self.output = parse_bool(args[0])

    @scpi("OUTPut[:STATe]", query=True)
    def _get_output(self, args: List[str], suffixes: List[int]) -> str:
        return "1" if self.output else "0"

    @scpi("READ", query=True)
    def _read(self, args: List[str], suffixes: List[int]) -> Optional[str]:
        if not self.output:
            raise ScpiError(-221, "Settings conflict; the output is off")
        volts, amps, _ = self._solved()
        if self.elements == "VOLT":
            return self.measured(volts + self.gauss(self.NOISE_V))
        return self.measured(amps + self.gauss(self.NOISE_A))


class SimCounter(SimInstrument):
    """Frequency counter. Channel 1 is the generator's output; anything else has no signal."""

    kind = "counter"
    idn = "benchrun,SIM-COUNTER,SIM0007,1.0"

    def reset(self) -> None:
        self.channel = 1
        self.gate_s = 0.1

    @scpi("CONFigure:FREQuency", nargs=(0, 3))
    def _configure(self, args: List[str], suffixes: List[int]) -> None:
        values = list(args)
        channel = 1
        if values and values[-1].startswith("(@"):
            channels = parse_channels([values.pop()])
            if len(channels) != 1 or channels[0] not in (1, 2, 3):
                raise ScpiError(-221, "Settings conflict; one channel from 1 to 3 is needed")
            channel = channels[0]
        if values:
            parse_number(values[0], "HZ", low=0.1, high=6e9, default=10e6)
        if len(values) > 1:
            parse_number(values[1], "HZ", low=1e-9, high=1e9, default=1.0)
        self.channel = channel
        self.gate_s = 0.1

    @scpi("[SENSe]:FREQuency:GATE:TIME", nargs=(1, 1))
    def _set_gate(self, args: List[str], suffixes: List[int]) -> None:
        self.gate_s = parse_number(args[0], "S", low=1e-4, high=1000.0, default=0.1)

    @scpi("[SENSe]:FREQuency:GATE:TIME", query=True)
    def _get_gate(self, args: List[str], suffixes: List[int]) -> str:
        return format_real(self.gate_s)

    @scpi("READ", query=True)
    def _read(self, args: List[str], suffixes: List[int]) -> str:
        if self.channel != 1 or not self.model.gen_on:
            return self.measured(OVERLOAD_VALUE)  # No signal to count.
        relative = self.gauss(1e-8 / math.sqrt(self.gate_s))
        return self.measured(self.model.gen_hz * (1.0 + relative))


class SimSwitch(SimInstrument):
    """A 20-channel multiplexer in slot 100. Channels 101 and 102 reach the multimeter."""

    kind = "switch"
    idn = "benchrun,SIM-SWITCH,SIM0008,1.0"
    FIRST = 101
    LAST = 120

    def reset(self) -> None:
        self.model.mux_closed.clear()
        self.model.mux_in_use = False

    def _checked(self, args: List[str]) -> List[int]:
        channels = parse_channels(args)
        for channel in channels:
            if not self.FIRST <= channel <= self.LAST:
                raise ScpiError(-222, f"Data out of range; channel {channel}")
        return channels

    @scpi("ROUTe:CLOSe", nargs=(1, 32))
    def _close(self, args: List[str], suffixes: List[int]) -> None:
        channels = self._checked(args)
        self.measured(0.0)
        self.model.mux_in_use = True
        self.model.mux_closed.update(channels)

    @scpi("ROUTe:CLOSe:EXCLusive", nargs=(1, 32))
    def _close_only(self, args: List[str], suffixes: List[int]) -> None:
        channels = self._checked(args)
        self.measured(0.0)
        self.model.mux_in_use = True
        self.model.mux_closed.clear()
        self.model.mux_closed.update(channels)

    @scpi("ROUTe:OPEN", nargs=(1, 32))
    def _open(self, args: List[str], suffixes: List[int]) -> None:
        self.model.mux_closed.difference_update(self._checked(args))

    @scpi("ROUTe:CLOSe", query=True, nargs=(1, 32))
    def _is_closed(self, args: List[str], suffixes: List[int]) -> str:
        return ",".join("1" if channel in self.model.mux_closed else "0" for channel in self._checked(args))


class SimLcr(SimInstrument):
    """LCR meter across the output capacitor, with a three-field ``FETCh?`` reply."""

    kind = "lcr"
    idn = "benchrun,SIM-LCR,SIM0009,1.0"
    NOISE_RELATIVE = 2e-5
    FUNCTIONS = ("CPD", "CSD", "CPQ", "CSQ", "LPD", "LSD", "RX", "ZTD")

    def reset(self) -> None:
        self.function = "CPD"
        self.hertz = 1000.0
        self.volts = 1.0
        self.source = "INT"
        self.armed = False
        self.triggered = False

    def _values(self) -> Tuple[float, float]:
        params = self.model.params
        omega = 2.0 * math.pi * self.hertz
        cap = params.cout_f * (1.0 + self.gauss(self.NOISE_RELATIVE))
        dissipation = params.cout_d * (1.0 + self.gauss(self.NOISE_RELATIVE))
        reactance = -1.0 / (omega * cap)
        resistance = dissipation * abs(reactance)
        if self.function in ("CPD", "CSD"):
            return cap, dissipation
        if self.function in ("CPQ", "CSQ"):
            return cap, 1.0 / dissipation
        if self.function in ("LPD", "LSD"):
            return -1.0 / (omega * omega * cap), dissipation
        if self.function == "RX":
            return resistance, reactance
        return math.hypot(resistance, reactance), math.degrees(math.atan2(reactance, resistance))

    @scpi("FUNCtion:IMPedance[:TYPE]", nargs=(1, 1))
    def _set_function(self, args: List[str], suffixes: List[int]) -> None:
        word = args[0].strip().upper()
        if word not in self.FUNCTIONS:
            raise ScpiError(-224, f"Illegal parameter value; {word}")
        self.function = word

    @scpi("FREQuency[:CW]", nargs=(1, 1))
    def _set_frequency(self, args: List[str], suffixes: List[int]) -> None:
        self.hertz = parse_number(args[0], "HZ", low=20.0, high=2e6, default=1000.0)

    @scpi("VOLTage[:LEVel]", nargs=(1, 1))
    def _set_level(self, args: List[str], suffixes: List[int]) -> None:
        self.volts = parse_number(args[0], "V", low=0.0, high=20.0, default=1.0)

    @scpi("TRIGger:SOURce", nargs=(1, 1))
    def _set_source(self, args: List[str], suffixes: List[int]) -> None:
        self.source = parse_choice(args[0], ("INTernal", "HOLD", "EXTernal", "BUS"))

    @scpi("INITiate[:IMMediate]")
    def _initiate(self, args: List[str], suffixes: List[int]) -> None:
        self.armed = True

    @scpi("TRIGger[:IMMediate]")
    def _trigger(self, args: List[str], suffixes: List[int]) -> None:
        if not self.armed:
            raise ScpiError(-211, "Trigger ignored")
        self.triggered = True

    @scpi("FETCh[:IMPedance][:FORMatted]", query=True)
    def _fetch(self, args: List[str], suffixes: List[int]) -> str:
        if not self.triggered:
            return "+0.00000E+00,+0.00000E+00,-1"
        self.triggered = False
        self.armed = False
        first, second = self._values()
        return f"{self.measured(first)},{format_real(second)},+0"


class SimMultiSupply(SimInstrument):
    """A three-output supply, each output feeding its own resistor. Separate from VIN."""

    kind = "psu_multi"
    idn = "benchrun,SIM-PSU3,SIM0010,1.0"
    OUTPUTS = (1, 2, 3)
    MAX_V = 30.0
    MAX_A = 5.0
    NOISE_V = 500e-6
    NOISE_A = 50e-6

    def reset(self) -> None:
        self.volts = {n: 0.0 for n in self.OUTPUTS}
        self.amps = {n: 1.0 for n in self.OUTPUTS}
        self.on = {n: False for n in self.OUTPUTS}

    def _outputs(self, args: List[str]) -> List[int]:
        channels = parse_channels(args)
        for channel in channels:
            if channel not in self.OUTPUTS:
                raise ScpiError(-222, f"Data out of range; output {channel}")
        return channels

    def _solved(self, output: int) -> Tuple[float, float, int]:
        """Volts, amps and the mode: 0 off, 1 constant current, 2 constant voltage."""
        if not self.on[output]:
            return 0.0, 0.0, 0
        ohms = self.model.params.rail_ohms
        amps = self.volts[output] / ohms
        if amps > self.amps[output]:
            return self.amps[output] * ohms, self.amps[output], 1
        return self.volts[output], amps, 2

    @scpi("[SOURce]:VOLTage[:LEVel][:IMMediate][:AMPLitude]", nargs=(2, 2))
    def _set_volts(self, args: List[str], suffixes: List[int]) -> None:
        value = parse_number(args[0], "V", low=0.0, high=self.MAX_V)
        for output in self._outputs(args[1:]):
            self.volts[output] = value

    @scpi("[SOURce]:CURRent[:LEVel][:IMMediate][:AMPLitude]", nargs=(2, 2))
    def _set_amps(self, args: List[str], suffixes: List[int]) -> None:
        value = parse_number(args[0], "A", low=0.0, high=self.MAX_A)
        for output in self._outputs(args[1:]):
            self.amps[output] = value

    @scpi("OUTPut[:STATe]", nargs=(2, 2))
    def _set_output(self, args: List[str], suffixes: List[int]) -> None:
        state = parse_bool(args[0])
        for output in self._outputs(args[1:]):
            self.on[output] = state

    @scpi("OUTPut[:STATe]", query=True, nargs=(1, 1))
    def _get_output(self, args: List[str], suffixes: List[int]) -> str:
        return ",".join("1" if self.on[output] else "0" for output in self._outputs(args))

    @scpi("MEASure[:SCALar]:VOLTage[:DC]", query=True, nargs=(1, 1))
    def _meas_volts(self, args: List[str], suffixes: List[int]) -> str:
        output = self._outputs(args)[0]
        return self.measured(self._solved(output)[0] + self.gauss(self.NOISE_V))

    @scpi("MEASure[:SCALar]:CURRent[:DC]", query=True, nargs=(1, 1))
    def _meas_amps(self, args: List[str], suffixes: List[int]) -> str:
        output = self._outputs(args)[0]
        return self.measured(self._solved(output)[1] + self.gauss(self.NOISE_A))

    @scpi("STATus:QUEStionable:INSTrument:ISUMmary#:CONDition", query=True)
    def _mode(self, args: List[str], suffixes: List[int]) -> str:
        if suffixes[0] not in self.OUTPUTS:
            raise ScpiError(-114, f"Header suffix out of range; {suffixes[0]}")
        return str(self._solved(suffixes[0])[2])


INSTRUMENT_CLASSES = {
    cls.kind: cls
    for cls in (SimDmm, SimSupply, SimLoad, SimScope, SimGenerator, SimSmu, SimCounter, SimSwitch, SimLcr, SimMultiSupply)
}
