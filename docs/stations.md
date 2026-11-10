# Instruments, stations and the audit log

A plan names instruments by role, such as `supply` and `dmm`. The results database can also hold
the instruments a lab owns (the inventory) and named stations that put one of them in each role.
A run on a named station takes its addresses from there and records which physical instrument,
by asset tag, filled each role. Every change to the inventory or a station is kept in an audit log.

Everything below was run on the simulators, in a fresh folder, with `sim` started in another
terminal and asked to write its addresses to a station file:

```bash
python -m benchrun sim --write-addresses sim_station.json
```

All of these commands take `--db` to choose the results database; the default is `benchrun.sqlite`
in the current folder. Each command that changes something takes `--actor NAME`, which is kept in
the audit log with the change. It is one line of free text, like `--operator` on `run`. Left out, it
is your login name, as on every benchrun command that writes the audit log; `--actor ""` records none.

## The inventory

An instrument has an asset tag, a driver, and optionally a manufacturer, model, serial number,
address, location and notes. Asset tags are 1 to 64 printable characters with no space at either
end, and are unique ignoring case: `psu-0001` finds `PSU-0001`. The driver is one of those that
`python -m benchrun drivers` lists. An address is checked the way plan addresses are, and stored in
full, so `192.0.2.11` is kept as `192.0.2.11:5025`. Two instruments in use cannot share an address.

```bash
python -m benchrun instrument add PSU-0001 --driver psu --address 127.0.0.1:5026 --manufacturer benchrun --model SIM-PSU --serial-number SIM0002 --location "Lab 1" --actor "Operator A"
```

```bash
python -m benchrun instrument add DMM-0001 --driver dmm --address 127.0.0.1:5025 --manufacturer benchrun --model SIM-DMM --serial-number SIM0001 --location "Lab 1" --actor "Operator A"
```

```bash
python -m benchrun instrument list
```

```text
asset tag  driver  manufacturer  model    serial   address         location  status
---------  ------  ------------  -------  -------  --------------  --------  ------
DMM-0001   dmm     benchrun      SIM-DMM  SIM0001  127.0.0.1:5025  Lab 1     active
PSU-0001   psu     benchrun      SIM-PSU  SIM0002  127.0.0.1:5026  Lab 1     active
2 instruments
```

`list` takes `--driver`, `--status`, `--location` (the whole name, any case) and `--search TEXT`,
which looks in the tag, manufacturer, model, serial number, address, location and notes. `--json`
is for scripts.

An instrument is `active`, `in-repair` or `retired`. Only an active one can be used in a run.

| Command | What it does |
|---|---|
| `instrument edit TAG --model X ...` | Change any field but the tag. An empty value clears a field. `--status` sets `active` or `in-repair` |
| `instrument retire TAG --reason TEXT` | Retire it. It stays in the inventory and in run history, and its address is free for another instrument |
| `instrument reactivate TAG --reason TEXT` | Make a retired or in-repair instrument active again |
| `instrument show TAG` | Its details, the stations it is on, and the newest runs that used it (`--runs N`, default 10) |

Nothing is ever deleted from the inventory, because recorded runs refer to instruments by tag.

## A station

A station has a name (1 to 64 printable characters, unique ignoring case), a description and a
location, and one instrument per role:

```bash
python -m benchrun station create LINE-1 --description "Regulator final test" --location "Lab 1" --actor "Operator A"
```

```bash
python -m benchrun station assign LINE-1 supply PSU-0001 --actor "Operator A"
```

```bash
python -m benchrun station assign LINE-1 dmm DMM-0001 --expect-idn SIM-DMM --actor "Operator A"
```

```bash
python -m benchrun station show LINE-1
```

```text
Station LINE-1: Regulator final test
Location: Lab 1
Created 2026-11-10T05:23:32.541+00:00, last changed 2026-11-10T05:23:33.106+00:00 (UTC); 0 runs recorded on it

role    asset tag  driver  address         status  timeout (s)  expected *IDN?
------  ---------  ------  --------------  ------  -----------  --------------
dmm     DMM-0001   dmm     127.0.0.1:5025  active               SIM-DMM
supply  PSU-0001   psu     127.0.0.1:5026  active
```

