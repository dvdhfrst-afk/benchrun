"""Engineering units: SI prefixes and safe conversion between compatible units."""

from __future__ import annotations

from typing import Tuple

# Quantities a driver can report. A plan's declared units must share the base.
BASE_UNITS = ("V", "Vpp", "Vrms", "A", "W", "Hz", "s", "Ohm", "F", "H", "S", "dB", "%", "")

_PREFIXES = {
    "p": 1e-12,
    "n": 1e-9,
    "u": 1e-6,
    "\u00b5": 1e-6,
    "m": 1e-3,
    "k": 1e3,
    "M": 1e6,
    "G": 1e9,
}


def split_unit(unit: str) -> Tuple[float, str]:
    """Return the scale factor and base of a unit: ``"mV"`` gives ``(0.001, "V")``.

    An unknown unit is treated as its own base with a scale of one, so plans can
    label calculated values with anything, such as ``"mV/V"``.
    """
    if unit in BASE_UNITS:
        return 1.0, unit
    if len(unit) > 1 and unit[0] in _PREFIXES and unit[1:] in BASE_UNITS:
        return _PREFIXES[unit[0]], unit[1:]
    return 1.0, unit


def compatible(unit_a: str, unit_b: str) -> bool:
    """True when a value can be converted from one unit to the other."""
    return split_unit(unit_a)[1] == split_unit(unit_b)[1]


def convert(value: float, from_unit: str, to_unit: str) -> float:
    """Convert ``value``. Raises ``ValueError`` when the units are incompatible."""
    from_scale, from_base = split_unit(from_unit)
    to_scale, to_base = split_unit(to_unit)
    if from_base != to_base:
        raise ValueError(f"cannot convert {from_unit!r} to {to_unit!r}")
    if from_scale == to_scale:
        return value
    # 0.0009 A in mA comes out as 0.8999999999999999 otherwise, and that would fail a limit of 0.9.
    return float(f"{value * from_scale / to_scale:.15g}")
