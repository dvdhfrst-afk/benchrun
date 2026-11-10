"""``benchrun serve``: the web interface and HTTP API over a results database."""

from __future__ import annotations

import argparse
import contextlib
import signal
import socket
from pathlib import Path
from typing import Any, Iterator, Optional

from benchrun.console import DEFAULT_DB, EXIT_ABORTED, EXIT_PASS, complain, open_store, say
from benchrun.errors import PlanError
from benchrun.web.launcher import Launcher
from benchrun.web.server import App, Server

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def is_loopback(host: str) -> bool:
    """True when every address the name stands for is this computer only."""
    try:
        found = socket.getaddrinfo(host.strip("[]"), None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError):
        raise PlanError([f"--host {host} is not an address of this computer"]) from None
    addresses = {str(info[4][0]) for info in found}
    return bool(addresses) and all(address.startswith("127.") or address == "::1" for address in addresses)


def url_of(host: str, port: int) -> str:
    shown = f"[{host.strip('[]')}]" if ":" in host else host
    return f"http://{shown}:{port}/"


@contextlib.contextmanager
def _stop_requests_interrupt() -> Iterator[None]:
    """A service stop, or Ctrl+Break on Windows, ends the server the way Ctrl+C does, so a run it started is stopped first."""

    def interrupt(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt()

    previous = {}
    for name in ("SIGTERM", "SIGBREAK", "SIGHUP"):
        number = getattr(signal, name, None)
        if number is not None:
            with contextlib.suppress(ValueError, OSError):
                previous[number] = signal.signal(number, interrupt)
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def make_app(args: argparse.Namespace) -> App:
    """Check the options, prepare the database, and build the app. Raises PlanError with every problem."""
    problems = []
    if not 0 <= args.port <= 65535:
        problems.append(f"--port {args.port} must be from 0 to 65535 (0 means any free port)")
    if args.allow_runs and not args.plans_dir:
        problems.append("--allow-runs needs --plans-dir DIR, the folder whose .json plans the browser may start")
    if args.plans_dir and not args.allow_runs:
        problems.append("--plans-dir only applies with --allow-runs")
    if args.plans_dir and not Path(args.plans_dir).is_dir():
        problems.append(f"--plans-dir {args.plans_dir} is not a folder")
    if not Path(args.db).exists() and not args.allow_runs:
        problems.append(f"no results database at {args.db}")
    if not problems and not args.public and not is_loopback(args.host):
        problems.append(f"--host {args.host} can be reached from other computers, and there is no login; add --public if that is what you want")
    if problems:
        raise PlanError(problems)
    with open_store(args.db):
        pass  # Created, or brought up to the current schema, once; every page then reads it read-only.
    db = Path(args.db).resolve()
    launcher = Launcher(db, Path(args.plans_dir).resolve()) if args.allow_runs else None
    return App(db, launcher, public=args.public, quiet=args.quiet)


def cmd_serve(args: argparse.Namespace) -> int:
    app = make_app(args)
    try:
        server = Server(args.host, args.port, app)
    except OSError as exc:
        complain(f"error: cannot listen on {args.host} port {args.port}: {exc}")
        complain("Another program may hold that port. Try --port with another number, or 0 for any free port.")
        return EXIT_ABORTED
    problem: Optional[str] = None
    try:
        if args.public:
            complain("Warning: --public serves this database to anyone who can reach this computer. There is no login.")
            if app.site.launcher is not None:
                complain("Warning: with --allow-runs, anyone who can reach it can also start runs on this bench.")
        say(f"benchrun web interface for {app.db}")
        say(f"  {url_of(args.host, app.port)}")
        if app.site.launcher is not None:
            say(f"  Runs can be started from the browser with the plans in {app.site.launcher.plans_dir}")
        say("Press Ctrl+C to stop.")
        try:
            with _stop_requests_interrupt():
                server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            say("Stopping.")
    finally:
        if app.site.launcher is not None:
            problem = app.site.launcher.close()
        server.server_close()
    if problem:
        complain(f"error: {problem}")
        return EXIT_ABORTED
    return EXIT_PASS


def add_commands(subparsers: Any) -> None:
    serve = subparsers.add_parser(
        "serve",
        help="browse results in a web browser, and read them over an HTTP JSON API",
        description="Serve pages and a JSON API over a results database. Pages only read it. "
        "It listens on this computer only, unless --public is given; there is no login.",
        epilog="Pages, endpoints and security: docs/web.md.",
    )
    serve.add_argument("--db", default=DEFAULT_DB, help=f"results database (default: {DEFAULT_DB})")
    serve.add_argument("--host", default=DEFAULT_HOST, help=f"address to listen on (default: {DEFAULT_HOST}, this computer only)")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port to listen on (default: {DEFAULT_PORT}; 0 means any free port)")
    serve.add_argument("--public", action="store_true", help="allow a --host other computers can reach; anyone who reaches it sees everything")
    serve.add_argument("--allow-runs", action="store_true", help="let the browser start runs, one at a time, from the plans in --plans-dir")
    serve.add_argument("--plans-dir", metavar="DIR", help="with --allow-runs, the folder of .json plans the browser may choose from")
    serve.add_argument("--quiet", action="store_true", help="do not print a line for every request")
    serve.set_defaults(func=cmd_serve)
