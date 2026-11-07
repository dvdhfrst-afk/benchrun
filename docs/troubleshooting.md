# Troubleshooting

Start with `probe`, which connects to each instrument and shows its identity without changing any
setting, and then add `--scpi-log` to a run to see every command and reply:

```bash
python -m benchrun probe my_plan.json --station my_station.json
```

```bash
python -m benchrun run my_plan.json --serial TEST --station my_station.json --scpi-log traffic.scpi.log
```

## Messages

| You see | What it means | What to do |
|---|---|---|
| `Cannot continue:` and a list | The plan or the arguments have a problem. Nothing was sent to any instrument and no run was recorded. | Fix each line. `validate` gives the same list. |
| `cannot connect to HOST:PORT`, saying refused | Nothing is listening on that address and port. | Check the address and that the instrument has raw sockets turned on. |
| `cannot connect to HOST:PORT: timed out` | Nothing answers at that address. | Check the network and any firewall. |
| `no response from HOST:PORT within N s` | Connected, but no reply to a query. | Raise `timeout_s`. Otherwise the instrument may not understand the command. Look at the log. benchrun ends each command with a line feed and expects each reply to end with one (a carriage return before it is fine), so set an instrument with a choice of line ending to that. |
| `non-numeric response` | The reply is not a plain number. | If text surrounds the number, set `reply_pattern`. If the reply is meant for an earlier query, look for a command that should not have answered. |
| A note saying overload | The instrument says the signal is out of range. | This counts against the unit, not the bench. Check the range and the wiring. |
| `instrument reported error after ...: -113,...` | The instrument rejected a command, and the run stopped so that an old setting cannot look like a pass. | Compare `drivers NAME --commands` with your manual, and replace the command in the plan's `commands`. |
| `*IDN? returned ..., which does not match` | `expect_idn` did not match what answered. Nothing was commanded. | Fix the address, or the pattern. |
| `is a SQLite file that benchrun did not create` | `--db` points at someone else's database. It has not been changed. | Choose another file. |
| `has schema N, newer than` | The database came from a newer benchrun. It has not been changed. | Upgrade benchrun. |
| `database is locked` | Another process kept it busy for longer than benchrun waits, about 30 seconds a write. The wait itself is silent, so readings can come late while it lasts. | Look for another benchrun or a database viewer holding it open. |
| `could not confirm these are off` | The run ended, but an output could not be read back as off. | **Check that instrument by hand before touching the bench.** |

## A run stays `running` in history

The process died: a kill, a power cut, a crash. Everything measured so far is in the database, and
reports say the run never finished. Check the outputs by hand, because nothing was able to switch them
off. Test the unit again with a new run.

## Results database

- An older database is upgraded when it is first opened. A copy of the old file is kept beside it as
  `NAME.schemaN.bak` and is never overwritten. Older versions of benchrun cannot read the new file; go
  back to the copy if you need to.
- A database on a network share is not recommended: SQLite's locking relies on the file system, and
  some network file systems do not get it right.
- To back up a database in use, use SQLite's own backup and do not copy the file during a run.

## Windows

- A plan written by PowerShell's `>` is UTF-16. benchrun reads that. Other encodings are refused, and
  the message says to save as UTF-8.
- A report open in a program that locks it, such as a spreadsheet, can stop a replacement. benchrun
  says so, exits with code 3, and leaves the verdict in the database. Close it and run `report`.
- Ctrl+C and Ctrl+Break both stop a run and switch outputs off. Ending the process from Task Manager
  cannot.
- A firewall prompt may appear the first time `sim` listens. It only listens on 127.0.0.1, so it can
  be declined.

## Linux

- `kill` (SIGTERM) and a closed terminal (SIGHUP) stop a run and switch outputs off. `kill -9` cannot.
- `sim` uses ports above 1024, so it needs no special permission.
- A port still shown as in use just after `sim` exits will free up shortly. Use `--base-port 0` for
  any free ports.

## Simulators

- `address already in use`: another `sim` is running, or something else has the ports. Use `--base-port`
  or `run --sim`.
- An example plan fails after `sim --dut poor-line`: it is meant to. Restart `sim` without `--dut`.
- A fault hangs a simulator until it restarts: `drop` and `hang` do not recover on purpose.
