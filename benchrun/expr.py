"""A small, safe expression language for plans.

Used for sweep-dependent settings (``"=vin * 0.5"``), per-point calculations
(``"=vin * iin"``) and step summaries (``"=(max(vout) - min(vout)) * 1000"``).
Expressions are parsed with ``ast`` and only arithmetic, names and the functions
listed below are allowed. Nothing is ever passed to ``eval``.
"""

from __future__ import annotations

import ast
import math
import operator
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Set

from benchrun.errors import ExpressionError


def _sequence(value: Any, func: str) -> Sequence[float]:
    if not isinstance(value, (list, tuple)):
        raise ExpressionError(f"{func}() needs a list of values, such as a measurement in a step summary")
    if not value:
        raise ExpressionError(f"{func}() got an empty list")
    return value


def _first(values: Any) -> float:
    return _sequence(values, "first")[0]


def _last(values: Any) -> float:
    return _sequence(values, "last")[-1]


def _mean(values: Any) -> float:
    items = _sequence(values, "mean")
    return sum(items) / len(items)


def _span(values: Any) -> float:
    items = _sequence(values, "span")
    return max(items) - min(items)


def _count(values: Any) -> int:
    return len(_sequence(values, "count"))


FUNCTIONS: Dict[str, Callable[..., float]] = {
    "abs": abs,
    "min": min,
    "max": max,
    "sqrt": math.sqrt,
    "log10": math.log10,
    "first": _first,
    "last": _last,
    "mean": _mean,
    "span": _span,
    "sum": sum,
    "count": _count,
}

# Functions that only make sense on a list of values.
_LIST_ONLY = {"first", "last", "mean", "span", "sum", "count"}

_BINARY = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}


MAX_EXPRESSION_CHARS = 2000


class Expression:
    """A parsed expression. Create it once, evaluate it at every sweep point."""

    def __init__(self, text: str):
        self.text = text.strip()
        if len(self.text) > MAX_EXPRESSION_CHARS:
            raise ExpressionError(f"expression is longer than {MAX_EXPRESSION_CHARS} characters: {text[:60]!r}...")
        try:
            tree = ast.parse(self.text, mode="eval")
        except (SyntaxError, ValueError, MemoryError, RecursionError) as exc:
            raise ExpressionError(f"cannot parse expression {text[:200]!r}") from exc
        self._body = tree.body
        self.names: Set[str] = set()
        try:
            self._check(self._body)
        except RecursionError as exc:
            raise ExpressionError(f"expression is too deeply nested: {text[:60]!r}...") from exc

    def __repr__(self) -> str:
        return f"Expression({self.text!r})"

    def _check(self, node: ast.AST) -> None:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ExpressionError(f"only numbers are allowed in expression {self.text!r}")
        elif isinstance(node, ast.Name):
            self.names.add(node.id)
        elif isinstance(node, ast.BinOp):
            if type(node.op) not in _BINARY:
                raise ExpressionError(f"operator not allowed in expression {self.text!r}")
            self._check(node.left)
            self._check(node.right)
        elif isinstance(node, ast.UnaryOp):
            if type(node.op) not in _UNARY:
                raise ExpressionError(f"operator not allowed in expression {self.text!r}")
            self._check(node.operand)
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS:
                raise ExpressionError(
                    f"unknown function in expression {self.text!r}; available: {', '.join(sorted(FUNCTIONS))}"
                )
            if node.keywords:
                raise ExpressionError(f"keyword arguments are not allowed in expression {self.text!r}")
            for arg in node.args:
                self._check(arg)
        else:
            raise ExpressionError(f"construct not allowed in expression {self.text!r}")

    def shape_problem(self, list_names: Set[str]) -> Optional[str]:
        """Check lists are only used where a list fits. ``list_names`` hold lists.

        In a step summary a measurement is the list of its values over the sweep,
        so ``vout * 2`` is a mistake in the plan, not a fault in the unit. This
        finds such mistakes at validation time.
        """
        return self._shape(self._body, list_names)

    def _shape(self, node: ast.AST, list_names: Set[str]) -> Optional[str]:
        if isinstance(node, ast.Name):
            if node.id in list_names:
                return f"{node.id!r} is a list here; use it inside max(), min(), mean(), first(), last() or span()"
            return None
        if isinstance(node, ast.BinOp):
            return self._shape(node.left, list_names) or self._shape(node.right, list_names)
        if isinstance(node, ast.UnaryOp):
            return self._shape(node.operand, list_names)
        if isinstance(node, ast.Call):
            func = node.func.id  # type: ignore[attr-defined]
            args = node.args
            is_list = len(args) == 1 and isinstance(args[0], ast.Name) and args[0].id in list_names
            if func in _LIST_ONLY:
                return None if is_list else f"{func}() needs one measurement from this step's sweep"
            if func in ("min", "max") and is_list:
                return None
            if func in ("min", "max") and len(args) < 2:
                return f"{func}() of a single number has no meaning here"
            for arg in args:
                found = self._shape(arg, list_names)
                if found:
                    return found
        return None

    def evaluate(self, env: Mapping[str, Any]) -> float:
        """Evaluate to a finite float, or raise ``ExpressionError``."""
        try:
            result = self._eval(self._body, env)
        except ExpressionError:
            raise
        except KeyError as exc:
            raise ExpressionError(f"{self.text}: unknown name {exc}") from exc
        except (ArithmeticError, TypeError, ValueError, RecursionError) as exc:
            raise ExpressionError(f"{self.text}: {exc}") from exc
        if isinstance(result, bool) or not isinstance(result, (int, float)):
            raise ExpressionError(f"{self.text}: result is a list, not a number; wrap it in max(), mean() or similar")
        if isinstance(result, float) and not math.isfinite(result):
            raise ExpressionError(f"{self.text}: result is not a finite number")
        return float(result)

    def _eval(self, node: ast.AST, env: Mapping[str, Any]) -> Any:
        if isinstance(node, ast.Constant):
            return float(node.value)  # As floats, 9**9**9 overflows at once instead of filling memory.
        if isinstance(node, ast.Name):
            return env[node.id]
        if isinstance(node, ast.BinOp):
            result = _BINARY[type(node.op)](self._eval(node.left, env), self._eval(node.right, env))
            if isinstance(result, complex):
                raise ExpressionError(f"{self.text}: a negative number has no real fractional power")
            return result
        if isinstance(node, ast.UnaryOp):
            return _UNARY[type(node.op)](self._eval(node.operand, env))
        if isinstance(node, ast.Call):
            args = [self._eval(arg, env) for arg in node.args]
            return FUNCTIONS[node.func.id](*args)  # type: ignore[attr-defined]
        raise ExpressionError(f"construct not allowed in expression {self.text!r}")
