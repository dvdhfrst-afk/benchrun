# Lots

A lot is a batch of units tested and counted together: a work order, a tray, a shipment. It is
tested with one plan, or as one product, and it has its own yield. Every unit tested in a lot is
an ordinary run linked to the lot, so `show`, `history`, `report`, `export` and `stats` see it as
they see any other run.

Lots live in the results database, next to the runs. Opening, closing and reopening a lot each
write an entry in the audit log (`benchrun audit --subject lot:NAME`).

## Commands

| command | what it does |
| --- | --- |
| `lot open NAME --plan FILE` | open a lot tested with this plan; the lot records the plan's name |
| `lot open NAME --product PART_NUMBER` | open a lot of this product, tested with the product's plan |
| `lot list` | every lot, newest first, with units tested, units to test, and yield |
| `lot show NAME` | one lot: units tested and to test, first-pass and final yield, failures by measurement |
| `lot run NAME` | test units one after another on the same bench, each as one run in the lot |
| `lot report NAME --out FOLDER` | write the lot summary as HTML, CSV and JSON |
| `lot close NAME` | close the lot, so no more runs can be added to it |
| `lot reopen NAME` | open a closed lot again |
| `run PLAN --serial SN --lot NAME` | add one run to an open lot |

`lot open` also takes `--quantity N`, the number of units in the lot, so that `lot show` can say
how many are still to test, and `--note TEXT` for one line such as the work order. A lot name is 1
to 64 printable characters. Wherever a command takes a lot, it also accepts the lot's numeric id.
Commands that change a lot take `--actor` to name who made the change in the audit log; the
default is the login name.

Every lot command takes `--db FILE` (default `benchrun.sqlite`).

## What a lot counts

The counts come from the statistics engine and follow its rules (see
[statistics.md](statistics.md)):

- A unit is a serial. A unit is **tested** once it has a complete run in the lot: a pass or a fail
  of the whole plan. An aborted run, a run still going and a run of only some steps (`--step`) are
  counted as runs, but they do not make a unit tested.
- **Units to test** is the quantity less the units tested. A unit whose only run aborted is still
  to test.
- **First-pass yield** is the share of tested units whose first complete run passed. **Final
  yield** is the share whose last complete run passed.
- **Failures by measurement** lists each failing measurement of the complete runs, most runs first,
  with its share of all failures and the running total (a Pareto).

## Testing a lot: `lot run`

`lot run NAME` sets up the bench once, then tests units one after another. Each unit is one run
in the lot, with the lot's plan, or its product's plan, variables, limits and serial pattern.

Serials come from one of two places:

- `--serials FILE`: one serial per line. Blank lines and lines starting with `#` are skipped. A
  serial that appears twice is refused. Every serial is checked before the first unit is tested,
  so a bad file tests nothing. The file is UTF-8, at most 1 MiB and 10000 serials.
- Otherwise the serial is asked for at the terminal, one unit at a time. An empty line or Ctrl+D
  (Ctrl+Z then Enter on Windows) ends the session. A serial that is not usable is refused, and the
  next one is asked for.

A serial that already passed in the lot is refused unless `--retest` is given. A serial that
failed may be tested again without it: that is a retest, and it counts in final yield.

A lot opened with `--plan` records the plan's name, not its file, so `lot run` takes the file
again with `--plan FILE`, and refuses a plan with another name. A product lot uses the product's
plan, or the one given with `--plan` if it has the same name.

Everything else passes through as for `run`: the station options (`--station`, `--address`,
`--station-name`), `--sim` with `--dut`, `--seed`, `--noise` and `--fault` (one set of simulators
serves the whole session), `--report-dir` and `--report-format` (each unit gets its own reports),
`--operator`, `--note` (kept with every run of the session), `--verbose`, `--scpi-log` and
`--auto-confirm`.

