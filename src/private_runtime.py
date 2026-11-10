"""Shared bounded operational I/O and OS-released locks, not orchestration."""
from contextlib import contextmanager
import json
import errno
import os
from pathlib import Path
import tempfile
import sys


def safe_path(value):
    candidate = Path(value)
    if '..' in candidate.parts:
        raise ValueError('Parent traversal in operational path')
    path = Path(os.path.abspath(candidate))
    if any(part.is_symlink() or (hasattr(os.path, 'isjunction') and os.path.isjunction(part))
           for part in (path, *path.parents)):
        raise ValueError('Link in operational path')
    return path


def fsync_directory(path):
    if os.name != 'nt':
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic_bytes(path, raw):
    path = safe_path(path)
    descriptor, name = tempfile.mkstemp(prefix='.state-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        safe_path(path)
        os.replace(name, path)
        durable = os.name != 'nt'
        try:
            fsync_directory(path.parent)
        except OSError:
            durable = False  # Bytes already published; do not misreport failure.
        return durable
    finally:
        if os.path.exists(name):
            os.unlink(name)


def atomic_json(path, document):
    return atomic_bytes(path, json.dumps(document, ensure_ascii=True, sort_keys=True).encode('utf-8'))


def bounded_read(path, limit):
    path = safe_path(path)
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError('Invalid or oversize operational input')
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('Operational input grew beyond limit')
    return raw


@contextmanager
def file_lock(path):
    path = safe_path(path)
    with path.open('a+b') as stream:
        if os.name == 'nt':
            import msvcrt
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            windows_lock(stream, msvcrt)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == 'nt':
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    # Keep the lock file inode stable; the OS releases its lock on process death.


@contextmanager
def operational_lock(root):
    root = operational_root(root)
    root.mkdir(parents=True, exist_ok=True)
    with file_lock(root / '.operation.lock'):
        yield


def operational_root(value):
    path = safe_path(value)
    repo = Path(__file__).absolute().parents[1]
    if path.is_relative_to(repo):
        relative = path.relative_to(repo)
        if not relative.parts or relative.parts[0] not in {'runtime', 'cache', 'data', 'reports'}:
            raise ValueError('Operational output must use an ignored runtime directory')
    return path


def windows_lock(stream, backend):
    try:
        backend.locking(stream.fileno(), backend.LK_NBLCK, 1)
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EDEADLK, errno.EAGAIN} or getattr(exc, "winerror", None) in {33, 36}:
            raise BlockingIOError("Operational lock is busy") from None
        raise


def registry_root(value=None):
    """One persistent registry for all workflows; explicit root wins over env."""
    configured = value if value is not None else os.environ.get('DONATION_RECEIPTS_REGISTRY_ROOT')
    if configured is not None:
        if not str(configured).strip():
            raise ValueError('Empty registry root')
        return operational_root(configured)
    if sys.platform == 'win32':
        local = os.environ.get('LOCALAPPDATA')
        if not local or not Path(local).is_absolute():
            raise ValueError('Absolute LOCALAPPDATA required for Windows registry')
        return operational_root(Path(local) / 'DonationReceiptsRuntime' / 'registry')
    return operational_root(Path(__file__).absolute().parents[1] / 'runtime' / 'registry')
