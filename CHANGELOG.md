# Changelog

## 2.0.0 (2026-11-09)

### Added

- Instrument inventory (`instrument`), named stations built from it (`station`), and `run`, `probe`
  and `validate --station-name`. A run on a station records each instrument's asset tag. See
  `docs/stations.md`.
- An audit log of every change to instruments, stations, products and lots, and of archive imports
  and runs marked abandoned, with the `audit` command. Every command that writes it takes `--actor`.
- Products (`product`): a part number's plan, variables, limits and serial pattern, used by
  `run --product`. Driver plugins from installed packages. See `docs/products.md` and
  `docs/plugins.md`.
- Lots (`lot`): open, test units one after another with `lot run`, add one with `run --lot`, show,
  report, close and reopen. See `docs/lots.md`.
- `stats`: Cp, Cpk, Pp and Ppk, yield, failure Pareto, histograms and individuals control charts with
  the Western Electric rules, as text, JSON, CSV or HTML. See `docs/statistics.md`.
- Report formats `json`, `markdown` and `certificate` (`report --format every`), and `export --format
  jsonl` and `xlsx`. See `docs/reports.md`.
- `archive export`, `list`, `verify` and `import` to move runs between databases with checks, and the
  `db` commands `info`, `check`, `backup`, `vacuum` and `mark-abandoned`. See `docs/archives.md` and
  `docs/database.md`.
- `serve`: a web interface and JSON API over a results database, with downloads and an optional run
  launcher. See `docs/web.md`.
- `history`, `export` and `archive export` take `--product`, `--lot` and `--station`.

### Changed

- Results databases go to schema 3. An older file is upgraded in one transaction when it is first
  opened, and a copy is kept beside it as `NAME.schema2.bak` (or `.schema1.bak`). Every run gets a
  uuid. Version 1.1.0 cannot read the new file.
- The capability column of `history --stats` is now labelled `ppk`. It was labelled `cpk`, but it
  always computed the overall-sigma index, which is Ppk; the numbers are unchanged. `stats` gives
  both Cpk and Ppk.
- The default report formats are unchanged: `report` and `run --report-dir` still write `all`, meaning
  html, csv and junit, and `export` still writes the same CSV file by default.
- `--actor` defaults to your login name on every command that writes the audit log.
- Part numbers and lot names are unique ignoring case and found ignoring case, like asset tags and
  station names, in every command, filter and web page.
- `instrument export` and `station export` write to `--out FILE`, as every other export does.

### Fixed

- A unit's page in the web interface showed a stray `</p>` and said "1 runs".
- A form too large for the web interface could reset the connection instead of answering 413.
- The web pages and API took a lot only by id in its address; they now take its name too.
- A product's web page opened with another case of its part number showed that spelling as its name.

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
- Deeply nested JSON printed a Python traceback.
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
- A stop request while waiting at an operator prompt on Windows was held off until someone pressed Enter.
- A stop request after the switch-off could leave a run marked as still going, or lose its result line.
- `reply_pattern` could read `0,500 V` as 500, and got in the way of `*OPC?` and status queries.
- Text that cannot be stored, from a terminal in another encoding or a lone escape in a plan, printed a
  Python traceback. An impossible host name did too.

## 1.0.0

First version: plans, runner, results database, reports, five drivers and simulators.
