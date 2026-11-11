"""Process-safe, reentrant serialization for local JSON authority.

Locks cover complete read-modify-write operations. No method holds more than
one state-file lock: callers finish ledger writes before commit writes.
POSIX flock releases on process death; unsupported platforms refuse writes.
"""
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
import os
import threading

_registry_guard = threading.Lock()
_registry = {}
_local = threading.local()


@contextmanager
def state_lock(path):
    import fcntl  # Refuse unsupported platforms rather than silently racing.
    path = Path(path).absolute()
    key = str(path)
    with _registry_guard:
        lock = _registry.setdefault(key, threading.RLock())
    with lock:
        held = getattr(_local, 'held', None)
        if held is None:
            held = _local.held = set()
        if key in held:
            yield
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path) + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            held.add(key)
            try:
                yield
            finally:
                held.remove(key)
        finally:
            os.close(fd)


def serialized_write(fn):
    @wraps(fn)
    def wrapped(self, *args, **kwargs):
        with state_lock(self.path):
            return fn(self, *args, **kwargs)
    return wrapped
