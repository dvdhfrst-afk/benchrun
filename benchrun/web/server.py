"""The HTTP server: checking each request, routing it, the security headers, downloads and the launcher's forms.

Nothing in a request becomes a file path, a header or a command line. Request
targets with traversal or encoded separators are refused, request sizes are
capped, every page reads the database through a read-only connection, and every
request that changes anything is a POST carrying this server's token from a page
this server served.
"""

from __future__ import annotations

import hmac
import http.server
import os
import re
import secrets
import shutil
import socket
import socketserver
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any, BinaryIO, Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import parse_qsl, unquote, urlsplit

from benchrun.archive import write_archive
from benchrun.console import CONTROL, complain
from benchrun.errors import PlanError
from benchrun.reports import REGISTRY, report_stem
from benchrun.reports.export import EXPORT_FORMATS, export_runs
from benchrun.web import api, data, pages
from benchrun.web.launcher import Launcher
from benchrun.web.markup import STYLESHEET, Markup, esc, page

MAX_TARGET = 4096
MAX_BODY = 16 * 1024
MAX_FIELDS = 64
REQUEST_TIMEOUT_S = 30
COPY_CHUNK = 64 * 1024

PAGE_POLICY = "default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
# A report is a page of its own with its style inside it. Sandboxed, it can do nothing but be read.
DOWNLOAD_POLICY = "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'; sandbox"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".xml": "application/xml; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".jsonl": "application/x-ndjson; charset=utf-8",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".zip": "application/zip",
}
LOOPBACK_NAMES = ("localhost", "127.0.0.1", "[::1]")
_HOST = re.compile(r"(?:[A-Za-z0-9.-]{1,253}|\[[0-9A-Fa-f:.]{2,45}\])(?::\d{1,5})?")
_BAD_TARGET = re.compile(r"%2f|%5c|%00|\\", re.IGNORECASE)
_ID = re.compile(r"[0-9]{1,18}")


class HttpError(Exception):
    def __init__(self, status: int, message: str, allow: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.allow = allow


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    content_type: str = "text/html; charset=utf-8"
    headers: Dict[str, str] = field(default_factory=dict)
    file: Optional[Path] = None  # Sent in pieces, then its folder is removed.
    policy: str = PAGE_POLICY


@dataclass
class Request:
    method: str
    segments: List[str]
    query: Dict[str, str]
    form: Dict[str, str]


def parse_target(target: str) -> Tuple[List[str], Dict[str, str]]:
    """The decoded path segments and the query. Raises HttpError for anything a browser would not send."""
    if len(target) > MAX_TARGET:
        raise HttpError(414, f"the request target is longer than {MAX_TARGET} characters")
    if not target.startswith("/") or target.startswith("//") or CONTROL.search(target) or " " in target:
        raise HttpError(400, "the request target must be a path on this server")
    parts = urlsplit(target)
    if _BAD_TARGET.search(parts.path):
        raise HttpError(400, "the request target holds an encoded separator or a backslash")
    segments = [] if parts.path == "/" else parts.path[1:].split("/")
    decoded = []
    for segment in segments:
        try:
            text = unquote(segment, errors="strict")
        except UnicodeDecodeError:
            raise HttpError(400, "the request target is not valid UTF-8") from None
        if text in (".", "..") or CONTROL.search(text):
            raise HttpError(400, "the request target must not step out of a folder")
        decoded.append(text)
    return decoded, parse_fields(parts.query)


def parse_fields(text: str) -> Dict[str, str]:
    try:
        pairs = parse_qsl(text, keep_blank_values=True, max_num_fields=MAX_FIELDS, errors="strict")
    except (ValueError, UnicodeDecodeError):
        raise HttpError(400, f"the query or form must be valid UTF-8 with at most {MAX_FIELDS} fields") from None
    found: Dict[str, str] = {}
    for key, value in pairs:
        if key in found:
            raise HttpError(400, f"give {key} only once")
        found[key] = value
    return found


def _whole(segment: str, what: str) -> int:
    if not _ID.fullmatch(segment):
        raise pages.NotFound(f"no {what} {segment!r}")
    return int(segment)


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _attachment(name: str) -> Dict[str, str]:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)  # Built by the server; this only makes sure.
    return {"Content-Disposition": f'attachment; filename="{safe}"'}


