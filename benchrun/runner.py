"""Executes a plan against instruments for one unit and records everything.

Result rules, in order of precedence:

* aborted: an instrument fault, a failed connection, an identity mismatch, an
  operator interrupt, or a failed teardown. Readings cannot be fully trusted.
* fail: every instrument behaved and at least one measurement failed. A reading
  past the instrument's range and a calculation with no answer both count.
* pass: every limited measurement passed, and at least one was evaluated.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import IO, Any, Callable, Dict, List, Mapping, Optional

from benchrun import __version__
from benchrun.drivers import DRIVERS
from benchrun.drivers.base import Instrument
from benchrun.errors import ExpressionError, IdentityMismatch, InstrumentFault, ReadingOverload
from benchrun.limits import ERROR, FAIL, PASS, Limit
from benchrun.plan import Call, Measure, Plan, Step, resolve
from benchrun.store import Store
from benchrun.transport import LoggingTransport, TcpTransport, Transport, parse_address
from benchrun.units import convert

RUN_PASS = "pass"
RUN_FAIL = "fail"
RUN_ABORTED = "aborted"

Console = Callable[[str], None]


@dataclass
class RunResult:
    run_id: int
    status: str
    reason: Optional[str]
    failed: int
    passed: int


class _RunAborted(Exception):
    """Raised after a step has recorded the fault that ends the run."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class _StopOnFail(Exception):
    """A step marked stop_on_fail has failed; later steps are not attempted."""


def _silent(_: str) -> None:
    pass


def default_transport(address: str, timeout_s: float) -> Transport:
    host, port = parse_address(address)
    return TcpTransport(host, port, timeout_s)


