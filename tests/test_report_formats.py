"""The report format registry, the json, markdown and certificate reports, and export to CSV, JSON Lines and xlsx."""

import contextlib
import copy
import csv
import io
import json
import re
import zipfile
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

from benchrun.cli import EXIT_PASS, EXIT_USAGE, main
from benchrun.errors import PlanError
from benchrun.reports import (
    CLASSIC_FORMATS,
    FORMATS,
    REGISTRY,
    ReportFormat,
    describe_formats,
    register,
    report_stem,
    resolve_formats,
    write_reports,
)
from benchrun.reports import xlsx
from benchrun.reports.certificate_report import MAX_STEPS_LISTED, render_certificate
from benchrun.reports.csv_report import render_csv, render_many, write_csv
from benchrun.reports.export import EXPORT_FORMATS, export_runs, write_jsonl
from benchrun.reports.json_report import render_json
from benchrun.reports.markdown_report import md, render_markdown
from benchrun.runner import Runner
from benchrun.sim.instruments import Fault
from tests.support import BenchTestCase, base_plan

ROOT = Path(__file__).parent.parent
QUICK = ROOT / "examples" / "regulator_quick.json"
MAIN_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
HOSTILE = '<script>alert(1)</script> | *bold* [link](http://example.invalid) @everyone $x$ & \x07\x1b[31m'


