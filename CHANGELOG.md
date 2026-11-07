# Changelog

## 1.1.0 (2026-11-07)

### Added

- Drivers for a source measure unit, a frequency counter, a switch or multiplexer, an inductance,
  capacitance and resistance (LCR) meter and a multi-output supply, each with a simulator and an example
  plan. Multimeter readings for resistance, AC current and frequency. Sources in `docs/drivers.md`.
- Commands: `show` for one run, `export` for many runs in one CSV file, `init` for a starter plan.
- `run` options: `--note`, `--step`, `--verbose`, `--auto-confirm`.
- `history` filters: `--instrument`, `--since`, `--until`.
- Operator prompts in plans, and `reply_pattern` for replies with text around the number.
- Outputs are read back after being switched off, and a run warns by name if one cannot be confirmed.
- Service stops and closed terminals now stop a run the way Ctrl+C does.
- `CHANGELOG.md`, `CONTRIBUTING.md`, `SECURITY.md`, troubleshooting, continuous integration and driver
  source documents, more examples, sample reports.

### Changed

- Results databases go to schema 2. An older file is upgraded in one transaction when it is first
  opened, and a copy is kept beside it as `NAME.schema1.bak`. Version 1.0.0 cannot read the new file.
- Report file names now include the run's start time, so runs from different databases cannot overwrite
  each other. A script that builds these names needs updating.
- The CSV report has a new last column, `run_note`.
- `--scpi-log` adds to an existing file and no longer replaces it.
- The multimeter sends `DEF` for the default range and not `AUTO`, which the manual it follows does not
  list. A plan that replaced these commands is not affected.
- Plan messages about wrong argument types are worded differently.

### Fixed

- An instrument with the wrong identity was sent a switch-off command at the end of the run. It is now
  never commanded.
- A database failure mid-run skipped teardown and left outputs on.
- Stale replies, or a second Ctrl+C, could stop the end-of-run switch-off.
- `--set x=nan` made every limit using `x` pass.
- `9**9**9` in an expression hung the program.
- A sweep step of 1e-13 rounded every point to 0.
- Nested sweeps could fill memory before being refused.
- A key given twice in a plan silently dropped the first, which could remove a limit.
- Deeply nested JSON printed a stack trace.
- A line break or a non-ASCII character in a command could send a second command, or turn a setting into
  a query.
- `1_000` was accepted as a number in replies. A channel of 1.5 was cut down to 1.
- Reports were not written atomically.
- A closed output pipe could abort a run.
- A SQLite file belonging to something else was given benchrun's tables. It is now refused unchanged.
- An endless stream of stale data could stop a run from ever going on.
- A limit using a failed value aborted the run; it now fails the unit, and the run goes on.
- A limit with low above high at run time blamed the unit; it now aborts.
- A reading exactly on a limit could fail through binary rounding, such as 0.0009 A against 0.9 mA.
- Closing the console window on Windows did not reach the switch-off.
- A stop request after the switch-off could leave a run marked as still going, or lose its result line.
- `reply_pattern` could read `0,500 V` as 500, and got in the way of `*OPC?` and status queries.
- Text that cannot be stored, from a terminal in another encoding or a lone escape in a plan, printed a
  stack trace. An impossible host name did too.

## 1.0.0

First version: plans, runner, results database, reports, five drivers and simulators.
