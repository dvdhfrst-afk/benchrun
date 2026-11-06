"""Shared test helpers."""

from __future__ import annotations

import copy
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from benchrun.plan import Plan, apply_addresses, parse_plan
from benchrun.runner import Runner, RunResult
from benchrun.sim.bench import SimBench
from benchrun.store import Store
from benchrun.transport import Transport


class FakeTransport(Transport):
    """Scripted transport. Replies are consumed by queries; error checks are clean."""

    description = "fake"

    def __init__(self, replies: Optional[List[str]] = None, errors: Optional[List[str]] = None):
        self.replies = list(replies or [])
        self.errors = list(errors or [])
        self.sent: List[str] = []
        self.pending: List[str] = []
        self.opened = False
        self.closed = False
        self.stale = ""

    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.closed = True

    def write_line(self, text: str) -> None:
        self.sent.append(text)
        if text == "SYST:ERR?":
            self.pending.append(self.errors.pop(0) if self.errors else '+0,"No error"')
        elif "?" in text:
            self.pending.append(self.replies.pop(0))

    def read_line(self) -> str:
        return self.pending.pop(0)

    def discard_pending(self) -> str:
        stale, self.stale = self.stale, ""
        return stale

    @property
    def commands(self) -> List[str]:
        """What was sent, leaving out the error queue checks."""
        return [line for line in self.sent if line != "SYST:ERR?"]


def base_plan() -> Dict[str, Any]:
    """A small plan on supply and multimeter: 5 V regulator at three input voltages."""
    return {
        "name": "small",
        "version": "1",
        "vars": {"vnom": 5.0},
        "instruments": {
            "supply": {"driver": "psu", "timeout_s": 2.0},
            "dmm": {"driver": "dmm", "timeout_s": 2.0},
        },
        "setup": [
            {"do": "supply.set_current_limit", "amps": 1.0},
            {"do": "supply.output_on"},
        ],
        "steps": [
            {
                "name": "vout_vs_vin",
                "sweep": {"var": "vin", "values": [8.0, 12.0, 16.0]},
                "apply": [{"do": "supply.set_voltage", "volts": "=vin"}],
                "measure": [
                    {
                        "name": "vout",
                        "read": "dmm.measure_dc_voltage",
                        "units": "V",
                        "low": "=vnom - 0.1",
                        "high": "=vnom + 0.1",
                    }
                ],
                "summary": [{"name": "span_mv", "calc": "span(vout) * 1000", "units": "mV", "high": 20.0}],
            },
            {
                "name": "final_check",
                "setup": [{"do": "supply.set_voltage", "volts": 12.0}],
                "measure": [{"name": "vout", "read": "dmm.measure_dc_voltage", "units": "V", "low": 4.9, "high": 5.1}],
            },
        ],
        "teardown": [{"do": "supply.output_off"}],
    }


def clone(data: Dict[str, Any]) -> Dict[str, Any]:
    return copy.deepcopy(data)


class BenchTestCase(unittest.TestCase):
    """Gives each test a temp folder and results database, and runs plans on simulators."""

    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp(prefix="benchrun-test-"))
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.store = Store(self.folder / "results.sqlite")
        self.addCleanup(self.store.close)

    def start_bench(self, **kwargs: Any) -> SimBench:
        kwargs.setdefault("noise", 0.0)
        bench = SimBench(**kwargs).start()
        self.addCleanup(bench.stop)
        return bench

    def on_bench(self, data: Dict[str, Any], bench: SimBench) -> Plan:
        plan = parse_plan(data)
        apply_addresses(plan, bench.addresses_for((s.name, s.driver) for s in plan.instruments.values()))
        return plan

    def run_plan(self, data: Dict[str, Any], serial: str = "SN-1", **bench_kwargs: Any) -> Tuple[RunResult, Dict[str, Any], SimBench]:
        bench = self.start_bench(**bench_kwargs)
        plan = self.on_bench(data, bench)
        result = Runner(plan, self.store, serial, sleep=lambda _: None).run()
        return result, self.store.run_detail(result.run_id), bench


def all_measurements(detail: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [m for step in detail["steps"] for m in step["measurements"]]


def step_named(detail: Dict[str, Any], name: str) -> Dict[str, Any]:
    return next(step for step in detail["steps"] if step["name"] == name)
