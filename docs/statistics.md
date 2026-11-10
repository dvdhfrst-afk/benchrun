# Production statistics

`benchrun stats` reads the results database and answers the questions a test engineer asks about
a batch of units: how capable is each measurement against its limits, is the process stable run to
run, what fraction of units pass the first time, and which measurements fail most often.

It only reads. Nothing in the database changes.

## Choosing runs

`--plan NAME` or `--product PART` is required. The other filters narrow the runs further:

| Option | Keeps |
|---|---|
| `--plan NAME` | runs of this plan |
| `--product PART` | runs recorded for this part number |
| `--station NAME` | runs recorded on this station |
| `--lot LOT` | runs in this lot, given by name or by numeric id |
| `--serial PREFIX` | serials that start with PREFIX (exactly: case counts, and `%` or `_` are ordinary characters) |
| `--since TIME`, `--until TIME` | runs started in this UTC window (`YYYY-MM-DD` or `YYYY-MM-DDTHH:MM`; `--until` with a date means the end of that day) |
| `--limit-runs N` | only the newest N runs that match everything above, by start time |
| `--step NAME` | only measurements of this step |
| `--measurement NAME` | only measurements with this name |

## What is left out, and why

Measurements count only from **complete runs**: runs that reached a verdict, pass or fail, on every
step of the plan.

- **Aborted runs** stopped because the bench failed (a lost connection, a timeout, an instrument
  error). They say nothing about the unit, and their last readings are often the ones taken as the
  bench went wrong.
- **Runs still `running`** have no result. Either they are still going, or the process stopped
  before recording one.
- **Runs of only some steps** (`run --step`) were not the whole test, so they would bias both yield
  and the populations of the steps they did run.

Every output starts with a line that counts them, for example
`16 runs selected, 15 complete runs used; left out: 1 aborted.`

Inside a complete run, a reading with no value (an overload, or a calculation that could not be
done) is counted in the `no value` column and in `fails`, but never in the statistics: there is no
number to use. A value that is not finite is treated the same way.

## Populations

Values are grouped by plan, step, measurement name, units and sweep point. A reading at 8 V input
and one at 16 V input are different populations, so each point of a sweep gets its own row.

If the limits of one measurement differ between runs (for example a plan run with `--set
vnom=5.05`), the group is split by set of limits. Each set has its own row, marked `limit set 1 of
2` and so on, numbered in the order they first appeared, and the summary says how many
measurements were split. Capability is never computed across different limits.

## Formulas

For values x1 ... xn in run order (by start time):

| Quantity | Formula |
|---|---|
| mean | (x1 + ... + xn) / n, summed exactly (`math.fsum`) and kept between the smallest and largest value |
| stdev (s) | sqrt( sum (xi - mean)^2 / (n - 1) ); none for fewer than 2 values, exactly 0 when all values are equal |
| percentile q | sort the values; rank h = (n - 1) q / 100 counted from 0; interpolate linearly between the values at floor(h) and floor(h) + 1. The median is q = 50. JSON and CSV also give the 1st, 5th, 25th, 75th, 95th and 99th |
| moving range | MRi = abs(xi - x(i-1)), for i = 2 ... n |
| within sigma | average of the moving ranges / 1.128 (d2 for subgroups of two) |
| overall sigma | s |
| Cp | (high - low) / (6 within sigma) |
| Cpk | min(high - mean, mean - low) / (3 within sigma) |
| Pp | (high - low) / (6 overall sigma) |
| Ppk | min(high - mean, mean - low) / (3 overall sigma) |

Cp and Cpk describe what the process could do if it stayed where it is from one unit to the next.
Pp and Ppk describe what it actually did across every unit selected. A process that drifts has a
Cpk well above its Ppk. Because the moving range depends on run order, Cp and Cpk change when the
same values come in a different order; Pp and Ppk do not.

With only one limit, Cp and Pp are not reported, and Cpk and Ppk use that side alone. Nothing is
reported, and the `note` column says why, when:

- the measurement has no limits (shown only in JSON and CSV, since it is the normal case for
  recorded values),
- there are fewer than 2 values,
- every value is the same (`no spread`), or
- the values are so far apart that the arithmetic leaves the range of a float.

Two values are enough to compute an index, but not to trust one: with fewer than about 25 values
the indices move a lot from one batch to the next.

