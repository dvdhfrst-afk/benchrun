# Driver plugins

A driver for an instrument benchrun does not know can live in its own Python package. Once that
package is installed, plans name its driver like any built-in one, and `run --sim` can use a
simulator the package provides. Nothing in benchrun has to change.

`examples/plugin_example/` is a complete plugin for the FH-1 fixture heater, an instrument made up
for this example, which warms the plate a unit sits on:

| File | What it is |
|---|---|
| `pyproject.toml` | The package, and the two entry points that tell benchrun about it |
| `benchrun_fixture_heater/driver.py` | The driver |
| `benchrun_fixture_heater/sim.py` | Its simulator |
| `heated_regulator.json` | A plan that uses it, with the built-in supply and multimeter |

## How benchrun finds a plugin

A package offers a driver through the `benchrun.drivers` entry point group, and a simulator for it,
under the same name, through `benchrun.simulators`:

```toml
[project.entry-points."benchrun.drivers"]
fixture_heater = "benchrun_fixture_heater.driver:FixtureHeater"

[project.entry-points."benchrun.simulators"]
fixture_heater = "benchrun_fixture_heater.sim:SimFixtureHeater"
```

The name on the left is the driver name plans use. It is 1 to 64 characters: lower-case letters,
digits and `_`, starting with a letter.

benchrun looks for plugins once, the first time a plan names a driver it does not have, or a
command lists drivers or starts simulators. A plugin is set aside, with the reason, when:

- it cannot be imported, whatever the error
- it is not a class based on `benchrun.drivers.base.Instrument`
- it has the name of a built-in driver; a plugin cannot replace one
- two installed packages offer the same name; neither is used
- its `COMMANDS` table lacks an entry of the base class, or holds a line break, a control
  character or a non-ASCII character
- it sets `HAS_OUTPUT = True` but does not override `safe_state`

A plugin set aside stops nothing else: other plans run as before. A plan that names it is refused
with exit code 2 and the reason. A simulator is set aside the same way when it is not a class
based on `benchrun.sim.instruments.SimInstrument`, when its `kind` is not the driver's name, or
when there is no usable driver plugin of that name.

## Writing a driver

Base the class on `Instrument` and keep every SCPI string in its `COMMANDS` table, starting from
the base class's own. A plan can then replace any one of them with no code, for an instrument
with a different dialect. Mark what plans may call:

- `@action` for a method a plan runs with `{"do": "heater.set_temperature", "celsius": 70}`
- `@reading("degC")` for a method that returns one number in those units, which a plan reads with
  `{"read": "heater.measure_temperature"}`. `@reading(None)` leaves the units to the plan.

Send commands with `self.command(key, **fields)`, which renders the template, sends it and then
reads the error queue, so a command the instrument rejected becomes a fault instead of a silent
pass. Read numbers with `self.number(key, **fields)`, which also refuses anything that is not a
plain decimal number, and the overload value. Annotate every argument (`celsius: float`): plans are
checked against the annotations before anything runs. The class's first docstring line is what
`drivers` prints.

### Safety rules

A plugin driver runs with outputs on, so it follows the same rules as the built-in ones:

- **Switch everything off in `safe_state`, then confirm it.** If the instrument can drive anything
  (an output, a heater, a relay), set `HAS_OUTPUT = True` and override `safe_state` to switch it
  off and ask whether it really is off. benchrun calls it at the end of every run, however the run
  ends, even when the plan's own teardown failed. Raise an `InstrumentFault` when the state cannot
  be confirmed; benchrun then tells the operator to check by hand. The example does this with the
  base class's `_confirm_off`.
- **Never send text that was not checked.** Only send what comes from `COMMANDS` templates with
  numeric fields, or text checked against a fixed set of words. A string argument from a plan must
  never reach the instrument as it is: a line break inside it would become a second command.
  benchrun refuses plan strings with control characters, and the `write` action is there for raw
  text on purpose; a driver must not add another way.
- **Refuse settings out of range before sending them.** The example refuses a plate temperature
  above 125 C with `CommandRefused`, which ends the run as aborted.
- **Remember nothing the instrument might have changed.** If the driver keeps settings to avoid
  sending them twice, override `_forget_settings`, which benchrun calls whenever a plan uses the
  raw `write` or `query_number`.

