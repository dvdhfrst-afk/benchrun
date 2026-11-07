"""Switch and multiplexer driver.

Command forms follow the 34970A/34972A Command Reference and the channel list
syntax in the 34972A User's Guide, document 34972-90001, Edition 6. See
docs/drivers.md. Never run on the hardware.
"""

from __future__ import annotations

from typing import Set

from benchrun.drivers.base import Instrument, action, parse_on_off, reading, whole_number
from benchrun.errors import BadResponse, InstrumentFault


class Switch(Instrument):
    """A relay switch or multiplexer, with channels numbered like ``101``.

    Every channel this driver closed is opened again when a run ends, and each
    is confirmed open. A channel closed by raw ``write`` is not known to the
    driver. If a plan does that, set the ``open_all`` command for the card, such
    as ``ROUT:OPEN (@101:120)``, and it is sent as well.
    """

    COMMANDS = {
        **Instrument.COMMANDS,
        "close": "ROUT:CLOS (@{channel:d})",
        "close_only": "ROUT:CLOS:EXCL (@{channel:d})",
        "open": "ROUT:OPEN (@{channel:d})",
        "is_closed": "ROUT:CLOS? (@{channel:d})",
        "open_all": "",
    }

    HAS_OUTPUT = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._closed: Set[int] = set()

    def reset(self) -> None:
        super().reset()
        self._closed.clear()

    def _channel(self, channel: float) -> int:
        return whole_number(channel, "channel", self.name)

    @action
    def close_channel(self, channel: int) -> None:
        """Close one channel and leave the others as they are."""
        number = self._channel(channel)
        self._closed.add(number)  # Noted first: a lost reply may still have closed it.
        self.command("close", channel=number)

    @action
    def close_only(self, channel: int) -> None:
        """Open every other channel on the card, then close this one."""
        number = self._channel(channel)
        self._closed.add(number)
        self.command("close_only", channel=number)
        self._closed = {number}

    @action
    def open_channel(self, channel: int) -> None:
        number = self._channel(channel)
        self.command("open", channel=number)
        self._closed.discard(number)

    @reading("")
    def is_closed(self, channel: int) -> float:
        """1 when the channel is closed, else 0."""
        return self.number("is_closed", channel=self._channel(channel))

    def safe_state(self) -> None:
        if self.commands["open_all"]:
            self.command("open_all")
        for number in sorted(self._closed):
            self.command("open", channel=number)
            query = self._render("is_closed", channel=number)
            reply = self._ask(query)
            state = parse_on_off(reply)
            if state is None:
                raise BadResponse(f"cannot read channel state {reply[:80]!r} in response to {query!r}", self.name)
            if state:
                raise InstrumentFault(f"channel {number} still reads closed after being opened", self.name)
        self._closed.clear()