### Histogram

`--histogram NAME` bins every population of measurement NAME. The bins have equal width from the
smallest value to the largest. Bin i holds values from its lower edge up to, but not including, its
upper edge; the last bin includes the largest value. The number of bins is Sturges' rule,
ceil(log2 n) + 1, unless `--bins N` (1 to 1000) is given. When every value is the same there is one
bin. The text output shows where the limits fall between the bins.

### Control chart

`--control NAME` draws an individuals and moving range chart for each population of NAME, with
values in run order:

| Line | Value |
|---|---|
| centre line | mean |
| UCL, LCL | mean + 3 within sigma, mean - 3 within sigma |
| moving range centre | average moving range |
| moving range UCL | 3.267 x average moving range |

and checks the Western Electric rules on the individuals:

1. One point beyond 3 sigma from the centre line.
2. Two of three points in a row beyond 2 sigma, on the same side.
3. Four of five points in a row beyond 1 sigma, on the same side.
4. Eight points in a row on the same side of the centre line.

"Beyond" is strict, so a point exactly on a zone line breaks nothing, and a point exactly on the
centre line ends a run for rule 4. For each rule the output lists the points that make up the
pattern (for rule 2, the two or three points beyond 2 sigma; for rule 4, every point of the run),
numbered from 1 in run order, with the run id and serial of each. There is no chart for fewer than 2
values or for values with no spread.

## Yield

`--yield` gives one row per plan. A **unit** is a serial with at least one complete run.

| Column | Meaning |
|---|---|
| runs | every selected run of the plan |
| pass, fail, aborted, running | runs of the whole plan by result |
| some steps | runs of only some steps, whatever their result |
| units | serials with at least one complete run |
| first-pass yield | units whose first complete run passed / units |
| final yield | units whose last complete run passed / units |
| retests | complete runs after each unit's first, added up |
| units retested | units with more than one complete run |

"First" and "last" follow the start time. Aborted and unfinished runs appear in the counts but not
in yield, so a unit whose first attempt aborted and whose second passed counts as a first-pass unit.

## Failure Pareto

`--pareto` counts failing readings in complete runs by plan, step and measurement name, the
measurement that failed in the most runs first. `runs` counts each run once however many sweep
points failed; `readings` counts every failing reading. `share` is that row's runs as a fraction of
all rows' runs, and `cumulative` adds the shares down the list.

## Output

`--format text` (the default) prints tables. `--format json` (or `--json`) gives everything,
including percentiles, both sigmas and every control chart point. `--format csv` gives one table per
view; text that a spreadsheet could run as a formula gets a leading apostrophe, as in the run
reports. `--format html` writes one self-contained page with inline CSS and SVG and no scripts: the
default view carries yield, the failure Pareto, the summary table and a histogram and control chart
for every population; the other views carry just what they show. Every name and value from the
database is escaped.

`--out FILE` writes the output to FILE all at once, or not at all. An existing file is only
replaced with `--force`.

`stats` reads the database a row at a time and never reads a run's stored plan. Measured on a test
database of 200,000 runs with 1,000,000 readings (940 MB): the summary took about 7 seconds, yield
and the Pareto about 2 seconds each, and `--limit-runs 1000` under 2 seconds. A control chart or
HTML page of every one of those readings took about 25 seconds and came to 66 to 78 MB, so on a
database that size use `--limit-runs`, `--since` or `--lot` for charts.

`stats` exits 0 when it showed something, and 2 for bad options, a missing database, no matching
runs, or a `--histogram` or `--control` name with no values.

## Worked example on the simulators

Twelve good units with different noise seeds, one unit whose output is set high and which then
passes on a second run, one unit with poor line regulation, and one run aborted by a dropped
multimeter connection:

```bash
python -m benchrun run examples/regulator_quick.json --serial SN-1001 --sim --seed 1 --db lot.sqlite
```

(and the same for SN-1002 to SN-1012 with seeds 2 to 12), then:

```bash
python -m benchrun run examples/regulator_quick.json --serial SN-1013 --sim --seed 13 --dut high-output --db lot.sqlite
```

```bash
python -m benchrun run examples/regulator_quick.json --serial SN-1013 --sim --seed 14 --db lot.sqlite
```

