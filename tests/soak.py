"""A long run for checking behaviour at scale. Not part of the unit tests.

    python tests/soak.py --units 500 --db soak.sqlite

Tests a stream of simulated units with the line and load regulation example, in one
process, sharing one database, the way a production station would. Every tenth
unit is a bad one and every 25th loses an instrument connection, and the script
checks that each outcome was recorded as the one expected and that the supply
ended up off every time. It prints what it measured; nothing else depends on it.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from benchrun.cli import main  # noqa: E402
from benchrun.plan import apply_addresses, load_plan  # noqa: E402
from benchrun.runner import Runner  # noqa: E402
from benchrun.sim.bench import SimBench  # noqa: E402
from benchrun.sim.instruments import Fault  # noqa: E402
from benchrun.store import Store  # noqa: E402

PLAN = ROOT / "examples" / "regulator_line_load.json"


def expected(index: int) -> str:
    if index % 25 == 24:
        return "aborted"
    if index % 10 == 9:
        return "fail"
    return "pass"


def one_unit(store: Store, index: int) -> str:
    plan = load_plan(PLAN)
    outcome = expected(index)
    faults = {"dmm": Fault("drop", 5)} if outcome == "aborted" else {}
    with SimBench(dut="poor-line" if outcome == "fail" else "good", seed=index, faults=faults) as bench:
        apply_addresses(plan, bench.addresses_for((s.name, s.driver) for s in plan.instruments.values()))
        for spec in plan.instruments.values():
            spec.timeout_s = 1.0
        result = Runner(plan, store, f"SOAK-{index:06d}", sleep=lambda _: None, console=lambda _: None).run()
        if bench.model.supply_on:
            raise SystemExit(f"unit {index}: the supply was left on")
        if result.unsafe:
            raise SystemExit(f"unit {index}: could not confirm off: {result.unsafe}")
    if result.status != outcome:
        raise SystemExit(f"unit {index}: expected {outcome}, recorded {result.status}: {result.reason}")
    return outcome


def timed(*argv: str) -> float:
    started = time.perf_counter()
    with open(os.devnull, "w", encoding="utf-8") as sink:
        stdout, sys.stdout = sys.stdout, sink
        try:
            code = main(list(argv))
        finally:
            sys.stdout = stdout
    if code != 0:
        raise SystemExit(f"{' '.join(argv)} exited with {code}")
    return time.perf_counter() - started


def main_soak() -> int:
    parser = argparse.ArgumentParser(description="Test many simulated units and report what was measured.")
    parser.add_argument("--units", type=int, default=500, help="how many units (default: 500)")
    parser.add_argument("--db", default="soak.sqlite", help="results database (default: soak.sqlite)")
    args = parser.parse_args()
    counts = {"pass": 0, "fail": 0, "aborted": 0}
    started = time.perf_counter()
    with Store(args.db) as store:
        first = time.perf_counter()
        for index in range(args.units):
            counts[one_unit(store, index)] += 1
            if index == 0:
                first = time.perf_counter() - first
        elapsed = time.perf_counter() - started
        runs = store.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        rows = store.conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0]
        still_running = store.conn.execute("SELECT COUNT(*) FROM runs WHERE status = 'running'").fetchone()[0]
    print(f"units tested: {args.units} ({counts['pass']} pass, {counts['fail']} fail, {counts['aborted']} aborted)")
    print(f"all outcomes as expected, supply off after every unit, runs left open: {still_running}")
    print(f"total time: {elapsed:.1f} s, {elapsed / args.units * 1000:.0f} ms per unit")
    print(f"database: {runs} runs, {rows} measurements, {os.path.getsize(args.db) / 1e6:.1f} MB")
    print(f"history --serial: {timed('history', '--serial', 'SOAK-000000', '--db', args.db) * 1000:.0f} ms")
    print(f"history --plan --stats: {timed('history', '--plan', 'regulator-line-load', '--stats', '--db', args.db) * 1000:.0f} ms")
    print(f"show --latest: {timed('show', '--latest', '--db', args.db) * 1000:.0f} ms")
    return 0


if __name__ == "__main__":
    sys.exit(main_soak())
