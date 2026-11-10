# Reports and exports

benchrun writes two kinds of file from the results database:

- **Reports**, one file per run per format, from `report` or from `run --report-dir`.
- **Exports**, many runs in one file, from `export`.

Every file is written to a temporary name first and renamed into place, so a crash or a full disk
never leaves half a file. Everything a report shows was recorded during the run; nothing is looked
up again later.

## Report formats

| Name | File ending | What it is for |
|---|---|---|
| `html` | `.html` | Full report for a browser, with plots of sweeps |
| `csv` | `.csv` | One row per measurement, for a spreadsheet or a script |
| `junit` | `.xml` | JUnit XML for a build server or test system |
| `json` | `.json` | The whole run record, for a script or another system |
| `markdown` | `.md` | A summary to paste into a ticket or a pull request |
| `certificate` | `-certificate.html` | A printable one-page test certificate with lines for a reviewer |

Two group names choose several at once:

- `all` means `html`, `csv` and `junit`. It meant that before the other formats existed, and it still
  does, so scripts that relied on it get the same three files.
- `every` means every format in the table.

`report --help` lists the same table.

File names follow one pattern: `run-`, the run id in six digits, the serial number with anything
unsafe in a file name replaced, the start time in UTC, then the ending. For example, run 12 of
serial SN-1001 started at 09:30:00 UTC on 9 November 2026 gives
`run-000012-SN-1001-20261109T093000.md` and `run-000012-SN-1001-20261109T093000-certificate.html`.
Writing a report again for the same run replaces the file with the same content.

### Choosing formats

`--format` can be given more than once. Without it, `report` writes `all`.

```bash
python -m benchrun report --latest --format markdown --format certificate --out reports
python -m benchrun report --serial SN-1001 --all --format every --out reports
python -m benchrun report --run 12 --format json --out reports
```

`run` writes reports when it is given `--report-dir`. `--report-format` picks which, can be given
more than once, and needs `--report-dir`. Without it, `run` writes `all`.

```bash
python -m benchrun run examples/regulator_quick.json --serial SN-1001 --sim --report-dir reports --report-format certificate --report-format json
```

## What counts as a pass

The markdown report and the certificate state the verdict in words, and only one case is called a
pass: a run of the whole plan that ended with the result `pass`.

| Run | Verdict shown |
|---|---|
| Whole plan, result pass | PASS |
| Whole plan, result fail | FAIL |
| Aborted (an instrument fault, a lost connection, a stop request) | ABORTED, with the reason |
| Still running, or the process stopped before recording a result | NOT FINISHED |
| Only some steps run (`run --step`), and they passed | PARTIAL |
| Only some steps run, and one failed or the run aborted | FAIL or ABORTED, saying it was partial |

Each verdict other than PASS comes with a sentence that starts "The unit did not pass" or "The unit
has not passed".

## html, csv and junit

These three are unchanged. The HTML report is one file with no scripts and nothing loaded from
elsewhere. The CSV report has one row per measurement, plus a row for each test step that measured
nothing and for a step or run that went wrong, so the file shows how far the run got. Free text
that a spreadsheet would treat as a formula (starting with `=`, `+`, `-`, `@`, a tab or a carriage
return) gets a leading apostrophe; numbers are left alone. The JUnit file makes every failed
measurement a failure and every measurement that could not be taken an error, and an aborted or
unfinished run always shows at least one error.

```text
run_id,serial,plan,plan_version,run_status,run_reason,step,step_status,measurement,kind,point,value,units,low,high,verdict,note,instrument,samples,taken_at,run_note
1,SN-1002,regulator-quick,1.0,fail,,output_voltage,fail,vout,read,"{""vin"": 8.0}",5.245959022,V,4.9,5.1,fail,,dmm,1,2026-11-10T05:23:05.785+00:00,
```

## markdown