Safety is the same as for `run`. Ctrl+C, a service stop or a closed console window stops the unit
on the bench the safe way: outputs go off, the result is saved as aborted, and the lot session
ends. A stop that arrives while a result is being saved is held until it is saved, and still ends
the session. The session also ends, after the unit on the bench, when an output could not be
confirmed off, when the results database failed, when the reports could not be written, or when an
operator prompt had nobody to answer it. An instrument fault aborts only that unit; the session
goes on to the next.

When the session ends, for whatever reason, it prints one line per unit tested and where the lot
now stands.

Exit codes: 0 if every unit tested in the session passed (or none was tested), 1 if any failed and
none aborted, 3 if any aborted, 2 for bad options, an unusable serials file, or a closed or unknown
lot.

## Adding one run: `run --lot`

`run --lot NAME` adds a single run to an open lot. The run must be of the lot's plan and, for a
product lot, of its product; when the run names no product, the lot's product is used. A serial
that already passed in the lot needs `--retest`.

## Closing a lot

`lot close NAME` refuses while a run in the lot is still running, and names those runs. If their
process ended without finishing them, mark them with `benchrun db mark-abandoned` first (see
[database.md](database.md)). `--force` closes the lot anyway, and the audit entry records that it
was forced and which runs were still running. A closed lot takes no more runs until `lot reopen`.

## The lot report

`lot report NAME --out FOLDER` writes three files named after the lot's id and name, such as
`lot-000001-L2026-11-01.html`. `--format html|csv|json` (repeatable) writes only those. An
existing file is never replaced without `--force`; if one is in the way, nothing is written.

- **HTML**: one self-contained page with no scripts and nothing loaded from elsewhere: the lot's
  details, yield, the Pareto of failures, every unit with its results in order, and a histogram of
  each limited measurement (one per sweep point and set of limits) with its limits and mean.
- **CSV**: one row per serial: `lot`, `serial`, `runs`, `results` (in order, `*` marking a run of
  only some steps), `first_result`, `final_result`, `last_run`, `last_status`, `last_started_at`
  and `failed_measurements` (in the final complete run). Text a spreadsheet would run as a formula
  starts with an apostrophe.
- **JSON**: format `benchrun-lot`, version 1: `lot`, `runs`, `units`, `running_runs`, `pareto`,
  `unit_results`, and `measurements` (each with its statistics, capability and histogram).
  `lot show NAME --json` prints the same, without `format`, `format_version` and `measurements`.

## Elsewhere

- `show` names the lot a run is in.
- `history --lot NAME` lists only the runs in a lot.
- `stats --lot NAME` takes the lot's name or its id.

## Worked example on the simulators

Open a lot of six units of the quick regulator plan:

```bash
python -m benchrun lot open L2026-11-01 --plan examples/regulator_quick.json --quantity 6 --note "Work order WO-0001" --actor "Operator B" --db lots.sqlite
```

```text
Opened lot L2026-11-01 of 6 units, tested as plan regulator-quick.
Test its units with: benchrun lot run L2026-11-01 --plan examples/regulator_quick.json
```

The first tray of four is in `tray1.txt`:

```text
# Work order WO-0001, tray 1
SN-1001
SN-1002
SN-1003
SN-1004
```

```bash
python -m benchrun lot run L2026-11-01 --plan examples/regulator_quick.json --serials tray1.txt --sim --operator "Operator A" --db lots.sqlite
```

Each unit prints what `run` prints, then the session ends with:

```text
unit  serial   run  result  reason or failed measurements
----  -------  ---  ------  -----------------------------
1     SN-1001  1    PASS
2     SN-1002  2    PASS
3     SN-1003  3    PASS
4     SN-1004  4    PASS
Session: 4 units tested: 4 pass, 0 fail, 0 aborted.
Lot L2026-11-01: 4 of 6 units tested, 2 to go. First-pass yield 100.0% (4 of 4 units), final yield 100.0% (4 of 4).
```

The next unit is typed at the terminal. Its output is high (`--dut high-output`), so it fails.
Typing SN-1001 again is refused, and an empty line ends the session, which exits 1:

```bash
python -m benchrun lot run L2026-11-01 --plan examples/regulator_quick.json --sim --dut high-output --db lots.sqlite
```

