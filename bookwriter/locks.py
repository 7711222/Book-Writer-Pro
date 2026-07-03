"""Cross-process advisory locks for book project directories.

The HTTP server (``bookwriter.server.service``) and the MCP server
(``bookwriter.mcp_server``) default to the *same* data directory, so two
separate processes can mutate one book's ``book.json`` / ``state.json`` /
chapter files concurrently. The in-memory one-job-per-book guard
(``EventBroker.start_job``) only covers a single process; writers in different
processes coordinate through an OS-level advisory lock on
``<book_dir>/.write.lock``.

The lock is held on an open file handle (``msvcrt.locking`` on Windows,
``fcntl.flock`` elsewhere), so the OS releases it automatically when the
holding process exits or crashes — no stale-lockfile / PID-liveness handling
is needed. The PID is written into the file purely as a debugging aid.

Usage::

    from bookwriter.locks import BookLockedError, book_write_lock

    with book_write_lock(book_dir, timeout=0):
        ... mutate the book's on-disk state ...
"""
from __future__ import annotations

import contextlib
import os
import time
from typing import Iterator

# Lives inside the book's project dir; its *name* is not a valid chapter/meta
# file so it never collides with the store layout.
LOCK_FILENAME = ".write.lock"


class BookLockedError(RuntimeError):
    """Another process (or thread) currently holds the book's write lock."""


if os.name == "nt":
    import msvcrt

    def _try_lock(f) -> bool:
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(f) -> None:
        with contextlib.suppress(OSError):
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(f) -> bool:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(f) -> None:
        with contextlib.suppress(OSError):
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def book_write_lock(book_dir: str, timeout: float = 0.0,
                    poll: float = 0.05) -> Iterator[None]:
    """Advisory cross-process writer lock for one book directory.

    Retries acquisition for up to ``timeout`` seconds (``0`` = a single
    non-blocking attempt), then raises :class:`BookLockedError`. Reentrant
    acquisition is NOT supported — a second acquisition from the same process
    fails exactly like one from another process, which is what the
    one-writer-per-book guarantee wants.
    """
    os.makedirs(book_dir, exist_ok=True)
    path = os.path.join(book_dir, LOCK_FILENAME)
    f = open(path, "a+", encoding="utf-8")
    try:
        deadline = time.monotonic() + max(0.0, timeout)
        while not _try_lock(f):
            if time.monotonic() >= deadline:
                raise BookLockedError(
                    "This book is being written by another process; try again shortly."
                )
            time.sleep(poll)
        try:
            # Best-effort debugging aid: record who holds the lock.
            with contextlib.suppress(OSError):
                f.seek(0)
                f.truncate()
                f.write(str(os.getpid()))
                f.flush()
            yield
        finally:
            _unlock(f)
    finally:
        f.close()
