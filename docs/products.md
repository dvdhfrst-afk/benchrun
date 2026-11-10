# Products

A product is a part number that benchrun knows how to test. It names the plan file it is tested
with, and it can carry what differs from one part to the next:

- its own values for some of the plan's variables, such as the nominal output voltage
- its own limits for single measurements, which replace the plan's limits for that measurement
- a serial number pattern, a regular expression every serial of this part must match

Products live in the results database, next to the runs. A plan file is never changed: when a run
starts, benchrun loads the plan, checks every product value against it, and only then puts the
values into the loaded plan. The run records the plan file exactly as it was read, the product's
part number, the variables it ran with, and on every measurement the limits it was judged against.

## Worked example: two regulators, one plan

`examples/regulator_line_load.json` tests a 5 V linear regulator. Say the factory builds two
versions on the same board: REG-500 with a 5.0 V output, and REG-525 with a 5.25 V output. Both
use the same plan; they differ in output voltage, in the input voltage needed to keep regulating
at full load, and in some limits.

Add both part numbers, each with the pattern its serials follow:

```bash
python -m benchrun product add REG-500 --plan examples/regulator_line_load.json --description "5.0 V regulator" --serial-pattern "R500-\d{4}"
python -m benchrun product add REG-525 --plan examples/regulator_line_load.json --description "5.25 V regulator" --serial-pattern "R525-\d{4}"
```

The plan checks the output against `vnom` and runs its low-headroom step at `vin_headroom`. For
REG-525 both must be higher, so give that part its own values:

```bash
python -m benchrun product set-var REG-525 vnom 5.25
python -m benchrun product set-var REG-525 vin_headroom 6.0
```

REG-500 allows a little more line regulation than the plan does. A limit is set for one
measurement of one step; leave out `--low` or `--high` to have no limit on that side:

```bash
python -m benchrun product set-limit REG-500 line_regulation line_reg --high 25
```

REG-525's limits come from a spreadsheet, saved as `reg-525-limits.csv`:

```text
step,measurement,low,high,units
line_regulation,line_reg,,20,mV
quiescent_current,iq,,4,mA
low_headroom,vout,5.2,5.3,V
```

Try it first with `--dry-run`, which shows what would change and changes nothing:

```bash
python -m benchrun product import-limits REG-525 reg-525-limits.csv --dry-run
```

```text
Limits for product REG-525 from reg-525-limits.csv:
action  step               measurement  limit         was
------  -----------------  -----------  ------------  ---
added   line_regulation    line_reg     <= 20 mV
added   quiescent_current  iq           <= 4 mA
added   low_headroom       vout         5.2 to 5.3 V
3 added, 0 changed, 0 unchanged, 0 removed
Dry run: nothing was changed.
```

Then again without it, which prints the same report followed by `Saved.` The part numbers now:

```bash
python -m benchrun product list
```

```text
part number  description       plan                               serial pattern  variables  limits  runs
-----------  ----------------  ---------------------------------  --------------  ---------  ------  ----
REG-500      5.0 V regulator   examples/regulator_line_load.json  R500-\d{4}      0          1       0
REG-525      5.25 V regulator  examples/regulator_line_load.json  R525-\d{4}      2          3       0
```

Before testing anything, `validate --product` shows every limit that will apply, and where it
comes from. A limit that depends on a sweep variable is shown as its expression:

```bash
python -m benchrun validate --product REG-525
```

```text
examples/regulator_line_load.json: OK
  regulator-line-load 1.0
  5 instruments, 5 steps, 101 measurements per run
  Product REG-525: 2 variables and 3 limits of its own
    vin_headroom = 6
    vnom = 5.25

step               measurement     units  low           high          from
-----------------  --------------  -----  ------------  ------------  -------
quiescent_current  iq              mA                   4             product
line_regulation    vout            V      5.15          5.35          plan
line_regulation    supply_limited                       0             plan
line_regulation    line_reg        mV                   20            product
load_regulation    vout            V      5.15          5.35          plan
load_regulation    pdiss           W                    8             plan
load_regulation    load_reg        mV                   30            plan
low_headroom       vout            V      5.2           5.3           product
ripple_rejection   measured_freq   Hz     =freq * 0.98  =freq * 1.02  plan
ripple_rejection   psrr            dB     40                          plan
ripple_rejection   worst_psrr      dB     40                          plan
```

Now test. With `--product`, the plan comes from the product, so no plan path is needed. The
simulator's `high-output` unit has a 5.25 V output, so it stands in for a REG-525:

```bash
python -m benchrun run --product REG-500 --serial R500-0001 --sim
python -m benchrun run --product REG-525 --serial R525-0001 --sim --dut high-output
```

Both end with `RESULT: PASS`. The same 5.25 V unit tested as a REG-500 fails, because REG-500
keeps the plan's 5.0 V:

```bash
python -m benchrun run --product REG-500 --serial R500-0002 --sim --dut high-output
```

```text
    FAIL line_regulation.vout at vin=7: 5.24346 V, limit 4.9 to 5.1 V
...
RESULT: FAIL
```

A serial that does not fit the part's pattern is refused, with exit code 2, before any instrument
is touched:

```bash
python -m benchrun run --product REG-525 --serial R500-0003 --sim
```

