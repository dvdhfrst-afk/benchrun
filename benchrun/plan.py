"""Test plans: loading, validation and the objects the runner executes.

The format is documented in docs/plan_format.md. Validation reports every problem
at once and needs no instruments, so a typo surfaces before the bench is powered.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Union

from benchrun.drivers import DRIVERS
from benchrun.errors import ExpressionError, PlanError
from benchrun.expr import FUNCTIONS, Expression
from benchrun.transport import parse_address
from benchrun.units import compatible

Number = Union[int, float]
MaybeExpr = Union[Number, str, bool, None, Expression]

MAX_SWEEP_POINTS = 100000
DEFAULT_TIMEOUT_S = 5.0

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def resolve(value: MaybeExpr, env: Mapping[str, Any]) -> Any:
    """Evaluate an expression against ``env``; anything else passes through."""
    if isinstance(value, Expression):
        return value.evaluate(env)
    return value


@dataclass
class InstrumentSpec:
    name: str
    driver: str
    address: Optional[str] = None
    timeout_s: float = DEFAULT_TIMEOUT_S
    expect_idn: Optional[str] = None
    commands: Dict[str, str] = field(default_factory=dict)


@dataclass
class Call:
    """One line of a setup sequence: an instrument action, or a pause."""

    instrument: Optional[str] = None
    method: Optional[str] = None
    args: Dict[str, MaybeExpr] = field(default_factory=dict)
    wait_s: MaybeExpr = None


@dataclass
class Sweep:
    var: str
    values: List[float]


@dataclass
class Measure:
    """A reading from an instrument, or a value calculated from other values."""

    name: str
    units: str = ""
    low: MaybeExpr = None
    high: MaybeExpr = None
    instrument: Optional[str] = None
    method: Optional[str] = None
    args: Dict[str, MaybeExpr] = field(default_factory=dict)
    samples: int = 1
    reading_unit: Optional[str] = None
    calc: Optional[Expression] = None

    @property
    def is_limited(self) -> bool:
        return self.low is not None or self.high is not None


@dataclass
class Step:
    name: str
    description: str = ""
    setup: List[Call] = field(default_factory=list)
    sweeps: List[Sweep] = field(default_factory=list)
    apply: List[Call] = field(default_factory=list)
    settle_s: float = 0.0
    measures: List[Measure] = field(default_factory=list)
    cleanup: List[Call] = field(default_factory=list)
    summary: List[Measure] = field(default_factory=list)
    stop_on_fail: bool = False

    def points(self) -> List[Dict[str, float]]:
        """Every sweep point in order: the first sweep is the outer loop."""
        if not self.sweeps:
            return [{}]
        names = [sweep.var for sweep in self.sweeps]
        return [dict(zip(names, combo)) for combo in itertools.product(*(s.values for s in self.sweeps))]


@dataclass
class Plan:
    name: str
    version: str
    description: str
    variables: Dict[str, float]
    instruments: Dict[str, InstrumentSpec]
    setup: List[Call]
    steps: List[Step]
    teardown: List[Call]
    source_text: str = ""
    source_hash: str = ""

    def driver_of(self, instrument: str):
        return DRIVERS[self.instruments[instrument].driver]


class _Parser:
    """Collects every problem so that one run of ``validate`` shows them all."""

    def __init__(self, overrides: Optional[Mapping[str, float]] = None):
        self.problems: List[str] = []
        self.overrides = dict(overrides or {})
        self.instruments: Dict[str, InstrumentSpec] = {}
        self.list_names: Set[str] = set()  # Names that hold lists, in a step summary.

    def problem(self, where: str, text: str) -> None:
        self.problems.append(f"{where}: {text}")

    def check_keys(self, where: str, data: Mapping[str, Any], allowed: Sequence[str], required: Sequence[str] = ()):
        for key in data:
            if key not in allowed:
                self.problem(where, f"unknown key {key!r}; allowed: {', '.join(allowed)}")
        for key in required:
            if key not in data:
                self.problem(where, f"missing required key {key!r}")

    def as_dict(self, where: str, value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            return value
        self.problem(where, f"expected an object, got {type(value).__name__}")
        return {}

    def as_list(self, where: str, value: Any) -> List[Any]:
        if isinstance(value, list):
            return value
        self.problem(where, f"expected a list, got {type(value).__name__}")
        return []

    def check_name(self, where: str, name: Any) -> bool:
        if not isinstance(name, str) or not _NAME_RE.match(name):
            self.problem(where, f"name {name!r} must start with a letter or underscore and use only A-Z, 0-9, _")
            return False
        return True

    def number(self, where: str, value: Any, minimum: Optional[float] = None) -> Optional[float]:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            self.problem(where, f"expected a number, got {value!r}")
            return None
        if minimum is not None and value < minimum:
            self.problem(where, f"must be at least {minimum:g}, got {value!r}")
            return None
        return float(value)

    def maybe_expression(self, where: str, value: Any, scope: Set[str]) -> MaybeExpr:
        """Turn a ``"=..."`` string into an Expression and check its names."""
        if isinstance(value, str) and value.startswith("="):
            return self.expression(where, value[1:], scope)
        return value

    def expression(self, where: str, text: str, scope: Set[str]) -> Optional[Expression]:
        try:
            parsed = Expression(text)
        except ExpressionError as exc:
            self.problem(where, str(exc))
            return None
        unknown = parsed.names - scope
        if unknown:
            self.problem(where, f"expression {text!r} uses unknown names: {', '.join(sorted(unknown))}")
            return None
        shape = parsed.shape_problem(self.list_names)
        if shape:
            self.problem(where, f"expression {text!r}: {shape}")
            return None
        return parsed

    # Top level -----------------------------------------------------------

    def parse(self, data: Any, source_text: str) -> Plan:
        data = self.as_dict("plan", data)
        self.check_keys(
            "plan",
            data,
            ("name", "version", "description", "vars", "instruments", "setup", "steps", "teardown"),
            ("name", "instruments", "steps"),
        )
        name = data.get("name", "")
        if not isinstance(name, str) or not name.strip():
            self.problem("plan", "name must be a non-empty string")
            name = str(name)
        variables = self.parse_variables(data.get("vars", {}))
        self.parse_instruments(data.get("instruments", {}))
        scope = set(variables)
        setup = self.parse_calls("setup", data.get("setup", []), scope)
        teardown = self.parse_calls("teardown", data.get("teardown", []), scope)
        steps = self.parse_steps(data.get("steps", []), scope)
        if steps and not any(m.is_limited for s in steps for m in s.measures + s.summary):
            self.problem("plan", "no measurement has limits, so no run of this plan could ever pass or fail")
        return Plan(
            name=name,
            version=str(data.get("version", "")),
            description=str(data.get("description", "")),
            variables=variables,
            instruments=self.instruments,
            setup=setup,
            steps=steps,
            teardown=teardown,
            source_text=source_text,
            source_hash=hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        )

    def parse_variables(self, raw: Any) -> Dict[str, float]:
        variables: Dict[str, float] = {}
        for key, value in self.as_dict("vars", raw).items():
            where = f"vars.{key}"
            if not self.check_name(where, key):
                continue
            if key in FUNCTIONS:
                self.problem(where, "name clashes with a built-in function")
                continue
            parsed = self.number(where, value)
            if parsed is not None:
                variables[key] = parsed
        for key, value in self.overrides.items():
            if key in variables:
                variables[key] = float(value)
            else:
                self.problem("--set", f"plan has no variable named {key!r}")
        return variables

    def parse_instruments(self, raw: Any) -> None:
        instruments = self.as_dict("instruments", raw)
        if not instruments:
            self.problem("instruments", "a plan needs at least one instrument")
        for name, spec in instruments.items():
            where = f"instruments.{name}"
            if not self.check_name(where, name):
                continue
            spec = self.as_dict(where, spec)
            self.check_keys(
                where, spec, ("driver", "address", "timeout_s", "expect_idn", "commands"), ("driver",)
            )
            driver = spec.get("driver")
            if driver not in DRIVERS:
                self.problem(where, f"unknown driver {driver!r}; available: {', '.join(sorted(DRIVERS))}")
                continue
            address = spec.get("address")
            if address is not None:
                try:
                    parse_address(str(address))
                except PlanError as exc:
                    self.problem(where, str(exc))
            timeout = self.number(f"{where}.timeout_s", spec.get("timeout_s", DEFAULT_TIMEOUT_S), 0.001)
            expect = spec.get("expect_idn")
            if expect is not None:
                try:
                    re.compile(str(expect))
                except re.error as exc:
                    self.problem(where, f"expect_idn is not a valid regular expression: {exc}")
            commands = self.as_dict(f"{where}.commands", spec.get("commands", {}))
            for key, template in commands.items():
                trouble = DRIVERS[driver].override_problem(key, str(template))
                if trouble:
                    self.problem(f"{where}.commands", trouble)
            self.instruments[name] = InstrumentSpec(
                name=name,
                driver=driver,
                address=None if address is None else str(address),
                timeout_s=timeout if timeout is not None else DEFAULT_TIMEOUT_S,
                expect_idn=None if expect is None else str(expect),
                commands={str(k): str(v) for k, v in commands.items()},
            )

    # Calls ---------------------------------------------------------------

    def split_target(self, where: str, target: Any):
        """Split ``"supply.set_voltage"`` and check the instrument exists."""
        if not isinstance(target, str) or target.count(".") != 1:
            self.problem(where, f"expected 'instrument.method', got {target!r}")
            return None, None
        instrument, method = target.split(".")
        if instrument not in self.instruments:
            self.problem(where, f"unknown instrument {instrument!r}; plan has: {', '.join(sorted(self.instruments))}")
            return None, None
        return instrument, method

    def parse_call(self, where: str, raw: Any, scope: Set[str]) -> Optional[Call]:
        raw = self.as_dict(where, raw)
        if "wait_s" in raw:
            self.check_keys(where, raw, ("wait_s",))
            wait = self.maybe_expression(f"{where}.wait_s", raw["wait_s"], scope)
            if not isinstance(wait, Expression):
                self.number(f"{where}.wait_s", wait, 0.0)
            return Call(wait_s=wait)
        if "do" not in raw:
            self.problem(where, "expected 'do' or 'wait_s'")
            return None
        instrument, method = self.split_target(where, raw["do"])
        if instrument is None:
            return None
        driver = self.instruments[instrument].driver
        actions = DRIVERS[driver].actions()
        if method not in actions:
            self.problem(where, f"driver {driver!r} has no action {method!r}; available: {', '.join(sorted(actions))}")
            return None
        kwargs = {k: v for k, v in raw.items() if k != "do"}
        trouble = DRIVERS[driver].bind_problem(actions[method], kwargs)
        if trouble:
            self.problem(where, f"{raw['do']}: {trouble}")
            return None
        args = {k: self.maybe_expression(f"{where}.{k}", v, scope) for k, v in kwargs.items()}
        return Call(instrument=instrument, method=method, args=args)

    def parse_calls(self, where: str, raw: Any, scope: Set[str]) -> List[Call]:
        calls = []
        for index, item in enumerate(self.as_list(where, raw)):
            call = self.parse_call(f"{where}[{index}]", item, scope)
            if call is not None:
                calls.append(call)
        return calls

    # Steps ---------------------------------------------------------------

    def parse_steps(self, raw: Any, scope: Set[str]) -> List[Step]:
        items = self.as_list("steps", raw)
        if not items:
            self.problem("steps", "a plan needs at least one step")
        steps: List[Step] = []
        seen: Set[str] = set()
        for index, item in enumerate(items):
            where = f"steps[{index}]"
            item = self.as_dict(where, item)
            name = item.get("name")
            if self.check_name(where, name):
                where = f"step {name}"
                if name in seen:
                    self.problem(where, "step name is used twice")
                seen.add(name)
            steps.append(self.parse_step(where, item, scope))
        return steps

    def parse_step(self, where: str, item: Dict[str, Any], plan_scope: Set[str]) -> Step:
        self.check_keys(
            where,
            item,
            (
                "name",
                "description",
                "setup",
                "sweep",
                "apply",
                "settle_s",
                "measure",
                "cleanup",
                "summary",
                "stop_on_fail",
            ),
            ("name",),
        )
        sweeps = self.parse_sweeps(where, item.get("sweep", []), plan_scope)
        point_scope = set(plan_scope) | {s.var for s in sweeps}
        settle = self.number(f"{where}.settle_s", item.get("settle_s", 0.0), 0.0)
        stop_on_fail = item.get("stop_on_fail", False)
        if not isinstance(stop_on_fail, bool):
            self.problem(where, "stop_on_fail must be true or false")
            stop_on_fail = False
        step = Step(
            name=str(item.get("name", "")),
            description=str(item.get("description", "")),
            setup=self.parse_calls(f"{where}.setup", item.get("setup", []), plan_scope),
            sweeps=sweeps,
            apply=self.parse_calls(f"{where}.apply", item.get("apply", []), point_scope),
            settle_s=settle if settle is not None else 0.0,
            cleanup=self.parse_calls(f"{where}.cleanup", item.get("cleanup", []), plan_scope),
            stop_on_fail=stop_on_fail,
        )
        taken: Set[str] = set(point_scope)
        for index, raw in enumerate(self.as_list(f"{where}.measure", item.get("measure", []))):
            measure = self.parse_measure(f"{where}.measure[{index}]", raw, point_scope, taken, False)
            if measure is not None:
                step.measures.append(measure)
                point_scope = point_scope | {measure.name}
        summary_scope = set(point_scope)
        self.list_names = set(point_scope) - plan_scope
        for index, raw in enumerate(self.as_list(f"{where}.summary", item.get("summary", []))):
            measure = self.parse_measure(f"{where}.summary[{index}]", raw, summary_scope, taken, True)
            if measure is not None:
                step.summary.append(measure)
                summary_scope.add(measure.name)
        self.list_names = set()
        if not step.measures and not step.summary:
            self.problem(where, "step has nothing to measure")
        return step

    def parse_sweeps(self, where: str, raw: Any, scope: Set[str]) -> List[Sweep]:
        items = raw if isinstance(raw, list) else [raw]
        sweeps: List[Sweep] = []
        for index, item in enumerate(items):
            sweep = self.parse_sweep(f"{where}.sweep[{index}]", item)
            if sweep is None:
                continue
            if sweep.var in scope or any(s.var == sweep.var for s in sweeps):
                self.problem(where, f"sweep variable {sweep.var!r} is already defined")
                continue
            sweeps.append(sweep)
        return sweeps

    def parse_sweep(self, where: str, raw: Any) -> Optional[Sweep]:
        raw = self.as_dict(where, raw)
        self.check_keys(where, raw, ("var", "values", "start", "stop", "step", "points"), ("var",))
        var = raw.get("var")
        if not self.check_name(where, var):
            return None
        if var in FUNCTIONS:
            self.problem(where, f"sweep variable {var!r} clashes with a built-in function")
            return None
        if "values" in raw:
            if any(k in raw for k in ("start", "stop", "step", "points")):
                self.problem(where, "use either 'values' or 'start' and 'stop', not both")
            values = [self.number(where, v) for v in self.as_list(where, raw["values"])]
            clean = [v for v in values if v is not None]
            if not clean:
                self.problem(where, "sweep has no values")
                return None
            return Sweep(var, clean)
        start = self.number(f"{where}.start", raw.get("start"))
        stop = self.number(f"{where}.stop", raw.get("stop"))
        if start is None or stop is None:
            return None
        return self._linear_sweep(where, var, start, stop, raw)

    def _linear_sweep(self, where: str, var: str, start: float, stop: float, raw: Dict[str, Any]) -> Optional[Sweep]:
        if ("step" in raw) == ("points" in raw):
            self.problem(where, "give exactly one of 'step' or 'points'")
            return None
        if "points" in raw:
            points = raw["points"]
            if isinstance(points, bool) or not isinstance(points, int) or points < 2 or points > MAX_SWEEP_POINTS:
                self.problem(where, f"points must be a whole number from 2 to {MAX_SWEEP_POINTS}")
                return None
            increment = (stop - start) / (points - 1)
            return Sweep(var, [round(start + i * increment, 12) for i in range(points)])
        step = self.number(f"{where}.step", raw["step"])
        if step is None:
            return None
        if step == 0 or (stop - start) * step < 0:
            self.problem(where, f"step {step:g} never gets from {start:g} to {stop:g}")
            return None
        count = int(math.floor((stop - start) / step + 1e-9)) + 1
        if count > MAX_SWEEP_POINTS:
            self.problem(where, f"sweep has {count} points; the most allowed is {MAX_SWEEP_POINTS}")
            return None
        return Sweep(var, [round(start + i * step, 12) for i in range(count)])

    def parse_measure(
        self, where: str, raw: Any, scope: Set[str], taken: Set[str], in_summary: bool
    ) -> Optional[Measure]:
        raw = self.as_dict(where, raw)
        self.check_keys(where, raw, ("name", "read", "args", "samples", "calc", "units", "low", "high"), ("name",))
        name = raw.get("name")
        if not self.check_name(where, name):
            return None
        if name in taken or name in FUNCTIONS:
            self.problem(where, f"name {name!r} is already used in this step, or is a variable or function")
            return None
        taken.add(name)
        units = raw.get("units", "")
        if not isinstance(units, str):
            self.problem(where, "units must be a string")
            units = ""
        low = self.maybe_expression(f"{where}.low", raw.get("low"), scope)
        high = self.maybe_expression(f"{where}.high", raw.get("high"), scope)
        for label, value in (("low", low), ("high", high)):
            if value is not None and not isinstance(value, Expression):
                self.number(f"{where}.{label}", value)
        if (
            isinstance(low, (int, float))
            and isinstance(high, (int, float))
            and not isinstance(low, bool)
            and not isinstance(high, bool)
            and low > high
        ):
            self.problem(where, f"low limit {low} is above high limit {high}")
        measure = Measure(name=name, units=units, low=low, high=high)
        has_read = "read" in raw
        has_calc = "calc" in raw
        if has_read == has_calc:
            self.problem(where, "give exactly one of 'read' or 'calc'")
            return None
        if has_calc:
            text = raw["calc"]
            if not isinstance(text, str):
                self.problem(where, "calc must be a string")
                return None
            measure.calc = self.expression(where, text.lstrip("="), scope)
            return measure if measure.calc is not None else None
        if in_summary:
            self.problem(where, "a step summary can only use 'calc', not 'read'")
            return None
        return self.parse_reading(where, raw, measure, scope)

    def parse_reading(self, where: str, raw: Dict[str, Any], measure: Measure, scope: Set[str]) -> Optional[Measure]:
        instrument, method = self.split_target(where, raw["read"])
        if instrument is None:
            return None
        driver = self.instruments[instrument].driver
        readings = DRIVERS[driver].readings()
        if method not in readings:
            self.problem(where, f"driver {driver!r} has no reading {method!r}; available: {', '.join(sorted(readings))}")
            return None
        kwargs = self.as_dict(f"{where}.args", raw.get("args", {}))
        trouble = DRIVERS[driver].bind_problem(readings[method], kwargs)
        if trouble:
            self.problem(where, f"{raw['read']}: {trouble}")
            return None
        reading_unit = readings[method]._bench_unit
        if reading_unit is not None:
            if "units" not in raw:
                measure.units = reading_unit
            elif not compatible(reading_unit, measure.units):
                self.problem(where, f"units {measure.units!r} do not fit {raw['read']}, which returns {reading_unit!r}")
                return None
        samples = raw.get("samples", 1)
        if isinstance(samples, bool) or not isinstance(samples, int) or samples < 1:
            self.problem(where, f"samples must be a whole number of at least 1, got {samples!r}")
            samples = 1
        measure.instrument = instrument
        measure.method = method
        measure.args = {k: self.maybe_expression(f"{where}.args.{k}", v, scope) for k, v in kwargs.items()}
        measure.samples = samples
        measure.reading_unit = reading_unit if reading_unit is not None else measure.units
        return measure


def parse_plan(data: Any, source_text: str = "", variables: Optional[Mapping[str, float]] = None) -> Plan:
    """Build a Plan from decoded JSON. Raises ``PlanError`` listing every problem."""
    parser = _Parser(variables)
    plan = parser.parse(data, source_text or json.dumps(data, sort_keys=True))
    if parser.problems:
        raise PlanError(parser.problems)
    return plan


def load_plan(path: Union[str, Path], variables: Optional[Mapping[str, float]] = None) -> Plan:
    """Read and validate a plan file."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise PlanError([f"cannot read plan {path}: {exc}"]) from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise PlanError([f"{path} is not valid JSON: {exc}"]) from exc
    return parse_plan(data, text, variables)


