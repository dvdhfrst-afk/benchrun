# Contributing

## Setup

There is nothing to install. With Python 3.9 or newer, from the repository root:

```bash
python -m unittest discover
```

`sh check.sh` also validates the examples and does an end-to-end run. `tests/soak.py` tests many units
in one go, and is not part of the unit tests.

## Rules for the code

- Standard library only, and Python 3.9 syntax. `tests/test_repo_rules.py` checks this.
- LF line endings and no long dashes in any text file.
- A user's mistake gets a plain message and an exit code, never a stack trace.
- A problem on the bench must never be recorded as a good unit. When unsure, abort.
- Anything that writes a file the user keeps goes through `benchrun/files.py`.
- Changing the database schema means adding a step to `_UPGRADES` in `benchrun/store.py` and raising
  `SCHEMA_VERSION`, with a test in `tests/test_store_upgrade.py` that starts from the old form with data.

## Adding a driver

1. Add `benchrun/drivers/NAME.py`, with every command in `COMMANDS` so a plan can replace it, then
   `@action` and `@reading("unit")` methods. Look at `counter.py` for a small one.
2. If it can energize anything, set `HAS_OUTPUT = True` and write `safe_state` so it switches off and
   reads back. Look at `psu_multi.py`.
3. Register it in `benchrun/drivers/__init__.py`.
4. Add a simulator in `benchrun/sim/instruments.py`, and add its kind to the end of `PORT_ORDER` in
   `benchrun/sim/bench.py`, so existing ports do not move.
5. Add tests of the exact commands sent, and against the simulator, to `tests/test_more_drivers.py`.
6. Add an example plan. A test requires one for every driver.
7. Add it to `docs/drivers.md`, and cite the manual it came from, with page numbers. Say what you could
   not confirm.

A claim that a driver has been run on real hardware needs to say which instrument, firmware and plan.
Nothing so far has been, and the README says so. Please keep that statement accurate.

## Changes

Add a line to `CHANGELOG.md`. Keep to one subject per change, and explain why in the message.

By contributing you agree that your work is under the MIT license in [LICENSE](LICENSE).