class Runner:
    """Runs one plan on one unit. Create a new Runner for each run."""

    def __init__(
        self,
        plan: Plan,
        store: Store,
        serial: str,
        operator: str = "",
        console: Console = _silent,
        scpi_log: Optional[IO[str]] = None,
        transport_factory: Callable[[str, float], Transport] = default_transport,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.plan = plan
        self.store = store
        self.serial = serial
        self.operator = operator
        self.console = console
        self.scpi_log = scpi_log
        self.transport_factory = transport_factory
        self.sleep = sleep
        self.instruments: Dict[str, Instrument] = {}
        self.run_id = -1
        self.limited_evaluated = 0
        self.passed = 0
        self.failed = 0

    # The run -------------------------------------------------------------

    def run(self) -> RunResult:
        plan = self.plan
        self.run_id = self.store.create_run(
            self.serial,
            plan.name,
            plan.version,
            plan.source_hash,
            plan.source_text,
            plan.variables,
            self.operator,
            __version__,
        )
        self.console(f"Run {self.run_id}: serial {self.serial}, plan {plan.name} {plan.version}".rstrip())
        setup_id = self.store.add_step(self.run_id, 0, "(setup)", "setup")
        step_ids = [self.store.add_step(self.run_id, i + 1, s.name, "test") for i, s in enumerate(plan.steps)]
        teardown_id = self.store.add_step(self.run_id, len(plan.steps) + 1, "(teardown)", "teardown")

        reason: Optional[str] = None
        skip_message = "not run: an earlier step failed with stop_on_fail set"
        try:
            try:
                self._run_setup(setup_id)
                for step, step_id in zip(plan.steps, step_ids):
                    self._run_step(step, step_id)
            except _StopOnFail:
                pass
            except _RunAborted as exc:
                reason = exc.reason
            except KeyboardInterrupt:
                reason = "interrupted by the operator"
                self._mark_running_step_aborted(reason)
            except Exception as exc:  # A bug must not read as a verdict, or lose the record.
                reason = f"internal error: {type(exc).__name__}: {exc}"
                self._mark_running_step_aborted(reason)
            try:
                teardown_problem = self._run_teardown(teardown_id)
            except KeyboardInterrupt:
                teardown_problem = "interrupted by the operator"
                self._mark_running_step_aborted(teardown_problem)
            if teardown_problem and reason is None:
                reason = f"teardown failed: {teardown_problem}"
        finally:
            self._close_instruments()

        if reason is not None:
            skip_message = f"not run: {reason}"
        self.store.skip_pending_steps(self.run_id, skip_message)
        status, reason = self._decide(reason)
        self.store.finish_run(self.run_id, status, reason)
        self.console(f"RESULT: {status.upper()}" + (f" ({reason})" if reason else ""))
        return RunResult(self.run_id, status, reason, self.failed, self.passed)

    def _decide(self, reason: Optional[str]):
        if reason is not None:
            return RUN_ABORTED, reason
        if self.failed:
            return RUN_FAIL, None
        if self.limited_evaluated == 0:
            return RUN_ABORTED, "no limited measurement was evaluated, so the unit has no verdict"
        return RUN_PASS, None

    def _mark_running_step_aborted(self, reason: str) -> None:
        with self.store.conn:
            self.store.conn.execute(
                "UPDATE steps SET status = 'error', message = ? WHERE run_id = ? AND status = 'running'",
                (reason, self.run_id),
            )

    # Connecting ----------------------------------------------------------

    def _connect(self, name: str) -> None:
        spec = self.plan.instruments[name]
        row_id = self.store.add_instrument(self.run_id, name, spec.driver, spec.address or "")
        transport = self.transport_factory(spec.address or "", spec.timeout_s)
        if self.scpi_log is not None:
            transport = LoggingTransport(transport, name, self.scpi_log)
        driver = DRIVERS[spec.driver](transport, name, spec.commands)
        driver.connect()
        self.instruments[name] = driver
        idn = driver.identify()
        self.store.set_instrument_idn(row_id, idn)
        self.console(f"  {name:<10} {spec.address:<24} {idn}")
        if spec.expect_idn and not re.search(spec.expect_idn, idn):
            raise IdentityMismatch(f"*IDN? returned {idn!r}, which does not match {spec.expect_idn!r}", name)
        driver.reset()

    def _run_setup(self, step_id: int) -> None:
        self.store.begin_step(step_id)
        try:
            for name in self.plan.instruments:
                self._connect(name)
            self._run_calls(self.plan.setup, dict(self.plan.variables))
        except (InstrumentFault, ExpressionError) as exc:
            self.store.finish_step(step_id, "error", str(exc))
            self.console(f"[ERROR] setup: {exc}")
            raise _RunAborted(str(exc)) from exc
        self.store.finish_step(step_id, PASS)

    def _run_teardown(self, step_id: int) -> Optional[str]:
        """Always attempted. A dead instrument must not stop the others going safe."""
        self.store.begin_step(step_id)
        problems: List[str] = []
        env = dict(self.plan.variables)
        for call in self.plan.teardown:
            if call.instrument is not None and call.instrument not in self.instruments:
                continue
            try:
                self._run_call(call, env)
            except (InstrumentFault, ExpressionError, TypeError, ValueError) as exc:
                problems.append(str(exc))
        for name, instrument in self.instruments.items():
            try:
                instrument.safe_state()
            except InstrumentFault as exc:
                problems.append(f"could not make {name} safe: {exc}")
        message = "; ".join(problems) if problems else None
        self.store.finish_step(step_id, "error" if problems else PASS, message)
        if problems:
            self.console(f"[ERROR] teardown: {message}")
        return message

    def _close_instruments(self) -> None:
        for instrument in self.instruments.values():
            instrument.close()

    # Steps ---------------------------------------------------------------

    def _run_calls(self, calls: List[Call], env: Mapping[str, Any]) -> None:
        for call in calls:
            self._run_call(call, env)

    def _run_call(self, call: Call, env: Mapping[str, Any]) -> None:
        if call.instrument is None:
            self.sleep(float(resolve(call.wait_s, env)))
            return
        kwargs = {key: resolve(value, env) for key, value in call.args.items()}
        getattr(self.instruments[call.instrument], call.method)(**kwargs)

    def _run_step(self, step: Step, step_id: int) -> None:
        self.store.begin_step(step_id)
        step_failed = [0]
        taken = [0]
        try:
            variables = self.plan.variables
            self._run_calls(step.setup, dict(variables))
            collected: Dict[str, List[Optional[float]]] = {m.name: [] for m in step.measures}
            for sweep in step.sweeps:
                collected[sweep.var] = []
            for point in step.points():
                env: Dict[str, Any] = {**variables, **point}
                for var, value in point.items():
                    collected[var].append(value)
                self._run_calls(step.apply, env)
                if step.settle_s:
                    self.sleep(step.settle_s)
                for measure in step.measures:
                    value = self._take(step_id, step, measure, env, point, step_failed)
                    taken[0] += 1
                    env[measure.name] = value
                    collected[measure.name].append(value)
            self._run_calls(step.cleanup, dict(variables))
            summary_env: Dict[str, Any] = {**variables, **collected}
            for measure in step.summary:
                value = self._take(step_id, step, measure, summary_env, {}, step_failed)
                taken[0] += 1
                summary_env[measure.name] = value
        except (InstrumentFault, ExpressionError) as exc:
            message = str(exc)
            self.store.finish_step(step_id, "error", message)
            self.console(f"[ERROR] {step.name}: {message}")
            raise _RunAborted(f"step {step.name}: {message}") from exc
        status = FAIL if step_failed[0] else PASS
        self.store.finish_step(step_id, status, f"{step_failed[0]} of {taken[0]} failed" if step_failed[0] else None)
        self.console(f"[{status.upper()}] {step.name} ({taken[0]} measured, {step_failed[0]} failed)")
        if step_failed[0] and step.stop_on_fail:
            raise _StopOnFail()

    # Measurements --------------------------------------------------------

    def _take(
        self,
        step_id: int,
        step: Step,
        measure: Measure,
        env: Mapping[str, Any],
        point: Mapping[str, float],
        step_failed: List[int],
    ) -> Optional[float]:
        limit = Limit(_as_limit(resolve(measure.low, env)), _as_limit(resolve(measure.high, env)))
        note: Optional[str] = None
        kind = "calc" if measure.calc is not None else "read"
        if measure.calc is not None:
            try:
                value: Optional[float] = measure.calc.evaluate(env)
            except ExpressionError as exc:
                # The inputs came from the unit: a zero that cannot be divided
                # by means the unit misbehaved, so this is its failure.
                value = None
                note = f"could not calculate: {exc}"
        else:
            try:
                value = self._read(measure, env)
            except ReadingOverload as exc:
                # The instrument works, but the unit took the signal past its
                # range. That says something about the unit, so it counts as a fail.
                value = None
                note = str(exc)
            except InstrumentFault as exc:
                self._record(step_id, measure, kind, point, None, limit, ERROR, str(exc))
                raise
        verdict = limit.evaluate(value)
        if value is None:
            verdict = FAIL  # Never a pass, and an unlimited calculation must still count.
        self._record(step_id, measure, kind, point, value, limit, verdict, note)
        if limit.is_limited or verdict == FAIL:
            self.limited_evaluated += 1
        if verdict == PASS:
            self.passed += 1
        elif verdict == FAIL:
            self.failed += 1
            step_failed[0] += 1
            self._report_failure(step, measure, point, value, limit, note)
        return value

    def _read(self, measure: Measure, env: Mapping[str, Any]) -> float:
        instrument = self.instruments[measure.instrument]
        kwargs = {key: resolve(value, env) for key, value in measure.args.items()}
        reader = getattr(instrument, measure.method)
        total = 0.0
        for _ in range(measure.samples):
            total += reader(**kwargs)
        mean = total / measure.samples
        return convert(mean, measure.reading_unit or measure.units, measure.units)

    def _record(
        self,
        step_id: int,
        measure: Measure,
        kind: str,
        point: Mapping[str, float],
        value: Optional[float],
        limit: Limit,
        verdict: str,
        note: Optional[str],
    ) -> None:
        self.store.add_measurement(
            self.run_id,
            step_id,
            measure.name,
            kind,
            point,
            value,
            measure.units,
            limit.low,
            limit.high,
            verdict,
            note,
            measure.instrument,
            measure.samples,
        )

    def _report_failure(
        self,
        step: Step,
        measure: Measure,
        point: Mapping[str, float],
        value: Optional[float],
        limit: Limit,
        note: Optional[str],
    ) -> None:
        where = " ".join(f"{k}={v:g}" for k, v in point.items())
        shown = "no value" if value is None else f"{value:.6g} {measure.units}".rstrip()
        extra = f" ({note})" if note else ""
        at = f" at {where}" if where else ""
        self.console(f"    FAIL {step.name}.{measure.name}{at}: {shown}, limit {limit.describe(measure.units)}{extra}")


def _as_limit(value: Any) -> Optional[float]:
    return None if value is None else float(value)

