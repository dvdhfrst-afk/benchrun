"""benchrun: an automated bench test runner.

Talks to instruments with SCPI (Standard Commands for Programmable Instruments)
over a raw TCP socket, runs JSON test plans per unit serial number, and records
every measurement in SQLite.
"""

__version__ = "2.0.0"
