"""Statistics as pure functions over lists of finite floats, in the order they were measured.

Nothing here touches the database. Every result that cannot be computed, because
there are too few values, no spread, no limits, or numbers too large for a float,
is ``None`` rather than a number that would mislead. Formulas are in docs/statistics.md.
"""

from __future__ import annotations

import bisect
import math
from typing import Any, Dict, List, Optional, Sequence

D2 = 1.128  # Mean range of two normal values, in sigmas: turns the average moving range into sigma.
D4 = 3.267  # Upper limit of a moving range chart, as a multiple of the average moving range.
PERCENTILES = (1, 5, 25, 75, 95, 99)
MAX_BINS = 1000

RULES = {
    1: "1 point beyond 3 sigma",
    2: "2 of 3 points in a row beyond 2 sigma on the same side",
    3: "4 of 5 points in a row beyond 1 sigma on the same side",
    4: "8 points in a row on the same side of the centre line",
}


def _finite(value: Optional[float]) -> Optional[float]:
    return value if value is not None and math.isfinite(value) else None


def mean(values: Sequence[float]) -> Optional[float]:
    """Exactly rounded, so long series do not drift; ``None`` for no values."""
    if not values:
        return None
    count = len(values)
    try:
        result = _finite(math.fsum(values) / count)
    except OverflowError:  # The sum passes the largest float even though the mean would not.
        result = _finite(math.fsum(value / count for value in values))
    # Dividing can round past the values themselves: the mean of three 3.3s would be 3.2999999999999994.
    return None if result is None else min(max(result, min(values)), max(values))


def sample_stdev(values: Sequence[float]) -> Optional[float]:
    """Sample standard deviation, dividing by n - 1. ``None`` below 2 values."""
    if len(values) < 2:
        return None
    if min(values) == max(values):
        return 0.0
    centre = mean(values)
    if centre is None:
        return None
    try:
        total = math.fsum((value - centre) * (value - centre) for value in values)
    except OverflowError:
        return None
    return _finite(math.sqrt(total / (len(values) - 1)))


def percentile(ordered: Sequence[float], q: float) -> Optional[float]:
    """The q-th percentile of sorted values, interpolating linearly between closest ranks.

    Rank h = (n - 1) * q / 100 counted from 0, so the 0th is the minimum, the
    100th the maximum and the 50th the usual median.
    """
    if not ordered:
        return None
    position = (len(ordered) - 1) * q / 100.0
    below = int(math.floor(position))
    fraction = position - below
    if fraction == 0 or below + 1 >= len(ordered):
        return ordered[below]
    # Weighted this way, two values near the float limit cannot overflow on the way.
    return _finite(ordered[below] * (1.0 - fraction) + ordered[below + 1] * fraction)


def summarize(values: Sequence[float]) -> Dict[str, Any]:
    ordered = sorted(values)
    return {
        "count": len(values),
        "mean": mean(values),
        "stdev": sample_stdev(values),
        "minimum": ordered[0] if ordered else None,
        "median": percentile(ordered, 50),
        "maximum": ordered[-1] if ordered else None,
        "percentiles": {f"p{q}": percentile(ordered, q) for q in PERCENTILES},
    }


def moving_ranges(values: Sequence[float]) -> List[float]:
    """|x[i] - x[i-1]| for each value after the first. Infinite if the difference is too large for a float."""
    return [abs(after - before) for before, after in zip(values, values[1:])]


def within_sigma(values: Sequence[float]) -> Optional[float]:
    """Short-term sigma: the average moving range divided by d2 = 1.128. ``None`` without spread."""
    if len(values) < 2:
        return None
    average = mean(moving_ranges(values))
    if average is None or average <= 0:
        return None
    return average / D2


def _indices(centre: float, sigma: Optional[float], low: Optional[float], high: Optional[float]) -> List[Optional[float]]:
    """The two-sided index (Cp or Pp) and the worst-side index (Cpk or Ppk) for one sigma."""
    if sigma is None:
        return [None, None]
    sides = []
    if high is not None:
        sides.append((high - centre) / (3.0 * sigma))
    if low is not None:
        sides.append((centre - low) / (3.0 * sigma))
    spread = (high - low) / (6.0 * sigma) if low is not None and high is not None else None
    return [_finite(spread), _finite(min(sides))]


