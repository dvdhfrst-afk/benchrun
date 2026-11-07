# Drivers and where their commands came from

**No driver has ever talked to a physical instrument.** Each has only met a simulator that was
written from the same reading of the same manual, so a misreading would pass every test here. Treat
the sources below as a head start for your own check, not as compatibility. Run `probe`, then try
your plan with nothing connected to the outputs, with `--scpi-log` on.

Every command a driver sends can be replaced from a plan with no code. To see them:

```bash
python -m benchrun drivers smu --commands
```

| Driver | Instrument type | Readings | Written from |
|---|---|---|---|
| `dmm` | Digital multimeter | DC and AC volts and amperes, 2-wire and 4-wire resistance, frequency | 34401A guide (partly) |
| `psu` | Single-output power supply | Volts, amperes, constant current flag | Common SCPI; **not checked against a manual** |
| `psu_multi` | Power supply with several outputs | Volts, amperes, constant current flag, per output | E36300 guide |
| `eload` | DC electronic load, constant current | Volts, amperes | DL3000 guide |
| `siggen` | Signal generator | None | Common SCPI; **not checked against a manual** |
| `scope` | Oscilloscope | Peak to peak, rms, mean, frequency, waveform-derived values | Common SCPI; **not checked against a manual** |
| `smu` | Source measure unit | Volts, amperes, at-limit flag | Series 2400 manual |
| `counter` | Frequency counter | Frequency | 53220A/53230A guide |
| `switch` | Relay switch or multiplexer | Channel state | 34970A/34972A reference |
| `lcr` | Inductance, capacitance and resistance (LCR) meter | Capacitance, inductance, impedance, first and second value | E4980A/AL guide |
| `scpi` | Anything | None; plans use `write` only | Not applicable |

## All drivers

| Command | Source |
|---|---|
| `*IDN?` `*RST` `*CLS` `*OPC?`, and `SYST:ERR?` with its `<number>,"<text>"` reply | SCPI-99, Version 1999.0, May 1999, Volume 1 sections 4.1.3.2 to 4.1.3.6, and the `SYSTem:ERRor[:NEXT]?` entry: https://www.ivifoundation.org/downloads/SCPI/scpi-99.pdf . Those common commands come from IEEE 488.2-1987, which was not read because it is not freely available. |
| Raw TCP on port 5025, each message ending in a newline | E36300 Series User's Guide, E36311-90001, Edition 5, June 2020, page 46: https://blogs.gwu.edu/ecelabs/files/2021/08/KeysightE36300SeriesUsersGuide.pdf |

All the Keysight pages were read from public mirrors at the addresses below, because the maker's own
site refused scripted downloads. The mirrors were not compared with the originals.

## `dmm`

HP 34401A Multimeter User's Guide, 34401-90004, February 1996:
https://web.mit.edu/6.101/www/reference/HP34401s.pdf

| Command | Page |
|---|---|
| `CONF:VOLT:DC`, `CONF:VOLT:AC`, `CONF:CURR:DC`, `CONF:CURR:AC`, `CONF:RES`, `CONF:FRES`, with range first and `DEF` for the default | 119 |
| `CONF:FREQ`; with no input the reading is 0 | 120 |
| `READ?` | 130 |
| Overload reads `9.90000000E+37` | 61 |
| Error form `-113,"Undefined header"` | 85 |

Not confirmed: that `DEF` means autorange on your meter, and the integration time commands
(`VOLT:DC:NPLC`, `CURR:DC:NPLC`), which were not looked up.

## `psu_multi`

E36300 Series Programming Guide, E36311-90008, Edition 3, October 2017:
https://xdevs.com/doc/HP_Agilent_Keysight/E36300A/E36311-90008_programming_guide.pdf

| Command | Page |
|---|---|
| `VOLT <v>,(@n)` | 89 |
| `CURR <a>,(@n)` | 44 |
| `OUTP ON,(@n)`, `OUTP? (@n)` answering 0 or 1 | 71 |
| `MEAS:VOLT? (@n)`, `MEAS:CURR? (@n)` | 69 |
| `STAT:QUES:INST:ISUM<n>:COND?`: 0 off, 1 constant current, 2 constant voltage, 3 hardware failure | 78 |
| Channel lists `(@2)` and `(@1:3)` | 17 |

Not confirmed: the guide's own text says that with output sequencing on, `OUTP?` gives the configured
state and not the actual one. The driver does not turn sequencing on and does not check for it.

## `eload`

DL3000 Series Programming Guide, RIGOL, February 2017, PGJ01103-1110:
https://docs.ampnuts.ru/my_docs/Rigol/DL3021/doc/DL3000_ProgrammingGuide.pdf

