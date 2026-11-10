# benchrun

An automated bench test runner. It drives instruments with SCPI (Standard Commands for
Programmable Instruments) over a raw TCP (Transmission Control Protocol) socket, runs test plans
written in JSON (JavaScript Object Notation) against one unit serial number at a time, records
every measurement in a SQLite database, and writes reports as HTML (HyperText Markup Language),
CSV (comma-separated values) and JUnit XML (Extensible Markup Language).

For a test line, the same database also holds the instruments you own and named stations built
from them, products with their own limits, and lots with their yield, with every change kept in an
audit log. `stats` gives capability, yield and control charts, `archive` moves runs between
databases with checks, and `serve` shows it all in a web browser.

It needs Python 3.9 or newer and nothing else: no VISA (Virtual Instrument Software Architecture)
library and no third-party package. You can run it straight from a clone, or install it to get a
`benchrun` command.

**What has been tested:** benchrun has only ever been run against its own bundled simulators and
a small fake instrument used by its tests. It has not been run against physical instruments, and
no compatibility with any vendor's instrument is claimed. Some drivers were written from published
manuals; [docs/drivers.md](docs/drivers.md) cites them and says what was not confirmed. See
[Pointing it at real instruments](#pointing-it-at-real-instruments) and
[What has and has not been tested](#what-has-and-has-not-been-tested).

## Why use it instead of a script

- **A failed bench never looks like a good unit.** Every command is followed by an error queue
  check. A lost connection, a timeout, a rejected setting or a garbled response ends the step with
  a readable message and records the run as aborted. Tests cover that a supply refusing a setting
  cannot turn into a pass measured at the old setting.
- **Every result says where it came from.** Each run stores the serial number, each instrument's `*IDN?`
  response and address, every value with its limits and verdict, the full plan text and its hash.
- **Plans are checked before the bench is powered.** `validate` reports every typo, unknown method,
  unit mismatch and bad limit at once, with no instruments connected.
- **The bench is left safe, and it says when it could not be.** Teardown runs after every outcome,
  including aborts and Ctrl+C. Outputs on supplies, loads, generators and source measure units are
  then switched off and read back, and switches are opened, even if the plan's teardown failed. If
  an output cannot be confirmed off, the run ends with a warning naming it.
  A power cut or a killed process cannot switch anything off; see
  [What benchrun cannot do](#what-benchrun-cannot-do).
- **An interrupted run keeps what it measured.** Every measurement is committed as it is taken.
- **Production records live with the results.** A run on a named station records which instrument,
  by asset tag, filled each role. A product supplies its plan, variables, limits and serial pattern,
  checked before anything is commanded. A lot counts units tested and to test, and first-pass and
  final yield.
- **Numbers you can check.** `stats` documents every formula it uses, and leaves aborted, unfinished
  and partial runs out of yield and capability, saying how many it left out.

## Install

From a clone, either run it in place with `python -m benchrun`, as every example below does, or
install it. Inside a virtual environment:

```bash
python -m pip install .
```

```bash
benchrun --version
```

Then `benchrun` and `python -m benchrun` are the same command. Uninstalling removes the program and
never touches results databases or reports.

## Quick start

Everything runs from the repository root. In one terminal, start the simulated bench: a digital
multimeter (DMM), a power supply, an electronic load, an oscilloscope, a signal generator, a source
measure unit, a frequency counter, a switch, an inductance, capacitance and resistance (LCR) meter
and a three-output supply. The first five are wired to a simulated 5 V linear regulator as the
device under test (DUT).

```bash
python -m benchrun sim
```

It listens on 127.0.0.1 only, on ports 5025 to 5034. Leave it running. In a second terminal, check
the plan, see that the instruments answer, and test a unit:

```bash
python -m benchrun validate examples/regulator_line_load.json
```

```bash
python -m benchrun probe examples/regulator_line_load.json
```

```bash
python -m benchrun run examples/regulator_line_load.json --serial SN-1001 --operator "Operator A" --report-dir reports
```

The run prints each instrument's identity, one line per step, a result, and the report files it
wrote. Their names end in the run's start time, so yours will differ:

```text
Run 1: serial SN-1001, plan regulator-line-load 1.0
  supply     127.0.0.1:5026           benchrun,SIM-PSU,SIM0002,1.0
  load       127.0.0.1:5027           benchrun,SIM-ELOAD,SIM0003,1.0
  dmm        127.0.0.1:5025           benchrun,SIM-DMM,SIM0001,1.0
  scope      127.0.0.1:5028           benchrun,SIM-SCOPE,SIM0005,1.0
  gen        127.0.0.1:5029           benchrun,SIM-SIGGEN,SIM0004,1.0
[PASS] quiescent_current (1 measured, 0 failed)
[PASS] line_regulation (30 measured, 0 failed)
[PASS] load_regulation (56 measured, 0 failed)
[PASS] low_headroom (1 measured, 0 failed)
[PASS] ripple_rejection (13 measured, 0 failed)
RESULT: PASS
Report: reports/run-000001-SN-1001-20261107T172757.html
Report: reports/run-000001-SN-1001-20261107T172757.csv
Report: reports/run-000001-SN-1001-20261107T172757.xml
```

Results go into `benchrun.sqlite` in the current folder; use `--db` to choose another file. Look
at them by serial number or by plan:

```bash
python -m benchrun history --serial SN-1001
```

```bash
python -m benchrun history --plan regulator-line-load
```

`show` prints one run, with its failures listed:

```bash
python -m benchrun show --serial SN-1001
```

To see a failing unit, stop the simulators with Ctrl+C and restart them with a bad regulator, then
run the same plan again:

```bash
python -m benchrun sim --dut poor-line
```

```bash
python -m benchrun run examples/regulator_line_load.json --serial SN-1002 --report-dir reports
```

To see an aborted run, restart them with a fault that drops the DMM connection after five readings:

```bash
python -m benchrun sim --fault dmm:drop:5
```

```bash
python -m benchrun run examples/regulator_line_load.json --serial SN-1003 --report-dir reports
```

That run ends with `RESULT: ABORTED` and exit code 3, and keeps the measurements taken before the
fault. A simulator with a `drop` or `hang` fault stays that way, so stop it with Ctrl+C when you are
done.

### Without a second terminal

`run --sim` starts private simulators on free ports for just that run. It is the quickest way to
try things, and it cannot collide with anything else on the machine:

```bash
python -m benchrun run examples/regulator_line_load.json --serial SN-2001 --sim
```

```bash
python -m benchrun run examples/regulator_line_load.json --serial SN-2002 --sim --dut high-dropout
```

```bash
python -m benchrun run examples/regulator_line_load.json --serial SN-2003 --sim --fault psu:error:3
```

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Run passed, or the command succeeded |
| 1 | Run finished and the unit failed |
| 2 | Bad plan or bad arguments, or nothing to read: no such run or database for `report`, `show`, `export` or `history`. No run was recorded |
| 3 | Run aborted, an instrument could not be reached, or the database or a report file could not be used |

An error message and these codes are all a user error produces; a Python traceback is a bug. Code 3 wins over the verdict if a report cannot be written after the run: the printed `RESULT` line
and the database still hold the verdict. `history` with no matches prints that and exits 0.

## How a run is judged

| Result | When |
|---|---|
| `pass` | Every limited measurement passed, and at least one was evaluated |
| `fail` | All instruments behaved and at least one measurement failed |
| `aborted` | Connection lost or refused, timeout, instrument error, garbled response, `*IDN?` mismatch, operator interrupt, or teardown failure |
| `running` | The process died before finishing; everything measured so far is still in the database |

Aborted outranks failed, because an aborted run was not completed. A few cases worth knowing:

- A run limited with `--step` is marked as partial. Its verdict is about those steps only, and it is
  left out of yield and statistics.

- Limits are inclusive, and either side may be left out.
- A measurement with no limits is recorded with the verdict `info`, and its value cannot fail a unit.
  Having no value can: an overload, or a calculation with no answer, still counts as a `fail`.
- A reading past the instrument's range (the SCPI overload value 9.9E37) is a `fail` for the unit,
  not an abort: the instrument is working, and the signal is out of range.
- A calculation with no answer, such as a ratio dividing by a dead output, is also a `fail`, and so
  is a limit worked out from a value that has no answer.
- Steps that were never reached are recorded as `skipped`, so a report always shows the whole plan.

## Test plans

A plan names its instruments and lists steps. Each step can sweep variables, apply settings at
each point, take readings with limits, and calculate summaries over the sweep. This is the core of
the line regulation step in [examples/regulator_line_load.json](examples/regulator_line_load.json):

```json
{
  "name": "line_regulation",
  "setup": [{ "do": "load.set_current", "amps": 0.1 }],
  "sweep": { "var": "vin", "start": 7.0, "stop": 20.0, "step": 1.0 },
  "apply": [{ "do": "supply.set_voltage", "volts": "=vin" }],
  "settle_s": 0.005,
  "measure": [
    {
      "name": "vout",
      "read": "dmm.measure_dc_voltage",
      "units": "V",
      "low": "=vnom - tol_v",
      "high": "=vnom + tol_v"
    }
  ],
  "summary": [
    { "name": "line_reg", "calc": "span(vout) * 1000", "units": "mV", "high": 30.0 }
  ]
}
```

Strings starting with `=` are expressions. They can use plan variables, sweep variables, and
earlier measurements in the same step. Units with SI prefixes convert for you: a reading of 0.0031 A
declared as `"units": "mA"` is recorded and limit-checked as 3.1.

[docs/plan_format.md](docs/plan_format.md) is the full reference. To list what each driver offers:

```bash
python -m benchrun drivers
```

`init` writes a starter plan to edit:

```bash
python -m benchrun init my_plan.json
```

To check a plan you are writing:

```bash
python -m benchrun validate my_plan.json
```

Override a variable for one run with `--set`, for example to test a 3.3 V part with the same plan.
The starter plan's limits follow `vnom`, and on the simulated 5 V part this one fails, as it should:

```bash
python -m benchrun run my_plan.json --serial SN-3001 --sim --set vnom=3.3
```

Other options on `run`:

| Option | What it does |
|---|---|
| `--note TEXT` | One line kept with the run, such as why it is being retested |
| `--step NAME` | Run only this step, plus the plan's `setup` and `teardown`; repeatable. Marks the run as partial |
| `--verbose` | Print every measurement as it is taken |
| `--auto-confirm` | Carry on past operator prompts without waiting, for scripts |
| `--scpi-log FILE` | Add all SCPI traffic to this file; earlier contents are kept |

A plan can stop for the operator with `{ "prompt": "Seat the unit and close the lid" }`. With no one
to answer, the run aborts and says to use `--auto-confirm`.
[examples/README.md](examples/README.md) lists the example plans.

## Reports and history

`--report-dir` on `run` writes all three formats when the run ends. `report` writes them later:

```bash
python -m benchrun report --serial SN-1001 --out reports
```

```bash
python -m benchrun report --run 3 --format junit --out reports
```

- **HTML** is one self-contained file: verdict, instruments with `*IDN?` responses, every
  measurement, and a plot for each limited measurement over a sweep.
- **CSV** has one row per measurement, with values at full precision. A step with no measurements,
  such as a skipped one, gets one row with the measurement columns empty.
- **JUnit XML** has one test case per measurement. Failures and aborts show as `failure` and
  `error`, and the serial, plan hash and instruments are in the suite's properties.

Report files are named `run-000012-SERIAL-20261107T101500.html`, using the run's start time in
UTC (Coordinated Universal Time) and the serial with anything unsafe in a file name replaced. They
are written to a temporary file first and then moved into place, so a report is either complete or
absent. [examples/sample_output](examples/sample_output) has one of each from a real run on the
simulators.

`export` writes many runs into one CSV file, and will not replace a file without `--force`. Text
cells that a spreadsheet would treat as a formula, such as a serial starting with `=`, get a leading
apostrophe:

```bash
python -m benchrun export --out line-load.csv --plan regulator-line-load --since 2026-01-01
```

`history` can also filter by `--status`, `--since`, `--until` and `--instrument`. `--instrument`
matches any part of an instrument's `*IDN?` response, so it finds every run made with one instrument
serial number.

`history --plan` adds first-pass yield (each serial counted once, on its first run that reached a
verdict; aborted runs say nothing about a unit). `--stats` adds per-measurement statistics, including
the process performance index (Ppk, from the overall spread; `stats` also gives Cpk). Each sweep point gets its own row, and so does each set of units
and limits, so a run with `--set vnom=3.3` never mixes with the 5 V runs. `--json` is for scripts:

```bash
python -m benchrun history --plan regulator-line-load --stats
```

```bash
python -m benchrun history --serial SN-1001 --json
```

`report --format every` adds a JSON record, a Markdown summary and a printable test certificate, and
`export --format` takes `csv`, `jsonl` or `xlsx`. [docs/reports.md](docs/reports.md) has the details.

## A small production setup

This walk-through was run on the simulators, with `python -m benchrun sim` in another terminal.
Every command changing a shared record takes `--actor`, kept in the audit log; left out, it is your
login name. Put the two simulated instruments in the inventory and make a station of them
([docs/stations.md](docs/stations.md)):

```bash
python -m benchrun instrument add PSU-0001 --driver psu --address 127.0.0.1:5026 --serial-number SIM0002 --location "Lab 1"
```

```bash
python -m benchrun instrument add DMM-0001 --driver dmm --address 127.0.0.1:5025 --serial-number SIM0001 --location "Lab 1"
```

```bash
python -m benchrun station create LINE-1 --location "Lab 1"
```

Then `station assign LINE-1 supply PSU-0001` and `station assign LINE-1 dmm DMM-0001`, and
`station check LINE-1` confirms each instrument answers as the inventory says. A part number names
its plan, and can carry its own variables, limits and serial pattern ([docs/products.md](docs/products.md)):

```bash
python -m benchrun product add REG-500 --plan examples/regulator_quick.json --serial-pattern "R500-\d{4}"
```

```bash
python -m benchrun product set-limit REG-500 output_voltage vout --low 4.95 --high 5.05
```

Open a lot of five units and test the four serials in `tray1.txt` on the station, one run each
([docs/lots.md](docs/lots.md)):

```bash
python -m benchrun lot open WO-0001 --product REG-500 --quantity 5
```

```bash
python -m benchrun lot run WO-0001 --serials tray1.txt --station-name LINE-1 --operator "Operator A"
```

All four passed. A fifth unit added with `run --product REG-500 --serial R500-0005 --lot WO-0001`
failed with a high output, and passed after rework. The lot then stood at:

```text
Units: 5 of 5 units tested, 0 to go
First-pass yield 80.0% (4 of 5 units), final yield 100.0% (5 of 5).
Retests: 1 run on 1 unit
```

`lot report WO-0001 --out reports` writes the lot as HTML, CSV and JSON. `stats` gives capability
(Cp, Cpk, Pp, Ppk), yield, a failure Pareto, histograms and control charts for any choice of plan,
product, station, lot or dates, with every formula in [docs/statistics.md](docs/statistics.md):

```bash
python -m benchrun stats --product REG-500 --lot WO-0001 --yield
```

To bring each bench's runs into one database, `archive export --lot WO-0001 --out wo1.zip` and then
`archive import wo1.zip --db central.sqlite` ([docs/archives.md](docs/archives.md)); `db info` and
`db check` look after a database ([docs/database.md](docs/database.md)). To browse everything,
including the audit log, at http://127.0.0.1:8765/ ([docs/web.md](docs/web.md)):

```bash
python -m benchrun serve
```

A driver for an instrument benchrun does not know can come from an installed package
([docs/plugins.md](docs/plugins.md)).

## Pointing it at real instruments

If your instruments speak the dialect the drivers use, the plan does not change, only the addresses.
There are three ways to give them: edit the `address` fields in the plan, pass a station file, or
pass single addresses.

```bash
python -m benchrun probe examples/regulator_line_load.json --station examples/station_example.json
```

```bash
python -m benchrun run examples/regulator_line_load.json --serial SN-4001 --station my_station.json
```

```bash
python -m benchrun run examples/regulator_quick.json --serial SN-4002 --address supply=192.0.2.11:5025 --address dmm=192.0.2.13:5025
```

The addresses in the first and third of these are placeholders, so as written they end in a
connection error. `my_station.json` is your own file, and until it exists the second says it cannot
read it. A station file looks like
`{"addresses": {"supply": "192.0.2.11:5025"}}`; see
[examples/station_example.json](examples/station_example.json). A key under `addresses` can also be a driver name,
which covers every instrument using that driver. It can describe more than a plan uses, and any
name that matches nothing is pointed out when you run. If the port is left out, 5025 is used. Run
`probe` first: it connects to each instrument, shows its `*IDN?` response and changes no settings.

**This path has not been exercised on physical hardware.** The simulators implement the commands
the drivers send, and the manuals behind them are cited in [docs/drivers.md](docs/drivers.md), but
instruments differ and neither of those is a test. Expect to check:

- Whether your instrument listens on a raw SCPI socket, and on which port.
- Whether its dialect matches. To see what each driver sends:

  ```bash
  python -m benchrun drivers psu --commands
  ```

  Any entry can be replaced per instrument in the plan, with no code, using the same field names.
  These replacements are part of the plan, so its recorded hash changes with them:

  ```json
  "scope": {
    "driver": "scope",
    "address": "192.0.2.14:5025",
    "commands": { "measure_vpp": ":MEASure:ITEM? VPP,CHANnel{channel:d}" }
  }
  ```

- Whether responses are plain numbers. A reply with a unit attached, such as `5.000V`, aborts the
  run rather than being guessed at, unless the instrument's `reply_pattern` in the plan says where
  the number is. An error query that does not answer in the form `0,"No error"` also aborts the run.
  That form cannot be changed from a plan, but the query can, through the `next_error` command.
- Which status bit your supply uses for constant current. `current_limited` reads bit 10 of
  `STAT:OPER:COND?`. With the wrong bit it always reads 0, and a limit on it proves nothing. Set the
  right one in the same `commands` section, for example `"constant_current_bit": "8"`.
- That the timing suits your instruments. `timeout_s` is per instrument, default 5 seconds.
  `settle_s` on a step is the wait between applying a stimulus and measuring.
- Use `"expect_idn"` with a regular expression to refuse the run if the wrong instrument answers.

To talk to an instrument by hand, here the simulated multimeter with `sim` running and no fault set:

```bash
python -m benchrun scpi 127.0.0.1:5025 "*IDN?" "SYST:ERR?"
```

To see all traffic from a run, add `--scpi-log traffic.scpi.log` to `run`. If something goes wrong,
[docs/troubleshooting.md](docs/troubleshooting.md) goes through what each message means.

## What benchrun cannot do

- **It cannot switch anything off once it is gone.** Ctrl+C, closing the window and a service stop
  all switch outputs off first. On Windows, closing the window allows about 5 seconds, so an instrument
  that has stopped answering can use up that time. A power cut, a killed process, a crash of the machine or a lost
  network cable cannot: the instrument keeps doing what it was last told, which may be an energized
  output. If that could hurt anyone or anything, use a hardware interlock, or the instrument's own
  protection or watchdog. Software on another machine is not a safety system.
- **Its safe state knows only what it was told.** Drivers switch off their own outputs. The `scpi`
  driver has no idea what its commands do, so put switch-off commands in the plan's `teardown`. On
  a switch or multi-output supply, anything closed or turned on through raw `write` needs the
  `open_all` or `all_off` command set. `validate` and the end of each run print a note about these
  cases, but nothing can check them.
- **It talks SCPI over raw TCP only.** No GPIB (General Purpose Interface Bus), USB (Universal Serial
  Bus), serial or VXI-11 (the VME eXtensions for Instrumentation LAN protocol). A gateway that gives
  a raw socket will do.
- **The network is not protected.** SCPI over TCP has no login and no encryption, so keep instruments
  on a bench network you trust. See [SECURITY.md](SECURITY.md).
- **Results are not tamper-proof.** The hash in each run shows which plan ran. It does not prove that
  the database has not been edited, and neither does the audit log.
- **The web interface has no login and no encryption.** It listens on this computer only unless
  given `--public`, and then anyone who can reach it sees every result.
- **Archives carry runs, not set-up.** A run arrives with its lot, its station name, its product's
  part number and its instruments' asset tags, but the inventory, stations and products themselves
  stay in the database they were made in.
- **A few runs make poor statistics.** `stats` computes capability from two values, but with fewer
  than about 25 the indices change a lot from one batch to the next.

## What has and has not been tested

| | Status |
|---|---|
| Physical instruments of any kind | **Never.** No driver has met the instrument it was written for |
| Bundled simulators | Every driver, example and failure mode in the test suite |
| Real sockets to a separate process | Timeouts, late, partial, garbled and flooded replies, dropped connections, a process dying mid-run, wrong identity, outputs that refuse to go off, stopping a run by signal |
| Python | Version 2.0.0: full suite on 3.9 and 3.12 on Windows 11. Version 1.1.0 also ran on 3.9 and 3.14 on Linux; 2.0.0 has not been run on Linux. Other versions not run |
| Operating systems | Windows 11. Linux for 1.1.0 only (Debian-based Python 3.9 container as a non-root user; Ubuntu under WSL, the Windows Subsystem for Linux, as root) |
| Inventory, stations, products, lots, statistics, archives, `db`, web | The walk-through above, end to end on simulators started by `sim` in a separate process, plus every report and export format, an archive imported into a second database, `db info` and `db check` on both, and the web pages and API read with Python's `urllib` |
| Upgrading a 1.1.0 database | A database written by 1.1.0 (pass, fail, aborted and some-steps runs) upgraded to schema 3 with its backup kept. Its runs read correctly in `history`, `stats`, `show`, every report and export format, `archive export` and `import`, a lot, and the web pages; a run file from 1.1.0's `show --json` imported and was recognised as already there. 1.1.0 then refuses the upgraded file, as intended |
| Web browsers | **Not tried.** The web interface was read with Python's `urllib` and the test suite's HTTP client, never in a real browser |
| xlsx exports | Checked as zip and XML by the tests and by reading them back with Python. **Never opened in Excel** or any other spreadsheet |
| macOS | Not run; no reason known why it would differ from Linux |
| More than one benchrun on one database | Six processes at once on one file, nothing lost. Each waits up to 30 seconds for the others |
| Scale | With 1.1.0: 2,000 simulated units in a row on one database: 1,760 pass, 160 fail and 80 aborted, each as expected, with the supply off after every one and no run left open. One plan with a sweep of 100,000 points, the most allowed |

What those two runs measured, with version 1.1.0 (not repeated for 2.0.0), on Windows 11 with Python 3.9, with simulators on the same machine and
other work running at the same time. They measure benchrun and SQLite, not a bench: real instruments
are far slower than simulators. Run [tests/soak.py](tests/soak.py) to get your own.

| Measured | 2,000 units | 100,000-point sweep |
|---|---|---|
| Measurements recorded | 194,880 | 100,001 |
| Total time | 1,437 s (719 ms per unit, including starting simulators) | 634 s (about 6 ms per point, including reports) |
| Database size | 37.1 MB | 12.7 MB |
| `history --serial` | 6 ms | |
| `history --plan --stats` | 1.7 s | 1.9 s, printing 9 MB |
| `show --latest` | 103 ms | 1.2 s |
| HTML, CSV and XML report sizes | | 26.9, 13.6 and 7.8 MB |

The time per measurement in a long sweep is mostly the commit that keeps it safe from a crash.

## The simulators

Each instrument is a real TCP server with a real SCPI parser: short and long mnemonics, optional
nodes, numeric suffixes, compound messages with `;` and the relative path rule, numeric suffixes
such as `500MV`, `MIN` and `MAX`, IEEE 488.2 common commands (`*IDN?`, `*RST`, `*CLS`, `*ESR?` and
others), and the error queue through `SYST:ERR?`. Measurements respond to stimulus through a
regulator model, with seeded noise so runs repeat. `--seed` picks the noise and `--noise 0` turns
it off.

| Option | Choices |
|---|---|
| `--dut` | `good`, `high-output`, `poor-line`, `poor-load`, `high-dropout`, `high-iq`, `poor-psrr`, `dead` |
| `--fault` | `instrument:kind[:after]`; instruments `dmm`, `psu`, `eload`, `scope`, `siggen`, `smu`, `counter`, `switch`, `lcr`, `psu_multi`; kinds `drop`, `hang`, `error`, `garbage`, `overload` (`siggen` and `switch` take only the first three) |

Use `--base-port` if ports 5025 to 5034 are taken. `--base-port 0` takes any free ports, and
`--write-addresses FILE` records them in a file you can pass to `--station`.
[docs/simulators.md](docs/simulators.md) has the equations.

## Layout

| Path | What it does |
|---|---|
| `benchrun/transport.py` | Raw TCP link, and the logging wrapper |
| `benchrun/drivers/` | Instrument base class and one driver per instrument type |
| `benchrun/plan.py`, `expr.py`, `units.py`, `limits.py` | Plan validation, expressions, units, verdicts |
| `benchrun/runner.py` | Executes a plan and decides the result |
| `benchrun/session.py` | What `run` and `lot run` share: addresses, simulators, one unit's run |
| `benchrun/store.py` | SQLite schema, upgrades of older databases, recording and queries |
| `benchrun/reports/` | Report formats, the lot report, and CSV, JSON Lines and xlsx exports |
| `benchrun/sim/` | SCPI parser, instruments, regulator model and socket server |
| `benchrun/inventory.py`, `stations.py`, `audit.py` | Instrument inventory, named stations, the audit log |
| `benchrun/products.py`, `product_commands.py`, `plugins.py` | Products and their values; driver plugins |
| `benchrun/lots.py`, `lot_commands.py` | Lots and `lot run` |
| `benchrun/stats/` | Statistics engine, queries, charts and the `stats` command |
| `benchrun/archive.py`, `maintenance.py` | Run archives; the `db` commands |
| `benchrun/web/` | `serve`: pages, JSON API, downloads and the run launcher |
| `benchrun/cli.py`, `console.py` | Command line, and the output and argument helpers every command uses |
| `tests/` | Standard library `unittest` suite, the fake instrument and `soak.py` |

## Checks

Run the tests from the repository root:

```bash
python -m unittest discover
```

`check.sh` runs the tests, validates the example plans and does an end-to-end run:

```bash
sh check.sh
```

Tests use ephemeral ports on 127.0.0.1, so they can run while `sim` is up. One fixture
([tests/fixtures/verdict_expected.json](tests/fixtures/verdict_expected.json)) holds results worked
out from the model equations separately from the code, including units that must fail and runs that
must abort; `tests/test_verdict_fixture.py` compares the real command line's database against it.
[docs/ci.md](docs/ci.md) has a job definition to copy into a continuous integration system.

## More

[CHANGELOG.md](CHANGELOG.md), [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md).

## License

MIT. See [LICENSE](LICENSE).
