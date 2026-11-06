# Plan format

A plan is a JSON file. Unknown keys are errors, so a typo such as `"hihg"` is caught by `validate`
instead of silently removing a limit.

## Top level

| Key | Required | Meaning |
|---|---|---|
| `name` | yes | Plan name. History and statistics group runs by it. |
| `version` | no | Free text, recorded with each run. |
| `description` | no | Free text. |
| `vars` | no | Named numbers. Override for one run with `--set name=value`. |
| `instruments` | yes | Object of instrument name to instrument settings. |
| `setup` | no | Calls made once before the first step. |
| `steps` | yes | List of steps, run in order. |
| `teardown` | no | Calls made once after the last step, after any outcome. |

Names of variables, instruments, steps and measurements start with a letter or underscore and use
only letters, digits and underscores.

## Instruments

```json
"supply": {
  "driver": "psu",
  "address": "192.0.2.11:5025",
  "timeout_s": 5.0,
  "expect_idn": "SIM-PSU|PSU-2000",
  "commands": { "measure_voltage": "MEAS:VOLT:DC?" }
}
```

| Key | Required | Meaning |
|---|---|---|
| `driver` | yes | `dmm`, `psu`, `eload`, `scope`, `siggen`, or `scpi` for raw commands only. |
| `address` | no | `host`, `host:port` or `[ipv6]:port`. The default port is 5025. Must be set here, or by `--station` or `--address`. |
| `timeout_s` | no | Seconds to wait for a response. Default 5. |
| `expect_idn` | no | Regular expression searched for in the `*IDN?` response. No match aborts the run. |
| `commands` | no | Replacement SCPI templates, by the keys that `drivers NAME --commands` lists. On `psu`, the key `constant_current_bit` holds a bit number, not a template. |

At the start of a run each instrument is connected and identified, its response is recorded, and
then it gets `*CLS` and `*RST`.

## Calls

A call is one line of `setup`, `apply`, `cleanup` or `teardown`:

```json
{ "do": "supply.set_voltage", "volts": "=vin" }
{ "wait_s": 0.25 }
```

Other keys of a `do` call are the method's arguments. Every driver also has `write`, which sends
any SCPI text and checks the error queue:

```json
{ "do": "dmm.write", "command": "VOLT:DC:NULL ON" }
```

## Steps

| Key | Meaning |
|---|---|
| `name` | Required, unique in the plan. |
| `description` | Free text. |
| `setup` | Calls made once before the sweep. |
| `sweep` | A sweep, or a list of sweeps for nested loops; the first is the outer loop. |
| `apply` | Calls made at every sweep point. |
| `settle_s` | Seconds to wait after `apply`, before measuring. |
| `measure` | Measurements taken at every sweep point, in order. |
| `cleanup` | Calls made once after the sweep. |
| `summary` | Calculations over the whole sweep, each with optional limits. |
| `stop_on_fail` | When true and this step fails, later steps are skipped. Teardown still runs. |

A step with no `sweep` measures once.

## Sweeps

```json
{ "var": "vin", "values": [7, 12, 20] }
{ "var": "vin", "start": 7, "stop": 20, "step": 1 }
{ "var": "vin", "start": 7, "stop": 20, "points": 14 }
```

`stop` is included when `step` lands on it. A negative `step` sweeps downward.

## Measurements

A reading from an instrument:

```json
{
  "name": "vout",
  "read": "dmm.measure_dc_voltage",
  "args": { "range_v": 10.0, "nplc": 1.0 },
  "units": "mV",
  "low": 4900,
  "high": 5100,
  "samples": 4
}
```

A value calculated from others:

```json
{ "name": "pdiss", "calc": "vin * iin - vout * iout", "units": "W", "high": 8.0 }
```

| Key | Meaning |
|---|---|
| `name` | Required, unique in the step. |
| `read` | `instrument.reading`. Exactly one of `read` or `calc`. |
| `calc` | An expression. A leading `=` is optional here. |
| `args` | Arguments to the reading. Values may be expressions. |
| `samples` | Take this many readings and record their mean. Default 1. |
| `units` | Must be compatible with what the reading returns; values and limits are in these units. For `read` it defaults to the reading's unit. For `calc` it is any label. |
| `low`, `high` | Inclusive limits, as numbers or expressions. Either may be left out. With neither, the verdict is `info`. |

Base units are `V`, `Vpp`, `Vrms`, `A`, `W`, `Hz`, `s`, `Ohm`, `dB` and `%`, with prefixes `p`, `n`,
`u`, `m`, `k`, `M` and `G`. `Vpp` and `Vrms` are different quantities from `V`, and converting
between them is refused.

## Expressions

Strings starting with `=` are expressions anywhere a number is expected: arguments, limits and
`wait_s`. Allowed are numbers, names, `+ - * / **`, parentheses, and these functions:

| Function | Meaning |
|---|---|
| `abs(x)`, `sqrt(x)`, `log10(x)` | As usual. |
| `min(a, b, ...)`, `max(a, b, ...)` | Of several numbers, or of one list. |
| `mean(list)`, `sum(list)`, `count(list)` | Over a list. |
| `first(list)`, `last(list)` | Ends of a list, in sweep order. |
| `span(list)` | `max(list) - min(list)`. |

What names mean depends on where the expression is:

| Where | Names available |
|---|---|
| Plan `setup` and `teardown`; step `setup` and `cleanup` | Plan variables. |
| `apply`, and `measure` | Plan variables, sweep variables at this point, and earlier measurements at this point. |
| `summary` | Plan variables as numbers; sweep variables and measurements as lists over the sweep; earlier summary values as numbers. |

Using a list where a number belongs, such as `"vout * 1000"` in a summary, is reported by `validate`.

## Readings and actions

```bash
python -m benchrun drivers
```

prints every action and reading, its arguments, and the unit each reading returns.
