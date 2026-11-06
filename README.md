# benchrun

An automated bench test runner. It drives instruments with SCPI (Standard Commands for
Programmable Instruments) over a raw TCP (Transmission Control Protocol) socket, runs test plans
written in JSON (JavaScript Object Notation) against one unit serial number at a time, records
every measurement in a SQLite database, and writes reports as HTML (HyperText Markup Language),
CSV (comma-separated values) and JUnit XML (Extensible Markup Language).

It needs Python 3.9 or newer and nothing else. There is no install step, no VISA (Virtual
Instrument Software Architecture) library and no third-party package.

**What has been tested:** benchrun has only ever been run against its own bundled simulators. It
has not been run against physical instruments, and no compatibility with any vendor's instrument
is claimed. The drivers use common SCPI commands; see
[Pointing it at real instruments](#pointing-it-at-real-instruments) for what that means in practice.

## Why use it instead of a script

- **A failed bench never looks like a good unit.** Every command is followed by an error queue
  check. A lost connection, a timeout, a rejected setting or a garbled response ends the step with
  a readable message and records the run as aborted. Tests cover that a supply refusing a setting
  cannot turn into a pass measured at the old setting.
- **Traceability is automatic.** Each run stores the serial number, each instrument's `*IDN?`
  response and address, every value with its limits and verdict, the full plan text and its hash.
- **Plans are checked before the bench is powered.** `validate` reports every typo, unknown method,
  unit mismatch and bad limit at once, with no instruments connected.
- **The bench is always left safe.** Teardown runs after every outcome, including aborts and
  Ctrl+C, and supply, load and generator outputs are switched off even if the plan's teardown fails.

## Quick start

Everything runs from the repository root. In one terminal, start the simulated bench: a digital
multimeter (DMM), a power supply, an electronic load, an oscilloscope and a signal generator, wired
to a simulated 5 V linear regulator as the device under test (DUT).

```bash
python -m benchrun sim
```

It listens on 127.0.0.1 only, on ports 5025 to 5029. Leave it running. In a second terminal, check
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

The run prints each instrument's identity, one line per step, and a result:

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
```

Results go into `benchrun.sqlite` in the current folder; use `--db` to choose another file. Look
at them by serial number or by plan:

```bash
python -m benchrun history --serial SN-1001
```

```bash
python -m benchrun history --plan regulator-line-load
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
| 2 | Bad plan, bad arguments, or nothing matching; no run was recorded |
| 3 | Run aborted, or an instrument could not be reached |

## How a run is judged

| Result | When |
|---|---|
| `pass` | Every limited measurement passed, and at least one was evaluated |
| `fail` | All instruments behaved and at least one measurement failed |
| `aborted` | Connection lost or refused, timeout, instrument error, garbled response, `*IDN?` mismatch, operator interrupt, or teardown failure |
| `running` | The process died before finishing; everything measured so far is still in the database |

Aborted outranks failed, because an aborted run was not completed. A few cases worth knowing:

- Limits are inclusive, and either side may be left out.
- A measurement with no limits is recorded with the verdict `info` and cannot fail a unit.
- A reading past the instrument's range (the SCPI overload value 9.9E37) is a `fail` for the unit,
  not an abort: the instrument is working, and the signal is out of range.
- A calculation with no answer, such as a ratio dividing by a dead output, is also a `fail`.
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

To check a plan you are writing:

```bash
python -m benchrun validate my_plan.json
```

Override a variable for one run with `--set`, for example to test a 3.3 V part with the same plan:

```bash
python -m benchrun run my_plan.json --serial SN-3001 --set vnom=3.3
```

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
- **CSV** has one row per measurement, with values at full precision.
- **JUnit XML** has one test case per measurement. Failures and aborts show as `failure` and
  `error`, and traceability data is in the suite's properties.

`history --plan` adds first-pass yield (each serial counted once, on its first run that reached a
verdict; aborted runs say nothing about a unit). `--stats` adds per-measurement statistics, kept
apart per sweep point, including the process capability index (Cpk). `--json` is for scripts:

```bash
python -m benchrun history --plan regulator-line-load --stats
```

```bash
python -m benchrun history --serial SN-1001 --json
```

## Pointing it at real instruments

The plan does not change. Only the addresses do, and there are three ways to give them: edit the
`address` fields in the plan, pass a station file, or pass single addresses.

```bash
python -m benchrun probe examples/regulator_line_load.json --station examples/station_example.json
```

```bash
python -m benchrun run examples/regulator_line_load.json --serial SN-4001 --station my_station.json
```

```bash
python -m benchrun run examples/regulator_quick.json --serial SN-4002 --address supply=192.0.2.11:5025 --address dmm=192.0.2.13:5025
```

A station file maps each instrument name in the plan to `host:port`; see
[examples/station_example.json](examples/station_example.json). If the port is left out, 5025 is
used. Run `probe` first: it connects to each instrument, shows its `*IDN?` response and changes no
settings.

**This path has not been exercised on physical hardware.** The simulators implement the commands
the drivers send, and those are widely used SCPI forms, but instruments differ. Expect to check:

- Whether your instrument listens on a raw SCPI socket, and on which port.
- Whether its dialect matches. To see what each driver sends:

  ```bash
  python -m benchrun drivers psu --commands
  ```

  Any entry can be replaced per instrument in the plan, with no code, using the same field names:

  ```json
  "scope": {
    "driver": "scope",
    "address": "192.0.2.14:5025",
    "commands": { "measure_vpp": ":MEASure:ITEM? VPP,CHANnel{channel:d}" }
  }
  ```

- That the timing suits your instruments. `timeout_s` is per instrument, default 5 seconds.
  `settle_s` on a step is the wait between applying a stimulus and measuring.
- Use `"expect_idn"` with a regular expression to refuse the run if the wrong instrument answers.

To talk to an instrument by hand:

```bash
python -m benchrun scpi 127.0.0.1:5025 "*IDN?" "SYST:ERR?"
```

To see all traffic from a run, add `--scpi-log traffic.scpi.log` to `run`.

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
| `--fault` | `instrument:kind[:after]`; instruments `dmm`, `psu`, `eload`, `scope`, `siggen`; kinds `drop`, `hang`, `error`, `garbage`, `overload` |

Use `--base-port` if ports 5025 to 5029 are taken. `--base-port 0` takes any free ports, and
`--write-addresses FILE` records them. [docs/simulators.md](docs/simulators.md) has the equations.

## Layout

| Path | What it does |
|---|---|
| `benchrun/transport.py` | Raw TCP link, and the logging wrapper |
| `benchrun/drivers/` | Instrument base class and one driver per instrument type |
| `benchrun/plan.py`, `expr.py`, `units.py`, `limits.py` | Plan validation, expressions, units, verdicts |
| `benchrun/runner.py` | Executes a plan and decides the result |
| `benchrun/store.py` | SQLite schema, recording and queries |
| `benchrun/reports/` | HTML, CSV and JUnit XML writers |
| `benchrun/sim/` | SCPI parser, instruments, regulator model and socket server |
| `benchrun/cli.py` | Command line |
| `tests/` | Standard library `unittest` suite |

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

## License

MIT. See [LICENSE](LICENSE).