A readable summary in this order: the result, the unit, the plan, the instruments, the steps, the
measurements that failed, then every measurement in a table. Every piece of text from outside (a
serial number, a note, an instrument's answer) is escaped, so it shows exactly as written and cannot
add formatting, link text, images, mentions, HTML or table columns where it is pasted. Line breaks in
a note become spaces and control characters are removed. Values are shown to six significant digits;
use `json` or `csv` for full precision.

```text
# Run 1: SN-1002: FAIL

**Result: FAIL.** The unit did not pass: at least one measurement was outside its limits.

3 measurements: 0 passed, 3 failed, 0 could not be taken, 0 recorded without limits.

## Failed measurements

| Step | Measurement | Point | Value | Limits | Verdict | Note |
|---|---|---|---|---|---|---|
| output\_voltage | vout | vin=8 | 5.24596 V | 4.9 to 5.1 | fail |  |
```

## certificate

A one-page HTML test certificate for one unit, to print or save as PDF from a browser. It shows:

- the verdict, in large type, with the sentence from the table above
- unit serial number, and product when the run recorded one
- plan name, version and SHA-256 hash, and the steps selected for a partial run
- start and finish time in UTC
- operator, and station when the run recorded one
- each instrument with its driver, its `*IDN?` response and its asset tag ("not recorded" when the
  run did not record one)
- each step with its status, how many measurements it took and how many failed
- lines for a reviewer's name, signature and date
- the benchrun version and the run's UUID

A pass has a green border. Anything else has a red border, its verdict word, and "NOT PASSED" printed
across the page and in the page title. These marks are ink, not background colour, so they survive a
black and white printer, which leaves out backgrounds by default. A plan of more than 20 steps lists
only the steps that did not pass, up to 20, with a count of the rest, so the page stays one page; the
HTML report lists every step. Like the HTML report, it has no scripts and loads nothing from
elsewhere, and all text is escaped.

```text
Test certificate                                         Run 1
FAIL
The unit did not pass: at least one measurement was outside its limits.
Unit serial    SN-1002
Test plan      regulator-quick
...
Reviewer name ____________   Signature ____________   Date ______
```

## json

The whole run record, for a script or another system. The plan text is included, so the file stands
on its own.

```json
{
  "format": "benchrun-run",
  "format_version": 1,
  "run": { "id": 1, "serial": "SN-1002", "status": "fail", "partial": false },
  "plan": { "name": "regulator-quick", "version": "1.0", "sha256": "7cd83e75" },
  "instruments": [{ "name": "dmm", "driver": "dmm", "idn": "benchrun,SIM-DMM,SIM0001,1.0" }],
  "counts": { "measurements": 3, "pass": 0, "fail": 3, "error": 0, "info": 0 },
  "steps": [{ "ordinal": 1, "name": "output_voltage", "status": "fail", "measurements": [] }]
}
```

That is shortened; the schema below lists every key a real file has.

### Schema, format_version 1

Keys always appear in the order listed. Numbers are JSON numbers, never strings. A value that is
missing is `null`. A number JSON cannot hold (infinity or not-a-number, which only a damaged or
hand-edited database could contain) is written as `null`. Times are ISO 8601 text in UTC, such as
`2026-11-10T05:23:05.737+00:00`. Text is UTF-8.

A reader should check that `format` is `benchrun-run` and refuse a `format_version` it does not know.
A later version may add keys; it will change `format_version` if it removes a key or changes what one
means.

| Key | Type | Meaning |
|---|---|---|
| `format` | text | Always `benchrun-run` |
| `format_version` | number | `1` |
| `run.id` | number | Run id in the database that wrote the file |
| `run.uuid` | text or null | Identifier of the run that is the same in any database |
| `run.serial` | text | Unit serial number |
| `run.product` | text | Product part number, or empty |
| `run.station` | text | Station name, or empty |
| `run.lot_id` | number or null | Lot the unit was tested in |
| `run.status` | text | `pass`, `fail`, `aborted` or `running` |
| `run.reason` | text or null | Why the run was aborted |
| `run.partial` | true or false | Whether only some steps were selected |
| `run.selected_steps` | list of text, or null | The steps selected for a partial run |
| `run.operator` | text | Operator label, or empty |
| `run.note` | text | The run's note, or empty |
| `run.started_at` | time | When the run started |
| `run.finished_at` | time or null | When it finished; null if it never did |
| `run.software_version` | text | benchrun version that ran it |
| `run.origin` | text | Where the run came from, or empty |
| `run.imported_at` | time or null | When the run was brought into this database, if it was |
| `plan.name` | text | Plan name |
| `plan.version` | text | Plan version |
| `plan.sha256` | text | SHA-256 hash of the plan text |
| `plan.variables` | object of numbers | Plan variables as used, after any `--set` |
| `plan.source` | text | The full plan text |
| `instruments[]` | list | One entry per instrument |
| `instruments[].name` | text | Name in the plan |
| `instruments[].driver` | text | Driver name |
| `instruments[].address` | text | `host:port` it was reached at |
| `instruments[].idn` | text or null | Its `*IDN?` response; null if it never answered |
| `instruments[].asset_tag` | text or null | Inventory asset tag, when one was recorded |
| `counts` | object of numbers | `measurements` (the total), then how many had each verdict: `pass`, `fail`, `error`, `info` |
| `steps[]` | list | Every step in order, including setup and teardown |
| `steps[].ordinal` | number | Position in the run |
| `steps[].name` | text | Step name |
| `steps[].kind` | text | `setup`, `test` or `teardown` |
| `steps[].status` | text | `pass`, `fail`, `error`, `skipped`, `running` or `pending` |
| `steps[].message` | text or null | What happened, such as why it was skipped |
| `steps[].started_at` | time or null | When the step started |
| `steps[].finished_at` | time or null | When it finished |
| `steps[].measurements[]` | list | Every measurement the step took, in order |
| `...measurements[].name` | text | Measurement name |
| `...measurements[].kind` | text | `read` (from an instrument) or `calc` (worked out from readings) |
| `...measurements[].point` | object of numbers | Sweep variables at this reading; empty outside a sweep |
| `...measurements[].value` | number or null | The value; null if it could not be taken |
| `...measurements[].units` | text | Units |
| `...measurements[].low` | number or null | Low limit; null if none |
| `...measurements[].high` | number or null | High limit; null if none |
| `...measurements[].verdict` | text | `pass`, `fail`, `error` (could not be taken) or `info` (no limits) |
| `...measurements[].note` | text or null | Why it failed or could not be taken |
| `...measurements[].instrument` | text or null | Instrument it was read from |
| `...measurements[].samples` | number | Readings combined into the value |
| `...measurements[].taken_at` | time | When it was recorded |

## Exports

`export` writes every run that matches its filters into one file, oldest run first. `--format`
chooses `csv` (the default), `jsonl` or `xlsx`. It will not replace an existing file without
`--force`.

```bash
python -m benchrun export --out results.csv
python -m benchrun export --plan regulator-quick --since 2026-11-01 --format jsonl --out november.jsonl
python -m benchrun export --status fail --format xlsx --out failures.xlsx
```

Runs are read and written one at a time, so the size of an export is limited by the disk, not by
memory.

### csv

The same columns and rows as the per-run CSV report, with one header for all runs. The file is the
same, byte for byte, as before `--format` existed.

### jsonl

JSON Lines: one JSON object per line, with the same rows as the CSV export (one per measurement, plus
a row for a test step that measured nothing and for a run with no steps). Each row carries its run's
fields, so every line stands on its own. Numbers are numbers, `point` is an object, missing values are
`null`, and `partial` is `true` or `false`. Every character outside ASCII is written as a `\u`
escape, so no reader can mistake a character inside a value for the end of a line.

Keys, in order: `run_id`, `run_uuid`, `serial`, `product`, `station`, `plan`, `plan_version`,
`plan_sha256`, `run_status`, `partial`, `run_reason`, `operator`, `run_note`, `run_started_at`,
`run_finished_at`, `step`, `step_kind`, `step_status`, `step_message`, `measurement`, `kind`, `point`,
`value`, `units`, `low`, `high`, `verdict`, `note`, `instrument`, `samples`, `taken_at`. The step and
measurement keys mean what they do in the JSON report; on a row with no step or no measurement they
are `null`.

```text
{"run_id":1,"run_uuid":"2f0021a684914cbf9a0a12caebf7ece6","serial":"SN-1002",...,"measurement":"vout","kind":"read","point":{"vin":8.0},"value":5.245959022,"units":"V","low":4.9,"high":5.1,"verdict":"fail",...}
```

### xlsx

An Excel workbook (Office Open XML), written by benchrun itself with no spreadsheet library. It has
three sheets:

- **Runs**, one row per run: `run_id`, `uuid`, `serial`, `product`, `station`, `plan`,
  `plan_version`, `plan_sha256`, `status`, `partial` (yes or no), `selected_steps`, `reason`,
  `operator`, `note`, `started_at`, `finished_at`, then counts of `measurements`, `passed`, `failed`
  and `errors`, and `software_version`.
- **Steps**, one row per step: `run_id`, `serial`, `ordinal`, `step`, `kind`, `status`, `message`,
  `started_at`, `finished_at`, `measurements`, `failed`.
- **Measurements**, one row per measurement: `run_id`, `serial`, `step`, `measurement`, `kind`,
  `point` (such as `vin=8`), `value`, `units`, `low`, `high`, `verdict`, `note`, `instrument`,
  `samples`, `taken_at`.

The header row is bold and stays in view while scrolling. Numbers are stored as numbers, at full
precision; times are ISO 8601 text, so they sort correctly and are never shifted by a time zone. Text
is stored as text and is never run as a formula; text that starts like a formula is also marked so
that editing the cell keeps it text. Characters that XML cannot hold are removed, and a cell longer
than 32,767 characters (the most a cell holds) is cut to that length. The same runs always give the
same file, byte for byte.

A worksheet holds at most 1,048,576 rows including its header. An export that would need more stops
before writing anything, with exit code 2 and this message:

```text
Cannot continue:
  the Measurements sheet would need more than 1,048,576 rows, the most a worksheet can hold
  narrow the export with --serial, --plan, --status, --since or --until, or use --format csv or jsonl
```

## Sample files

[examples/sample_output](../examples/sample_output) has one file of every report format and of the
jsonl and xlsx exports, all from one run on the simulators with a failing simulated unit. They were
made with these commands:

```bash
python -m benchrun run examples/regulator_quick.json --serial SN-1002 --operator "Operator A" --sim --dut high-output --seed 1 --db sample.sqlite --report-dir examples/sample_output --report-format every
python -m benchrun export --db sample.sqlite --format jsonl --out examples/sample_output/export.jsonl
python -m benchrun export --db sample.sqlite --format xlsx --out examples/sample_output/export.xlsx
```
