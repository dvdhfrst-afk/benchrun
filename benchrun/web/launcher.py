"""Starting one run at a time from the browser, as a child ``python -m benchrun run`` process.

The child gets an argument list, never a shell, and only plan files found directly in
the plans folder. Its output is kept as it arrives. A stop is the same request a
terminal sends (Ctrl+Break on Windows, Ctrl+C elsewhere), so the run switches its
outputs off and records itself as aborted. An operator prompt is answered by
sending the Enter key the run is waiting for.
"""

from __future__ import annotations

import collections
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Mapping, Optional

import benchrun
from benchrun import stations
from benchrun.console import CONTROL, check_label, check_serial
from benchrun.errors import PlanError
from benchrun.store import now_iso

MAX_PLANS = 1000
MAX_LINES = 5000  # Lines of output kept; older ones are dropped and counted.
MAX_LINE_CHARS = 2000
STOP_WAIT_S = 60.0  # How long closing the server waits for a stopped run to switch off and record.

_RUN_LINE = re.compile(r"Run (\d+): serial ")
_ENTER_LINE = "Press Enter to go on"
_MEANING = {0: "passed", 1: "failed", 2: "could not start: the plan or an argument was refused", 3: "aborted, or could not complete"}


def plan_files(folder: Path) -> List[str]:
    """The names of the ``.json`` files directly in ``folder``. Links and subfolders are never offered."""
    names = []
    for entry in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
        if entry.suffix.lower() == ".json" and not entry.is_symlink() and entry.is_file():
            names.append(entry.name)
            if len(names) == MAX_PLANS:
                break
    return names


@dataclass
class LaunchedRun:
    """One child run: what it was asked to do, what it has printed, and how it ended."""

    form: Dict[str, Any]
    args: List[str]
    started_at: str
    process: subprocess.Popen
    lines: Deque[str] = field(default_factory=lambda: collections.deque(maxlen=MAX_LINES))
    dropped: int = 0
    run_id: Optional[int] = None
    prompt: Optional[str] = None  # What the operator is asked, while the run waits for Enter.
    stop_sent: bool = False
    stop_problem: Optional[str] = None
    returncode: Optional[int] = None
    finished_at: Optional[str] = None

    @property
    def running(self) -> bool:
        return self.returncode is None

    def outcome(self) -> str:
        if self.returncode is None:
            return "running"
        return _MEANING.get(self.returncode, f"ended with exit code {self.returncode}")

    def snapshot(self) -> Dict[str, Any]:
        return {
            "form": dict(self.form),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "running": self.running,
            "exit_code": self.returncode,
            "outcome": self.outcome(),
            "run_id": self.run_id,
            "prompt": self.prompt,
            "stop_sent": self.stop_sent,
            "stop_problem": self.stop_problem,
            "output": list(self.lines),
            "dropped_lines": self.dropped,
        }