def capability(values: Sequence[float], low: Optional[float], high: Optional[float]) -> Dict[str, Any]:
    """Cp and Cpk from within-run sigma, Pp and Ppk from the overall sample sigma.

    Cp and Pp need both limits. With one limit, Cpk and Ppk use that side alone.
    ``note`` says why nothing was computed, if nothing was.
    """
    result: Dict[str, Any] = {"cp": None, "cpk": None, "pp": None, "ppk": None, "sigma_within": None, "sigma_overall": None, "note": None}
    if low is None and high is None:
        result["note"] = "no limits"
        return result
    if len(values) < 2:
        result["note"] = "fewer than 2 values"
        return result
    if min(values) == max(values):
        result["note"] = "no spread"
        return result
    centre = mean(values)
    overall = sample_stdev(values)
    within = within_sigma(values)
    if centre is None or overall is None or within is None:
        result["note"] = "values too large to compute"
        return result
    result["sigma_within"], result["sigma_overall"] = within, overall
    result["cp"], result["cpk"] = _indices(centre, within, low, high)
    result["pp"], result["ppk"] = _indices(centre, overall, low, high)
    return result


def sturges_bins(count: int) -> int:
    """ceil(log2 n) + 1 bins, the usual default for a near-normal sample."""
    return 1 if count <= 1 else int(math.ceil(math.log2(count))) + 1


def histogram(values: Sequence[float], bins: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Equal-width bins from the smallest value to the largest; bin i holds edge[i] <= x < edge[i+1], the last includes the top.

    With no spread every value lands in one bin, whatever was asked for.
    """
    if not values:
        return None
    rule = "given" if bins else "Sturges"
    wanted = bins or sturges_bins(len(values))
    bottom, top = min(values), max(values)
    if bottom == top:
        return {"rule": rule, "bins": 1, "edges": [bottom, top], "counts": [len(values)]}
    # Interpolated, so a range wider than the largest float still gives finite edges.
    edges = [bottom * (1.0 - i / wanted) + top * (i / wanted) for i in range(wanted + 1)]
    edges[0], edges[-1] = bottom, top
    counts = [0] * wanted
    for value in values:
        counts[min(bisect.bisect_right(edges, value) - 1, wanted - 1)] += 1
    return {"rule": rule, "bins": wanted, "edges": edges, "counts": counts}


def western_electric(z: Sequence[float]) -> Dict[int, List[int]]:
    """The indices of the points that make up each broken rule, from distances in sigmas from the centre line.

    "Beyond" is strict, so a point exactly on a zone line breaks nothing, and a point
    exactly on the centre line ends a run for rule 4.
    """
    hits: Dict[int, set] = {rule: set() for rule in RULES}
    hits[1].update(i for i, distance in enumerate(z) if abs(distance) > 3.0)
    for rule, size, need, line in ((2, 3, 2, 2.0), (3, 5, 4, 1.0)):
        for sign in (1.0, -1.0):
            beyond = [sign * distance > line for distance in z]
            for end in range(size - 1, len(z)):
                window = range(end - size + 1, end + 1)
                if sum(beyond[i] for i in window) >= need:
                    hits[rule].update(i for i in window if beyond[i])
    side, start = 0, 0
    for i, distance in enumerate(z):
        here = 1 if distance > 0 else -1 if distance < 0 else 0
        if here == 0 or here != side:
            side, start = here, i
        if here and i - start + 1 == 8:
            hits[4].update(range(start, i + 1))
        elif here and i - start + 1 > 8:
            hits[4].add(i)
    return {rule: sorted(points) for rule, points in hits.items()}


def control_chart(values: Sequence[float]) -> Dict[str, Any]:
    """An individuals and moving range chart with 3-sigma limits, and the Western Electric rules it breaks.

    ``note`` says why there is no chart, if there is none.
    """
    chart: Dict[str, Any] = {"count": len(values), "note": None}
    if len(values) < 2:
        chart["note"] = "fewer than 2 values"
        return chart
    if min(values) == max(values):
        chart["note"] = "no spread"
        return chart
    centre = mean(values)
    sigma = within_sigma(values)
    if centre is None or sigma is None:
        chart["note"] = "values too large to compute"
        return chart
    ranges = moving_ranges(values)
    average_range = sigma * D2
    z = [(value - centre) / sigma for value in values]
    chart.update(
        centre=centre,
        sigma=sigma,
        ucl=_finite(centre + 3.0 * sigma),
        lcl=_finite(centre - 3.0 * sigma),
        mr_centre=average_range,
        mr_ucl=_finite(D4 * average_range),
        moving_ranges=[None] + ranges,
        rules=western_electric(z),
    )
    return chart
