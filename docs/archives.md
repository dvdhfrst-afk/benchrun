# Moving runs between databases

Each bench writes its own results database. To bring runs together, for example from every station on
a line into one database for the quality team, export them into an archive on one computer and import
the archive on another. Nothing is merged or overwritten on the way: a run either arrives whole, is
recognised as already there, or is refused with a reason.

## Commands

Write the runs you want to an archive. `archive export` takes the same filters as `export` (`--serial`,
`--plan`, `--status`, `--since`, `--until`), and `--run ID`, which can be given more than once. Filters
given together all apply. It refuses to replace an existing file unless you add `--force`, and it
writes the file all at once, so a full disk never leaves half an archive behind.

```bash
python -m benchrun archive export --db line1.sqlite --out line1-week45.zip --since 2026-11-03
python -m benchrun archive export --db line1.sqlite --out two-runs.zip --run 12 --run 15
```

See what is inside without importing anything:

```bash
python -m benchrun archive list line1-week45.zip
```

```
line1-week45.zip: benchrun archive (format 1) made 2026-11-10T05:25:09.081+00:00 by benchrun 1.1.0 from database e4b176d03b575c6a4416b39aaa32f401
2 runs readable

uuid                              serial   plan                 status  started (UTC)        measured
--------------------------------  -------  -------------------  ------  -------------------  --------
aec5ac52956a4e729a63e3ba47f56ef1  SN-1001  regulator-quick 1.0  PASS    2026-11-10 05:25:08  3
da3a0ebcc1ae4cbcbbd3e628e82ddbbf  SN-1002  regulator-quick 1.0  FAIL    2026-11-10 05:25:08  3
```

Check an archive after copying it, before you rely on it:

```bash
python -m benchrun archive verify line1-week45.zip
```

```
line1-week45.zip: OK, 2 runs; every member matches its SHA-256 and size, and every run record is sound
```

Import it. See what would happen first with `--dry-run`, which does the whole import inside a
transaction and then rolls it back, and does not create the database if it is not there yet:

```bash
python -m benchrun archive import line1-week45.zip --db central.sqlite --dry-run
python -m benchrun archive import line1-week45.zip --db central.sqlite
```

```
line1-week45.zip: benchrun archive (format 1) made 2026-11-10T05:25:09.081+00:00 by benchrun 1.1.0 from database e4b176d03b575c6a4416b39aaa32f401
outcome   run here  uuid                              serial   plan                 status  started (UTC)
--------  --------  --------------------------------  -------  -------------------  ------  -------------------
imported  1         aec5ac52956a4e729a63e3ba47f56ef1  SN-1001  regulator-quick 1.0  PASS    2026-11-10 05:25:08
imported  2         da3a0ebcc1ae4cbcbbd3e628e82ddbbf  SN-1002  regulator-quick 1.0  FAIL    2026-11-10 05:25:08
Imported 2 runs into central.sqlite; 0 already there, 0 refused.
```

`archive list`, `archive verify` and `archive import` all take `--json`.

## How runs are matched

Every run has a uuid, made when it starts, that stays with it in every database. Import compares
uuids:

- **A uuid that is not in the database:** the run is added. It gets new row ids, and its instruments,
  steps and measurements are added with it, pointing at the new ids. New runs are added in the order
  the archive lists them, which is the order they started.
- **The same run already there:** it is skipped. Two copies are the same run when everything recorded
  about them is the same: the run, its plan text and variables, its instruments, every step and every
  measurement. Where and when a copy was imported does not count.
- **A different run with the same uuid:** it is refused, and the run already in the database is left
  exactly as it was. This happens when a run was changed in one database after it was copied, or when
  a file was edited by hand. The import goes on with the other runs, and the command exits with 1.
- **A run still marked running:** it is refused, because a run being recorded somewhere else, or one
  whose process died, would look like a finished run that measured less. Add `--include-unfinished` to
  import such runs anyway; they stay marked running in the new database. If the run later finishes in
  its own database, that finished copy is a different run with the same uuid, and is refused. Use
  `db mark-abandoned` in the source database for runs whose process ended (see
  [database.md](database.md)).

