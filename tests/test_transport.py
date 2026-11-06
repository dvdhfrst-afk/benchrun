"""The TCP transport against a tiny scripted socket server."""

import io
import socket
import threading
import time
import unittest

from benchrun.errors import ConnectionLost, PlanError, ResponseTimeout
from benchrun.transport import DEFAULT_PORT, LoggingTransport, TcpTransport, parse_address


class ScriptedServer:
    """Accepts one connection and runs a function on it."""

    def __init__(self, handler):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.handler = handler
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        conn, _ = self.listener.accept()
        try:
            self.handler(conn)
        finally:
            conn.close()

    def close(self):
        self.listener.close()
        self.thread.join(timeout=2.0)


class ParseAddress(unittest.TestCase):
    def test_good_addresses(self):
        for text, want in (
            ("192.0.2.1:5025", ("192.0.2.1", 5025)),
            ("bench-dmm", ("bench-dmm", DEFAULT_PORT)),
            ("  host:80 ", ("host", 80)),
            ("[::1]:5025", ("::1", 5025)),
            ("[::1]", ("::1", DEFAULT_PORT)),
        ):
            self.assertEqual(parse_address(text), want, text)

    def test_bad_addresses(self):
        for text in ("", ":5025", "host:", "host:abc", "host:0", "host:70000", "::1", "[::1", "[::1]x", "[]:1"):
            with self.assertRaises(PlanError, msg=text):
                parse_address(text)


class TcpBehavior(unittest.TestCase):
    def transport(self, handler, timeout_s=0.5):
        server = ScriptedServer(handler)
        self.addCleanup(server.close)
        link = TcpTransport("127.0.0.1", server.port, timeout_s)
        link.open()
        self.addCleanup(link.close)
        return link

    def test_write_ends_with_newline_and_read_strips_it(self):
        received = []

        def handler(conn):
            received.append(conn.recv(100))
            conn.sendall(b"hello\r\n")

        link = self.transport(handler)
        link.write_line("*IDN?")
        self.assertEqual(link.read_line(), "hello")
        self.assertEqual(received, [b"*IDN?\n"])

    def test_response_split_across_packets(self):
        def handler(conn):
            conn.sendall(b"par")
            time.sleep(0.05)
            conn.sendall(b"tial\n")

        self.assertEqual(self.transport(handler).read_line(), "partial")

    def test_two_lines_in_one_packet_stay_separate(self):
        def handler(conn):
            conn.sendall(b"one\ntwo\n")

        link = self.transport(handler)
        self.assertEqual(link.read_line(), "one")
        self.assertEqual(link.read_line(), "two")

    def test_timeout_is_a_response_timeout(self):
        release = threading.Event()
        self.addCleanup(release.set)

        link = self.transport(lambda conn: release.wait(5), timeout_s=0.05)
        with self.assertRaises(ResponseTimeout):
            link.read_line()

    def test_trickling_bytes_cannot_outlast_the_deadline(self):
        stop = threading.Event()
        self.addCleanup(stop.set)

        def handler(conn):
            while not stop.is_set():
                conn.sendall(b"x")
                time.sleep(0.01)

        link = self.transport(handler, timeout_s=0.1)
        started = time.monotonic()
        with self.assertRaises(ResponseTimeout):
            link.read_line()
        self.assertLess(time.monotonic() - started, 2.0)
        stop.set()

    def test_peer_close_is_connection_lost(self):
        link = self.transport(lambda conn: None)
        with self.assertRaises(ConnectionLost):
            link.read_line()

    def test_refused_connection_names_the_address(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        link = TcpTransport("127.0.0.1", port, 0.5)
        with self.assertRaisesRegex(ConnectionLost, f"127.0.0.1:{port}"):
            link.open()

    def test_use_before_open_is_connection_lost(self):
        link = TcpTransport("127.0.0.1", 1, 0.5)
        with self.assertRaises(ConnectionLost):
            link.write_line("x")
        with self.assertRaises(ConnectionLost):
            link.read_line()

    def test_close_twice_is_harmless(self):
        link = self.transport(lambda conn: None)
        link.close()
        link.close()

    def test_discard_pending_returns_waiting_data(self):
        ready = threading.Event()

        def handler(conn):
            conn.sendall(b"stale\n")
            ready.set()
            conn.recv(10)

        link = self.transport(handler)
        ready.wait(2)
        deadline = time.monotonic() + 2
        found = ""
        while not found and time.monotonic() < deadline:
            found = link.discard_pending()
        self.assertEqual(found, "stale\n")
        link.close()

    def test_discard_pending_notices_a_closed_peer(self):
        link = self.transport(lambda conn: None)
        deadline = time.monotonic() + 2
        with self.assertRaises(ConnectionLost):
            while time.monotonic() < deadline:
                link.discard_pending()
                time.sleep(0.005)


class Logging(unittest.TestCase):
    def test_traffic_is_written_with_direction_and_label(self):
        def handler(conn):
            conn.recv(100)
            conn.sendall(b"reply\n")

        server = ScriptedServer(handler)
        self.addCleanup(server.close)
        stream = io.StringIO()
        link = LoggingTransport(TcpTransport("127.0.0.1", server.port, 1.0), "dmm", stream)
        link.open()
        link.write_line("READ?")
        self.assertEqual(link.read_line(), "reply")
        link.close()
        text = stream.getvalue()
        self.assertIn("dmm >> READ?", text)
        self.assertIn("dmm << reply", text)
        self.assertIn("dmm -- close", text)

    def test_read_failure_is_logged_and_still_raised(self):
        server = ScriptedServer(lambda conn: None)
        self.addCleanup(server.close)
        stream = io.StringIO()
        link = LoggingTransport(TcpTransport("127.0.0.1", server.port, 1.0), "dmm", stream)
        link.open()
        with self.assertRaises(ConnectionLost):
            link.read_line()
        link.close()
        self.assertIn("!!", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