def load_addresses(path: Union[str, Path]) -> Dict[str, str]:
    """Read a station file: ``{"addresses": {"dmm": "192.0.2.10:5025", ...}}``."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise PlanError([f"cannot read station file {path}: {exc}"]) from exc
    except ValueError as exc:
        raise PlanError([f"station file {path} is not valid JSON: {exc}"]) from exc
    addresses = data.get("addresses") if isinstance(data, dict) else None
    if not isinstance(addresses, dict):
        raise PlanError([f'station file {path} needs an object like {{"addresses": {{"dmm": "host:5025"}}}}'])
    return {str(k): str(v) for k, v in addresses.items()}


def apply_addresses(plan: Plan, addresses: Mapping[str, str]) -> None:
    """Replace instrument addresses. Rejects names the plan does not have."""
    unknown = [name for name in addresses if name not in plan.instruments]
    if unknown:
        raise PlanError(
            [f"address given for unknown instrument {name!r}; plan has: {', '.join(sorted(plan.instruments))}" for name in unknown]
        )
    problems = []
    for name, address in addresses.items():
        try:
            parse_address(address)
        except PlanError as exc:
            problems.append(f"{name}: {exc}")
        plan.instruments[name].address = address
    missing = [spec.name for spec in plan.instruments.values() if not spec.address]
    problems.extend(f"instrument {name!r} has no address; set it in the plan, a station file or --address" for name in missing)
    if problems:
        raise PlanError(problems)