Everything an import does happens in one transaction. If anything fails part way, such as a full disk
or a locked database, nothing at all is added.

An imported run records where it came from. `origin` is the database id of the database that recorded
it (kept as it was if the run had already been imported once before), and `imported_at` is when it
arrived. A run that names a lot brings the lot with it: if the database has no lot with that name, it
is added with the archive's details and an audit entry (`lot.create`); if it has one, the run is linked
to it.

Each import, except a dry run, writes one audit entry, action `archive.import`, subject
`archive:<file name>`, with the counts of runs imported, skipped and refused and the uuids refused.

## Exit codes

| code | meaning |
| --- | --- |
| 0 | done: every run was imported or was already there; `verify` and `list` found nothing wrong |
| 1 | `verify` or `list` found problems, or `import` refused one or more runs |
| 2 | bad arguments, a missing file, or a file with problems, in which case `import` changes nothing |
| 3 | could not complete, such as a database error; nothing was imported |

## Runs saved by `show --json`

`archive import` also takes a file written by `show --json`, which holds one run. This is how runs
saved as files by benchrun 1.1.0 come into a database. The first command prints the run; save what it
prints as `old-run.json`, then import that file:

```bash
python -m benchrun show --run 7 --json --db old.sqlite
python -m benchrun archive import old-run.json --db central.sqlite
```

benchrun 1.1.0 gave runs no uuid, so such a file has none. The run is given one made from its
content: the same file always gets the same uuid, so importing it twice skips it the second time,
whatever its line endings or encoding (UTF-8, with or without a byte order mark, or UTF-16, which
Windows PowerShell 5.1 writes for `>`). If the database already holds the same run under another uuid,
as happens when a 1.1.0 database was upgraded, the file's run is recognised by its serial number, start
time and plan, compared in full, and skipped. A file written by a later `show --json` carries the run's
own uuid, which is used as it is. The lot such a file names by row id cannot be found in another
database, so the run arrives without one. `origin` is set to `file:<file name>`.

`archive list` and `archive verify` read these files too.

## The archive format

An archive is a zip file with two kinds of member:

- `manifest.json`: `format` (`"benchrun-archive"`), `format_version` (1), `created_at`,
  `benchrun_version`, `database_id` (the source database's id), `run_count`, and `members`, a list
  giving each run member's `name`, `sha256` and `size` in bytes.
- `runs/<uuid>.json`: one whole run: every column of the run, including the full plan text and its
  variables, and its `lot` (or null), `instruments`, and `steps`, each step holding its
  `measurements`. Row ids are left out, because they mean nothing in another database.

A reader that finds a `format_version` newer than it knows refuses the archive and says so, rather
than guessing at it.

## Archives from somewhere else

An archive may have been damaged on the way, edited, or made to cause harm. Before anything is
imported, every member is read and checked, and every problem found is reported at once:

- Member names must be `manifest.json` or `runs/<32 hex digits>.json`. Absolute paths, `..`, backslashes,
  names that appear twice and anything else are refused. Nothing is ever extracted to disk.
- At most 100,001 members, 64 MiB unpacked per member, and 4 GiB unpacked in all. Sizes are counted
  while reading, not taken from the zip headers, which can say anything. Encrypted members and
  compression other than deflate are refused.
- Every member's size and SHA-256 must match the manifest, every run member must be listed there and
  every listed member must be present, and `run_count` must agree.
- Every run record must be strict JSON (no repeated keys, no `NaN`) with exactly the fields described
  above, each of the right type: text within its length limit (the plan text up to 16 MiB, operator and
  note up to 2,000 characters, messages up to 100,000, names up to 1,000), times with a UTC offset as
  the database stores them, known run and step statuses and verdicts, and different ordinals for the
  steps of a run. The uuid inside must match the member's name.
