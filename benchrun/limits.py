"""Limits and verdicts for a single measurement."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

PASS = "pass"
FAIL = "fail"
INFO = "info"  # Recorded for traceability; carries no limits, cannot fail a unit.
ERROR = "error"  # The measurement could not be taken.


@dataclass(frozen=True)
class Limit:
    """Inclusive limits. Either side may be absent for a one-sided test."""

    low: Optional[float] = None
    high: Optional[float] = None

    @property
    def is_limited(self) -> bool:
        return self.low is not None or self.high is not None

    def evaluate(self, value: Optional[float]) -> str:
        """Return the verdict for ``value``.

        A missing or non-finite value never passes: a limited measurement fails
        and an unlimited one is an error, so neither can hide a bad reading.
        """
        if value is None or not math.isfinite(value):
            return FAIL if self.is_limited else ERROR
        if not self.is_limited:
            return INFO
        if self.low is not None and value < self.low:
            return FAIL
        if self.high is not None and value > self.high:
            return FAIL
        return PASS

    def describe(self, units: str = "") -> str:
        suffix = f" {units}" if units else ""
        if self.low is not None and self.high is not None:
            return f"{self.low:g} to {self.high:g}{suffix}"
        if self.low is not None:
            return f">= {self.low:g}{suffix}"
        if self.high is not None:
            return f"<= {self.high:g}{suffix}"
        return "none"
