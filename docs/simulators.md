# Simulators

`python -m benchrun sim` serves five instruments on 127.0.0.1, wired to one simulated linear
regulator. The model is simple enough to work out by hand on purpose; that is how
`tests/fixtures/verdict_expected.json` was produced.

## Wiring

```text
supply (+ generator ripple) --> VIN  [regulator]  VOUT --> electronic load
                                                       --> multimeter
oscilloscope CH1 = VOUT, CH2 = VIN
```

| Instrument | Default port | Identity |
|---|---|---|
| Multimeter (`dmm`) | 5025 | `benchrun,SIM-DMM,SIM0001,1.0` |
| Power supply (`psu`) | 5026 | `benchrun,SIM-PSU,SIM0002,1.0` |
| Electronic load (`eload`) | 5027 | `benchrun,SIM-ELOAD,SIM0003,1.0` |
| Oscilloscope (`scope`) | 5028 | `benchrun,SIM-SCOPE,SIM0005,1.0` |
| Signal generator (`siggen`) | 5029 | `benchrun,SIM-SIGGEN,SIM0004,1.0` |

## Regulator equations

With the supply on, `vin` is the supply setting (plus the generator offset when its output is on),
and `i` is the load setting when the load input is on, otherwise 0:

```text
regulated = vnom + line_reg * (vin - 12) - load_reg * i
ceiling   = vin - (dropout0 + dropout_r * i)
vout      = min(regulated, ceiling)
iin       = i + iq
```

| Parameter | `good` value | Meaning |
|---|---|---|
| `vnom` | 5.0 V | Output at 12 V input, no load |
| `line_reg` | 0.001 V/V | Output change per input volt |
| `load_reg` | 0.015 V/A | Output drop per load ampere |
| `dropout0` | 0.35 V | Dropout voltage at no load |
| `dropout_r` | 0.25 V/A | Added dropout per load ampere |
| `iq` | 3 mA | Quiescent input current |
| `ilimit` | 1.5 A | Output current limit |
| `psrr_db` | 70 dB | Power supply rejection ratio at low frequency |
| `psrr_corner_hz` | 1000 Hz | Rejection falls 20 dB per decade above this |

Special cases:

- Below 2.5 V input the regulator is off: `vout`, `iin` and load current are 0.
- A load demand above `ilimit` folds the output back to 0 V, with `iout = ilimit`.
- If `iin` exceeds the supply's current limit, the supply reports constant current, its rail
  collapses to at most 0.2 V, `vout` and load current are 0, and it reports `iin` equal to its limit.

## Ripple

The generator's output couples onto VIN. With amplitude `vpp` at frequency `f`:

```text
psrr(f)    = psrr_db - 10 * log10(1 + (f / psrr_corner_hz)^2)
ripple_out = vpp * 10^(-psrr(f) / 20)
```

When `ceiling < regulated` the regulator has no headroom and ripple passes through unchanged.

## Simulated units

| `--dut` | Change from `good` | Expected to fail in the example plan |
|---|---|---|
| `high-output` | `vnom` 5.25 V | Output voltage limits |
| `poor-line` | `line_reg` 0.005 V/V | Line regulation |
| `poor-load` | `load_reg` 0.05 V/A | Load regulation |
| `high-dropout` | `dropout0` 1.0 V | Low headroom |
| `high-iq` | `iq` 20 mA | Quiescent current |
| `poor-psrr` | `psrr_db` 45 dB | Ripple rejection, with an overload at 10 kHz |
| `dead` | No output at all | Almost everything |

## Noise and repeatability

Noise is Gaussian, from one seeded generator per instrument, so the same seed with the same command
sequence on a freshly started bench gives identical readings. `--noise` scales it; 0 turns it off.

| Reading | Noise (rms) |
|---|---|
| Multimeter DC volts | 30 uV, divided by the square root of NPLC (number of power line cycles) |
| Multimeter DC amperes | 20 uA, divided by the square root of NPLC |
| Supply volts, amperes | 500 uV, 50 uA |
| Load volts, amperes | 500 uV, 100 uA |
| Oscilloscope trace | 0.01 division |

