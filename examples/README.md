# Examples

Each plan passes with `run --sim`, which starts private simulators for just that run:

```bash
python -m benchrun run examples/resistor_iv_curve.json --serial EX-1 --sim
```

| Plan | Shows | Drivers |
|---|---|---|
| [regulator_quick.json](regulator_quick.json) | The smallest useful plan | `psu`, `dmm` |
| [regulator_line_load.json](regulator_line_load.json) | Sweeps, summaries, calculated values, a scope and a generator | `psu`, `eload`, `dmm`, `scope`, `siggen` |
| [resistor_iv_curve.json](resistor_iv_curve.json) | Force volts, measure amperes, work out ohms | `smu` |
| [capacitor_lcr.json](capacitor_lcr.json) | Capacitance and dissipation at two frequencies, with units in uF | `lcr` |
| [generator_frequency_accuracy.json](generator_frequency_accuracy.json) | Limits that follow a sweep | `siggen`, `counter` |
| [scan_through_switch.json](scan_through_switch.json) | One meter, two points | `psu`, `dmm`, `switch` |
| [three_rails.json](three_rails.json) | A sweep over outputs, using a variable as the channel | `psu_multi` |
| [fixture_with_operator.json](fixture_with_operator.json) | Stopping for the operator, and checking for a short before power | `psu`, `dmm` |
| [station_example.json](station_example.json) | Not a plan: a station file giving real addresses, for `--station` | |

`fixture_with_operator.json` waits for Enter. Add `--auto-confirm` to go on without waiting:

```bash
python -m benchrun run examples/fixture_with_operator.json --serial EX-2 --sim --auto-confirm
```

The limits suit the simulated parts. Change them for yours.

## Sample output

[sample_output](sample_output) holds every report format from one real run of `regulator_quick.json`
on the simulators, with a failing simulated unit so that there is something to see, and that run
exported as JSON Lines and as an Excel workbook:

```bash
python -m benchrun run examples/regulator_quick.json --serial SN-1002 --operator "Operator A" --sim --dut high-output --seed 1 --db sample.sqlite --report-dir examples/sample_output --report-format every
python -m benchrun export --db sample.sqlite --format jsonl --out examples/sample_output/export.jsonl
python -m benchrun export --db sample.sqlite --format xlsx --out examples/sample_output/export.xlsx
```

The HTML files open in any browser. Each is one file and loads nothing else.
[docs/reports.md](../docs/reports.md) describes every format.