A role is matched to a plan the way a station file's keys are: a role named like one of the plan's
instruments is that instrument, and otherwise a role named like a driver covers the plan's
instrument with that driver. One inventory instrument can fill only one role on a station.

`assign` replaces whatever was in the role, including its settings. `--timeout SECONDS` replaces the
plan's `timeout_s` for that instrument, and `--expect-idn REGEX` replaces its `expect_idn`; left
out, the plan's own values apply. A retired instrument, or one with no address, cannot be assigned.
`station unassign NAME ROLE` empties a role, and `station list` lists the stations.

`station rename OLD NEW` renames a station. Runs already recorded keep the name they ran under, so
`history --station OLD` still finds them. `station delete NAME` refuses while runs were recorded on
the station, unless `--force` is given; even then the runs are kept, and the instruments stay in
the inventory.

### Checking a station

`check` connects to each instrument on a station, reads its `*IDN?` response and error queue, and
changes no setting, as `probe` does. It compares the response (maker, model, serial number,
firmware) with the inventory record, ignoring case and only where the record has a value, and with
the role's expected pattern:

```bash
python -m benchrun station check LINE-1
```

```text
role    asset tag  address         *IDN?                         state
------  ---------  --------------  ----------------------------  -----
dmm     DMM-0001   127.0.0.1:5025  benchrun,SIM-DMM,SIM0001,1.0  ok
supply  PSU-0001   127.0.0.1:5026  benchrun,SIM-PSU,SIM0002,1.0  ok
All 2 instruments on station LINE-1 answered and match the inventory.
```

It exits with 3 if any instrument does not answer, answers as something else, or is not active.

## Running on a station

`run`, `probe` and `validate` take `--station-name NAME`, looked up in `--db`:

```bash
python -m benchrun validate examples/regulator_quick.json --station-name LINE-1
```

```text
examples/regulator_quick.json: OK
  regulator-quick 1.0
  2 instruments, 1 steps, 3 measurements per run
  Station LINE-1: supply=PSU-0001 (127.0.0.1:5026), dmm=DMM-0001 (127.0.0.1:5025)
```

```bash
python -m benchrun run examples/regulator_quick.json --serial SN-1001 --operator "Operator A" --station-name LINE-1
```

```text
Run 1: serial SN-1001, plan regulator-quick 1.0
  supply     127.0.0.1:5026           benchrun,SIM-PSU,SIM0002,1.0
  dmm        127.0.0.1:5025           benchrun,SIM-DMM,SIM0001,1.0
[PASS] output_voltage (3 measured, 0 failed)
RESULT: PASS
```

Every address comes from the station, so `--station-name` cannot be combined with `--station FILE`,
`--address` or `--sim`. Before anything is connected, each instrument in the plan must have a role
on the station, filled by an active instrument with an address and the same driver as the plan
uses. Otherwise the command stops with exit code 2 and nothing is recorded. Roles the plan does
not use are pointed out in a note.

The run records the station's name and each instrument's asset tag. `show` and `show --json`
include them, and so does the instrument table of the HTML report:

```bash
python -m benchrun show --latest
```

```text
Run 1: serial SN-1001, plan regulator-quick 1.0
Result: PASS
Started 2026-11-10T05:23:34.302+00:00, finished 2026-11-10T05:23:34.386+00:00 (UTC)
Station: LINE-1
Operator: Operator A

instrument  driver  address         asset tag  *IDN?
----------  ------  --------------  ---------  ----------------------------
supply      psu     127.0.0.1:5026  PSU-0001   benchrun,SIM-PSU,SIM0002,1.0
dmm         dmm     127.0.0.1:5025  DMM-0001   benchrun,SIM-DMM,SIM0001,1.0

[PASS] (setup) (0 measured, 0 failed)
[PASS] output_voltage (3 measured, 0 failed)
[PASS] (teardown) (0 measured, 0 failed)
```

`history --station NAME` lists the runs recorded on a station:

```bash
python -m benchrun history --station LINE-1
```

`instrument show` lists the runs an instrument took part in, and in which role:

```bash
python -m benchrun instrument show PSU-0001
```