## Instrument behavior that matters to a plan

- **Multimeter:** with a fixed range, a signal above 1.2 times the range reads as overload (9.9E37).
  `CONF` resets integration time to 1 NPLC.
- **Oscilloscope:** measurements come from a simulated 1000-point, 8-bit record covering 10
  horizontal and 8 vertical divisions. A trace off screen reads as overload, so set AC coupling to
  look at ripple on a DC rail. `MEAS:FREQ?` needs at least two cycles on screen, otherwise it reads
  as overload.
- **Supply limits:** 0 to 30 V, 0 to 5 A. **Load limit:** 0 to 5 A. Beyond these, the setting is
  rejected with error -222 and the old value stays.
- **Errors:** queue of 20, oldest first, read with `SYST:ERR?`. A query with a bad header gets no
  response and queues an error, so a controller finds out by timing out and reading the queue.

## SCPI commands

Long forms are shown. Shortened forms and optional nodes follow the usual rules: in
`[SOURce]:VOLTage[:LEVel]`, the short form is the capitals and bracketed nodes may be left out.

| Instrument | Commands |
|---|---|
| All | `*IDN?` `*RST` `*CLS` `*OPC` `*OPC?` `*WAI` `*TST?` `*ESR?` `*ESE` `*ESE?` `*SRE` `*SRE?` `*STB?` `SYSTem:ERRor[:NEXT]?` `SYSTem:VERSion?` |
| Multimeter | `CONFigure[:VOLTage][:DC]` `CONFigure:CURRent[:DC]` `CONFigure:VOLTage:AC` `CONFigure?` `MEASure[:VOLTage][:DC]?` `MEASure:CURRent[:DC]?` `MEASure:VOLTage:AC?` `READ?` `[SENSe]:VOLTage[:DC]:NPLCycles` `[SENSe]:CURRent[:DC]:NPLCycles` `[SENSe]:VOLTage[:DC]:RANGe[:UPPer]` `[SENSe]:CURRent[:DC]:RANGe[:UPPer]` |
| Supply | `[SOURce]:VOLTage[:LEVel][:IMMediate][:AMPLitude]` `[SOURce]:CURRent[:LEVel][:IMMediate][:AMPLitude]` `OUTPut[:STATe]` `MEASure[:SCALar]:VOLTage[:DC]?` `MEASure[:SCALar]:CURRent[:DC]?` `STATus:OPERation:CONDition?` (bit 8 constant voltage, bit 10 constant current) |
| Load | `[SOURce]:CURRent[:LEVel][:IMMediate][:AMPLitude]` `INPut[:STATe]` `MEASure[:SCALar]:VOLTage[:DC]?` `MEASure[:SCALar]:CURRent[:DC]?` |
| Generator | `[SOURce]:FUNCtion[:SHAPe]` (`SINusoid`, `SQUare`, `TRIangle`) `[SOURce]:FREQuency[:CW]` `[SOURce]:VOLTage[:LEVel][:IMMediate][:AMPLitude]` `[SOURce]:VOLTage[:LEVel][:IMMediate]:OFFSet` `OUTPut[:STATe]` |
| Oscilloscope | `CHANnel<n>:SCALe` `CHANnel<n>:OFFSet` `CHANnel<n>:COUPling` `TIMebase[:MAIN]:SCALe` `MEASure:VPP?` `MEASure:VRMS?` `MEASure:VAVerage?` `MEASure:FREQuency?` `WAVeform:SOURce` `WAVeform:XINCrement?` `WAVeform:DATA?` |

## Fault injection

`--fault instrument:kind[:after]` lets `after` readings through (default 0), then:

| Kind | Effect on the next and later readings |
|---|---|
| `drop` | Closes the connection and refuses new sessions. |
| `hang` | Stops answering anything. |
| `error` | Answers normally, and queues error -300. |
| `garbage` | Answers `#BAD#`. |
| `overload` | Answers 9.9E37. |