```text
Serial number of the next unit (an empty line or Ctrl+D ends the session):
SN-1005

Unit 1 of this session: SN-1005. Lot L2026-11-01: 4 of 6 units tested, 2 to go.
Run 5: serial SN-1005, plan regulator-quick 1.0
  ...
    FAIL output_voltage.vout at vin=8: 5.24596 V, limit 4.9 to 5.1 V
    FAIL output_voltage.vout at vin=12: 5.25002 V, limit 4.9 to 5.1 V
    FAIL output_voltage.vout at vin=16: 5.25394 V, limit 4.9 to 5.1 V
[FAIL] output_voltage (3 measured, 3 failed)
RESULT: FAIL

Serial number of the next unit (an empty line or Ctrl+D ends the session):
SN-1001
serial SN-1001 already passed in lot L2026-11-01; add --retest to test it again. Type another serial.

Serial number of the next unit (an empty line or Ctrl+D ends the session):


unit  serial   run  result  reason or failed measurements
----  -------  ---  ------  -----------------------------
1     SN-1005  5    FAIL    output_voltage.vout
Session: 1 unit tested: 0 pass, 1 fail, 0 aborted.
Lot L2026-11-01: 5 of 6 units tested, 1 to go. First-pass yield 80.0% (4 of 5 units), final yield 80.0% (4 of 5).
```

SN-1005 is reworked and passes on a second run, added with `run --lot`. The last unit's run aborts
when the multimeter drops its connection:

```bash
python -m benchrun run examples/regulator_quick.json --serial SN-1005 --lot L2026-11-01 --note "reworked R12" --sim --seed 5 --db lots.sqlite
python -m benchrun run examples/regulator_quick.json --serial SN-1006 --lot L2026-11-01 --sim --fault dmm:drop:1 --db lots.sqlite
```

Where the lot stands:

```bash
python -m benchrun lot show L2026-11-01 --units --db lots.sqlite
```

```text
Lot L2026-11-01: OPEN
Plan: regulator-quick
Note: Work order WO-0001
Opened 2026-11-10T06:07:56.364+00:00 (UTC)
Units: 5 of 6 units tested, 1 to go; 1 more with no complete run yet
First-pass yield 80.0% (4 of 5 units), final yield 100.0% (5 of 5).
Retests: 1 run on 1 unit
Runs: 7: 5 pass, 1 fail, 1 aborted, 0 running

Failures by measurement:
step            measurement  runs  readings  share   cumulative
--------------  -----------  ----  --------  ------  ----------
output_voltage  vout         1     3         100.0%  100.0%

serial   runs  results in order  final  last run  failed in the final run
-------  ----  ----------------  -----  --------  -----------------------
SN-1001  1     pass              pass   1
SN-1002  1     pass              pass   2
SN-1003  1     pass              pass   3
SN-1004  1     pass              pass   4
SN-1005  2     fail, pass        pass   6
SN-1006  1     aborted                  7
```

SN-1006 has no complete run, so it is still to test. The lot report, the runs, and the statistics
of this lot alone:

```bash
python -m benchrun lot report L2026-11-01 --out lot-report --db lots.sqlite
python -m benchrun history --lot L2026-11-01 --db lots.sqlite
python -m benchrun stats --plan regulator-quick --lot L2026-11-01 --db lots.sqlite
```

When the last unit is done, close the lot:

```bash
python -m benchrun lot close L2026-11-01 --actor "Operator A" --db lots.sqlite
python -m benchrun audit --subject lot:L2026-11-01 --db lots.sqlite
```

```text
entry  at (UTC)             action      subject          actor
-----  -------------------  ----------  ---------------  ----------
2      2026-11-10 06:08:01  lot.close   lot:L2026-11-01  Operator A
    forced=false
1      2026-11-10 06:07:56  lot.create  lot:L2026-11-01  Operator B
    note="Work order WO-0001", plan_name="regulator-quick", product="", quantity=6
```