```text
PSU-0001: psu, active
  Manufacturer: benchrun
  Model: SIM-PSU
  Serial number: SIM0002
  Address: 127.0.0.1:5026
  Location: Lab 1
  Added 2026-11-10T05:23:31.580+00:00, last changed 2026-11-10T05:23:31.580+00:00 (UTC)
Stations: LINE-1 as supply
Newest runs that used it (up to 10):
run  started (UTC)        serial   plan                 status  as      station
---  -------------------  -------  -------------------  ------  ------  -------
1    2026-11-10 05:23:34  SN-1001  regulator-quick 1.0  PASS    supply  LINE-1
```

When an instrument goes for repair, runs on its station are refused until it is back:

```bash
python -m benchrun instrument edit PSU-0001 --status in-repair --notes "Output relay clicks" --actor "Operator B"
```

```bash
python -m benchrun run examples/regulator_quick.json --serial SN-1002 --station-name LINE-1
```

```text
Cannot continue:
  supply: PSU-0001 is in-repair, so it cannot be used
```

```bash
python -m benchrun instrument reactivate PSU-0001 --reason "Relay replaced" --actor "Operator B"
```

## Station files

`station import` reads the station files that `--station FILE` already uses, such as
[examples/station_example.json](../examples/station_example.json) or the file `sim --write-addresses`
writes. Each key under `addresses` becomes a role. An address the inventory already has, on an
instrument that is not retired, is used as it is; any other address gets a new inventory
instrument with the next free tag for its driver, such as `PSU-0002`, at the station's location.

A role named like a driver, as in the simulators' file, uses that driver. For a role named like a
plan instrument, give the plan with `--plan`, or the driver with `--driver ROLE=DRIVER`
(repeatable). `--dry-run` shows what would be made and changes nothing:

```bash
python -m benchrun station import examples/station_example.json --name BENCH-A --plan examples/regulator_line_load.json --location "Lab 2" --dry-run
```

```text
Station BENCH-A from examples/station_example.json:
role    asset tag    driver  address          inventory
------  -----------  ------  ---------------  --------------
supply  PSU-0002     psu     192.0.2.11:5025  new instrument
load    ELOAD-0001   eload   192.0.2.12:5025  new instrument
dmm     DMM-0002     dmm     192.0.2.13:5025  new instrument
scope   SCOPE-0001   scope   192.0.2.14:5025  new instrument
gen     SIGGEN-0001  siggen  192.0.2.15:5025  new instrument
Dry run: nothing was changed. It would create the station and add 5 instruments.
```

The simulators' own file reuses the two instruments added above and adds the other eight:

```bash
python -m benchrun station import sim_station.json --name SIM-BENCH --actor "Operator A"
```

```text
Station SIM-BENCH from sim_station.json:
role       asset tag       driver     address         inventory
---------  --------------  ---------  --------------  ------------------------
dmm        DMM-0001        dmm        127.0.0.1:5025  already in the inventory
psu        PSU-0001        psu        127.0.0.1:5026  already in the inventory
eload      ELOAD-0001      eload      127.0.0.1:5027  new instrument
scope      SCOPE-0001      scope      127.0.0.1:5028  new instrument
siggen     SIGGEN-0001     siggen     127.0.0.1:5029  new instrument
smu        SMU-0001        smu        127.0.0.1:5030  new instrument
counter    COUNTER-0001    counter    127.0.0.1:5031  new instrument
switch     SWITCH-0001     switch     127.0.0.1:5032  new instrument
lcr        LCR-0001        lcr        127.0.0.1:5033  new instrument
psu_multi  PSU-MULTI-0001  psu_multi  127.0.0.1:5034  new instrument
Created station SIM-BENCH with 10 roles, and added 8 instruments to the inventory.
```

An import is all or nothing. It is refused if the station already exists, if two roles share an
address, if a role's driver is unknown, or if the file disagrees with the inventory, such as an
address that belongs to an instrument with another driver.

`station export NAME --out FILE` writes a station file. Its `addresses` are what `--station FILE` reads,
so a station can still be used by a benchrun that has no database, or on another machine:

```bash
python -m benchrun station export LINE-1 --out line1_station.json
```

```bash
python -m benchrun probe examples/regulator_quick.json --station line1_station.json
```

