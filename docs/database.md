# Looking after a results database

The `db` commands look after one results database: what it holds, whether it is sound, a safe copy,
reclaiming space, and closing runs left running by a process that ended. Each takes `--db` (default
`benchrun.sqlite`) and refuses to run, without creating anything, if there is no database at that path.

## What is in it: `db info`

```bash
python -m benchrun db info --db central.sqlite
```

```
Database:        C:\bench\central.sqlite
Schema version:  3
Database id:     382c62bbddfc0e9fe9a6ae65b1f31a31
Size:            144.0 KiB (147456 bytes)
Runs:            3 (2 pass, 1 fail, 0 aborted, 0 running); 3 imported
Instruments:     0
Stations:        0
Products:        0
Lots:            0
Oldest run:      run 3, serial SN-1001, started 2026-11-10T05:08:09.851+00:00
Newest run:      run 2, serial SN-1002, started 2026-11-10T05:25:08.676+00:00
```

The database id is made once, when the database is created, and travels with every run exported from
it as that run's origin (see [archives.md](archives.md)). The size includes the write-ahead log, if
there is one. `--json` gives the same as an object.

## Whether it is sound: `db check`

```bash
python -m benchrun db check --db central.sqlite
```

It runs SQLite's integrity check and foreign key check, and then looks for what those cannot see:

- measurements filed under a step that belongs to a different run;
- runs without a uuid, which cannot be archived;
- steps still pending or running in runs that have finished;
- runs with a status benchrun does not know;
- runs still marked running, with how long ago they started.

A run still marked running may be in progress on this bench right now, so it is listed but is only a
problem once it started more than 24 hours ago; change that with `--older-than HOURS`. The command
exits with 0 when nothing is wrong and 1 when anything is, so a scheduled job can watch it. `--json`
gives `ok`, the list of `problems`, and the `running` runs with their ages.

```
Runs still marked running:
run  serial   plan             started (UTC)        age (hours)
---  -------  ---------------  -------------------  -----------
3    SN-1003  regulator-quick  2026-11-08 07:00:00  46.4

line1.sqlite: 1 problem
  run 3 (serial SN-1003) has been marked running for 46.4 hours
Runs left running by a process that ended can be closed with: benchrun db mark-abandoned --older-than 24
```

## A copy: `db backup`

```bash
python -m benchrun db backup --db central.sqlite --out central-backup.sqlite
```

This uses SQLite's online backup, so it is safe while benches go on writing to the database; copying
the file with the operating system while a run is being recorded can give a copy that does not open.
The copy is checked with SQLite's quick check and written under a temporary name, then renamed into
place, so `--out` is either the whole copy or untouched. It refuses to replace an existing file unless
you add `--force`, and refuses to write over the database itself.

## Reclaiming space: `db vacuum`

```bash
python -m benchrun db vacuum --db central.sqlite
```

Rebuilds the file so that space left by deleted rows is given back, and prints the size before and
after. It needs free disk space about the size of the database while it works, and holds the database
locked until it is done, so run it when no bench is recording.

## Runs left running: `db mark-abandoned`

A run whose process was killed, or whose computer lost power, stays marked `running`. History counts it
as never finished, and `archive import` refuses it. Once you know such runs will not finish, close them:

```bash
python -m benchrun db mark-abandoned --older-than 24 --db line1.sqlite --dry-run
python -m benchrun db mark-abandoned --older-than 24 --db line1.sqlite
```

```
run  serial   plan             started (UTC)        age (hours)  steps closed
---  -------  ---------------  -------------------  -----------  ------------
3    SN-1003  regulator-quick  2026-11-08 07:00:00  46.4         2
Marked 1 run aborted (abandoned: the process ended without finishing).
```

Every run still marked running that started more than `--older-than` hours ago becomes `aborted`, with
the reason `abandoned: the process ended without finishing`. Its steps are closed the way the runner
closes them when a run ends early: a step that was running becomes `error` with that reason, and a step
that had not started becomes `skipped` with `not run:` and the reason. Its finish time is set to the
last time anything was recorded for it, which is the nearest record of when its process stopped. Each
run marked writes one audit entry, action `run.mark-abandoned`, subject `run:<uuid>`, in the same
transaction as the change, naming `--actor` (default: your login name). A run that finishes while the command is looking at it is left alone.
`--dry-run` lists the runs and changes nothing; `--json` gives the same list.

`--older-than` must be above 0, and has no default: choose a time longer than your longest run.