| Command | Page |
|---|---|
| Constant current level `[SOUR]:CURR[:LEV][:IMM]` | 2-21 |
| Function `[SOUR]:FUNC` | 2-19 |
| Input `[SOUR]:INP[:STAT]`, query answering 0 or 1 | 2-19 |
| `MEAS:VOLT?` | 2-13 |
| `MEAS:CURR?` | 2-14 |

The driver offers constant current only, because that is all that was confirmed.

## `smu`

Series 2400 SourceMeter User's Manual, 2400S-900-01 Rev. K, September 2011:
https://download.tek.com/manual/2400S-900-01_K-Sep2011_User.pdf

| Command | Page |
|---|---|
| `:SOUR:FUNC VOLT` or `CURR` | 18-74 |
| `:SOUR:VOLT:LEV`, `:SOUR:CURR:LEV` | 18-78 |
| `:SENS:CURR:PROT`, `:SENS:VOLT:PROT` | 18-68 |
| `:SENS:CURR:PROT:TRIP?`, `:SENS:VOLT:PROT:TRIP?`, 1 means at the limit | 18-69 |
| `:SENS:FUNC "CURR"` | 18-59 |
| `:FORM:ELEM CURR` | 18-49 |
| `:OUTP ON` or `OFF` | 18-56 |
| `:READ?`, which needs the output on | 17-5 |

Not confirmed: one selected element replying as a single value is inferred from the manual's statement
that unselected elements are left out; no such example is printed. The reply text of `:OUTP?` is not
shown either, so both `0`/`1` and `OFF`/`ON` are accepted.

## `counter`

53220A/53230A User's Guide, 53220-90001, Edition 3, September 2017:
https://ecse.rpi.edu/courses/static/ECSE-4770/Keysight_Equipment_User_Manuals/53220-90001.pdf

| Command | Page |
|---|---|
| `CONF:FREQ <expected>,<resolution>,(@<channel>)` | 94 |
| `SENS:FREQ:GATE:TIME` | 172 |
| `READ?` | 248 |

Not confirmed: the guide prints `CONF:FREQ 20E6, 0.1, (@1)`. The driver sends `DEF` for values a plan
leaves out, which the syntax line allows but no example shows. If your counter objects, give both
`expected_hz` and `resolution_hz` in the plan. The separate programmer's reference was not obtained.

## `switch`

34970A/34972A Command Reference, no part number printed:
https://documentation.help/Keysight-34970A-34972A/documentation.pdf , and for channel list syntax,
34970A/34972A User's Guide, 34972-90001, Edition 6, page 103: https://www.farnell.com/datasheets/2936609.pdf

| Command | Where in the reference |
|---|---|
| `ROUT:CLOS (@ch)`, `ROUT:CLOS? (@ch)` answering 1 for closed | `ROUTe:CLOSe` |
| `ROUT:CLOS:EXCL (@ch)` | `ROUTe:CLOSe:EXCLusive` |
| `ROUT:OPEN (@ch)` | `ROUTe:OPEN` |

Not confirmed, and worth knowing: this reference has no command to open everything, so the driver
opens each channel it closed, and `open_all` is empty until a plan sets it. RF (radio frequency)
multiplexers such as the 34905A do not accept `ROUT:OPEN`, per the same reference, so the safe state
will report a problem on those. A channel in a scan list makes the close commands fail.

## `lcr`

E4980A/AL User's Guide, E4980-90230, Edition 16, October 2019:
https://www.cmc.ca/wp-content/uploads/2019/07/E4980A-User-Guide.pdf

| Command | Page |
|---|---|
| `FUNC:IMP <type>` | 353 |
| `FREQ` | 349 |
| `VOLT` | 379 |
| `TRIG:SOUR BUS`, `TRIG:IMM` | 378 |
| `INIT` | 357 |
| `FETC?` | 346 |
| Reply `<A>,<B>,<status>[,<bin>]`, with status -1 no data, 0 normal, +1 overload, +3 source overload, +4 level control cannot regulate | 264 |

Not confirmed: the three-field reply with the comparator off is built from the text; no clean example
is printed. `measure_primary` and `measure_secondary` take separate triggers, so they are two
measurements and not one pair. The level range could not be read from the copy used, so the driver
does not check it.

## `psu`, `siggen` and `scope`

These use common SCPI forms and were written before the sources above were collected. None was checked
against a particular manual. In particular, `psu.current_limited` reads bit 10 of `STAT:OPER:COND?`,
which is a default that must be checked for your supply, since the wrong bit reads 0 and proves
nothing. Expect oscilloscope commands to need replacing: they differ most between makers.

## Adding a driver

See [CONTRIBUTING.md](../CONTRIBUTING.md).
