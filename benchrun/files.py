"""Writing files so that a crash or a full disk never leaves half of one behind."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Union


def atomic_write_text(path: Union[str, Path], text: str) -> None:
    """Replace ``path`` with ``text`` all at once, or leave it exactly as it was.

    The temporary file sits in the same folder, because a rename is only atomic
    within one file system. Line endings are written as given on every platform.
    """
    target = Path(path)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)
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
