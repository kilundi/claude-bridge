#!/usr/bin/env python3
"""A single advisory lock shared by the watcher and the bridge launcher.

Any process that is allowed to *run* the work-order watcher holds an exclusive
flock on ``watcher.lock`` for its whole lifetime.  Everything that might start a
watcher (``watcher.py`` itself, ``watcher.sh start`` and ``bridge.py``'s
``start_bridge``) goes through this same lock, so two starts racing at the same
instant can never both begin work.

The lock is an OS ``flock`` on the open file description, so it disappears on its
own when the holder exits (even on a crash) - there is no stale lock file to
clean up.
"""
from __future__ import annotations

import fcntl
import os


def acquire(path: str, fd: int | None = None) -> int | None:
    """Take the lock without blocking.

    Returns the locked file descriptor, or ``None`` if another process holds it.

    ``fd`` is an already-open, already-locked descriptor inherited from a parent
    (watcher.sh passes fd 9): it lives on the *same* open file description as the
    parent's lock, so re-locking it simply succeeds.
    """
    if fd is None:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        opened = True
    else:
        opened = False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        if opened:
            try:
                os.close(fd)
            except OSError:
                pass
        return None
    return fd


def locked_by_other(path: str) -> bool:
    """True if some other process currently holds the lock (checked, then freed)."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    fcntl.flock(fd, fcntl.LOCK_UN)
    return False


def release(fd: int | None) -> None:
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass
