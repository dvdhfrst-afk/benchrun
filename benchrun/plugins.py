"""Drivers, and simulators for them, from installed packages.

A package offers a driver through the ``benchrun.drivers`` entry point group, and
may offer a simulator for it under the same name in ``benchrun.simulators``.
Plugins are looked for once, the first time a plan names a driver benchrun does
not have, or a command lists drivers or starts simulators. A plugin that cannot be
imported, or is not a proper driver, is set aside with the reason: anything else
keeps working, and a plan that names it is refused with that reason.
"""

from __future__ import annotations

import importlib.metadata
import inspect
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from benchrun.drivers import DRIVERS
from benchrun.drivers.base import Instrument
from benchrun.sim.instruments import SimInstrument

DRIVER_GROUP = "benchrun.drivers"
SIMULATOR_GROUP = "benchrun.simulators"
BUILT_IN = frozenset(DRIVERS)
MAX_PROBLEM_CHARS = 300

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass
class Plugin:
    """One entry point, and what became of it."""

    name: str
    group: str
    target: str  # Where the entry point says the class is, such as ``package.module:Class``.
    package: str  # The installed package that offers it, with its version, where Python can tell.
    obj: Any = None  # The class, once loaded and found usable.
    problem: Optional[str] = None

    @property
    def where(self) -> str:
        return f"{self.package}, {self.target}" if self.package else self.target


_found: Optional[Tuple[Dict[str, Plugin], Dict[str, Plugin]]] = None


def _entry_points(group: str) -> List[Any]:
    """Entry points in ``group``, on Python 3.9 (a dict of groups) and on later versions alike."""
    everything = importlib.metadata.entry_points()
    if hasattr(everything, "select"):
        items: Iterable[Any] = everything.select(group=group)
    else:
        items = everything.get(group, ())
    unique: Dict[Tuple[str, str], Any] = {}
    for item in items:  # A package on the path twice is listed twice.
        unique.setdefault((item.name, item.value), item)
    return list(unique.values())


def _package(entry: Any) -> str:
    dist = getattr(entry, "dist", None)  # Not there on Python 3.9.
    if dist is None:
        return ""
    try:
        return f"{dist.metadata['Name']} {dist.version}"
    except Exception:  # Broken metadata must not stop the listing.
        return ""


