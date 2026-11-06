"""Serves SCPI devices over raw TCP sockets on the loopback interface."""

from __future__ import annotations

import os
import socket
import threading
from typing import Set

from benchrun.sim.instruments import SimInstrument
from benchrun.sim.scpi import DeviceDisconnect, DeviceHang

LOOPBACK = "127.0.0.1"
MAX_MESSAGE_BYTES = 1024 * 1024


class InstrumentServer:
    """Accepts connections for one device. Bound to 127.0.0.1 only."""

    def __init__(self, device: SimInstrument, lock: threading.Lock, port: int = 0):
        self.device = device
        self.lock = lock
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name != "nt":
            # On Windows this option would let two servers share a port.
            self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._listener.bind((LOOPBACK, port))
            self._listener.listen(8)
        except OSError:
            self._listener.close()
            raise
        self.port: int = self._listener.getsockname()[1]
        self._connections: Set[socket.socket] = set()
        self._state_lock = threading.Lock()
        self._stopping = False
        self._thread = threading.Thread(target=self._accept_loop, daemon=True, name=f"sim-{device.kind}")

    @property
    def address(self) -> str:
        return f"{LOOPBACK}:{self.port}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopping = True
        # Connect to ourselves so a blocked accept() returns on every platform.
        try:
            socket.create_connection((LOOPBACK, self.port), timeout=0.5).close()
        except OSError:
            pass
        self._thread.join(timeout=2.0)
        self._listener.close()
        with self._state_lock:
            connections = list(self._connections)
        for conn in connections:
            _close_quietly(conn)

    def _accept_loop(self) -> None:
        while not self._stopping:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            if self._stopping or self.device.dead:
                _close_quietly(conn)
                continue
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self._state_lock:
                self._connections.add(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        buffer = bytearray()
        try:
            while not self._stopping:
                try:
                    chunk = conn.recv(65536)
                except OSError:
                    return
                if not chunk:
                    return
                buffer.extend(chunk)
                while True:
                    newline = buffer.find(b"\n")
                    if newline < 0:
                        break
                    line = bytes(buffer[:newline]).decode("ascii", "replace").rstrip("\r")
                    del buffer[: newline + 1]
                    if not self._answer(conn, line):
                        return
                if len(buffer) > MAX_MESSAGE_BYTES:
                    with self.lock:
                        self.device.push_error(-223, "Too much data")
                    buffer.clear()
        finally:
            with self._state_lock:
                self._connections.discard(conn)
            _close_quietly(conn)

    def _answer(self, conn: socket.socket, line: str) -> bool:
        """Handle one message. Returns False when the connection must end."""
        with self.lock:
            if self.device.dead:
                return False
            if self.device.hung:
                return True
            try:
                response = self.device.process(line)
            except DeviceDisconnect:
                return False
            except DeviceHang:
                return True
        if response is not None:
            try:
                conn.sendall(response.encode("ascii", "replace") + b"\n")
            except OSError:
                return False
        return True


def _close_quietly(conn: socket.socket) -> None:
    try:
        conn.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        conn.close()
    except OSError:
        pass