The file also has a `station` section with the name, description, location and, for each role,
the asset tag, driver, timeout and expected pattern. `--station FILE` ignores it; `station import`
reads it, so a station exported from one database imports into another unchanged. `export` will not
replace a file without `--force`.

## Inventory files

`instrument export --out FILE` writes instruments to CSV or JSON, chosen by the file's name or by
`--format`, and takes the same filters as `list`. `instrument import FILE` reads the same formats:

```bash
python -m benchrun instrument export --out inventory.csv
```

```bash
python -m benchrun instrument import inventory.csv --dry-run
```

```text
0 would be added, 0 would change, 10 unchanged, 0 refused.
Dry run: nothing was changed.
```

A CSV file starts with a header naming its columns, from `asset_tag`, `driver`, `manufacturer`,
`model`, `serial_number`, `address`, `location`, `status` and `notes`. Only `asset_tag` is
required. A JSON file is `{"instruments": [{"asset_tag": "PSU-0001", "driver": "psu"}]}` with the
same keys. For each row:

- A tag the inventory does not have is added, and needs a driver.
- A tag it has is changed to the values given. A column or key left out keeps what is there, and so
  does an empty `driver` or `status` cell; an empty cell in any other column clears that field.
- The report says what was added, what changed from what, and what was left as it was.

An import is all or nothing too. If any row is refused, nothing is imported, and the report lists
every refused row with the reason: a tag given twice in the file (ignoring case), an unknown column
or key, a row with the wrong number of cells, a value that is not one line of text, an unknown
driver or status, a bad address, or two instruments in use ending up at one address. Files are at
most 4 MiB and 10,000 rows, in UTF-8 (or UTF-16 with a byte order mark); a JSON file may not repeat
a key or use `NaN`.

In a CSV export, text a spreadsheet would treat as a formula gets a leading apostrophe, as in the
run export. Import takes that apostrophe off again, so an exported file imports back unchanged.

## The audit log

Every change to an instrument or a station writes one entry, in the same transaction as the
change: when, by which `--actor`, the action, the subject (such as `instrument:PSU-0001` or
`station:LINE-1`) and what changed. An edit records each field as `[old, new]`.

```bash
python -m benchrun audit --subject instrument:PSU-0001
```

```text
entry  at (UTC)             action                 subject              actor
-----  -------------------  ---------------------  -------------------  ----------
7      2026-11-10 05:23:36  instrument.reactivate  instrument:PSU-0001  Operator B
    reason="Relay replaced", status=["in-repair", "active"]
6      2026-11-10 05:23:35  instrument.edit        instrument:PSU-0001  Operator B
    notes=["", "Output relay clicks"], status=["active", "in-repair"]
1      2026-11-10 05:23:31  instrument.add         instrument:PSU-0001  Operator A
    address="127.0.0.1:5026", asset_tag="PSU-0001", driver="psu", location="Lab 1", manufacturer="benchrun", model="SIM-PSU", notes="", serial_number="SIM0002", status="active"
```

| Option | What it selects |
|---|---|
| `--subject S` | One subject. Ending in `:`, as in `station:`, every subject of that kind |
| `--action A` | `instrument.add`, `instrument.edit`, `instrument.retire` or `instrument.reactivate`; `station.create`, `station.assign`, `station.unassign`, `station.rename`, `station.delete` or `station.import`. Products, lots, archive imports and `db mark-abandoned` name theirs the same way, such as `product.set-limit`, `lot.close`, `archive.import` and `run.mark-abandoned` |
| `--actor A` | Changes made by this actor; `--actor ""` finds those made with none |
| `--since`, `--until` | A UTC date or time, `YYYY-MM-DD` or `YYYY-MM-DDTHH:MM` |
| `--limit N` | The newest N, default 100 |
| `--json` | Everything, for scripts |

A rename is recorded under the new name, with the old one in its detail. Importing a station
records an `instrument.add` for each new instrument and one `station.import` for the station with all its roles.

The audit log shows who changed what through benchrun. Like the rest of the database, it is not
proof against someone editing the file directly.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Done, or nothing needed changing |
| 2 | A bad argument or file, something that does not exist, or a run that cannot use the station. Nothing was changed |
| 3 | `station check` found a problem, or the database could not be used |