def _short(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text if len(text) <= MAX_PROBLEM_CHARS else text[: MAX_PROBLEM_CHARS - 3] + "..."


def _load(entry: Any, group: str) -> Plugin:
    plugin = Plugin(str(entry.name), group, str(entry.value), _package(entry))
    if not _NAME_RE.match(plugin.name):
        plugin.problem = "the name must be 1 to 64 characters: lower-case letters, digits and _, starting with a letter"
        return plugin
    if plugin.name in BUILT_IN:
        plugin.problem = f"benchrun has a built-in {'driver' if group == DRIVER_GROUP else 'simulator'} of that name, which a plugin cannot replace"
        return plugin
    try:
        obj = entry.load()
    except (Exception, SystemExit) as exc:  # Whatever a plugin does on import, the rest must go on.
        plugin.problem = f"it cannot be imported: {_short(exc)}"
        return plugin
    try:
        plugin.problem = driver_class_problem(obj) if group == DRIVER_GROUP else simulator_class_problem(obj, plugin.name)
    except Exception as exc:  # A class that breaks when looked at is not a usable driver.
        plugin.problem = f"it cannot be checked: {_short(exc)}"
    if plugin.problem is None:
        plugin.obj = obj
    return plugin


def driver_class_problem(obj: Any) -> Optional[str]:
    """Why a class cannot be used as a driver, or None."""
    if not inspect.isclass(obj) or not issubclass(obj, Instrument) or obj is Instrument:
        return "it is not a driver: it must be a class based on benchrun.drivers.base.Instrument"
    commands = obj.COMMANDS
    if not isinstance(commands, dict):
        return "its COMMANDS must be a dictionary of command names to SCPI text"
    missing = sorted(set(Instrument.COMMANDS) - set(commands))
    if missing:
        return f"its COMMANDS must keep every entry of the base class; missing: {', '.join(missing)}"
    for key, template in commands.items():
        if not isinstance(key, str) or not isinstance(template, str):
            return "its COMMANDS must map text to text"
        if any(ord(ch) < 32 or ord(ch) > 126 for ch in template):
            return f"its command {key!r} has a line break, a control character or a non-ASCII character"
    if obj.HAS_OUTPUT and obj.safe_state is Instrument.safe_state:
        return "it has an output (HAS_OUTPUT) but does not say how to switch it off: it must override safe_state"
    return None


def simulator_class_problem(obj: Any, name: str) -> Optional[str]:
    """Why a class cannot simulate the driver ``name``, or None."""
    if not inspect.isclass(obj) or not issubclass(obj, SimInstrument):
        return "it is not a simulator: it must be a class based on benchrun.sim.instruments.SimInstrument"
    if obj.kind != name:
        return f"its kind is {obj.kind!r}; it must be {name!r}, the name of the driver it simulates"
    if not isinstance(obj.idn, str) or any(ord(ch) < 32 or ord(ch) > 126 for ch in obj.idn):
        return "its idn must be one line of plain ASCII text"
    return None


def _discover() -> Tuple[Dict[str, Plugin], Dict[str, Plugin]]:
    found: List[Dict[str, Plugin]] = []
    for group in (DRIVER_GROUP, SIMULATOR_GROUP):
        loaded: Dict[str, Plugin] = {}
        for entry in _entry_points(group):
            plugin = _load(entry, group)
            if plugin.name in loaded:
                other = loaded[plugin.name]
                for clash in (plugin, other):
                    clash.obj = None
                    clash.problem = f"more than one package offers this name ({other.where}; {plugin.where}), so neither is used"
                plugin.name = f"{plugin.name} ({plugin.target})"  # Listed as well, under a name of its own.
            loaded[plugin.name] = plugin
        found.append(loaded)
    drivers, simulators = found
    for name, plugin in simulators.items():
        if plugin.obj is not None and (name not in drivers or drivers[name].obj is None):
            plugin.obj = None
            plugin.problem = "there is no usable driver plugin of this name for it to simulate"
    return drivers, simulators


def load() -> Tuple[Dict[str, Plugin], Dict[str, Plugin]]:
    """Find every plugin once, and make the usable drivers available to plans."""
    global _found
    if _found is None:
        _found = _discover()
        for name, plugin in _found[0].items():
            if plugin.obj is not None:
                DRIVERS[name] = plugin.obj
    return _found


def forget() -> None:
    """Drop what was found, so the next use looks again. For tests and long-lived callers."""
    global _found
    for name in list(DRIVERS):
        if name not in BUILT_IN:
            del DRIVERS[name]
    _found = None


def simulators() -> Dict[str, type]:
    """Usable plugin simulators by driver name."""
    return {name: plugin.obj for name, plugin in load()[1].items() if plugin.obj is not None}


def origin(name: str) -> str:
    """Where a driver comes from, for listings."""
    if name in BUILT_IN:
        return "built-in"
    plugin = load()[0].get(name)
    return f"plugin: {plugin.where}" if plugin is not None else "unknown"


def driver_problem(name: Any) -> Optional[str]:
    """Why a plan cannot use a driver of this name, or None when it can."""
    if isinstance(name, str) and name in BUILT_IN:
        return None
    drivers = load()[0]
    plugin = drivers.get(name) if isinstance(name, str) else None
    if plugin is not None and plugin.problem is not None:
        return f"driver {name!r} comes from a plugin ({plugin.where}) that cannot be used: {plugin.problem}"
    if plugin is not None:
        return None
    return f"unknown driver {name!r}; available: {', '.join(sorted(DRIVERS))}"
