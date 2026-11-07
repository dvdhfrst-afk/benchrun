"""Line-oriented transports for SCPI traffic.

The raw socket style (commonly port 5025) is plain text, one program message
per line, terminated by a newline. Drivers only see the small ``Transport``
interface, so another link type can be added without touching them.
"""

from __future__ import annotations

import select
import socket
import time
from datetime import datetime, timezone
from typing import IO, Optional, Tuple

from benchrun.errors import CommandRefused, ConnectionLost, PlanError, ResponseTimeout

DEFAULT_PORT = 5025
DEFAULT_TIMEOUT_S = 5.0
MAX_LINE_BYTES = 16 * 1024 * 1024


def encode_command(text: str) -> bytes:
    """One program message as bytes, or refuse it.

    A line break would turn one message into two, and a character replaced on
    the way out could turn a setting into a query, so neither is ever sent.
    """
    if any(ord(ch) < 32 or ord(ch) > 126 for ch in text):
        raise CommandRefused(f"command {text!r} has a line break or a non-ASCII character, so it was not sent")
    return text.encode("ascii") + b"\n"


def parse_address(address: str) -> Tuple[str, int]:
    """Split ``host``, ``host:port`` or ``[v6-host]:port`` into host and port."""
    text = address.strip()
    if not text:
        raise PlanError(["instrument address is empty"])
    if text.startswith("["):
        host, sep, rest = text[1:].partition("]")
        if not sep or not host:
            raise PlanError([f"bad instrument address {address!r}"])
        if not rest:
            return host, DEFAULT_PORT
        if not rest.startswith(":"):
            raise PlanError([f"bad instrument address {address!r}"])
        port_text = rest[1:]
    else:
        host, sep, port_text = text.partition(":")
        if not host or ":" in port_text:
            raise PlanError([f"bad instrument address {address!r}"])
        if not sep:
            return host, DEFAULT_PORT
    if not port_text.isdigit() or not 0 < int(port_text) < 65536:
        raise PlanError([f"bad port in instrument address {address!r}"])
    return host, int(port_text)


class Transport:
    """What a driver needs from a link to an instrument."""

    description = "transport"

    def open(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def write_line(self, text: str) -> None:
        raise NotImplementedError

    def read_line(self) -> str:
        raise NotImplementedError

    def discard_pending(self) -> str:
        """Drop and return anything already waiting to be read."""
        raise NotImplementedError


class TcpTransport(Transport):
    """SCPI over a raw TCP socket."""

    def __init__(self, host: str, port: int = DEFAULT_PORT, timeout_s: float = DEFAULT_TIMEOUT_S):
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self.description = f"{host}:{port}"
        self._sock: Optional[socket.socket] = None
        self._buffer = bytearray()

    def open(self) -> None:
        self.close()
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
        except OSError as exc:
            raise ConnectionLost(f"cannot connect to {self.description}: {exc}") from exc
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        self._buffer.clear()

    def _require_socket(self) -> socket.socket:
        if self._sock is None:
            raise ConnectionLost(f"connection to {self.description} is not open")
        return self._sock

    def write_line(self, text: str) -> None:
        message = encode_command(text)
        sock = self._require_socket()
        sock.settimeout(self.timeout_s)
        try:
            sock.sendall(message)
        except socket.timeout as exc:
            raise ResponseTimeout(f"timed out sending {text!r} to {self.description}") from exc
        except OSError as exc:
            raise ConnectionLost(f"connection to {self.description} lost sending {text!r}: {exc}") from exc

    def read_line(self) -> str:
        sock = self._require_socket()
        deadline = time.monotonic() + self.timeout_s
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                line = bytes(self._buffer[:newline])
                del self._buffer[: newline + 1]
                return line.decode("ascii", "replace").rstrip("\r")
            if len(self._buffer) > MAX_LINE_BYTES:
                raise ConnectionLost(f"response from {self.description} exceeds {MAX_LINE_BYTES} bytes")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ResponseTimeout(f"no response from {self.description} within {self.timeout_s:g} s")
            sock.settimeout(remaining)
            try:
                chunk = sock.recv(65536)
            except socket.timeout as exc:
                raise ResponseTimeout(f"no response from {self.description} within {self.timeout_s:g} s") from exc
            except OSError as exc:
                raise ConnectionLost(f"connection to {self.description} lost: {exc}") from exc
            if not chunk:
                raise ConnectionLost(f"connection to {self.description} closed by the instrument")
            self._buffer.extend(chunk)

    def discard_pending(self) -> str:
        sock = self._require_socket()
        discarded = bytes(self._buffer)
        self._buffer.clear()
        try:
            while select.select([sock], [], [], 0)[0]:
                chunk = sock.recv(65536)
                if not chunk:
                    raise ConnectionLost(f"connection to {self.description} closed by the instrument")
                discarded += chunk
                if len(discarded) > MAX_LINE_BYTES:
                    raise ConnectionLost(f"{self.description} keeps sending data nobody asked for")
        except OSError as exc:
            raise ConnectionLost(f"connection to {self.description} lost: {exc}") from exc
        return discarded.decode("ascii", "replace")


class LoggingTransport(Transport):
    """Wraps a transport and writes all traffic to a text stream."""

    def __init__(self, inner: Transport, label: str, stream: IO[str]):
        self.inner = inner
        self.label = label
        self.stream = stream
        self.description = inner.description

    def _log(self, direction: str, text: str) -> None:
        """A full disk must not stop a run, or stop outputs being switched off."""
        stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        try:
            self.stream.write(f"{stamp} {self.label} {direction} {text}\n")
            self.stream.flush()
        except (OSError, ValueError):
            pass

    def open(self) -> None:
        self._log("--", f"open {self.inner.description}")
        self.inner.open()

    def close(self) -> None:
        self._log("--", "close")
        self.inner.close()

    def write_line(self, text: str) -> None:
        self._log(">>", text)
        self.inner.write_line(text)

    def read_line(self) -> str:
        try:
            line = self.inner.read_line()
        except Exception as exc:
            self._log("!!", str(exc))
            raise
        self._log("<<", line)
        return line

    def discard_pending(self) -> str:
        discarded = self.inner.discard_pending()
        if discarded:
            self._log("xx", discarded.rstrip("\r\n"))
        return discarded
