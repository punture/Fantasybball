"""Atomic file writes for state that several FBM processes read while another rewrites it (the league snapshot,
Flaim's remembered week windows): a reader sees the old file or the new one, never a truncated one."""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path


def replace_retrying(src: str | Path, dst: Path) -> None:
    """os.replace, retried briefly: on Windows it fails while another process has the target open for reading."""
    for attempt in range(40):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == 39:
                raise
            time.sleep(0.05)


def write_text_atomic(path: Path, text: str) -> None:
    """Write `text` (UTF-8) to `path` via a temp file in the same folder, fsync'd, then renamed over the target. The
    temp file comes from mkstemp, so the result is private to the user (0600 on POSIX) from its first byte."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        replace_retrying(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
