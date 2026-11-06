"""Works out expected verdicts with plain arithmetic. Imports nothing from benchrun.

Run: python tests/fixtures/work_out_expected.py tests/fixtures/verdict_expected.json
"""
import json
import math
import sys

GOOD = dict(line=0.001, load=0.015, d0=0.35, dr=0.25, iq=0.003, psrr=70.0)


def vout(p, vin, i):
    if p.get("dead"):
        return 0.0
    return min(5.0 + p["line"] * (vin - 12.0) - p["load"] * i, vin - (p["d0"] + p["dr"] * i))


def verdict(v, lo, hi):
    if v is None:
        return "fail"
    if lo is None and hi is None:
        return "info"
    if lo is not None and v < lo:
        return "fail"
    if hi is not None and v > hi:
        return "fail"
    return "pass"


def row(step, name, point, v, lo, hi, units, tol=2e-8, worked="", forced=None):
    return dict(step=step, name=name, point=point, value=v, low=lo, high=hi, units=units,
                tolerance=tol, verdict=forced or verdict(v, lo, hi), worked=worked)


def case_rows(p):
    rows = []
    vs = []
    for vin in (6.0, 12.0, 18.0):
        v = vout(p, vin, 0.5)
        vs.append(v)
        worked = "dead unit" if p.get("dead") else (
            f"min(5 + {p['line']}*({vin}-12) - {p['load']}*0.5, {vin} - {p['d0']} - {p['dr']}*0.5)")
        rows.append(row("line", "vout", {"vin": vin}, v, 4.95, 5.05, "V", worked=worked))
    span = (max(vs) - min(vs)) * 1000.0
    rows.append(row("line", "line_reg", {}, span, None, 25.0, "mV", worked="(max(vout)-min(vout))*1000"))
    vl = []
    for i in (0.0, 0.5, 1.0):
        v = vout(p, 12.0, i)
        vl.append(v)
        iin = 0.0 if p.get("dead") else (i + p["iq"]) * 1000.0
        iout = 0.0 if p.get("dead") else i
        rows.append(row("load", "vout", {"iload": i}, v, 4.95, 5.05, "V", worked=f"5 - {p.get('load')}*{i}"))
        rows.append(row("load", "iin", {"iload": i}, iin, 1.0, 1010.0, "mA", worked=f"({i} + {p.get('iq')})*1000"))
        rows.append(row("load", "iout", {"iload": i}, iout, None, None, "A", worked="load setting"))
        pd = 12.0 * iin / 1000.0 - v * iout
        rows.append(row("load", "pdiss", {"iload": i}, pd, None, 8.0, "W", worked="12*iin/1000 - vout*iout"))
    rows.append(row("load", "load_reg", {}, (vl[0] - vl[-1]) * 1000.0, None, 20.0, "mV",
                    worked="(first(vout)-last(vout))*1000"))
    v0 = vout(p, 12.0, 0.0)
    rows.append(row("exact_limit", "vout", {}, v0, None, 5.0, "V", worked="exactly 5 V on an inclusive high limit"))
    v = vout(p, 5.6, 1.0)
    rows.append(row("headroom", "vout", {}, v, 4.9, None, "V",
                    worked=f"min(5 + {p.get('line')}*(5.6-12) - {p.get('load')}, 5.6 - {p.get('d0')} - {p.get('dr')})"))
    rin = 1.0 / (2 * math.sqrt(2)) * 1000.0
    for f in (100.0, 1000.0, 10000.0):
        rows.append(row("ripple", "ripple_in", {"freq": f}, rin, None, None, "mVrms", tol=5.0,
                        worked="1 Vpp sine is 353.55 mVrms, within 8-bit quantization"))
        if p.get("dead"):
            rows.append(row("ripple", "ripple_out", {"freq": f}, 0.0, None, None, "mVrms", tol=1e-6, worked="no output"))
            rows.append(row("ripple", "psrr", {"freq": f}, None, 45.0, None, "dB", tol=0,
                            worked="division by zero: no answer, so the unit fails"))
            continue
        db = p["psrr"] - 10.0 * math.log10(1.0 + (f / 1000.0) ** 2)
        vpp_out = 10.0 ** (-db / 20.0)
        if vpp_out / 2.0 > 0.004:
            rows.append(row("ripple", "ripple_out", {"freq": f}, None, None, None, "mVrms", tol=0, forced="fail",
                            worked=f"{vpp_out*1000:.1f} mVpp exceeds the 8 mV screen: overload"))
            rows.append(row("ripple", "psrr", {"freq": f}, None, 45.0, None, "dB", tol=0,
                            worked="its input was an overload: no answer"))
        else:
            rows.append(row("ripple", "ripple_out", {"freq": f}, vpp_out / (2 * math.sqrt(2)) * 1000.0, None, None,
                            "mVrms", tol=0.02, worked=f"rejection {db:.2f} dB, within quantization"))
            rows.append(row("ripple", "psrr", {"freq": f}, db, 45.0, None, "dB", tol=0.5,
                            worked=f"{p['psrr']} - 10*log10(1+({f}/1000)^2), within quantization"))
    return rows


def overall(rows):
    return "fail" if any(r["verdict"] == "fail" for r in rows) else "pass"


cases = []
for label, dut, p in (
    ("good unit passes", "good", GOOD),
    ("poor line regulation fails only the span", "poor-line", dict(GOOD, line=0.005)),
    ("high dropout fails wherever input headroom is short", "high-dropout", dict(GOOD, d0=1.0)),
    ("poor ripple rejection fails, with an overload", "poor-psrr", dict(GOOD, psrr=45.0)),
    ("dead unit fails", "dead", dict(GOOD, dead=True)),
):
    rows = case_rows(p)
    cases.append(dict(label=label, dut=dut, faults=[], status=overall(rows), measurements=rows))

good = case_rows(GOOD)

# The multimeter gives 4 good readings (3 in line, 1 in load), then its connection drops.
kept, seen, hit = [], 0, None
for r in good:
    if r["name"] == "vout":
        if seen == 4:
            hit = dict(r, value=None, verdict="error", tolerance=0, worked="5th multimeter reading: connection dropped")
            break
        seen += 1
    kept.append(r)
cases.append(dict(label="multimeter connection lost aborts the run", dut="good", faults=["dmm:drop:4"],
                  status="aborted", reason_contains="dmm", skipped_steps=["exact_limit", "headroom", "ripple"],
                  failed_step="load", measurements=kept + [hit]))

# The supply gives 2 good readings, then reports a device error alongside the 3rd.
kept, seen, hit = [], 0, None
for r in good:
    if r["name"] == "iin":
        if seen == 2:
            hit = dict(r, value=None, verdict="error", tolerance=0, worked="3rd supply reading: error queue holds -300")
            break
        seen += 1
    kept.append(r)
cases.append(dict(label="instrument error aborts the run", dut="good", faults=["psu:error:2"],
                  status="aborted", reason_contains="-300", skipped_steps=["exact_limit", "headroom", "ripple"],
                  failed_step="load", measurements=kept + [hit]))

with open(sys.argv[1], "w", encoding="utf-8", newline="\n") as handle:
    json.dump({
        "about": "Expected results worked out from the equations in docs/simulators.md, separately from the "
                 "runner and simulator code. Each 'worked' field shows the arithmetic.",
        "cases": cases,
    }, handle, indent=1)
    handle.write("\n")
for c in cases:
    print(c["dut"], c["faults"], c["status"], len(c["measurements"]),
          [(r["step"], r["name"]) for r in c["measurements"] if r["verdict"] in ("fail", "error")][:6])