```bash
python -m benchrun run examples/regulator_quick.json --serial SN-1014 --sim --seed 15 --dut poor-line --db lot.sqlite
```

```bash
python -m benchrun run examples/regulator_quick.json --serial SN-1015 --sim --seed 16 --fault dmm:drop:2 --db lot.sqlite
```

The summary:

```bash
python -m benchrun stats --plan regulator-quick --db lot.sqlite
```

```text
16 runs selected, 15 complete runs used; left out: 1 aborted.

step            name  point   units  low  high  n   no value  fails  mean     stdev      min      median   max      cp    cpk   pp    ppk   note
--------------  ----  ------  -----  ---  ----  --  --------  -----  -------  ---------  -------  -------  -------  ----  ----  ----  ----  ----
output_voltage  vout  vin=8   V      4.9  5.1   15  0         1      5.01159  0.0649841  4.97999  4.99598  5.24602  1.02  0.90  0.51  0.45
output_voltage  vout  vin=12  V      4.9  5.1   15  0         1      5.01665  0.0645474  4.99991  4.99999  5.24997  1.05  0.88  0.52  0.43
output_voltage  vout  vin=16  V      4.9  5.1   15  0         1      5.02172  0.0643804  5.00391  5.00401  5.25397  1.02  0.80  0.52  0.41
```

The one high unit dominates. Its 5.25 V readings make the overall sigma about twice the run-to-run
sigma, so Ppk (0.45 at 8 V) sits well below Cpk (0.90). The control chart shows why:

```bash
python -m benchrun stats --plan regulator-quick --db lot.sqlite --control vout
```

```text
output_voltage / vout at vin=8 (V), plan regulator-quick, limits 4.9 to 5.1
  15 values in run order
  Centre 5.01159, sigma 0.0327025 (average moving range 0.0368885 / 1.128)
  LCL 4.91348, UCL 5.1097; moving range UCL 0.120515
  Rule 1 (1 point beyond 3 sigma): 13
  Rule 2 (2 of 3 points in a row beyond 2 sigma on the same side): none
  Rule 3 (4 of 5 points in a row beyond 1 sigma on the same side): none
  Rule 4 (8 points in a row on the same side of the centre line): 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12
```

followed by a table of every point with its run, serial and moving range. Point 13 is the first run
of SN-1013. Rule 4 fires on points 1 to 12 because that one unit lifts the centre line above every
good unit: a single outlier is enough to break both rules.

Yield and the failure Pareto:

```bash
python -m benchrun stats --plan regulator-quick --db lot.sqlite --yield
```

```text
plan             runs  pass  fail  aborted  running  some steps  units  first-pass yield  final yield  retests  units retested
---------------  ----  ----  ----  -------  -------  ----------  -----  ----------------  -----------  -------  --------------
regulator-quick  16    14    1     1        0        0           14     92.9%             100.0%       1        1
```

```bash
python -m benchrun stats --plan regulator-quick --db lot.sqlite --pareto
```

```text
1 failed run of 15 complete runs.

plan             step            measurement  runs  readings  share   cumulative
---------------  --------------  -----------  ----  --------  ------  ----------
regulator-quick  output_voltage  vout         1     3         100.0%  100.0%
```

The poor-line unit passes here: this short plan only checks the output at three inputs, and its
reading at 8 V (4.98 V) is the lowest but inside the limits. Two more runs with different limits:

```bash
python -m benchrun run examples/regulator_quick.json --serial SN-1016 --sim --seed 16 --set vnom=5.05 --db lot.sqlite
```

(and SN-1017 with seed 17) put each measurement into two limit sets, and the summary marks every
row `limit set 1 of 2` or `limit set 2 of 2` and ends with
`Limits changed between runs for 3 measurements: each set of limits has its own row, and capability
is never computed across them.` With two nearly equal values, the second set's indices run into the
thousands, which is why so few values deserve little trust.

The whole picture as one page:

```bash
python -m benchrun stats --plan regulator-quick --db lot.sqlite --format html --out lot.html
```

## `history --stats`

`history --plan NAME --stats` uses the same engine and the same exclusions, and keeps its own
columns. Its `cpk` column has always been computed from the overall standard deviation, which is
what `stats` calls Ppk; its other numbers (n, fails, mean, stdev, min, max) match `stats` exactly.
`history --serial` there means exactly that serial, not a prefix.
