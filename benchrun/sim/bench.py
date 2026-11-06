"""Starts a full simulated bench: all instruments around one device under test."""

from __future__ import annotations

import threading
from typing import Dict, Iterable, Mapping, Optional, Sequence

from benchrun.errors import PlanError
from benchrun.sim.instruments import FAULT_KINDS, INSTRUMENT_CLASSES, Fault, SimInstrument
from benchrun.sim.model import DUT_VARIANTS, BenchModel, RegulatorParams
from benchrun.sim.server import InstrumentServer

DEFAULT_BASE_PORT = 5025
PORT_ORDER = ("dmm", "psu", "eload", "scope", "siggen")


def parse_fault(text: str) -> Dict[str, Fault]:
    """Parse ``instrument:kind[:after]``, for example ``dmm:drop:5``."""
    pieces = text.split(":")
    problem = (
        f"bad fault {text!r}; expected instrument:kind[:after] with instrument in "
        f"{', '.join(PORT_ORDER)} and kind in {', '.join(FAULT_KINDS)}"
    )
    if len(pieces) not in (2, 3) or pieces[0] not in PORT_ORDER or pieces[1] not in FAULT_KINDS:
        raise PlanError([problem])
    after = 0
    if len(pieces) == 3:
        if not pieces[2].isdigit():
            raise PlanError([problem])
        after = int(pieces[2])
    if pieces[0] == "siggen" and pieces[1] in ("garbage", "overload"):
        raise PlanError([f"bad fault {text!r}; the generator has no readings to corrupt, so use drop, hang or error"])
    return {pieces[0]: Fault(pieces[1], after)}


class SimBench:
    """All simulated instruments, each served on its own localhost port.

    With ``base_port=0`` the operating system picks free ephemeral ports.
    Otherwise instruments take consecutive ports from ``base_port``, in the
    order of ``PORT_ORDER``. Use as a context manager, or call start and stop.
    """

    def __init__(
        self,
        dut: str = "good",
        seed: int = 1,
        noise: float = 1.0,
        base_port: int = 0,
        faults: Optional[Mapping[str, Fault]] = None,
        params: Optional[RegulatorParams] = None,
    ):
        if params is None:
            if dut not in DUT_VARIANTS:
                raise PlanError([f"unknown simulated unit {dut!r}; choose from {', '.join(DUT_VARIANTS)}"])
            params = DUT_VARIANTS[dut]
        self.dut = dut
        self.seed = seed
        self.model = BenchModel(params)
        self.lock = threading.Lock()
        self.base_port = base_port
        faults = faults or {}
        self.devices: Dict[str, SimInstrument] = {
            kind: INSTRUMENT_CLASSES[kind](self.model, seed=seed, noise=noise, fault=faults.get(kind))
            for kind in PORT_ORDER
        }
        self.servers: Dict[str, InstrumentServer] = {}

    def start(self) -> "SimBench":
        try:
            for offset, kind in enumerate(PORT_ORDER):
                port = self.base_port + offset if self.base_port else 0
                server = InstrumentServer(self.devices[kind], self.lock, port)
                self.servers[kind] = server
                server.start()
        except OSError:
            self.stop()
            raise
        return self

    def stop(self) -> None:
        for server in self.servers.values():
            server.stop()
        self.servers.clear()

    def __enter__(self) -> "SimBench":
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    @property
    def addresses(self) -> Dict[str, str]:
        """Address of each simulated instrument, keyed by driver name."""
        return {kind: server.address for kind, server in self.servers.items()}

    def addresses_for(self, instruments: Iterable[Sequence[str]]) -> Dict[str, str]:
        """Map plan instrument names to addresses, given (name, driver) pairs."""
        found = {}
        for name, driver in instruments:
            if driver in self.servers:
                found[name] = self.servers[driver].address
        return found
