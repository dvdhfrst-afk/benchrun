"""The web interface and HTTP API: pages, JSON, downloads, security, and starting runs from the browser."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode

from benchrun import cli
from benchrun.archive import Archive
from benchrun.plan import apply_addresses, parse_plan
from benchrun.reports import REGISTRY
from benchrun.runner import Runner
from benchrun.sim.bench import SimBench
from benchrun.store import Store
from benchrun.web import data
from benchrun.web.launcher import Launcher, plan_files
from benchrun.web.markup import item_url
from benchrun.web.server import App, Server
from tests.support import base_plan, clone

ROOT = Path(__file__).resolve().parent.parent
FOLDER: Path = Path()
DB: Path = Path()
HOSTILE = '<script>alert("x")</script>'


def _quiet_cli(*argv: str) -> int:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return cli.main(list(argv))


def _run(store: Store, bench: SimBench, serial: str, **runner: Any) -> int:
    plan = parse_plan(base_plan())
    apply_addresses(plan, bench.addresses_for((s.name, s.driver) for s in plan.instruments.values()))
    return Runner(plan, store, serial, sleep=lambda _: None, **runner).run().run_id


def setUpModule() -> None:
    """One database for every reading test: passes, failures, an abort, a run still going, a lot, a station, a product."""
    global FOLDER, DB
    FOLDER = Path(tempfile.mkdtemp(prefix="benchrun-web-"))
    DB = FOLDER / "results.sqlite"
    plan_file = FOLDER / "small.json"
    plan_file.write_text(json.dumps(base_plan()), encoding="utf-8")
    db = str(DB)
    for argv in (
        ("instrument", "add", "PSU-0001", "--driver", "psu", "--address", "127.0.0.1:5026", "--actor", "Operator A"),
        ("instrument", "add", "DMM-0001", "--driver", "dmm", "--address", "127.0.0.1:5025", "--model", "SIM-DMM"),
        ("station", "create", "LINE-1", "--location", "Lab 1"),
        ("station", "assign", "LINE-1", "supply", "PSU-0001"),
        ("station", "assign", "LINE-1", "dmm", "DMM-0001"),
        ("product", "add", "PN-100-A", "--plan", str(plan_file), "--description", "Fake regulator"),
        ("product", "set-limit", "PN-100-A", "final_check", "vout", "--low", "4.8", "--high", "5.2"),
    ):
        assert _quiet_cli(*argv, "--db", db) == 0, argv
    with Store(DB) as store:
        with store.conn:
            store.conn.execute(
                "INSERT INTO lots (name, product, plan_name, quantity, note, created_at) VALUES ('L2026-11-01', 'PN-100-A', 'small', 5, 'Fake lot', '2026-11-01T08:00:00.000+00:00')"
            )
        lot_id = store.conn.execute("SELECT id FROM lots").fetchone()[0]
        good = SimBench(noise=0.0).start()
        bad = SimBench(noise=0.0, dut="high-output").start()
        try:
            _run(store, good, "SN-1001", operator="Operator A", lot_id=lot_id, product="PN-100-A")
            _run(store, bad, "SN-1002", lot_id=lot_id, product="PN-100-A")
            _run(store, good, "SN-1002", lot_id=lot_id, product="PN-100-A", note="retest after rework")
            _run(store, good, "SN-1003", station="LINE-1", asset_tags={"supply": "PSU-0001", "dmm": "DMM-0001"})
            _run(store, good, HOSTILE)
            _run(store, good, "SN/1004")
        finally:
            for bench in (good, bad):
                bench.stop()
        aborted = store.create_run("SN-1005", "small", "1", "x", "{}", {}, "", "1.1.0")
        step = store.add_step(aborted, 0, "vout_vs_vin", "test")
        store.add_measurement(aborted, step, "vout", "read", {"vin": 8.0}, float("inf"), "V", 4.9, 5.1, "error", "overload")
        store.finish_run(aborted, "aborted", "dmm: overload")
        store.create_run("SN-1006", "small", "1", "x", "{}", {}, "", "1.1.0")  # Still going.


def tearDownModule() -> None:
    shutil.rmtree(FOLDER, ignore_errors=True)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class ServerCase(unittest.TestCase):
    app: App
    server: Server

    @classmethod
    def make_app(cls) -> App:
        return App(DB, quiet=True)

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = cls.make_app()
        cls.server = Server("127.0.0.1", 0, cls.app)
        cls.thread = threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.app.port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def fetch(self, path: str, method: str = "GET", body: Optional[bytes] = None, headers: Optional[Dict[str, str]] = None) -> Tuple[int, Any, bytes]:
        request = urllib.request.Request(self.base + path, data=body, method=method, headers=headers or {})
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(request, timeout=30) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, exc.headers, exc.read()

    def page(self, path: str, status: int = 200) -> str:
        code, headers, body = self.fetch(path)
        self.assertEqual(code, status, body[:500])
        self.assertTrue(headers["Content-Type"].startswith("text/html"), headers["Content-Type"])
        return body.decode("utf-8")

    def json(self, path: str, status: int = 200) -> Dict[str, Any]:
        code, headers, body = self.fetch(path)
        self.assertEqual(code, status, body[:500])
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        return json.loads(body, parse_constant=lambda word: self.fail(f"JSON holds {word}"))

    def raw(self, request: bytes) -> Tuple[int, bytes]:
        with socket.create_connection(("127.0.0.1", self.app.port), timeout=10) as sock:
            sock.sendall(request)
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        answer = b"".join(chunks)
        return int(answer.split(b" ", 2)[1]), answer

    def post(self, path: str, fields: Dict[str, str], origin: Optional[str] = "same") -> Tuple[int, Any, bytes]:
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if origin == "same":
            headers["Origin"] = self.base
        elif origin is not None:
            headers["Origin"] = origin
        return self.fetch(path, "POST", urlencode(fields).encode("ascii"), headers)

    def run_id(self, serial: str) -> int:
        conn = sqlite3.connect(DB)
        try:
            return conn.execute("SELECT id FROM runs WHERE serial = ? ORDER BY id DESC", (serial,)).fetchone()[0]
        finally:
            conn.close()


class Pages(ServerCase):
    def test_dashboard_shows_running_runs_yield_and_recent_runs(self):
        text = self.page("/")
        self.assertIn("Runs still marked running", text)
        self.assertIn(f'href="/runs/{self.run_id("SN-1006")}"', text)
        self.assertIn("Yield today", text)
        self.assertIn("Yield this week", text)
        self.assertIn('href="/stats?plan=small"', text)
        self.assertIn("Recent runs", text)
        self.assertIn('<link rel="stylesheet" href="/static/style.css">', text)
        self.assertNotIn("<script", text)

    def test_runs_list_filters_and_pages(self):
        text = self.page("/runs?serial=SN-1002")
        self.assertIn("retest after rework", text)
        self.assertEqual(text.count('<span class="badge'), 2)
        text = self.page("/runs?status=fail")
        self.assertIn(">SN-1002<", text)
        self.assertNotIn(">SN-1001<", text)
        self.assertIn(">SN-1003<", self.page("/runs?station=line-1"))
        lot_text = self.page("/runs?lot=L2026-11-01")
        self.assertIn(">L2026-11-01<", lot_text)
        self.assertNotIn(">SN-1003<", lot_text)
        first = self.page("/runs?per_page=3")
        self.assertIn("Page 1 of 3, 8 in all", first)
        self.assertIn("page=2", first)
        last = self.page("/runs?per_page=3&page=3")
        self.assertIn(">SN-1001<", last)
        self.assertNotIn("Older", last)
        self.assertIn("/download/export?serial=SN-1002&amp;format=csv", self.page("/runs?serial=SN-1002"))

    def test_bad_filters_are_plain_400s(self):
        self.assertIn("status must be one of", self.page("/runs?status=nope", 400))
        self.assertIn("is not a time", self.page("/runs?since=yesterday", 400))
        self.assertIn("no lot", self.page("/runs?lot=L-none", 400))
        self.assertIn("per_page must be a whole number", self.page("/runs?per_page=100000", 400))
        self.assertIn("give serial only once", self.page("/runs?serial=a&serial=b", 400))

    def test_run_detail_has_plots_instruments_and_every_download(self):
        run_id = self.run_id("SN-1003")
        text = self.page(f"/runs/{run_id}")
        self.assertIn("<svg", text)
        self.assertIn("vout versus vin", text)
        self.assertIn("PSU-0001", text)
        self.assertIn("LINE-1", text)
        for name in REGISTRY:
            self.assertIn(f'href="/runs/{run_id}/report/{name}"', text)
        self.assertIn(f'href="/api/runs/{run_id}"', text)
        self.assertIn('href="/serials/SN-1003"', text)
        lot_run = self.run_id("SN-1001")
        self.assertIn('href="/lots/1"', self.page(f"/runs/{lot_run}"))
        self.assertIn("no run with id 999", self.page("/runs/999", 404))
        self.assertIn("no run", self.page("/runs/abc", 404))

    def test_every_report_format_downloads(self):
        run_id = self.run_id("SN-1001")
        for name, report_format in REGISTRY.items():
            with self.subTest(name):
                code, headers, body = self.fetch(f"/runs/{run_id}/report/{name}")
                self.assertEqual(code, 200)
                disposition = headers["Content-Disposition"]
                self.assertRegex(disposition, r'^attachment; filename="run-0+\d+-SN-1001-\d{8}T\d{6}' + re.escape(report_format.ending) + '"$')
                self.assertIn("sandbox", headers["Content-Security-Policy"])
                self.assertEqual(body.decode("utf-8"), report_format.render(Store(DB).run_detail(run_id)))
        self.assertIn("no report format", self.page(f"/runs/{run_id}/report/pdf", 404))

    def test_hostile_text_is_escaped_everywhere(self):
        run_id = self.run_id(HOSTILE)
        for path in ("/", "/runs", f"/runs/{run_id}", item_url("/serials", HOSTILE, "serial")):
            with self.subTest(path):
                text = self.page(path)
                self.assertNotIn(HOSTILE, text)
                self.assertIn("&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;", text)
        code, headers, body = self.fetch(f"/runs/{run_id}/report/csv")
        self.assertNotIn("<", headers["Content-Disposition"])

    def test_unit_history_by_serial_including_one_with_a_slash(self):
        text = self.page("/serials/SN-1002")
        self.assertIn("Unit SN-1002", text)
        self.assertIn("2 runs in all.</p>", text)
        self.assertNotIn("&lt;/p&gt;", text)
        self.assertIn("1 run in all.</p>", self.page("/serials?serial=SN%2F1004"))
        self.assertIn('href="/serials?serial=SN%2F1004"', self.page("/runs"))
        self.assertIn("Unit SN/1004", self.page("/serials?serial=SN%2F1004"))
        self.assertIn("no runs of serial", self.page("/serials/SN-0000", 404))

    def test_plans_and_statistics(self):
        text = self.page("/plans")
        self.assertIn('href="/stats?plan=small"', text)
        text = self.page("/stats?plan=small")
        self.assertIn("complete runs used", text)
        self.assertIn("left out: 1 aborted, 1 never finished", text)
        self.assertIn("First-pass yield", text)
        self.assertIn("Failures", text)
        self.assertIn("Histogram of", text)
        self.assertIn("individuals chart", text)
        self.assertIn('href="/api/stats?plan=small"', text)
        self.assertIn("Choose a plan or a product", self.page("/stats"))
        self.assertIn("No matching runs", self.page("/stats?plan=none-such"))
        self.assertIn("1 runs selected", self.page("/stats?plan=small&station=LINE-1"))

    def test_lots(self):
        text = self.page("/lots")
        self.assertIn('href="/lots/1"', text)
        text = self.page("/lots/1")
        self.assertIn("Lot L2026-11-01", text)
        self.assertIn("Units still to test", text)
        self.assertIn("<td>3</td>", text)  # Five ordered, two tested.
        self.assertIn("50.0%", text)  # First-pass: SN-1002 failed first.
        self.assertIn("100.0%", text)  # Final: it passed on retest.
        self.assertIn("Failures", text)
        self.assertIn("no lot named &#x27;9&#x27;", self.page("/lots/9", 404))
        # By name too, ignoring case, as every lot option on the command line takes it.
        self.assertIn("Lot L2026-11-01", self.page("/lots/l2026-11-01"))

    def test_stations_instruments_products_and_audit(self):
        text = self.page("/stations")
        self.assertIn("LINE-1", text)
        self.assertIn('href="/instruments/PSU-0001"', text)
        self.assertIn("1 runs recorded on it", text)
        text = self.page("/instruments?driver=dmm")
        self.assertIn("DMM-0001", text)
        self.assertNotIn("PSU-0001", text)
        text = self.page("/instruments/PSU-0001")
        self.assertIn("LINE-1 (supply)", text)
        self.assertIn(">SN-1003<", text)
        self.assertIn("no instrument", self.page("/instruments/NOPE-1", 404))
        text = self.page("/products")
        self.assertIn('href="/products/PN-100-A"', text)
        text = self.page("/products/PN-100-A")
        self.assertIn("Fake regulator", text)
        self.assertIn("final_check", text)
        self.assertIn("3 runs recorded as this product", text)
        text = self.page("/products/pn-100-a")
        self.assertIn("<h1>Product PN-100-A</h1>", text, "named as it was added, whatever case the address used")
        self.assertIn("3 runs recorded as this product", text)
        self.assertIn("Instrument PSU-0001", self.page("/instruments/psu-0001"))
        text = self.page("/audit")
        self.assertIn("instrument:PSU-0001", text)
        self.assertIn("Operator A", text)
        text = self.page("/audit?per_page=2")
        self.assertIn("page=2", text)
        self.assertIn("station:LINE-1", self.page("/audit?subject=station:"))

    def test_starting_runs_is_off_unless_asked_for(self):
        self.assertIn("--allow-runs", self.page("/launch", 404))
        self.assertNotIn('href="/launch"', self.page("/"))
        code, _, body = self.post("/launch", {"token": self.app.site.token, "serial": "SN-1"})
        self.assertEqual(code, 403, body)


class Api(ServerCase):
    def test_index_lists_endpoints(self):
        self.assertIn("/api/runs/<id>", self.json("/api")["endpoints"])

    def test_runs_are_paged_and_filtered(self):
        found = self.json("/api/runs?per_page=2&page=2")
        self.assertEqual(found["paging"], {"page": 2, "per_page": 2, "has_more": True, "total": 8, "pages": 4})
        self.assertEqual(len(found["runs"]), 2)
        found = self.json("/api/runs?serial=SN-1002&status=pass")
        self.assertEqual([run["note"] for run in found["runs"]], ["retest after rework"])
        self.assertEqual(found["runs"][0]["lot"], "L2026-11-01")
        self.assertEqual(found["filter"], {"serial": "SN-1002", "status": "pass"})

    def test_one_run_is_strict_json_with_nonfinite_values_as_null(self):
        run_id = self.run_id("SN-1005")
        run = self.json(f"/api/runs/{run_id}")["run"]
        self.assertEqual(run["status"], "aborted")
        self.assertIsNone(run["steps"][0]["measurements"][0]["value"])
        run = self.json(f"/api/runs/{self.run_id('SN-1001')}")["run"]
        self.assertEqual(run["lot"], "L2026-11-01")
        self.assertEqual(run["product"], "PN-100-A")

    def test_errors_are_objects_with_status_codes(self):
        self.assertEqual(self.json("/api/runs/999", 404), {"error": {"status": 404, "code": "not_found", "message": "no run with id 999"}})
        self.assertEqual(self.json("/api/runs?status=x", 400)["error"]["code"], "bad_request")
        self.assertEqual(self.json("/api/nothing", 404)["error"]["status"], 404)
        code, headers, body = self.fetch("/api/runs", "PUT", b"")
        self.assertEqual(code, 405)
        self.assertEqual(headers["Allow"], "GET, HEAD, POST")
        self.assertEqual(json.loads(body)["error"]["code"], "method_not_allowed")

    def test_serials(self):
        found = self.json("/api/serials/SN-1002")
        self.assertEqual(found["paging"]["total"], 2)
        self.assertEqual(self.json("/api/serials?serial=SN%2F1004")["serial"], "SN/1004")
        self.assertEqual(self.json("/api/serials/SN-0000", 404)["error"]["code"], "not_found")

    def test_stats_views(self):
        summary = self.json("/api/stats?plan=small")
        self.assertEqual(summary["view"], "summary")
        self.assertEqual(summary["runs"]["aborted"], 1)
        self.assertTrue(summary["measurements"])
        yields = self.json("/api/stats?plan=small&view=yield")["yield"][0]
        self.assertEqual(yields["units_with_verdict"], 5)
        pareto = self.json("/api/stats?plan=small&view=pareto")["pareto"]
        self.assertEqual(pareto["failed_runs"], 1)
        histogram = self.json("/api/stats?plan=small&view=histogram&measurement=vout&bins=4")
        self.assertEqual(histogram["charts"][0]["histogram"]["bins"], 4)
        self.assertEqual(self.json("/api/stats?plan=small&lot=1&view=yield")["runs"]["selected"], 3)
        self.assertIn("needs measurement", self.json("/api/stats?plan=small&view=control", 400)["error"]["message"])
        self.assertIn("plan=NAME", self.json("/api/stats", 400)["error"]["message"])
        self.assertIn("view must be", self.json("/api/stats?plan=small&view=pie", 400)["error"]["message"])

    def test_lots_stations_instruments_products_audit(self):
        lot = self.json("/api/lots")["lots"][0]
        self.assertEqual((lot["name"], lot["quantity"], lot["units"]["units_with_verdict"], lot["units"]["to_test"]), ("L2026-11-01", 5, 2, 3))
        detail = self.json("/api/lots/1")
        self.assertEqual(detail["lot"]["name"], "L2026-11-01")
        self.assertEqual(detail["runs"]["selected"], 3)
        self.assertEqual(detail["units"]["first_pass_yield"], 0.5)
        self.assertEqual(detail["units"]["final_yield"], 1.0)
        self.assertEqual(detail["pareto"]["rows"][0]["runs"], 1)
        self.assertEqual([unit["serial"] for unit in detail["unit_results"]], ["SN-1001", "SN-1002"])
        self.assertEqual(self.json("/api/lots/9", 404)["error"]["message"], "no lot named '9' and none with that id")
        self.assertEqual(self.json("/api/lots/L2026-11-01")["unit_results"], detail["unit_results"])
        station = self.json("/api/stations")["stations"][0]
        self.assertEqual([role["asset_tag"] for role in station["roles"]], ["DMM-0001", "PSU-0001"])
        self.assertEqual(len(self.json("/api/instruments")["instruments"]), 2)
        self.assertEqual(self.json("/api/instruments?status=active&search=sim-dmm")["instruments"][0]["asset_tag"], "DMM-0001")
        self.assertEqual(self.json("/api/instruments/psu-0001")["instrument"]["recent_runs"][0]["serial"], "SN-1003")
        self.assertEqual(self.json("/api/products")["products"][0]["runs"], 3)
        self.assertEqual(self.json("/api/products/PN-100-A")["product"]["limits"][0]["low"], 4.8)
        audit = self.json("/api/audit?per_page=3")
        self.assertEqual(len(audit["entries"]), 3)
        self.assertTrue(audit["paging"]["has_more"])
        rest = self.json("/api/audit?per_page=3&page=3")
        self.assertFalse(rest["paging"]["has_more"])
        self.assertEqual(self.json("/api/audit?subject=product:PN-100-A")["entries"][0]["action"], "product.set-limit")


class Downloads(ServerCase):
    def test_exports_of_a_filtered_list(self):
        code, headers, body = self.fetch("/download/export?format=csv&serial=SN-1002")
        self.assertEqual(code, 200)
        self.assertEqual(headers["Content-Type"], "text/csv; charset=utf-8")
        self.assertRegex(headers["Content-Disposition"], r'^attachment; filename="benchrun-export-\d{8}T\d{6}Z\.csv"$')
        lines = body.decode("utf-8").splitlines()
        self.assertTrue(all(",SN-1002," in line for line in lines[1:]), lines[:3])
        code, _, body = self.fetch("/download/export?format=jsonl&lot=1")
        self.assertEqual({json.loads(line)["serial"] for line in body.decode("utf-8").splitlines()}, {"SN-1001", "SN-1002"})
        code, headers, body = self.fetch("/download/export?format=xlsx")
        self.assertEqual(code, 200)
        self.assertIn("xl/workbook.xml", zipfile.ZipFile(io.BytesIO(body)).namelist())
        self.assertEqual(self.fetch("/download/export?format=pdf")[0], 400)
        self.assertEqual(self.fetch("/download/export?serial=SN-0000")[0], 404)

    def test_archive_of_a_filtered_list_verifies(self):
        code, headers, body = self.fetch("/download/archive?product=PN-100-A")
        self.assertEqual(code, 200)
        self.assertEqual(headers["Content-Type"], "application/zip")
        path = FOLDER / "download.zip"
        path.write_bytes(body)
        archive = Archive(path)
        try:
            self.assertEqual(archive.check(), [])
            self.assertEqual(archive.manifest["run_count"], 3)
        finally:
            archive.close()

    def test_no_temporary_files_are_left(self):
        before = set(Path(tempfile.gettempdir()).glob("benchrun-web-*"))
        self.fetch("/download/export?format=csv")
        self.fetch("/download/archive")
        after = set(Path(tempfile.gettempdir()).glob("benchrun-web-*"))
        self.assertEqual(after - before, set())


class Security(ServerCase):
    def assertSecure(self, headers: Any, page: bool = True) -> None:
        policy = headers["Content-Security-Policy"]
        self.assertIn("default-src 'none'", policy)
        self.assertIn("frame-ancestors 'none'", policy)
        if page:
            self.assertNotIn("unsafe-inline", policy)
            self.assertNotIn("script-src", policy)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Server"], "benchrun")

    def test_every_kind_of_answer_has_the_headers(self):
        for path in ("/", "/api/runs", "/static/style.css", "/nope", "/api/nope", "/runs?status=x"):
            with self.subTest(path):
                _, headers, _ = self.fetch(path)
                self.assertSecure(headers)
        _, headers, _ = self.fetch(f"/runs/{self.run_id('SN-1001')}/report/html")
        self.assertSecure(headers, page=False)
        status, answer = self.raw(b"GET /" + b"a" * 70000 + b" HTTP/1.0\r\n\r\n")
        self.assertEqual(status, 414)
        self.assertIn(b"X-Content-Type-Options: nosniff", answer)

    def test_traversal_and_encoded_separators_are_refused(self):
        for target in ("/runs/%2e%2e/1", "/static/..%2fstyle.css", "/static/%2E%2E%5Cstyle.css", "/a%2Fb", "/runs/%00", "/runs/%ff"):
            with self.subTest(target):
                self.assertEqual(self.fetch(target)[0], 400)
        for target in (b"/static/../style.css", b"/static\\style.css", b"http://example.invalid/", b"*"):
            with self.subTest(target):
                status, _ = self.raw(b"GET " + target + b" HTTP/1.0\r\nHost: 127.0.0.1:%d\r\n\r\n" % self.app.port)
                self.assertEqual(status, 400)
        status, _ = self.raw(b"GET //example.invalid/x HTTP/1.0\r\nHost: 127.0.0.1:%d\r\n\r\n" % self.app.port)
        self.assertEqual(status, 404)  # Read as a path on this server, never as another site.

    def test_only_this_computers_names_are_answered(self):
        status, _ = self.raw(b"GET / HTTP/1.0\r\nHost: rebound.example\r\n\r\n")
        self.assertEqual(status, 421)
        status, _ = self.raw(b"GET / HTTP/1.0\r\nHost: localhost:1\r\n\r\n")
        self.assertEqual(status, 421)
        status, _ = self.raw(b"GET / HTTP/1.0\r\n\r\n")
        self.assertEqual(status, 400)
        status, _ = self.raw(b"GET / HTTP/1.0\r\nHost: localhost:%d\r\n\r\n" % self.app.port)
        self.assertEqual(status, 200)
        status, _ = self.raw(b"GET / HTTP/1.0\r\nHost: evil\r\nnext: x\r\n\r\n")
        self.assertEqual(status, 421)

    def test_posts_need_the_token_and_this_origin(self):
        token = self.app.site.token
        self.assertEqual(self.post("/launch/stop", {"token": "wrong"})[0], 403)
        self.assertEqual(self.post("/launch/stop", {"token": token}, origin="http://elsewhere.example")[0], 403)
        self.assertEqual(self.post("/launch/stop", {"token": token}, origin=None)[0], 403)
        self.assertEqual(self.post("/launch/stop", {"token": token}, origin="null")[0], 403)
        self.assertEqual(self.post("/runs", {"token": token})[0], 405)
        self.assertEqual(self.fetch("/launch/stop")[0], 405)
        big = b"token=" + b"x" * 20000
        code, _, body = self.fetch("/launch", "POST", big, {"Content-Type": "application/x-www-form-urlencoded", "Origin": self.base})
        self.assertEqual(code, 413, body)
        code, _, _ = self.fetch("/launch", "POST", b"{}", {"Content-Type": "application/json", "Origin": self.base})
        self.assertEqual(code, 415)
        status, _ = self.raw(b"POST /launch HTTP/1.0\r\nHost: 127.0.0.1:%d\r\nOrigin: http://127.0.0.1:%d\r\n\r\n" % (self.app.port, self.app.port))
        self.assertEqual(status, 411)

    def test_a_refused_body_still_gets_its_answer_and_not_a_reset(self):
        # A client still sending a body the server has already refused used to get its connection reset,
        # and so never saw the 413. The rest of the body arrives after the server has decided.
        body = b"token=" + b"x" * 200000
        head = (
            b"POST /launch HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nOrigin: %s\r\n"
            b"Content-Type: application/x-www-form-urlencoded\r\nContent-Length: %d\r\n\r\n"
        ) % (self.app.port, self.base.encode("ascii"), len(body))
        with socket.create_connection(("127.0.0.1", self.app.port), timeout=10) as sock:
            sock.sendall(head + body[:1000])
            time.sleep(0.3)
            sock.sendall(body[1000:])
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        answer = b"".join(chunks)
        self.assertTrue(answer.startswith(b"HTTP/1.0 413 "), answer[:200])
        self.assertIn(b"at most", answer)

    def test_head_gives_the_headers_without_a_body(self):
        status, answer = self.raw(b"HEAD /runs HTTP/1.0\r\nHost: 127.0.0.1:%d\r\n\r\n" % self.app.port)
        self.assertEqual(status, 200)
        head, _, body = answer.partition(b"\r\n\r\n")
        self.assertIn(b"Content-Security-Policy", head)
        self.assertEqual(body, b"")

    def test_an_internal_error_is_a_plain_500_and_the_server_goes_on(self):
        with unittest.mock.patch("benchrun.web.pages.plans", side_effect=ValueError("broken")), contextlib.redirect_stderr(io.StringIO()) as err:
            text = self.page("/plans", 500)
        self.assertIn("internal error in benchrun", text)
        self.assertNotIn("broken", text)
        self.assertIn("ValueError('broken')", err.getvalue())
        self.page("/plans")

    def test_pages_never_change_the_database(self):
        before = hashlib.sha256(DB.read_bytes()).hexdigest()
        for path in ("/", "/runs", "/plans", "/stats?plan=small", "/lots", "/lots/1", "/api/stats?plan=small&view=control&measurement=vout", "/download/archive"):
            self.fetch(path)
        self.assertEqual(hashlib.sha256(DB.read_bytes()).hexdigest(), before)
        conn = data.connect_read_only(DB)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM runs")
        finally:
            conn.close()

    def test_a_database_that_cannot_be_read_is_a_503(self):
        app = App(FOLDER / "missing.sqlite", quiet=True)
        app.port = 80
        response = app.respond("GET", "/api/runs", {"Host": "localhost"}, io.BytesIO())
        self.assertEqual(response.status, 503)
        self.assertEqual(json.loads(response.body)["error"]["code"], "service_unavailable")


def _prompt_plan() -> Dict[str, Any]:
    plan = clone(base_plan())
    plan["setup"].insert(0, {"prompt": "Seat the unit"})
    return plan


class Launching(ServerCase):
    @classmethod
    def make_app(cls) -> App:
        cls.folder = Path(tempfile.mkdtemp(prefix="benchrun-web-launch-"))
        cls.plans = cls.folder / "plans"
        cls.plans.mkdir()
        (cls.plans / "small.json").write_text(json.dumps(base_plan()), encoding="utf-8")
        (cls.plans / "prompted.json").write_text(json.dumps(_prompt_plan()), encoding="utf-8")
        (cls.plans / "notes.txt").write_text("not a plan", encoding="utf-8")
        (cls.plans / "nested").mkdir()
        (cls.plans / "nested" / "hidden.json").write_text("{}", encoding="utf-8")
        cls.db = cls.folder / "results.sqlite"
        Store(cls.db).close()
        return App(cls.db, Launcher(cls.db, cls.plans), quiet=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.app.site.launcher.close(30)
        super().tearDownClass()
        shutil.rmtree(cls.folder, ignore_errors=True)

    def wait_for(self, test: Any, timeout: float = 60.0) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.app.site.launcher.snapshot()
            if state is not None and test(state):
                return state
            time.sleep(0.05)
        self.fail(f"gave up waiting: {self.app.site.launcher.snapshot()}")

    def start(self, plan: str, serial: str, **extra: str) -> Tuple[int, Any, bytes]:
        return self.post("/launch", {"token": self.app.site.token, "plan": plan, "serial": serial, **extra})

    def record(self, run_id: int) -> Dict[str, Any]:
        with Store(self.db) as store:
            return store.run_detail(run_id)

    def test_a_run_can_join_an_open_lot_and_a_passed_unit_needs_a_retest(self):
        self.assertEqual(_quiet_cli("lot", "open", "L-WEB", "--plan", str(self.plans / "small.json"), "--quantity", "2", "--db", str(self.db)), 0)
        self.assertIn('<option value="L-WEB">', self.page("/launch"))
        self.assertEqual(self.start("small.json", "WEB-6", sim="on", lot="L-WEB")[0], 303)
        state = self.wait_for(lambda s: not s["running"])
        self.assertEqual(state["exit_code"], 0, state["output"])
        self.assertIn("--lot=L-WEB", self.app.site.launcher.current.args)
        with Store(self.db) as store:
            lot_id = store.conn.execute("SELECT lot_id FROM runs WHERE id = ?", (state["run_id"],)).fetchone()[0]
            self.assertEqual(store.conn.execute("SELECT name FROM lots WHERE id = ?", (lot_id,)).fetchone()[0], "L-WEB")
        code, _, body = self.start("small.json", "WEB-6", sim="on", lot="L-WEB")
        self.assertEqual(code, 400)
        self.assertIn("already passed in lot L-WEB; tick retest", body.decode("utf-8"))
        self.assertEqual(self.start("small.json", "WEB-6", sim="on", lot="L-WEB", retest="on")[0], 303)
        state = self.wait_for(lambda s: not s["running"])
        self.assertEqual(state["exit_code"], 0, state["output"])
        self.assertIn("--retest", self.app.site.launcher.current.args)
        self.assertIn("no lot named", self.start("small.json", "WEB-7", lot="L-NONE")[2].decode("utf-8"))
        self.assertIn("only applies to a unit in a lot", self.start("small.json", "WEB-7", retest="on")[2].decode("utf-8"))

    def test_only_json_files_directly_in_the_folder_are_offered(self):
        self.assertEqual(plan_files(self.plans), ["prompted.json", "small.json"])
        text = self.page("/launch")
        self.assertIn('<option value="small.json">', text)
        self.assertNotIn("hidden.json", text)
        self.assertNotIn("notes.txt", text)
        self.assertIn(f'name="token" value="{self.app.site.token}"', text)
        self.assertIn('href="/launch"', self.page("/"))

    def test_a_run_from_the_browser_is_recorded_and_its_output_shown(self):
        code, headers, _ = self.start("small.json", "WEB-1", sim="on", operator="Operator A")
        self.assertEqual(code, 303)
        self.assertEqual(headers["Location"], "/launch")
        state = self.wait_for(lambda s: not s["running"])
        self.assertEqual(state["exit_code"], 0, state["output"])
        self.assertIn("RESULT: PASS", state["output"])
        record = self.record(state["run_id"])
        self.assertEqual((record["serial"], record["operator"], record["status"]), ("WEB-1", "Operator A", "pass"))
        text = self.page("/launch")
        self.assertIn("RESULT: PASS", text)
        self.assertIn(f'href="/runs/{state["run_id"]}"', text)
        self.assertIn("passed", text)
        self.assertNotIn('http-equiv="refresh"', text)
        self.assertIn('name="plan"', text)

    def test_an_operator_prompt_is_answered_from_the_browser_and_one_run_at_a_time(self):
        self.assertEqual(self.start("prompted.json", "WEB-2", sim="on")[0], 303)
        self.wait_for(lambda s: s["prompt"] == "Seat the unit")
        text = self.page("/launch")
        self.assertIn("Seat the unit", text)
        self.assertIn('action="/launch/continue"', text)
        self.assertIn('http-equiv="refresh"', text)
        self.assertNotIn('<form class="launch"', text)
        code, _, body = self.start("small.json", "WEB-3", sim="on")
        self.assertEqual(code, 409)
        self.assertIn("still going", body.decode("utf-8"))
        self.assertEqual(self.post("/launch/continue", {"token": self.app.site.token})[0], 303)
        state = self.wait_for(lambda s: not s["running"])
        self.assertEqual(state["exit_code"], 0, state["output"])
        self.assertEqual(self.record(state["run_id"])["status"], "pass")

    def test_the_stop_button_stops_the_run_the_way_a_terminal_does(self):
        self.assertEqual(self.start("prompted.json", "WEB-4", sim="on")[0], 303)
        self.wait_for(lambda s: s["prompt"] is not None and s["run_id"] is not None)
        self.assertEqual(self.post("/launch/stop", {"token": self.app.site.token})[0], 303)
        state = self.wait_for(lambda s: not s["running"])
        self.assertEqual(state["exit_code"], 3, state["output"])
        self.assertTrue(state["stop_sent"])
        record = self.record(state["run_id"])
        self.assertEqual(record["status"], "aborted")
        self.assertIn("interrupted", record["reason"])

    def test_the_form_is_checked_before_anything_starts(self):
        cases = (
            ({"plan": "../small.json", "serial": "WEB-5"}, "not a .json file in the plans folder"),
            ({"plan": "nested/hidden.json", "serial": "WEB-5"}, "not a .json file in the plans folder"),
            ({"plan": "small.json", "serial": ""}, "serial number"),
            ({"plan": "", "serial": "WEB-5"}, "choose a plan"),
            ({"plan": "small.json", "serial": "WEB-5", "product": "PN-NONE"}, "no product"),
            ({"plan": "small.json", "serial": "WEB-5", "station": "LINE-9"}, "no station named"),
        )
        for fields, expected in cases:
            with self.subTest(fields):
                code, _, body = self.post("/launch", {"token": self.app.site.token, **fields})
                self.assertEqual(code, 400)
                self.assertIn(expected, body.decode("utf-8"))
        state = self.app.site.launcher.snapshot()
        self.assertTrue(state is None or state["form"]["serial"] != "WEB-5", state)

    def test_the_command_line_never_goes_through_a_shell(self):
        launcher = self.app.site.launcher
        chosen = {"plan": "small.json", "serial": "-x; rm -rf /", "operator": "", "product": "", "station": "", "sim": True}
        args = launcher.command(chosen)
        self.assertEqual(args[:4], [sys.executable, "-m", "benchrun", "run"])
        self.assertIn("--serial=-x; rm -rf /", args)
        self.assertEqual(args[4], str(self.plans / "small.json"))


class Command(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="benchrun-web-cmd-"))
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.db = str(self.folder / "results.sqlite")
        Store(self.db).close()

    def problems(self, *argv: str) -> str:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["serve", *argv]), 2)
        return err.getvalue()

    def test_options_are_checked(self):
        self.assertIn("add --public", self.problems("--db", self.db, "--host", "0.0.0.0"))
        self.assertIn("--allow-runs needs --plans-dir", self.problems("--db", self.db, "--allow-runs"))
        self.assertIn("only applies with --allow-runs", self.problems("--db", self.db, "--plans-dir", str(self.folder)))
        self.assertIn("is not a folder", self.problems("--db", self.db, "--allow-runs", "--plans-dir", str(self.folder / "none")))
        self.assertIn("no results database", self.problems("--db", str(self.folder / "none.sqlite")))
        self.assertIn("--port 70000", self.problems("--db", self.db, "--port", "70000"))

    def test_serve_answers_and_stops_on_a_stop_request(self):
        flags = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}
        process = subprocess.Popen(
            [sys.executable, "-m", "benchrun", "serve", "--db", self.db, "--port", "0", "--quiet"],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **flags,
        )
        self.addCleanup(process.kill)
        lines = [process.stdout.readline() for _ in range(2)]
        address = lines[1].strip()
        self.assertRegex(address, r"^http://127\.0\.0\.1:\d+/$")
        with urllib.request.urlopen(address + "api/runs", timeout=30) as response:
            self.assertEqual(json.loads(response.read())["paging"]["total"], 0)
        process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)
        out, err = process.communicate(timeout=30)
        self.assertEqual(process.returncode, 0, out + err)
        self.assertIn("Stopping.", out)
        self.assertEqual(err, "")


if __name__ == "__main__":
    unittest.main()