```text
Cannot continue:
  serial 'R500-0003' does not match the pattern for product REG-525: R525-\d{4}
```

The product is on every run tested with it:

```bash
python -m benchrun history --product REG-525
python -m benchrun show --serial R525-0001
```

```text
run  started (UTC)        serial     product  plan                     status  pass  fail  error  reason or note
---  -------------------  ---------  -------  -----------------------  ------  ----  ----  -----  --------------
2    2026-11-10 05:23:39  R525-0001  REG-525  regulator-line-load 1.0  PASS    61    0     0
```

`show` prints a `Product: REG-525` line, `show --json` has a `product` field, and the HTML report
has a Product row. A product's own limits can be written out again, ready to edit and import:

```bash
python -m benchrun product export-limits REG-525 --out reg-525-limits-now.csv
```

It prints `Wrote 3 limits for product REG-525 to reg-525-limits-now.csv`, and the file holds:

```text
step,measurement,low,high,units
quiescent_current,iq,,4.0,mA
line_regulation,line_reg,,20.0,mV
low_headroom,vout,5.2,5.3,V
```

## How product values are applied

- **Plan.** `run --product PN` and `validate --product PN` use the product's plan file. Give a plan
  path as well to use that plan with the product's values instead; the values are then checked
  against that plan. A relative plan path is read from the folder benchrun runs in.
- **Variables** replace the plan's `vars` of the same name. `--set` still wins over both, so a
  one-off change needs no edit to the product. A product can only set variables the plan has.
- **Limits** replace both sides of the plan's limit for that step and measurement, including
  limits written as expressions. A side left empty has no limit. A step summary value, such as
  `line_reg` above, is a measurement too. Every other measurement keeps the plan's limits.
- **Serial pattern.** The whole serial must match. With no pattern, any serial is accepted.

Everything is checked before anything is commanded, and any problem stops the run with exit code
2: a variable or a limit for a step or measurement the plan does not have, a limit for a
measurement the plan gives no units (a bare number could be misread), a limit that is not a
number, or a low limit above the high one. The same checks run when a value is set, when limits
are imported, and when `product edit --plan` points a product at another plan.

## Commands

All of them take `--db FILE` (default `benchrun.sqlite`). Commands that change something take
`--actor NAME`, the name written to the audit log; it defaults to your login name.

| Command | Does |
|---|---|
| `product add PN --plan FILE [--description TEXT] [--serial-pattern REGEX]` | Adds a part number. The plan is loaded and checked now. |
| `product list [--json]` | Every part number, with counts of its variables, limits and runs. |
| `product show PN [--json]` | One part number with its variables and limits. |
| `product edit PN [--description TEXT] [--plan FILE] [--serial-pattern REGEX]` | Changes what is given. `--serial-pattern ""` accepts any serial. |
| `product delete PN [--force]` | Deletes a part number with its values. Refused while runs name it, unless `--force`; runs are never deleted and keep the part number. |
| `product set-var PN NAME VALUE` | Gives a plan variable this part's own value. |
| `product unset-var PN NAME` | Goes back to the plan's value. |
| `product set-limit PN STEP MEASUREMENT [--low X] [--high Y]` | Replaces the plan's limits for one measurement. |
| `product unset-limit PN STEP MEASUREMENT` | Goes back to the plan's limits. |
| `product import-limits PN FILE [--dry-run] [--replace]` | Sets limits from a CSV file. `--replace` also removes the part's limits that are not in the file. |
| `product export-limits PN [--out FILE] [--force]` | Writes the part's own limits as CSV, or prints them. Never replaces a file without `--force`. |

A part number is 1 to 64 printable characters with no space at either end. Every change writes one
entry to the audit log, under the subject `product:PN`, with the old and new values.

## Limit files

The first row names the columns `step`, `measurement`, `low`, `high` and `units`, in any order. Each
other row is one measurement. `units` is there so the person editing the file can see them; it
must match the units the plan gives that measurement, so a limit typed in mV for a value in V is
caught. Empty rows are skipped.

The file is refused as a whole if any row is wrong: an unknown step or measurement, a measurement
listed twice, a limit that is not a plain number, low above high, units that do not match, a row
with too few or too many cells, or a file that is not UTF-8 text, holds control characters, or is
larger than 1 MiB. Every bad row is listed with its row number, and nothing is changed.

A spreadsheet's leading apostrophe, which marks a cell as text, is accepted. On export, text that a
spreadsheet would run as a formula gets one, the same as in benchrun's CSV reports.

## Serial patterns

A pattern is a regular expression the whole serial must match, such as `SN-\d{4}` or
`PN100-[A-Z]{2}\d{6}`. It is checked when it is set. Python's matcher can take years on a short
string with some patterns, so benchrun accepts only patterns that cannot:

- at most 200 characters
- no repeat of a group that holds a repeat or a `|`, such as `(a+)+` or `(a|b)*`
- at most 3 repeats of varying length (`*`, `+`, `?`, `{2,6}`); fixed counts such as `{4}` are free
- `|` choices that multiply to at most 16
- groups `( )` and `(?: )` only, and only `\d`, `\w` and `\s` among letter escapes

Serials are at most 128 characters, and a pattern is never tried on a longer one.
