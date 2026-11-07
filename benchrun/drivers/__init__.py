"""Instrument drivers and the registry plans use to find them by name."""

from __future__ import annotations

from typing import Dict, Type

from benchrun.drivers.base import Instrument
from benchrun.drivers.counter import FrequencyCounter
from benchrun.drivers.dmm import Dmm
from benchrun.drivers.eload import ElectronicLoad
from benchrun.drivers.lcr import LcrMeter
from benchrun.drivers.psu import PowerSupply
from benchrun.drivers.psu_multi import MultiOutputSupply
from benchrun.drivers.scope import Oscilloscope
from benchrun.drivers.siggen import SignalGenerator
from benchrun.drivers.smu import SourceMeasureUnit
from benchrun.drivers.switch import Switch

DRIVERS: Dict[str, Type[Instrument]] = {
    "dmm": Dmm,
    "psu": PowerSupply,
    "eload": ElectronicLoad,
    "scope": Oscilloscope,
    "siggen": SignalGenerator,
    "smu": SourceMeasureUnit,
    "counter": FrequencyCounter,
    "switch": Switch,
    "lcr": LcrMeter,
    "psu_multi": MultiOutputSupply,
    "scpi": Instrument,
}
