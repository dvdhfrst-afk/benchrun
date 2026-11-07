"""A misbehaving instrument in its own process, reached over a real TCP socket.

It imports nothing from benchrun, so it cannot share a bug or a code path with
the code under test. Everything it receives goes to a journal file, which is how
a test proves what reached the instrument after benchrun has exited.

Usage: fake_instrument.py --journal FILE [--idn TEXT] [--value N] [--fault KIND:AFTER]

The fault fires on the read after AFTER good reads. Kinds:

    silent_once  that read gets no reply; everything else keeps working
    late         that read's reply arrives 1 second late
    partial      half a number with no line end, then nothing more for that read
    garbled      bytes that mean nothing
    with_unit    a reply with a unit stuck on the end, such as 5.000V
    crlf         readings end in a carriage return and a line feed
    flood        data with no line end until the connection is closed
    hang         stops answering anything, but stays connected
    drop         closes the connection, and accepts new ones
    die          the whole process ends at once
    stuck_on     (AFTER is ignored) the output reads on after being switched off
    refuse_off   (AFTER is ignored) rejects switching the output off
"""

import argparse
import os
import socket
import threading
import time

OVERLOAD = "9.90000000E+37"


class Instrument:
    def __init__(self, args):
        self.idn = args.idn
        self.value = args.value
        self.kind, _, after = (args.fault or "none:0").partition(":")
        self.after = int(after or 0)
        self.reads = 0
        self.fired = False
        self.hung = False
        self.output = False
        self.errors = []
        self.journal = open(args.journal, "a", encoding="utf-8", buffering=1)

    def note(self, text):
        self.journal.write(text + "\n")

    def due(self):
        """True once, on the read the fault applies to."""
        if self.fired or self.kind in ("none", "stuck_on", "refuse_off"):
            return False
        if self.reads >= self.after:
            self.fired = True
            return True
        return False

    def read(self, conn):
        """Answer a measurement. Returns False when the connection must end."""
        if self.due():
            self.note(f"FAULT {self.kind}")
            if self.kind == "silent_once":
                self.errors.append('-420,"Query UNTERMINATED"')
                return True
            if self.kind == "late":
                time.sleep(1.0)
            elif self.kind == "partial":
                conn.sendall(b"5.00")
                return True
            elif self.kind == "garbled":
                conn.sendall(b"\xff\xfe#@!\n")
                return True
            elif self.kind == "crlf":
                conn.sendall(f"{self.value:.9E}\r\n".encode())
                return True
            elif self.kind == "with_unit":
                conn.sendall(f"{self.value:.3f}V\n".encode())
                return True
            elif self.kind == "flood":
                try:
                    while True:
                        conn.sendall(b"x" * 65536)
                except OSError:
                    return False
            elif self.kind == "hang":
                self.hung = True
                return True
            elif self.kind == "drop":
                return False
            elif self.kind == "die":
                self.note("DIED")
                os._exit(9)
        self.reads += 1
        conn.sendall(f"{self.value:.9E}\n".encode())
        return True

    def handle(self, line, conn):
        word = line.strip().upper()
        if self.hung:
            return True
        if word == "*IDN?":
            conn.sendall((self.idn + "\n").encode())
        elif word == "*RST":
            self.set_output(False)
        elif word == "*CLS":
            self.errors.clear()
        elif word == "SYST:ERR?":
            reply = self.errors.pop(0) if self.errors else '+0,"No error"'
            conn.sendall((reply + "\n").encode())
        elif word in ("OUTP ON", "INP ON"):
            self.set_output(True)
        elif word in ("OUTP OFF", "INP OFF"):
            if self.kind == "refuse_off":
                self.errors.append('-221,"Settings conflict"')
            else:
                self.set_output(False)
        elif word in ("OUTP?", "INP?"):
            reply = "1" if self.output or self.kind == "stuck_on" else "0"
            conn.sendall((reply + "\n").encode())
        elif word.startswith(("VOLT ", "CURR ", "CONF:")) or word.split(" ")[0].endswith("NPLC"):
            pass
        elif word in ("READ?", "MEAS:VOLT?", "MEAS:CURR?"):
            return self.read(conn)
        else:
            self.errors.append(f'-113,"Undefined header; {word[:40]}"')
        return True

    def set_output(self, state):
        self.output = state
        self.note(f"OUTPUT {'on' if state else 'off'}")

    def serve(self, conn):
        self.note("CONNECT")
        buffer = b""
        try:
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    text = line.decode("ascii", "replace").rstrip("\r")
                    self.note(f"RX {text}")
                    if not self.handle(text, conn):
                        return
        except OSError:
            pass
        finally:
            self.note("DISCONNECT")
            conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--journal", required=True)
    parser.add_argument("--idn", default="Example,FAKE-1,0001,1.0")
    parser.add_argument("--value", type=float, default=5.0)
    parser.add_argument("--fault")
    args = parser.parse_args()
    instrument = Instrument(args)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    print(f"PORT {listener.getsockname()[1]}", flush=True)
    while True:
        conn, _ = listener.accept()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        threading.Thread(target=instrument.serve, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
