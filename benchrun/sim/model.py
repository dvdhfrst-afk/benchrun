"""The simulated bench: a linear voltage regulator wired to the instruments.

Wiring::

    supply (+ generator ripple) --> VIN  [regulator]  VOUT --> electronic load
                                                           --> multimeter
    oscilloscope CH1 = VOUT, CH2 = VIN

The equations are documented in docs/simulators.md. They are deliberately simple
enough to work out by hand, so test results can be checked independently.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Dict

UVLO_V = 2.5  # Below this input voltage the regulator is off.
COLLAPSED_VIN_V = 0.2  # Input rail once the supply current limit collapses it.


@dataclass(frozen=True)
class RegulatorParams:
    """Parameters of the simulated device under test."""

    vnom: float = 5.0  # Output at VREF_IN with no load, volts.
    vref_in: float = 12.0  # Input voltage at which the output equals vnom, volts.
    line_reg: float = 0.001  # Output change per input volt, V/V.
    load_reg: float = 0.015  # Output drop per load ampere, ohms.
    dropout0: float = 0.35  # Dropout voltage at no load, volts.
    dropout_r: float = 0.25  # Added dropout per load ampere, ohms.
    iq: float = 0.003  # Quiescent input current, amperes.
    ilimit: float = 1.5  # Output current limit, amperes.
    psrr_db: float = 70.0  # Ripple rejection at low frequency, dB.
    psrr_corner_hz: float = 1000.0  # Rejection falls 20 dB per decade above this.
    alive: bool = True  # False models a unit with no output at all.


DUT_VARIANTS: Dict[str, RegulatorParams] = {
    "good": RegulatorParams(),
    "high-output": RegulatorParams(vnom=5.25),
    "poor-line": RegulatorParams(line_reg=0.005),
    "poor-load": RegulatorParams(load_reg=0.05),
    "high-dropout": RegulatorParams(dropout0=1.0),
    "high-iq": RegulatorParams(iq=0.02),
    "poor-psrr": RegulatorParams(psrr_db=45.0),
    "dead": replace(RegulatorParams(), alive=False),
}


@dataclass(frozen=True)
class OperatingPoint:
    """The solved electrical state of the bench."""

    vin: float = 0.0
    iin: float = 0.0
    vout: float = 0.0
    iout: float = 0.0
    supply_current_limited: bool = False
    ripple_in_vpp: float = 0.0
    ripple_out_vpp: float = 0.0
    ripple_hz: float = 0.0
    ripple_shape: str = "SIN"


class BenchModel:
    """Settings made by the instruments, and the operating point they produce."""

    def __init__(self, params: RegulatorParams):
        self.params = params
        self.supply_on = False
        self.supply_volts = 0.0
        self.supply_amps = 1.0
        self.load_on = False
        self.load_amps = 0.0
        self.gen_on = False
        self.gen_vpp = 0.1
        self.gen_hz = 1000.0
        self.gen_offset = 0.0
        self.gen_shape = "SIN"

    def psrr_db(self, hz: float) -> float:
        """Ripple rejection at ``hz``, a single-pole roll-off from the corner."""
        p = self.params
        return p.psrr_db - 10.0 * math.log10(1.0 + (hz / p.psrr_corner_hz) ** 2)

    def solve(self) -> OperatingPoint:
        p = self.params
        if not self.supply_on:
            return OperatingPoint()
        vin = self.supply_volts + (self.gen_offset if self.gen_on else 0.0)
        vin = max(vin, 0.0)
        ripple_in = self.gen_vpp if self.gen_on else 0.0
        hz = self.gen_hz if self.gen_on else 0.0
        demand = self.load_amps if self.load_on else 0.0

        if vin < UVLO_V or not p.alive:
            return OperatingPoint(
                vin=vin,
                iin=0.0,
                ripple_in_vpp=ripple_in,
                ripple_hz=hz,
                ripple_shape=self.gen_shape,
            )

        iout = min(demand, p.ilimit)
        regulated = p.vnom + p.line_reg * (vin - p.vref_in) - p.load_reg * iout
        ceiling = vin - (p.dropout0 + p.dropout_r * iout)
        in_dropout = ceiling < regulated
        vout = max(min(regulated, ceiling), 0.0)
        if demand > p.ilimit:
            vout = 0.0  # Regulator current limit: output folds back.
        iin = iout + p.iq

        if iin > self.supply_amps:
            # The supply cannot deliver it: its rail collapses in constant current.
            return OperatingPoint(
                vin=min(vin, COLLAPSED_VIN_V),
                iin=self.supply_amps,
                supply_current_limited=True,
                ripple_in_vpp=ripple_in,
                ripple_hz=hz,
                ripple_shape=self.gen_shape,
            )

        if in_dropout:
            ripple_out = ripple_in  # No headroom: ripple passes straight through.
        elif hz > 0.0:
            ripple_out = ripple_in * 10.0 ** (-self.psrr_db(hz) / 20.0)
        else:
            ripple_out = 0.0
        return OperatingPoint(
            vin=vin,
            iin=iin,
            vout=vout,
            iout=iout,
            ripple_in_vpp=ripple_in,
            ripple_out_vpp=ripple_out,
            ripple_hz=hz,
            ripple_shape=self.gen_shape,
        )