class App:
    """Everything one server knows: its database, its token, whether runs may start, and who may connect."""

    def __init__(self, db: Path, launcher: Optional[Launcher] = None, public: bool = False, quiet: bool = False):
        self.db = db
        self.site = pages.Site(token=secrets.token_urlsafe(32), launcher=launcher)
        self.public = public
        self.quiet = quiet
        self.bound_host = "127.0.0.1"
        self.port = 0

    # Checks ----------------------------------------------------------------

    def check_host(self, host: Optional[str]) -> None:
        """Only names of this computer, unless it serves the network, so a page elsewhere cannot rebind a name to it."""
        if not host or not _HOST.fullmatch(host):
            raise HttpError(400, "the Host header is missing or is not a host name")
        if self.public:
            return
        name, _, port = host.rpartition(":") if not host.endswith("]") else (host, "", "")
        if not name:
            name, port = host, ""
        allowed = LOOPBACK_NAMES + (self.bound_host.lower(),)
        if name.lower() not in allowed or (port and int(port) != self.port) or (not port and self.port != 80):
            raise HttpError(421, "this server only answers requests for localhost, 127.0.0.1 or [::1] on its own port")

    def read_form(self, headers: Mapping[str, str], rfile: BinaryIO) -> Dict[str, str]:
        if headers.get("Transfer-Encoding"):
            raise HttpError(411, "send the form with a Content-Length")
        length = headers.get("Content-Length")
        if length is None:
            raise HttpError(411, "send the form with a Content-Length")
        if not _ID.fullmatch(length.strip()):
            raise HttpError(400, "the Content-Length is not a number")
        if int(length) > MAX_BODY:
            raise HttpError(413, f"a form may be at most {MAX_BODY} bytes")
        kind = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if kind != "application/x-www-form-urlencoded":
            raise HttpError(415, "send the form as application/x-www-form-urlencoded")
        body = rfile.read(int(length))
        try:
            return parse_fields(body.decode("ascii"))
        except UnicodeDecodeError:
            raise HttpError(400, "the form must be URL-encoded") from None

    def check_origin(self, headers: Mapping[str, str], form: Mapping[str, str]) -> None:
        """A state change must come from a page of this server, and carry the token that page was given."""
        own = f"http://{headers.get('Host')}"
        origin = headers.get("Origin")
        if origin is not None:
            same = origin == own
        else:
            referer = headers.get("Referer") or ""
            same = referer == own or referer.startswith(own + "/")
        if not same:
            raise HttpError(403, "this request did not come from a page of this server")
        token = form.get("token", "")
        if not hmac.compare_digest(token.encode("utf-8", "replace"), self.site.token.encode("ascii")):
            raise HttpError(403, "this form is out of date or did not come from this server; load the page again")

    # Answering ---------------------------------------------------------------

    def respond(self, method: str, target: str, headers: Mapping[str, str], rfile: BinaryIO) -> Response:
        is_api = target == "/api" or target.startswith(("/api/", "/api?"))
        try:
            segments, query = parse_target(target)
            self.check_host(headers.get("Host"))
            if method not in ("GET", "HEAD", "POST"):
                raise HttpError(405, f"{method} is not used here", allow="GET, HEAD, POST")
            form: Dict[str, str] = {}
            if method == "POST":
                form = self.read_form(headers, rfile)
                self.check_origin(headers, form)
            request = Request(method, segments, query, form)
            if segments == ["static", "style.css"]:
                return Response(body=STYLESHEET.encode("utf-8"), content_type="text/css; charset=utf-8")
            conn = data.connect_read_only(self.db)
            try:
                return self.route(conn, request)
            finally:
                conn.close()
        except HttpError as exc:
            return self.error(is_api, exc.status, exc.message, exc.allow)
        except pages.NotFound as exc:
            return self.error(is_api, 404, str(exc))
        except PlanError as exc:
            return self.error(is_api, 400, "; ".join(exc.problems))
        except sqlite3.Error as exc:
            return self.error(is_api, 503, f"the results database cannot be read: {exc}")
        except Exception as exc:  # The server goes on; this one request gets a 500.
            complain(f"error: internal error in benchrun answering {method} {target[:200]}: {exc!r}")
            return self.error(is_api, 500, "internal error in benchrun; the server log says more")

    def error(self, is_api: bool, status: int, message: str, allow: Optional[str] = None) -> Response:
        phrase = HTTPStatus(status).phrase
        headers = {"Allow": allow} if allow else {}
        if is_api:
            code = phrase.lower().replace(" ", "_").replace("-", "_")
            return Response(status, api.error_body(status, code, message), "application/json; charset=utf-8", headers)
        body = Markup(f"<h1>{esc(phrase)}</h1><p>{esc(message)}</p>")
        return Response(status, page(phrase, body, runs_allowed=self.site.runs_allowed).encode("utf-8"), headers=headers)

    def route(self, conn: sqlite3.Connection, request: Request) -> Response:
        s, q = request.segments, request.query
        if request.method == "POST":
            return self.post(conn, request)
        if s[:1] == ["api"]:
            return self.api(conn, s[1:], q)
        if s[:1] == ["download"]:
            return self.download(conn, s[1:], q)
        site = self.site
        listed: Dict[Tuple[str, ...], Callable[[pages.Site, sqlite3.Connection, Mapping[str, str]], str]] = {
            (): pages.dashboard,
            ("runs",): pages.runs,
            ("plans",): pages.plans,
            ("stats",): pages.stats,
            ("lots",): pages.lots,
            ("stations",): pages.station_list,
            ("instruments",): pages.instrument_list,
            ("audit",): pages.audit_log,
        }
        if tuple(s) in listed:
            return self.html(listed[tuple(s)](site, conn, q))
        if s == ["launch"]:
            return self.html(pages.launch(site, conn))
        if s[:1] == ["launch"] and len(s) == 2 and s[1] in ("stop", "continue"):
            raise HttpError(405, "use the buttons on the launch page", allow="POST")
        if s == ["products"]:
            part = q.get("part")
            return self.html(pages.product(site, conn, part) if part else pages.product_list(site, conn, q))
        if s == ["serials"]:
            return self.html(pages.serial(site, conn, self.need(q, "serial"), q))
        if len(s) == 2:
            kind, name = s
            if kind == "runs":
                return self.html(pages.run(site, conn, _whole(name, "run")))
            if kind == "serials":
                return self.html(pages.serial(site, conn, name, q))
            if kind == "lots":
                return self.html(pages.lot(site, conn, _whole(name, "lot")))
            if kind == "instruments":
                return self.html(pages.instrument(site, conn, name))
            if kind == "products":
                return self.html(pages.product(site, conn, name))
        if len(s) == 4 and s[0] == "runs" and s[2] == "report":
            return self.report(conn, _whole(s[1], "run"), s[3])
        raise pages.NotFound(f"nothing here at /{'/'.join(s)}")

    @staticmethod
    def need(query: Mapping[str, str], name: str) -> str:
        value = query.get(name, "")
        if not value:
            raise HttpError(400, f"say which with {name}=")
        return value

    @staticmethod
    def html(text: str, status: int = 200) -> Response:
        return Response(status, text.encode("utf-8"))

    def api(self, conn: sqlite3.Connection, s: List[str], q: Dict[str, str]) -> Response:
        listed: Dict[str, Callable[[sqlite3.Connection, Mapping[str, str]], Dict[str, Any]]] = {
            "runs": api.runs,
            "plans": api.plans,
            "stats": api.stats,
            "lots": api.lots,
            "stations": api.stations,
            "instruments": api.instruments,
            "products": api.product_list,
            "audit": api.audit,
        }
        payload: Optional[Dict[str, Any]] = None
        if not s:
            payload = api.index(conn, q)
        elif len(s) == 1 and s[0] in listed:
            payload = listed[s[0]](conn, q)
        elif s == ["serials"]:
            payload = api.serial(conn, self.need(q, "serial"), q)
        elif len(s) == 2:
            kind, name = s
            if kind == "runs":
                payload = api.run(conn, _whole(name, "run"))
            elif kind == "serials":
                payload = api.serial(conn, name, q)
            elif kind == "lots":
                payload = api.lot(conn, _whole(name, "lot"))
            elif kind == "instruments":
                payload = api.instrument(conn, name)
            elif kind == "products":
                payload = api.product(conn, name)
        if payload is None:
            raise pages.NotFound(f"no API endpoint /api/{'/'.join(s)}; /api lists them")
        return Response(body=api.encode(payload), content_type="application/json; charset=utf-8")

    # Downloads ---------------------------------------------------------------

    def report(self, conn: sqlite3.Connection, run_id: int, name: str) -> Response:
        report_format = REGISTRY.get(name)
        if report_format is None:
            raise pages.NotFound(f"no report format {name!r}; the formats are {', '.join(REGISTRY)}")
        detail = data.ReadOnlyStore(conn).run_detail(run_id)
        if detail is None:
            raise pages.NotFound(f"no run with id {run_id}")
        file_name = report_stem(detail) + report_format.ending
        return Response(
            body=report_format.render(detail).encode("utf-8"),
            content_type=CONTENT_TYPES[Path(file_name).suffix],
            headers=_attachment(file_name),
            policy=DOWNLOAD_POLICY,
        )

    def download(self, conn: sqlite3.Connection, s: List[str], q: Dict[str, str]) -> Response:
        if s not in (["export"], ["archive"]):
            raise pages.NotFound("the downloads are /download/export and /download/archive")
        export_format = q.get("format", "csv") if s == ["export"] else "zip"
        if s == ["export"] and export_format not in EXPORT_FORMATS:
            raise HttpError(400, f"format must be one of {', '.join(EXPORT_FORMATS)}")
        chosen = data.run_filter(conn, {k: v for k, v in q.items() if k != "format"})
        ids = data.run_ids(conn, chosen)
        if not ids:
            raise pages.NotFound("no matching runs, so there is nothing to download")
        store = data.ReadOnlyStore(conn)
        folder = Path(tempfile.mkdtemp(prefix="benchrun-web-"))
        try:
            kind = "archive" if s == ["archive"] else "export"
            out = folder / f"benchrun-{kind}-{_stamp()}.{export_format}"
            if s == ["archive"]:
                write_archive(store, ids, out)
            else:
                # One run in memory at a time, however many there are.
                export_runs((store.run_detail(run_id) for run_id in ids), out, export_format)
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        return Response(content_type=CONTENT_TYPES[out.suffix], headers=_attachment(out.name), file=out, policy=DOWNLOAD_POLICY)

    # Starting runs ---------------------------------------------------------------

    def post(self, conn: sqlite3.Connection, request: Request) -> Response:
        launcher = self.site.launcher
        s = request.segments
        if s not in (["launch"], ["launch", "stop"], ["launch", "continue"]):
            raise HttpError(405, "nothing here takes a POST", allow="GET, HEAD")
        if launcher is None:
            raise HttpError(403, "starting runs from the browser is switched off; start the server with --allow-runs --plans-dir DIR")
        if s == ["launch", "stop"]:
            launcher.stop()
        elif s == ["launch", "continue"]:
            launcher.answer()
        else:
            try:
                chosen = launcher.check(conn, request.form)
            except PlanError as exc:
                return self.html(pages.launch(self.site, conn, request.form, exc.problems), 400)
            try:
                launcher.start(chosen)
            except RuntimeError as exc:
                return self.html(pages.launch(self.site, conn, request.form, [str(exc)]), 409)
            except OSError as exc:
                return self.html(pages.launch(self.site, conn, request.form, [f"the run could not be started: {exc}"]), 500)
        return Response(303, headers={"Location": "/launch"})


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "benchrun"
    sys_version = ""
    timeout = REQUEST_TIMEOUT_S
    server: "Server"

    def version_string(self) -> str:
        return self.server_version

    def do_GET(self) -> None:
        self.serve("GET")

    def do_HEAD(self) -> None:
        self.serve("HEAD")

    def do_POST(self) -> None:
        self.serve("POST")

    def do_PUT(self) -> None:
        self.serve("PUT")

    def do_DELETE(self) -> None:
        self.serve("DELETE")

    def do_PATCH(self) -> None:
        self.serve("PATCH")

    def do_OPTIONS(self) -> None:
        self.serve("OPTIONS")

    def serve(self, method: str) -> None:
        response = self.server.app.respond(method, self.path, self.headers, self.rfile)
        self.write(response, with_body=method != "HEAD")

    def send_error(self, code: int, message: Optional[str] = None, explain: Optional[str] = None) -> None:
        """Errors found before a request reaches the app, such as a request line that is too long."""
        self.close_connection = True
        text = message if message and code != 400 else HTTPStatus(code).description
        self.write(self.server.app.error(False, code, text), with_body=self.command != "HEAD")

    def write(self, response: Response, with_body: bool = True) -> None:
        size = response.file.stat().st_size if response.file is not None else len(response.body)
        try:
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Content-Security-Policy", response.policy)
            for name, value in response.headers.items():
                self.send_header(name, value)
            self.end_headers()
            if not with_body:
                return
            if response.file is None:
                self.wfile.write(response.body)
            else:
                with response.file.open("rb") as handle:
                    shutil.copyfileobj(handle, self.wfile, COPY_CHUNK)
        except (ConnectionError, socket.timeout):
            self.close_connection = True  # The browser went away; nothing to tell it.
        finally:
            if response.file is not None:
                shutil.rmtree(response.file.parent, ignore_errors=True)

    def log_message(self, format: str, *args: Any) -> None:
        if not self.server.app.quiet:
            complain(f"{self.address_string()} - {format % args}")


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"  # On Windows it would let a second server take the same port.

    def __init__(self, host: str, port: int, app: App):
        self.address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
        self.app = app
        super().__init__((host.strip("[]"), port), Handler)
        app.bound_host = host if ":" not in host else f"[{host.strip('[]')}]"
        app.port = self.server_address[1]

    def server_bind(self) -> None:
        # The parent also looks up the host's full name, which can take seconds and is never used here.
        socketserver.TCPServer.server_bind(self)
        self.server_name = str(self.server_address[0])
        self.server_port = self.server_address[1]
