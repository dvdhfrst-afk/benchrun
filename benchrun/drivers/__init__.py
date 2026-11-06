"""Instrument drivers and the registry plans use to find them by name."""

from __future__ import annotations

from typing import Dict, Type

from benchrun.drivers.base import Instrument
from benchrun.drivers.dmm import Dmm
from benchrun.drivers.eload import ElectronicLoad
from benchrun.drivers.psu import PowerSupply
from benchrun.drivers.scope import Oscilloscope
from benchrun.drivers.siggen import SignalGenerator

DRIVERS: Dict[str, Type[Instrument]] = {
    "dmm": Dmm,
    "psu": PowerSupply,
    "eload": ElectronicLoad,
    "scope": Oscilloscope,
    "siggen": SignalGenerator,
    "scpi": Instrument,
}