class Launcher:
    def __init__(self, db: Path, plans_dir: Path):
        self.db = db
        self.plans_dir = plans_dir
        self._lock = threading.Lock()
        self.current: Optional[LaunchedRun] = None

    def plans(self) -> List[str]:
        return plan_files(self.plans_dir)

    def snapshot(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return None if self.current is None else self.current.snapshot()

    def busy(self) -> bool:
        with self._lock:
            return self.current is not None and self.current.running

    def check(self, conn: sqlite3.Connection, form: Mapping[str, str]) -> Dict[str, Any]:
        """The form's choices, each checked against what exists. Raises PlanError with every problem."""
        problems: List[str] = []
        chosen: Dict[str, Any] = {"plan": form.get("plan", ""), "serial": form.get("serial", "").strip()}
        product = form.get("product", "")
        station = form.get("station", "")
        chosen["sim"] = form.get("sim") == "on"
        if chosen["plan"] and chosen["plan"] not in self.plans():
            problems.append(f"plan {chosen['plan']!r} is not a .json file in the plans folder")
        if not chosen["plan"] and not product:
            problems.append("choose a plan, or a product to use the plan it is tested with")
        try:
            chosen["serial"] = check_serial(chosen["serial"])
        except PlanError as exc:
            problems.extend(exc.problems)
        try:
            chosen["operator"] = check_label(form.get("operator", "").strip(), "operator")
        except PlanError as exc:
            problems.extend(exc.problems)
        if product and conn.execute("SELECT 1 FROM products WHERE part_number = ?", (product,)).fetchone() is None:
            problems.append(f"no product {product!r} in this database")
        chosen["product"] = product
        found = stations.get_station(conn, station) if station else None
        if station and found is None:
            problems.append(f"no station named {station!r} in this database")
        chosen["station"] = found["name"] if found else ""
        if chosen["sim"] and station:
            problems.append("a station and the simulators cannot be combined: the simulators are not on any station")
        if problems:
            raise PlanError(problems)
        return chosen

    def command(self, chosen: Mapping[str, Any]) -> List[str]:
        """The argument list. Values go after an equals sign, so one that starts with a dash stays a value."""
        args = [sys.executable, "-m", "benchrun", "run"]
        if chosen["plan"]:
            args.append(str(self.plans_dir / chosen["plan"]))
        args += [f"--serial={chosen['serial']}", f"--db={self.db}"]
        for flag, key in (("--operator", "operator"), ("--product", "product"), ("--station-name", "station")):
            if chosen[key]:
                args.append(f"{flag}={chosen[key]}")
        if chosen["sim"]:
            args.append("--sim")
        return args

    def start(self, chosen: Mapping[str, Any]) -> LaunchedRun:
        """Start the run. Raises RuntimeError while another is still going."""
        with self._lock:
            if self.current is not None and self.current.running:
                raise RuntimeError("a run started here is still going; stop it or wait for it to finish")
            args = self.command(chosen)
            env = dict(os.environ)
            source_root = str(Path(benchrun.__file__).resolve().parent.parent)
            env["PYTHONPATH"] = os.pathsep.join(p for p in (source_root, env.get("PYTHONPATH", "")) if p)
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUNBUFFERED"] = "1"
            options: Dict[str, Any] = {}
            if os.name == "nt":
                options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            else:
                options["start_new_session"] = True  # A Ctrl+C at the server is handled by the server, which stops this run.
            process = subprocess.Popen(
                args,
                cwd=str(self.plans_dir),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                **options,
            )
            launched = LaunchedRun(form=dict(chosen), args=args, started_at=now_iso(), process=process)
            self.current = launched
        threading.Thread(target=self._follow, args=(launched,), name="benchrun-web-run", daemon=True).start()
        return launched

    def _follow(self, launched: LaunchedRun) -> None:
        stream = launched.process.stdout
        assert stream is not None
        while True:
            raw = stream.readline(MAX_LINE_CHARS * 4)
            if not raw:
                break
            line = CONTROL.sub("?", raw.decode("utf-8", "replace").rstrip("\r\n"))[:MAX_LINE_CHARS]
            with self._lock:
                if len(launched.lines) == MAX_LINES:
                    launched.dropped += 1
                launched.lines.append(line)
                found = _RUN_LINE.match(line)
                if found and launched.run_id is None:
                    launched.run_id = int(found.group(1))
                if line.startswith("OPERATOR: "):
                    launched.prompt = line[len("OPERATOR: "):]
                elif not line.startswith(_ENTER_LINE):
                    launched.prompt = None
        code = launched.process.wait()
        stream.close()
        with self._lock:
            if launched.process.stdin is not None:
                launched.process.stdin.close()
        with self._lock:
            launched.prompt = None
            launched.returncode = code
            launched.finished_at = now_iso()

    def stop(self) -> str:
        """Send the stop request. Returns what happened, in words."""
        with self._lock:
            launched = self.current
            if launched is None or not launched.running:
                return "No run is going."
            try:
                if os.name == "nt":
                    launched.process.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
                else:
                    launched.process.send_signal(signal.SIGINT)
            except OSError as exc:
                launched.stop_problem = f"the stop request could not be sent ({exc}); stop the run at the bench"
                return launched.stop_problem
            launched.stop_sent = True
            return "Stop sent. The run switches its outputs off and records itself as aborted."

    def answer(self) -> str:
        """Press Enter for the operator, when the run is waiting at a prompt."""
        with self._lock:
            launched = self.current
            if launched is None or not launched.running or launched.prompt is None:
                return "The run is not waiting for the operator."
            stdin = launched.process.stdin
            try:
                assert stdin is not None
                stdin.write(b"\n")
                stdin.flush()
            except OSError as exc:
                return f"the answer could not be sent ({exc})"
            launched.prompt = None
            return "Answered."

    def close(self, wait_s: float = STOP_WAIT_S) -> Optional[str]:
        """For the server stopping: stop a run still going and give it time to switch off. A problem, in words, or None."""
        if not self.busy():
            return None
        self.stop()
        deadline = time.monotonic() + wait_s
        while self.busy() and time.monotonic() < deadline:
            time.sleep(0.1)
        if self.busy():
            return f"the run started from the browser is still going after {wait_s:g} s; check the bench"
        return None
