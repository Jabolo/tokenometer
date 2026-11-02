"""Coordinate ledger mutations, snapshots and their publication across processes.

The lock file is persistent: deleting it while a process holds a lock would
allow another process to lock a different inode and defeat mutual exclusion.
An inherited descriptor allows a scheduler to retain the same kernel lock
while a refresh subprocess works and until the resulting data is published.
"""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
import time

from .ledger import _private_parent


def _valid_lock_file(info):
    return (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1)


@contextmanager
def ledger_lock(ledger_path, *, timeout=30.0, inherited_fd=None):
    """Lock the ledger's private sibling file, yielding its open descriptor.

    Hold this lock through ingestion, snapshot and export; a publisher must
    retain it until publication finishes. Pass the yielded descriptor to a
    subprocess explicitly (e.g. ``pass_fds``), then supply ``inherited_fd``
    there. The child verifies and locks the descriptor without unlocking the
    shared open-file description. No environment variable grants lock access.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 <= timeout < float('inf'):
        raise ValueError('Invalid ledger lock timeout')
    ledger_path = Path(ledger_path)
    parent = _private_parent(ledger_path, create=True)
    lock_path = parent / (ledger_path.name + '.lock')
    borrowed = inherited_fd is not None
    if borrowed:
        if type(inherited_fd) is not int or inherited_fd < 0:
            raise ValueError('Invalid inherited ledger lock')
        try:
            fd = os.dup(inherited_fd)
        except OSError:
            raise ValueError('Invalid inherited ledger lock') from None
    else:
        try:
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        except OSError:
            raise ValueError('Cannot open ledger lock') from None
    try:
        try:
            actual = os.fstat(fd)
            named = lock_path.lstat()
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            if (not _valid_lock_file(actual) or not _valid_lock_file(named)
                    or (actual.st_dev, actual.st_ino) != (named.st_dev, named.st_ino)
                    or flags & os.O_ACCMODE != os.O_RDWR):
                raise ValueError('Invalid ledger lock file')
        except OSError:
            raise ValueError('Invalid ledger lock file') from None
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Ledger lock is busy') from None
                time.sleep(min(0.05, remaining))
        # Catch replacement before proceeding; never delete the lock file.
        try:
            named = lock_path.lstat()
            if not _valid_lock_file(named) or (actual.st_dev, actual.st_ino) != (named.st_dev, named.st_ino):
                raise ValueError('Ledger lock file changed')
        except OSError:
            raise ValueError('Ledger lock file changed') from None
        yield fd
    finally:
        # Closing the child's duplicate preserves the parent's inherited lock.
        # Explicit LOCK_UN here would release the shared open description.
        os.close(fd)