def call(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main([str(a) for a in argv])
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class Runs(BenchTestCase):
    """Real runs on the simulators, and hand-made ones for what a bench cannot easily produce."""

    def sim(self, serial="SN-1001", only_steps=None, **bench):
        bench_obj = self.start_bench(**bench)
        plan = self.on_bench(base_plan(), bench_obj)
        result = Runner(
            plan,
            self.store,
            serial,
            operator="Operator A",
            sleep=lambda _: None,
            only_steps=only_steps,
            station="LINE-1",
            product="PN-100-A",
            asset_tags={"dmm": "DMM-0001"},
        ).run()
        return self.store.run_detail(result.run_id)

    def passed(self):
        return self.sim("SN-OK")

    def failed(self):
        return self.sim("SN-BAD", dut="high-output")

    def aborted(self):
        return self.sim("SN-AB", faults={"dmm": Fault("drop", 1)})

    def partial(self):
        return self.sim("SN-PART", only_steps=["final_check"])

    def unfinished(self):
        run_id = self.store.create_run("SN-RUN", "p", "1", "hash", "{}", {}, "Operator A", "1.1.0")
        return self.store.run_detail(run_id)

    def empty(self):
        run_id = self.store.create_run("SN-EMPTY", "p", "1", "hash", "{}", {}, "", "1.1.0")
        self.store.finish_run(run_id, "aborted", "no instrument answered")
        return self.store.run_detail(run_id)

    def hostile(self):
        store = self.store
        run_id = store.create_run(
            "=HYPERLINK(\"http://example.invalid\")" + HOSTILE,
            "+plan" + HOSTILE,
            "-1" + HOSTILE,
            "hash",
            "{}",
            {"vin": 5.0},
            "@Operator A" + HOSTILE,
            "1.1.0",
            note="line one\nline two\r\u2028" + HOSTILE,
            station="=STATION" + HOSTILE,
            product="PN" + HOSTILE,
        )
        row = store.add_instrument(run_id, "dmm" + HOSTILE, "dmm", "127.0.0.1:1", "-ASSET" + HOSTILE)
        store.set_instrument_idn(row, "<b>idn</b>\x00\x01" + HOSTILE)
        step_id = store.add_step(run_id, 1, "step" + HOSTILE, "test")
        store.add_measurement(run_id, step_id, "=SUM(A1)" + HOSTILE, "read", {"vin": 5.0}, 9.5, "@V", 4.9, 5.1, "fail", note="=1+1" + HOSTILE)
        store.add_measurement(run_id, step_id, "huge", "read", {}, float("inf"), "V", float("-inf"), None, "fail")
        store.finish_step(step_id, "fail", "message" + HOSTILE)
        store.finish_run(run_id, "fail")
        return store.run_detail(run_id)

    def many(self, count=3000):
        detail = copy.deepcopy(self.failed())
        template = detail["steps"][1]["measurements"][0]
        rows = []
        for index in range(count):
            m = dict(template)
            m["point"] = {"vin": float(index)}
            m["value"] = 5.0 + index * 1e-6
            m["verdict"] = "fail" if index % 7 == 0 else "pass"
            rows.append(m)
        detail["steps"][1]["measurements"] = rows
        return detail

    def every_kind(self):
        return {
            "pass": self.passed(),
            "fail": self.failed(),
            "aborted": self.aborted(),
            "partial": self.partial(),
            "unfinished": self.unfinished(),
            "empty": self.empty(),
            "hostile": self.hostile(),
        }


class Registry(Runs):
    def test_every_format_is_registered_once_with_its_own_ending(self):
        self.assertEqual(FORMATS, ("html", "csv", "junit", "json", "markdown", "certificate"))
        endings = [f.ending for f in REGISTRY.values()]
        self.assertEqual(len(set(endings)), len(endings))
        self.assertEqual(REGISTRY["markdown"].ending, ".md")
        self.assertEqual(REGISTRY["json"].ending, ".json")
        self.assertEqual(REGISTRY["certificate"].ending, "-certificate.html")
        for report_format in REGISTRY.values():
            self.assertTrue(report_format.description)

    def test_names_endings_and_groups_cannot_be_taken_twice(self):
        render = REGISTRY["csv"].render
        for clash in (
            ReportFormat("html", ".other", "x", render),
            ReportFormat("other", ".csv", "x", render),
            ReportFormat("every", ".every", "x", render),
        ):
            with self.subTest(clash.name), self.assertRaises(ValueError):
                register(clash)
        self.assertEqual(tuple(REGISTRY), FORMATS)

    def test_all_keeps_its_old_meaning_and_every_means_every_format(self):
        self.assertEqual(resolve_formats(["all"]), CLASSIC_FORMATS)
        self.assertEqual(resolve_formats(["every"]), FORMATS)
        self.assertEqual(resolve_formats(["markdown", "all", "csv", "markdown"]), ("markdown", "html", "csv", "junit"))
        with self.assertRaises(PlanError):
            resolve_formats(["pdf"])

    def test_help_lists_every_format(self):
        text = describe_formats()
        for name in FORMATS + ("all", "every"):
            self.assertIn(name, text)

    def test_every_format_renders_every_kind_of_run(self):
        for kind, detail in self.every_kind().items():
            for name, report_format in REGISTRY.items():
                with self.subTest(kind=kind, format=name):
                    text = report_format.render(detail)
                    self.assertIsInstance(text, str)
                    self.assertGreater(len(text), 50)

    def test_file_names_follow_the_run_pattern(self):
        detail = self.passed()
        paths = write_reports(detail, self.folder / "out", ["every"])
        stem = report_stem(detail)
        self.assertRegex(stem, r"^run-\d{6}-SN-OK-\d{8}T\d{6}$")
        self.assertEqual(
            [p.name for p in paths],
            [stem + ending for ending in (".html", ".csv", ".xml", ".json", ".md", "-certificate.html")],
        )
        self.assertEqual(len(write_reports(detail, self.folder / "classic")), 3)

    def test_classic_formats_are_unchanged_by_the_registry(self):
        detail = self.failed()
        paths = write_reports(detail, self.folder, ["csv"])
        self.assertEqual(paths[0].read_text(encoding="utf-8"), render_csv(detail))


class JsonReport(Runs):
    def load(self, detail):
        def refuse(name):
            raise AssertionError(f"{name} is not JSON")

        return json.loads(render_json(detail), parse_constant=refuse)

    def test_top_level_shape_and_key_order(self):
        record = self.load(self.passed())
        self.assertEqual(list(record), ["format", "format_version", "run", "plan", "instruments", "counts", "steps"])
        self.assertEqual(record["format"], "benchrun-run")
        self.assertEqual(record["format_version"], 1)
        self.assertEqual(
            list(record["run"]),
            [
                "id", "uuid", "serial", "product", "station", "lot_id", "status", "reason", "partial",
                "selected_steps", "operator", "note", "started_at", "finished_at", "software_version",
                "origin", "imported_at",
            ],
        )  # fmt: skip
        self.assertEqual(list(record["plan"]), ["name", "version", "sha256", "variables", "source"])

    def test_the_same_keys_in_the_same_order_for_every_kind_of_run(self):
        def shape(value):
            if isinstance(value, dict):
                return [(key, shape(item)) for key, item in value.items() if key not in ("variables", "point")]
            if isinstance(value, list):
                return [shape(item) for item in value[:1]]
            return None

        first = shape(self.load(self.passed()))
        self.assertEqual(shape(self.load(self.failed())), first)
        self.assertEqual(render_json(self.passed()).count("\n"), render_json(self.passed()).count("\n"))

    def test_numbers_stay_numbers_and_nothing_recorded_is_left_out(self):
        detail = self.failed()
        record = self.load(detail)
        self.assertEqual(record["run"]["status"], "fail")
        self.assertEqual(record["run"]["station"], "LINE-1")
        self.assertEqual(record["run"]["product"], "PN-100-A")
        self.assertEqual(record["run"]["operator"], "Operator A")
        self.assertEqual(record["plan"]["source"], detail["plan_source"])
        self.assertEqual(record["plan"]["variables"], {"vnom": 5.0})
        dmm = next(i for i in record["instruments"] if i["name"] == "dmm")
        self.assertEqual(dmm["asset_tag"], "DMM-0001")
        self.assertIn("SIM-DMM", dmm["idn"])
        m = record["steps"][1]["measurements"][0]
        self.assertIsInstance(m["value"], float)
        self.assertIsInstance(m["low"], float)
        self.assertEqual(m["point"], {"vin": 8.0})
        self.assertEqual(m["value"], detail["steps"][1]["measurements"][0]["value"])
        self.assertEqual(record["counts"]["fail"], 4)

    def test_aborted_partial_and_unfinished_runs(self):
        aborted = self.load(self.aborted())["run"]
        self.assertEqual(aborted["status"], "aborted")
        self.assertRegex(aborted["reason"], "closed|lost")
        partial = self.load(self.partial())["run"]
        self.assertTrue(partial["partial"])
        self.assertEqual(partial["selected_steps"], ["final_check"])
        unfinished = self.load(self.unfinished())["run"]
        self.assertEqual(unfinished["status"], "running")
        self.assertIsNone(unfinished["finished_at"])

    def test_hostile_text_and_numbers_that_json_cannot_hold(self):
        record = self.load(self.hostile())
        self.assertTrue(record["run"]["serial"].startswith("=HYPERLINK"))
        huge = record["steps"][0]["measurements"][1]
        self.assertIsNone(huge["value"])
        self.assertIsNone(huge["low"])

    def test_empty_run(self):
        record = self.load(self.empty())
        self.assertEqual(record["steps"], [])
        self.assertEqual(record["instruments"], [])
        self.assertEqual(record["counts"]["measurements"], 0)


class MarkdownReport(Runs):
    def test_sections_come_in_order(self):
        text = render_markdown(self.failed())
        positions = [text.index(h) for h in ("**Result:", "## Unit", "## Plan", "## Instruments", "## Steps", "## Failed measurements", "## All measurements")]
        self.assertEqual(positions, sorted(positions))

    def test_verdict_for_each_kind_of_run(self):
        expected = {
            "pass": "PASS",
            "fail": "FAIL",
            "aborted": "ABORTED",
            "partial": "PARTIAL",
            "unfinished": "NOT FINISHED",
            "empty": "ABORTED",
        }
        details = self.every_kind()
        for kind, word in expected.items():
            with self.subTest(kind):
                text = render_markdown(details[kind])
                self.assertIn(f"**Result: {word}.**", text)
                if kind != "pass":
                    self.assertNotIn("Result: PASS", text)
                    self.assertIn("did not pass", text.replace("has not passed", "did not pass"))

    def test_failed_run_lists_what_failed(self):
        text = render_markdown(self.failed())
        failed = text.split("## Failed measurements")[1].split("## All measurements")[0]
        self.assertEqual(failed.count("| fail |"), 4)
        self.assertIn("4.9 to 5.1", failed)
        self.assertIn("vin=8", failed)
        self.assertIn("DMM-0001", text)
        self.assertIn("LINE-1", text)

    def test_aborted_run_says_why(self):
        self.assertRegex(render_markdown(self.aborted()), "Reason: .*(closed|lost)")

    def test_partial_run_names_the_steps(self):
        text = render_markdown(self.partial())
        self.assertIn("**Steps selected:** final\\_check", text)
        self.assertIn("not a full test", text)

    def test_hostile_text_cannot_escape(self):
        text = render_markdown(self.hostile())
        self.assertNotRegex(text, r"(?<!\\)[<>]")
        self.assertIn("\\<script\\>", text)
        self.assertNotRegex(text, r"(?<!\\)@everyone")
        self.assertNotRegex(text, r"(?<!\\)\[link")
        self.assertNotRegex(text, "[\x00-\x08\x0b-\x1f\x7f\u2028]")
        for table in re.findall(r"(?:^\|.*\n)+", text, re.M):
            widths = {len(re.findall(r"(?<!\\)\|", line)) for line in table.splitlines()}
            self.assertEqual(len(widths), 1, table)

    def test_md_escapes_every_markdown_and_html_special(self):
        self.assertEqual(md("a|b*c_d`e<f>g[h]i!j#k~l&m@n$o\\p"), "a\\|b\\*c\\_d\\`e\\<f\\>g\\[h\\]i\\!j\\#k\\~l\\&m\\@n\\$o\\\\p")
        self.assertEqual(md("one\r\ntwo\nthree"), "one two three")
        self.assertEqual(md(None), "")

    def test_empty_run(self):
        text = render_markdown(self.empty())
        self.assertIn("No steps were recorded.", text)
        self.assertIn("No instruments were recorded.", text)
        self.assertIn("No measurements were recorded.", text)

    def test_many_measurements(self):
        text = render_markdown(self.many(2000))
        every = text.split("## All measurements")[1]
        self.assertEqual(sum(1 for line in every.splitlines() if "| vin=" in line), 2000)


class Balanced(HTMLParser):
    VOID = {"meta", "br", "hr", "img", "input", "link"}

    def __init__(self):
        super().__init__()
        self.stack = []
        self.tags = set()

    def handle_starttag(self, tag, attrs):
        self.tags.add(tag)
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if self.stack.pop() != tag:
            raise AssertionError(f"</{tag}> closes the wrong element")


class Certificate(Runs):
    def parse(self, text):
        parser = Balanced()
        parser.feed(text)
        parser.close()
        return parser

    def verdict_box(self, text):
        return re.search(r'<div class="verdict[^"]*" role="status">(.*?)</div>', text).group(0)

    def test_a_pass_looks_like_a_pass(self):
        text = render_certificate(self.passed())
        self.assertIn('<div class="verdict passed"', text)
        self.assertIn("<strong>PASS</strong>", text)
        self.assertNotIn("NOT PASSED", text)
        self.assertIn("<title>Test certificate: SN-OK: PASS</title>", text)

    def test_nothing_else_ever_looks_like_a_pass(self):
        details = self.every_kind()
        for kind in ("fail", "aborted", "partial", "unfinished", "empty", "hostile"):
            with self.subTest(kind):
                text = render_certificate(details[kind])
                box = self.verdict_box(text)
                self.assertNotIn("passed", box.split(">")[0])
                self.assertNotIn("<strong>PASS</strong>", box)
                self.assertIn("did not pass", box.replace("has not passed", "did not pass"))
                self.assertIn('<div class="mark" aria-hidden="true">NOT PASSED</div>', text)
                self.assertIn(": NOT PASSED</title>", text)

    def test_partial_pass_is_called_partial(self):
        text = render_certificate(self.partial())
        self.assertIn("<strong>PARTIAL</strong>", text)
        self.assertIn("final_check", text)
        self.assertIn("not a full test", text)

    def test_everything_a_reviewer_needs(self):
        detail = self.failed()
        text = render_certificate(detail)
        for expected in (
            "SN-BAD", "PN-100-A", "small", detail["plan_hash"], detail["started_at"], detail["finished_at"],
            "Operator A", "LINE-1", "DMM-0001", "SIM-DMM", "Reviewer name", "Signature", "Date", "vout_vs_vin",
        ):  # fmt: skip
            self.assertIn(expected, text)
        self.assertIn('<span class="muted">not recorded</span>', text)  # The supply has no asset tag.

    def test_self_contained_and_well_formed(self):
        for kind, detail in self.every_kind().items():
            with self.subTest(kind):
                text = render_certificate(detail)
                parser = self.parse(text)
                self.assertEqual(parser.stack, [])
                self.assertFalse(parser.tags & {"script", "img", "link", "iframe"})
                self.assertNotIn("src=", text)

    def test_hostile_text_is_escaped(self):
        text = render_certificate(self.hostile())
        self.assertNotIn("<script", text)
        self.assertNotIn("<b>idn", text)
        self.assertIn("&lt;script&gt;", text)
        self.assertNotRegex(text, "[\x00-\x08\x0b-\x1f]")

    def test_a_long_plan_still_fits_one_page_and_lists_what_did_not_pass(self):
        detail = copy.deepcopy(self.failed())
        template = detail["steps"][1]
        steps = []
        for index in range(30):
            step = dict(template, name=f"step_{index:02d}", status="pass", measurements=[])
            steps.append(step)
        steps[17]["status"] = "fail"
        detail["steps"] = steps
        text = render_certificate(detail)
        listed = re.findall(r"<tr><td>(step_\d+)</td>", text)
        self.assertEqual(listed, ["step_17"])
        self.assertIn("29 of the 30 steps passed", text)
        self.assertLessEqual(len(listed), MAX_STEPS_LISTED)

    def test_empty_run(self):
        text = render_certificate(self.empty())
        self.assertIn("No steps were recorded.", text)
        self.assertIn("no instrument answered", text)


class CsvReport(Runs):
    def test_formula_looking_text_is_neutralised(self):
        rows = list(csv.DictReader(io.StringIO(render_csv(self.hostile()))))
        self.assertTrue(rows[0]["serial"].startswith("'=HYPERLINK"))
        self.assertTrue(rows[0]["plan"].startswith("'+plan"))
        self.assertTrue(rows[0]["plan_version"].startswith("'-1"))
        self.assertTrue(rows[0]["note"].startswith("'=1+1"))

    def test_streaming_writer_matches_the_single_report(self):
        details = [self.passed(), self.failed()]
        buffer = io.StringIO()
        write_csv(buffer, iter(details))
        self.assertEqual(buffer.getvalue(), render_many(details))


def read_sheet(archive, number):
    """A worksheet as a list of rows, each a dict of column letter to (value, cell element)."""
    root = ET.fromstring(archive.read(f"xl/worksheets/sheet{number}.xml"))
    rows = []
    for row in root.iter(f"{MAIN_NS}row"):
        cells = {}
        for cell in row:
            letters = re.match(r"[A-Z]+", cell.get("r")).group(0)
            if cell.get("t") == "inlineStr":
                value = "".join(t.text or "" for t in cell.iter(f"{MAIN_NS}t"))
            else:
                value = float(cell.find(f"{MAIN_NS}v").text)
            cells[letters] = (value, cell)
        rows.append(cells)
    return root, rows


class Export(Runs):
    def export(self, details, export_format, name=None):
        path = self.folder / (name or f"out.{export_format}")
        export_runs(iter(details), path, export_format)
        return path

    def test_formats(self):
        self.assertEqual(EXPORT_FORMATS, ("csv", "jsonl", "xlsx"))

    def test_csv_export_is_byte_for_byte_the_old_one(self):
        details = [self.passed(), self.failed(), self.empty()]
        path = self.export(details, "csv")
        self.assertEqual(path.read_bytes(), render_many(details).encode("utf-8"))

    def test_jsonl_has_the_rows_csv_has_with_types_kept(self):
        details = list(self.every_kind().values())
        csv_rows = list(csv.DictReader(io.StringIO(render_many(details))))
        lines = self.export(details, "jsonl").read_bytes().split(b"\n")
        self.assertEqual(lines[-1], b"")
        rows = [json.loads(line, parse_constant=float) for line in lines[:-1]]
        self.assertEqual(len(rows), len(csv_rows))
        for line in lines:
            line.decode("ascii")
        first = rows[0]
        self.assertEqual(first["serial"], "SN-OK")
        self.assertEqual(first["station"], "LINE-1")
        self.assertIsInstance(first["value"], float)
        self.assertIsInstance(first["point"], dict)
        self.assertFalse(first["partial"])
        self.assertTrue(any(row["partial"] for row in rows))
        empty = next(row for row in rows if row["serial"] == "SN-EMPTY")
        self.assertIsNone(empty["step"])
        self.assertIsNone(empty["measurement"])
        huge = next(row for row in rows if row["measurement"] == "huge")
        self.assertIsNone(huge["value"])
        hostile = next(row for row in rows if row["serial"].startswith("=HYPERLINK"))
        self.assertIn("\u2028", hostile["run_note"])

    def test_writers_take_one_run_at_a_time(self):
        details = [self.passed(), self.failed()]
        for writer in (write_csv, write_jsonl):
            buffer = io.StringIO()

            def one_at_a_time():
                yield details[0]
                self.assertIn("SN-OK", buffer.getvalue(), writer.__name__)
                yield details[1]

            writer(buffer, one_at_a_time())
            self.assertIn("SN-BAD", buffer.getvalue())

    def test_xlsx_package_parts(self):
        path = self.export([self.passed(), self.failed()], "xlsx")
        with zipfile.ZipFile(path) as archive:
            self.assertIsNone(archive.testzip())
            names = set(archive.namelist())
            types = ET.fromstring(archive.read("[Content_Types].xml"))
            ns = "{http://schemas.openxmlformats.org/package/2006/content-types}"
            overrides = {o.get("PartName"): o.get("ContentType") for o in types.iter(f"{ns}Override")}
            for part in overrides:
                self.assertIn(part.lstrip("/"), names)
            self.assertTrue(overrides["/xl/workbook.xml"].endswith("sheet.main+xml"))
            self.assertTrue(overrides["/xl/styles.xml"].endswith("styles+xml"))
            self.assertEqual(sum(1 for t in overrides.values() if t.endswith("worksheet+xml")), 3)
            defaults = {d.get("Extension") for d in types.iter(f"{ns}Default")}
            self.assertEqual(defaults, {"rels", "xml"})
            rel_ns = "{http://schemas.openxmlformats.org/package/2006/relationships}"
            root_rel = ET.fromstring(archive.read("_rels/.rels")).find(f"{rel_ns}Relationship")
            self.assertEqual(root_rel.get("Target"), "xl/workbook.xml")
            self.assertTrue(root_rel.get("Type").endswith("/officeDocument"))
            links = {
                r.get("Id"): r.get("Target") for r in ET.fromstring(archive.read("xl/_rels/workbook.xml.rels")).iter(f"{rel_ns}Relationship")
            }
            for target in links.values():
                self.assertIn(f"xl/{target}", names)
            workbook = ET.fromstring(archive.read("xl/workbook.xml"))
            r_id = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
            sheets = [(s.get("name"), links[s.get(r_id)]) for s in workbook.iter(f"{MAIN_NS}sheet")]
            self.assertEqual(
                sheets,
                [("Runs", "worksheets/sheet1.xml"), ("Steps", "worksheets/sheet2.xml"), ("Measurements", "worksheets/sheet3.xml")],
            )
            styles = ET.fromstring(archive.read("xl/styles.xml"))
            fonts = list(styles.find(f"{MAIN_NS}fonts"))
            xfs = list(styles.find(f"{MAIN_NS}cellXfs"))
            self.assertIsNotNone(fonts[int(xfs[xlsx.STYLE_HEADER].get("fontId"))].find(f"{MAIN_NS}b"))
            self.assertEqual(xfs[xlsx.STYLE_QUOTED].get("quotePrefix"), "1")
            self.assertEqual(int(styles.find(f"{MAIN_NS}cellXfs").get("count")), len(xfs))

    def test_xlsx_sheets_hold_the_runs(self):
        passed, failed = self.passed(), self.failed()
        path = self.export([passed, failed], "xlsx")
        with zipfile.ZipFile(path) as archive:
            root, runs = read_sheet(archive, 1)
            _, steps = read_sheet(archive, 2)
            _, measurements = read_sheet(archive, 3)
        pane = root.find(f"{MAIN_NS}sheetViews/{MAIN_NS}sheetView/{MAIN_NS}pane")
        self.assertEqual((pane.get("state"), pane.get("ySplit"), pane.get("topLeftCell")), ("frozen", "1", "A2"))
        widths = [float(col.get("width")) for col in root.iter(f"{MAIN_NS}col")]
        self.assertEqual(len(widths), len(xlsx.RUN_COLUMNS))
        self.assertTrue(all(6 <= width <= 60 for width in widths))
        header = runs[0]
        self.assertEqual([header[k][0] for k in sorted(header, key=lambda c: (len(c), c))], [name for name, _ in xlsx.RUN_COLUMNS])
        self.assertTrue(all(cell.get("s") == str(xlsx.STYLE_HEADER) for _, cell in header.values()))
        self.assertEqual(len(runs), 3)
        self.assertEqual(runs[1]["A"][0], float(passed["id"]))
        self.assertIsNone(runs[1]["A"][1].get("t"))
        self.assertEqual(runs[1]["C"][0], "SN-OK")
        self.assertEqual(runs[2]["I"][0], "fail")
        self.assertEqual(runs[1]["O"][0], passed["started_at"])
        self.assertEqual(runs[1]["O"][1].get("t"), "inlineStr")
        self.assertEqual(len(steps), 1 + len(passed["steps"]) + len(failed["steps"]))
        all_values = [m["value"] for d in (passed, failed) for s in d["steps"] for m in s["measurements"]]
        self.assertEqual(len(measurements), 1 + len(all_values))
        self.assertEqual([row["G"][0] for row in measurements[1:]], all_values)
        self.assertEqual(measurements[1]["F"][0], "vin=8")
        self.assertEqual(measurements[1]["I"][0], 4.9)

    def test_xlsx_hostile_text(self):
        path = self.export([self.hostile()], "xlsx")
        with zipfile.ZipFile(path) as archive:
            raw = b"".join(archive.read(f"xl/worksheets/sheet{n}.xml") for n in (1, 2, 3))
            _, runs = read_sheet(archive, 1)
            _, measurements = read_sheet(archive, 3)
        self.assertNotIn(b"<f>", raw)
        self.assertNotIn(b"<script", raw)
        self.assertNotRegex(raw.decode("utf-8"), "[\x00-\x08\x0b\x0c\x0e-\x1f]")
        serial, cell = runs[1]["C"]
        self.assertTrue(serial.startswith('=HYPERLINK("http://example.invalid")<script>'))
        self.assertNotIn("\x07", serial)
        self.assertEqual(cell.get("t"), "inlineStr")
        self.assertEqual(cell.get("s"), str(xlsx.STYLE_QUOTED))
        self.assertTrue(runs[1]["N"][0].startswith("line one\nline two\r\u2028<script>"))
        self.assertEqual(measurements[1]["D"][1].get("s"), str(xlsx.STYLE_QUOTED))
        self.assertEqual(measurements[1]["H"][1].get("s"), str(xlsx.STYLE_QUOTED))
        self.assertNotIn("G", measurements[2])  # Infinity is left empty, never written as text.

    def test_xlsx_cells(self):
        self.assertEqual(xlsx.column_letters(0), "A")
        self.assertEqual(xlsx.column_letters(25), "Z")
        self.assertEqual(xlsx.column_letters(26), "AA")
        self.assertEqual(xlsx.column_letters(16383), "XFD")
        self.assertEqual(xlsx.cell_xml("B2", None), "")
        self.assertEqual(xlsx.cell_xml("B2", 3), '<c r="B2"><v>3</v></c>')
        self.assertEqual(xlsx.cell_xml("B2", 0.1), '<c r="B2"><v>0.1</v></c>')
        self.assertEqual(xlsx.cell_xml("B2", float("nan")), "")
        self.assertIn("<t xml:space=\"preserve\">a &amp; b &lt;c&gt;</t>", xlsx.cell_xml("B2", "a & b <c>"))
        self.assertIn("&#13;", xlsx.cell_xml("B2", "a\rb"))
        self.assertEqual(len(xlsx.clean_text("x" * 40000)), xlsx.MAX_CELL_CHARS)
        self.assertEqual(xlsx.clean_text("a\x00b\ufffec"), "abc")

    def test_xlsx_empty_run_and_many_measurements(self):
        path = self.export([self.empty(), self.many(3000)], "xlsx")
        with zipfile.ZipFile(path) as archive:
            _, runs = read_sheet(archive, 1)
            _, measurements = read_sheet(archive, 3)
        self.assertEqual(len(runs), 3)
        self.assertEqual(runs[1]["C"][0], "SN-EMPTY")
        self.assertEqual(len(measurements), 1 + 3000 + 1)  # The last is the final check, after the sweep.
        self.assertEqual(measurements[3000]["F"][0], "vin=2999")

    def test_xlsx_row_limit_is_a_plain_error_and_writes_nothing(self):
        path = self.folder / "big.xlsx"
        with mock.patch.object(xlsx, "MAX_ROWS", 3), self.assertRaises(PlanError) as caught:
            export_runs(iter([self.failed()]), path, "xlsx")
        self.assertIn("Measurements sheet would need more than 3 rows", caught.exception.problems[0])
        self.assertFalse(path.exists())
        self.assertEqual(list(self.folder.glob(".big.xlsx*")), [])

    def test_xlsx_is_the_same_for_the_same_runs(self):
        details = [self.failed()]
        first = self.export(details, "xlsx", "one.xlsx").read_bytes()
        self.assertEqual(self.export(details, "xlsx", "two.xlsx").read_bytes(), first)


class Commands(BenchTestCase):
    def setUp(self):
        super().setUp()
        self.db = self.folder / "cli.sqlite"

    def sim_run(self, *extra, serial="SN-1001"):
        return call("run", QUICK, "--serial", serial, "--sim", "--db", self.db, *extra)

    def test_run_writes_the_chosen_reports(self):
        out = self.folder / "out"
        code, _, err = self.sim_run("--report-dir", out, "--report-format", "certificate", "--report-format", "json")
        self.assertEqual(code, EXIT_PASS, err)
        names = sorted(p.name for p in out.iterdir())
        self.assertEqual(len(names), 2)
        self.assertTrue(names[0].endswith("-certificate.html"))
        self.assertTrue(names[1].endswith(".json"))
        self.assertEqual(names[0].replace("-certificate.html", ""), names[1].replace(".json", ""))

    def test_run_report_format_needs_a_report_dir(self):
        code, _, err = self.sim_run("--report-format", "json")
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("--report-dir", err)
        self.assertFalse(self.db.exists())

    def test_report_format_is_repeatable_and_every_means_every(self):
        self.sim_run()
        out = self.folder / "rep"
        code, text, _ = call("report", "--latest", "--format", "markdown", "--format", "json", "--out", out, "--db", self.db)
        self.assertEqual(code, EXIT_PASS)
        self.assertEqual(sorted(p.suffix for p in out.iterdir()), [".json", ".md"])
        self.assertEqual(text.count("Report: "), 2)
        code, _, _ = call("report", "--latest", "--format", "every", "--out", self.folder / "every", "--db", self.db)
        self.assertEqual(len(list((self.folder / "every").iterdir())), 6)
        code, _, _ = call("report", "--latest", "--format", "all", "--out", self.folder / "all", "--db", self.db)
        self.assertEqual(sorted(p.suffix for p in (self.folder / "all").iterdir()), [".csv", ".html", ".xml"])

    def test_unknown_format_is_refused(self):
        self.sim_run()
        code, _, err = call("report", "--latest", "--format", "pdf", "--db", self.db)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("invalid choice", err)
        code, _, _ = call("export", "--out", self.folder / "x.pdf", "--format", "pdf", "--db", self.db)
        self.assertEqual(code, EXIT_USAGE)

    def test_export_each_format(self):
        self.sim_run(serial="SN-1001")
        self.sim_run("--dut", "high-output", serial="SN-1002")
        for export_format in EXPORT_FORMATS:
            with self.subTest(export_format):
                path = self.folder / f"all.{export_format}"
                code, text, err = call("export", "--out", path, "--format", export_format, "--db", self.db)
                self.assertEqual(code, EXIT_PASS, err)
                self.assertIn("Wrote 2 runs", text)
                self.assertGreater(path.stat().st_size, 200)
                code, _, err = call("export", "--out", path, "--format", export_format, "--db", self.db)
                self.assertEqual(code, EXIT_USAGE)
                self.assertIn("--force", err)
        with zipfile.ZipFile(self.folder / "all.xlsx") as archive:
            _, runs = read_sheet(archive, 1)
        self.assertEqual([row["C"][0] for row in runs[1:]], ["SN-1001", "SN-1002"])

    def test_export_row_limit_exits_with_a_plain_message(self):
        self.sim_run()
        path = self.folder / "big.xlsx"
        with mock.patch.object(xlsx, "MAX_ROWS", 2):
            code, _, err = call("export", "--out", path, "--format", "xlsx", "--db", self.db)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("the most a worksheet can hold", err)
        self.assertTrue(err.startswith("Cannot continue:"), err)
        self.assertFalse(path.exists())
