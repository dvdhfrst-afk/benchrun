"""Whole runs through the real command line, against instruments in other processes.

Nothing here is in-process: benchrun runs as ``python -m benchrun``, and each fake
instrument is its own process behind a real TCP socket. What the instrument
received is read back from its journal after benchrun has exited.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import shutil
import time
import unittest
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).parent.parent
FAKE = ROOT / "tests" / "fake_instrument.py"
TIMEOUT_S = 0.4


def small_plan(settle_s: float = 0.0, read_supply: bool = False, points: int = 6) -> Dict:
    measures = [{"name": "vout", "read": "dmm.measure_dc_voltage", "units": "V", "low": 4.9, "high": 5.1}]
    if read_supply:
        measures.append({"name": "vin_read", "read": "supply.measure_voltage", "units": "V", "low": 4.9, "high": 5.1})
    return {
        "name": "sockets",
        "version": "1",
        "instruments": {
            "supply": {"driver": "psu", "timeout_s": TIMEOUT_S},
            "dmm": {"driver": "dmm", "timeout_s": TIMEOUT_S},
        },
        "setup": [{"do": "supply.output_on"}],
        "steps": [
            {
                "name": "sweep",
                "sweep": {"var": "vin", "start": 1, "stop": points, "step": 1},
                "apply": [{"do": "supply.set_voltage", "volts": "=vin"}],
                "settle_s": settle_s,
                "measure": measures,
            },
            {"name": "after", "measure": [{"name": "again", "read": "dmm.measure_dc_voltage", "low": 4.9, "high": 5.1}]},
        ],
    }


class SocketCase(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp(prefix="benchrun-sockets-"))
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.db = self.folder / "results.sqlite"

    def fake(self, name: str, fault: Optional[str] = None, idn: str = "Example,FAKE-1,0001,1.0") -> Tuple[str, Path]:
        journal = self.folder / f"{name}.journal"
        command = [sys.executable, "-I", str(FAKE), "--journal", str(journal), "--idn", idn]
        if fault:
            command += ["--fault", fault]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, text=True)
        self.addCleanup(process.stdout.close)
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        port = int(process.stdout.readline().split()[1])
        return f"127.0.0.1:{port}", journal

    def write_plan(self, plan: Dict) -> Path:
        path = self.folder / "plan.json"
        path.write_text(json.dumps(plan), encoding="utf-8")
        return path

    def command(self, plan: Dict, addresses: Dict[str, str], *extra: str) -> List[str]:
        args = [sys.executable, "-m", "benchrun", "run", str(self.write_plan(plan)), "--serial", "SN-1", "--db", str(self.db)]
        for name, address in addresses.items():
            args += ["--address", f"{name}={address}"]
        return args + list(extra)

    def run_benchrun(self, plan: Dict, addresses: Dict[str, str], *extra: str) -> subprocess.CompletedProcess:
        done = subprocess.run(
            self.command(plan, addresses, *extra), cwd=ROOT, capture_output=True, text=True, timeout=120
        )
        self.assertNotIn("Traceback", done.stderr)
        self.assertNotIn("Traceback", done.stdout)
        return done

    def record(self) -> sqlite3.Row:
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        return conn.execute("SELECT * FROM runs").fetchone()

    def measurements(self) -> int:
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        return conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0]

    def steps(self) -> Dict[str, str]:
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        return dict(conn.execute("SELECT name, status FROM steps").fetchall())

    def journal(self, path: Path, deadline_s: float = 2.0) -> List[str]:
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def assertOutputOff(self, path: Path) -> None:
        lines = self.journal(path)
        states = [line for line in lines if line.startswith("OUTPUT ")]
        self.assertEqual(states[-1], "OUTPUT off", lines[-12:])
        self.assertIn("RX OUTP?", lines, "benchrun must ask whether it really went off")

    def assertReadableReports(self, *extra: str) -> None:
        folder = self.folder / "reports"
        done = subprocess.run(
            [sys.executable, "-m", "benchrun", "report", "--latest", "--db", str(self.db), "--out", str(folder), *extra],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(len(list(folder.glob("run-*"))), 3)


class GoodRun(SocketCase):
    def test_pass_and_outputs_off(self):
        supply, supply_log = self.fake("supply")
        dmm, _ = self.fake("dmm")
        done = self.run_benchrun(small_plan(), {"supply": supply, "dmm": dmm})
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.record()["status"], "pass")
        self.assertEqual(self.measurements(), 7)
        self.assertOutputOff(supply_log)


class FaultyInstrument(SocketCase):
    def assertAbortedSafely(self, done, reason_part: str, supply_log: Path) -> sqlite3.Row:
        self.assertEqual(done.returncode, 3, done.stdout + done.stderr)
        row = self.record()
        self.assertEqual(row["status"], "aborted")
        self.assertIn(reason_part, row["reason"])
        self.assertIsNotNone(row["finished_at"])
        self.assertOutputOff(supply_log)
        self.assertReadableReports()
        return row

    def run_with_dmm_fault(self, fault: str) -> Tuple[subprocess.CompletedProcess, Path]:
        supply, supply_log = self.fake("supply")
        dmm, _ = self.fake("dmm", fault)
        return self.run_benchrun(small_plan(), {"supply": supply, "dmm": dmm}), supply_log

    def test_reading_times_out(self):
        done, log = self.run_with_dmm_fault("silent_once:2")
        self.assertAbortedSafely(done, "no response", log)
        self.assertEqual(self.measurements(), 3, "the readings before the fault stay, plus the failed one")
        self.assertEqual(self.steps()["sweep"], "error")
        self.assertEqual(self.steps()["after"], "skipped")

    def test_timeout_shows_the_instruments_own_error(self):
        done, log = self.run_with_dmm_fault("silent_once:0")
        row = self.assertAbortedSafely(done, "no response", log)
        self.assertIn("Query UNTERMINATED", row["reason"])

    def test_connection_drops_mid_run(self):
        done, log = self.run_with_dmm_fault("drop:3")
        self.assertAbortedSafely(done, "closed by the instrument", log)
        self.assertEqual(self.measurements(), 4)

    def test_garbled_reply(self):
        done, log = self.run_with_dmm_fault("garbled:2")
        self.assertAbortedSafely(done, "non-numeric", log)

    def test_half_a_reply_then_silence(self):
        done, log = self.run_with_dmm_fault("partial:1")
        self.assertAbortedSafely(done, "no response", log)

    def test_late_reply_never_reads_as_the_next_answer(self):
        done, log = self.run_with_dmm_fault("late:1")
        self.assertAbortedSafely(done, "no response", log)

    def test_carriage_return_before_the_line_feed_is_understood(self):
        supply, _ = self.fake("supply")
        dmm, _ = self.fake("dmm", "crlf:0")
        done = self.run_benchrun(small_plan(), {"supply": supply, "dmm": dmm})
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_reply_with_a_unit_attached_is_not_guessed_at(self):
        done, log = self.run_with_dmm_fault("with_unit:1")
        self.assertAbortedSafely(done, "non-numeric", log)

    def test_reply_pattern_reads_a_unit_attached_reply(self):
        supply, _ = self.fake("supply")
        dmm, _ = self.fake("dmm", "with_unit:1")
        plan = small_plan()
        plan["instruments"]["dmm"]["reply_pattern"] = r"^([-+0-9.eE]+)V?$"
        done = self.run_benchrun(plan, {"supply": supply, "dmm": dmm})
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_endless_data_cannot_hold_up_the_run(self):
        started = time.monotonic()
        done, log = self.run_with_dmm_fault("flood:1")
        self.assertAbortedSafely(done, "", log)
        self.assertLess(time.monotonic() - started, 60)

    def test_instrument_process_dies(self):
        supply, supply_log = self.fake("supply")
        dmm, dmm_log = self.fake("dmm", "die:2")
        done = self.run_benchrun(small_plan(), {"supply": supply, "dmm": dmm})
        self.assertAbortedSafely(done, "", supply_log)
        self.assertIn("DIED", self.journal(dmm_log))


class SupplyItselfFails(SocketCase):
    """The hard cases: the instrument that has to go safe is the one in trouble."""

    def test_dropped_connection_reconnects_only_to_switch_off(self):
        supply, supply_log = self.fake("supply", "drop:1")
        dmm, _ = self.fake("dmm")
        done = self.run_benchrun(small_plan(read_supply=True), {"supply": supply, "dmm": dmm})
        self.assertEqual(done.returncode, 3, done.stdout)
        self.assertEqual(self.record()["status"], "aborted")
        lines = self.journal(supply_log)
        self.assertEqual(lines.count("CONNECT"), 2, lines)
        after_reconnect = lines[len(lines) - 1 - lines[::-1].index("CONNECT") :]
        self.assertIn("RX OUTP OFF", after_reconnect)
        self.assertNotIn("RX *RST", after_reconnect, "the second session exists only to switch off")
        self.assertOutputOff(supply_log)
        self.assertNotIn("WARNING", done.stdout)

    def test_supply_process_dies_and_the_operator_is_warned(self):
        supply, _ = self.fake("supply", "die:1")
        dmm, _ = self.fake("dmm")
        done = self.run_benchrun(small_plan(read_supply=True), {"supply": supply, "dmm": dmm})
        self.assertEqual(done.returncode, 3)
        self.assertIn("WARNING: could not confirm these are off: supply", done.stdout)
        row = self.record()
        self.assertEqual(row["status"], "aborted")
        self.assertIsNotNone(row["finished_at"])
        self.assertReadableReports()

    def test_hung_supply_warns_and_the_run_still_ends(self):
        supply, _ = self.fake("supply", "hang:1")
        dmm, _ = self.fake("dmm")
        done = self.run_benchrun(small_plan(read_supply=True), {"supply": supply, "dmm": dmm})
        self.assertEqual(done.returncode, 3)
        self.assertIn("WARNING: could not confirm these are off: supply", done.stdout)

    def test_output_that_stays_on_is_not_a_pass(self):
        supply, _ = self.fake("supply", "stuck_on")
        dmm, _ = self.fake("dmm")
        done = self.run_benchrun(small_plan(), {"supply": supply, "dmm": dmm})
        self.assertEqual(done.returncode, 3, done.stdout)
        self.assertEqual(self.record()["status"], "aborted")
        self.assertIn("still reads on", self.record()["reason"])
        self.assertIn("WARNING: could not confirm these are off: supply", done.stdout)

    def test_supply_refusing_to_switch_off_is_not_a_pass(self):
        supply, _ = self.fake("supply", "refuse_off")
        dmm, _ = self.fake("dmm")
        done = self.run_benchrun(small_plan(), {"supply": supply, "dmm": dmm})
        self.assertEqual(done.returncode, 3)
        self.assertIn("Settings conflict", self.record()["reason"])


class WrongInstrument(SocketCase):
    def test_wrong_identity_is_never_commanded(self):
        supply, supply_log = self.fake("supply", idn="Someone,OTHER-9,77,1.0")
        dmm, dmm_log = self.fake("dmm")
        plan = small_plan()
        plan["instruments"]["supply"]["expect_idn"] = "FAKE-1"
        done = self.run_benchrun(plan, {"supply": supply, "dmm": dmm})
        self.assertEqual(done.returncode, 3)
        row = self.record()
        self.assertIn("does not match", row["reason"])
        received = [line for line in self.journal(supply_log) if line.startswith("RX ")]
        self.assertEqual(received, ["RX *IDN?"], "a wrong instrument gets no reset and no switch-off")
        self.assertEqual(self.measurements(), 0)
        self.assertFalse([line for line in self.journal(dmm_log) if line == "RX READ?"])

    def test_wrong_identity_is_recorded(self):
        supply, _ = self.fake("supply", idn="Someone,OTHER-9,77,1.0")
        dmm, _ = self.fake("dmm")
        plan = small_plan()
        plan["instruments"]["supply"]["expect_idn"] = "FAKE-1"
        self.run_benchrun(plan, {"supply": supply, "dmm": dmm})
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        idns = dict(conn.execute("SELECT name, idn FROM run_instruments").fetchall())
        self.assertEqual(idns["supply"], "Someone,OTHER-9,77,1.0")


class NetworkDown(SocketCase):
    def closed_port(self) -> str:
        import socket

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return f"127.0.0.1:{probe.getsockname()[1]}"

    def test_nothing_listening(self):
        dmm, dmm_log = self.fake("dmm")
        done = self.run_benchrun(small_plan(), {"supply": self.closed_port(), "dmm": dmm})
        self.assertEqual(done.returncode, 3)
        row = self.record()
        self.assertEqual(row["status"], "aborted")
        self.assertIn("cannot connect", row["reason"])
        self.assertIn("127.0.0.1", row["reason"])
        self.assertFalse([line for line in self.journal(dmm_log) if line == "RX READ?"])
        self.assertReadableReports()

    def test_probe_names_what_is_down_and_exits_3(self):
        dmm, _ = self.fake("dmm")
        args = [sys.executable, "-m", "benchrun", "probe", str(self.write_plan(small_plan()))]
        args += ["--address", f"supply={self.closed_port()}", "--address", f"dmm={dmm}"]
        done = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 3)
        self.assertIn("cannot connect", done.stdout)
        self.assertIn("FAKE-1", done.stdout)

    def test_unresolvable_host_is_plain(self):
        done = self.run_benchrun(small_plan(), {"supply": "no-such-host.invalid:5025", "dmm": self.closed_port()})
        self.assertEqual(done.returncode, 3)
        self.assertIn("cannot connect", self.record()["reason"])


class StoppedFromOutside(SocketCase):
    def start_slow_run(self) -> Tuple[subprocess.Popen, Path]:
        supply, supply_log = self.fake("supply")
        dmm, _ = self.fake("dmm")
        flags = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}
        process = subprocess.Popen(
            self.command(small_plan(settle_s=0.3, points=200), {"supply": supply, "dmm": dmm}),
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **flags,
        )
        self.addCleanup(process.kill)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and "OUTPUT on" not in self.journal(supply_log):
            time.sleep(0.05)
        time.sleep(0.7)
        return process, supply_log

    def test_stop_request_switches_off_and_records(self):
        process, supply_log = self.start_slow_run()
        process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)
        out, err = process.communicate(timeout=60)
        self.assertNotIn("Traceback", err)
        self.assertEqual(process.returncode, 3, out + err)
        self.assertEqual(self.record()["status"], "aborted")
        self.assertIn("interrupted", self.record()["reason"])
        self.assertOutputOff(supply_log)
        self.assertGreater(self.measurements(), 0)

    def test_hard_kill_leaves_a_readable_partial_record(self):
        process, _ = self.start_slow_run()
        process.kill()
        process.communicate(timeout=60)
        row = self.record()
        self.assertEqual(row["status"], "running")
        self.assertIsNone(row["finished_at"])
        self.assertGreater(self.measurements(), 0)
        self.assertReadableReports()
        html = next((self.folder / "reports").glob("*.html")).read_text(encoding="utf-8")
        self.assertIn("never finished", html)
        shown = subprocess.run(
            [sys.executable, "-m", "benchrun", "show", "--latest", "--db", str(self.db)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("an output may still be on", shown.stdout)


class BadResultsFile(SocketCase):
    def assertNoInstrumentTouched(self, done, supply_log: Path) -> None:
        self.assertEqual(done.returncode, 3, done.stdout + done.stderr)
        self.assertEqual(self.journal(supply_log), [])

    def test_read_only_database_touches_nothing(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("file permissions do not stop root")
        supply, supply_log = self.fake("supply")
        dmm, _ = self.fake("dmm")
        self.run_benchrun(small_plan(), {"supply": supply, "dmm": dmm})
        before_size = self.db.stat().st_size
        before = len(self.journal(supply_log))
        os.chmod(self.db, stat.S_IREAD)
        self.addCleanup(os.chmod, self.db, stat.S_IREAD | stat.S_IWRITE)
        done = self.run_benchrun(small_plan(), {"supply": supply, "dmm": dmm})
        self.assertEqual(done.returncode, 3, done.stdout + done.stderr)
        self.assertIn("readonly", done.stderr)
        self.assertEqual(len(self.journal(supply_log)), before, "no instrument may be touched without a record")
        self.assertEqual(self.db.stat().st_size, before_size)

    def test_garbage_database_is_left_alone(self):
        self.db.write_bytes(b"this is not a database, and it matters to someone" * 100)
        original = self.db.read_bytes()
        supply, supply_log = self.fake("supply")
        dmm, _ = self.fake("dmm")
        done = self.run_benchrun(small_plan(), {"supply": supply, "dmm": dmm})
        self.assertNoInstrumentTouched(done, supply_log)
        self.assertIn("cannot be used", done.stderr)
        self.assertEqual(self.db.read_bytes(), original)

    def test_database_where_a_folder_cannot_be_made(self):
        supply, supply_log = self.fake("supply")
        dmm, _ = self.fake("dmm")
        (self.folder / "a_file").write_text("not a folder", encoding="utf-8")
        args = self.command(small_plan(), {"supply": supply, "dmm": dmm}, "--db", str(self.folder / "a_file" / "r.sqlite"))
        done = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertNotIn("Traceback", done.stderr)
        self.assertNoInstrumentTouched(done, supply_log)
        self.assertIn("cannot open results database", done.stderr)

    def test_report_folder_blocked_by_a_file_keeps_the_verdict(self):
        supply, _ = self.fake("supply")
        dmm, _ = self.fake("dmm")
        blocker = self.folder / "reports_file"
        blocker.write_text("in the way", encoding="utf-8")
        done = self.run_benchrun(small_plan(), {"supply": supply, "dmm": dmm}, "--report-dir", str(blocker))
        self.assertEqual(done.returncode, 3)
        self.assertIn("RESULT: PASS", done.stdout)
        self.assertIn("cannot write reports", done.stderr)
        self.assertEqual(self.record()["status"], "pass")

    def test_output_closed_early_changes_nothing(self):
        """As with `benchrun run ... | head -1`: the reader going away must not change the result."""
        supply, supply_log = self.fake("supply")
        dmm, _ = self.fake("dmm")
        process = subprocess.Popen(
            self.command(small_plan(settle_s=0.05), {"supply": supply, "dmm": dmm}),
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(process.kill)
        process.stdout.readline()
        process.stdout.close()
        err = process.stderr.read()
        process.stderr.close()
        process.wait(timeout=60)
        self.assertNotIn("Traceback", err)
        self.assertEqual(process.returncode, 0, err)
        self.assertEqual(self.record()["status"], "pass")
        self.assertOutputOff(supply_log)


if __name__ == "__main__":
    unittest.main()