## Writing a simulator

Base the class on `SimInstrument`, set `kind` to the driver's name and `idn` to what `*IDN?` should
answer, and register handlers with `@scpi`. Return readings through `self.measured(value)`, so the
`--fault` option works on it, and add noise with `self.gauss(sigma)`, so `--noise` and `--seed`
work. `self.model` is the shared simulated bench, with the regulator the built-in instruments are
wired to; the example heater does not need it. The simulator is served after the built-in ones,
so with `sim` and its default ports the fixture heater is on 127.0.0.1:5035.

## Installing and checking

Install benchrun first, from its own folder, then the plugin. `-e` (editable) means changes to the
plugin's source take effect without installing again:

```bash
pip install -e .
pip install -e examples/plugin_example
```

The plugin's `pyproject.toml` does not list benchrun as a dependency, so that pip cannot fetch an
unrelated package of the same name. Then check that benchrun found it:

```bash
benchrun drivers --plugins
```

```text
name            kind       package                        from                                          state
--------------  ---------  -----------------------------  --------------------------------------------  -----
fixture_heater  driver     benchrun-fixture-heater 0.1.0  benchrun_fixture_heater.driver:FixtureHeater  ok
fixture_heater  simulator  benchrun-fixture-heater 0.1.0  benchrun_fixture_heater.sim:SimFixtureHeater  ok
```

Any plugin that cannot be used shows its reason in the `state` column, and the command then exits
with 3, so a script can check a bench PC. `--json` gives the same as data. `drivers` marks where
each driver comes from:

```bash
benchrun drivers fixture_heater --commands
```

```text
fixture_heater (plugin: benchrun-fixture-heater 0.1.0, benchrun_fixture_heater.driver:FixtureHeater): FH-1 fixture heater (made up): ``TEMP`` sets the plate, ``OUTP`` switches the heater, ``MEAS:TEMP?`` reads it.
  action   heater_off()
  action   heater_on()
  action   set_temperature(celsius)
  ...
```

## Testing

Run the example plan on the simulators. The heater's simulator starts with the built-in ones:

```bash
benchrun run examples/plugin_example/heated_regulator.json --serial SN-1001 --sim
```

```text
Run 1: serial SN-1001, plan heated-regulator 1.0
  supply     127.0.0.1:54707          benchrun,SIM-PSU,SIM0002,1.0
  dmm        127.0.0.1:54706          benchrun,SIM-DMM,SIM0001,1.0
  heater     127.0.0.1:54725          benchrun-example,FH-1,SIM0100,1.0
[PASS] hot_output (4 measured, 0 failed)
RESULT: PASS
```

Then make it go wrong, and check that it fails safely. A fault on the plugin's simulator works like
one on a built-in simulator, and `--scpi-log` shows the heater being switched off and checked:

```bash
benchrun run examples/plugin_example/heated_regulator.json --serial SN-1002 --sim --fault fixture_heater:garbage:1 --scpi-log heater.scpi.log
```

```text
[ERROR] hot_output: heater: non-numeric response '#BAD#' to 'MEAS:TEMP?'
RESULT: ABORTED (step hot_output: heater: non-numeric response '#BAD#' to 'MEAS:TEMP?')
```

The run exits with 3, and the end of the heater's part of the log reads:

```text
2026-11-10T05:26:34.944+00:00 heater >> OUTP OFF
2026-11-10T05:26:34.944+00:00 heater >> SYST:ERR?
2026-11-10T05:26:34.944+00:00 heater << +0,"No error"
2026-11-10T05:26:34.944+00:00 heater >> OUTP?
2026-11-10T05:26:34.944+00:00 heater << 0
2026-11-10T05:26:34.944+00:00 heater -- close
```

benchrun's own tests load this example through a stand-in for the entry point lookup, with no
installation, in `tests/test_plugins.py`; a plugin's own tests can do the same by patching
`benchrun.plugins._entry_points` and calling `benchrun.plugins.forget()` around each test.

Before connecting real hardware, run the plan with nothing wired to the outputs and `--scpi-log`
on, and read the log: every command your driver sends is in it.
