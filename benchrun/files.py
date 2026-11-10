"""Writing files so that a crash or a full disk never leaves half of one behind."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import IO, Any, Iterator, Union


@contextlib.contextmanager
def atomic_open(path: Union[str, Path], binary: bool = False) -> Iterator[IO[Any]]:
    """A handle on a temporary file that replaces ``path`` only if the block finishes.

    The temporary file sits in the same folder, because a rename is only atomic
    within one file system. Text is UTF-8 and line endings are written as given
    on every platform. If the block raises, ``path`` is left exactly as it was.
    """
    target = Path(path)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        if binary:
            handle = temporary.open("wb")
        else:
            handle = temporary.open("w", encoding="utf-8", newline="")
        with handle:
            yield handle
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(temporary, target)
        except PermissionError as exc:
            raise PermissionError(f"cannot replace {target}; is it open in another program? ({exc})") from exc
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def atomic_write_text(path: Union[str, Path], text: str) -> None:
    """Replace ``path`` with ``text`` all at once, or leave it exactly as it was."""
    with atomic_open(path) as handle:
        handle.write(text)
